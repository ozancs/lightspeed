"""Rat Rig V-Core 3.1 300 Enclosure 2.0 raw CAD GLB -> web GLB + JSON, same look as voron24.

Source: the official Fusion share "Rat Rig V-Core 3.1 - 300 - Enclosure 2.0" exported to STEP by the owner
(models/source/ratrig/vcore31_300.step, a copy of the downloaded .stp). Rat Rig publishes the V-Core 3 files
under CC BY-NC-SA 4.0 (github.com/Rat-Rig/V-core-3 LICENSE). Converted with cascadio to models/raw/ratrig_raw.glb.

Output coordinates: mm, Z up, front at -Y, bed plate front-left corner at x/y 0, PEI top at z 0.

    python -I build_ratrig.py [--list]
"""
import json
import os
import re
import sys

import numpy as np
import trimesh

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_model import (RAW, OUT, BLACK, DARK_GREY, RAIL_STEEL, PEI_GOLD, RED,  # noqa: E402
                         part_color, quant)
import lod  # noqa: E402

KEY = 'ratrig'
SRC = RAW + r"\ratrig_raw.glb"
METAL = (92, 96, 102)
BED_ALU = (64, 66, 70)
GLASS = (225, 232, 238)
MIN_SIZE = 8.0

DROP_PATH = re.compile(
    r"(^_ptfe_filament_guide|^Bowden Clamp|^Enclosure 2.0 Spool Holder|^Rear Shelf|^Wire Guide|^Y-Axis Endstop|"
    r"^_CoreXY Belts|Foam Seal|^Electronics Panel Assembly[^/]*/(?!panel_electronics)|"
    r"EVA3 Assembly v4:1/(PTFE Tube|Endstop Module|cable_guide))", re.I)
DROP_NAME = re.compile(
    r"(screw|\bnut\b|nut -|washer|t-nut|shim|spacer|insert|\bmagnet|dowel|pin -|endstop|ptfe|belt|cable|wire|"
    r"chain|switch)", re.I)
KEEP_NAME = re.compile(r"(^Lead Screw - |bearing|pulley|stepper|carriage|rail)", re.I)
EXTRUSION = re.compile(r"^(T-Slot 3030|V-Slot 2020)", re.I)
RAIL = re.compile(r"(Linear Rail|^Lead Screw - )", re.I)
GLASS_NAME = re.compile(r"^panel_enc2_(door|side|top|back)_", re.I)

BUDGET = 230000   # triangles, whole model (lod.py: per part, measured error)


def group_of(path):
    s = '/'.join(path)
    if s.startswith('EVA3 Assembly'):
        return 'toolhead'
    if s.startswith('X-Gantry Assembly'):
        return 'gantry_y'
    if re.match(r"(Bed Assembly|Z-Axis Arm Assembly)", s):
        return 'bed'
    if any(GLASS_NAME.search(n) for n in path):
        return 'glass'
    if re.search(r"(^Base Panel|^Electronics Panel Assembly|/(Front Door|Left Panel|Right Panel|Top Panel|Back Panel) - 300|"
                 r"3030 Panel Holder|Panel Magnet Holder)", s):
        return 'panels'
    if re.match(r"(Enclosure 2.0 Frame Assembly|Leadscrew Constraint|Z-Axis Transmission|CoreXY|Lead Screw - )", s):
        return 'frame'
    return None


def colour_of(grp, c, path):
    r, g, b = c
    leafs = path[-3:]
    if grp == 'glass':
        return GLASS
    if any(EXTRUSION.search(n) for n in leafs):
        return DARK_GREY
    if any(RAIL.search(n) for n in leafs):
        return RAIL_STEEL
    if grp == 'bed':
        if any('FlexPlate' in n for n in leafs) and r > 120 and b < 40:
            return PEI_GOLD
        if any(n.startswith('Bed Plate') for n in leafs):
            return BED_ALU
    if g > 200 and r < 100 and b < 60:     # Rat Rig green printed parts -> the owner's red accent
        return RED
    if r > 120 and g < 60 and b < 60:
        return RED
    if max(c) < 40:
        return BLACK
    if c == quant((64, 64, 64)) or c == quant((55, 55, 55)):   # black anodised plates, opaque panels
        return BLACK
    return METAL


