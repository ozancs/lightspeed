"""Per-part, error-bounded decimation shared by every model script.

The old pipeline merged all parts of one group+colour into one mesh and decimated it with one ratio
(about 1/4 on the Voron 2.4 gantry). Small printed parts (AB drives, idlers, clips) lost most of their
triangles next to the big extrusions, fast_simplification left needles and spikes (up to several mm
on parts with open CAD seams), and collapses could pull one part onto a touching one.

Here every CAD part is decimated on its own, against a measured error:
- each part gets a ladder of candidates: border-safe lossless, QEM (fast_simplification) at 65 % .. 5 %,
  and vertex clustering at 0.1 .. 3.2 mm cells (robust on dense motors/threads where QEM stalls or spikes);
- the two-sided surface deviation (exact point-triangle distance, mm) of every candidate is measured;
- one global tolerance (mm) is searched so the model fits its triangle budget; every part takes the
  smallest candidate within min(tolerance, rel x its middle extent), so a 3 mm clip or a 20 mm profile
  stays exact while big plates and motors give up their triangles;
- parts under `small` triangles keep at least `floor` of them (only lossless goes lower);
- `fixed` parts (extrusions the page measures, the nozzle, primitives) are kept as they are.
Ladders are cached in models/raw/<key>_lod.pkl (keyed by the part's geometry), so tuning a budget is quick.

    parts = [(bucket_key, mesh, fixed[, link]), ...]
    out, report = decimate_parts(parts, budget, cache='voron24')   # out: bucket_key -> [meshes]

`link` (optional, a name): parts with the same link (same bucket) leave as ONE mesh, chained by a zero-area
triangle between their nearest vertices, so the page treats them as one island: a skirt run (corner skirt,
centre piece, corner skirt) then stretches at the page's cut planes inside the centre piece instead of the
corner skirts moving away and leaving gaps.
"""
import hashlib
import os
import pickle
import time

import numpy as np
import trimesh
import fast_simplification
from scipy.spatial import cKDTree

QEM = (0.65, 0.45, 0.3, 0.2, 0.12, 0.07, 0.04)
CELL = (0.15, 0.3, 0.6, 1.2, 2.4)
VERSION = 3   # bump when the ladder changes (cache key)
RAWDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'models', 'raw')


def _clean(m):
    """drop degenerate and duplicate faces (area ~0 or repeated vertices) without touching the shape"""
    f = m.faces
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    if len(f):
        ok &= m.area_faces > 1e-9
    if not ok.all():
        m.update_faces(ok)
    if len(m.faces):
        u = np.unique(np.sort(m.faces, axis=1), axis=0, return_index=True)[1]
        if len(u) < len(m.faces):
            keep = np.zeros(len(m.faces), bool)
            keep[u] = True
            m.update_faces(keep)
    m.remove_unreferenced_vertices()
    return m


def qem(m, target, lossless=False):
    v, f = fast_simplification.simplify(m.vertices.astype(np.float64), m.faces.astype(np.int32),
                                        target_count=max(4, int(target)), agg=5.0, lossless=lossless,
                                        preserve_border=lossless)
    return _clean(trimesh.Trimesh(v, f, process=False))


def cluster(m, h):
    """vertex clustering on an h mm grid (cell mean); bounded error, never stalls"""
    key = np.floor(m.vertices / h).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    nv = int(inv.max()) + 1
    v = np.zeros((nv, 3))
    np.add.at(v, inv, m.vertices)
    v /= np.bincount(inv, minlength=nv)[:, None]
    return _clean(trimesh.Trimesh(v, inv[m.faces], process=False))


def make(m, how):
    kind, p = how
    if kind == 'full':
        return m
    if kind == 'lossless':
        return qem(m, 4, lossless=True)
    if kind == 'qem':
        return qem(m, int(round(p * len(m.faces))))
    return cluster(m, p)


