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
   points, and refine the homography with RANSAC;
6. **verify the identity of the labelling** with the four large markers and the
   chirality sign (:func:`big_marker_witness`), and rank candidates by that
   evidence before marker count/residual.

The result is a mapping ``(u, v) -> (x, y)`` in the canonical 8x5 frame, which
is the same board frame in every image.  Points that are not visible are
simply absent.

Step 6 is what keeps the labels honest.  The 40-point lattice is symmetric
under the Klein four-group, so ``identity``, ``rot180``, ``mirror_u`` and
``mirror_v`` all reproduce the same 40 image points with the same residual;
only the four *large* markers (three on-lattice at ``(2,1), (4,1), (5,1)`` and
one off-lattice chiral marker at ``(3.5, 3)``) can tell them apart, plus
``det(H) > 0`` for a front-facing plate.  An observation therefore carries
``witness``/``identity_ok`` and a mirrored labelling is never returned.
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


def thresholds_from_histogram(gray: np.ndarray, percentiles) -> dict[tuple[bool, float], int]:
    """All polarity/percentile thresholds in one 256-bin pass.

    ``np.percentile`` is exact but costs ~3 ms on a 1280x1024 frame, and the
    auto search needs six of them per camera (three percentiles, two
    polarities).  A histogram gives the same interpolated quantiles from a single
    O(n) pass: for an inverted image the quantile at p is the original image's
    quantile at 100-p, so both polarities come from one histogram.
    """
    img = np.ascontiguousarray(gray, dtype=np.uint8)
    hist = np.bincount(img.ravel(), minlength=256).astype(np.int64)
    total = int(hist.sum())
    cdf = np.cumsum(hist)
    out: dict[tuple[bool, float], int] = {}

    def quantile(frac: float) -> float:
        """Linear interpolation, matching numpy's default percentile method."""
        if total <= 1:
            return 0.0
        pos = (frac / 100.0) * (total - 1)
        lo = int(np.floor(pos))
        hi = min(lo + 1, total - 1)
        base = int(np.searchsorted(cdf, lo + 1, side="left"))
        value_lo = float(min(base, 255))
        base_hi = int(np.searchsorted(cdf, hi + 1, side="left"))
        value_hi = float(min(base_hi, 255))
        return value_lo + (value_hi - value_lo) * (pos - lo)

    for pct in percentiles:
        direct = quantile(float(pct))
        inverted = 255.0 - quantile(100.0 - float(pct))
        out[(False, float(pct))] = max(30, int(direct))
        out[(True, float(pct))] = max(30, int(inverted))
    return out


def threshold_for(gray: np.ndarray, invert: bool = False,
                  percentile: float = 99.0) -> int:
    """The binarisation threshold a full image would use for this combination.

    Exposed so a cropped (ROI) detection can use the *same* threshold as the
    full frame: computing a percentile inside a crop would change the threshold
    and therefore the result, which would silently alter calibration decisions.
    """
    img = gray if not invert else (255 - gray)
    return max(30, int(np.percentile(img, percentile)))


def detect_blobs(gray: np.ndarray, invert: bool = False,
                 percentile: float = 99.0,
                 min_area: float = 25.0, max_diameter: float = 60.0,
                 refine: bool = True, refine_mode: str = "centroid",
                 threshold: int | None = None,
                 offset: tuple[float, float] = (0.0, 0.0)) -> list[Blob]:
    """Detect circular marker blobs.  ``invert`` selects dark-on-light markers.

    ``threshold`` overrides the percentile (used by ROI detection so a crop sees
    the full-frame threshold) and ``offset`` shifts the returned centres back
    into full-image coordinates.
    """
    img = gray if not invert else (255 - gray)
    thr = int(threshold) if threshold is not None else max(
        30, int(np.percentile(img, percentile)))
    ox, oy = float(offset[0]), float(offset[1])
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
    if ox or oy:
        kept = [Blob(b.x + ox, b.y + oy, b.area, b.w, b.h) for b in kept]
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
    #: identity evidence: how many of the four large markers were found at the
    #: positions this labelling predicts (``-1`` = not evaluated, the historical
    #: "labels are unverified" state)
    witness_score: int = -1
    #: full :func:`big_marker_witness` result (``ok`` is the verified flag)
    witness: dict = field(default_factory=dict)

    @property
    def uv(self) -> np.ndarray:
        return np.array(sorted(self.points.keys()), float)

    @property
    def pixels(self) -> np.ndarray:
        return np.array([self.points[tuple(k)] for k in sorted(self.points.keys())], float)

    @property
    def identity_ok(self) -> bool:
        """True when the large-marker/chirality evidence verified this labelling."""
        return bool(self.witness.get("ok"))

    def as_dict(self) -> dict:
        return {
            "points": {f"{u},{v}": [float(x), float(y)] for (u, v), (x, y) in self.points.items()},
            "homography": self.homography.tolist(),
            "n_lattice": self.n_lattice,
            "rms": self.rms,
            "max_residual": self.max_residual,
            "big_markers": np.asarray(self.big_markers).tolist(),
            "detector": self.detector,
            "witness_score": int(self.witness_score),
            "witness": dict(self.witness),
        }


