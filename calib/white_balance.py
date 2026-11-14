"""Photometric (white-balance) calibration — a layer *separate* from geometry.

Why this module exists
----------------------
The reconstruction engine debayers the colour stream and applies **no** white
balance (``docs/reconstruction/real_data_report.md`` §14): the raw sensor keeps a
strong green cast (``R/G 0.72, B/G 0.65`` on the highlight set of a real
recording).  EXStar instead stores a diagonal gain matrix measured by its
``calcWhiteBalance`` step.  This module reconstructs *that* measurement, stores
the result as its own versioned profile layer, and refuses to publish numbers it
cannot defend.

What is deliberately **not** here
---------------------------------
* No geometry: this module never reads or writes a CCF, an intrinsic, an
  extrinsic, or anything under a factory profile directory.  A photometric
  profile carries ``geometry_profile_digest: null`` by construction and cannot
  be merged into a geometric one.
* No implicit activation: every profile is written with
  ``activation.activated = false`` and nothing here can set it true.
  :func:`write_profile` refuses factory paths.
* No claim of colorimetric accuracy.  This is a grey-world estimate on the
  scanner's own white plate, and a single diagonal gain cannot remove the
  colour shading that the same plate shows across the field (measured in
  ``docs/calibration/white_balance.md``).

The estimator (recovered from EXStar)
-------------------------------------
``Sn3DColorCorrect.dll`` ``SNColorAlgorithm::snWhiteBalanceCali`` sums each
channel over the **centred sub-window** ``width/divisor x height/divisor``,
skips pixels whose brightest channel exceeds a clip level, subtracts an optional
per-channel dark current, and returns a 3x3 matrix in which

    matrix[0] = sum(green) / sum(slot 0)
    matrix[4] = 1.0                          (hard-coded)
    matrix[8] = sum(green) / sum(slot 2)

on the *interleaved 3-channel buffer* it is handed.  Re-measured against the two
stored files this reproduces ``wbdata.txt`` (``1.603466`` / ``1.794047``) and
``wbdata_noled.txt`` (``1.605239`` / ``1.796383``) to about 1.5 %.

**The slot-order trap.**  EXStar's slot 0 is the Bayer phase at
``(odd row, odd column)`` of the stored mosaic, and its slot 2 is the phase at
``(even, even)``.  OpenCV's ``cv2.cvtColor(mosaic, COLOR_BayerRG2BGR)`` — the
call the engine uses — puts the ``(even, even)`` phase in *slot 0*.  The two
orders are therefore **reversed**, and copying EXStar's ``diag(...)`` straight
onto our BGR buffer mis-corrects by ~10 % on both channels.  This module always
states gains in :attr:`CHANNEL_ORDER` (our buffer) and provides
:func:`to_exstar_matrix` / :func:`from_exstar_matrix` for the swap.

Whether the ``(even, even)`` phase is physically the red or the blue sensor site
is **not** established by the calibration files; every quantity here is defined
against phase position and the engine's slot order, so the estimate does not
depend on that label.  Only an absolute-colour claim would.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

import numpy as np

__all__ = [
    "PROFILE_SCHEMA", "PROFILE_SCHEMA_VERSION", "LAYER", "CHANNEL_ORDER",
    "SLOT0_PHASE", "EXSTAR_SLOT0_PHASE", "ESTIMATOR_ID",
    "EstimatorConfig", "PhasePlanes", "ChannelSummary", "WbEstimate",
    "RefusedCalibration", "bayer_phase_planes", "window_bounds", "estimate",
    "estimate_pair", "to_exstar_matrix", "from_exstar_matrix", "apply_gains",
    "revert_gains", "correct", "neutrality", "mask_means", "clipped_mask",
    "plate_mask", "build_profile", "profile_digest",
    "write_profile", "load_profile", "validate_profile",
]

#: schema identifier written into every profile; loading enforces version 1
PROFILE_SCHEMA = "einstar.white_balance.profile"
PROFILE_SCHEMA_VERSION = 1
#: this layer is photometric and must never be merged with a geometric profile
LAYER = "photometric"
#: channel order of :attr:`WbEstimate.gains` — the engine's BGR buffer
CHANNEL_ORDER = "BGR"
#: which half-resolution Bayer phase lands in slot 0 of that buffer
SLOT0_PHASE = "p00"
#: which phase EXStar's stored ``diag(...)`` applies its first gain to
EXSTAR_SLOT0_PHASE = "p11"
#: name of the estimator, recorded so a different one can coexist in the profile
ESTIMATOR_ID = "exstar-central-window-greyworld"

#: directory fragments that hold factory / device calibration; never written here
_FACTORY_MARKERS = ("res/einscan-e10", "res/einscan-ea", "einstar-calibration-03",
                    "/factory/", "\\factory\\")


class RefusedCalibration(RuntimeError):
    """Raised when the evidence is not good enough to publish gains."""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EstimatorConfig:
    """Parameters of the recovered EXStar estimator plus Linux-side guard rails.

    ``window_divisor``, ``clip_level``, ``subtract_dark`` and ``dark_current``
    are the *recovered* EXStar parameters.  The rest are acceptance thresholds:
    they never move the point estimate, they only decide whether it may be
    published.
    """

    #: window is ``width//divisor x height//divisor``, centred (EXStar: >= 3)
    window_divisor: int = 8
    #: a pixel is skipped when any channel exceeds this fraction of full scale
    clip_level: float = 0.95
    #: subtract ``dark_current`` before summing (EXStar: on, values from ParaDark)
    subtract_dark: bool = True
    #: per-channel dark current in slot order, fractions of full scale
    dark_current: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: plausibility band for a published gain (tools/scan/color.py uses the same)
    min_gain: float = 0.25
    max_gain: float = 4.0
    #: refuse when more than this fraction of the window is clipped
    max_clipped_fraction: float = 0.20
    #: refuse when fewer valid pixels than this were summed
    min_samples: int = 1024
    #: refuse when a dark-subtracted channel mean is below this (DN of 255)
    min_channel_dn: float = 2.0
    #: status may only become "validated" when the holdout cast is below this
    max_holdout_cast: float = 0.05

    def as_dict(self) -> dict:
        return {
            "window_divisor": int(self.window_divisor),
            "clip_level": float(self.clip_level),
            "subtract_dark": bool(self.subtract_dark),
            "dark_current_slot_order": [float(v) for v in self.dark_current],
            "min_gain": float(self.min_gain),
            "max_gain": float(self.max_gain),
            "max_clipped_fraction": float(self.max_clipped_fraction),
            "min_samples": int(self.min_samples),
            "min_channel_dn": float(self.min_channel_dn),
            "max_holdout_cast": float(self.max_holdout_cast),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "EstimatorConfig":
        return cls(
            window_divisor=int(d.get("window_divisor", 8)),
            clip_level=float(d.get("clip_level", 0.95)),
            subtract_dark=bool(d.get("subtract_dark", True)),
            dark_current=tuple(float(v) for v in
                               d.get("dark_current_slot_order", (0.0, 0.0, 0.0))),
            min_gain=float(d.get("min_gain", 0.25)),
            max_gain=float(d.get("max_gain", 4.0)),
            max_clipped_fraction=float(d.get("max_clipped_fraction", 0.20)),
            min_samples=int(d.get("min_samples", 1024)),
            min_channel_dn=float(d.get("min_channel_dn", 2.0)),
            max_holdout_cast=float(d.get("max_holdout_cast", 0.05)),
        )


# ---------------------------------------------------------------------------
# Bayer handling
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PhasePlanes:
    """Half-resolution planes of one raw mosaic, named by phase position.

    ``p00`` is the phase at even row / even column, ``p11`` the phase at odd
    row / odd column, and ``g`` the mean of the two remaining (green) phases.
    Names are positions, not colours: see the module docstring.
    """

    p00: np.ndarray
    g: np.ndarray
    p11: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return self.g.shape

    def as_bgr(self) -> np.ndarray:
        """Stack in the engine's BGR slot order: slot0 = ``p00``, slot2 = ``p11``.

        This is what ``cv2.cvtColor(mosaic, COLOR_BayerRG2BGR)`` produces
        (verified: a synthetic mosaic with distinct phase levels maps
        ``(even, even)`` to output slot 0).
        """
        return np.stack([self.p00, self.g, self.p11], axis=-1)


def bayer_phase_planes(mosaic) -> PhasePlanes:
    """Split an even-sized raw mosaic into (p00, green, p11) half-resolution planes.

    The two green phases are averaged, which is what a linear demosaic does to
    first order; on a smooth plate the plane means are unchanged by it.  This is
    an analysis helper, not a replacement for the engine's debayer.
    """
    m = np.asarray(mosaic, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError(f"expected a 2-D mosaic, got shape {m.shape}")
    h, w = m.shape
    if h < 4 or w < 4 or (h % 2) or (w % 2):
        raise ValueError(f"mosaic must be even and at least 4x4, got {m.shape}")
    return PhasePlanes(p00=m[0::2, 0::2],
                       g=0.5 * (m[0::2, 1::2] + m[1::2, 0::2]),
                       p11=m[1::2, 1::2])


def window_bounds(shape: tuple[int, int], divisor: int) -> tuple[int, int, int, int]:
    """Centred ``(row0, col0, rows, cols)`` window, EXStar's integer arithmetic.

    Mirrors ``snWhiteBalanceCali``: ``rows = h//divisor``, ``cols = w//divisor``
    and both offsets are ``size//2 - extent//2`` so the window is centred.
    """
    if divisor < 1:
        raise ValueError("window_divisor must be >= 1")
    h, w = int(shape[0]), int(shape[1])
    rows, cols = h // divisor, w // divisor
    if rows < 1 or cols < 1:
        raise ValueError(f"divisor {divisor} leaves an empty window for {shape}")
    return h // 2 - rows // 2, w // 2 - cols // 2, rows, cols


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChannelSummary:
    """What the estimator measured, kept whole so every number can be audited."""

    n_total: int
    n_valid: int
    n_clipped: int
    clipped_fraction: float
    sum_slots: tuple[float, float, float]
    mean_slots: tuple[float, float, float]
    window: tuple[int, int, int, int]

    def as_dict(self) -> dict:
        return {
            "n_total": int(self.n_total),
            "n_valid": int(self.n_valid),
            "n_clipped": int(self.n_clipped),
            "clipped_fraction": round(float(self.clipped_fraction), 6),
            "sum_slots": [round(float(v), 6) for v in self.sum_slots],
            "mean_slots": [round(float(v), 6) for v in self.mean_slots],
            "mean_slots_dn": [round(float(v) * 255.0, 4) for v in self.mean_slots],
            "window_row0_col0_rows_cols": [int(v) for v in self.window],
        }


@dataclass(frozen=True)
class WbEstimate:
    """One grey-world estimate plus everything needed to judge it."""

    gains: tuple[float, float, float]          # engine BGR buffer order
    summary: ChannelSummary
    refused: bool = False
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        ok = not self.refused
        return {
            "gains_bgr": [round(float(g), 9) for g in self.gains] if ok else None,
            "exstar_matrix": to_exstar_matrix(self.gains) if ok else None,
            "refused": bool(self.refused),
            "reasons": list(self.reasons),
            "measurement": self.summary.as_dict(),
        }

    def require(self) -> "WbEstimate":
        """Return self, or raise :class:`RefusedCalibration` with the reasons."""
        if self.refused:
            raise RefusedCalibration("; ".join(self.reasons) or "refused")
        return self


def estimate(mosaic, cfg: Optional[EstimatorConfig] = None,
             *, label: str = "") -> WbEstimate:
    """Grey-world gains from one raw mosaic, EXStar's window and rejection rules.

    Returns a :class:`WbEstimate`; data problems produce ``refused=True`` with
    reasons instead of an exception so a caller can report them.
    """
    cfg = cfg or EstimatorConfig()
    planes = bayer_phase_planes(mosaic)
    r0, c0, rows, cols = window_bounds(planes.shape, cfg.window_divisor)
    cut = (slice(r0, r0 + rows), slice(c0, c0 + cols))
    slots = [planes.p00[cut] / 255.0, planes.g[cut] / 255.0, planes.p11[cut] / 255.0]

    if cfg.subtract_dark:
        dark = [float(v) for v in cfg.dark_current]
        if len(dark) != 3:
            raise ValueError("dark_current must have three values")
        slots = [s - d for s, d in zip(slots, dark)]

    stack = np.stack(slots, axis=0)
    clipped = (stack > float(cfg.clip_level)).any(axis=0)
    valid = ~clipped
    n_total = int(valid.size)
    n_valid = int(valid.sum())
    n_clipped = int(clipped.sum())

    sums = tuple(float(stack[i][valid].sum()) for i in range(3))
    means = tuple(float(stack[i][valid].mean()) if n_valid else float("nan")
                  for i in range(3))
    summary = ChannelSummary(
        n_total=n_total, n_valid=n_valid, n_clipped=n_clipped,
        clipped_fraction=(n_clipped / n_total) if n_total else 0.0,
        sum_slots=sums, mean_slots=means, window=(r0, c0, rows, cols))

    tag = f" [{label}]" if label else ""
    reasons: list[str] = []
    if n_valid < cfg.min_samples:
        reasons.append(f"too few valid pixels: {n_valid} < {cfg.min_samples}{tag}")
    if n_total and summary.clipped_fraction > cfg.max_clipped_fraction:
        reasons.append(f"window is {100.0 * summary.clipped_fraction:.1f}% clipped "
                       f"(> {100.0 * cfg.max_clipped_fraction:.0f}%): over-exposed{tag}")
    if any(m * 255.0 < cfg.min_channel_dn for m in means):
        reasons.append("a channel is at the dark/noise floor (slot means in DN = "
                       f"{[round(m * 255.0, 2) for m in means]}){tag}")
    if any(m <= 0.0 for m in means):
        reasons.append(f"dark current exceeds the signal in at least one slot{tag}")

    gains = (float("nan"), 1.0, float("nan"))
    if not reasons:
        g0 = sums[1] / sums[0] if sums[0] else float("nan")
        g2 = sums[1] / sums[2] if sums[2] else float("nan")
        gains = (g0, 1.0, g2)
        if not all(np.isfinite(gains)):
            reasons.append(f"non-finite gain (a slot summed to zero){tag}")
        elif not all(cfg.min_gain <= g <= cfg.max_gain for g in (g0, g2)):
            reasons.append(
                f"gain outside plausibility band [{cfg.min_gain}, {cfg.max_gain}]: "
                f"slot0={g0:.3f} slot2={g2:.3f}{tag}")

    if reasons:
        return WbEstimate(gains=gains, summary=summary, refused=True,
                          reasons=tuple(reasons))
    return WbEstimate(gains=gains, summary=summary)


def estimate_pair(led_on, led_off, cfg: Optional[EstimatorConfig] = None,
                  *, labels: Sequence[str] = ("led_on", "led_off")
                  ) -> dict[str, WbEstimate]:
    """Estimate both EXStar conditions (illuminator on / off) from two mosaics."""
    return {labels[0]: estimate(led_on, cfg, label=labels[0]),
            labels[1]: estimate(led_off, cfg, label=labels[1])}


# ---------------------------------------------------------------------------
# matrix helpers / application
# ---------------------------------------------------------------------------
def to_exstar_matrix(gains_bgr: Sequence[float]) -> list[float]:
    """Return the 9-float row-major matrix EXStar writes to ``wbdata*.txt``.

    EXStar's slot 0 is our ``p11`` and its slot 2 is our ``p00``, so the gains
    are **reversed** relative to :attr:`CHANNEL_ORDER`.  This is the single most
    error-prone step in the whole layer, hence a dedicated function and tests.
    """
    g0, g1, g2 = (float(v) for v in gains_bgr)
    return [g2, 0.0, 0.0, 0.0, g1, 0.0, 0.0, 0.0, g0]


def from_exstar_matrix(matrix: Sequence[float]) -> tuple[float, float, float]:
    """Inverse of :func:`to_exstar_matrix`; rejects a non-diagonal matrix."""
    m = np.asarray(list(matrix), dtype=np.float64).reshape(3, 3)
    if np.any(np.abs(m - np.diag(np.diag(m))) > 1e-6):
        raise ValueError("matrix is not diagonal; this layer models diagonal gains only")
    d = np.diag(m)
    return (float(d[2]), float(d[1]), float(d[0]))


def apply_gains(bgr, gains_bgr: Sequence[float]) -> np.ndarray:
    """Multiply a BGR array by diagonal gains, **without** clipping.

    No clipping on purpose: the operation must be exactly invertible by
    :func:`revert_gains` so a photometric profile can never destroy data.  A
    display path may clip afterwards.
    """
    a = np.asarray(bgr, dtype=np.float64)
    if a.shape[-1] != 3:
        raise ValueError(f"expected a BGR array with 3 channels, got {a.shape}")
    return a * np.asarray(gains_bgr, dtype=np.float64)[None, None, :]


def revert_gains(bgr, gains_bgr: Sequence[float]) -> np.ndarray:
    """Exact inverse of :func:`apply_gains`."""
    g = np.asarray(gains_bgr, dtype=np.float64)
    if np.any(g == 0.0):
        raise ValueError("cannot revert a zero gain")
    return np.asarray(bgr, dtype=np.float64) / g[None, None, :]


def correct(bgr, gains_bgr: Sequence[float], dark_current=(0.0, 0.0, 0.0)
            ) -> np.ndarray:
    """The full photometric chain: subtract dark, then apply the gains.

    The gains are *solved* on dark-subtracted data (that is what the recovered
    estimator does), so applying them to raw 8-bit values over-corrects by the
    pedestal: measured on the EXStar frames that is a **+11 % to +15 %** residual
    cast.  Any implementation that applies the gains must subtract the same dark
    current first, which is why the operation lives in one function.
    """
    d = np.asarray(dark_current, dtype=np.float64) * 255.0
    return apply_gains(np.asarray(bgr, dtype=np.float64) - d[None, None, :],
                       gains_bgr)


# ---------------------------------------------------------------------------
# colour-cast metric (same definition as tools/scan/color.py)
# ---------------------------------------------------------------------------
def neutrality(bgr, *, percentile: float = 95.0, mask=None) -> dict:
    """Channel ratios of the highlight set — the metric used in the colour doc.

    ``r_over_g`` and ``b_over_g`` near 1.0 mean neutral; ``max_cast`` is the
    larger absolute deviation.  ``mask`` restricts the pixels considered (a
    plate region), which is what makes the number meaningful on a scanner frame.
    Here ``b``/``r`` are the engine's slot labels (slot 0 / slot 2).
    """
    a = np.asarray(bgr, dtype=np.float64)
    if a.ndim != 3 or a.shape[-1] != 3:
        raise ValueError(f"expected a BGR array with 3 channels, got {a.shape}")
    flat = a.reshape(-1, 3)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        if m.shape != a.shape[:2]:
            raise ValueError(f"mask shape {m.shape} != image {a.shape[:2]}")
        flat = flat[m.reshape(-1)]
    if len(flat) == 0:
        raise ValueError("no pixels selected")
    lum = flat.mean(axis=1)
    ref = flat[lum >= float(np.percentile(lum, float(percentile)))]
    if len(ref) == 0:                                     # pragma: no cover
        ref = flat
    mean = np.maximum(ref.mean(axis=0), 1e-12)
    slot0, g, slot2 = mean
    r_over_g = float(slot2 / g)
    b_over_g = float(slot0 / g)
    return {
        "r_over_g": round(r_over_g, 6),
        "b_over_g": round(b_over_g, 6),
        "max_cast": round(max(abs(r_over_g - 1.0), abs(b_over_g - 1.0)), 6),
        "n_reference": int(len(ref)),
        "percentile": float(percentile),
        "mean_slots": [round(float(v), 6) for v in mean],
    }


def mask_means(bgr, mask) -> dict:
    """Per-slot means over ``mask``, with the same ratio metric as :func:`neutrality`.

    Used for before/after comparisons on a fixed pixel set: unlike the p95
    highlight metric it cannot silently migrate to a different population when
    the gains change, so a change in the number is a change in the colour.
    """
    a = np.asarray(bgr, dtype=np.float64)
    m = np.asarray(mask, dtype=bool)
    if m.shape != a.shape[:2]:
        raise ValueError(f"mask shape {m.shape} != image {a.shape[:2]}")
    if not m.any():
        raise ValueError("no pixels selected")
    mean = np.maximum(a[m].mean(axis=0), 1e-12)
    slot0, g, slot2 = mean
    return {
        "r_over_g": round(float(slot2 / g), 6),
        "b_over_g": round(float(slot0 / g), 6),
        "max_cast": round(max(abs(float(slot2 / g) - 1.0),
                              abs(float(slot0 / g) - 1.0)), 6),
        "n_pixels": int(m.sum()),
        "mean_slots": [round(float(v), 6) for v in mean],
    }


def clipped_mask(planes: PhasePlanes, clip_level: float = 0.95) -> np.ndarray:
    """Pixels whose brightest slot exceeds ``clip_level`` — no colour to measure."""
    lim = float(clip_level) * 255.0
    return (planes.p00 > lim) | (planes.g > lim) | (planes.p11 > lim)


def plate_mask(planes: PhasePlanes, percentile: float = 90.0, *,
               clip_level: float = 0.95,
               exclude_window: Optional[tuple[int, int, int, int]] = None
               ) -> np.ndarray:
    """'The lit plate' mask: green above a percentile, unclipped, minus the window."""
    mask = planes.g >= float(np.percentile(planes.g, float(percentile)))
    mask = mask & ~clipped_mask(planes, clip_level)
    if exclude_window is not None:
        r0, c0, rows, cols = exclude_window
        keep = np.ones_like(mask)
        keep[r0:r0 + rows, c0:c0 + cols] = False
        mask = mask & keep
    return mask


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_profile(estimates: Mapping[str, WbEstimate], *,
                  estimator: EstimatorConfig,
                  source: Mapping,
                  holdout: Optional[Mapping[str, Mapping]] = None,
                  capture_settings: Optional[Mapping] = None,
                  notes: Sequence[str] = (),
                  created_utc: Optional[str] = None) -> dict:
    """Assemble the versioned photometric profile from one or more conditions.

    ``estimates`` maps a condition name (``led_on`` / ``led_off``) to its
    estimate.  The profile is never activated and carries no geometric
    reference: it is a separate layer by construction.
    """
    conditions: dict[str, dict] = {}
    reasons: list[str] = []
    for name, est in estimates.items():
        rec = est.as_dict()
        if holdout and name in holdout:
            rec["holdout"] = dict(holdout[name])
        conditions[str(name)] = rec
        reasons.extend(f"{name}: {r}" for r in est.reasons)

    refused = any(e.refused for e in estimates.values())
    hold = [c["holdout"] for c in conditions.values() if c.get("holdout")]
    worst = max((float(h.get("max_cast", 1.0)) for h in hold), default=None)
    if refused:
        status = "refused"
    elif worst is not None and worst <= estimator.max_holdout_cast:
        status = "validated"
    else:
        status = "estimated"

    profile = {
        "schema": PROFILE_SCHEMA,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "layer": LAYER,
        "channel_order": CHANNEL_ORDER,
        "slot0_phase": SLOT0_PHASE,
        "exstar_slot0_phase": EXSTAR_SLOT0_PHASE,
        "estimator": {
            "id": ESTIMATOR_ID,
            "description": ("grey-world over the centred window, each channel "
                            "normalised to green after dark subtraction, "
                            "clipped pixels rejected"),
            "config": estimator.as_dict(),
        },
        "geometry_profile_digest": None,
        "status": status,
        "activation": {
            "activated": False,
            "auto_activate": False,
            "note": ("photometric layer; never applied implicitly and never "
                     "written into a factory profile. Activation is a separate, "
                     "explicit, human decision."),
        },
        "conditions": conditions,
        "refusal_reasons": reasons,
        "holdout_worst_max_cast": worst,
        "capture_settings": dict(capture_settings or {}),
        "source": dict(source),
        "notes": list(notes),
        "created_utc": created_utc or _now(),
    }
    profile["digest"] = profile_digest(profile)
    return profile


def profile_digest(profile: Mapping) -> str:
    """SHA-256 over the profile with the digest field itself removed."""
    body = {k: v for k, v in profile.items() if k != "digest"}
    return _sha256_bytes(json.dumps(body, sort_keys=True,
                                    separators=(",", ":")).encode())


def _is_factory_path(path: Path) -> bool:
    s = str(path).replace("\\", "/").lower()
    return any(marker.replace("\\", "/") in s for marker in _FACTORY_MARKERS)


def write_profile(profile: Mapping, path: str | Path, *,
                  overwrite: bool = False) -> Path:
    """Write a profile as JSON.  Refuses factory paths and re-checks the layer."""
    p = Path(path)
    if _is_factory_path(p):
        raise RefusedCalibration(
            f"refusing to write a photometric profile into a factory path: {p}")
    if profile.get("layer") != LAYER:
        raise RefusedCalibration(f"not a {LAYER!r} profile: {profile.get('layer')!r}")
    if profile.get("activation", {}).get("activated"):
        raise RefusedCalibration("refusing to write an activated profile")
    if p.exists() and not overwrite:
        raise FileExistsError(f"profile already exists: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    return p


def load_profile(path: str | Path) -> dict:
    """Load and validate a photometric profile; never returns an activated one."""
    p = Path(path)
    profile = json.loads(p.read_text())
    if profile.get("schema") != PROFILE_SCHEMA:
        raise ValueError(f"not a {PROFILE_SCHEMA} profile: {profile.get('schema')!r}")
    if int(profile.get("schema_version", -1)) != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version "
                         f"{profile.get('schema_version')!r}")
    if profile.get("layer") != LAYER:
        raise ValueError(f"wrong layer {profile.get('layer')!r}; expected {LAYER!r}")
    if profile.get("channel_order") != CHANNEL_ORDER:
        raise ValueError(f"channel_order {profile.get('channel_order')!r} != "
                         f"{CHANNEL_ORDER!r}; refusing a possible red/blue swap")
    if profile.get("slot0_phase") != SLOT0_PHASE:
        raise ValueError(f"slot0_phase {profile.get('slot0_phase')!r} != "
                         f"{SLOT0_PHASE!r}")
    if profile.get("activation", {}).get("activated"):
        raise ValueError("profile is marked activated; this layer never activates")
    return profile


def validate_profile(profile: Mapping, frames: Mapping[str, object], *,
                     percentile: float = 90.0) -> dict:
    """Independent re-measurement of a profile against frames.

    For each condition the frame is measured again on two disjoint pixel sets —
    the estimation window and the bright plate region *outside* it — before and
    after applying the stored gains.  Both sets exclude saturated pixels.

    The window result is a **consistency check** (grey-world makes it neutral by
    construction, so a large residual means the profile was applied in the wrong
    slot order).  The outside result is the informative one: it is how much cast
    a single diagonal gain leaves across the field.
    """
    cfg = EstimatorConfig.from_dict(
        (profile.get("estimator") or {}).get("config") or {})
    gains_by_condition = {}
    for name, rec in (profile.get("conditions") or {}).items():
        if rec.get("refused") or rec.get("gains_bgr") is None:
            continue
        gains_by_condition[name] = tuple(float(v) for v in rec["gains_bgr"])

    report: dict = {"conditions": {}, "status": profile.get("status"),
                    "max_cast_limit": cfg.max_holdout_cast}
    worst = 0.0
    for name, mosaic in frames.items():
        if name not in gains_by_condition:
            continue
        planes = bayer_phase_planes(mosaic)
        bgr = planes.as_bgr()
        r0, c0, rows, cols = window_bounds(planes.shape, cfg.window_divisor)
        win_mask = np.zeros(planes.shape, dtype=bool)
        win_mask[r0:r0 + rows, c0:c0 + cols] = True
        win_mask &= ~clipped_mask(planes, cfg.clip_level)
        out_mask = plate_mask(planes, percentile, clip_level=cfg.clip_level,
                              exclude_window=(r0, c0, rows, cols))
        if not out_mask.any():
            out_mask = ~win_mask & ~clipped_mask(planes, cfg.clip_level)
        gains = gains_by_condition[name]
        after = correct(bgr, gains, cfg.dark_current)
        rec = {
            "gains_bgr": [round(g, 9) for g in gains],
            "window_before": mask_means(bgr, win_mask),
            "window_after": mask_means(after, win_mask),
            "outside_before": mask_means(bgr, out_mask),
            "outside_after": mask_means(after, out_mask),
            "outside_pixels": int(out_mask.sum()),
        }
        worst = max(worst, float(rec["outside_after"]["max_cast"]))
        report["conditions"][name] = rec
    report["outside_worst_after"] = round(worst, 6)
    # a report with nothing measurable never passes
    report["passes"] = bool(report["conditions"]) and bool(
        worst <= float(cfg.max_holdout_cast))
    return report


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def _read_mosaic(path: str | Path) -> np.ndarray:
    import cv2  # local import: the analysis side is the only cv2 consumer

    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"cannot read {path}")
    if img.ndim != 2:
        raise SystemExit(f"{path} is not a single-channel mosaic (shape {img.shape})")
    return img


def _sha256_file(path: str | Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _holdout_for(mosaic, cfg: EstimatorConfig, gains: Sequence[float]) -> Optional[dict]:
    planes = bayer_phase_planes(mosaic)
    bgr = planes.as_bgr()
    r0, c0, rows, cols = window_bounds(planes.shape, cfg.window_divisor)
    mask = plate_mask(planes, 90.0, clip_level=cfg.clip_level,
                      exclude_window=(r0, c0, rows, cols))
    if not mask.any():
        return None
    before = mask_means(bgr - np.asarray(cfg.dark_current) * 255.0, mask)
    after = mask_means(correct(bgr, gains, cfg.dark_current), mask)
    return {"outside_before": before, "outside_after": after,
            "max_cast": after["max_cast"], "pixels": int(mask.sum())}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m calib.white_balance`` — estimate / show / validate / apply."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m calib.white_balance",
        description="Photometric (white-balance) calibration layer. "
                    "Never activates itself and never writes factory paths.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    est = sub.add_parser("estimate", help="estimate gains from wb frames")
    est.add_argument("--led-on", required=True, help="raw mosaic, illuminator on")
    est.add_argument("--led-off", required=True, help="raw mosaic, illuminator off")
    est.add_argument("--out", required=True, help="profile JSON to write")
    est.add_argument("--divisor", type=int, default=8)
    est.add_argument("--clip-level", type=float, default=0.95)
    est.add_argument("--dark", type=float, nargs=3, default=(0.0, 0.0, 0.0),
                     metavar=("S0", "G", "S2"),
                     help="dark current, fractions of full scale, slot order")
    est.add_argument("--no-dark", action="store_true")
    est.add_argument("--intervention", default="",
                     help="what was done to the scene (recorded verbatim)")
    est.add_argument("--overwrite", action="store_true")

    show = sub.add_parser("show", help="summarise a profile")
    show.add_argument("profile")

    val = sub.add_parser("validate", help="re-measure a profile on frames")
    val.add_argument("profile")
    val.add_argument("--led-on", required=True)
    val.add_argument("--led-off", required=True)

    app = sub.add_parser("apply", help="apply stored gains to a mosaic (writes PNG)")
    app.add_argument("profile")
    app.add_argument("--condition", default="led_on")
    app.add_argument("--image", required=True)
    app.add_argument("--out", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "estimate":
        cfg = EstimatorConfig(window_divisor=args.divisor,
                              clip_level=args.clip_level,
                              subtract_dark=not args.no_dark,
                              dark_current=tuple(float(v) for v in args.dark))
        frames = {"led_on": _read_mosaic(args.led_on),
                  "led_off": _read_mosaic(args.led_off)}
        ests = estimate_pair(frames["led_on"], frames["led_off"], cfg)
        holdout = {}
        for name, mosaic in frames.items():
            gains = ests[name].gains
            if not ests[name].refused:
                h = _holdout_for(mosaic, cfg, gains)
                if h is not None:
                    holdout[name] = h
        source = {
            "frames": {k: {"path": str(p), "sha256": _sha256_file(p)}
                       for k, p in (("led_on", args.led_on),
                                    ("led_off", args.led_off))},
            "intervention": args.intervention,
        }
        profile = build_profile(ests, estimator=cfg, source=source,
                                holdout=holdout,
                                capture_settings={"illuminator_route": 1})
        write_profile(profile, args.out, overwrite=args.overwrite)
        print(f"status   : {profile['status']}")
        for name, rec in profile["conditions"].items():
            if rec["refused"]:
                print(f"{name:9s}: REFUSED {rec['reasons']}")
            else:
                print(f"{name:9s}: slot0={rec['gains_bgr'][0]:.5f} "
                      f"slot2={rec['gains_bgr'][2]:.5f}  "
                      f"exstar={[round(v, 5) for v in rec['exstar_matrix']]}")
        if profile["refusal_reasons"]:
            print("reasons  : " + "; ".join(profile["refusal_reasons"]))
        print(f"profile  : {args.out}")
        return 0

    if args.cmd == "show":
        p = load_profile(args.profile)
        print(f"schema   : {p['schema']} v{p['schema_version']} ({p['layer']})")
        print(f"status   : {p['status']}")
        print(f"activated: {p['activation']['activated']}")
        print(f"digest   : {p['digest']}")
        for name, rec in p["conditions"].items():
            print(f"{name:9s}: {'REFUSED' if rec['refused'] else rec['gains_bgr']}")
        return 0

    if args.cmd == "validate":
        p = load_profile(args.profile)
        frames = {"led_on": _read_mosaic(args.led_on),
                  "led_off": _read_mosaic(args.led_off)}
        rep = validate_profile(p, frames)
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0 if rep["passes"] else 1

    if args.cmd == "apply":
        import cv2

        p = load_profile(args.profile)
        rec = (p.get("conditions") or {}).get(args.condition)
        if rec is None or rec.get("refused"):
            raise SystemExit(f"condition {args.condition!r} has no usable gains")
        mosaic = _read_mosaic(args.image)
        cfg = EstimatorConfig.from_dict(
            (p.get("estimator") or {}).get("config") or {})
        out = correct(bayer_phase_planes(mosaic).as_bgr(), rec["gains_bgr"],
                      cfg.dark_current)
        cv2.imwrite(str(args.out), np.clip(out, 0.0, 255.0).astype(np.uint8))
        print(f"wrote {args.out} (condition {args.condition})")
        return 0
    return 2                                              # pragma: no cover


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