class Surf:
    """nearest-surface queries without rtree: KD tree of points spread over every triangle (tagged
    with the triangle) gives k candidates and an upper bound; points above `eps` are then checked
    exactly against every triangle whose box lies within that bound"""

    def __init__(self, mesh, spacing, cap=600_000):
        self.tri = mesh.triangles
        F = len(mesh.faces)
        n = int(min(cap, max(2 * F, mesh.area / max(spacing, 1e-3) ** 2)))
        pts, fi = trimesh.sample.sample_surface(mesh, n, seed=1)
        self.pts = np.vstack([mesh.triangles_center, pts])
        self.fid = np.concatenate([np.arange(F), fi])
        self.kd = cKDTree(self.pts)
        self.lo, self.hi = self.tri.min(1), self.tri.max(1)

    def dist(self, p, k=6, eps=0.02):
        k = min(k, len(self.pts))
        _, idx = self.kd.query(p, k=k)
        cand = self.fid[idx.reshape(len(p), -1)]
        pp = np.repeat(p, cand.shape[1], axis=0)
        cp = trimesh.triangles.closest_point(self.tri[cand.ravel()], pp)
        d = np.linalg.norm(cp - pp, axis=1).reshape(len(p), -1)
        best = d.min(1)
        sel_all = np.nonzero(best > eps)[0]
        step = max(8, int(4e6 // max(1, len(self.tri))))
        for i0 in range(0, len(sel_all), step):
            sel = sel_all[i0:i0 + step]
            q = p[sel]
            g = np.maximum(np.maximum(self.lo[None] - q[:, None], q[:, None] - self.hi[None]), 0)
            pi, ti = np.nonzero((g * g).sum(2) <= (best[sel] ** 2)[:, None] + 1e-12)
            if not len(pi):
                continue
            c = trimesh.triangles.closest_point(self.tri[ti], q[pi])
            dd = np.linalg.norm(c - q[pi], axis=1)
            m = np.full(len(sel), np.inf)
            np.minimum.at(m, pi, dd)
            best[sel] = np.minimum(best[sel], m)
        return best


def probe(m, n=600, seed=2):
    """points to measure a mesh with: its vertices (corners, features, spikes) + area samples"""
    rng = np.random.default_rng(seed)
    v = m.vertices
    if len(v) > n:
        v = v[rng.choice(len(v), n, replace=False)]
    s, _ = trimesh.sample.sample_surface(m, n, seed=seed)
    return np.vstack([v, s])


def deviation(a, b, sa=None, pa=None):
    """two-sided surface deviation between meshes a and b (mesh units): (max, mean)"""
    sa = sa or Surf(a, a.scale / 60.0)
    pa = probe(a) if pa is None else pa
    d1 = Surf(b, a.scale / 60.0).dist(pa)
    d2 = sa.dist(probe(b))
    return float(max(d1.max(), d2.max())), float(0.5 * (d1.mean() + d2.mean()))


def ladder(m, small, floor):
    """candidates [(tris, dev_max, how)] from the full part down"""
    n = len(m.faces)
    out = [(n, 0.0, ('full', 0))]
    if n < 40:
        return out
    sa, pa = Surf(m, m.scale / 60.0), probe(m)
    lo = int(floor * n) if n < small else 12

    def add(how):
        d = make(m, how)
        if not len(d.faces) or len(d.faces) >= n:
            return None
        e = deviation(m, d, sa, pa)[0]
        out.append((len(d.faces), e, how))
        return len(d.faces), e
    add(('lossless', 0))
    for r in QEM:
        if r * n < lo:
            break
        res = add(('qem', r))
        if res is None or res[1] > 3.0:
            break
    mid = float(np.sort(m.extents)[1])
    for h in CELL:
        if h > mid / 6:
            break
        res = add(('cell', h))
        if res is None or res[0] < lo or res[1] > 3.0:
            break
    # a candidate under the floor is only allowed when it is lossless
    return [c for c in out if c[0] >= lo or c[1] < 1e-3]


def choose(cands, lim):
    pick = cands[0]
    for c in cands:
        if c[1] <= lim + 1e-9 and c[0] < pick[0]:
            pick = c
    return pick


def _hash(m):
    h = hashlib.sha1(np.ascontiguousarray(m.vertices, np.float32).tobytes())
    h.update(np.ascontiguousarray(m.faces, np.int32).tobytes())
    return h.hexdigest()


def decimate_parts(parts, budget, small=1500, floor=0.65, rel=0.01, cache=None, verbose=True):
    """parts: [(key, mesh, fixed)] -> ({key: [mesh]}, report). Each mesh is merged on its own first."""
    t0 = time.time()
    path = os.path.join(RAWDIR, '%s_lod.pkl' % cache) if cache else None
    store = {}
    if path and os.path.exists(path):
        try:
            store = pickle.load(open(path, 'rb'))
        except Exception:
            store = {}
    items, new = [], 0
    links = []
    for p in parts:
        key, m, fixed = p[:3]
        links.append(p[3] if len(p) > 3 else None)
        m = trimesh.Trimesh(np.asarray(m.vertices, np.float64).copy(), m.faces.copy(), process=False)
        m.merge_vertices()
        _clean(m)
        if not len(m.faces):
            links.pop()
            continue
        if fixed:
            lad = [(len(m.faces), 0.0, ('full', 0))]
        else:
            hk = '%d|%s|%d|%g|%g' % (VERSION, _hash(m), len(m.faces), small, floor)
            lad = store.get(hk)
            if lad is None:
                lad = store[hk] = ladder(m, small, floor)
                new += 1
        items.append((key, m, lad, float(np.sort(m.extents)[1])))
    if path and new:
        pickle.dump(store, open(path, 'wb'))
    if path and os.environ.get('LOD_DUMP'):   # input parts for quality.py
        pickle.dump([(k, m.vertices, m.faces) for k, m, _, _ in items],
                    open(os.path.join(RAWDIR, '%s_parts.pkl' % cache), 'wb'))
    n0 = sum(len(m.faces) for _, m, _, _ in items)
    least = sum(min(c[0] for c in lad) for _, _, lad, _ in items)

    def total(tol, rel):
        return sum(choose(lad, min(tol, rel * s))[0] for _, _, lad, s in items)
    # smallest relative cap that can reach the budget, then the smallest absolute tolerance under it
    for f in (1, 1.5, 2, 3, 4, 6, 10, 100):
        if total(4.0, rel * f) <= budget:
            break
    rel = rel * f
    if total(0.0, rel) <= budget:
        tol = 0.0
    else:
        lo, hi = 0.0, 4.0
        for _ in range(36):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if total(mid, rel) <= budget else (mid, hi)
        tol = hi
    out, rep, chains = {}, [], {}
    for (key, m, lad, s), ln in zip(items, links):
        c = choose(lad, min(tol, rel * s))
        d = make(m, c[2])
        if ln is None:
            out.setdefault(key, []).append(d)
        else:
            chains.setdefault((key, ln), []).append(d)
        rep.append({'key': key, 'n0': len(m.faces), 'n': c[0], 'dev': c[1], 'size': s, 'how': c[2], 'link': ln})
    for (key, ln), ms in chains.items():
        out.setdefault(key, []).append(chain(ms))
    n1 = sum(r['n'] for r in rep)
    if verbose:
        print('lod: %d parts (%d new ladders, %.0f s), %d -> %d tri, budget %d, least possible %d, '
              'tolerance %.3f mm (cap %.1f %% of the middle extent)'
              % (len(items), new, time.time() - t0, n0, n1, budget, least, tol, rel * 100))
    if n1 > budget * 1.01:
        print('lod: WARNING over budget (%d > %d): raise `rel` or cap the dense parts' % (n1, budget))
    return out, {'parts': rep, 'tol': tol, 'n0': n0, 'n1': n1}


def chain(meshes):
    """one mesh, one island: every connected piece of every part (open CAD seams split a part into several)
    is joined to the pieces before it by a zero-area triangle between the nearest vertex pair"""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    m = trimesh.util.concatenate(meshes)
    v, f = np.asarray(m.vertices, np.float64), np.asarray(m.faces)
    e = np.r_[f[:, [0, 1]], f[:, [1, 2]]]
    n, lab = connected_components(coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(len(v),) * 2), directed=False)
    used = np.unique(f)
    order = list(dict.fromkeys(lab[used]))            # pieces in part order
    extra, done = [], np.nonzero(lab == order[0])[0]
    for c in order[1:]:
        idx = np.nonzero(lab == c)[0]
        d, j = cKDTree(v[done]).query(v[idx])
        k = int(np.argmin(d))
        extra.append([done[j[k]], idx[k], idx[k]])
        done = np.r_[done, idx]
    if extra:
        f = np.vstack([f, np.array(extra)])
    return trimesh.Trimesh(v, f, process=False)


def needles(mesh, min_angle_deg=1.0):
    """(zero-area faces, needle faces with a smallest angle under min_angle_deg)"""
    tri = mesh.triangles
    L = np.sort(np.c_[np.linalg.norm(tri[:, 1] - tri[:, 2], axis=1), np.linalg.norm(tri[:, 0] - tri[:, 2], axis=1),
                      np.linalg.norm(tri[:, 0] - tri[:, 1], axis=1)], axis=1)
    area = mesh.area_faces
    zero = area < 1e-6
    s = 2 * area / np.maximum(L[:, 1] * L[:, 2], 1e-12)
    needle = np.degrees(np.arcsin(np.clip(s, 0, 1))) < min_angle_deg
    return int(zero.sum()), int((needle & ~zero).sum())
