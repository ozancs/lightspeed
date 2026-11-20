"""Numeric quality check of a web model against its CAD parts (no rendering).

Needs models/raw/<key>_parts.pkl, written by a build run with LOD_DUMP=1 (the parts as they go into
lod.decimate_parts, before decimation). For every CAD part and every GLB given, it measures:
- kept:  triangles of the GLB inside the part's box / triangles of the part (old GLBs have no part identity)
- fwd:   max / mean distance from the part's surface to the GLB mesh of its bucket (lost shape)
- back:  max distance from the GLB triangles inside the part's box to the CAD parts there (spikes, mm)
(a part with nothing left near it counts as 99 mm), plus zero-area and needle (< 1 degree) faces per file. Max deviation is also the bound on how far a
decimated surface can move into a neighbouring part (there is no direct intersection test).

    python -I tools/models/quality.py voron24 old.glb web/models/voron24.glb [--top 15]
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402
import trimesh  # noqa: E402
import lod  # noqa: E402


def bucket_meshes(path):
    sc = trimesh.load(path, force='scene')
    out = {}
    for name, g in sc.geometry.items():
        if '__' in name:
            grp, col = name.split('__')
            out[(grp, tuple(int(col[i:i + 2], 16) for i in (0, 2, 4)))] = g
    return out


def crop(mesh, lo, hi, pad):
    c = mesh.triangles_center
    ok = np.all((c >= lo - pad) & (c <= hi + pad), axis=1)
    if not ok.any():
        return None
    m = trimesh.Trimesh(mesh.vertices, mesh.faces[ok], process=False)
    m.remove_unreferenced_vertices()
    return m


def measure(parts, glb):
    B = bucket_meshes(glb)
    rows = []
    allparts = [(k, trimesh.Trimesh(v, f, process=False)) for k, v, f in parts]
    by_key = {}
    for i, (k, m) in enumerate(allparts):
        by_key.setdefault(k, []).append(i)
    boxes = np.array([m.bounds for _, m in allparts])
    for i, (k, m) in enumerate(allparts):
        out = B.get(k)
        lo, hi = m.bounds
        pad = 2.0
        if out is None:
            rows.append((k, len(m.faces), 0, np.inf, np.inf, np.inf))
            continue
        near = crop(out, lo, hi, pad + 3.0)
        if near is None:
            rows.append((k, len(m.faces), 0, np.inf, np.inf, np.inf))
            continue
        d1 = lod.Surf(near, 1.0).dist(lod.probe(m, 800))
        inside = crop(out, lo, hi, 0.0)
        kept = 0 if inside is None else len(inside.faces)
        back = 0.0
        if inside is not None:
            # CAD parts of the same bucket whose box overlaps this one: the GLB triangles here belong to them
            j = [x for x in by_key[k] if np.all(boxes[x][0] <= hi + pad) and np.all(boxes[x][1] >= lo - pad)]
            ref = trimesh.util.concatenate([allparts[x][1] for x in j])
            back = float(lod.Surf(ref, 1.0).dist(lod.probe(inside, 600)).max())
        rows.append((k, len(m.faces), kept, float(d1.max()), float(d1.mean()), back))
    return rows


def faces_stats(glb):
    sc = trimesh.load(glb, force='scene')
    z = n = t = 0
    for g in sc.geometry.values():
        a, b = lod.needles(g)
        z += a
        n += b
        t += len(g.faces)
    return t, z, n


def main():
    key = sys.argv[1]
    args = sys.argv[2:]
    files = [a for i, a in enumerate(args) if not a.startswith('--') and not (i and args[i - 1] == '--top')]
    top = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 15
    parts = pickle.load(open(os.path.join(lod.RAWDIR, '%s_parts.pkl' % key), 'rb'))
    raw = trimesh.util.concatenate([trimesh.Trimesh(v, f, process=False) for _, v, f in parts])
    a, b = lod.needles(raw)
    print('%s CAD parts: %d, %d tri, zero-area %d, needles %d' % (key, len(parts), len(raw.faces), a, b))
    res = {}
    for f in files:
        rows = measure(parts, f)
        res[f] = rows
        t, z, n = faces_stats(f)
        size = np.array([np.sort(trimesh.Trimesh(v, ff, process=False).extents)[1] for _, v, ff in parts])
        lost = sum(1 for r in rows if not np.isfinite(r[3]))
        print('  parts with no triangles left near them: %d' % lost)
        fm = np.array([r[3] if np.isfinite(r[3]) else 99.0 for r in rows])
        rel = fm / np.maximum(size, 1e-3)
        bk = np.array([r[5] if np.isfinite(r[5]) else 99.0 for r in rows])
        kept = np.array([r[2] / max(1, r[1]) for r in rows])
        small = np.array([r[1] < 1500 for r in rows])
        print('\n== %s: %d tri, %.2f MB, zero-area %d, needles %d' % (f, t, os.path.getsize(f) / 1e6, z, n))
        print('  part fwd deviation  max %.2f mm, p95 %.3f mm, median %.3f mm; parts over 0.25 mm: %d, over 1 mm: %d'
              % (fm.max(), np.quantile(fm, 0.95), np.median(fm), (fm > 0.25).sum(), (fm > 1.0).sum()))
        print('  relative to the middle extent: p95 %.2f %%, parts over 2 %%: %d, over 5 %%: %d'
              % (100 * np.quantile(rel, 0.95), (rel > 0.02).sum(), (rel > 0.05).sum()))
        print('  spikes (back) max %.2f mm, p95 %.3f mm, parts over 0.5 mm: %d' % (bk.max(), np.quantile(bk, 0.95), (bk > 0.5).sum()))
        print('  small parts (< 1500 tri, %d): kept ratio median %.2f, min %.2f' % (small.sum(), np.median(kept[small]), kept[small].min()))
        for g in sorted(set(r[0][0] for r in rows)):
            sel = [i for i, r in enumerate(rows) if r[0][0] == g]
            print('  %-9s parts %3d  fwd max %.2f  p95 %.3f  back max %.2f  rel>2%% %d'
                  % (g, len(sel), fm[sel].max(), np.quantile(fm[sel], 0.95), bk[sel].max(), (rel[sel] > 0.02).sum()))
    if len(files) == 2:
        old, new = res[files[0]], res[files[1]]
        order = np.argsort([-max(r[3], r[5]) for r in old])[:top]
        print('\nworst parts in %s (fwd max / back max mm, tri inside box): old -> new' % files[0])
        for i in order:
            k, n0 = old[i][0], old[i][1]
            print('  %-24s %6d tri  %.2f/%.2f (%d) -> %.2f/%.2f (%d)' % ('%s__%02x%02x%02x' % ((k[0],) + k[1]), n0,
                  old[i][3], old[i][5], old[i][2], new[i][3], new[i][5], new[i][2]))


if __name__ == '__main__':
    main()
