"""Detection that can run outside the GUI/controller process.

The live path needs acquisition and preview to keep the device cadence while a
frame is being analysed.  A Python thread does not provide that isolation: the
detector's per-blob loops hold the GIL, and the measured acquisition loop fell
from 10 Hz to ~6.4 Hz when detection ran alongside it.  These functions are
module-level and picklable so a ``ProcessPoolExecutor`` can own the CPU work;
the controller keeps the small result and the pose/acceptance logic.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import numpy as np

from calib import detect as _detect


def prefer_partial(gray: np.ndarray, observation: Any) -> Any:
    """Use the partial assignment when the strict one is wrong or missing.

    Identical to the original controller helper; it lives here so the process
    worker and the thread path share one implementation.
    """
    try:
        from calib import detect_partial
        need_partial = observation is None
        if not need_partial:
            us = {round(float(k[0])) for k in observation.points}
            vs = {round(float(k[1])) for k in observation.points}
            need_partial = (min(us) > 0 or min(vs) > 0
                            or max(us) < 7 or max(vs) < 4)
        if not need_partial:
            return observation
        result = detect_partial.detect_grid_partial(gray)
        cand = result.observation
        if cand is None:
            return observation
        if observation is None:
            return cand
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
                                            box=_box(observation))
        observation = _detect.detect_grid_auto(
            gray, early_lattice=early_lattice, early_rms=early_rms)
        observation = prefer_partial(gray, observation)
        return DetectionOutcome(observation, used_roi=False,
                                fallback=roi is not None,
                                box=_box(observation))
    except Exception:
        if os.environ.get("EINSTAR_DETECT_DEBUG") == "1":
            raise
        return DetectionOutcome(None, fallback=roi is not None)


def _box(observation: Any):
    if observation is None or not getattr(observation, "points", None):
        return None
    pts = np.asarray(list(observation.points.values()), float)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()),
            float(pts[:, 0].max()), float(pts[:, 1].max()))
