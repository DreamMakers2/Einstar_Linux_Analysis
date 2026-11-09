"""Rectangle-axis lattice recovery for the Einstar plate images.

The hole cloud of an N x M regular grid has a rectangular convex hull whose
edges are the lattice axes.  We take those axes from the minimum-area
rectangle, cluster projections, assign integer indices, then refine with a
homography.  No calibration/baseline input.
"""
from __future__ import annotations
import cv2, numpy as np
from scipy.spatial import cKDTree
from lattice_lib import load_gray, detect_blobs


def _pitch(P):
    d, _ = cKDTree(P[:, :2]).query(P[:, :2], k=2)
    nn = d[:, 1]
    lo, hi = np.percentile(nn, [25, 75])
    h, e = np.histogram(nn, bins=30, range=(lo, hi))
    return 0.5 * (e[np.argmax(h)] + e[np.argmax(h) + 1])


def _rect_axes(P):
    rect = cv2.minAreaRect(P[:, :2].astype(np.float32))
    (cx, cy), (w, h), ang = rect
    a = np.radians(ang)
    # minAreaRect convention: width along (cos a, sin a)
    e_w = np.array([np.cos(a), np.sin(a)])
    e_h = np.array([-np.sin(a), np.cos(a)])
    if w >= h:
        return e_w, e_h, w, h
    return e_h, e_w, h, w


def _cluster1d(x, k, iters=60):
    # robust 1-D k-means with quantile init
    q = np.quantile(x, np.linspace(0, 1, k))
    c = q.copy()
    for _ in range(iters):
        d = np.abs(x[:, None] - c[None, :])
        l = d.argmin(1)
        for j in range(k):
            if (l == j).any():
                c[j] = x[l == j].mean()
        c = np.sort(c)
    d = np.abs(x[:, None] - c[None, :])
    l = d.argmin(1)
    return c, l


def fit(path, thr=200, amin=12, amax=700, expected=None, verbose=False):
    g = load_gray(path)
    P = detect_blobs(g, None, thr=thr, amin=amin, amax=amax)
    if len(P) < 12:
        return dict(ok=False, n=len(P), path=path)
    p = _pitch(P)
    keep = np.ones(len(P), bool)
    for it in range(5):
        Q = P[keep]
        e1, e2, w, h = _rect_axes(Q)
        u = (P[:, :2] - Q[:, :2].mean(0)) @ e1
        v = (P[:, :2] - Q[:, :2].mean(0)) @ e2
        ku = max(2, int(round(w / p)) + 1)
        kv = max(2, int(round(h / p)) + 1)
        # clamp to plausible plate grids
        ku = min(ku, 12); kv = min(kv, 12)
        cu, lu = _cluster1d(u, ku)
        cvv, lv = _cluster1d(v, kv)
        # residual to nearest cluster centre
        ru = np.abs(u - cu[lu]); rv = np.abs(v - cvv[lv])
        newkeep = (ru < 0.30 * p) & (rv < 0.30 * p)
        if newkeep.sum() < 12:
            break
        keep = newkeep
    Q = P[keep]
    e1, e2, w, h = _rect_axes(Q)
    u = (P[:, :2] - Q[:, :2].mean(0)) @ e1
    v = (P[:, :2] - Q[:, :2].mean(0)) @ e2
    ku = min(max(2, int(round(w / p)) + 1), 12)
    kv = min(max(2, int(round(h / p)) + 1), 12)
    cu, lu = _cluster1d(u, ku); cvv, lv = _cluster1d(v, kv)
    ii = np.argsort(np.argsort(cu))[lu]
    jj = np.argsort(np.argsort(cvv))[lv]
    # homography refinement
    H = None
    inl = keep.copy()
    for _ in range(6):
        src = np.column_stack([ii[inl], jj[inl]]).astype(np.float32)
        dst = P[inl, :2].astype(np.float32)
        if len(src) < 6: break
        Hn, _ = cv2.findHomography(src, dst, 0)
        if Hn is None: break
        allp = cv2.perspectiveTransform(np.column_stack([ii, jj])[None].astype(np.float32), Hn)[0]
        res = np.linalg.norm(allp - P[:, :2], axis=1)
        inl = res < 0.22 * p
        H = Hn
    allp = cv2.perspectiveTransform(np.column_stack([ii, jj])[None].astype(np.float32), H)[0]
    res = np.linalg.norm(allp - P[:, :2], axis=1)
    return dict(ok=True, path=path, g=g, P=P, p=p, ii=ii, jj=jj, inl=inl, resid=res,
                H=H, ku=ku, kv=kv, e1=e1, e2=e2, areas=P[:, 2], keep=keep)
