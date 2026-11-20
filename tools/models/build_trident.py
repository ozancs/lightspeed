"""Raw CAD GLB -> web GLB + JSON for the bed-in-Z Vorons (Trident here, V0 in build_voron0.py).

Same pipeline and palette as build_model.py (imported helpers), plus:
- a moving bed group (bed, bed frame, Z joints) instead of a flying gantry,
- clear sheets split from their clips/hinges (glass = only the sheets),
- the model is moved so the bed plate's front-left corner is x,y 0 and the bed top as drawn is z 0,
- the JSON also carries the measured frame, bed, beam, toolhead mount, nozzle tip and kinematics.

    python -I tools/models/build_trident.py [--list GROUP]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I drops the script dir
import numpy as np
import trimesh
from build_model import (RAW, OUT, BLACK, DARK_GREY, RAIL_STEEL, PEI_GOLD, RED,
                         part_color, quant, decimate)

BED_PLATE = (96, 96, 96)   # aluminium plate edge, as voron24 (606060)

# fasteners, belts, chains, wiring, switches. Word-bounded so "Leadscrew" / "Cable_Cover" survive.
DROP = (
    r"(\b(SHCS|BHCS|FHCS|BHSC|SHSC)\b|Self Tap|Thumb Screw|DIN 913|Hex ?nut|Hammerhead|T-Nut|"
    r"Heat Set|Threaded Insert|Washer|\d+x\d+mm Magnet|Knurled Nut|^Nuts|^Screws|\bNut\b|"
    r"^(A|B)[ _]Belt|^Belts|Z Belts|Chain|IGUS|^Cable (Tie|Frame)|Wire Anchor|PTFE|Bowden|"
    r"Endstop|Microswitch|D2F|WAGO|Wago|221-4|Foam Tape|Spool|Rubber Foot|Rubber Feet)")


def is_sheet(m):
    e = np.sort(m.extents)
    return e[0] < 8.0 and e[1] > 60.0


def recolor(group, c, names, cfg):
    """Owner's palette: black body, dark grey extrusions/metal, red accents, steel rails, gold PEI."""
    r, g, b = c
    if group == 'glass':
        return c
    near = names[:4]
    if group == 'bed':
        if any(re.search(cfg['pei'], n) for n in near):
            return PEI_GOLD
        if any(re.search(cfg['plate'], n) for n in near):
            return BED_PLATE
    if any(re.search(r"MGN ?\d|MGN Carriage", n) for n in near):   # linear rails + blocks: steel
        return RAIL_STEEL
    if any(re.search(r"(Extrusion|HFSB5|^1515|2020-)", n) for n in names[:3]):
        return DARK_GREY
    if r >= 110 and g < 60 and b < 60:          # red / dark red accents (quantised 120,0,0 included)
        return RED
    if max(c) < 40:
        return BLACK
    return DARK_GREY


def collect(cfg):
    sc = trimesh.load(cfg['src'], force='scene')
    G = sc.graph
    parent = {e[1]: e[0] for e in G.to_edgelist()}
    drop = re.compile(cfg['drop'], re.I)
    parts = []   # (group, names, mesh, colour)
    nd = 0
    for node in G.nodes_geometry:
        T, gname = G[node]
        names, x = [], node
        while True:
            names.append(x)
            if x not in parent:
                break
            x = parent[x]
        if any(drop.search(n) for n in names[:4]):
            nd += 1
            continue
        grp = None
        path = ' < '.join(names)
        for gr, pat in cfg['groups']:
            if (re.search(pat[5:], path) if pat.startswith('PATH:') else any(re.search(pat, n) for n in names)):
                grp = gr
                break
        if grp is None:
            nd += 1
            continue
        src = sc.geometry[gname]
        m = src.copy()
        m.apply_transform(T)
        m.apply_scale(cfg['scale'])
        if np.linalg.norm(m.extents) < cfg['min_size'] and not any(re.search(cfg['nozzle'], n) for n in names):
            nd += 1
            continue
        col = quant(part_color(src))
        if grp == 'sheet':   # clear sheets -> glass, everything else (clips, hinges, opaque panels) -> panels
            glassy = (is_sheet(m) and min(col) >= 180
                      and any(re.search(cfg['glass'], n) for n in names))
            grp = 'glass' if glassy else 'panels'
            if grp == 'panels' and is_sheet(m):
                col = BLACK
        parts.append((grp, names, m, col))
    print('kept', len(parts), 'dropped', nd)
    return parts


def bbox(parts, pat, group=None):
    rx = re.compile(pat[5:] if pat.startswith('PATH:') else pat)
    hit = (lambda n: rx.search(' < '.join(n))) if pat.startswith('PATH:') else (lambda n: any(rx.search(x) for x in n))
    sel = [m for g, n, m, c in parts if (group is None or g == group) and hit(n)]
    if not sel:
        raise SystemExit('no part matches %r' % pat)
    b = np.array([m.bounds for m in sel])
    return b[:, 0].min(0), b[:, 1].max(0), sel


def r1(v):
    return [round(float(x), 1) for x in v]


