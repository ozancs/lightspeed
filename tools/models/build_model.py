"""Raw CAD GLB -> light web GLB, real colours, named groups + JSON metadata.

Parts are grouped by the names of their ancestors in the CAD tree, then
split by colour inside each group (one mesh per group+colour, named
'<group>__<rrggbb>'). Fasteners, electronics and tiny parts are dropped,
every part is decimated on its own against a measured error (lod.py), units become mm, Z up.

    python -I build_model.py voron24|rx
"""
import json
import pathlib
import os
import re
import sys

import numpy as np
import trimesh

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lod  # noqa: E402

RAW = str(pathlib.Path(__file__).resolve().parents[2] / "models" / "raw")
OUT = str(pathlib.Path(__file__).resolve().parents[2] / "web" / "models")

DROP_NAME = (
    r"(SHCS|BHCS|FHCS|BHSC|Screw|Nut|Insert|Shim|Washer|T-nut|Magnet|"
    r"WAGO|221-|Meanwell|SSR|Pi Assembly|Controllers|Bracket|DIN|Rail Clip|"
    r"MOLEX|Cable Chain|Chain Link|Chain X|PTFE|PFTE|Bowden|Spool|Belt Guard|"
    r"Endstop|Microswitch|Hall Effect|Electronics|Exhaust|Filter|"
    r"Door Latch|door_hinge|Z Chain|IGUS|Z_Chain|"
    r"^(A Belt|B Belt|Z Belts)(:| ))")   # belts would fight the animation

CONFIG = {
    'voron24': {
        'src': RAW + r"\voron24_raw.glb",
        'scale': 1000.0,      # file is in metres
        'drop': DROP_NAME,
        'groups': [           # first match on any ancestor name wins
            ('toolhead', r"^X Carriage"),
            ('gantry_y', r"^X Axis"),
            ('gantry_z', r"^Gantry"),
            ('glass', r"^Transparent Panels"),
            ('panels', r"^(Opaque Panels|Z Belt Cover)"),
            ('bed', r"^Bed Components"),
            ('frame', r"^(Frame|Skirt|Z Assembly|Panels)"),
        ],
        'budget': 345000,     # triangles for the whole model (lod.py, per part)
        'min_size': 14.0,     # mm, bbox diagonal of a part kept
        'recolor': 'voron',
    },
    'rx': {
        'src': RAW + r"\rx_raw.glb",
        'scale': 1000.0,
        'drop': r"(SHCS|BHCS|FHCS|Screw|Nut|Insert|Washer|Magnet)",
        'recolor': 'rx',
        'groups': [('toolhead', r".")],
        'budget': 70000,
        'min_size': 6.0,
        'clip': 150.0,        # drop pieces farther than this (mm) from the centre
        'add_fan': True,      # the STEP has no part fan behind the front ring
    },
}


def part_color(g):
    try:
        c = g.visual.material.baseColorFactor
        if c is not None:
            return tuple(int(x) for x in c[:3])
    except Exception:
        pass
    try:
        return tuple(int(x) for x in g.visual.main_color[:3])
    except Exception:
        return (128, 128, 128)


BLACK = (20, 21, 24)
DARK_GREY = (56, 59, 64)
RAIL_STEEL = (140, 144, 150)
PEI_GOLD = (118, 92, 44)   # darker textured PEI, does not grab the eye
EXTRUSION = re.compile(r"(Extrusions:|HFSB5)", re.I)   # frame/gantry profile groups
FAN_GREY = (44, 47, 52)
RED = (196, 22, 28)
ORANGE = (255, 107, 26)


def recolor(scheme, group, c, names):
    """Owner's palette: black body, dark grey extrusions/metal, red accents."""
    r, g, b = c
    if scheme == 'rx':
        # all black; only the orange ring + logo on the front cover stay orange
        if any('front cover' in n.lower() for n in names) and r > 200 and 60 < g < 120 and b < 40:
            return ORANGE
        if any('electronicsss' in n.lower() for n in names):
            return FAN_GREY
        return BLACK
    if scheme == 'voron':
        if group == 'glass':
            return c
        if group == 'bed':
            # textured PEI sheet gold, plate edge stays as drawn
            return PEI_GOLD if (r > 110 and b < 80) else c
        if any(re.search(r"MGN\d+ (rail|-)", n) for n in names[:3]):  # linear rails: steel
            return RAIL_STEEL
        if any(EXTRUSION.search(n) for n in names):  # aluminium profiles
            return DARK_GREY
        if r > 120 and g < 60 and b < 60:          # red / dark red accents
            return RED
        if max(c) < 40:                            # black printed parts
            return BLACK
        return DARK_GREY                           # extrusions, metal, grey parts
    return c


def quant(c):
    # merge near-identical colours so a group does not explode into meshes
    return tuple(min(255, int(round(x / 24.0) * 24)) for x in c)