def walk():
    sc = trimesh.load(SRC, force='scene')
    G = sc.graph
    parent = {e[1]: e[0] for e in G.to_edgelist()}
    for node in G.nodes_geometry:
        T, gname = G[node]
        path = []
        x = node
        while x in parent:
            path.append(x)
            x = parent[x]
        path = path[::-1][1:]          # drop the file root, root-first
        yield node, path, sc.geometry[gname], T


def main(listing=False):
    items = []
    for node, path, src, T in walk():
        m = src.copy()
        m.apply_transform(T)
        m.apply_scale(1000.0)
        items.append((path, src, m))
    # shift: bed plate front-left corner to x/y 0, PEI top to z 0
    plate = [m for p, _, m in items if p[-1].startswith('Bed Plate')]
    pei = [m for p, s, m in items if any('FlexPlate' in n for n in p) and part_color(s)[0] > 120]
    pmin = np.min([m.bounds[0] for m in plate], axis=0)
    ztop = max(m.bounds[1][2] for m in pei)
    shift = np.array([-pmin[0], -pmin[1], -ztop])

    buckets, meta_parts = [], {}
    kept = dropped = 0
    stats = {}
    for path, src, m in items:
        s = '/'.join(path)
        if DROP_PATH.search(s):
            dropped += 1
            continue
        if any(DROP_NAME.search(n) for n in path[-3:]) and not any(KEEP_NAME.search(n) for n in path[-2:]):
            dropped += 1
            continue
        grp = group_of(path)
        if grp is None or np.linalg.norm(m.extents) < MIN_SIZE:
            dropped += 1
            continue
        m.apply_translation(shift)
        col = colour_of(grp, quant(part_color(src)), path)
        kept += 1
        for tag, pat in (('nozzle', r"^EVA3 Assembly v4:1/Phaetus Rapido"), ('block', r"^EVA3 Assembly v4:1/_MGN12C Carriage"),
                         ('beam', r"^X-Gantry Assembly[^/]*/V-Slot 2020"), ('xrail', r"^X-Gantry Assembly[^/]*/MGN12R"),
                         ('pei', r"FlexPlate"), ('plate', r"/Bed Plate"), ('zcar', r"^Z-Axis Arm Assembly[^/]*/_MGN12C"),
                         ('zrail', r"MGN12R 400mm 3030"), ('extr', r"^Enclosure 2.0 Frame Assembly[^/]*/T-Slot 3030"),
                         ('yrail', r"MGN12R 350mm 3030"), ('front', r"panel_enc2_door")):
            if re.search(pat, s):
                meta_parts.setdefault(tag, []).append(m)
        buckets.append(((grp, col), m, False))
        if listing:
            k = (grp, '/'.join(path[:2])[:110])
            stats[k] = stats.get(k, 0) + len(m.faces)
    print('kept', kept, 'dropped', dropped, 'shift', shift.round(2).tolist())
    if listing:
        for (g, k), n in sorted(stats.items()):
            print('%-9s %7d  %s' % (g, n, k))
        return

    dec, _ = lod.decimate_parts(buckets, BUDGET, cache=KEY)
    merged = {k: trimesh.util.concatenate(v) for k, v in dec.items()}
    scene = trimesh.Scene()
    meta = {'groups': {}}
    for (g, c), mesh in sorted(merged.items(), key=lambda kv: kv[0]):
        # no welding across parts: every part stays its own island (the page moves short islands as one piece)
        hexc = '%02x%02x%02x' % c
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                baseColorFactor=[int(round(((x / 255.0) ** 2.2) * 255)) for x in c] + [255],
                metallicFactor=0.1, roughnessFactor=0.7))
        name = '%s__%s' % (g, hexc)
        scene.add_geometry(mesh, node_name=name, geom_name=name)
        gm = meta['groups'].setdefault(g, {'tri': 0, 'min': [1e9] * 3, 'max': [-1e9] * 3, 'colours': []})
        gm['tri'] += int(len(mesh.faces))
        gm['min'] = np.minimum(gm['min'], mesh.bounds[0]).round(1).tolist()
        gm['max'] = np.maximum(gm['max'], mesh.bounds[1]).round(1).tolist()
        gm['colours'].append(hexc)

    def bb(tag):
        v = np.vstack([m.vertices for m in meta_parts[tag]])
        return v.min(0), v.max(0)
    r1 = lambda a: [round(float(x), 1) for x in a]
    nv = np.vstack([m.vertices for m in meta_parts['nozzle']])
    lo = nv[:, 2].min()
    tip = [*nv[nv[:, 2] < lo + 0.3][:, :2].mean(0), lo]
    bmin, bmax = bb('block')
    emin, emax = bb('extr')
    beam, xr, yr = bb('beam'), bb('xrail'), bb('yrail')
    pei, plate = bb('pei'), bb('plate')
    zcar_lo = bb('zcar')[0][2]
    zrail_lo = bb('zrail')[0][2]
    fr = bb('front')
    P = 30.0
    meta.update({
        'source': 'Rat Rig V-Core 3.1 300 Enclosure 2.0, official Fusion share exported to STEP; '
                  'Rat Rig V-Core 3 files are CC BY-NC-SA 4.0 (github.com/Rat-Rig/V-core-3)',
        'units': 'mm, Z up, front -Y; CAD shifted by %s so the bed plate front-left corner is x/y 0 and the PEI top z 0'
                 % [round(float(x), 2) for x in shift],
        'build_volume': [300, 300, 300],
        'kinematics': {'bed': 'z', 'gantry_y': 'y', 'toolhead': 'x'},
        'pose': 'CAD pose: bed at the top (nozzle touching the PEI), carriage centred on X',
        'extrusion': {'outer_min': r1(emin), 'outer_max': r1(emax), 'profile': P,
                      'x0': round(emin[0] + P, 1), 'x1': round(emax[0] - P, 1),
                      'front': round(emin[1] + P, 1), 'rear': round(emax[1] - P, 1),
                      'bottom': round(emin[2] + P, 1), 'top': round(emax[2] - P, 1),
                      'note': 'outer 3030 cube incl. the Enclosure 2.0 top extension; inner rear sub-frame and '
                              'the 3030 ring carrying the Y rails (outer z %s..%s) are inside it'
                              % (round(480 + shift[2], 1), round(510 + shift[2], 1))},
        'front_face': {'y': r1(fr[0])[1], 'note': 'outer face of the front door sheet; extrusion front face y %s' % round(emin[1], 1)},
        'bed_top': {'x0': r1(pei[0])[0], 'x1': r1(pei[1])[0], 'y0': r1(pei[0])[1], 'y1': r1(pei[1])[1], 'z': r1(pei[1])[2],
                    'plate_min': r1(plate[0]), 'plate_max': r1(plate[1]),
                    'z_travel': round(zcar_lo - zrail_lo, 1),
                    'z_travel_note': 'geometric: bed-arm carriages down to the bottom of the Z rails; '
                                     'Rat Rig rates the 300 at 300 mm Z'},
        'beam': {'min': r1(beam[0]), 'max': r1(beam[1]), 'rail_min': r1(xr[0]), 'rail_max': r1(xr[1])},
        'y_rails': {'min': r1(yr[0]), 'max': r1(yr[1])},
        'toolhead_mount': r1((bmin + bmax) / 2),
        'nozzle_tip': r1(tip),
        'toolhead_lowest_z': meta['groups']['toolhead']['min'][2],
    })
    for g, gm in meta['groups'].items():
        print('%-9s %6d tri  %2d colours  min %s max %s' % (g, gm['tri'], len(gm['colours']), gm['min'], gm['max']))
    glb = scene.export(file_type='glb')
    open(os.path.join(OUT, KEY + '.glb'), 'wb').write(glb)
    json.dump(meta, open(os.path.join(OUT, KEY + '.json'), 'w'), indent=1)
    print('total tri', sum(gm['tri'] for gm in meta['groups'].values()), 'glb', round(len(glb) / 1e6, 2), 'MB')
    print(json.dumps({k: v for k, v in meta.items() if k != 'groups'}, indent=1))


if __name__ == '__main__':
    main('--list' in sys.argv)
