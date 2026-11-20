"""Shared pipeline for the extra CAD models (toolheads, bed slinger).

Uses the palette and decimation of build_model.py. Parts are picked by the
part (geometry) name and their ancestor names, recoloured per part, decimated
per part with one shared ratio (small parts are protected, so nothing small
like a gearbox or an LED carrier disappears), merged per group+colour and
written as '<group>__<rrggbb>' meshes with linear glTF colour factors.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import trimesh
import fast_simplification

from build_model import (RAW, OUT, BLACK, DARK_GREY, RAIL_STEEL, PEI_GOLD,  # noqa: F401
                         FAN_GREY, RED, ORANGE, part_color)

DIFFUSER = (214, 214, 210)   # frosted LED diffusers (the page lights them)
BRASS = (150, 112, 48)

# STEP (Z up, front -Y, metres) -> toolhead file (Y up, front +Z, mm), like rx.glb
TOOLHEAD_ROT = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], float)


def clean(gname):
    """geometry name without trimesh's _N suffix"""
    m = re.match(r"^(.*?)(_\d+)?$", gname)
    return m.group(1)


def iter_parts(path, xform=None, scale=1000.0):
    """yield (names, part_name, cad_colour, mesh) per geometry instance; names = node .. root"""
    sc = trimesh.load(path, force='scene')
    G = sc.graph
    parent = {e[1]: e[0] for e in G.to_edgelist()}
    for node in G.nodes_geometry:
        T, gname = G[node]
        names = [node]
        x = node
        while x in parent:
            x = parent[x]
            names.append(x)
        src = sc.geometry[gname]
        m = trimesh.Trimesh(src.vertices.copy(), src.faces.copy(), process=False)
        m.apply_transform(T)
        m.apply_scale(scale)
        if xform is not None:
            m.apply_transform(xform)
        yield names, clean(gname), part_color(src), m


def decimate_to(mesh, target):
    n0 = len(mesh.faces)
    if n0 <= target or target < 12:
        return mesh
    v, f = fast_simplification.simplify(
        mesh.vertices.astype(np.float32), mesh.faces.astype(np.int32),
        target_reduction=1.0 - target / float(n0))
    return trimesh.Trimesh(v, f, process=False)


def build(key, parts, targets, protect=150, extra_meta=None):
    """parts: list of (group, colour, mesh[, fixed]). fixed parts are not decimated (low-poly
    profiles and plates that would tear). Writes web/models/<key>.glb + .json."""
    by_group = {}
    buckets = {}
    fixed_n = {}
    for p in parts:
        g, c, m = p[:3]
        m.merge_vertices()
        if len(p) > 3 and p[3]:
            buckets.setdefault((g, c), []).append(m)
            fixed_n[g] = fixed_n.get(g, 0) + len(m.faces)
        else:
            by_group.setdefault(g, []).append((c, m))
    for g, lst in by_group.items():
        tot = sum(len(m.faces) for _, m in lst)
        tgt = max(1000, targets[g] - fixed_n.get(g, 0))
        # one ratio for the group; parts under `protect` triangles keep theirs
        lo, hi = 0.0, 1.0
        for _ in range(40):
            r = (lo + hi) / 2
            n = sum(min(len(m.faces), max(protect, int(len(m.faces) * r))) for _, m in lst)
            lo, hi = (r, hi) if n < tgt else (lo, r)
        r = lo if tot > tgt else 1.0
        for c, m in lst:
            t = min(len(m.faces), max(protect, int(len(m.faces) * r)))
            buckets.setdefault((g, c), []).append(decimate_to(m, t))
    scene = trimesh.Scene()
    meta = {'groups': {}}
    for (g, c), ms in sorted(buckets.items()):
        mesh = trimesh.util.concatenate(ms)
        mesh.merge_vertices()
        if not len(mesh.faces):
            continue
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
        print('%-10s %6d tri  %s  min %s max %s' % (g, gm['tri'], gm['colours'], gm['min'], gm['max']))
    if extra_meta:
        meta.update(extra_meta)
    os.makedirs(OUT, exist_ok=True)
    glb = scene.export(file_type='glb')
    open(os.path.join(OUT, key + '.glb'), 'wb').write(glb)
    json.dump(meta, open(os.path.join(OUT, key + '.json'), 'w'), indent=1)
    print(key, 'total', tot, 'tri', round(len(glb) / 1e6, 2), 'MB')
    return meta


def r1(p):
    return [round(float(x), 1) for x in p]
