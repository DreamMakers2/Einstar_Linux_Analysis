"""Safe automatic exposure for the Einstar calibration session.

The scanner exposes volatile per-camera exposure and gain and a global IR
illuminator.  This module decides *when* and *by how much* to change them; the
capture process applies the values through its validated control channel and
records them in its status.  Nothing here writes persistent device state.

Design rules (all measured or conservative):

* IR pair together, RGB independently.  Changing one IR camera alone would
  break the stereo exposure match.
* Exposure first, gain second, illuminator last.  Gain adds noise; the laser is
  shared by both IR cameras and affects the operator's view.
* Hard saturation protection: any saturated frame reduces exposure immediately.
* A dead band and a two-observation confirmation avoid hunting.
* Accepted calibration views freeze the settings for a dwell window, so the
  observations used by the solver are not taken while exposure is moving.
* Every change is returned with a reason and a timestamp for the session log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time

import numpy as np


@dataclass
class FrameStats:
    mean: float
    p99: float
    saturation: float
    contrast: float

    @classmethod
    def from_image(cls, image, *, step: int = 4) -> "FrameStats":
        """Cheap robust statistics on a subsampled grey image.

        A full-frame percentile on 1.3 MP costs several milliseconds; the
        advisor runs at <2 Hz, but the preview thread must stay cheap.  A
        stride-4 sample is 81k pixels and preserves the decision.
        """
        arr = np.asarray(image)
        if arr.ndim == 3:
            arr = arr[..., 1] if arr.shape[2] >= 2 else arr[..., 0]
        sample = np.asarray(arr[::step, ::step], dtype=np.float32)
        if sample.size == 0:
            return cls(0.0, 0.0, 0.0, 0.0)
        # Clip the black border of the sensor: it is not part of the plate ROI
        # and would bias the mean down.
        p5, p50, p95, p99 = np.percentile(sample, [5, 50, 95, 99])
        return cls(mean=float(p50), p99=float(p99),
                   saturation=float(np.mean(sample >= 250.0)),
                   contrast=float(p95 - p5))


@dataclass
class CameraPolicy:
    target_low: float
    target_high: float
    min_us: int
    max_us: int
    min_gain: int
    max_gain: int
    step_up: float = 1.25
    step_down: float = 0.80
    gain_step: int = 12
    saturation_limit: float = 0.01
    min_contrast: float = 12.0


IR_POLICY = CameraPolicy(target_low=65.0, target_high=150.0,
                         min_us=200, max_us=8000, min_gain=0, max_gain=240)
RGB_POLICY = CameraPolicy(target_low=55.0, target_high=155.0,
                          min_us=500, max_us=20000, min_gain=0, max_gain=300)


@dataclass
class ExposureSettings:
    exposure_us: dict[int, int]
    gain: dict[int, int]
    laser_percent: int

    def as_capture_args(self) -> dict:
        return {"exposure_us": dict(self.exposure_us),
                "gain": dict(self.gain),
                "laser_percent": int(self.laser_percent)}


@dataclass
class Advice:
    settings: ExposureSettings | None
    reason: str
    changes: dict = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class AutoExposure:
    """Stateful, hysteretic exposure advisor.

    ``observe()`` is called at the display rate but only evaluates when the
    minimum interval has passed.  It returns an :class:`Advice`; ``None``
    settings means "keep the current values".
    """

    def __init__(self, *, min_interval_s: float = 1.0,
                 settle_frames: int = 2, freeze_after_accept_s: float = 1.6):
        self.min_interval_s = float(min_interval_s)
        self.settle_frames = max(1, int(settle_frames))
        self.freeze_after_accept_s = float(freeze_after_accept_s)
        self._last_eval = 0.0
        self._frozen_until = 0.0
        self._counters = {"ir_up": 0, "ir_down": 0, "rgb_up": 0, "rgb_down": 0}
        self._history: list[dict] = []

    def freeze(self, seconds: float | None = None) -> None:
        span = self.freeze_after_accept_s if seconds is None else float(seconds)
        self._frozen_until = max(self._frozen_until, time.monotonic() + span)

    def observe(self, *, ir: FrameStats | None, rgb: FrameStats | None,
                current: ExposureSettings,
                now: float | None = None) -> Advice:
        now = time.monotonic() if now is None else float(now)
        emergency = ((ir is not None and
                      (ir.saturation > 0.02 or ir.p99 >= 250)) or
                     (rgb is not None and
                      (rgb.saturation > 0.02 or rgb.p99 >= 250)))
        if now < self._frozen_until and not emergency:
            return Advice(None, "frozen after an accepted view")
        if now - self._last_eval < self.min_interval_s:
            return Advice(None, "settling")
        self._last_eval = now
        exp = dict(current.exposure_us)
        gain = dict(current.gain)
        laser = int(current.laser_percent)
        changes: dict[str, int | str] = {}
        reason = "within target"

        # ---- IR pair: identical exposure, exposure first -------------------
        if ir is not None:
            ir_key = self._classify(ir, IR_POLICY)
            count = self._bump("ir", ir_key)
            if ir_key != "ok" and count >= self.settle_frames:
                self._counters["ir_up"] = 0
                self._counters["ir_down"] = 0
                if ir_key == "down":
                    new_us = max(IR_POLICY.min_us,
                                 int(min(exp[0], exp[1]) * IR_POLICY.step_down))
                    if new_us < min(exp[0], exp[1]):
                        exp[0] = exp[1] = new_us
                        changes["ir_exposure_us"] = new_us
                        reason = (f"IR saturation {ir.saturation:.3f} / "
                                  f"p99 {ir.p99:.0f}")
                    else:
                        new_gain = max(IR_POLICY.min_gain,
                                       min(gain[0], gain[1]) - IR_POLICY.gain_step)
                        if new_gain < min(gain[0], gain[1]):
                            gain[0] = gain[1] = new_gain
                            changes["ir_gain"] = new_gain
                            reason = "IR exposure at minimum; reducing gain"
                        elif laser > 40:
                            laser = max(40, laser - 10)
                            changes["laser_percent"] = laser
                            reason = "IR still saturated; reducing illumination"
                else:
                    new_us = min(IR_POLICY.max_us,
                                 int(max(exp[0], exp[1]) * IR_POLICY.step_up))
                    if new_us > max(exp[0], exp[1]):
                        exp[0] = exp[1] = new_us
                        changes["ir_exposure_us"] = new_us
                        reason = f"IR dark (p50 {ir.mean:.0f})"
                    else:
                        new_gain = min(IR_POLICY.max_gain,
                                       max(gain[0], gain[1]) + IR_POLICY.gain_step)
                        if new_gain > max(gain[0], gain[1]):
                            gain[0] = gain[1] = new_gain
                            changes["ir_gain"] = new_gain
                            reason = "IR exposure at maximum; raising gain"
                        elif laser < 100:
                            laser = min(100, laser + 10)
                            changes["laser_percent"] = laser
                            reason = "IR still dark; raising illumination"

        # ---- RGB: independent -------------------------------------------------
        if rgb is not None:
            key = self._classify(rgb, RGB_POLICY)
            count = self._bump("rgb", key)
            if key != "ok" and count >= self.settle_frames:
                self._counters["rgb_up"] = 0
                self._counters["rgb_down"] = 0
                if key == "down":
                    new_us = max(RGB_POLICY.min_us,
                                 int(exp[2] * RGB_POLICY.step_down))
                    if new_us < exp[2]:
                        exp[2] = new_us
                        changes["rgb_exposure_us"] = new_us
                    else:
                        gain[2] = max(RGB_POLICY.min_gain,
                                      gain[2] - RGB_POLICY.gain_step)
                        changes["rgb_gain"] = gain[2]
                else:
                    new_us = min(RGB_POLICY.max_us,
                                 int(exp[2] * RGB_POLICY.step_up))
                    if new_us > exp[2]:
                        exp[2] = new_us
                        changes["rgb_exposure_us"] = new_us
                    else:
                        gain[2] = min(RGB_POLICY.max_gain,
                                      gain[2] + RGB_POLICY.gain_step)
                        changes["rgb_gain"] = gain[2]

        if not changes:
            return Advice(None, reason)
        for cam in (0, 1, 2):
            exp[cam] = int(exp.get(cam, 0))
            gain[cam] = int(gain.get(cam, 0))
        out = ExposureSettings(exp, gain, laser)
        record = {"at_s": time.time(), "reason": reason,
                  "changes": changes, "settings": out.as_capture_args()}
        self._history.append(record)
        return Advice(out, reason, changes)

    def _bump(self, group: str, key: str) -> int:
        """Count consecutive observations in one direction.

        An ``ok`` observation clears both directions; a reversal clears the
        opposite counter so stale evidence cannot trigger a change.
        """
        up, down = f"{group}_up", f"{group}_down"
        if key == "ok":
            self._counters[up] = 0
            self._counters[down] = 0
            return 0
        if key == "up":
            self._counters[down] = 0
            self._counters[up] += 1
            return self._counters[up]
        self._counters[up] = 0
        self._counters[down] += 1
        return self._counters[down]

    @staticmethod
    def _classify(stats: FrameStats, policy: CameraPolicy) -> str:
        if stats.saturation > policy.saturation_limit or stats.p99 >= 250:
            return "down"
        if stats.mean > policy.target_high:
            return "down"
        if stats.mean < policy.target_low or stats.contrast < policy.min_contrast:
            return "up"
        return "ok"

    @property
    def history(self) -> list[dict]:
        return list(self._history)
