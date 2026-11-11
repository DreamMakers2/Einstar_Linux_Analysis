"""Colour post-processing for scanned points and meshes.

Measured on real frames, the colour camera delivers a strong green cast because
its raw Bayer output is used without white-balance gains: the brightest 5 % of
pixels (the white plate background) sit at **R/G 0.72, B/G 0.65**.  The cast is
stable across frames and sessions, so it is a sensor/processing property rather
than the scene.

The correction here is deliberately simple and auditable:

* estimate one gain per channel so that the *brightest* pixels of the sampled
  colour set become neutral (grey-world on the highlight, which is where a
  scanner's white reference normally is);
* clamp the gains, apply them, and report both the estimated gains and the
  neutrality before/after.

This removes a cast relative to an assumed-neutral highlight.  It is **not**
colorimetric calibration: without a colour chart or a measured white reference
it cannot claim absolute colour accuracy, and the numbers say so.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["WhiteBalance", "estimate_white_balance", "apply_white_balance",
           "neutrality"]

#: channels at or above this percentile of luminance form the reference set
HIGHLIGHT_PERCENTILE = 95.0
#: gains are clamped so a pathological frame cannot blow the image up
MIN_GAIN, MAX_GAIN = 0.25, 4.0


@dataclass
class WhiteBalance:
    gains: tuple[float, float, float]      # R, G, B multipliers
    reference_rgb: tuple[float, float, float]
    n_reference: int
    percentile: float = HIGHLIGHT_PERCENTILE

    def as_dict(self) -> dict:
        return {"gains": [round(float(g), 4) for g in self.gains],
                "reference_rgb": [round(float(v), 2) for v in self.reference_rgb],
                "n_reference": int(self.n_reference),
                "percentile": float(self.percentile)}


def _as_rgb(rgb) -> np.ndarray:
    a = np.asarray(rgb, float).reshape(-1, 3)
    if a.size and a.max() <= 1.5:      # normalised input -> 0..255
        a = a * 255.0
    return a


def estimate_white_balance(rgb, *, percentile: float = HIGHLIGHT_PERCENTILE,
                           min_pixels: int = 64) -> WhiteBalance:
    """Grey-world gains from the brightest ``percentile`` of the pixels."""
    a = _as_rgb(rgb)
    if len(a) < min_pixels:
        raise ValueError(f"need at least {min_pixels} samples, got {len(a)}")
    lum = a.mean(axis=1)
    threshold = float(np.percentile(lum, float(percentile)))
    ref = a[lum >= threshold]
    if len(ref) == 0:  # pragma: no cover - percentile guarantees something
        ref = a
    mean = np.maximum(ref.mean(axis=0), 1e-6)
    target = float(mean.mean())
    gains = np.clip(target / mean, MIN_GAIN, MAX_GAIN)
    return WhiteBalance(tuple(float(g) for g in gains),
                        tuple(float(v) for v in mean), int(len(ref)),
                        float(percentile))


def apply_white_balance(rgb, wb: WhiteBalance) -> np.ndarray:
    """Apply gains, keeping the result in the input's numeric range."""
    a = np.asarray(rgb, float).copy()
    normalised = a.size and float(np.nanmax(a)) <= 1.5
    if normalised:
        a = a * 255.0
    a = a.reshape(-1, 3) * np.asarray(wb.gains, float)[None, :]
    a = np.clip(a, 0.0, 255.0)
    out = (a / 255.0) if normalised else a
    return out.reshape(np.shape(rgb))


def neutrality(rgb, *, percentile: float = HIGHLIGHT_PERCENTILE) -> dict:
    """Channel ratios of the highlight set; 1.0 on both means neutral."""
    a = _as_rgb(rgb)
    lum = a.mean(axis=1)
    ref = a[lum >= float(np.percentile(lum, percentile))]
    mean = np.maximum(ref.mean(axis=0), 1e-6)
    g = max(float(mean[1]), 1e-6)
    return {"r_over_g": float(mean[0] / g), "b_over_g": float(mean[2] / g),
            "max_cast": float(max(abs(mean[0] / g - 1.0),
                                  abs(mean[2] / g - 1.0))),
            "n_reference": int(len(ref))}
