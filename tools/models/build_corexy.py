"""VzBoT Vz-330 (enclosed CoreXY box) raw CAD GLB -> web GLB + JSON, same look as voron24.

Source: VzBoT3D/VzBoT-Vz330, CAD/Full Printer/Vz-330_v1.2.zip ("Vz-330_WEB v2.step"),
licence CC BY-NC 4.0. Converted with cascadio to models/raw/corexy_raw.glb.

The CAD shows some alternatives on top of each other (2WD and AWD front idlers, two X-beam copies,
two CPAP mounts, MGN9C/MGN9H top plates); only one of each is kept.
Output coordinates: mm, Z up, front at -Y, bed top at z=0, bed plate spans x/y 0..330.

    python -I build_corexy.py [--list]
"""
import json
import os
import re
import sys

import numpy as np
import trimesh

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_model import (RAW, OUT, BLACK, DARK_GREY, RAIL_STEEL, PEI_GOLD, RED,  # noqa: E402
                         part_color, quant, decimate)

KEY = 'corexy'
SRC = RAW + r"\corexy_raw.glb"
METAL = (92, 96, 102)        # bolts-free metal parts, motors, pulleys (not the extrusion grey)
BED_ALU = (64, 66, 70)       # bed plate edge
GLASS = (225, 232, 238)      # page swaps in its own glass material
SHIFT = np.array([165.0, 145.0, 13.6])   # CAD -> output: bed plate corner to 0,0, bed top to z 0
MIN_SIZE = 8.0

# whole sub-assemblies left out (path anywhere): electronics, side blowers, duplicates/alternatives
DROP_PATH = re.compile(
    r"(^Electronic:1|^RSCS:1|Vz-Printhead Printed MGN9-revised v24:1/gantry:1|2WD Front Idlers|"
    r"/Y end stop switch mount:1|AWD Y end stop|Rear sliding mount CPAP 15mm|Top plate MGN9H|X-enstop|"
    r"Enclose fan|CPAP fan mount|Z Stopper|Z-Chain-Mount|Z limit switch|cable holder|/Belts:1|Z Belt)", re.I)
# fasteners, belts, switches, wiring (leaf or its two parents)
DROP_NAME = re.compile(
    r"(screw|\bnut\b|nut v|washer|t[ -]?nut|socket head|button head|countersunk|shoulder bolt|bolt|"
    r"hardware|belt|endstop|micro ?swit|cable|chain|wire|insert|\bmagnet\b|spacer|knob|clip|"
    r"\bm[2-5] \d+ ?mm|\d+mm m[2-5]|din rail|switch)", re.I)
# kept even inside a 'hardware' folder: motion parts that should be seen
KEEP_NAME = re.compile(r"(pulley|idler|bearing|gt2|smooth rod|lead ?screw$|leadscrew v|trapezoidal|nema|stepper|"
                       r"Bed Knob|knob:|Lead Screw (upper|lower) bracket)", re.I)
EXTRUSION = re.compile(r"(8020-20|^2020-|2020-Low|^Copy of 8020)", re.I)
RAIL = re.compile(r"(rail|6725K432|smooth rod|lead ?screw|leadscrew)", re.I)
CLEAR = (235, 235, 229)

TARGETS = {'frame': 80000, 'glass': 3000, 'panels': 25000, 'bed': 10000, 'gantry_y': 16000, 'toolhead': 40000}


def group_of(path):
    s = '/'.join(path)
    if re.search(r"Vz-Printhead Printed|MGN9-H-block", s):
        return 'toolhead'
    if re.search(r"gantry 2WD \+ AWD:1/(Y gantry printed|CF square tube|MGN9-rail-400|Y sliding block)", s):
        return 'gantry_y'
    if re.search(r"Custom Bed assembly v7|LM10LUU|Trapezoidal lead screw Nut|Vz oldham", s):
        return 'bed'
    if s.startswith('Enclosure (2)'):
        return 'panels'   # clear sheets are moved to 'glass' by colour
    if re.match(r"(Frame:1|gantry 2WD \+ AWD:1|Z group \(1\):1)", s):
        return 'frame'
    return None