def build(key, cfg, list_group=None):
    parts = collect(cfg)
    # origin: bed plate front-left corner x,y = 0, PEI top as drawn z = 0
    pmin, pmax, _ = bbox(parts, cfg['plate'], 'bed')
    smin, smax, _ = bbox(parts, cfg['pei'], 'bed')
    off = np.array([-pmin[0], -pmin[1], -smax[2]])
    for g, n, m, c in parts:
        m.apply_translation(off)
    if list_group:
        for g, n, m, c in parts:
            if g == list_group:
                print('%-80s %s %s %s' % (' < '.join(n[:4])[:80], c, r1(m.bounds[0]), r1(m.bounds[1])))
        return

    # ---- measurements (named parts, before decimation)
    meta = {'name': cfg['title'], 'source': cfg['source'], 'units': 'mm', 'up': 'z', 'front': '-y',
            'cad_offset': r1(off), 'build': cfg['build']}
    emin, emax, _ = bbox(parts, cfg['cube'], 'frame')
    p = cfg['profile']
    meta['extrusion'] = {
        'profile': p, 'outer': {'min': r1(emin), 'max': r1(emax)},
        'inner': {'x0': round(emin[0] + p, 1), 'x1': round(emax[0] - p, 1),
                  'y_front': round(emin[1] + p, 1), 'y_rear': round(emax[1] - p, 1),
                  'z_bottom': round(emin[2] + p, 1), 'z_top': round(emax[2] - p, 1)}}
    meta['front_face'] = round(float(emin[1]), 1)   # outer front face of the extrusion cube (y)
    smin, smax, _ = bbox(parts, cfg['pei'], 'bed')
    meta['bed'] = {'top': round(float(smax[2]), 1),
                   'rect': {'x0': round(smin[0], 1), 'x1': round(smax[0], 1),
                            'y0': round(smin[1], 1), 'y1': round(smax[1], 1)},
                   'plate': {'min': r1(pmin + off), 'max': r1(pmax + off)}}
    bmin, bmax, _ = bbox(parts, cfg['beam'], 'gantry_y')
    meta['beam'] = {'min': r1(bmin), 'max': r1(bmax)}
    cmin, cmax, _ = bbox(parts, cfg['carriage'])
    nrm = np.array(cfg['mount_normal'], float)
    ctr = (cmin + cmax) / 2
    face = np.where(nrm > 0, cmax, np.where(nrm < 0, cmin, ctr))
    meta['mount'] = {'point': r1(face), 'normal': cfg['mount_normal'], 'carriage_centre': r1(ctr),
                     'carriage': {'min': r1(cmin), 'max': r1(cmax)}}
    _, _, noz = bbox(parts, cfg['nozzle'], 'toolhead')
    v = np.vstack([m.vertices for m in noz])
    lo = v[:, 2].min()
    tip = v[v[:, 2] < lo + 0.3].mean(0)
    tip[2] = lo
    meta['nozzle'] = r1(tip)
    Z = cfg['build'][2]
    meta['bed']['z_travel'] = [round(float(tip[2]), 1), round(float(tip[2] - Z), 1)]  # bed top at Z=0 .. Z=max
    meta['kinematics'] = {'bed': 'z', 'gantry_y': 'y', 'toolhead': 'x'}
    for k, pat in cfg.get('extra_boxes', {}).items():
        a, b, _ = bbox(parts, pat)
        meta[k] = {'min': r1(a), 'max': r1(b)}

    # ---- merge per group+colour, palette, decimate, export (as build_model.main)
    buckets = {}
    for g, n, m, c in parts:
        col = recolor(g, c, n, cfg)
        buckets.setdefault((g, col), []).append(m)
    merged = {k: trimesh.util.concatenate(v) for k, v in buckets.items()}
    gtot = {}
    for (g, c), mesh in merged.items():
        gtot[g] = gtot.get(g, 0) + len(mesh.faces)
    scene = trimesh.Scene()
    meta['groups'] = {}
    for (g, c), mesh in sorted(merged.items(), key=lambda kv: kv[0]):
        mesh.merge_vertices()
        mesh = decimate(mesh, int(cfg['targets'][g] * len(mesh.faces) / float(gtot[g])))
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
    tot = 0
    for g, gm in meta['groups'].items():
        tot += gm['tri']
        print('%-9s %6d tri  %2d colours  min %s max %s' % (g, gm['tri'], len(gm['colours']), gm['min'], gm['max']))
    glb = scene.export(file_type='glb')
    open(os.path.join(OUT, key + '.glb'), 'wb').write(glb)
    json.dump(meta, open(os.path.join(OUT, key + '.json'), 'w'), indent=1)
    print({k: v for k, v in meta.items() if k != 'groups'})
    print('total', tot, 'tri  glb', round(len(glb) / 1e6, 2), 'MB')


CFG = {
    'title': 'Voron Trident 250',
    'source': 'github.com/VoronDesign/Voron-Trident CAD/trident_assembly.step.zip (GPLv3)',
    'src': RAW + r"\trident_raw.glb",
    'scale': 1000.0,
    'drop': DROP + r"|^Probes|Panasonic",
    'groups': [          # first match on any ancestor name wins; unmatched (Tools, Filament_Path, Electronics) drop
        ('toolhead', r"^(Toolhead_Assembly|MGN12H:)"),  # the X carriage block rides with the toolhead
        ('gantry_y', r"^X Axis"),
        ('bed', r"^(Bed Components|Bed Extrusions|Z_Left|Z_Right|Z_Rear|3x Wago 221-412 Mount):"),
        ('sheet', r"^Panels:"),
        ('frame', r"^(Frame|Skirt|Z Assembly|Gantry):"),
    ],
    'glass': r"^Transparent Panels",
    'pei': r"^Flex Sheet",
    'plate': r"MIC6 Bed",
    'cube': r"^(Corner|Top|Bottom) Extrusions",
    'profile': 20,
    'beam': r"^HFSB5-20-330",
    'carriage': r"^MGN12H:",
    'mount_normal': [0, -1, 0],     # the toolhead bolts on the front face of the MGN12 block
    'nozzle': r"^e3d Revo Voron",
    'build': [250, 250, 250],
    'targets': {'frame': 200000, 'glass': 12000, 'panels': 25000, 'bed': 15000,
                'gantry_y': 30000, 'toolhead': 45000},
    'min_size': 14.0,
}

if __name__ == '__main__':
    build('trident', CFG, sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == '--list' else None)
