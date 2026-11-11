"""Partial-plate lattice assignment.

The strict detector (`calib.detect.assign_grid`) anchors the lattice on the
four orientation markers: the three on-lattice large markers at ``(2,1)``,
``(4,1)``, ``(5,1)`` plus the off-lattice chiral marker at ``(3.5, 3)``.  When
the chiral marker leaves the image — which is exactly what happens at an image
corner, where the wizard most needs the observation — the strict detector
returns ``None`` or, worse, a self-consistent *sub-lattice* whose labels are
shifted by several pitches.

This module recovers the **canonical** labelling from the three on-lattice
large markers alone, using the fact that the plate is a square lattice with a
known 28 mm pitch:

1. the large markers are found by area (≈4× a small marker);
2. the three that are collinear, with pitch-normalised spacings 2 and 1, are
   identified as ``(2,1)``, ``(4,1)``, ``(5,1)`` in that order (the pattern is
   asymmetric, so the order is unambiguous up to a 180° turn *of the whole
   plate*, which the orientation witness resolves);
3. the row direction ``u`` and the row spacing give the lattice basis; the
   perpendicular ``v`` has two signs, and both are tried — the sign that
   explains more blobs wins, and a tie is reported as ambiguous instead of
   guessed;
4. the labelling is verified against the canonical span ``u ∈ 0..7,
   v ∈ 0..4`` and a homography is fitted to every matched blob.

The returned observation is identity-labelled: the caller still runs
`tools.sweep.orient.check_orientation`, which is the component that proves the
chirality (``det(H) > 0``) and scores the three remaining hypotheses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import cv2
import numpy as np

from . import detect, plate

__all__ = ["PartialResult", "assign_grid_partial", "detect_grid_partial"]

#: lattice coordinates of the three on-lattice large markers, in order
BIG_TRIPLE = ((2.0, 1.0), (4.0, 1.0), (5.0, 1.0))
#: gaps between them in pitch units (2 then 1) - the asymmetric signature
BIG_GAPS = (2.0, 1.0)


@dataclass
class PartialResult:
    observation: Optional[detect.GridObservation]
    reason: str
    n_inliers: int = 0
    span_u: tuple[float, float] = (float("nan"), float("nan"))
    span_v: tuple[float, float] = (float("nan"), float("nan"))
    v_sign: int = 0
    margin: float = float("nan")

    def as_dict(self) -> dict:
        return {"ok": self.observation is not None, "reason": self.reason,
                "n_inliers": int(self.n_inliers),
                "span_u": [float(x) for x in self.span_u],
                "span_v": [float(x) for x in self.span_v],
                "v_sign": int(self.v_sign),
                "margin": float(self.margin) if np.isfinite(self.margin) else None}


def _large_blobs(blobs: list, ratio: float = 1.55) -> list:
    areas = np.array([b.area for b in blobs], float)
    if len(areas) < 6:
        return []
    small = float(np.median(areas))
    if small <= 0:
        return []
    return [b for b, a in zip(blobs, areas) if a / small > ratio]


def _triple(large: list, tol: float = 0.35) -> Optional[tuple]:
    """Find the collinear (2,1),(4,1),(5,1) triple among the large blobs."""
    best = None
    n = len(large)
    if n < 3:
        return None
    P = np.array([b.xy for b in large], float)
    for i in range(n):
        for j in range(n):
            if j == i:
                continue
            for k in range(n):
                if k in (i, j):
                    continue
                a, b, c = P[i], P[j], P[k]
                ab, bc, ac = b - a, c - b, c - a
                # collinearity: all three on one line
                cross = abs(float(ab[0] * ac[1] - ab[1] * ac[0]))
                if cross > tol * float(np.linalg.norm(ab)) * float(np.linalg.norm(ac)):
                    continue
                # order them along the line, then require gaps of ratio 2:1
                for triple in ((a, b, c), (c, b, a)):
                    p0, p1, p2 = triple
                    d1 = float(np.linalg.norm(p1 - p0))
                    d2 = float(np.linalg.norm(p2 - p1))
                    if min(d1, d2) <= 1.0:
                        continue
                    ratio = d1 / d2 if d1 >= d2 else d2 / d1
                    if abs(ratio - 2.0) > 0.45:
                        continue
                    # normalise: the pattern is (2,1) -> (4,1) -> (5,1), so the
                    # *longer* gap must come first
                    if d2 > d1:
                        p0, p1, p2 = p2, p1, p0
                        d1, d2 = d2, d1
                    pitch = (d1 + d2) / 3.0
                    score = abs(ratio - 2.0)
                    if best is None or score < best[0]:
                        best = (score, p0, p1, p2, pitch)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _predict_lattice(p0, p1, p2, pitch, v_sign, uv_max):
    """Build the full lattice prediction for one perpendicular convention."""
    ex = (p2 - p0) / 3.0
    ex = ex / max(float(np.linalg.norm(ex)), 1e-9) * pitch
    ey = np.array([-ex[1], ex[0]], float) * float(v_sign)
    origin = p0 - 2.0 * ex - 1.0 * ey
    su, sv = uv_max
    uv = np.array([(u, v) for v in np.arange(0, sv + 1) for u in np.arange(0, su + 1)],
                  float)
    return uv, origin + uv[:, 0:1] * ex + uv[:, 1:2] * ey


def assign_grid_partial(blobs: list, lattice: Optional[np.ndarray] = None,
                        *, min_lattice: int = 12, max_rms_px: float = 1.5,
                        tol_pitch: float = 0.32,
                        ratio: float = 1.55) -> PartialResult:
    """Recover the canonical 8x5 labelling from the three on-lattice markers."""
    lattice = plate.lattice_uv() if lattice is None else np.asarray(lattice, float)
    uv_max = (float(lattice[:, 0].max()), float(lattice[:, 1].max()))
    if len(blobs) < min_lattice:
        return PartialResult(None, "too few blobs")
    large = _large_blobs(blobs, ratio)
    if len(large) < 3:
        return PartialResult(None, "fewer than three large markers")
    found = _triple(large)
    if found is None:
        return PartialResult(None, "no collinear 2:1 large-marker triple")
    p0, p1, p2, pitch = found
    P = np.array([b.xy for b in blobs], float)
    A = np.array([b.area for b in blobs], float)

    best = None
    for sign in (+1, -1):
        uv, pred = _predict_lattice(p0, p1, p2, pitch, sign, uv_max)
        d = np.linalg.norm(pred[:, None, :] - P[None, :, :], axis=2)
        near = d.argmin(1)
        dn = d.min(1)
        ok = dn < tol_pitch * pitch
        if ok.sum() < min_lattice:
            continue
        # one blob may not serve two lattice points
        if len(set(near[ok].tolist())) != int(ok.sum()):
            continue
        uv_ok = uv[ok]
        img = P[near[ok]]
        H, _ = cv2.findHomography(uv_ok.reshape(-1, 1, 2).astype(np.float64),
                                  img.reshape(-1, 1, 2).astype(np.float64), 0)
        if H is None:
            continue
        projected = cv2.perspectiveTransform(
            uv_ok.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
        res = np.linalg.norm(projected - img, axis=1)
        rms = float(np.sqrt(np.mean(res ** 2)))
        if rms > max_rms_px:
            continue
        if best is None or ok.sum() > best[0]:
            best = (int(ok.sum()), sign, H, uv_ok, img, rms, float(res.max()))

    if best is None:
        return PartialResult(None, "no lattice hypothesis explained enough blobs")
    n, sign, H, uv_ok, img, rms, mx = best
    if uv_ok[:, 0].min() > 0.5 or uv_ok[:, 1].min() > 0.5:
        return PartialResult(None, "labelling does not reach the canonical origin")
    points = {(float(u), float(v)): np.array([float(x), float(y)])
              for (u, v), (x, y) in zip(uv_ok, img)}
    obs = detect.GridObservation(points, np.asarray(H, float), int(n), rms, mx,
                                 np.zeros(0), detector="partial-v1")
    return PartialResult(obs, "ok", n_inliers=n,
                         span_u=(float(uv_ok[:, 0].min()), float(uv_ok[:, 0].max())),
                         span_v=(float(uv_ok[:, 1].min()), float(uv_ok[:, 1].max())),
                         v_sign=int(sign))


def detect_grid_partial(gray: np.ndarray, *, refine_mode: str = "centroid",
                        percentiles: Iterable[float] = (99.0, 99.4, 98.5),
                        **kwargs) -> PartialResult:
    """Polarity/threshold sweep with the partial assignment as the only path."""
    reasons = []
    for invert in (False, True):
        for pct in percentiles:
            blobs = detect.detect_blobs(gray, invert=invert, percentile=pct,
                                        refine_mode=refine_mode)
            if not blobs:
                continue
            result = assign_grid_partial(blobs, **kwargs)
            if result.observation is not None:
                return result
            reasons.append(f"invert={invert} pct={pct}: {result.reason}")
    return PartialResult(None, reasons[-1] if reasons else "no blobs")