#: large/small marker area ratio.  Recovered plate: the large markers are
#: printed ~1.75x the small radius, i.e. ~3x the area; 1.55 is the measured
#: lower bound across the recorded corpus and the synthetic renders.
LARGE_AREA_RATIO = 1.55

#: An *identity-verified* observation with at least this many markers and this
#: lattice residual ends the polarity/threshold sweep after the first pass.
#: Twelve is the smallest support the wizard ever uses (``min_shared_markers``);
#: a free PnP fit with 10-12 of the 40 markers reproduces the 40-marker pose to
#: 0.09 deg / 0.12 mm (p95 0.22 deg / 0.37 mm) on the 109 recorded full-board
#: views, so a verified 12-marker labelling is already a usable observation.
VERIFIED_EARLY_MIN = 12
VERIFIED_EARLY_RMS = 0.5


def big_marker_witness(P: np.ndarray, A: np.ndarray, H: np.ndarray, pitch: float,
                       shape: tuple[int, int] | None = None, *,
                       small_area: float | None = None,
                       large_ratio: float = LARGE_AREA_RATIO,
                       tol_pitch: float = 0.30,
                       margin_px: float = 8.0,
                       min_visible: int = 3) -> dict:
    """Score the four large-marker witnesses of one ``(u,v) -> pixel`` labelling.

    This is the identity evidence: the three on-lattice large markers at
    ``(2,1), (4,1), (5,1)`` and the off-lattice chiral marker at ``(3.5, 3)``.
    For a labelling to be *verified* every witness that is predicted inside the
    frame must be a large blob at the predicted pixel, at least ``min_visible``
    witnesses must be visible, and ``det(H) > 0`` (a front-facing plate; a
    mirrored labelling has already been rejected upstream).

    With ``shape=None`` the historical strict reading is used: all four
    witnesses count, so three is not enough.  With an image shape a witness that
    is predicted outside the usable frame cannot be observed and is excused --
    this is what makes a partially visible plate verifiable.
    """
    big_uv = plate.big_points_uv()
    H = np.asarray(H, float)
    out = {"ok": False, "score": 0, "n_visible": 0, "det_h": float("nan"),
           "visible": [], "hits": [], "offsets_px": [], "area_ratio": [],
           "small_area_px": None, "pitch_px": float(pitch) if np.isfinite(pitch) else None}
    if H.shape != (3, 3) or not np.all(np.isfinite(H)) or not len(P):
        return out
    det = float(np.linalg.det(H))
    out["det_h"] = det
    pix = cv2.perspectiveTransform(big_uv.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
    d = np.linalg.norm(pix[:, None, :] - P[None, :, :], axis=2)
    near = d.argmin(1)
    dn = d.min(1)
    if small_area is None or not np.isfinite(small_area) or small_area <= 0:
        small_area = float(np.median(A)) if len(A) else float("nan")
    out["small_area_px"] = float(small_area) if np.isfinite(small_area) else None
    ratio = A[near] / max(float(small_area), 1e-9) if np.isfinite(small_area) else np.zeros(len(big_uv))
    if shape is None:
        visible = np.ones(len(big_uv), bool)
    else:
        h, w = int(shape[0]), int(shape[1])
        visible = ((pix[:, 0] >= margin_px) & (pix[:, 0] <= w - 1 - margin_px)
                   & (pix[:, 1] >= margin_px) & (pix[:, 1] <= h - 1 - margin_px))
    hits = (dn < float(tol_pitch) * float(pitch)) & (ratio > float(large_ratio))
    out.update(visible=[bool(v) for v in visible],
               hits=[bool(x) for x in hits],
               offsets_px=[float(x) for x in dn],
               area_ratio=[float(x) for x in ratio],
               n_visible=int(np.sum(visible)),
               score=int(np.sum(hits & visible)))
    n_visible = out["n_visible"]
    out["ok"] = bool(det > 0.0
                     and n_visible >= int(min_visible)
                     and out["score"] == n_visible)
    return out


def _matched_small_area(A: np.ndarray, idx: np.ndarray, inl: np.ndarray,
                        lattice: np.ndarray, big: np.ndarray) -> float:
    """Median area of the blobs matched to *small* lattice points."""
    is_big = np.min(np.linalg.norm(lattice[:, None, :] - big[None, :, :], axis=2), axis=1) < 0.3
    sel = inl & ~is_big
    if not np.any(sel):
        return float("nan")
    return float(np.median(A[idx[sel]]))


def _pattern_candidates(P: np.ndarray, A: np.ndarray):
    """Yield candidate (src3, q) large-marker configurations.

    ``src3`` are the three collinear points in canonical order (u=2,4,5) and
    ``q`` is the off-line point (u=3.5, v=3).
    """
    n = len(P)
    if n < 4:
        return []
    med = float(np.median(A))
    # Only *large* blobs can be anchors: the three collinear markers and the
    # chiral one are all rejected below unless their area exceeds 1.25x the
    # median.  Enumerating triples over every blob was the dominant cost on
    # low-threshold passes (126 blobs -> 325k triples); restricting the pool to
    # the large blobs is exactly equivalent and O(10) triples instead.
    pool = np.where(A > 1.25 * med)[0]
    if len(pool) < 4:
        return []
    tris = np.array(list(itertools.combinations(pool.tolist(), 3)), dtype=int)
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
        for k in pool:
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


def _evaluate_labelling(P: np.ndarray, H: np.ndarray, lattice: np.ndarray,
                        pitch: float, tol: float = 0.30
                        ) -> tuple[int, float, float, np.ndarray, np.ndarray, np.ndarray]:
    """Match the predicted lattice against the blobs for one homography.

    A lattice point is inlier only when its nearest blob is inside ``tol``
    pitches *and* clearly nearer than the runner-up (``0.55*``), so a marker can
    never be claimed by two labels.
    """
    pred = cv2.perspectiveTransform(lattice.reshape(-1, 1, 2).astype(np.float64),
                                    np.asarray(H, float)).reshape(-1, 2)
    d = np.linalg.norm(P[None, :, :] - pred[:, None, :], axis=2)
    idx = d.argmin(1)
    d1 = d.min(1)
    d2 = np.partition(d, 1, axis=1)[:, 1]
    inl = (d1 < tol * pitch) & (d1 < 0.55 * d2)
    n = int(inl.sum())
    rms = float(np.sqrt(np.mean(d1[inl] ** 2))) if n else 999.0
    mx = float(d1[inl].max()) if n else 999.0
    return n, rms, mx, inl, idx, pred


def _is_big_label(lattice: np.ndarray, big: np.ndarray, tol: float = 0.3) -> np.ndarray:
    return np.min(np.linalg.norm(lattice[:, None, :] - big[None, :, :], axis=2), axis=1) < tol


def _fit_from_anchor(P: np.ndarray, src3: np.ndarray, q: np.ndarray,
                     lattice: np.ndarray, big: np.ndarray):
    """Bootstrap and refine one labelling from a (3 collinear + chiral) anchor set.

    Three of the four anchors are collinear, and a four-point projective solve is
    *degenerate* for such a set (the DLT returns the degenerate map that sends the
    triple to the origin: measured ``det ~ 1e-15``), so the seed is an affine
    solve through the anchors followed by RANSAC on the lattice, as before.

    The refinement then fits only the *small* lattice points, deliberately
    leaving the four large markers out of the fit.  That keeps the large-marker
    witness independent evidence instead of a tautology, and it stops RANSAC
    from drifting onto a lattice symmetry: a 180-degree relabelling reproduces
    the same 40 pixels and was measured to beat the correct map by 0.004 px of
    rms on a real tilted frame (which silently discards the identity evidence
    the anchors carry).
    """
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
    if not np.isfinite(pitch) or pitch <= 0:
        return None
    near_big = np.min(np.linalg.norm(lattice[:, None, :] - big[None, :, :], axis=2), axis=1) < 2.6
    d = np.linalg.norm(P[None, :, :] - pred[:, None, :], axis=2)
    idx = d.argmin(1); d1 = d.min(1); d2 = np.partition(d, 1, axis=1)[:, 1]
    ok = (d1 < 0.8 * pitch) & (d1 < 0.8 * d2) & near_big
    if int(ok.sum()) < 8:
        return None
    # RANSAC is seeded from OpenCV's global RNG.  Without this, two runs of the
    # same recording can choose different inlier sets on a borderline view and
    # accept a different set of calibration views (observed: 29 vs 31 accepted
    # on the same 87 real groups).  A calibration decision must be reproducible.
    cv2.setRNGSeed(0)
    H, _ = cv2.findHomography(lattice[ok], P[idx[ok]], cv2.RANSAC, 0.7 * pitch)
    if H is None or not np.all(np.isfinite(H)):
        return None
    small = ~_is_big_label(lattice, big)
    for _ in range(6):
        n, rms, mx, inl, idx, _ = _evaluate_labelling(P, H, lattice, pitch)
        sel = inl & small
        if int(sel.sum()) < 8:
            break
        cv2.setRNGSeed(0)
        Hn, _ = cv2.findHomography(lattice[sel], P[idx[sel]], cv2.RANSAC, 0.30 * pitch)
        if Hn is None or not np.all(np.isfinite(Hn)):
            break
        H = Hn
    n, rms, mx, inl, idx, _ = _evaluate_labelling(P, H, lattice, pitch)
    if n == 0:
        return None
    return H, n, rms, mx, inl, idx, pitch


def assign_grid(blobs: list[Blob], lattice: Optional[np.ndarray] = None,
                big: Optional[np.ndarray] = None,
                shape: tuple[int, int] | None = None,
                early_n: int = 34) -> Optional[GridObservation]:
    """Assign canonical lattice coordinates to detected blobs.

    Every anchor candidate is fitted and then *verified*: the four large markers
    must be found where the final labelling predicts them, and a front-facing
    ``det(H) > 0`` outranks a mirrored one.  Candidates are ranked by
    verification first and by marker count/residual second; ranking by count and
    residual alone is unsound because the 40-point lattice is symmetric under the
    Klein four-group and a relabelled fit reproduces the same points with almost
    the same residual.  The first candidate that is verified on at least
    ``early_n`` markers is accepted at once, which keeps the common case at one
    fit.
    """
    if len(blobs) < 30:
        return None
    lattice = plate.lattice_uv() if lattice is None else lattice
    big = plate.big_points_uv() if big is None else big
    P = np.array([b.xy for b in blobs], float)
    A = np.array([b.area for b in blobs], float)
    best = None
    for _, src3, q in _pattern_candidates(P, A):
        r = _fit_from_anchor(P, src3, q, lattice, big)
        if r is None:
            continue
        H, n, rms, mx, inl, idx, pitch = r
        small = _matched_small_area(A, idx, inl, lattice, big)
        w = big_marker_witness(P, A, H, pitch, shape,
                               small_area=small if np.isfinite(small) else None)
        # front-facing first: a mirrored labelling is still *returned* (the
        # orientation witness reports it as mirrored_geometry, which is a
        # diagnostic the operator sees), but it can never outrank a
        # front-facing one -- and the live path refuses to carry it forward.
        key = (int(w["det_h"] > 0), int(w["ok"]), int(w["score"]), n, -rms)
        if best is None or key > best[0]:
            best = (key, H, n, rms, mx, inl, idx, pitch, w)
        if w["ok"] and n >= int(early_n):
            break
    if best is None:
        return None
    _, H, n, rms, mx, inl, idx, pitch, w = best
    pts = {}
    for k in np.where(inl)[0]:
        u, v = lattice[k]
        x, y = P[idx[k]]
        pts[(float(u), float(v))] = np.array([x, y])
    # pixel readout of the four canonical large-marker positions (diagnostics
    # only; the homography is what defines them)
    bm = cv2.perspectiveTransform(big.reshape(-1, 1, 2).astype(np.float64),
                                  np.asarray(H, float)).reshape(-1, 2)
    return GridObservation(pts, H, n, rms, mx, bm, witness_score=int(w["score"]),
                           witness=dict(w))


def detect_grid(gray: np.ndarray, invert: bool = False,
                percentile: float = 99.0,
                refine_mode: str = "centroid",
                threshold: int | None = None,
                offset: tuple[float, float] = (0.0, 0.0),
                shape: tuple[int, int] | None = None) -> Optional[GridObservation]:
    blobs = detect_blobs(gray, invert=invert, percentile=percentile,
                         refine_mode=refine_mode, threshold=threshold,
                         offset=offset)
    return assign_grid(blobs, shape=shape if shape is not None else gray.shape)


def detect_grid_auto(gray: np.ndarray,
                     refine_mode: str = "centroid",
                     roi: tuple[int, int, int, int] | None = None,
                     early_lattice: int | None = None,
                     early_rms: float | None = None
                     ) -> Optional[GridObservation]:
    """Try both polarities and several thresholds; return the best observation.

    ``roi`` is ``(x0, y0, x1, y1)`` in image coordinates: the heavy per-pixel
    work then runs on that crop only, while the thresholds are still computed on
    the **full** image and the returned coordinates are shifted back.  That makes
    the ROI path exactly equivalent to the full-frame one inside the crop and
    2-3x cheaper; the caller is responsible for the crop containing the plate.
    """
    full = gray
    offset = (0.0, 0.0)
    work = gray
    if roi is not None:
        x0, y0, x1, y1 = (int(v) for v in roi)
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(gray.shape[1], x1); y1 = min(gray.shape[0], y1)
        if x1 - x0 > 32 and y1 - y0 > 32:
            work = np.ascontiguousarray(gray[y0:y1, x0:x1])
            offset = (float(x0), float(y0))
    best = None
    first = True
    percentiles = (99.0, 99.4, 98.5)
    thresholds = thresholds_from_histogram(full, percentiles)
    for invert in (False, True):
        for pct in percentiles:
            thr = thresholds[(invert, float(pct))]
            obs = detect_grid(work, invert=invert, percentile=pct,
                              refine_mode=refine_mode, threshold=thr,
                              offset=offset, shape=full.shape)
            if first and obs is not None and obs.identity_ok:
                # Two early exits, both gated on a *verified* identity (without
                # that gate the exit froze a 180-degree relabelling that a later
                # threshold would have corrected):
                #  1. the caller's full-lattice rule (all markers, tiny residual);
                #  2. a verified partial observation with the smallest support
                #     the wizard uses and a residual far below the acceptance
                #     limit -- otherwise every clipped view paid all six
                #     polarity/threshold passes (measured p50 655 ms/frame on the
                #     real tail frames).
                full_board = (early_lattice is not None and early_rms is not None
                              and obs.n_lattice >= int(early_lattice)
                              and obs.rms <= float(early_rms))
                partial_ok = (obs.n_lattice >= VERIFIED_EARLY_MIN
                              and obs.rms <= max(VERIFIED_EARLY_RMS,
                                                 float(early_rms) if early_rms else 0.0))
                if full_board or partial_ok:
                    return obs
            first = False
            if obs is None:
                continue
            # verified identity first, then the witness count, then support
            key = (int(obs.identity_ok), int(obs.witness_score), obs.n_lattice, -obs.rms)
            if best is None or key > best[0]:
                best = (key, obs)
    return None if best is None else best[1]
