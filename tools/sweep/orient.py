"""Orientation witness for the plate UV assignment.

``calib.detect.detect_grid_auto`` returns the 40 lattice markers with canonical
``(u, v)`` labels, but it returns ``big_markers = array([])``: the four large
orientation markers are *used* as the homography anchors and then dropped, so
nothing in the returned observation proves that the labels are the intended
ones.

That matters because the 40-point lattice is symmetric: the Klein four-group

    identity, rot180 (u,v)->(U-u, V-v),
    mirror_u (u,v)->(U-u, v), mirror_v (u,v)->(u, V-v)

maps the lattice onto itself, so **every one of those four label assignments
reproduces the same 40 image points with exactly the same residual**.  The
lattice residual therefore cannot detect a flipped assignment; only the
large-marker pattern can.  ``mirror_u``/``mirror_v`` correspond to viewing the
plate from behind, ``rot180`` to holding it upside down.

This module re-detects the blobs (areas included) and scores the four label
hypotheses by asking, for each one, whether the plate's four *large* markers
land on the positions that hypothesis predicts:

* three on-lattice large markers at (2,1), (4,1), (5,1), area ~4x the small
  markers;
* one **off-lattice** chiral marker at (3.5, 3) that is not part of the 40-point
  lattice at all.

The identity hypothesis scores 4/4 when all four witnesses are visible;
``mirror_u`` can reach 3/4 (it maps (3.5,3) to itself and swaps (2,1)<->(5,1));
``rot180`` and ``mirror_v`` reach 0/4.  A frame whose best hypothesis is not
the identity, or whose identity score ties with an alternative, is reported as
orientation-uncertain and rejected by the caller.

``det(H) > 0`` is a second, independent witness: a front-facing plate viewed
from the marker side must map the (u right, v down) board frame to the (x
right, y down) pixel frame with positive determinant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from calib import detect, plate


@dataclass
class OrientationCheck:
    """Result of the large-marker / chirality witness for one grid observation."""
    ok: bool                       # identity labels verified
    reason: str
    score: int                     # identity score, 0..4
    scores: dict[str, int]         # hypothesis -> score
    min_area_ratio: float          # smallest large/small area ratio at a witness
    det_h: float                   # sign of the lattice homography determinant
    n_blobs: int
    small_area_px: float
    pitch_px: float
    witness_offsets_px: list = field(default_factory=list)
    #: how many of the four witnesses are predicted to be inside the image
    n_visible: int = 0
    visible_mask: list = field(default_factory=list)
    expected_visible: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "reason": self.reason,
            "score": int(self.score),
            "scores": {k: int(v) for k, v in self.scores.items()},
            "margin": int(self.score - max([v for k, v in self.scores.items()
                                            if k != "identity"] or [0])),
            "min_area_ratio": _r(self.min_area_ratio),
            "det_h": _r(self.det_h, 3),
            "n_blobs": int(self.n_blobs),
            "small_area_px": _r(self.small_area_px, 2),
            "pitch_px": _r(self.pitch_px, 3),
            "witness_offsets_px": [_r(v, 3) for v in self.witness_offsets_px],
            "n_visible": int(self.n_visible),
            "visible_mask": [bool(x) for x in self.visible_mask],
        }


def _r(v, nd=4):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, nd)


#: label symmetries of the lattice -> the four indistinguishable assignments
def _symmetries(umax: float, vmax: float) -> dict:
    return {
        "identity": lambda u, v: (u, v),
        "rot180": lambda u, v: (umax - u, vmax - v),
        "mirror_u": lambda u, v: (umax - u, v),
        "mirror_v": lambda u, v: (u, vmax - v),
    }


def _median_pitch(points: np.ndarray) -> float:
    if len(points) < 4:
        return 0.0
    d = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    np.fill_diagonal(d, 1e9)
    return float(np.median(d.min(1)))


def _pick_blobs(gray: np.ndarray, obs) -> tuple[list, int]:
    """Re-detect the blobs of the polarity that produced ``obs``.

    ``detect_grid_auto`` tries both polarities internally and does not say which
    one won, so re-detect both and keep the set that actually contains the
    observation's own image points.
    """
    best = (None, -1)
    for invert in (False, True):
        blobs = detect.detect_blobs(gray, invert=invert, percentile=99.0)
        if not blobs:
            continue
        P = np.array([b.xy for b in blobs], float)
        d = np.linalg.norm(P[:, None, :] - obs.pixels[None, :, :], axis=2)
        near = int(np.sum(d.min(0) < 1.5))
        if near > best[1]:
            best = (blobs, near)
    return best[0] or [], best[1]


def _hypothesis_hit(name: str, uv_item, syms, obs, P, A, small_area, pitch,
                    large_area_ratio: float) -> bool:
    """Does hypothesis ``name`` place a large marker at its predicted pixel?"""
    fn = syms[name]
    u, v = fn(float(uv_item[0]), float(uv_item[1]))
    pix = cv2.perspectiveTransform(np.array([[[u, v]]], np.float32),
                                   obs.homography).reshape(2)
    d = np.linalg.norm(P - pix, axis=1)
    j = int(np.argmin(d))
    if d[j] >= 0.30 * pitch or not np.isfinite(small_area) or small_area <= 0:
        return False
    return bool(A[j] / small_area > large_area_ratio)


def check_orientation(gray: np.ndarray, obs, big_uv: np.ndarray | None = None,
                      large_area_ratio: float = 1.6,
                      image_shape: tuple[int, int] | None = None,
                      margin_px: float = 10.0,
                      min_visible_witnesses: int = 3) -> OrientationCheck:
    """Score the four lattice label assignments against the large markers.

    ``image_shape=(height, width)`` turns on the **visibility-aware** rule used
    by the calibration wizard: a witness whose predicted pixel position lies
    outside the image cannot be observed at all, so it must not count against a
    partially visible plate.  With an image shape the decision is

    * strict: identity must hit **every** witness that is predicted inside the
      image, and beat every alternative hypothesis on the same subset, and
      ``det(H) > 0``;
    * a frame with fewer than ``min_visible_witnesses`` predicted-visible
      witnesses is rejected as ``insufficient_witness`` (two witnesses cannot
      separate identity from ``mirror_u``);
    * a witness that is predicted *inside* the image but missing, small or
      displaced still fails the frame - only genuine out-of-frame loss is
      excused.

    Without ``image_shape`` the historical strict 4/4 rule is kept, so the sweep
    analysis and every existing caller are unchanged.
    """
    if obs is None or len(obs.points) < 8:
        return OrientationCheck(False, "no observation", 0, {}, float("nan"),
                                float("nan"), 0, float("nan"), float("nan"))
    uv_all = plate.lattice_uv()
    umax, vmax = float(uv_all[:, 0].max()), float(uv_all[:, 1].max())
    big_uv = plate.big_points_uv() if big_uv is None else np.asarray(big_uv, float)
    syms = _symmetries(umax, vmax)

    blobs, _ = _pick_blobs(gray, obs)
    if len(blobs) < 8:
        return OrientationCheck(False, "no blobs", 0, {}, float("nan"),
                                float(np.linalg.det(obs.homography)), len(blobs),
                                float("nan"), float("nan"))
    P = np.array([b.xy for b in blobs], float)
    A = np.array([b.area for b in blobs], float)
    pitch = _median_pitch(obs.pixels)
    # median small-marker area: blobs matched to the lattice, robust median
    d = np.linalg.norm(P[:, None, :] - obs.pixels[None, :, :], axis=2)
    matched = d.min(0) < 0.25 * pitch
    small_area = float(np.median(A[d.argmin(0)[matched]])) if matched.any() else float("nan")

    det_h = float(np.linalg.det(np.asarray(obs.homography, float)))
    scores: dict[str, int] = {}
    detail: dict[str, list] = {}
    for name, fn in syms.items():
        uv = np.array([fn(float(u), float(v)) for u, v in big_uv], float)
        pix = cv2.perspectiveTransform(uv.reshape(-1, 1, 2).astype(np.float32),
                                      obs.homography).reshape(-1, 2)
        dd = np.linalg.norm(pix[:, None, :] - P[None, :, :], axis=2)
        near = dd.argmin(1)
        dn = dd.min(1)
        ratio = A[near] / max(small_area, 1e-9) if np.isfinite(small_area) else np.zeros(4)
        hits = (ratio > large_area_ratio) & (dn < 0.30 * pitch)
        scores[name] = int(hits.sum())
        detail[name] = [float(x) for x in dn]

    # physical positions of the four witnesses under the identity labels; the
    # same physical markers are used for every hypothesis, only their *labels*
    # differ, so visibility is judged once, on these positions.
    id_pix = cv2.perspectiveTransform(big_uv.reshape(-1, 1, 2).astype(np.float32),
                                      obs.homography).reshape(-1, 2)
    id_dn = np.linalg.norm(id_pix[:, None, :] - P[None, :, :], axis=2)
    id_near = id_dn.argmin(1)
    id_d = id_dn.min(1)
    hit_all = (A[id_near] / max(small_area, 1e-9) > large_area_ratio) & (id_d < 0.30 * pitch)

    if image_shape is None:
        visible = np.ones(len(big_uv), bool)
    else:
        height, width = int(image_shape[0]), int(image_shape[1])
        visible = ((id_pix[:, 0] >= margin_px) & (id_pix[:, 0] <= width - 1 - margin_px) &
                   (id_pix[:, 1] >= margin_px) & (id_pix[:, 1] <= height - 1 - margin_px))
    n_visible = int(visible.sum())

    # score every hypothesis on the *visible* subset only
    scored = {name: int(sum(1 for i, uvv in enumerate(big_uv)
                            if visible[i] and _hypothesis_hit(name, uvv, syms, obs, P, A,
                                                              small_area, pitch,
                                                              large_area_ratio)))
              for name in syms}
    id_score = scored["identity"]
    alt_best = max((v for k, v in scored.items() if k != "identity"), default=0)
    min_ratio = float(np.min(A[id_near][hit_all & visible] / max(small_area, 1e-9))) \
        if np.any(hit_all & visible) else 0.0

    if det_h <= 0:
        reason, ok = "mirrored_geometry", False
    elif n_visible < max(1, int(min_visible_witnesses)):
        reason, ok = "insufficient_witness", False
    elif id_score < n_visible:
        reason, ok = ("ambiguous" if id_score == alt_best else "partial_witness"), False
    elif id_score <= alt_best:
        reason, ok = ("ambiguous" if id_score == alt_best else "flipped"), False
    elif n_visible < len(big_uv):
        reason, ok = "partial_verified", True
    else:
        reason, ok = "verified", True
    result = OrientationCheck(ok, reason, id_score, scored, min_ratio, det_h,
                              len(blobs), small_area, pitch,
                              [float(x) for x in id_d])
    result.n_visible = n_visible
    result.visible_mask = [bool(v) for v in visible]
    result.expected_visible = n_visible
    return result
