"""Hypothesis test: which regular grid does a hole cloud form?

For each candidate (ncols, nrows) the four corners of the minimum-area
rectangle of the *density-filtered* hole cloud are mapped to the grid corners,
all points are mapped through that homography, and the fraction of grid nodes
occupied (one point per node, residual < 0.25 pitch) is scored.  No
calibration, no baseline.
"""
from __future__ import annotations
import cv2, numpy as np
from scipy.spatial import cKDTree
from lattice_lib import load_gray, detect_blobs


def density_filter(P, p=None, k=2, fac=1.35, iters=4):
    P = P.copy()
    for _ in range(iters):
        if len(P) < 8:
            break
        if p is None:
            d, _ = cKDTree(P[:, :2]).query(P[:, :2], k=2)
            p = np.median(d[:, 1])
        d, _ = cKDTree(P[:, :2]).query(P[:, :2], k=min(len(P), 8))
        n = (d[:, 1:] < fac * p).sum(1)
        keep = n >= k
        if keep.sum() < 8:
            break
        if keep.all():
            break
        P = P[keep]
        d2, _ = cKDTree(P[:, :2]).query(P[:, :2], k=2)
        p = np.median(d2[:, 1])
    return P, p


def fit_hyp(P, nc, nr):
    rect = cv2.minAreaRect(P[:, :2].astype(np.float32))
    box = cv2.boxPoints(rect).astype(np.float32)
    ctr = box.mean(0)
    ang = np.arctan2(box[:, 1] - ctr[1], box[:, 0] - ctr[0])
    box = box[np.argsort(ang)]
    k = int(np.argmin(box[:, 0] + box[:, 1]))
    box = np.roll(box, -k, axis=0)
    if (box[1, 0]-box[0, 0])*(box[2, 1]-box[0, 1]) - (box[1, 1]-box[0, 1])*(box[2, 0]-box[0, 0]) < 0:
        box = box[[0, 3, 2, 1]]
    dst = np.array([[0, 0], [nc-1, 0], [nc-1, nr-1], [0, nr-1]], np.float32)
    H = cv2.getPerspectiveTransform(box, dst)
    uv = cv2.perspectiveTransform(P[:, None, :2].astype(np.float32), H)[:, 0, :]
    ij = np.round(uv).astype(int)
    res = np.linalg.norm(uv - ij, axis=1)
    inside = (ij[:, 0] >= 0) & (ij[:, 0] < nc) & (ij[:, 1] >= 0) & (ij[:, 1] < nr)
    inl = inside & (res < 0.25)
    nodes = set(map(tuple, ij[inl]))
    return dict(H=H, box=box, uv=uv, ij=ij, res=res, inl=inl,
                coverage=len(nodes) / (nc * nr), nodes=nodes, n_in=inl.sum())


def analyse(path, thr=200, amin=12, amax=700, candidates=((8,5),(8,7),(8,6),(8,4),(7,5),(9,5))):
    g = load_gray(path)
    P0 = detect_blobs(g, None, thr=thr, amin=amin, amax=amax)
    P, p = density_filter(P0)
    out = dict(path=path, g=g, P=P, p=p, n0=len(P0), n=len(P), hyps={})
    if len(P) < 12:
        out['ok'] = False
        return out
    for (nc, nr) in candidates:
        out['hyps'][(nc, nr)] = fit_hyp(P, nc, nr)
    # choose best coverage, tie-break lower median residual
    def key(k):
        h = out['hyps'][k]
        return (round(h['coverage'], 2), -np.median(h['res'][h['inl']]) if h['inl'].any() else -9)
    out['best'] = max(out['hyps'], key=key)
    out['ok'] = True
    return out
