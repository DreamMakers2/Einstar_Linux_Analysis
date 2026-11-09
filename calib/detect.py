"""Marker detection and grid assignment for the Einstar calibration plate.

The plate is a white plate with 40 small circular holes and 4 large holes
(``calib.plate``).  The detector works on the raw 8-bit camera frame:

1. threshold the image (polarity is auto-detected, because the vendor saves
   the IR frames with bright holes while a normal photograph of the white
   board has dark holes);
2. find connected components, keep compact round blobs;
3. refine each centroid with an intensity-weighted local centroid;
4. find the 4 large markers by searching for the recovered orientation code
   (three collinear markers with 2:1 spacing plus one marker at the midpoint
   two rows away);
5. bootstrap a homography from the large-marker pattern, match all 40 lattice
   points, and refine the homography with RANSAC.

The result is a mapping ``(u, v) -> (x, y)`` in the canonical 8x5 frame, which
is the same board frame in every image.  Points that are not visible are
simply absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
from typing import Optional

import cv2
import numpy as np

from . import plate


# --------------------------------------------------------------------------
# blobs
# --------------------------------------------------------------------------

@dataclass
class Blob:
    x: float
    y: float
    area: float
    w: int
    h: int

    @property
    def xy(self) -> np.ndarray:
        return np.array([self.x, self.y], float)


def _weighted_centroid(gray: np.ndarray, x: int, y: int, r: int, invert: bool) -> tuple[float, float]:
    """Intensity-weighted centroid of a blob window (sub-pixel, robust)."""
    h, w = gray.shape
    x0, x1 = max(0, x - r), min(w, x + r + 1)
    y0, y1 = max(0, y - r), min(h, y + r + 1)
    patch = gray[y0:y1, x0:x1].astype(np.float64)
    if invert:
        patch = 255.0 - patch
    # local background from the window border
    border = np.concatenate([patch[0, :], patch[-1, :], patch[:, 0], patch[:, -1]])
    bg = float(np.median(border))
    wt = np.clip(patch - bg, 0.0, None)
    s = wt.sum()
    if s <= 1e-9:
        return float(x), float(y)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    return float((wt * xs).sum() / s), float((wt * ys).sum() / s)


def _ellipse_centre(gray: np.ndarray, x: int, y: int, r: int):
    """Fit an ellipse to the local edge and return its centre (sub-pixel)."""
    h, w = gray.shape
    x0, x1 = max(0, x - r), min(w, x + r + 1)
    y0, y1 = max(0, y - r), min(h, y + r + 1)
    patch = gray[y0:y1, x0:x1]
    if patch.size < 12:
        return None
    bg = float(np.percentile(patch, 10))
    peak = float(np.percentile(patch, 98))
    if peak - bg < 8:
        return None
    thr = bg + 0.5 * (peak - bg)
    _, bw = cv2.threshold(patch, thr, 255, cv2.THRESH_BINARY)
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if len(c) < 8:
        return None
    (cx, cy), (MA, ma), ang = cv2.fitEllipse(c)
    if not (2.0 < MA < 40 and 2.0 < ma < 40):
        return None
    return float(cx + x0), float(cy + y0)


def detect_blobs(gray: np.ndarray, invert: bool = False,
                 percentile: float = 99.0,
                 min_area: float = 25.0, max_diameter: float = 60.0,
                 refine: bool = True, refine_mode: str = "centroid") -> list[Blob]:
    """Detect circular marker blobs.  ``invert`` selects dark-on-light markers."""
    img = gray if not invert else (255 - gray)
    thr = max(30, int(np.percentile(img, percentile)))
    _, bw = cv2.threshold(img, thr, 255, cv2.THRESH_BINARY)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    out: list[Blob] = []
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if area < min_area or w > max_diameter * 2 or h > max_diameter * 2:
            continue
        if not (0.45 < w / max(h, 1) < 2.2):
            continue
        x, y = float(cent[i][0]), float(cent[i][1])
        if refine and refine_mode == "edge":
            try:
                from .detectors_audit import loc_edge_ellipse
                loc = loc_edge_ellipse(img, x, y, max(9, int(0.8 * max(w, h)) + 4),
                                       adaptive=True, invert=invert,
                                       prior_r=max(3.0, 0.6 * max(w, h)))
                if loc.ok:
                    x, y = float(loc.x), float(loc.y)
            except Exception:
                pass
        elif refine and refine_mode == "ellipse":
            e = _ellipse_centre(img, int(round(x)), int(round(y)),
                                max(6, int(0.9 * max(w, h))))
            # the ellipse fit can lock onto a neighbouring hole or a saturated
            # rim; accept it only when it agrees with the blob centroid
            if e is not None and (e[0] - x) ** 2 + (e[1] - y) ** 2 < (0.6 * max(w, h)) ** 2:
                x, y = e
        elif refine:
            x, y = _weighted_centroid(img, int(round(x)), int(round(y)),
                                      max(4, int(0.7 * max(w, h))), False)
        out.append(Blob(x, y, area, w, h))
    # drop duplicates from split blobs, keeping the larger one
    out.sort(key=lambda b: -b.area)
    kept: list[Blob] = []
    for b in out:
        if all((b.x - q.x) ** 2 + (b.y - q.y) ** 2 > 16.0 for q in kept):
            kept.append(b)
    return kept


# --------------------------------------------------------------------------
# grid assignment
# --------------------------------------------------------------------------

@dataclass
class GridObservation:
    """One view of the plate: canonical (u,v) -> pixel coordinates."""
    points: dict[tuple[float, float], np.ndarray]
    homography: np.ndarray            # lattice (u,v) -> pixel
    n_lattice: int
    rms: float
    max_residual: float
    big_markers: np.ndarray
    detector: str = "pattern-v1"

    @property
    def uv(self) -> np.ndarray:
        return np.array(sorted(self.points.keys()), float)

    @property
    def pixels(self) -> np.ndarray:
        return np.array([self.points[tuple(k)] for k in sorted(self.points.keys())], float)

    def as_dict(self) -> dict:
        return {
            "points": {f"{u},{v}": [float(x), float(y)] for (u, v), (x, y) in self.points.items()},
            "homography": self.homography.tolist(),
            "n_lattice": self.n_lattice,
            "rms": self.rms,
            "max_residual": self.max_residual,
            "big_markers": np.asarray(self.big_markers).tolist(),
            "detector": self.detector,
        }


def _pattern_candidates(P: np.ndarray, A: np.ndarray):
    """Yield candidate (src3, q) large-marker configurations.

    ``src3`` are the three collinear points in canonical order (u=2,4,5) and
    ``q`` is the off-line point (u=3.5, v=3).
    """
    n = len(P)
    if n < 4:
        return []
    med = float(np.median(A))
    tris = np.array(list(itertools.combinations(range(n), 3)), dtype=int)
    p = P[tris]
    d01 = np.linalg.norm(p[:, 0] - p[:, 1], axis=1)
    d02 = np.linalg.norm(p[:, 0] - p[:, 2], axis=1)
    d12 = np.linalg.norm(p[:, 1] - p[:, 2], axis=1)
    D = np.stack([d01, d02, d12], 1)
    ei = np.argmax(D, 1)
    E0 = np.zeros((len(tris), 2)); E1 = np.zeros_like(E0); M = np.zeros_like(E0)
    for k, (a, b) in enumerate([(0, 1), (0, 2), (1, 2)]):
        m = ei == k
        E0[m] = p[m, a]; E1[m] = p[m, b]; M[m] = p[m, 3 - a - b]
    L = np.linalg.norm(E1 - E0, axis=1)
    denom = L * L + 1e-12
    t = np.sum((M - E0) * (E1 - E0), axis=1) / denom
    line = np.abs((E1 - E0)[:, 0] * (M - E0)[:, 1] - (E1 - E0)[:, 1] * (M - E0)[:, 0]) / (L + 1e-12)
    ratio = np.minimum(t, 1 - t) / np.maximum(np.maximum(t, 1 - t), 1e-12)
    tri_area = np.stack([A[tris[:, 0]], A[tris[:, 1]], A[tris[:, 2]]], 1)
    area_sim = tri_area.min(1) / np.maximum(tri_area.max(1), 1e-12)
    sel = ((L > 25) & (line < 0.06 * L) & (ratio > 0.38) & (ratio < 0.64)
           & (area_sim > 0.35) & (tri_area.min(1) > 1.25 * med))
    out = []
    for ii in np.where(sel)[0]:
        src3 = np.array([E0[ii], M[ii], E1[ii]])
        if t[ii] < 0.5:
            src3 = src3[::-1]
        Lv = L[ii]
        e = E1[ii] - E0[ii]
        for k in range(n):
            if k in tris[ii]:
                continue
            if A[k] < 1.35 * med:
                continue
            q = P[k]
            tq = float(np.dot(q - E0[ii], e) / (Lv * Lv))
            off = abs(e[0] * (q - E0[ii])[1] - e[1] * (q - E0[ii])[0]) / Lv
            if not (0.34 < tq < 0.66 and off > 0.16 * Lv):
                continue
            score = float(A[list(tris[ii])].sum() + A[k])
            out.append((score, src3, q))
    out.sort(key=lambda z: -z[0])
    return out[:24]


def _fit_from_anchor(P: np.ndarray, src3: np.ndarray, q: np.ndarray,
                     lattice: np.ndarray, big: np.ndarray):
    src = np.vstack([src3, q])
    A = np.hstack([src, np.ones((4, 1))])
    M, *_ = np.linalg.lstsq(A, big, rcond=None)
    A2 = M.T
    if abs(np.linalg.det(A2[:, :2])) < 1e-12:
        return None
    Minv = np.linalg.inv(np.vstack([A2, [0, 0, 1]]))[:2, :]
    pred = lattice @ Minv[:, :2].T + Minv[:, 2]
    dd = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
    np.fill_diagonal(dd, 1e9)
    pitch = float(np.median(dd.min(1)))
    near_big = np.min(np.linalg.norm(lattice[:, None, :] - big[None, :, :], axis=2), axis=1) < 2.6
    d = np.linalg.norm(P[None, :, :] - pred[:, None, :], axis=2)
    idx = d.argmin(1); d1 = d.min(1); d2 = np.partition(d, 1, axis=1)[:, 1]
    ok = (d1 < 0.8 * pitch) & (d1 < 0.8 * d2) & near_big
    if ok.sum() < 8:
        return None
    H, _ = cv2.findHomography(lattice[ok], P[idx[ok]], cv2.RANSAC, 0.7 * pitch)
    if H is None:
        return None
    for _ in range(8):
        pred = cv2.perspectiveTransform(lattice.reshape(-1, 1, 2), H).reshape(-1, 2)
        d = np.linalg.norm(P[None, :, :] - pred[:, None, :], axis=2)
        idx = d.argmin(1); d1 = d.min(1); d2 = np.partition(d, 1, axis=1)[:, 1]
        inl = (d1 < 0.32 * pitch) & (d1 < 0.55 * d2)
        if inl.sum() < 30:
            break
        Hn, _ = cv2.findHomography(lattice[inl], P[idx[inl]], cv2.RANSAC, 0.18 * pitch)
        if Hn is None:
            break
        H = Hn
    pred = cv2.perspectiveTransform(lattice.reshape(-1, 1, 2), H).reshape(-1, 2)
    d = np.linalg.norm(P[None, :, :] - pred[:, None, :], axis=2)
    d1 = d.min(1); d2 = np.partition(d, 1, axis=1)[:, 1]
    inl = (d1 < 0.30 * pitch) & (d1 < 0.55 * d2)
    n = int(inl.sum())
    rms = float(np.sqrt(np.mean(d1[inl] ** 2))) if n else 999.0
    mx = float(d1[inl].max()) if n else 999.0
    return H, n, rms, mx, inl, idx, pitch


def assign_grid(blobs: list[Blob], lattice: Optional[np.ndarray] = None,
                big: Optional[np.ndarray] = None) -> Optional[GridObservation]:
    """Assign canonical lattice coordinates to detected blobs."""
    if len(blobs) < 30:
        return None
    lattice = plate.lattice_uv() if lattice is None else lattice
    big = plate.big_points_uv() if big is None else big
    P = np.array([b.xy for b in blobs], float)
    A = np.array([b.area for b in blobs], float)
    cands = _pattern_candidates(P, A)
    best = None
    for _, src3, q in cands:
        r = _fit_from_anchor(P, src3, q, lattice, big)
        if r is None:
            continue
        H, n, rms, mx, inl, idx, pitch = r
        key = (n, -rms)
        if best is None or key > best[0]:
            best = (key, H, n, rms, mx, inl, idx)
    if best is None:
        return None
    _, H, n, rms, mx, inl, idx = best
    pts = {}
    for k in np.where(inl)[0]:
        u, v = lattice[k]
        x, y = P[idx[k]]
        pts[(float(u), float(v))] = np.array([x, y])
    bm = np.array([src for src in []])  # big markers are the anchors; keep empty
    return GridObservation(pts, H, n, rms, mx, bm)


def detect_grid(gray: np.ndarray, invert: bool = False,
                percentile: float = 99.0,
                refine_mode: str = "centroid") -> Optional[GridObservation]:
    blobs = detect_blobs(gray, invert=invert, percentile=percentile,
                         refine_mode=refine_mode)
    obs = assign_grid(blobs)
    if obs is not None:
        return obs
    return None


def detect_grid_auto(gray: np.ndarray,
                     refine_mode: str = "centroid") -> Optional[GridObservation]:
    """Try both polarities and several thresholds; return the best observation."""
    best = None
    for invert in (False, True):
        for pct in (99.0, 99.4, 98.5):
            obs = detect_grid(gray, invert=invert, percentile=pct,
                              refine_mode=refine_mode)
            if obs is None:
                continue
            key = (obs.n_lattice, -obs.rms)
            if best is None or key > best[0]:
                best = (key, obs)
    return None if best is None else best[1]
