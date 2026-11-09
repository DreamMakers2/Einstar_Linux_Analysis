"""Plate-quad rectification + grid indexing (offline, no calibration)."""
from __future__ import annotations
import cv2, numpy as np
from lattice_lib import load_gray, detect_blobs


def plate_quad(g, erode=6):
    b = cv2.GaussianBlur(g, (9, 9), 0)
    lo, _ = cv2.threshold(b, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    lo = max(lo * 0.55, 20)
    m = (b > lo).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = (lab == idx).astype(np.uint8)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cnts, key=cv2.contourArea)
    # fit 4 lines by splitting the contour into 4 sides via extreme points
    hull = cv2.convexHull(c).reshape(-1, 2).astype(np.float64)
    if len(hull) < 4:
        return None
    # use minimum-area rectangle as an initialisation, then refine each side
    rect = cv2.minAreaRect(hull.astype(np.float32))
    box = cv2.boxPoints(rect).astype(np.float64)
    # order: top-left, top-right, bottom-right, bottom-left (by angle)
    ctr = box.mean(0)
    ang = np.arctan2(box[:, 1] - ctr[1], box[:, 0] - ctr[0])
    box = box[np.argsort(ang)]  # ccw-ish
    # rotate so first is top-left (min x+y)
    k = int(np.argmin(box[:, 0] + box[:, 1]))
    box = np.roll(box, -k, axis=0)
    # ensure clockwise order in image coords
    cr = (box[1,0]-box[0,0])*(box[2,1]-box[0,1]) - (box[1,1]-box[0,1])*(box[2,0]-box[0,0])
    if cr < 0:
        box = box[[0, 3, 2, 1]]
    return box, mask


def homography_from_quad(quad, W=1.0, H=1.0):
    dst = np.array([[0, 0], [W, 0], [W, H], [0, H]], np.float32)
    return cv2.getPerspectiveTransform(quad.astype(np.float32), dst)


def analyse_image(path, thr=90, amin=25, amax=600, erode=6, expected=(8, 5)):
    g = load_gray(path)
    q = plate_quad(g, erode=erode)
    if q is None:
        return dict(path=path, ok=False, reason='no plate')
    quad, mask = q
    H = homography_from_quad(quad, 1.0, 1.0)
    P = detect_blobs(g, mask if erode else None, thr=thr, amin=amin, amax=amax)
    if len(P) < 12:
        return dict(path=path, ok=False, reason='few blobs', quad=quad, n=len(P))
    uv = cv2.perspectiveTransform(P[:, None, :2].astype(np.float32), H)[:, 0, :]
    # robust: keep points inside the unit square (allow small margin)
    keep = (uv[:, 0] > -0.02) & (uv[:, 0] < 1.02) & (uv[:, 1] > -0.02) & (uv[:, 1] < 1.02)
    uv = uv[keep]; Q = P[keep]
    # separate big/small by area
    areas = Q[:, 2]
    med = np.median(areas)
    big = areas > 1.8 * med
    return dict(path=path, ok=True, g=g, quad=quad, H=H, P=Q, uv=uv, areas=areas,
                med=med, big=big, mask=mask)


def index_grid(u, v, ncols=8, nrows=5, tol=0.35):
    """Assign column/row indices to normalised coordinates and report residuals."""
    # cluster u into ncols bins by k-means on sorted values
    from scipy.cluster.vq import kmeans2
    def cluster(x, k):
        if len(x) < k:
            return None, None
        c, l = kmeans2(x.reshape(-1, 1), k, minit='++', seed=0)
        order = np.argsort(c[:, 0]); rank = np.empty(k, int); rank[order] = np.arange(k)
        return c[:, 0][order], rank[l]
    cu, iu = cluster(u, ncols)
    cv_, iv = cluster(v, nrows)
    if cu is None or cv_ is None:
        return None
    return cu, iu, cv_, iv
