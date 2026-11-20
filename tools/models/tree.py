"""Print the GLB node tree to a depth with triangle totals and world bbox per subtree."""
import sys
import numpy as np
import trimesh

sc = trimesh.load(sys.argv[1], force='scene')
depth_max = int(sys.argv[2]) if len(sys.argv) > 2 else 3
G = sc.graph
children = {}
for e in G.to_edgelist():
    a, b = e[0], e[1]
    children.setdefault(a, []).append(b)


def subtree_stats(n):
    tri, pts = 0, []
    stack = [n]
    while stack:
        x = stack.pop()
        stack.extend(children.get(x, []))
        try:
            T, gname = G[x]
        except Exception:
            continue
        if gname and gname in sc.geometry:
            g = sc.geometry[gname]
            tri += len(g.faces)
            pts.append(trimesh.transform_points(g.bounds, T))
    if pts:
        p = np.vstack(pts) * 1000.0
        return tri, p.min(0), p.max(0)
    return tri, None, None


def walk(n, d):
    tri, mn, mx = subtree_stats(n)
    if mn is not None:
        print('  ' * d + '%s  [%d tri]  min %s max %s' % (n[:60], tri, mn.round(0).tolist(), mx.round(0).tolist()))
    if d < depth_max:
        for c in children.get(n, []):
            walk(c, d + 1)


walk(G.base_frame, 0)
