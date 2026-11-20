"""List every geometry part with its ancestor path, colour, size (mm) and triangles."""
import sys
import numpy as np
import trimesh

sc = trimesh.load(sys.argv[1], force='scene')
pat = sys.argv[2].lower() if len(sys.argv) > 2 else ''
G = sc.graph
parent = {}
for e in G.to_edgelist():
    parent[e[1]] = e[0]
for node in G.nodes_geometry:
    T, gname = G[node]
    g = sc.geometry[gname]
    path = []
    x = node
    while x in parent:
        path.append(x)
        x = parent[x]
    p = ' < '.join(path[:4])
    if pat and pat not in p.lower():
        continue
    try:
        c = tuple(int(v) for v in g.visual.material.baseColorFactor[:3])
    except Exception:
        c = None
    b = trimesh.transform_points(g.bounds, T) * 1000.0
    ext = (b[1] - b[0]).round(0).tolist()
    print('%-70s %-16s %s %6d' % (p[:70], c, ext, len(g.faces)))
