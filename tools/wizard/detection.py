"""Detection that can run outside the GUI/controller process.

The live path needs acquisition and preview to keep the device cadence while a
frame is being analysed.  A Python thread does not provide that isolation: the
detector's per-blob loops hold the GIL, and the measured acquisition loop fell
from 10 Hz to ~6.4 Hz when detection ran alongside it.  These functions are
module-level and picklable so a ``ProcessPoolExecutor`` can own the CPU work;
the controller keeps the small result and the pose/acceptance logic.

Identity is the contract here
-----------------------------
Both detectors now report *verification*, not just a labelling:
:attr:`calib.detect.GridObservation.identity_ok` is true only when the four
large markers (three on-lattice, one off-lattice chiral) are found where the
labelling predicts them and ``det(H) > 0``.  The helpers below therefore

* never *accept* a mirrored observation (``det(H) <= 0``) and never prefer one
  over a verified candidate -- a mirror is not physically realisable for the
  printed side of the plate.  A mirrored strict result is still returned when
  nothing verifies, so the witness can report ``mirrored_geometry`` to the
  operator instead of a vague "no markers";
* prefer the *verified* candidate over the one with more markers, because the
  40-point lattice is symmetric under the Klein four-group and a relabelled fit
  reproduces the same pixels with the same residual (measured on real frames:
  the mirrored/rotated labelling won on rms by 0.004 px before this rule);
* only fall back to the partial assignment when it is verified, or when the
  strict result is absent/unverified and the partial one has at least as much
  support.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import numpy as np

from calib import detect as _detect


def _det(observation: Any) -> float:
    try:
        return float(np.linalg.det(np.asarray(observation.homography, float)))
    except Exception:
        return float("nan")


def _verified(observation: Any) -> bool:
    """Identity verified by the detector's large-marker witness."""
    if observation is None:
        return False
    flag = getattr(observation, "identity_ok", None)
    if flag is None:                      # observation from an older producer
        return True
    return bool(flag)


def _mirrored(observation: Any) -> bool:
    det = _det(observation)
    return bool(np.isfinite(det) and det <= 0.0)


def _span_complete(observation: Any) -> bool:
    us = {round(float(k[0])) for k in observation.points}
    vs = {round(float(k[1])) for k in observation.points}
    return bool(us) and bool(vs) and min(us) == 0 and min(vs) == 0 \
        and max(us) == 7 and max(vs) == 4


#: a verified observation with at least this many markers is already a usable
#: view, so the (6-pass) partial sweep is not run again: 12 is the wizard's
#: ``min_shared_markers`` and a verified 10-12 marker PnP is within 0.1 deg /
#: 0.12 mm of the 40-marker pose on the recorded corpus.
MIN_VERIFIED_MARKERS = 12


def _needs_partial(observation: Any) -> bool:
    """Is another (partial) assignment attempt worth its 6-pass cost?"""
    if observation is None or not observation.points:
        return True
    if _mirrored(observation) or not _verified(observation):
        return True
    if int(observation.n_lattice) >= MIN_VERIFIED_MARKERS:
        # identity verified and enough markers to be used: the partial sweep can
        # only add markers to a view that is already accepted, and it costs the
        # full polarity/threshold sweep (measured: 34 detect_grid calls per
        # ingest on clipped frames, p50 655 ms per frame).
        return False
    return not _span_complete(observation) or True


