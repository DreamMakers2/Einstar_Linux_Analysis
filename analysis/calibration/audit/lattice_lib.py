"""Offline plate-image analysis helpers (no calibration, no stereo baseline).

Pipeline
--------
1. bright-blob detection (holes are bright in the vendor IR corpus),
2. lattice pitch from the nearest-neighbour distance mode,
3. axial-angle voting over short displacement vectors -> two lattice axes,
4. integer assignment + iterative homography refinement (perspective-safe),
5. occupancy grid and per-image geometry.

Nothing here uses the CCF baseline or any calibration result; the only metric
anchor used elsewhere is the physical plate outline.
"""
from __future__ import annotations
import cv2, numpy as np
from scipy.spatial import cKDTree


def load_gray(path):
    g = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if g is None:
        raise FileNotFoundError(path)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY)
    return g


def detect_blobs(g, mask=None, thr=90, amin=25, amax=700, wmin=5, wmax=28):
    bw = (g > thr).astype(np.uint8)
    if mask is not None:
        bw &= mask
    n, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    out = []
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        w = stats[i, cv2.CC_STAT_WIDTH]; h = stats[i, cv2.CC_STAT_HEIGHT]
        if amin <= a <= amax and wmin <= w <= wmax and wmin <= h <= wmax and 0.5 < w / h < 2.0:
            out.append((cent[i][0], cent[i][1], a, w, h))
    return np.array(out, float)


def pitch_estimate(P):
    d, _ = cKDTree(P[:, :2]).query(P[:, :2], k=2)
    nn = d[:, 1]
    # mode via histogram of the 20-80 percentile band
    lo, hi = np.percentile(nn, [20, 80])
    hist, edges = np.histogram(nn, bins=40, range=(lo, hi))
    p = 0.5 * (edges[np.argmax(hist)] + edges[np.argmax(hist) + 1])
    return p


def axis_vectors(P, p):
    """Return two lattice vectors from axial-angle voting."""
    diffs = []
    for i in range(len(P)):
        d = P[i + 1:, :2] - P[i, :2]
        L = np.hypot(d[:, 0], d[:, 1])
        m = (L > 0.82 * p) & (L < 1.20 * p)
        if m.any():
            diffs.append(d[m])
    D = np.vstack(diffs)
    th = np.arctan2(D[:, 1], D[:, 0])
    # axial density on [0, pi)
    bins = np.linspace(0, np.pi, 91)
    h, _ = np.histogram(th % np.pi, bins=bins)
    h = np.convolve(np.r_[h[-2:], h, h[:2]], np.ones(3) / 3, 'same')[2:-2]
    k = int(np.argmax(h))
    th0 = 0.5 * (bins[k] + bins[k + 1])
    def sel(delta):
        dd = np.angle(np.exp(1j * 2 * (th - (th0 + delta)))) / 2
        return np.abs(dd) < np.radians(12)
    e1 = D[sel(0)].mean(0) if sel(0).sum() > 5 else None
    e2 = D[sel(np.pi / 2)].mean(0) if sel(np.pi / 2).sum() > 5 else None
    if e1 is None or e2 is None:
        # fall back to the strongest perpendicular pair among long vectors
        return None
    # canonical signs: e1 to +x, e2 so that (e1,e2) is right-handed
    if e1[0] < 0: e1 = -e1
    if e2[1] < 0 and abs(e2[0]) < abs(e2[1]):
        e2 = -e2
    if e1[0] * e2[1] - e1[1] * e2[0] < 0:
        e2 = -e2
    # orthogonalise e2 slightly against e1 (rectangular lattice)
    e2 = e2 - e1 * (np.dot(e1, e2) / np.dot(e1, e1))
    return np.column_stack([e1, e2])


def assign_homography(P, M, nit=8, tol=0.22):
    c = P[:, :2].mean(0)
    s = int(np.argmin(((P[:, :2] - c) ** 2).sum(1)))
    O = P[s, :2].astype(np.float64)
    ij = np.linalg.solve(M, (P[:, :2] - O).T).T
    H = None
    inl = np.ones(len(P), bool)
    for it in range(nit):
        ijr = np.round(ij)
        # residual in pixels under current model
        if H is None:
            pred = O + ijr @ M.T
        else:
            pred = cv2.perspectiveTransform(ijr[None].astype(np.float32), H)[0]
        resid = np.linalg.norm(pred - P[:, :2], axis=1)
        scale = np.median(np.linalg.norm(M, axis=0))
        inl = resid < tol * scale
        if inl.sum() < 12:
            break
        Hn, _ = cv2.findHomography(ijr[inl].astype(np.float32),
                                   P[inl, :2].astype(np.float32), 0)
        if Hn is None:
            break
        H = Hn
        pred_all = cv2.perspectiveTransform(ijr[None].astype(np.float32), H)[0]
        # re-estimate continuous coords by inverting the homography
        Hi = np.linalg.inv(H)
        uv = cv2.perspectiveTransform(P[:, None, :2].astype(np.float32), Hi)[:, 0, :]
        ij = uv
    ijr = np.round(ij)
    pred = cv2.perspectiveTransform(ijr[None].astype(np.float32), H)[0] if H is not None else O + ijr @ M.T
    resid = np.linalg.norm(pred - P[:, :2], axis=1)
    return dict(ij=ijr, ijf=ij, H=H, M=M, O=O, resid=resid, inlier=inl)


def analyse(path, thr=90, amin=25, amax=700, mask=None, erode=0):
    g = load_gray(path)
    m = mask
    if m is not None and erode:
        m = cv2.erode(m, np.ones((erode, erode), np.uint8))
    P = detect_blobs(g, m, thr=thr, amin=amin, amax=amax)
    if len(P) < 12:
        return dict(path=path, ok=False, n=len(P))
    p = pitch_estimate(P)
    M = axis_vectors(P, p)
    if M is None:
        return dict(path=path, ok=False, n=len(P), reason='no axes')
    fit = assign_homography(P, M)
    inl = fit['inlier']
    areas = P[inl, 2]
    med = np.median(areas) if len(areas) else np.nan
    return dict(path=path, ok=True, g=g, P=P, fit=fit, pitch_px=p,
                med_area=med, big=areas > 1.8 * med, inl_areas=areas)
