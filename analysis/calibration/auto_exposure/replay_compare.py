"""Before/after closed-loop simulation for the auto-exposure audit.

The real session log cannot be replayed closed-loop because the exposure
changes alter the recorded images.  This script therefore drives the legacy
controller (``_legacy_exposure.py``, extracted from git HEAD) and the current
controller through the same synthetic plant and the same distance schedule,
and reports the behaviour difference.  It is a control-law comparison, not a
photometric measurement.

Scene model (linear sensor, empirical normalisation)::

    plate_p50 = 70 DN * (exposure / 1200 us) * (400 mm / distance)^2

with the plate clipped at 250 DN.  The retro-reflective markers always occupy
a small fraction of the *full* frame and clip at 255 DN - that is exactly the
signal the legacy controller mistook for plate saturation.  Plate ROI
saturation is reported separately *after* the marker disks are masked, which is
what the current controller consumes.

Run with the project venv::

    .venv/bin/python analysis/calibration/auto_exposure/replay_compare.py
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from tools.wizard import exposure as new  # noqa: E402


def _load_legacy():
    spec = importlib.util.spec_from_file_location(
        "_legacy_exposure", HERE / "_legacy_exposure.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load_legacy()


def plate_dn(distance_mm: float, exposure_us: float,
             base_us: float = 1200.0) -> float:
    return min(250.0, 70.0 * (exposure_us / float(base_us))
               * (400.0 / float(distance_mm)) ** 2)


def marker_fraction(distance_mm: float) -> float:
    """Saturated marker area as a fraction of the *full* frame.

    Taken from the 208 accepted views of the real session: 0.5 % at 400-450 mm,
    0.3 % at mid range, larger when the plate fills the frame at ~320 mm.
    """
    if distance_mm <= 340.0:
        return 0.020
    if distance_mm <= 430.0:
        return 0.005
    return 0.0035


def bloom_saturation(plate_dn_value: float) -> float:
    """Extra full-frame clipping from plate highlights as exposure rises."""
    return max(0.0, plate_dn_value - 50.0) * 0.00025


class Scene:
    def __init__(self, distance_mm: float):
        self.distance_mm = float(distance_mm)

    def legacy_stats(self, exposure_us: float, lost: bool,
                     base_us: float = 1200.0, marker_scale: float = 1.0,
                     bloom_scale: float = 1.0):
        if lost:
            # legacy ``_auto_expose`` falls back to the whole frame.  The
            # plate is out of view, so there is no marker clipping either:
            # the median is dark background and p99 is below the clip rule.
            return legacy.FrameStats(mean=25.0, p99=238.0,
                                     saturation=0.002, contrast=18.0)
        plate = plate_dn(self.distance_mm, exposure_us, base_us)
        frac = marker_fraction(self.distance_mm) * marker_scale
        sat = frac + bloom_saturation(plate) * bloom_scale
        if plate >= 250.0:
            sat += 0.22
        p99 = 255.0 if sat >= 0.010 else 238.0
        return legacy.FrameStats(
            mean=plate, p99=p99, saturation=min(1.0, sat),
            contrast=min(160.0, 0.7 * plate))

    def new_stats(self, exposure_us: float, lost: bool, lost_for_s: float,
                  base_us: float = 1200.0):
        plate = plate_dn(self.distance_mm, exposure_us, base_us)
        if lost:
            if lost_for_s <= 2.0:
                # stale plate box: brightness is still meaningful, but an "up"
                # correction is suppressed while the box is stale
                return new.FrameStats(mean=plate, p99=255.0, saturation=0.0,
                                      contrast=min(160.0, 0.7 * plate),
                                      p95=min(255.0, 1.6 * plate),
                                      metering="stale")
            # no ROI at all: dark background median is not an exposure target
            return new.FrameStats(mean=25.0, p99=255.0, saturation=0.0,
                                  contrast=18.0, p95=255.0,
                                  metering="frame")
        clipped = plate >= 250.0
        return new.FrameStats(
            mean=plate, p99=255.0,
            saturation=0.5 if clipped else 0.0,
            contrast=min(160.0, 0.7 * plate),
            p95=min(255.0, 1.6 * plate), metering="roi")


DISTANCE_SCHEDULE = (
    (0.0, 20.0, 420.0),  # settle at a normal distance
    (20.0, 40.0, None),  # 420 -> 550 (far)
    (40.0, 55.0, 550.0),
    (55.0, 75.0, None),  # 550 -> 320 (close)
    (75.0, 90.0, 320.0),
    (90.0, 110.0, None),  # 320 -> 420
    (110.0, 180.0, 420.0),
)
# The real session rejected most decisions (704 / 866) and many of those had
# no usable marker detection at all, so the GUI's bbox source was frequently
# empty.  Model a lost detection for 2.5 s out of every 8 s.
DETECTION_LOSS_PERIOD = 8.0
DETECTION_LOSS_SPAN = 2.5
DT = 0.75


def distance_at(t: float) -> float:
    for start, end, value in DISTANCE_SCHEDULE:
        if start <= t < end:
            if value is not None:
                return value
            prev = distance_at(start - 1e-6)
            frac = (t - start) / (end - start)
            # find the next fixed value
            for s2, e2, v2 in DISTANCE_SCHEDULE:
                if abs(s2 - end) < 1e-9 and v2 is not None:
                    return prev + (v2 - prev) * frac
            return prev
    return 420.0


def lost_at(t: float) -> bool:
    return (t % DETECTION_LOSS_PERIOD) < DETECTION_LOSS_SPAN


def run(controller, *, legacy_mode: bool):
    scene = Scene(420.0)
    # The retained run begins at the previous run's last applied values: the
    # capture child applies the existing exposure-control.txt at startup.
    start = ({0: 2338, 1: 2338, 2: 20000}, {0: 105, 1: 105, 2: 300}, 100)
    if legacy_mode:
        cur = legacy.ExposureSettings(*start)
    else:
        cur = new.ExposureSettings(*start)
        # The live window re-establishes the configured baseline once.
        cur = new.ExposureSettings({0: 1200, 1: 1200, 2: 5000},
                                   {0: 105, 1: 105, 2: 180}, 100)
        baseline_writes = 1
    if legacy_mode:
        baseline_writes = 0
    stats_module = legacy if legacy_mode else new
    writes = baseline_writes
    noops = 0
    reversals = 0
    last_dir = 0
    out_of_band_s = 0.0
    saturated_s = 0.0
    rgb_pinned_s = 0.0
    lost_since = None
    timeline = []
    t = 0.0
    interval = 1.5 if legacy_mode else DT   # GUI 0.75 gate + 1.0 s advisor
    last_eval = -1.0
    while t <= 180.0:
        d = distance_at(t)
        scene.distance_mm = d
        lost = lost_at(t)
        if lost and lost_since is None:
            lost_since = t
        if not lost:
            lost_since = None
        lost_for = 0.0 if lost_since is None else t - lost_since
        if not lost:
            lost_for = 0.0
        exp_ir = cur.exposure_us[0]
        exp_rgb = cur.exposure_us[2]
        if legacy_mode:
            ir_stats = scene.legacy_stats(exp_ir, lost)
            # the colour image clips its markers far less than the IR cameras
            rgb_stats = scene.legacy_stats(exp_rgb, lost, base_us=5000.0,
                                           marker_scale=0.3,
                                           bloom_scale=0.2)
        else:
            ir_stats = scene.new_stats(exp_ir, lost, lost_for)
            rgb_stats = scene.new_stats(exp_rgb, lost, lost_for, base_us=5000.0)
        if last_eval < 0 or t - last_eval >= interval:
            advice = controller.observe(ir=ir_stats, rgb=rgb_stats,
                                        current=cur, now=t)
            last_eval = t
            if advice.changed and advice.settings is not None:
                proposal = advice.settings
                if proposal.as_capture_args() == cur.as_capture_args():
                    noops += 1
                else:
                    writes += 1
                    direction = (1 if proposal.exposure_us[0] > cur.exposure_us[0]
                                 else (-1 if proposal.exposure_us[0]
                                       < cur.exposure_us[0] else 0))
                    if direction and last_dir and direction != last_dir:
                        reversals += 1
                    if direction:
                        last_dir = direction
                    timeline.append({"t": round(t, 1), "d": round(d, 1),
                                     "reason": advice.reason,
                                     "changes": dict(advice.changes)})
                    cur = proposal
        plate = plate_dn(d, cur.exposure_us[0])
        if plate < 65.0 or plate > 150.0:
            out_of_band_s += interval
        if plate >= 250.0:
            saturated_s += interval
        if cur.exposure_us[2] >= 20000 and cur.gain[2] >= 300:
            rgb_pinned_s += interval
        t += interval
    return {
        "started_from_pinned_state": not legacy_mode,
        "baseline_writes": baseline_writes,
        "writes": writes,
        "noop_writes": noops,
        "ir_reversals": reversals,
        "changes": len(timeline),
        "out_of_band_s": round(out_of_band_s, 1),
        "saturated_s": round(saturated_s, 1),
        "rgb_pinned_s": round(rgb_pinned_s, 1),
        "final_ir_us": cur.exposure_us[0],
        "final_rgb_us": cur.exposure_us[2],
        "final_gain": [cur.gain[0], cur.gain[2]],
        "final_plate_dn": round(plate_dn(distance_at(180.0),
                                         cur.exposure_us[0]), 1),
        "timeline": timeline,
    }


def _noop_field_count(requested: dict, current: dict) -> int:
    """Changes that request the value already read back (no-op fields)."""
    count = 0
    for group, key in (("ir_exposure_us", "exposure_us"),
                       ("ir_gain", "gain"),
                       ("rgb_exposure_us", "exposure_us"),
                       ("rgb_gain", "gain"),
                       ("laser_percent", "laser_percent")):
        if group not in requested:
            continue
        if key == "laser_percent":
            if requested[group] == current[key]:
                count += 1
            continue
        cams = (0, 1) if group.startswith("ir_") else (2,)
        value = requested[group]
        if all(current[key][str(c)] == value if str(c) in current[key]
               else current[key][c] == value for c in cams):
            count += 1
    return count


def pinned_noop_scenario(module, *, legacy_mode: bool) -> dict:
    """The real end-of-session state: RGB pinned, plate out of view.

    The capture child applied ``exposure-control.txt`` at start, so the second
    recording began at IR 2325 us and RGB 20000 us / gain 300 (observed in
    ``ui_metrics.json``).  With no detectable plate the legacy controller
    meters the dark background and re-requests the same RGB gain on every
    evaluation; the current one holds and reports the limit.
    """
    cur = module.ExposureSettings({0: 2325, 1: 2325, 2: 20000},
                                  {0: 105, 1: 105, 2: 300}, 100)
    controller = (module.AutoExposure() if legacy_mode else
                  module.AutoExposure(min_interval_s=0.75))
    writes = 0
    noops = 0
    noop_fields = 0
    limits = 0
    interval = 1.5 if legacy_mode else 0.75
    t = 0.0
    for _ in range(40):
        if legacy_mode:
            stats = module.FrameStats(mean=20.0, p99=238.0,
                                      saturation=0.002, contrast=8.0)
        else:
            stats = module.FrameStats(mean=20.0, p99=238.0,
                                      saturation=0.002, contrast=8.0,
                                      metering="frame")
        advice = controller.observe(ir=stats, rgb=stats, current=cur, now=t)
        t += interval
        if advice.changed and advice.settings is not None:
            requested = advice.settings.as_capture_args()
            noop_fields += _noop_field_count(requested, cur.as_capture_args())
            if requested == cur.as_capture_args():
                noops += 1
            else:
                writes += 1
                cur = advice.settings
        if getattr(advice, "at_limits", ()):
            limits += 1
    return {"evaluations": 40, "writes": writes, "noop_writes": noops,
            "noop_fields": noop_fields, "limit_reports": limits,
            "final_rgb": [cur.exposure_us[2], cur.gain[2]]}


def main() -> int:
    old = run(legacy.AutoExposure(), legacy_mode=True)
    current = run(new.AutoExposure(min_interval_s=DT), legacy_mode=False)
    pinned_old = pinned_noop_scenario(legacy, legacy_mode=True)
    pinned_new = pinned_noop_scenario(new, legacy_mode=False)
    result = {"mode": "synthetic closed loop, 180 s, 320-550 mm sweep",
              "legacy": old, "current": current,
              "pinned_legacy": pinned_old, "pinned_current": pinned_new}
    (HERE / "before_after.json").write_text(
        json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for key in ("writes", "noop_writes", "ir_reversals", "out_of_band_s",
                "saturated_s", "rgb_pinned_s", "final_ir_us", "final_rgb_us",
                "final_plate_dn"):
        print(f"{key:16s} legacy={old[key]!s:>8}  current={current[key]!s:>8}")
    print("legacy changes:", len(old["timeline"]),
          " current changes:", len(current["timeline"]))
    for key in ("writes", "noop_writes", "noop_fields", "limit_reports"):
        print(f"pinned/{key:12s} legacy={pinned_old[key]!s:>8}"
              f"  current={pinned_new[key]!s:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
