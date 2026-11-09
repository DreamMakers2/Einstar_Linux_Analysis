"""Robust grid recovery: hull-quad RANSAC + integer-node scoring.

For every 4-subset of the convex-hull vertices (ordered cyclically) and every
candidate (ncols, nrows), map the quad to the grid corners and count how many
holes land on distinct integer nodes.  The true corner quad of the true grid
maximises the occupied-node count.  No calibration or baseline input.
"""
from __future__ import annotations
import itertools
import cv2, numpy as np
from lattice_lib import load_gray, detect_blobs, pitch_estimate


def hull_vertices(P, maxv=18):
    h = cv2.convexHull(P[:, :2].astype(np.float32)).reshape(-1, 2)
    if len(h) > maxv:
        peri = cv2.arcLength(h.reshape(-1, 1, 2), True)
        h = cv2.approxPolyDP(h.reshape(-1, 1, 2), 0.008 * peri, True).reshape(-1, 2)
    return h


def order_cyclic(q):
    ctr = q.mean(0)
    ang = np.arctan2(q[:, 1] - ctr[1], q[:, 0] - ctr[0])
    return q[np.argsort(ang)].astype(np.float32)


def homography_quad(q, nc, nr):
    dst = np.array([[0, 0], [nc - 1, 0], [nc - 1, nr - 1], [0, nr - 1]], np.float32)
    return cv2.getPerspectiveTransform(q, dst)


def quad_assignments(q):
    """All 8 dihedral assignments of the 4 quad corners (cyclic order kept)."""
    out = []
    for rev in (False, True):
        qq = q[::-1] if rev else q
        for sh in range(4):
            out.append(np.roll(qq, -sh, axis=0))
    return out


def node_score(P, H, nc, nr, tol=0.20):
    uv = cv2.perspectiveTransform(P[:, None, :2].astype(np.float32), H)[:, 0, :]
    ij = np.round(uv).astype(int)
    res = np.linalg.norm(uv - ij, axis=1)
    inside = (ij[:, 0] >= 0) & (ij[:, 0] < nc) & (ij[:, 1] >= 0) & (ij[:, 1] < nr)
    inl = inside & (res < tol)
    nodes = set(map(tuple, ij[inl]))
    return len(nodes), inl, uv, ij, res


def refine(P, H, nc, nr, tol=0.25, iters=6):
    for _ in range(iters):
        n, inl, uv, ij, res = node_score(P, H, nc, nr, tol=tol)
        if inl.sum() < 10:
            return H, n, None
        src = ij[inl].astype(np.float32)
        dst = P[inl, :2].astype(np.float32)
        Hn, _ = cv2.findHomography(src, dst, 0)
        if Hn is None:
            break
        H = np.linalg.inv(Hn)   # keep the image->model convention
    n, inl, uv, ij, res = node_score(P, H, nc, nr, tol=tol)
    return H, n, (inl, uv, ij, res)


def search(path, thr=200, amin=12, amax=700, hyps=((8, 5), (8, 7)), max_quads=4000, seed=0):
    g = load_gray(path)
    P = detect_blobs(g, None, thr=thr, amin=amin, amax=amax)
    out = dict(path=path, g=g, P=P, n=len(P), ok=False)
    if len(P) < 20:
        return out
    hv = hull_vertices(P)
    if len(hv) < 4:
        return out
    quads = list(itertools.combinations(range(len(hv)), 4))
    rng = np.random.default_rng(seed)
    if len(quads) > max_quads:
        quads = [quads[i] for i in rng.choice(len(quads), max_quads, replace=False)]
    best = None
    for nc, nr in hyps:
        for quad_idx in quads:
            q = order_cyclic(hv[list(quad_idx)])
            if cv2.contourArea(q.reshape(-1, 1, 2)) < 1000:
                continue
            for qa in quad_assignments(q):
                H = homography_quad(qa, nc, nr)
                n, inl, uv, ij, res = node_score(P, H, nc, nr)
                score = n - 0.5 * (nc * nr - n)
                if best is None or score > best[0]:
                    best = (score, n, nc, nr, qa.copy(), H.copy())
    if best is None:
        return out
    _, n, nc, nr, q, H = best
    H, n, extra = refine(P, H, nc, nr)
    if extra is None:
        return out
    inl, uv, ij, res = extra
    # recompute coverage and occupancy
    nodes = set(map(tuple, ij[inl]))
    occ = np.zeros((nr, nc), np.uint8)
    for (a, b) in nodes:
        occ[b, a] = 1
    # assign every detected point to its node (nearest integer, inside)
    allnodes = {}
    for k in range(len(P)):
        a, b = ij[k]
        if 0 <= a < nc and 0 <= b < nr and res[k] < 0.25:
            allnodes.setdefault((a, b), []).append(k)
    out.update(ok=True, nc=nc, nr=nr, quad=q, H=H, inl=inl, uv=uv, ij=ij, res=res,
               occ=occ, coverage=len(nodes) / (nc * nr), nodes=nodes, allnodes=allnodes,
               cover_n=n, hull=hvv if (hvv:=hv) is not None else hv)
    return out
