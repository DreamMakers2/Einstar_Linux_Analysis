"""Corner-quad grid hypothesis fitting for hole clouds (offline, no calibration).

The four extreme holes of a full rectangular grid are the vertices of the
maximum-area quadrilateral inscribed in the point set.  Mapping them to the
four grid corners gives a homography under which every hole must fall on an
integer node; coverage and residuals identify the true (ncols, nrows).
"""
from __future__ import annotations
import itertools
import cv2, numpy as np
from lattice_lib import load_gray, detect_blobs


def max_area_quad(P):
    hull = cv2.convexHull(P[:, :2].astype(np.float32)).reshape(-1, 2)
    if len(hull) < 4:
        return None
    if len(hull) > 24:
        # reduce hull with a coarser approximation
        hull = cv2.approxPolyDP(hull.reshape(-1, 1, 2), 0.01 * cv2.arcLength(hull.reshape(-1, 1, 2), True), True).reshape(-1, 2)
    best = (-1, None)
    idx = range(len(hull))
    for a, b, c, d in itertools.combinations(idx, 4):
        q = hull[[a, b, c, d]]
        # order around centroid
        ctr = q.mean(0)
        ang = np.arctan2(q[:, 1] - ctr[1], q[:, 0] - ctr[0])
        q = q[np.argsort(ang)]
        area = 0.5 * abs(np.dot(q[:, 0], np.roll(q[:, 1], -1)) - np.dot(q[:, 1], np.roll(q[:, 0], -1)))
        if area > best[0]:
            best = (area, q)
    return best[1]


def order_quad(q):
    ctr = q.mean(0)
    ang = np.arctan2(q[:, 1] - ctr[1], q[:, 0] - ctr[0])
    q = q[np.argsort(ang)]
    # start at the corner with min x+y, keep clockwise in image coords
    k = int(np.argmin(q[:, 0] + q[:, 1]))
    q = np.roll(q, -k, axis=0)
    cr = (q[1, 0]-q[0, 0])*(q[2, 1]-q[0, 1]) - (q[1, 1]-q[0, 1])*(q[2, 0]-q[0, 0])
    if cr < 0:
        q = q[[0, 3, 2, 1]]
    return q.astype(np.float32)


def score_hyp(P, quad, nc, nr, shift=0):
    q = np.roll(quad, -shift, axis=0)
    dst = np.array([[0, 0], [nc-1, 0], [nc-1, nr-1], [0, nr-1]], np.float32)
    H = cv2.getPerspectiveTransform(q, dst)
    uv = cv2.perspectiveTransform(P[:, None, :2].astype(np.float32), H)[:, 0, :]
    ij = np.round(uv).astype(int)
    res = np.linalg.norm(uv - ij, axis=1)
    inside = (ij[:, 0] >= 0) & (ij[:, 0] < nc) & (ij[:, 1] >= 0) & (ij[:, 1] < nr)
    # residual measured in image space at the assigned node
    nodes = np.column_stack([ij[:, 0], ij[:, 1]]).astype(np.float32)
    pred = cv2.perspectiveTransform(nodes[None], np.linalg.inv(H))[0]
    ires = np.linalg.norm(pred - P[:, :2], axis=1)
    inl = inside & (ires < 0.30 * np.median(np.abs(ires)) + 1)  # provisional, refined below
    # use model-space tolerance instead
    inl = inside & (res < 0.25)
    uniq = len(set(map(tuple, ij[inl])))
    return dict(H=H, uv=uv, ij=ij, res=res, ires=ires, inl=inl,
                coverage=uniq / (nc * nr), uniq=uniq, n=inl.sum())


def analyse(path, thr=200, amin=12, amax=700, cands=((8, 5), (8, 7), (8, 6), (8, 4), (5, 8), (7, 8))):
    g = load_gray(path)
    P = detect_blobs(g, None, thr=thr, amin=amin, amax=amax)
    out = dict(path=path, g=g, P=P, n=len(P), hyps={})
    if len(P) < 20:
        out['ok'] = False
        return out
    q = max_area_quad(P)
    if q is None:
        out['ok'] = False
        return out
    q = order_quad(q)
    out['quad'] = q
    best = None
    for nc, nr in cands:
        bs = None
        for sh in range(4):
            s = score_hyp(P, q, nc, nr, sh)
            if bs is None or (s['coverage'], -np.median(s['res'][s['inl']])) > (bs['coverage'], -np.median(bs['res'][bs['inl']])):
                bs = s; bs['shift'] = sh
        out['hyps'][(nc, nr)] = bs
    out['best'] = max(out['hyps'], key=lambda k: (round(out['hyps'][k]['coverage'], 2),
                                                  -np.median(out['hyps'][k]['res'][out['hyps'][k]['inl']])))
    out['ok'] = True
    return out