def fan_4010(centre, depth_axis_z=True):
    """A simple 40x40x10 axial fan (frame, shroud ring, hub, 7 blades), facing +Z."""
    from trimesh.creation import box, cylinder, annulus
    parts = []
    t = 10.0
    for dx, dy, sx, sy in ((0, 18.5, 40, 3), (0, -18.5, 40, 3), (18.5, 0, 3, 40), (-18.5, 0, 3, 40)):
        b = box((sx, sy, t)); b.apply_translation((dx, dy, 0)); parts.append(b)
    parts.append(annulus(r_min=17.6, r_max=19.0, height=t, sections=48))
    hub = cylinder(radius=7.0, height=t * 0.8, sections=32)
    blades = []
    for i in range(7):
        bl = box((10.5, 3.0, 1.2))
        bl.apply_translation((12.0, 0, 0))
        R = trimesh.transformations.rotation_matrix(np.radians(25), (1, 0, 0))
        bl.apply_transform(R)
        bl.apply_transform(trimesh.transformations.rotation_matrix(2 * np.pi * i / 7, (0, 0, 1)))
        blades.append(bl)
    frame = trimesh.util.concatenate(parts)
    rotor = trimesh.util.concatenate([hub] + blades)
    for m in (frame, rotor):
        m.apply_translation(centre)
    return frame, rotor


def main(key):
    cfg = CONFIG[key]
    sc = trimesh.load(cfg['src'], force='scene')
    G = sc.graph
    parent = {}
    for e in G.to_edgelist():
        parent[e[1]] = e[0]
    drop = re.compile(cfg['drop'], re.I)
    parts = []            # (group, colour), mesh, fixed
    dropped = kept = 0
    for node in G.nodes_geometry:
        T, gname = G[node]
        names = []
        x = node
        while True:
            names.append(x)
            if x not in parent:
                break
            x = parent[x]
        if any(drop.search(n) for n in names[:3]):
            dropped += 1
            continue
        src = sc.geometry[gname]
        m = trimesh.Trimesh(src.vertices.copy(), src.faces.copy(), process=False)
        m.apply_transform(T)
        m.apply_scale(cfg['scale'])
        if np.linalg.norm(m.extents) < cfg['min_size']:
            dropped += 1
            continue
        grp = None
        for g, pat in cfg['groups']:
            if any(re.search(pat, n) for n in names):
                grp = g
                break
        if grp is None:
            dropped += 1
            continue
        # round only the CAD colours (fewer meshes); the palette stays exact
        col = quant(part_color(src))
        if cfg.get('recolor'):
            col = recolor(cfg['recolor'], grp, col, names)
        # the page measures the extrusion cube / X beam and the nozzle tip from these: keep them exact
        fixed = ((col == DARK_GREY and any(EXTRUSION.search(n) for n in names))
                 or any(re.search(r"V7_wo_fan", n) for n in names[:2]))
        parts.append(((grp, col), m, fixed))
        kept += 1
    print('kept', kept, 'dropped', dropped)
    if cfg.get('add_fan'):
        # behind the orange front ring: centre x 2.5, y 57.5; front cover inner face ~z 38
        frame, rotor = fan_4010((2.5, 57.5, 31.0))
        parts.append((('toolhead', FAN_GREY), frame, True))
        parts.append((('toolhead', BLACK), rotor, True))
    if cfg.get('clip'):
        # drop pieces (connected components) farther than clip mm from the centre
        centre = np.median(np.vstack([m.vertices for _, m, _ in parts]), axis=0)
        kept_parts = []
        for k, m, f in parts:
            comps = [p for p in m.split(only_watertight=False) if np.linalg.norm(p.centroid - centre) < cfg['clip']]
            if comps:
                kept_parts.append((k, trimesh.util.concatenate(comps), f))
        parts = kept_parts
    dec, _ = lod.decimate_parts(parts, cfg['budget'], cache=key)
    merged = {k: trimesh.util.concatenate(v) for k, v in dec.items()}

    scene = trimesh.Scene()
    meta = {'groups': {}}
    for (g, c), mesh in sorted(merged.items(), key=lambda kv: kv[0]):
        if mesh is None or not len(mesh.faces):
            continue
        # no welding across parts: every part stays its own island (the page moves short islands as one piece)
        hexc = '%02x%02x%02x' % c
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                # glTF colour factors are linear, the palette is sRGB
                baseColorFactor=[int(round(((x / 255.0) ** 2.2) * 255)) for x in c] + [255],
                metallicFactor=0.1,
                roughnessFactor=0.7))
        name = '%s__%s' % (g, hexc)
        scene.add_geometry(mesh, node_name=name, geom_name=name)
        gm = meta['groups'].setdefault(g, {'tri': 0, 'min': [1e9] * 3,
                                           'max': [-1e9] * 3, 'colours': []})
        gm['tri'] += int(len(mesh.faces))
        gm['min'] = np.minimum(gm['min'], mesh.bounds[0]).round(1).tolist()
        gm['max'] = np.maximum(gm['max'], mesh.bounds[1]).round(1).tolist()
        gm['colours'].append(hexc)
    for g, gm in meta['groups'].items():
        print('%-9s %6d tri  %2d colours  min %s max %s' % (
            g, gm['tri'], len(gm['colours']), gm['min'], gm['max']))
    print('total', sum(gm['tri'] for gm in meta['groups'].values()), 'tri')
    os.makedirs(OUT, exist_ok=True)
    glb = scene.export(file_type='glb')
    open(os.path.join(OUT, key + '.glb'), 'wb').write(glb)
    json.dump(meta, open(os.path.join(OUT, key + '.json'), 'w'), indent=1)
    print('glb', round(len(glb) / 1e6, 2), 'MB')


if __name__ == '__main__':
    main(sys.argv[1])