def prefer_partial(gray: np.ndarray, observation: Any) -> Any:
    """Use the partial assignment when the strict one is wrong or missing.

    The strict detector anchors on all four orientation markers, so a plate whose
    chiral marker has left the frame either yields ``None`` or a self-consistent
    *sub-lattice* whose labels are shifted.  The partial assignment anchors on
    the large markers alone and recovers the canonical span, so it is preferred
    whenever it is verified and the strict result is not (or explains less).

    A mirrored observation is never returned; that is a safety rule, not a
    quality preference.
    """
    mirrored = observation is not None and _mirrored(observation)
    try:
        if not _needs_partial(observation):
            return observation
        from calib import detect_partial
        result = detect_partial.detect_grid_partial(
            gray, early_lattice=MIN_VERIFIED_MARKERS)
        cand = result.observation
        if cand is not None and _mirrored(cand):
            cand = None
        if cand is None:
            return observation
        if observation is None:
            return cand
        if mirrored:
            # a mirrored strict labelling is never *accepted* (the witness
            # rejects det(H) <= 0), but it is kept as the diagnostic unless a
            # verified partial labelling replaces it
            return cand if _verified(cand) else observation
        strict_ok = _verified(observation)
        cand_ok = _verified(cand) or _det(cand) > 0.0 and result.witness_score > 0
        if cand_ok and not strict_ok:
            return cand
        if strict_ok and not cand_ok:
            return observation
        if int(cand.n_lattice) > int(observation.n_lattice):
            return cand
        return observation
    except Exception:
        return observation


def detect_task(task: tuple) -> "DetectionOutcome":
    """Executor.map entry point: unpack one (gray, roi, early_lattice) task."""
    return detect_one(*task)


@dataclass
class DetectionOutcome:
    observation: Any
    used_roi: bool = False
    fallback: bool = False
    box: tuple[float, float, float, float] | None = None
    #: identity verified by the large-marker/chirality witness
    identity_ok: bool = False
    #: short diagnostic ("strict", "partial", "roi", "none", error text)
    reason: str = ""


def detect_one(gray: np.ndarray, roi: tuple[int, int, int, int] | None = None,
               early_lattice: int = 40, early_rms: float = 0.5) -> DetectionOutcome:
    """Full detection for one camera: ROI, early exit, partial fallback, box."""
    try:
        gray = np.ascontiguousarray(np.asarray(gray, dtype=np.uint8))
        observation = None
        if roi is not None:
            observation = _detect.detect_grid_auto(
                gray, roi=roi, early_lattice=early_lattice, early_rms=early_rms)
            if observation is not None:
                pts = np.asarray(list(observation.points.values()), float)
                edge = 8.0
                inside = ((pts[:, 0] > roi[0] + edge)
                          & (pts[:, 0] < roi[2] - edge)
                          & (pts[:, 1] > roi[1] + edge)
                          & (pts[:, 1] < roi[3] - edge))
                if not bool(inside.all()):
                    observation = None          # may be clipped: redo full
                elif len(observation.points) < int(early_lattice):
                    # A crop can clip the lattice while every *detected* point
                    # is still inside the crop (observed on a sparse replay:
                    # a stale ROI kept 15 of the 36 corner markers and the
                    # view was then rejected for insufficient stereo overlap).
                    # Re-run on the full frame; it can only find more markers.
                    observation = None
                else:
                    observation = prefer_partial(gray, observation)
                    return DetectionOutcome(observation, used_roi=True,
                                            box=_box(observation),
                                            identity_ok=_verified(observation),
                                            reason=_label(observation))
        observation = _detect.detect_grid_auto(
            gray, early_lattice=early_lattice, early_rms=early_rms)
        observation = prefer_partial(gray, observation)
        return DetectionOutcome(observation, used_roi=False,
                                fallback=roi is not None,
                                box=_box(observation),
                                identity_ok=_verified(observation),
                                reason=_label(observation) if observation is not None
                                else "none")
    except Exception:
        if os.environ.get("EINSTAR_DETECT_DEBUG") == "1":
            raise
        return DetectionOutcome(None, fallback=roi is not None, reason="error")


def _label(observation: Any) -> str:
    if observation is None:
        return "none"
    return "strict" if str(getattr(observation, "detector", "")).startswith("pattern") \
        else str(getattr(observation, "detector", "detected"))


def _box(observation: Any):
    if observation is None or not getattr(observation, "points", None):
        return None
    pts = np.asarray(list(observation.points.values()), float)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()),
            float(pts[:, 0].max()), float(pts[:, 1].max()))