def colour_of(grp, c, path):
    r, g, b = c
    leafs = path[-3:]
    if grp == 'glass':
        return GLASS
    if any(EXTRUSION.search(n) for n in leafs) or any(n.startswith('CF tube') for n in leafs):
        return DARK_GREY
    if any(RAIL.search(n) for n in leafs[-2:]):
        return RAIL_STEEL
    if grp == 'bed':
        if (r, g, b) == quant((200, 127, 25)):
            return PEI_GOLD
        if any('bed (1)' in n for n in leafs):
            return BED_ALU
    if r > 120 and g < 60 and b < 60:
        return RED
    if max(c) < 40:
        return BLACK
    if grp == 'panels' and any(n.startswith('Enclosure:1') or 'bottom panel' in n for n in leafs):
        return BLACK      # rear / bottom aluminium sheets: dark, like the voron opaque panels
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
    buckets, meta_parts = {}, {}
    kept = dropped = 0
    stats = {}
    for node, path, src, T in walk():
        s = '/'.join(path)
        leaf3 = path[-3:]
        if DROP_PATH.search(s):
            dropped += 1
            continue
        if any(DROP_NAME.search(n) for n in leaf3) and not any(KEEP_NAME.search(n) for n in path[-2:]):
            dropped += 1
            continue
        grp = group_of(path)
        if grp is None:
            dropped += 1
            continue
        m = src.copy()
        m.apply_transform(T)
        m.apply_scale(1000.0)
        m.apply_translation(SHIFT)
        if np.linalg.norm(m.extents) < MIN_SIZE:
            dropped += 1
            continue
        raw = part_color(src)
        if grp == 'panels' and max(abs(a - b) for a, b in zip(raw, CLEAR)) < 4:
            grp = 'glass'
        col = colour_of(grp, quant(raw), path)
        buckets.setdefault((grp, col), []).append(m)
        kept += 1
        # parts the JSON is measured from
        for tag, pat in (('nozzle', r"Hotends:1/Goliath"), ('block', r"MGN9-H-block"), ('beam', r"CF tube 440"),
                         ('pei', r"PEI Magnetic"), ('bedlow', r"Bed bearing \+ screw mount"),
                         ('zlow', r"Z Lower Rod Mount"), ('extr', r"^Frame:1/(8020|Copy of 8020|2020-Low)"),
                         ('xrail', r"MGN9-rail-400"), ('yrail', r"Y Rail-"), ('front', r"Front panels")):
            if re.search(pat, s):
                meta_parts.setdefault(tag, []).append(m)
        if listing:
            k = (grp, '/'.join(path[:3])[:110])
            stats[k] = stats.get(k, 0) + len(m.faces)
    print('kept', kept, 'dropped', dropped)
    if listing:
        for (g, k), n in sorted(stats.items()):
            print('%-9s %7d  %s' % (g, n, k))
        return

    merged = {k: trimesh.util.concatenate(v) for k, v in buckets.items()}
    gtot = {}
    for (g, c), mesh in merged.items():
        gtot[g] = gtot.get(g, 0) + len(mesh.faces)
    scene = trimesh.Scene()
    meta = {'groups': {}}
    for (g, c), mesh in sorted(merged.items(), key=lambda kv: kv[0]):
        mesh.merge_vertices()
        share = TARGETS[g] * len(mesh.faces) / float(gtot[g])
        mesh = decimate(mesh, int(share))
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
    # nozzle tip: centre of the lowest hotend vertices
    nv = np.vstack([m.vertices for m in meta_parts['nozzle']])
    lo = nv[:, 2].min()
    tip = [*nv[nv[:, 2] < lo + 0.3][:, :2].mean(0), lo]
    th_lo = meta['groups']['toolhead']['min'][2]
    bmin, bmax = bb('block')
    emin, emax = bb('extr')
    beam = bb('beam')
    pei = bb('pei')
    bed_low = bb('bedlow')[0][2]
    zlow_top = bb('zlow')[1][2]
    fr = bb('front')
    xr, yr = bb('xrail'), bb('yrail')
    meta.update({
        'source': 'VzBoT Vz-330 v1.2 (github.com/VzBoT3D/VzBoT-Vz330, CAD/Full Printer), CC BY-NC 4.0',
        'units': 'mm, Z up, front -Y; CAD shifted by %s so the bed plate is x/y 0..330 and its top z 0' % SHIFT.tolist(),
        'build_volume': [330, 330, 397],
        'kinematics': {'bed': 'z', 'gantry_y': 'y', 'toolhead': 'x'},
        'pose': 'CAD pose: bed at the top (print z 0), carriage centred on X, beam at its CAD Y',
        # inner faces of the 2020/2040 extrusion cube: x0/x1 left/right, front/rear, bottom/top
        'extrusion': {'outer_min': r1(emin), 'outer_max': r1(emax), 'profile': 20,
                      'x0': round(emin[0] + 20, 1), 'x1': round(emax[0] - 20, 1),
                      'front': round(emin[1] + 20, 1), 'rear': round(emax[1] - 20, 1),
                      'bottom': round(emin[2] + 20, 1), 'top': round(emax[2] - 20, 1)},
        'front_face': {'y': r1(fr[0])[1], 'note': 'outer face of the front door sheets; extrusion front face y %s' % round(emin[1], 1)},
        'bed_top': {'x0': r1(pei[0])[0], 'x1': r1(pei[1])[0], 'y0': r1(pei[0])[1], 'y1': r1(pei[1])[1], 'z': r1(pei[1])[2],
                    'z_travel': round(bed_low - zlow_top, 1),
                    'z_travel_note': 'free drop of the bed carriage before it meets the lower rod mounts (geometric limit, not a firmware value)'},
        'beam': {'min': r1(beam[0]), 'max': r1(beam[1]), 'rail_min': r1(xr[0]), 'rail_max': r1(xr[1])},
        'y_rails': {'min': r1(yr[0]), 'max': r1(yr[1])},
        'toolhead_mount': r1((bmin + bmax) / 2),
        'nozzle_tip': r1(tip),
        'toolhead_lowest_z': th_lo,
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
