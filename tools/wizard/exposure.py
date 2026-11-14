"""Safe automatic exposure for the Einstar calibration session.

The scanner exposes volatile per-camera exposure and gain and a global IR
illuminator.  This module decides *when* and *by how much* to change them; the
capture process applies the values through its validated control channel and
reports them in its status.  Nothing here writes persistent device state.

Design rules (all measured or conservative):

* IR pair together, RGB independently.  Changing one IR camera alone would
  break the stereo exposure match, so the advisor always writes one value for
  cameras 0 and 1 and uses the darker of the pair as the step base.
* Exposure first, gain second, illuminator last.  Gain adds noise; the laser is
  shared by both IR cameras and affects the operator's view.
* Meter the plate, never the background.  Brightness comes from the padded
  bounding box of the latest detected markers.  The retro-reflective markers
  are *intentionally* clipped at 255 DN, so their pixels are excluded from the
  statistics; otherwise every frame looks saturated and the controller
  oscillates between the dark-background rule and the clipping rule.
* Hard clipping reacts immediately (one observation, fast step, bypasses the
  accepted-view freeze).  Soft corrections need two consecutive observations
  and use smaller steps.
* A dead band plus the two-observation confirmation avoid hunting while still
  recovering at far distance; a change that would not alter a value is never
  emitted (no no-op writes).
* At a device limit the advisor reports the limit once and holds instead of
  re-requesting the same value forever.
* Accepted calibration views and stationary holds freeze soft corrections, so
  the observations used by the solver are not taken while exposure is moving.
* Every requested change is tracked against the next device readback; a
  readback that never matches is reported (the capture child can reject a
  write) and exposed through :meth:`AutoExposure.status`.
* Every applied change is returned with a reason and a timestamp for the
  session log.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time

import numpy as np

# Marker cores are saturated by design.  A disk of this radius (full-resolution
# pixels) is excluded from the metered ROI before computing the clipping
# statistics.  The observed marker diameter is ~20-30 px at 1280x1024.
MARKER_MASK_RADIUS_PX = 24


@dataclass
class FrameStats:
    """Robust statistics of the metered region of one camera."""

    mean: float          # median (p50) brightness, DN
    p99: float
    saturation: float    # fraction of metered pixels >= 250 DN
    contrast: float      # p95 - p5 inside the metered region
    p95: float = 0.0
    metering: str = "roi"     # "roi" | "stale" | "frame"

    @classmethod
    def from_image(cls, image, *, bbox=None, exclude=None, step: int = 4,
                   marker_radius: int = MARKER_MASK_RADIUS_PX) -> "FrameStats":
        """Cheap robust statistics on a subsampled grey image.

        ``bbox`` is an optional ``(x0, y0, x1, y1)`` region.  Auto exposure
        measures the *plate* through this box: the full frame's median is
        dominated by the dark background and is not a useful exposure target.

        ``exclude`` is an optional iterable of ``(x, y)`` full-resolution marker
        centres.  A square of half-width ``marker_radius`` around each centre is
        ignored so the deliberately clipped marker cores do not masquerade as
        plate clipping.  ``metering`` is ``"roi"`` when a bbox was supplied and
        ``"frame"`` otherwise; the caller may replace it with ``"stale"``.
        """
        arr = np.asarray(image)
        if arr.ndim == 3:
            arr = arr[..., 1] if arr.shape[2] >= 2 else arr[..., 0]
        if arr.size == 0:
            return cls(0.0, 0.0, 0.0, 0.0,
                       metering="roi" if bbox is not None else "frame")
        h, w = arr.shape[:2]
        x0, y0, x1, y1 = 0, 0, w, h
        if bbox is not None:
            bx0, by0, bx1, by1 = (int(v) for v in bbox)
            x0 = max(0, min(w - 2, bx0))
            y0 = max(0, min(h - 2, by0))
            x1 = max(x0 + 2, min(w, bx1))
            y1 = max(y0 + 2, min(h, by1))
        crop = arr[y0:y1, x0:x1]
        mask = None
        if exclude is not None:
            mask = np.ones(crop.shape, bool)
            radius = max(1, int(marker_radius))
            for point in exclude:
                try:
                    px, py = float(point[0]), float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
                cx = int(round(px)) - x0
                cy = int(round(py)) - y0
                if (cx < -radius or cy < -radius
                        or cx > crop.shape[1] + radius
                        or cy > crop.shape[0] + radius):
                    continue
                yy0 = max(0, cy - radius)
                yy1 = min(crop.shape[0], cy + radius + 1)
                xx0 = max(0, cx - radius)
                xx1 = min(crop.shape[1], cx + radius + 1)
                if yy1 > yy0 and xx1 > xx0:
                    mask[yy0:yy1, xx0:xx1] = False
        sample = crop[::step, ::step]
        mask_sample = mask[::step, ::step] if mask is not None else None
        if mask_sample is not None and mask_sample.any():
            values = sample[mask_sample]
        else:
            # An over-wide mask must not blank the measurement: fall back to
            # the unmasked sample rather than reporting a black frame.
            values = sample.reshape(-1)
        values = np.asarray(values, dtype=np.float32)
        if values.size == 0:
            return cls(0.0, 0.0, 0.0, 0.0,
                       metering="roi" if bbox is not None else "frame")
        p5, p50, p95, p99 = np.percentile(values, [5, 50, 95, 99])
        return cls(mean=float(p50), p99=float(p99),
                   saturation=float(np.mean(values >= 250.0)),
                   contrast=float(p95 - p5), p95=float(p95),
                   metering="roi" if bbox is not None else "frame")

    def as_dict(self) -> dict:
        return asdict(self)


def plate_bbox(points, *, pad: float = 0.15, min_points: int = 4):
    """Padded bounding box of the detected plate markers, or ``None``.

    ``points`` is an iterable of ``(x, y)`` image coordinates in the same frame
    that will be metered.
    """
    if points is None:
        return None
    try:
        arr = np.asarray(points, float).reshape(-1, 2)
    except (TypeError, ValueError):
        return None
    if len(arr) < max(1, int(min_points)):
        return None
    x0, y0 = arr.min(axis=0)
    x1, y1 = arr.max(axis=0)
    pad_px = float(pad) * max(float(x1 - x0), float(y1 - y0), 20.0)
    return (float(x0 - pad_px), float(y0 - pad_px),
            float(x1 + pad_px), float(y1 + pad_px))


class RoiHold:
    """Last valid plate box with a bounded staleness window.

    Detection is lost for most frames in a real session (ambiguous markers,
    motion).  Falling back to full-frame statistics is what pinned the RGB
    camera at maximum exposure and made the IR loop hunt the dark background,
    so a recently valid plate box is reused for ``hold_s`` seconds.  After that
    the metering is explicitly ``"none"`` and the advisor holds instead of
    chasing a background median.
    """

    def __init__(self, *, hold_s: float = 2.0, pad: float = 0.15,
                 min_points: int = 4, fresh_s: float = 0.5):
        self.hold_s = float(hold_s)
        #: a detection older than this is reused as a *stale* box: clipping may
        #: still step down but exposure is never raised from it
        self.fresh_s = float(fresh_s)
        self.pad = float(pad)
        self.min_points = int(min_points)
        self._box = None
        self._points: list[tuple[float, float]] = []
        self._at = 0.0

    def box(self, points, now: float, *, observed_at: float | None = None):
        """Return ``(bbox, metering, marker_points)``.

        ``metering`` is ``"roi"`` for a fresh detection, ``"stale"`` when the
        previous box is reused, and ``"none"`` when no usable region is known.

        ``observed_at`` is when the detection was actually made.  The GUI polls
        ``snap["detections"]`` at 10 Hz while detections are only refreshed by
        a decision; without this stamp the same cached list would be re-labelled
        "fresh" forever and a stale plate box could raise exposure from the
        dark background (independent verification found exactly that).
        """
        box = plate_bbox(points, pad=self.pad, min_points=self.min_points)
        stamp = float(now if observed_at is None else observed_at)
        if box is not None:
            arr = np.asarray(points, float).reshape(-1, 2)
            self._box = box
            self._points = [(float(x), float(y)) for x, y in arr]
            self._at = stamp
            age = float(now) - stamp
            if age > self.hold_s:
                return None, "none", []
            return box, ("roi" if age <= self.fresh_s else "stale"), list(self._points)
        if self._box is not None and (float(now) - self._at) <= self.hold_s:
            return self._box, "stale", list(self._points)
        return None, "none", []

    def reset(self) -> None:
        self._box = None
        self._points = []
        self._at = 0.0


@dataclass
class CameraPolicy:
    target_low: float
    target_high: float
    min_us: int
    max_us: int
    min_gain: int
    max_gain: int
    step_up: float = 1.25
    step_down: float = 0.85
    fast_down: float = 0.60
    gain_step: int = 12
    # Plate clipping, measured after excluding the saturated marker cores.
    soft_saturation: float = 0.04
    hard_saturation: float = 0.15
    # Full-frame fallback: only gross clipping is trusted (markers are ~1 %).
    frame_hard_saturation: float = 0.25
    clip_p95: float = 250.0
    # Refuse an "up" step when the plate highlights are already close to
    # clipping; the loop then holds instead of stepping into a clip/down cycle.
    headroom_p95: float = 235.0
    min_contrast: float = 12.0
    low_margin: float = 0.05      # fraction of target_low
    high_margin: float = 0.05     # fraction of target_high
    min_laser: int = 40


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

    def copy(self) -> "ExposureSettings":
        return ExposureSettings(dict(self.exposure_us), dict(self.gain),
                                int(self.laser_percent))


@dataclass
class Advice:
    settings: ExposureSettings | None
    reason: str
    changes: dict = field(default_factory=dict)
    at_limits: tuple[str, ...] = ()
    stats: dict | None = None
    readback_ok: bool | None = None
    mismatch: dict | None = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)


class AutoExposure:
    """Stateful, hysteretic exposure advisor.

    ``observe()`` is called at the display rate but only evaluates when the
    minimum interval has passed.  It returns an :class:`Advice`; ``None``
    settings means "keep the current values".
    """

    def __init__(self, *, min_interval_s: float = 0.75,
                 settle_frames: int = 2, freeze_after_accept_s: float = 1.6,
                 readback_grace_s: float = 2.0):
        self.min_interval_s = float(min_interval_s)
        self.settle_frames = max(1, int(settle_frames))
        self.freeze_after_accept_s = float(freeze_after_accept_s)
        self.readback_grace_s = float(readback_grace_s)
        self._last_eval = float("-inf")
        self._frozen_until = 0.0
        self._counters = {"ir_up": 0, "ir_down": 0, "rgb_up": 0, "rgb_down": 0}
        self._history: list[dict] = []
        self._pending: tuple[dict, float] | None = None
        self._unconfirmed: dict | None = None
        self._last_readback: dict | None = None
        self._last_mismatch: dict | None = None
        self._mismatch_count = 0
        self._readback_ok: bool | None = None
        self._last_stats: dict | None = None
        self._last_limits: tuple[str, ...] = ()
        self._last_reason = ""
        self._evaluations = 0

    # -- external events -------------------------------------------------
    def freeze(self, seconds: float | None = None) -> None:
        span = self.freeze_after_accept_s if seconds is None else float(seconds)
        self._frozen_until = max(self._frozen_until, time.monotonic() + span)

    def reset(self) -> None:
        """Forget per-run history, confirmations and readback tracking."""
        self._last_eval = float("-inf")
        self._frozen_until = 0.0
        self._counters = {key: 0 for key in self._counters}
        self._history.clear()
        self._pending = None
        self._unconfirmed = None
        self._last_readback = None
        self._last_mismatch = None
        self._mismatch_count = 0
        self._readback_ok = None
        self._last_stats = None
        self._last_limits = ()
        self._last_reason = ""
        self._evaluations = 0

    # -- decision ---------------------------------------------------------
    def observe(self, *, ir: FrameStats | None, rgb: FrameStats | None,
                current: ExposureSettings,
                now: float | None = None,
                stationary: bool = False) -> Advice:
        now = time.monotonic() if now is None else float(now)
        self._evaluations += 1
        self._check_readback(current, now)
        self._last_stats = {
            "ir": None if ir is None else ir.as_dict(),
            "rgb": None if rgb is None else rgb.as_dict(),
        }
        ir_hard = self._is_hard(ir, IR_POLICY)
        rgb_hard = self._is_hard(rgb, RGB_POLICY)
        hard = ir_hard or rgb_hard
        if now < self._frozen_until and not hard:
            self._last_reason = "frozen after an accepted view"
            return Advice(None, self._last_reason)
        if (stationary and not hard
                and not self._urgent(ir, IR_POLICY)
                and not self._urgent(rgb, RGB_POLICY)):
            self._last_reason = "stationary hold"
            return Advice(None, self._last_reason)
        if not hard and now - self._last_eval < self.min_interval_s:
            self._last_reason = "settling"
            return Advice(None, self._last_reason)
        self._last_eval = now
        exp = dict(current.exposure_us)
        gain = dict(current.gain)
        laser = int(current.laser_percent)
        changes: dict[str, int | str] = {}
        reasons: list[str] = []
        limits: list[str] = []

        if ir is not None:
            laser = self._adjust_ir(ir, exp, gain, laser, changes, reasons,
                                    limits)
        if rgb is not None:
            self._adjust_rgb(rgb, exp, gain, changes, reasons, limits)

        self._last_limits = tuple(limits)
        if not changes:
            if limits:
                reason = "at limit: " + ", ".join(limits)
            elif reasons:
                reason = "; ".join(reasons)
            else:
                reason = "within target"
            self._last_reason = reason
            return Advice(None, reason, at_limits=tuple(limits),
                          stats=self._last_stats,
                          readback_ok=self._readback_ok,
                          mismatch=self._last_mismatch)
        for cam in (0, 1, 2):
            exp[cam] = int(exp.get(cam, 0))
            gain[cam] = int(gain.get(cam, 0))
        out = ExposureSettings(exp, gain, laser)
        if out.as_capture_args() == current.as_capture_args():
            # Belt and braces: a "change" that reproduces the readback is not
            # a change and must never be written or logged.
            reason = "within target"
            self._last_reason = reason
            return Advice(None, reason, at_limits=tuple(limits),
                          stats=self._last_stats,
                          readback_ok=self._readback_ok,
                          mismatch=self._last_mismatch)
        reason = "; ".join(reasons) if reasons else "adjusted"
        self._last_reason = reason
        record = {"at_s": time.time(), "reason": reason,
                  "changes": dict(changes),
                  "settings": out.as_capture_args(),
                  "stats": self._last_stats,
                  "at_limits": list(limits)}
        self._history.append(record)
        self._pending = (out.as_capture_args(), now)
        self._unconfirmed = out.as_capture_args()
        self._readback_ok = None
        return Advice(out, reason, dict(changes), at_limits=tuple(limits),
                      stats=self._last_stats, readback_ok=None,
                      mismatch=self._last_mismatch)

    # -- per-group adjustments -------------------------------------------
    def _adjust_ir(self, stats: FrameStats, exp: dict, gain: dict, laser: int,
                   changes: dict, reasons: list[str],
                   limits: list[str]) -> int:
        policy = IR_POLICY
        key, cause = self._classify(stats, policy)
        count = self._bump("ir", key)
        if key in ("ok", "hold"):
            if cause == "headroom":
                limits.append(f"IR at highlight headroom (p95 "
                              f"{stats.p95:.0f}); holding")
            return laser
        need = 1 if key == "hard" else self.settle_frames
        if count < need:
            return laser
        self._counters["ir_up"] = 0
        self._counters["ir_down"] = 0
        base = min(int(exp.get(0, 0)), int(exp.get(1, 0)))
        if key in ("down", "hard"):
            step = policy.fast_down if key == "hard" else policy.step_down
            new_us = max(policy.min_us, int(base * step))
            if new_us < base:
                exp[0] = exp[1] = new_us
                changes["ir_exposure_us"] = new_us
                reasons.append(self._reason("IR", cause, stats))
                return laser
            new_gain = max(policy.min_gain, min(int(gain.get(0, 0)),
                                                int(gain.get(1, 0)))
                           - policy.gain_step)
            pair_gain = min(int(gain.get(0, 0)), int(gain.get(1, 0)))
            if new_gain < pair_gain:
                gain[0] = gain[1] = new_gain
                changes["ir_gain"] = new_gain
                reasons.append(f"IR at minimum exposure; gain -> {new_gain}")
                return laser
            new_laser = max(policy.min_laser, int(laser) - 10)
            if new_laser < int(laser):
                changes["laser_percent"] = new_laser
                reasons.append(f"IR exposure/gain at minimum; illuminator -> "
                               f"{new_laser}%")
                return new_laser
            limits.append("IR exposure, gain and illuminator at minimum")
            return laser
        # Up.
        if stats.metering == "stale":
            limits.append("IR metering stale; holding")
            return laser
        new_us = min(policy.max_us, int(base * policy.step_up))
        if new_us > base:
            exp[0] = exp[1] = new_us
            changes["ir_exposure_us"] = new_us
            reasons.append(self._reason("IR", cause, stats))
            return laser
        new_gain = min(policy.max_gain, max(int(gain.get(0, 0)),
                                            int(gain.get(1, 0)))
                       + policy.gain_step)
        pair_gain = max(int(gain.get(0, 0)), int(gain.get(1, 0)))
        if new_gain > pair_gain:
            gain[0] = gain[1] = new_gain
            changes["ir_gain"] = new_gain
            reasons.append(f"IR exposure at maximum; gain -> {new_gain}")
            return laser
        new_laser = min(100, int(laser) + 10)
        if new_laser > int(laser):
            changes["laser_percent"] = new_laser
            reasons.append(f"IR exposure/gain at maximum; illuminator -> "
                           f"{new_laser}%")
            return new_laser
        limits.append("IR exposure, gain and illuminator at maximum")
        return laser

    def _adjust_rgb(self, stats: FrameStats, exp: dict, gain: dict,
                    changes: dict, reasons: list[str], limits: list[str]) -> None:
        policy = RGB_POLICY
        key, cause = self._classify(stats, policy)
        count = self._bump("rgb", key)
        if key in ("ok", "hold"):
            if cause == "headroom":
                limits.append(f"RGB at highlight headroom (p95 "
                              f"{stats.p95:.0f}); holding")
            return
        need = 1 if key == "hard" else self.settle_frames
        if count < need:
            return
        self._counters["rgb_up"] = 0
        self._counters["rgb_down"] = 0
        base = int(exp.get(2, 0))
        if key in ("down", "hard"):
            step = policy.fast_down if key == "hard" else policy.step_down
            new_us = max(policy.min_us, int(base * step))
            if new_us < base:
                exp[2] = new_us
                changes["rgb_exposure_us"] = new_us
                reasons.append(self._reason("RGB", cause, stats))
                return
            new_gain = max(policy.min_gain, int(gain.get(2, 0))
                           - policy.gain_step)
            if new_gain < int(gain.get(2, 0)):
                gain[2] = new_gain
                changes["rgb_gain"] = new_gain
                reasons.append(f"RGB at minimum exposure; gain -> {new_gain}")
                return
            limits.append("RGB exposure and gain at minimum")
            return
        if stats.metering == "stale":
            limits.append("RGB metering stale; holding")
            return
        new_us = min(policy.max_us, int(base * policy.step_up))
        if new_us > base:
            exp[2] = new_us
            changes["rgb_exposure_us"] = new_us
            reasons.append(self._reason("RGB", cause, stats))
            return
        new_gain = min(policy.max_gain, int(gain.get(2, 0))
                       + policy.gain_step)
        if new_gain > int(gain.get(2, 0)):
            gain[2] = new_gain
            changes["rgb_gain"] = new_gain
            reasons.append(f"RGB exposure at maximum; gain -> {new_gain}")
            return
        limits.append(f"RGB exposure {int(base)} us and gain "
                      f"{int(gain.get(2, 0))} at maximum")

    @staticmethod
    def _reason(label: str, cause: str, stats: FrameStats) -> str:
        if cause == "clipping":
            return (f"{label} clipping (sat {stats.saturation:.3f}, "
                    f"p95 {stats.p95:.0f})")
        if cause == "saturation":
            return f"{label} saturation (sat {stats.saturation:.3f})"
        if cause == "bright":
            return f"{label} bright (p50 {stats.mean:.0f})"
        if cause == "contrast":
            return f"{label} low contrast ({stats.contrast:.0f})"
        return f"{label} dark (p50 {stats.mean:.0f})"

    # -- readback ---------------------------------------------------------
    def _check_readback(self, current: ExposureSettings, now: float) -> None:
        self._last_readback = current.as_capture_args()
        if (self._unconfirmed is not None
                and current.as_capture_args() == self._unconfirmed):
            # The request eventually reached the device.  This clears both a
            # pending confirmation and a previously reported mismatch.
            self._readback_ok = True
            self._pending = None
            self._unconfirmed = None
            self._last_mismatch = None
            return
        if self._pending is None:
            return
        requested, at = self._pending
        if now - at <= self.readback_grace_s:
            # The child polls the control file; its readback is expected to
            # lag the request by up to one poll interval.
            return
        self._last_mismatch = {
            "requested": requested,
            "readback": current.as_capture_args(),
            "after_s": round(float(now - at), 3),
        }
        self._mismatch_count += 1
        self._readback_ok = False
        self._pending = None

    @property
    def last_mismatch(self) -> dict | None:
        return self._last_mismatch

    # -- classification ---------------------------------------------------
    @staticmethod
    def _is_hard(stats: FrameStats | None, policy: CameraPolicy) -> bool:
        if stats is None:
            return False
        return AutoExposure._classify(stats, policy)[0] == "hard"

    @staticmethod
    def _urgent(stats: FrameStats | None, policy: CameraPolicy) -> bool:
        """True when the frame is far outside the target and needs action even
        during a stationary hold."""
        if stats is None or stats.metering == "frame":
            return False
        return (stats.mean < policy.target_low * 0.80
                or stats.mean > policy.target_high * 1.20)

    @staticmethod
    def _classify(stats: FrameStats, policy: CameraPolicy) -> tuple[str, str]:
        if stats.metering == "frame":
            # No plate ROI: the median is the dark background.  Only gross
            # full-sensor clipping may override the hold.
            if stats.saturation >= policy.frame_hard_saturation:
                return "hard", "clipping"
            return "hold", "no-roi"
        if (stats.saturation >= policy.hard_saturation
                or stats.p95 >= policy.clip_p95):
            return "hard", "clipping"
        if stats.saturation > policy.soft_saturation:
            return "down", "saturation"
        low = policy.target_low * (1.0 - policy.low_margin)
        high = policy.target_high * (1.0 + policy.high_margin)
        if stats.mean > high:
            return "down", "bright"
        if stats.mean < low:
            if stats.p95 >= policy.headroom_p95:
                return "hold", "headroom"
            return "up", "dark"
        if stats.contrast < policy.min_contrast:
            return "up", "contrast"
        return "ok", ""

    def _bump(self, group: str, key: str) -> int:
        """Count consecutive observations in one direction.

        An ``ok``/``hold`` observation clears both directions; a reversal
        clears the opposite counter so stale evidence cannot trigger a change.
        """
        up, down = f"{group}_up", f"{group}_down"
        if key in ("ok", "hold"):
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

    # -- introspection ----------------------------------------------------
    @property
    def history(self) -> list[dict]:
        return list(self._history)

    def status(self) -> dict:
        """Diagnostics for the session log / UI (never used for control)."""
        readback = self._last_readback or {}
        exp = readback.get("exposure_us") or {}
        gain = readback.get("gain") or {}
        hardware_max = {
            "ir_exposure": bool(exp) and int(exp.get(0, 0)) >= IR_POLICY.max_us,
            "rgb_exposure": bool(exp) and int(exp.get(2, 0)) >= RGB_POLICY.max_us,
            "rgb_gain": bool(gain) and int(gain.get(2, 0)) >= RGB_POLICY.max_gain,
        }
        pair_mismatch = (bool(exp)
                         and int(exp.get(0, 0)) != int(exp.get(1, 0)))
        return {
            "evaluations": self._evaluations,
            "hardware_max": hardware_max,
            "ir_pair_mismatch": pair_mismatch,
            "changes": len(self._history),
            "min_interval_s": self.min_interval_s,
            "settle_frames": self.settle_frames,
            "freeze_remaining_s": max(0.0, self._frozen_until - time.monotonic()),
            "counters": dict(self._counters),
            "last_reason": self._last_reason,
            "last_stats": self._last_stats,
            "last_limits": list(self._last_limits),
            "requested": None if self._pending is None else self._pending[0],
            "readback": self._last_readback,
            "readback_ok": self._readback_ok,
            "mismatch": self._last_mismatch,
            "mismatch_count": self._mismatch_count,
        }
