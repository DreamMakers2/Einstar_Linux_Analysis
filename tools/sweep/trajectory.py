"""Trajectory analysis: motion, turnaround points and sweep segmentation.

The capture is a hand-held/robot sweep of a rigid plate, so the pose sequence is
a 1-D curve through pose space with reversals.  Nothing here assumes a
particular amplitude or axis: the dominant translation axis is *measured*
(largest peak-to-peak among the camera-frame x, y, z of the board origin), and
the reversals are local extrema of that axis after smoothing.

All timing uses the group's ``host_time_ns`` when available (device trigger
period is 100 ms; host timestamps jitter by a few hundred microseconds), and
falls back to the nominal cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from scipy.signal import find_peaks


def smooth(x: np.ndarray, window: int = 7) -> np.ndarray:
    """Centred moving average with edge padding (deterministic)."""
    x = np.asarray(x, float)
    window = int(max(1, window))
    if window <= 1 or len(x) < 3:
        return x.copy()
    window = min(window, len(x) if len(x) % 2 == 1 else len(x) - 1)
    if window % 2 == 0:
        window += 1
    if window <= 1:
        return x.copy()
    pad = window // 2
    xp = np.pad(x, (pad, pad), mode="edge")
    k = np.ones(window) / window
    return np.convolve(xp, k, mode="valid")


def dominant_axis(xyz: np.ndarray) -> tuple[int, float]:
    """Index and peak-to-peak range of the largest-range translation axis."""
    xyz = np.asarray(xyz, float)
    if len(xyz) < 2:
        return 0, 0.0
    ptp = np.nanmax(xyz, axis=0) - np.nanmin(xyz, axis=0)
    ptp = np.where(np.isfinite(ptp), ptp, 0.0)
    k = int(np.argmax(ptp))
    return k, float(ptp[k])


AXIS_NAME = {0: "x", 1: "y", 2: "z"}


@dataclass
class Turnaround:
    index: int          # position in the analysed sequence
    group: int
    kind: str           # "max" | "min"
    value_mm: float     # raw value at the extremum frame
    smooth_mm: float    # smoothed value used to locate it
    prominence_mm: float

    def as_dict(self) -> dict:
        return {"index": int(self.index), "group": int(self.group),
                "kind": self.kind,
                "value_mm": round(float(self.value_mm), 3),
                "smooth_mm": round(float(self.smooth_mm), 3),
                "prominence_mm": round(float(self.prominence_mm), 3)}


def find_turnarounds(groups: Sequence[int], values: np.ndarray,
                     window: int = 7,
                     min_prominence_frac: float = 0.06,
                     min_prominence_mm: float = 1.5,
                     min_separation: int = 6) -> tuple[list[Turnaround], np.ndarray]:
    """Local extrema of the dominant translation axis.

    ``min_prominence`` is ``max(min_prominence_mm, frac * peak_to_peak)`` so the
    decision scales with the observed motion instead of a hard-coded amplitude.
    """
    v = np.asarray(values, float)
    s = smooth(v, window)
    ptp = float(np.nanmax(s) - np.nanmin(s)) if len(s) else 0.0
    prom = max(float(min_prominence_mm), float(min_prominence_frac) * ptp)
    out: list[Turnaround] = []
    if len(s) < 5 or ptp <= 0:
        return out, s
    for kind, sig in (("max", s), ("min", -s)):
        idx, props = find_peaks(sig, prominence=prom, distance=int(min_separation))
        for k, p in zip(idx, props["prominences"]):
            if k <= 0 or k >= len(s) - 1:
                continue
            out.append(Turnaround(int(k), int(groups[k]), kind, float(v[k]),
                                  float(s[k]), float(p)))
    out.sort(key=lambda t: t.index)
    return out, s


def split_sweeps(n: int, turnarounds: Sequence[Turnaround],
                 smooth_vals: Optional[np.ndarray] = None) -> list[tuple[int, int, str]]:
    """Split ``0..n-1`` into legs at the turnarounds.

    Returns ``(start, end, direction)`` with inclusive indices; ``direction`` is
    ``"+"``/``"-"`` from the sign of the smoothed derivative over the leg, or
    ``"0"`` when the leg is flat.
    """
    cuts = [0] + [t.index for t in turnarounds if 0 < t.index < n - 1] + [n]
    legs = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        if b <= a:
            continue
        if smooth_vals is not None and b - a >= 2:
            d = float(smooth_vals[min(b, n - 1)] - smooth_vals[a])
            direction = "+" if d > 0 else ("-" if d < 0 else "0")
        else:
            direction = "0"
        legs.append((int(a), int(b - 1), direction))
    return legs


@dataclass
class Motion:
    dt_s: float
    d_center_mm: float
    d_rot_deg: float
    speed_mm_s: float
    ang_speed_deg_s: float

    def as_dict(self) -> dict:
        return {"dt_s": round(float(self.dt_s), 6),
                "d_center_mm": round(float(self.d_center_mm), 4),
                "d_rot_deg": round(float(self.d_rot_deg), 5),
                "speed_mm_s": round(float(self.speed_mm_s), 3),
                "ang_speed_deg_s": round(float(self.ang_speed_deg_s), 3)}


def motion_between(prev: dict, cur: dict) -> Optional[Motion]:
    """Frame-to-frame motion of the board origin between two pose records."""
    if not prev or not cur:
        return None
    t0 = np.asarray(prev["t"], float)
    t1 = np.asarray(cur["t"], float)
    R0 = np.asarray(prev["R"], float).reshape(3, 3)
    R1 = np.asarray(cur["R"], float).reshape(3, 3)
    dt = float(cur.get("time_s", np.nan) - prev.get("time_s", np.nan))
    if not np.isfinite(dt) or dt <= 0:
        return None
    d = float(np.linalg.norm(t1 - t0))
    c = (np.trace(R1 @ R0.T) - 1.0) / 2.0
    ang = float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))
    return Motion(dt, d, ang, d / dt, ang / dt)


def range_stats(values: Sequence[float]) -> dict:
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], float)
    if v.size == 0:
        return {"n": 0}
    q = np.percentile(v, [0, 1, 5, 50, 95, 99, 100])
    return {"n": int(v.size),
            "min": round(float(q[0]), 4), "p1": round(float(q[1]), 4),
            "p5": round(float(q[2]), 4), "p50": round(float(q[3]), 4),
            "p95": round(float(q[4]), 4), "p99": round(float(q[5]), 4),
            "max": round(float(q[6]), 4),
            "mean": round(float(v.mean()), 4),
            "std": round(float(v.std(ddof=1)) if v.size > 1 else 0.0, 4),
            "range": round(float(q[6] - q[0]), 4)}
