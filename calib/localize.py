"""Sub-pixel localisation of the Einstar calibration-plate markers.

This module is the *refinement* stage of the detector: it takes a marker seed
(``x``, ``y``, expected radius ``r``) or a complete grid assignment
(:class:`calib.detect.GridObservation`) and returns sub-pixel marker centres
with a per-marker uncertainty, a quality score and an explicit failure reason.

It is deliberately independent of :mod:`calib.detect` (no import cycle, no
state) so it can be used by the offline calibration, by the live guidance loop
and by the audits.  The only compatibility requirement is that the
``centroid`` method reproduces :func:`calib.detect._weighted_centroid` exactly
for an integer seed (asserted in ``tests/test_localize.py``).

Marker photometry (measured on the 75-image vendor corpus)
----------------------------------------------------------
The IR/Tex frames show bright, **saturated** (255 DN) round markers on a
40-80 DN background; the 50 %-level silhouette radius is 3.3-7.1 px
(median 4.6 px at 240-610 mm), i.e. only ~9 px across, so every localiser
works with 20-40 usable edge samples.  Both polarities occur (a normal
photograph of the white plate has dark holes), and the large markers are
~1.7x the small radius.  See ``analysis/calibration/audit/detector_report.md``
for the corpus statistics.

Methods
-------
``centroid``
    The current production refinement: intensity-weighted centroid in a
    square window after a border-median background, ``max(gray-bg, 0)``
    weights.  Kept bit-identical to :func:`calib.detect._weighted_centroid`.
``centroid_bg``
    Same idea but (a) the background is a *local plane* fitted robustly to an
    annulus around the marker instead of a border median, (b) the integration
    window is circular (``rho < 1.35 r``) so a neighbouring marker or a plate
    edge cannot pull it, and (c) saturated pixels are kept (a flat top is
    symmetric) but flagged.  This removes the illumination-gradient and
    neighbour bias of ``centroid`` at the same cost.
``ellipse``
    Adaptive 50 % threshold -> contour -> ``cv2.fitEllipse`` on the contour
    point set.  This is the *legacy* outline centre: it is fitted in
    distorted pixel coordinates and its threshold depends on the (saturated)
    peak, so it is the least accurate method; kept for comparison and for
    frames with no usable gradient.
``gradient``
    Orthogonal-gradient edge fit at a circle model: for ``n_rays`` directions
    from the seed, the radial intensity derivative is sampled at 0.25 px and
    its extremum is refined by a parabola; the resulting sub-pixel edge
    points are fitted with a robust circle (Kasa + IRLS).  No Canny, no
    blur, ~0.1 ms/marker.  Cheapest edge-based method.
``conic``
    Gaussian blur -> adaptive Canny -> parabolic sub-pixel edge refinement
    along the gradient normal -> robust direct conic fit (Halir-Flusser)
    **in undistorted normalised camera coordinates** -> algebraic centre,
    projected back to pixels.  Fitting after undistortion removes the
    distortion-induced centre bias (worth 0.05-0.15 px at 240-320 mm).
``conic_perspective``
    ``conic`` plus the perspective-eccentricity correction: the fitted conic
    is back-projected onto the marker plane (its normal comes from the local
    homography, or from a supplied pose), a 3-D circle is fitted in that
    plane and the projection of its *centre* is returned.  The projection of
    a 3-D circle is an ellipse whose centre is displaced from the projection
    of the circle centre by ``~ (f/2)(r/d)^2 sin(t) cos(t)`` (0.004 px on the
    vendor corpus at 450 mm, 0.05-0.2 px at 240 mm / large markers); the
    correction is exact to the conic-fit noise.  It also reports the
    correction vector and a *circularity* residual that validates the plane
    normal.
``auto``
    Per-marker selection/fusion.  The cheap ``centroid_bg`` estimate and the
    best available edge estimate (``conic_perspective`` -> ``conic`` ->
    ``gradient``) are compared; they are fused by inverse variance when they
    agree within ``max(agree_px, k*sigma)`` and the edge estimate is chosen
    (with a reduced quality) when they do not.  Falls back to
    ``centroid_bg``/``centroid`` on low contrast, and always reports why.

Uncertainty
-----------
``sigma_px`` is a *predicted* 1-sigma radial centre error, from
(a) centroid: first-order noise propagation through the intensity weights
plus the measured background noise plus a 0.03 px systematic floor,
(b) edge fits: the orthogonal residual RMS of the fit divided by
``sqrt(n_eff)`` with a shape factor.  It was validated against split-half
repeats on 2786 real markers and against synthetic Monte Carlo: reported /
actual random error is 0.91 for ``conic``, 0.73 for ``ellipse``, 2.36 for
``gradient`` (conservative) and 0.61-1.27 for the fused/centroid variants
(``agents/localize/report.md`` section 3.5).  Use it as a BA weight; treat
the centroid-family value as an order of magnitude only.

Every returned dict contains at least::

    {"u", "v", "sigma_px", "method", "quality", "n_edges", "ok", "reason"}

plus diagnostics (``polarity``, ``radius_px``, ``contrast``, ``saturated``,
``partial``, ``coverage``, ``ellipse``, ``plane``, ``correction``,
``runtime_ms`` ...).  ``u, v`` are pixel coordinates in the input image
(``x`` right, ``y`` down), the same convention as :mod:`calib.detect`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import warnings
from typing import Any, Iterable, Mapping, Optional, Sequence

import cv2
import numpy as np

__all__ = [
    "METHODS", "SIGMA_CALIBRATION", "GridRefinement",
    "localize_marker", "refine_grid", "refine_observation",
    "plane_normal_from_homography", "backproject_circle_centre",
]

#: methods accepted by :func:`localize_marker`
METHODS = ("centroid", "centroid_bg", "ellipse", "gradient", "conic",
           "conic_perspective", "auto")

#: multiplicative calibration of the analytic sigma (see report.md)
SIGMA_CALIBRATION = 1.0

#: systematic floor of the centroid-family position on real images (px):
#: measured as the centroid-vs-conic_perspective offset over the vendor corpus
#: (0.02-0.12 px depending on image radius; 0.03 is a conservative single
#: number).  Added in quadrature so that ``auto`` does not let a tiny random
#: sigma outvote the edge estimate.
_CENTROID_SYSTEMATIC_FLOOR = 0.03

#: pixel-discretisation contribution to the centroid repeatability, in pixels
#: at a 4 px marker radius (measured on the synthetic Monte Carlo: the
#: continuous-centroid CRLB is ~0.002 px, the observed floor is ~0.006 px
#: because a disc rasterised on the pixel grid is not an exact disc).
_PIXELATION_FLOOR = 0.006

#: marker silhouette radius / marker pitch on the Einstar plate.  Measured on
#: the vendor corpus: r = 1.77 mm (median) at a 27.72-28 mm pitch; used only
#: to seed the radius search in :func:`refine_grid`.  INFERRED from the
#: established pitch (see calib.plate), not a physical measurement.
RADIUS_OVER_PITCH = 0.064

_TWO_PI = 2.0 * math.pi
_SQRT2 = math.sqrt(2.0)


# ==========================================================================
# small numeric helpers
# ==========================================================================

def _as_gray(gray: np.ndarray) -> np.ndarray:
    """Return a 2-D grayscale array without copying when possible.

    Keeping the input dtype matters for speed: converting a 1280x1024 uint8
    frame to float64 costs ~2 ms, which would dominate a 41-marker refinement.
    All arithmetic below promotes to float64 per *patch*, not per frame.
    """
    g = np.asarray(gray)
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY)
    if g.ndim != 2:
        raise ValueError(f"gray must be 2-D, got shape {g.shape}")
    return g


def _bilinear(img: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Bilinear sample; returns NaN where the coordinate is out of bounds."""
    h, w = img.shape
    xs = np.asarray(xs, np.float64)
    ys = np.asarray(ys, np.float64)
    inside = (xs >= 0.0) & (xs <= w - 1.0) & (ys >= 0.0) & (ys <= h - 1.0)
    xc = np.clip(xs, 0.0, w - 1.0)
    yc = np.clip(ys, 0.0, h - 1.0)
    x0 = np.floor(xc).astype(np.int32)
    y0 = np.floor(yc).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = xc - x0
    fy = yc - y0
    out = ((img[y0, x0] * (1.0 - fx) + img[y0, x1] * fx) * (1.0 - fy)
           + (img[y1, x0] * (1.0 - fx) + img[y1, x1] * fx) * fy)
    return np.where(inside, out, np.nan)


def _patch_bounds(shape: tuple[int, int], x: float, y: float, r: float,
                  pad: int = 2) -> tuple[int, int, int, int]:
    h, w = shape
    r = max(float(r), 1.0)
    x0 = max(0, int(math.floor(x - r)) - pad)
    x1 = min(w, int(math.ceil(x + r)) + pad + 1)
    y0 = max(0, int(math.floor(y - r)) - pad)
    y1 = min(h, int(math.ceil(y + r)) + pad + 1)
    return x0, x1, y0, y1


def _clip01(v: float) -> float:
    return float(min(1.0, max(0.0, v)))


def _median(v: np.ndarray) -> float:
    v = np.asarray(v, np.float64)
    v = v[np.isfinite(v)]
    return float(np.median(v)) if v.size else float("nan")


# ==========================================================================
# marker probe: polarity, contrast, radius, saturation, blur width
# ==========================================================================

@dataclass
class _Probe:
    polarity: int          # +1 bright marker on dark background, -1 dark on bright
    inner: float           # median intensity inside the marker
    outer: float           # background level (outer part of the scan)
    contrast: float        # |inner - outer| in DN
    radius: float          # 50 %-level silhouette radius (px)
    radius_ok: bool
    edge_width: float      # 10-90 % transition width (px), a blur proxy
    saturated: bool        # marker core is at the sensor maximum
    sat_frac: float
    n_rays: int
    valid_frac: float
    rmse: float            # residual of the profile vs its median (noise proxy)
    profile: np.ndarray = field(default=None, repr=False)
    rho: np.ndarray = field(default=None, repr=False)

    @property
    def sign(self) -> float:
        return float(self.polarity)


def _probe_marker(gray: np.ndarray, x: float, y: float, r_hint: float,
                  n_ang: int = 24, ds: float = 0.3,
                  max_scale: float = 2.5, invert: Optional[bool] = None) -> _Probe:
    """Radial profile probe: polarity, contrast, edge radius, saturation.

    ``r_hint`` is a *lower bound* on the marker radius (the scan covers up to
    ``max_scale * r_hint`` so a large marker probed with the small-marker hint
    is still measured).  The background is the outermost 15 % of the scan,
    the inside is the median of ``rho < 0.5 r_hint``.
    """
    h, w = gray.shape
    r_hint = max(float(r_hint), 1.5)
    rmax = max_scale * r_hint
    rho = np.arange(0.0, rmax + 0.5 * ds, ds)
    th = np.arange(n_ang, dtype=np.float64) * (_TWO_PI / n_ang)
    xs = x + np.cos(th)[:, None] * rho[None, :]
    ys = y + np.sin(th)[:, None] * rho[None, :]
    I = _bilinear(gray, xs, ys)
    valid = np.isfinite(I)
    if not valid.any():
        return _Probe(+1, float("nan"), float("nan"), 0.0, r_hint, False, 0.0,
                      False, 0.0, 0, 0.0, float("nan"), None, rho)
    if valid.all():
        prof = np.median(I, axis=0)          # common case: fully inside
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            prof = np.nanmedian(np.where(valid, I, np.nan), axis=0)
    ok_r = np.isfinite(prof)
    if ok_r.sum() < 6:
        return _Probe(+1, float("nan"), float("nan"), 0.0, r_hint, False, 0.0,
                      False, 0.0, 0, 0.0, float("nan"), prof, rho)
    # fill gaps in the profile by linear interpolation over valid rho
    if not ok_r.all():
        prof = np.interp(rho, rho[ok_r], prof[ok_r])
    inner = _median(prof[rho <= max(0.5 * r_hint, 1.0)])
    outer = _median(prof[rho >= 0.85 * rho[-1]])
    contrast = inner - outer
    polarity = +1 if contrast >= 0 else -1
    if invert is not None:
        polarity = -1 if invert else +1
        if np.isfinite(contrast) and np.sign(contrast) != polarity and abs(contrast) > 6:
            # caller's polarity contradicts the data; trust the data but note it
            polarity = +1 if contrast >= 0 else -1
    amp = abs(contrast)
    mid = 0.5 * (inner + outer)
    # 50 % crossing, scanning outward from the seed; a crossing is only
    # accepted when the profile *stays* on the far side (median of the next
    # samples), which rejects noise wiggles inside a saturated flat top
    s = polarity * (prof - mid)
    cand = np.where((s[:-1] > 0) & (s[1:] <= 0))[0]
    cross = []
    for c in cand:
        nxt = prof[c + 1:min(c + 4, len(prof))]
        if nxt.size and _median(polarity * (nxt - mid)) <= 0.0:
            cross.append(int(c))
    cross = np.array(cross, dtype=int)
    radius, radius_ok = r_hint, False
    edge_width = float("nan")
    if not cross.size and max_scale < 8.0:
        # the marker is larger than the scan (close range / large markers):
        # retry once with a wider scan instead of silently clipping the window
        return _probe_marker(gray, x, y, r_hint * 2.5, n_ang=n_ang, ds=ds,
                             max_scale=max_scale * 1.6, invert=invert)
    if cross.size:
        j = int(cross[0])
        denom = prof[j] - prof[j + 1]
        frac = 0.5 if abs(denom) < 1e-9 else (prof[j] - mid) / denom
        radius = float(rho[j] + ds * min(max(frac, 0.0), 1.0))
        radius_ok = 0.6 * r_hint <= radius <= 0.98 * rho[-1]
        # 10-90 % transition width (blur proxy), measured on the signed
        # profile: 0 % = background, 100 % = marker interior
        lvl10 = polarity * (outer + 0.1 * amp * polarity)
        lvl90 = polarity * (outer + 0.9 * amp * polarity)
        j10 = j90 = None
        for k in range(j, -1, -1):
            if polarity * prof[k] >= lvl90:
                j90 = k
                break
        for k in range(j, len(prof)):
            if polarity * prof[k] <= lvl10:
                j10 = k
                break
        if j10 is not None and j90 is not None:
            edge_width = abs(float(rho[j10] - rho[j90]))
    # saturation: sensor maximum in the marker core (no full-frame scan for
    # the usual uint8 frames)
    if gray.dtype == np.uint8:
        sat_thr = 254.5
    else:
        sat_thr = float(np.nanmax(gray)) - 0.5
    core_vals = I[:, rho <= max(0.8 * radius, 1.0)]
    finite_core = core_vals[np.isfinite(core_vals)]
    sat_frac = float(np.mean(finite_core >= sat_thr)) if finite_core.size else 0.0
    saturated = sat_frac > 0.25
    rmse = float(np.sqrt(np.nanmean((I - prof[None, :]) ** 2))) if valid.any() else float("nan")
    return _Probe(polarity, inner, outer, float(contrast), radius, radius_ok,
                  float(edge_width), saturated, sat_frac, n_ang,
                  float(np.mean(valid[np.isfinite(valid)]) if valid.any() else 0.0),
                  rmse, prof, rho)


# ==========================================================================
# centroid family
# ==========================================================================

def _weighted_centroid_compat(gray: np.ndarray, x: int, y: int, r: int,
                              invert: bool) -> tuple[float, float]:
    """Bit-identical copy of :func:`calib.detect._weighted_centroid`."""
    h, w = gray.shape
    x0, x1 = max(0, x - r), min(w, x + r + 1)
    y0, y1 = max(0, y - r), min(h, y + r + 1)
    patch = gray[y0:y1, x0:x1].astype(np.float64)
    if patch.ndim != 2 or min(patch.shape) < 2:
        return float(x), float(y)
    if invert:
        patch = 255.0 - patch
    border = np.concatenate([patch[0, :], patch[-1, :], patch[:, 0], patch[:, -1]])
    bg = float(np.median(border))
    wt = np.clip(patch - bg, 0.0, None)
    s = wt.sum()
    if s <= 1e-9:
        return float(x), float(y)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    return float((wt * xs).sum() / s), float((wt * ys).sum() / s)


def _plane_background(gray: np.ndarray, x: float, y: float, r_in: float,
                      r_out: float, n_ang: int = 24, ds: float = 0.5,
                      n_iter: int = 2):
    """Robust local plane ``b0 + bx*dx + by*dy`` from an annulus.

    Returns ``(coeff, sigma_bg)`` or ``(None, nan)`` when the annulus has too
    few valid samples.
    """
    h, w = gray.shape
    r_in = max(r_in, 1.5)
    r_out = max(r_out, r_in + 1.0)
    rho = np.arange(r_in, r_out + 0.5 * ds, ds)
    th = np.arange(n_ang, dtype=np.float64) * (_TWO_PI / n_ang)
    xs = x + np.cos(th)[:, None] * rho[None, :]
    ys = y + np.sin(th)[:, None] * rho[None, :]
    I = _bilinear(gray, xs, ys)
    m = np.isfinite(I)
    if m.sum() < 12:
        return None, float("nan")
    dx = (xs - x)[m]
    dy = (ys - y)[m]
    v = I[m]
    A = np.column_stack([np.ones_like(dx), dx, dy])
    wgt = np.ones_like(v)
    coef = None
    for _ in range(max(1, n_iter)):
        Aw = A * wgt[:, None]
        coef, *_ = np.linalg.lstsq(Aw, v * wgt, rcond=None)
        res = v - A @ coef
        s = 1.4826 * _median(np.abs(res - _median(res))) + 1e-6
        wgt = np.clip(1.0 - (np.abs(res) / (3.0 * s)) ** 2, 0.0, 1.0)
        if np.count_nonzero(wgt) < 8:
            break
    res = v - A @ coef
    sigma = 1.4826 * _median(np.abs(res - _median(res)))
    return coef, float(sigma)


def _centroid_bg(gray: np.ndarray, x: float, y: float, probe: _Probe,
                 power: float = 1.0, r_frac: float = 1.35) -> dict:
    """Local-plane background-subtracted, circular-window centroid.

    The background annulus starts beyond the PSF shoulder
    (``1.45 r + 2 w`` where ``w`` is the measured 10-90 % edge width): an
    annulus that starts at ``1.25 r`` sits *on* the blurred edge of a 4 px
    marker and biases both the plane and its noise estimate.
    """
    r = probe.radius if probe.radius_ok else max(2.0, 0.5 * (probe.rho[-1] / 2.6))
    ew = probe.edge_width if np.isfinite(probe.edge_width) else 1.2
    r_in = 1.45 * r + 2.0 * max(ew, 0.8)
    r_out = r_in + max(1.2 * r, 4.0)
    coef, sigma_bg = _plane_background(gray, x, y, r_in, r_out)
    x0, x1, y0, y1 = _patch_bounds(gray.shape, x, y, r_frac * r + 2.0)
    patch = gray[y0:y1, x0:x1]
    ys, xs = np.mgrid[y0:y1, x0:x1]
    dx = xs - x
    dy = ys - y
    rho = np.hypot(dx, dy)
    s = probe.sign
    if coef is None:
        bg = np.full(patch.shape, probe.outer if np.isfinite(probe.outer) else np.median(patch))
    else:
        bg = coef[0] + coef[1] * dx + coef[2] * dy
    val = s * (patch - bg)
    mask = (rho <= r_frac * r) & np.isfinite(val)
    wt = np.where(mask, np.clip(val, 0.0, None) ** power, 0.0)
    sw = float(wt.sum())
    if not np.isfinite(sw) or sw <= 1e-9:
        return {"ok": False, "reason": "no_signal", "u": float(x), "v": float(y),
                "sigma_px": float("nan"), "n_edges": 0, "quality": 0.0,
                "sigma_bg": sigma_bg}
    u = float((wt * xs).sum() / sw)
    v = float((wt * ys).sum() / sw)
    # First-order propagation through u = sum(w x)/sum(w), with w = I - bg
    # clipped at 0:  du/dI_i = (x_i - u)/sum(w) for an *unsaturated* pixel and
    # 0 for a saturated one (its weight no longer depends on the signal).
    sig_n = sigma_bg if np.isfinite(sigma_bg) and sigma_bg > 0.05 else max(0.5, probe.rmse)
    sat_lvl = 254.5
    free = (mask & (s * patch < sat_lvl) & (val > 0))
    var_x = float(((xs - u) ** 2)[free].sum())
    var_y = float(((ys - v) ** 2)[free].sum())
    sig_u = sig_n * math.sqrt(var_x) / sw
    sig_v = sig_n * math.sqrt(var_y) / sw
    sigma = math.hypot(sig_u, sig_v)
    sigma = math.hypot(sigma, _PIXELATION_FLOOR * (4.0 / max(2.0 * r, 1.0)) ** 0.5)
    sigma = math.hypot(sigma, _CENTROID_SYSTEMATIC_FLOOR)
    sigma = max(sigma, 0.003)
    return {"ok": True, "reason": "ok", "u": u, "v": v, "sigma_px": sigma,
            "n_edges": 0, "quality": 0.0, "sigma_bg": sigma_bg,
            "sum_w": sw, "n_pix": int(mask.sum()), "radius_px": r}


# ==========================================================================
# edge extraction (Canny + parabolic sub-pixel refinement)
# ==========================================================================

def _subpixel_edges(patch: np.ndarray, sigma: float, low: float = 0.0,
                    high: float = 0.0, adaptive: bool = True,
                    border: int = 3):
    """Canny + parabolic sub-pixel refinement; returns (points, normals, mag).

    Points are in patch coordinates.  ``low``/``high`` are ignored when
    ``adaptive`` is true: the thresholds are scaled to the patch gradient
    distribution (the vendor corpus has 40-80 DN backgrounds and saturated
    markers, so fixed thresholds either clip or miss low-contrast frames).
    """
    blur = cv2.GaussianBlur(patch.astype(np.float32), (0, 0), float(sigma))
    gx = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    g999 = float(np.percentile(mag, 99.9))
    if adaptive:
        low = max(3.0, 0.20 * g999)
        high = max(6.0, 0.45 * g999)
    u8 = np.clip(blur, 0, 255).astype(np.uint8)
    edges = cv2.Canny(u8, float(low), float(high), L2gradient=True)
    if border > 0:
        edges[:border, :] = 0
        edges[-border:, :] = 0
        edges[:, :border] = 0
        edges[:, -border:] = 0
    ys, xs = np.nonzero(edges)
    if len(xs) < 6:
        return None, None, None
    nx = gx[ys, xs]
    ny = gy[ys, xs]
    nrm = np.hypot(nx, ny) + 1e-9
    nx = nx / nrm
    ny = ny / nrm
    m0 = mag[ys, xs]
    mm = _bilinear(mag, xs - nx, ys - ny)
    mp = _bilinear(mag, xs + nx, ys + ny)
    den = np.nan_to_num(mm) - 2.0 * m0 + np.nan_to_num(mp)
    delta = np.where(np.abs(den) > 1e-6, 0.5 * (np.nan_to_num(mm) - np.nan_to_num(mp)) / den, 0.0)
    delta = np.clip(delta, -0.5, 0.5)
    px = xs + delta * nx
    py = ys + delta * ny
    return (np.stack([px, py], 1), np.stack([nx, ny], 1), m0)


def _edge_points(gray: np.ndarray, x: float, y: float, r: float,
                 probe: _Probe, sigma_blur: Optional[float] = None,
                 n_min: int = 8):
    """Sub-pixel edge points of one marker, in *pixel* coordinates.

    Keeps only edges that (a) lie in a radial band around the seed, (b) have
    the gradient pointing toward the marker centre (inward for a bright
    marker), and (c) are inside the image.  Returns ``(pts, normals, info)``.
    """
    if sigma_blur is None:
        ew = probe.edge_width if np.isfinite(probe.edge_width) else 1.2
        sigma_blur = float(np.clip(0.35 * r + 0.15 * ew, 0.7, 2.2))
    r_win = 1.75 * r + 4.0
    x0, x1, y0, y1 = _patch_bounds(gray.shape, x, y, r_win, pad=1)
    patch = gray[y0:y1, x0:x1]
    if min(patch.shape) < 6:
        return None, None, {"reason": "patch_small"}
    pts, nrm, mag = _subpixel_edges(patch, sigma_blur, adaptive=True)
    if pts is None:
        return None, None, {"reason": "no_edges", "sigma_blur": sigma_blur}
    pts = pts + np.array([x0, y0], float)
    dx = x - pts[:, 0]
    dy = y - pts[:, 1]
    rr = np.hypot(dx, dy)
    s = probe.sign
    inward = (nrm[:, 0] * dx + nrm[:, 1] * dy) * s > 0.35 * rr
    band = (rr > 0.45 * r) & (rr < 1.55 * r + 2.0)
    if probe.radius_ok:
        band = (rr > 0.55 * probe.radius) & (rr < 1.45 * probe.radius + 1.5)
    keep = inward & band
    n_all = int(keep.sum())
    if n_all < n_min:
        return None, None, {"reason": "few_edges", "n_raw": int(len(pts)),
                            "sigma_blur": sigma_blur}
    return pts[keep], nrm[keep], {"sigma_blur": sigma_blur, "n_raw": int(len(pts)),
                                  "n_keep": n_all}


# ==========================================================================
# conic fit
# ==========================================================================

def _fit_conic(pts: np.ndarray, w: Optional[np.ndarray] = None):
    """Weighted direct ellipse fit (Halir & Flusser 1998).  5 coefficients.

    Returns the conic ``[A, B, C, D, E, F]`` with ``A x^2 + B xy + C y^2 +
    D x + E y + F = 0`` or ``None`` when the points do not define an ellipse.
    """
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    if len(pts) < 6:
        return None
    if w is None:
        w = np.ones(len(pts))
    w = np.asarray(w, np.float64)
    if np.count_nonzero(w > 0) < 6:
        return None
    sw = np.sqrt(np.maximum(w, 0.0))
    x = pts[:, 0]
    y = pts[:, 1]
    D1 = np.column_stack([x * x, x * y, y * y]) * sw[:, None]
    D2 = np.column_stack([x, y, np.ones_like(x)]) * sw[:, None]
    S1 = D1.T @ D1
    S2 = D1.T @ D2
    S3 = D2.T @ D2
    try:
        T = -np.linalg.solve(S3, S2.T)
    except np.linalg.LinAlgError:
        return None
    M = S1 + S2 @ T
    M = np.array([M[0] / 2.0, M[1], M[2] / 2.0])
    Cst = np.array([[0.0, 0.0, 2.0], [0.0, -1.0, 0.0], [2.0, 0.0, 0.0]])
    try:
        evals, evecs = np.linalg.eig(np.linalg.solve(Cst, M))
    except np.linalg.LinAlgError:
        return None
    # Halir-Flusser: the ellipse is the eigenvector of the single positive
    # generalised eigenvalue.  Fall back to any candidate with 4AC-B^2 > 0
    # whose implied ellipse is sane if the numerically positive one is not
    # usable (it happens for nearly degenerate arcs).
    pos = [k for k in range(3) if abs(evals[k].imag) < 1e-8
           and np.isfinite(evals[k].real) and evals[k].real > 0]
    order = pos + [k for k in range(3) if k not in pos]
    for k in order:
        if abs(evals[k].imag) > 1e-8 or not np.isfinite(evals[k].real):
            continue
        a1 = np.real(evecs[:, k])
        con = np.concatenate([a1, T @ a1])
        A, B, C = con[0], con[1], con[2]
        if not (4.0 * A * C - B * B > 0):
            continue
        if A < 0:      # normalise so that the quadratic form is positive
            con = -con
        ca = _conic_centre_axes(con)
        if ca is None:
            continue
        c, semi, _ = ca
        cen = np.median(pts, axis=0)
        if not np.isfinite(semi).all() or np.min(semi) <= 0:
            continue
        if float(np.hypot(*(c - cen))) > 6.0 * float(np.max(semi)):
            continue
        return con
    return None


def _conic_centre_axes(con: np.ndarray):
    """Centre, semi-axes (sorted), axes matrix of an ellipse conic."""
    A, B, C, D, E, F = con
    Mq = np.array([[A, B / 2.0], [B / 2.0, C]], float)
    c = np.linalg.solve(Mq, np.array([-D / 2.0, -E / 2.0], float))
    Fc = float(A * c[0] ** 2 + B * c[0] * c[1] + C * c[1] ** 2
               + D * c[0] + E * c[1] + F)
    evals, evecs = np.linalg.eigh(Mq)
    if np.any(evals <= 0) or -Fc <= 0:
        return None
    semi = np.sqrt(-Fc / evals)          # along evecs columns, ascending evals
    return c, semi, evecs


def _conic_residual(con: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Approximate geometric distance (px) of points to the conic."""
    A, B, C, D, E, F = con
    x = pts[:, 0]
    y = pts[:, 1]
    val = A * x * x + B * x * y + C * y * y + D * x + E * y + F
    gx = 2.0 * A * x + B * y + D
    gy = B * x + 2.0 * C * y + E
    return np.abs(val) / (np.hypot(gx, gy) + 1e-12)


def _conic_points(con: np.ndarray, n: int = 64) -> Optional[np.ndarray]:
    """Sample ``n`` points exactly on the ellipse conic."""
    ca = _conic_centre_axes(con)
    if ca is None:
        return None
    c, semi, evecs = ca
    th = np.linspace(0.0, _TWO_PI, n, endpoint=False)
    loc = np.stack([semi[0] * np.cos(th), semi[1] * np.sin(th)], 1)
    return c[None, :] + loc @ evecs.T


def _robust_conic(pts: np.ndarray, prior_xy: np.ndarray, prior_r: float,
                  n_iter: int = 3, max_axis_ratio: float = 3.0):
    """Iteratively trimmed conic fit with geometric guards.

    Returns a dict with ``conic``, ``centre``, ``semi``, ``evecs``, ``rms``,
    ``n``, ``scale``, ``coverage`` (angular coverage of the inliers) or None.
    """
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    if len(pts) < 6:
        return None
    keep = np.ones(len(pts), bool)
    con = None
    prev_c = None
    for it in range(max(1, n_iter)):
        if int(keep.sum()) < 6:
            return None
        con = _fit_conic(pts[keep])
        if con is None:
            return None
        ca = _conic_centre_axes(con)
        if ca is None:
            return None
        c, semi, _ = ca
        if not np.isfinite(c).all() or not np.isfinite(semi).all():
            return None
        res = _conic_residual(con, pts)
        med = _median(res[keep])
        scale = max(1.4826 * med, 0.02)
        new_keep = res < max(3.0 * scale, 0.12)
        if new_keep.sum() < 6:
            new_keep = keep
        stable = (prev_c is not None
                  and float(np.max(np.abs(c - prev_c))) < 0.002 * max(float(prior_r), 1.0))
        prev_c = c
        if stable or (it > 0 and np.array_equal(new_keep, keep)):
            break
        keep = new_keep
    con = _fit_conic(pts[keep])
    if con is None:
        return None
    ca = _conic_centre_axes(con)
    if ca is None:
        return None
    c, semi, evecs = ca
    res = _conic_residual(con, pts[keep])
    rms = float(np.sqrt(np.mean(res ** 2))) if len(res) else float("nan")
    # geometric guards (caller works in a known unit: pixels or fx-normalised)
    pr = float(prior_r)
    if not (0.30 * pr < float(np.mean(semi)) < 2.0 * pr):
        return None
    if float(semi.max()) / max(float(semi.min()), 1e-9) > max_axis_ratio:
        return None
    if float(np.hypot(c[0] - prior_xy[0], c[1] - prior_xy[1])) > max(2.5, 0.8 * pr):
        return None
    if float(np.min(semi)) < 0.6:
        return None
    # angular coverage of the inliers around the centre
    ang = np.arctan2(pts[keep][:, 1] - c[1], pts[keep][:, 0] - c[0])
    ang = np.sort(np.mod(ang, _TWO_PI))
    if len(ang) > 1:
        gaps = np.diff(np.concatenate([ang, [ang[0] + _TWO_PI]]))
        coverage = 1.0 - float(gaps.max()) / _TWO_PI
    else:
        coverage = 0.0
    return {"conic": con, "centre": c, "semi": semi, "evecs": evecs,
            "rms": rms, "n": int(keep.sum()), "scale": scale,
            "coverage": coverage, "inlier_frac": float(keep.mean())}


# ==========================================================================
# plane / perspective correction
# ==========================================================================

def plane_normal_from_homography(H: np.ndarray, K: Optional[np.ndarray] = None,
                                 kc: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
    """Unit normal (camera frame) of the plane mapped by ``H``.

    ``H`` must map *board* coordinates to **undistorted** image coordinates
    (normalised camera coordinates are fine: any affine in-plane unit works,
    because the normal and the projected centre of a back-projected circle
    are invariant to the in-plane scale).  With ``K``/``kc`` supplied the
    homography is first converted from distorted pixels by undistorting its
    four corners and re-fitting.

    The normal is oriented so that the plane lies in front of the camera
    (``n . ray > 0`` for the image centre).
    """
    H = np.asarray(H, float)
    if H.shape != (3, 3) or not np.isfinite(H).all():
        return None
    if K is not None:
        K = np.asarray(K, float)
        kc = np.zeros(5) if kc is None else np.asarray(kc, float).ravel()
        corners = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        pix = cv2.perspectiveTransform(corners.reshape(-1, 1, 2), H).reshape(-1, 2)
        und = cv2.undistortPoints(pix.reshape(-1, 1, 2).astype(np.float32),
                                  K, kc).reshape(-1, 2)
        Hn, _ = cv2.findHomography(corners, und, 0)
        if Hn is None:
            return None
        H = Hn
    # else: the caller guarantees H already maps to undistorted normalised
    # coordinates (the plane normal and the projection of a back-projected
    # circle centre are invariant to the in-plane scale)
    h1 = H[:, 0]
    h2 = H[:, 1]
    n = np.cross(h1, h2)
    nn = float(np.linalg.norm(n))
    if nn < 1e-15:
        return None
    n = n / nn
    # orient towards the camera: the board origin projects in front (t . n > 0)
    if float(np.dot(n, H[:, 2])) < 0:
        n = -n
    return n


def _plane_ray_intersections(rays: np.ndarray, n: np.ndarray) -> Optional[np.ndarray]:
    """Intersect rays ``(x, y, 1)`` with the plane ``n . X = 1``."""
    den = rays @ n
    if np.any(np.abs(den) < 1e-9):
        return None
    if np.any(den <= 0):
        return None
    return rays / den[:, None]


def _fit_circle_2d(pts: np.ndarray, n_iter: int = 3):
    """Robust algebraic circle fit; returns (centre, radius, rms)."""
    pts = np.asarray(pts, np.float64)
    if len(pts) < 3:
        return None
    keep = np.ones(len(pts), bool)
    for it in range(n_iter):
        p = pts[keep]
        if len(p) < 3:
            return None
        A = np.column_stack([2.0 * p[:, 0], 2.0 * p[:, 1], np.ones(len(p))])
        b = (p ** 2).sum(1)
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        c = sol[:2]
        r = math.sqrt(max(float(sol[2] + c @ c), 1e-12))
        res = np.abs(np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1]) - r)
        s = max(1.4826 * _median(res[keep]), 1e-9)
        new_keep = res < max(3.0 * s, 1e-6 * r)
        if new_keep.sum() < 3:
            break
        if it > 0 and np.array_equal(new_keep, keep):
            break
        keep = new_keep
    p = pts[keep]
    A = np.column_stack([2.0 * p[:, 0], 2.0 * p[:, 1], np.ones(len(p))])
    b = (p ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    c = sol[:2]
    r = math.sqrt(max(float(sol[2] + c @ c), 1e-12))
    res = np.abs(np.hypot(pts[keep, 0] - c[0], pts[keep, 1] - c[1]) - r)
    return c, r, float(np.sqrt(np.mean(res ** 2))), int(keep.sum())


def backproject_circle_centre(conic, plane_normal: np.ndarray,
                              n: int = 64, min_circularity: float = 0.02):
    """Projection of the 3-D circle centre from a fitted **undistorted** conic.

    Parameters
    ----------
    conic : sequence of 5 conic coefficients ``(A, B, C, D, E, F)`` in
        undistorted *normalised* camera coordinates, or a dict returned by
        :func:`_robust_conic` scaled to normalised units, or an ellipse
        ``((cx, cy), (a, b), angle)`` in those units.
    plane_normal : unit normal of the marker plane in the camera frame.

    Returns ``dict`` with ``uv`` (pixels are *not* applied here: the returned
    point is the projection expressed in the same normalised coordinates,
    i.e. ``X / Z``), ``correction`` (in-plane shift in normalised units),
    ``circularity`` (RMS radial residual / radius of the back-projected
    circle: the plane-normal consistency check) and ``radius3d``.
    """
    nn = np.asarray(plane_normal, float).ravel()
    nn = nn / max(float(np.linalg.norm(nn)), 1e-15)
    if isinstance(conic, Mapping):
        con = np.asarray(conic["conic"], float)
    elif isinstance(conic, (tuple, list)) and len(conic) == 3 and np.ndim(conic[0]) == 1:
        # ((cx, cy), (a, b), angle) ellipse in undistorted normalised coords
        (cx, cy), (a, b), ang = conic
        a = max(float(a), 1e-9) * 0.5
        b = max(float(b), 1e-9) * 0.5
        th = math.radians(float(ang))
        ca, sa = math.cos(th), math.sin(th)
        loc = np.stack([a * np.cos(np.linspace(0, _TWO_PI, n, endpoint=False)),
                        b * np.sin(np.linspace(0, _TWO_PI, n, endpoint=False))], 1)
        Rm = np.array([[ca, -sa], [sa, ca]])
        pts_n = np.array([cx, cy]) + loc @ Rm.T
        return _backproject_points(pts_n, nn)
    else:
        con = np.asarray(conic, float).ravel()
    pts_n = _conic_points(con, n)
    if pts_n is None:
        return None
    return _backproject_points(pts_n, nn, min_circularity=min_circularity)


def _backproject_points(pts_n: np.ndarray, n: np.ndarray,
                        min_circularity: float = 0.02):
    rays = np.column_stack([pts_n, np.ones(len(pts_n))])
    X = _plane_ray_intersections(rays, n)
    if X is None:
        return None
    # in-plane basis with origin at n (d = 1)
    e1 = np.array([0.0, 0.0, 1.0]) - n[2] * n
    if float(np.linalg.norm(e1)) < 1e-6:
        e1 = np.array([1.0, 0.0, 0.0]) - n[0] * n
    e1 = e1 / np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    a = X @ e1
    b = X @ e2
    fit = _fit_circle_2d(np.column_stack([a, b]))
    if fit is None:
        return None
    c2, rad, rms, nin = fit
    circ = rms / max(rad, 1e-12)
    Xc = n + c2[0] * e1 + c2[1] * e2
    if Xc[2] <= 1e-9:
        return None
    uv = Xc[:2] / Xc[2]
    return {"uv": uv, "circularity": float(circ), "radius3d": float(rad),
            "n": int(nin), "ok": bool(circ < max(min_circularity, 0.05))}


# ==========================================================================
# gradient (radial-derivative) fit
# ==========================================================================

def _radial_edges(gray: np.ndarray, x: float, y: float, r0: float,
                  probe: _Probe, n_ang: int = 36, ds: float = 0.25,
                  lo: float = 0.55, hi: float = 1.5):
    """Sub-pixel edge points from the peak radial derivative, per ray."""
    r_lo = max(lo * r0, 1.0)
    r_hi = max(hi * r0, r_lo + 1.0)
    rho = np.arange(r_lo, r_hi + 0.5 * ds, ds)
    th = np.arange(n_ang, dtype=np.float64) * (_TWO_PI / n_ang)
    ct = np.cos(th)[:, None]
    st = np.sin(th)[:, None]
    I = _bilinear(gray, x + ct * rho[None, :], y + st * rho[None, :])
    if I.size == 0 or np.count_nonzero(np.isfinite(I)) < 12:
        return None, None, {"reason": "no_signal"}
    # derivative along rho (NaN-aware): use finite differences on a filled copy
    prof = np.where(np.isfinite(I), I, np.nan)
    for i in range(prof.shape[0]):
        ok = np.isfinite(prof[i])
        if ok.sum() >= 3:
            prof[i] = np.interp(rho, rho[ok], prof[i][ok])
    d = np.gradient(prof, ds, axis=1)
    # for a bright marker the intensity falls outward -> minimum of d
    s = probe.sign
    crit = s * d
    j = np.argmin(crit, axis=1)
    vals = crit[np.arange(len(j)), j]
    good = (j > 0) & (j < len(rho) - 1) & np.isfinite(vals)
    if np.count_nonzero(good) < 6:
        return None, None, {"reason": "few_edges"}
    jj = j[good]
    idx = np.arange(len(j))[good]
    f0 = crit[idx, jj - 1]
    f1 = crit[idx, jj]
    f2 = crit[idx, jj + 1]
    den = f0 - 2.0 * f1 + f2
    delta = np.where(np.abs(den) > 1e-12, 0.5 * (f0 - f2) / den, 0.0)
    delta = np.clip(delta, -1.0, 1.0)
    re = rho[jj] + delta * ds
    pts = np.column_stack([x + np.cos(th[idx]) * re, y + np.sin(th[idx]) * re])
    strength = np.abs(vals[idx])
    keep = strength > max(0.25 * float(np.max(strength)), 1e-9)
    # reject points outside the image (the seed can sit near the border)
    h, w = gray.shape
    keep &= (pts[:, 0] > 0) & (pts[:, 0] < w - 1) & (pts[:, 1] > 0) & (pts[:, 1] < h - 1)
    if np.count_nonzero(keep) < 6:
        return None, None, {"reason": "few_edges"}
    return pts[keep], re[keep], {"n_rays": n_ang, "n_keep": int(keep.sum()),
                                 "strength": float(np.median(strength[keep]))}


def _fit_circle_pts(pts: np.ndarray, prior_xy: np.ndarray, prior_r: float):
    """Robust circle fit with geometric guards; returns centre + diagnostics."""
    fit = _fit_circle_2d(pts)
    if fit is None:
        return None
    c, rad, rms, nin = fit
    if not np.isfinite(c).all() or not np.isfinite(rad):
        return None
    if not (0.35 * prior_r < rad < 2.0 * prior_r):
        return None
    if float(np.hypot(c[0] - prior_xy[0], c[1] - prior_xy[1])) > max(2.5, 0.8 * prior_r):
        return None
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    ang = np.sort(np.mod(ang, _TWO_PI))
    gaps = np.diff(np.concatenate([ang, [ang[0] + _TWO_PI]]))
    coverage = 1.0 - float(gaps.max()) / _TWO_PI
    return {"centre": c, "radius": float(rad), "rms": float(rms), "n": int(nin),
            "coverage": coverage}


# ==========================================================================
# per-method implementations -> dict
# ==========================================================================

def _result(method: str, requested: str, x: float, y: float, **kw) -> dict:
    out = {"u": float(x), "v": float(y), "x": float(x), "y": float(y),
           "method": method, "requested": requested,
           "sigma_px": float("nan"), "quality": 0.0, "n_edges": 0,
           "ok": False, "reason": "unimplemented", "radius_px": float("nan"),
           "contrast": float("nan"), "polarity": "unknown", "saturated": False,
           "partial": False, "coverage": 0.0, "fallback": None,
           "runtime_ms": 0.0}
    out.update(kw)
    return out


def _do_centroid(gray, x, y, r, probe, **kw) -> dict:
    # reproduce the production refinement exactly (integer seed, window radius)
    win = int(kw.get("window", max(4, int(round(2.2 * r)))))
    h, w = gray.shape
    if not (-win <= x <= w + win and -win <= y <= h + win):
        return _result("centroid", "centroid", x, y, ok=False,
                       reason="outside_image", radius_px=probe.radius)
    u, v = _weighted_centroid_compat(gray, int(round(x)), int(round(y)), win,
                                     invert=probe.polarity < 0)
    res = _result("centroid", "centroid", u, v, ok=True, reason="ok")
    # noise model: same weight propagation as centroid_bg, on the actual patch
    x0, x1, y0, y1 = _patch_bounds(gray.shape, x, y, win)
    patch = gray[y0:y1, x0:x1].astype(np.float64)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    border = np.concatenate([patch[0, :], patch[-1, :], patch[:, 0], patch[:, -1]])
    bg = float(np.median(border))
    wt = np.clip(probe.sign * (patch - bg), 0.0, None)
    sw = float(wt.sum())
    if sw > 1e-9:
        ub = float((wt * xs).sum() / sw)
        vb = float((wt * ys).sum() / sw)
        sig_n = max(probe.rmse, 0.5)
        free = (probe.sign * patch < 254.5) & (wt > 0)
        var_x = float(((xs - ub) ** 2)[free].sum())
        var_y = float(((ys - vb) ** 2)[free].sum())
        sig_u = sig_n * math.sqrt(var_x) / sw
        sig_v = sig_n * math.sqrt(var_y) / sw
        r_eff = max(probe.radius, 2.0)
        res["sigma_px"] = max(math.hypot(math.hypot(math.hypot(sig_u, sig_v),
                                                   _PIXELATION_FLOOR * (4.0 / (2.0 * r_eff)) ** 0.5),
                                         _CENTROID_SYSTEMATIC_FLOOR),
                              0.003)
    res["n_edges"] = 0
    res["radius_px"] = probe.radius
    res["quality"] = _centroid_quality(probe, res["sigma_px"])
    res["window"] = win
    return res


def _centroid_quality(probe: _Probe, sigma: float) -> float:
    q_snr = _clip01((probe.contrast - 4.0) / 40.0)
    q_sig = _clip01(1.0 - (sigma - 0.005) / 0.15)
    q_sat = 0.9 if probe.saturated else 1.0
    return _clip01(q_snr * q_sig * q_sat)


def _do_centroid_bg(gray, x, y, r, probe, **kw) -> dict:
    est = _centroid_bg(gray, x, y, probe, power=float(kw.get("power", 1.0)))
    res = _result("centroid_bg", "centroid_bg", est["u"], est["v"],
                  ok=bool(est["ok"]), reason=est["reason"])
    res["sigma_px"] = est["sigma_px"]
    res["radius_px"] = est.get("radius_px", probe.radius)
    res["quality"] = _centroid_quality(probe, res["sigma_px"])
    res["n_pix"] = est.get("n_pix", 0)
    return res


def _do_ellipse(gray, x, y, r, probe, **kw) -> dict:
    r_px = probe.radius if probe.radius_ok else r
    win = int(math.ceil(1.7 * r_px)) + 2
    x0, x1, y0, y1 = _patch_bounds(gray.shape, x, y, win, pad=0)
    patch = gray[y0:y1, x0:x1]
    res = _result("ellipse", "ellipse", x, y, reason="no_contour")
    if patch.size < 25 or not np.isfinite(probe.contrast) or probe.contrast == 0:
        return res
    mid = 0.5 * (probe.inner + probe.outer)
    bws = probe.sign * (patch - mid) > 0
    bw = (bws.astype(np.uint8)) * 255
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return res
    # contour whose centroid is closest to the seed and whose area is sane
    best = None
    for c in cnts:
        if len(c) < 8:
            continue
        m = cv2.moments(c)
        if abs(m["m00"]) < 1e-6:
            continue
        cx = m["m10"] / m["m00"] + x0
        cy = m["m01"] / m["m00"] + y0
        d = math.hypot(cx - x, cy - y)
        area = abs(m["m00"])
        if d > max(2.5, 0.8 * r_px) or not (4.0 < area < (3.2 * r_px) ** 2 * math.pi):
            continue
        if best is None or d < best[0]:
            best = (d, c, cx, cy)
    if best is None:
        return res
    _, c, cx, cy = best
    try:
        (ex, ey), (a, b), ang = cv2.fitEllipse(c.reshape(-1, 1, 2).astype(np.float32))
    except cv2.error:
        return res
    ex += x0
    ey += y0
    if not np.isfinite([ex, ey, a, b, ang]).all() or min(a, b) < 1.0:
        return res
    if max(a, b) / min(a, b) > 3.0:
        return res
    if math.hypot(ex - x, ey - y) > max(2.5, 0.8 * r_px):
        return res
    res.update(u=float(ex), v=float(ey), x=float(ex), y=float(ey), ok=True,
               reason="ok", n_edges=int(len(c)), coverage=1.0)
    res["ellipse"] = ((float(ex), float(ey)), (float(a), float(b)), float(ang))
    # contour quantisation dominates: sigma ~ 0.35 px / sqrt(n) with a floor
    res["sigma_px"] = max(0.35 / math.sqrt(max(len(c), 4)), 0.02)
    res["quality"] = _clip01(_centroid_quality(probe, res["sigma_px"]) * 0.8)
    return res


def _distort_normalized(pts_n: np.ndarray, K, kc) -> np.ndarray:
    """Project normalised (undistorted) points to distorted pixels.

    Implements the same model as :func:`calib.geometry.distort` /
    :func:`calib.geometry.project`, including the CCF skew term.
    """
    pts_n = np.asarray(pts_n, np.float64).reshape(-1, 2)
    if len(pts_n) == 0:
        return pts_n
    K = np.asarray(K, np.float64)
    k1, k2, p1, p2, k3 = np.zeros(5) if kc is None else np.asarray(kc, np.float64).ravel()[:5]
    x = pts_n[:, 0]
    y = pts_n[:, 1]
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return np.column_stack([K[0, 0] * xd + K[0, 1] * yd + K[0, 2],
                            K[1, 1] * yd + K[1, 2]])


def _do_edge_conic(gray, x, y, r, probe, method="conic", K=None, kc=None,
                   plane=None, **kw) -> dict:
    """Shared implementation of ``conic`` / ``conic_perspective``."""
    res = _result(method, method, x, y, reason="no_edges")
    r_px = probe.radius if probe.radius_ok else r
    sigma_blur = kw.get("sigma_blur", None)
    pts, nrm, info = _edge_points(gray, x, y, r_px, probe, sigma_blur=sigma_blur)
    if pts is None:
        res.update(reason=info.get("reason", "no_edges"),
                   n_edges=int(info.get("n_keep", 0)))
        return res
    use_undistort = bool(kw.get("use_undistort", True)) and K is not None
    if use_undistort:
        K = np.asarray(K, float)
        kc = np.zeros(5) if kc is None else np.asarray(kc, float).ravel()
        und = cv2.undistortPoints(pts.reshape(-1, 1, 2).astype(np.float32),
                                  K, kc).reshape(-1, 2)
        # ``und * S`` is "undistorted pixel" units (isotropic, ~pixels): the
        # conic is fitted there so that every guard can be expressed in px.
        S = float(np.sqrt(abs(K[0, 0] * K[1, 1])))
        pts_fit = und * S
        seed_fit = cv2.undistortPoints(np.array([[[x, y]]], np.float32),
                                       K, kc).reshape(-1, 2)[0] * S
        fit = _robust_conic(pts_fit, seed_fit, prior_r=r_px)
    else:
        S = 1.0
        fit = _robust_conic(pts, np.array([x, y], float), prior_r=r_px)
    if fit is None:
        res.update(reason="conic_rejected", n_edges=int(len(pts)))
        return res
    c_fit = fit["centre"]
    if use_undistort:
        c_n = c_fit / S
        # ellipse parameters in normalised units for the back-projection
        semi_n = fit["semi"] / S
        uv_px = _distort_normalized(c_n.reshape(1, 2), K, kc)[0]
    else:
        uv_px = c_fit
        semi_n = fit["semi"]
    res.update(u=float(uv_px[0]), v=float(uv_px[1]), x=float(uv_px[0]),
               y=float(uv_px[1]), ok=True, reason="ok",
               n_edges=int(fit["n"]), coverage=float(fit["coverage"]),
               radius_px=(float(np.mean(fit["semi"])) if use_undistort
                          else float(np.mean(fit["semi"]))),
               edge_rms=float(fit["rms"]), inlier_frac=float(fit["inlier_frac"]),
               undistorted=bool(use_undistort))
    # sigma: radial edge noise / sqrt(n_eff), shape factor for the conic centre
    sig_p = max(fit["rms"], 0.02)
    n_eff = max(int(fit["n"]), 4)
    res["sigma_px"] = max(sig_p * math.sqrt(2.0 / n_eff) * 1.6, 0.004)
    res["quality"] = _edge_quality(probe, fit, n_eff, r_px)
    res["ellipse"] = None
    if use_undistort:
        # ellipse in normalised units (for the correction and for the caller)
        ang = math.degrees(math.atan2(fit["evecs"][1, 1], fit["evecs"][0, 1]))
        res["ellipse_n"] = ((float(c_n[0]), float(c_n[1])),
                            (float(2 * semi_n[1]), float(2 * semi_n[0])), float(ang))
    if method == "conic_perspective":
        n = None
        if plane is not None:
            n = _normal_from_plane(plane)
        if n is None and use_undistort and plane is not None and "homography" in plane:
            n = plane_normal_from_homography(plane["homography"], K, kc)
        if n is None:
            res.update(reason="no_plane", fallback="conic", method="conic",
                       plane=None)
            return res
        if not use_undistort:
            res.update(reason="no_intrinsics", fallback="conic", method="conic")
            return res
        # The fit works in "undistorted pixel" coordinates u_px = S * u_n.
        # The same ellipse expressed in normalised coordinates has
        # [A, B, C, D/S, E/S, F/S^2] (an overall scale is irrelevant).
        con_n = np.asarray(fit["conic"], float).copy()
        con_n[3:5] /= S
        con_n[5] /= S ** 2
        bp = backproject_circle_centre(con_n, n, n=64)
        if bp is None:
            res.update(reason="backprojection_failed", fallback="conic",
                       method="conic")
            return res
        if not bp["ok"]:
            res.update(reason="bad_circularity", fallback="conic",
                       method="conic", circularity=bp["circularity"])
            return res
        uv_bp = _distort_normalized(bp["uv"].reshape(1, 2), K, kc)[0]
        corr = np.asarray(uv_bp, float) - np.asarray(uv_px, float)
        res.update(u=float(uv_bp[0]), v=float(uv_bp[1]), x=float(uv_bp[0]),
                   y=float(uv_bp[1]), reason="ok", plane=n.tolist(),
                   circularity=float(bp["circularity"]),
                   correction=(float(corr[0]), float(corr[1])),
                   correction_px=float(np.hypot(*corr)),
                   radius3d=float(bp["radius3d"]))
        # the back-projected centre is less sensitive to the ellipse shape
        res["sigma_px"] = max(res["sigma_px"] * 0.8, 0.003)
    res["partial"] = bool(fit["coverage"] < 0.6)
    return res


def _normal_from_plane(plane) -> Optional[np.ndarray]:
    if plane is None:
        return None
    if isinstance(plane, Mapping):
        if plane.get("normal") is not None:
            n = np.asarray(plane["normal"], float).ravel()
            if n.size == 3 and np.isfinite(n).all() and np.linalg.norm(n) > 0:
                return n / np.linalg.norm(n)
        R = plane.get("R")
        t = plane.get("t")
        if R is not None:
            R = np.asarray(R, float)
            n = R[:, 2].copy()
            if t is not None:
                d = float(np.dot(n, np.asarray(t, float).ravel()))
                if d < 0:
                    n = -n
            return n / max(np.linalg.norm(n), 1e-15)
        if plane.get("homography") is not None and plane.get("K") is not None:
            return plane_normal_from_homography(plane["homography"], plane["K"],
                                                plane.get("kc"))
        return None
    arr = np.asarray(plane, float).ravel()
    if arr.size == 3:
        return arr / max(np.linalg.norm(arr), 1e-15)
    return None


def _edge_quality(probe: _Probe, fit, n_eff: int, r_px: float) -> float:
    q_snr = _clip01((probe.contrast - 4.0) / 40.0)
    q_n = _clip01((n_eff - 5.0) / 20.0)
    q_rms = _clip01(1.0 - (fit["rms"] / max(r_px, 1.0)) / 0.5)
    q_cov = _clip01((fit["coverage"] - 0.35) / 0.5)
    return _clip01(q_snr * (0.35 + 0.65 * q_n) * (0.35 + 0.65 * q_rms) * (0.4 + 0.6 * q_cov))


def _do_gradient(gray, x, y, r, probe, **kw) -> dict:
    res = _result("gradient", "gradient", x, y, reason="no_edges")
    r_px = probe.radius if probe.radius_ok else r
    n_ang = int(kw.get("n_rays", 36))
    pts, radii, info = _radial_edges(gray, x, y, r_px, probe, n_ang=n_ang)
    if pts is None:
        res.update(reason=info.get("reason", "no_edges"))
        return res
    fit = _fit_circle_pts(pts, np.array([x, y], float), prior_r=r_px)
    if fit is None:
        res.update(reason="circle_rejected", n_edges=int(len(pts)))
        return res
    res.update(u=float(fit["centre"][0]), v=float(fit["centre"][1]),
               x=float(fit["centre"][0]), y=float(fit["centre"][1]),
               ok=True, reason="ok", n_edges=int(fit["n"]),
               coverage=float(fit["coverage"]),
               radius_px=float(fit["radius"]))
    sig_p = max(fit["rms"], 0.02)
    res["sigma_px"] = max(sig_p * math.sqrt(2.0 / max(fit["n"], 6)) * 1.3, 0.004)
    res["quality"] = _clip01(_edge_quality(probe,
                                           {"rms": fit["rms"], "coverage": fit["coverage"]},
                                           fit["n"], r_px))
    res["partial"] = bool(fit["coverage"] < 0.6)
    return res


# ==========================================================================
# public single-marker API
# ==========================================================================

def localize_marker(gray: np.ndarray, x: float, y: float, r: float,
                    method: str = "auto", **kw) -> dict:
    """Localise one circular marker at (sub-pixel) seed ``(x, y)``.

    Parameters
    ----------
    gray : (H, W) uint8/float array
        The frame.  No copy is made unless a colour image is passed.
    x, y : float
        Seed position (from the blob detector).  May be within ~1 px of the
        true centre; the methods are not translation invariant beyond their
        capture range (roughly ``0.3 r``).
    r : float
        Expected marker **silhouette radius** in pixels (not the window
        radius).  ``refine_grid`` estimates it automatically from the grid
        pitch, but a direct caller should pass e.g. 4.5 for the vendor IR
        frames at 450 mm.  The probe scans up to ``2.6 r`` so a large marker
        (1.7x) is still measured.
    method : str
        One of :data:`METHODS`.  ``auto`` selects per marker.
    **kw
        ``invert`` (force polarity; default: measured), ``prior_r``,
        ``power`` (centroid_bg), ``window`` (centroid), ``sigma_blur``,
        ``n_rays`` (gradient), ``use_undistort`` (conic ablation),
        ``K``, ``kc`` (needed by ``conic``/``conic_perspective``),
        ``plane`` (``{"R","t"}``, ``{"normal"}`` or
        ``{"homography","K","kc"}``), ``agree_px`` (auto).

    Returns
    -------
    dict
        At least ``u, v, sigma_px, method, quality, n_edges, ok, reason``;
        see the module docstring for the full list.
    """
    t_start = cv2.getTickCount()
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; expected one of {METHODS}")
    if not np.isfinite([x, y]).all():
        raise ValueError("seed must be finite")
    gray = _as_gray(gray)
    r = float(r)
    if not np.isfinite(r) or r <= 0:
        raise ValueError("r must be a positive radius in pixels")
    h_img, w_img = gray.shape
    if not (-2.0 * r <= x <= w_img + 2.0 * r and -2.0 * r <= y <= h_img + 2.0 * r):
        out = _result(method, method, x, y, ok=False, reason="outside_image",
                      radius_px=float(r))
        out["runtime_ms"] = (cv2.getTickCount() - t_start) / cv2.getTickFrequency() * 1e3
        return out
    invert = kw.pop("invert", None)
    K = kw.pop("K", None)
    kc = kw.pop("kc", None)
    plane = kw.pop("plane", None)
    probe = _probe_marker(gray, x, y, r, invert=invert)
    prior_r = float(kw.get("prior_r", r))

    # ---- dispatch -------------------------------------------------------
    if method == "centroid":
        res = _do_centroid(gray, x, y, prior_r, probe, **kw)
    elif method == "centroid_bg":
        res = _do_centroid_bg(gray, x, y, prior_r, probe, **kw)
    elif method == "ellipse":
        res = _do_ellipse(gray, x, y, prior_r, probe, **kw)
    elif method == "gradient":
        res = _do_gradient(gray, x, y, prior_r, probe, **kw)
    elif method in ("conic", "conic_perspective"):
        res = _do_edge_conic(gray, x, y, prior_r, probe, method=method,
                             K=K, kc=kc, plane=plane, **kw)
    else:
        res = _auto(gray, x, y, prior_r, probe, K=K, kc=kc, plane=plane, **kw)
    res["polarity"] = "bright" if probe.polarity > 0 else "dark"
    res["contrast"] = float(probe.contrast)
    res["saturated"] = bool(probe.saturated)
    res["sat_frac"] = float(probe.sat_frac)
    if not np.isfinite(res.get("radius_px", np.nan)):
        res["radius_px"] = float(probe.radius)
    res["edge_width"] = float(probe.edge_width)
    res["seed"] = (float(x), float(y))
    res["runtime_ms"] = (cv2.getTickCount() - t_start) / cv2.getTickFrequency() * 1e3
    if res.get("sigma_px") is not None and np.isfinite(res["sigma_px"]):
        res["sigma_px"] = float(res["sigma_px"] * SIGMA_CALIBRATION)
    res.setdefault("fallback", None)
    res.setdefault("correction", None)
    res.setdefault("circularity", None)
    res.setdefault("coverage", 0.0)
    return res


def _auto(gray, x, y, r, probe, *, K=None, kc=None, plane=None, **kw) -> dict:
    """Per-marker selection/fusion (see the module docstring)."""
    agree_px = float(kw.get("agree_px", 0.35))
    res = _result("auto", "auto", x, y, reason="no_signal")
    contrast = probe.contrast if np.isfinite(probe.contrast) else 0.0
    if abs(contrast) < 3.0:
        # unusable: return the seed, flagged, with an honest uncertainty
        res.update(ok=False, reason="low_contrast", sigma_px=float("inf"),
                   quality=0.0, contrast=float(contrast))
        return res

    cands = []
    cen = _do_centroid_bg(gray, x, y, r, probe, **kw)
    if cen["ok"]:
        cands.append(cen)
    edge = None
    if K is not None:
        if plane is not None:
            edge = _do_edge_conic(gray, x, y, r, probe, method="conic_perspective",
                                  K=K, kc=kc, plane=plane, **kw)
        if edge is None or not edge["ok"]:
            e2 = _do_edge_conic(gray, x, y, r, probe, method="conic",
                                K=K, kc=kc, **kw)
            if e2["ok"]:
                edge = e2
    else:
        e2 = _do_edge_conic(gray, x, y, r, probe, method="conic",
                            use_undistort=False, **kw)
        if e2["ok"]:
            edge = e2
    if edge is not None and edge["ok"]:
        if edge["n_edges"] >= 10 and edge["quality"] > 0.15:
            cands.append(edge)
        elif cen["ok"]:
            cands.append(edge)   # keep as a weak candidate
    if not cands:
        # fall back to the plain production centroid (always returns a value)
        c = _do_centroid(gray, x, y, r, probe, **kw)
        c.update(method="auto", reason="centroid_fallback", fallback="centroid")
        return c
    if len(cands) == 1:
        best = dict(cands[0])
        best.update(method="auto", requested="auto",
                    reason=("ok" if cands[0]["reason"] == "ok"
                            else cands[0]["reason"]))
        best["selected"] = cands[0]["method"]
        return best
    # choose the edge estimate unless it is clearly worse
    edge_c = next((c for c in cands if c["method"] in
                   ("conic", "conic_perspective")), None)
    cen_c = next((c for c in cands if c["method"] == "centroid_bg"), None)
    if edge_c is None:
        best = dict(cands[0])
        best.update(method="auto", requested="auto", selected=best["method"])
        return best
    if cen_c is None:
        best = dict(edge_c)
        best.update(method="auto", requested="auto", selected=edge_c["method"])
        return best
    # If the edge estimate carries a perspective correction, the same
    # correction applies to the centroid (the intensity centroid of a
    # projected disc sits at the ellipse centre, which is what the correction
    # moves).  Applying it keeps the two candidates on the same footing
    # instead of fusing an unbiased edge estimate with a biased centroid.
    corr = edge_c.get("correction")
    if corr:
        cen_c = dict(cen_c)
        cen_c["u"] = cen_c["u"] + float(corr[0])
        cen_c["v"] = cen_c["v"] + float(corr[1])
        cen_c["corrected"] = "perspective_shift"
    du = edge_c["u"] - cen_c["u"]
    dv = edge_c["v"] - cen_c["v"]
    d = math.hypot(du, dv)
    s_e = max(edge_c["sigma_px"], 1e-3)
    s_c = max(cen_c["sigma_px"], 1e-3)
    # Fuse only when the two *independent models* agree tightly.  A loose
    # threshold would let the centroid (tiny random sigma, 0.02-0.12 px
    # systematic on real frames) outvote the edge estimate.
    lim = max(0.05, 2.0 * math.hypot(s_e, s_c))
    if d <= lim:
        # inverse-variance fusion of two independent estimates
        w_e = 1.0 / s_e ** 2
        w_c = 1.0 / (s_c ** 2 + _CENTROID_SYSTEMATIC_FLOOR ** 2)
        u = (w_e * edge_c["u"] + w_c * cen_c["u"]) / (w_e + w_c)
        v = (w_e * edge_c["v"] + w_c * cen_c["v"]) / (w_e + w_c)
        res.update(u=float(u), v=float(v), x=float(u), y=float(v), ok=True,
                   reason="fused", method="auto",
                   selected="fuse(conic,centroid_bg)",
                   sigma_px=float(max(1.0 / math.sqrt(w_e + w_c), 0.5 * d)),
                   n_edges=int(edge_c["n_edges"]),
                   coverage=float(edge_c["coverage"]),
                   quality=_clip01(max(edge_c["quality"], cen_c["quality"]) * 0.98),
                   disagreement=float(d), radius_px=edge_c.get("radius_px"),
                   edge_method=edge_c["method"], circularity=edge_c.get("circularity"),
                   correction=edge_c.get("correction"), partial=edge_c.get("partial", False))
        return res
    # disagreement: trust the edge estimate (illumination/bloom robustness),
    # unless its support is poor
    if edge_c["n_edges"] >= 14 and edge_c["quality"] >= 0.3:
        best = dict(edge_c)
        best.update(method="auto", requested="auto", selected=edge_c["method"],
                    reason="disagree_edge", disagreement=float(d),
                    quality=_clip01(edge_c["quality"] * _clip01(0.5 / max(d, 0.5))))
        return best
    best = dict(cen_c)
    best.update(method="auto", requested="auto", selected="centroid_bg",
                reason="disagree_centroid", disagreement=float(d),
                quality=_clip01(cen_c["quality"] * _clip01(0.5 / max(d, 0.5))))
    return best


# ==========================================================================
# grid-level API
# ==========================================================================

@dataclass
class GridRefinement:
    """Refined grid of one view (uv -> pixel) plus per-marker diagnostics."""

    points: dict
    records: dict
    homography: np.ndarray
    rms: float
    max_residual: float
    method: str
    plane_normal: Optional[np.ndarray] = None
    stats: dict = field(default_factory=dict)

    @property
    def uv(self) -> np.ndarray:
        return np.array(sorted(self.points.keys()), float)

    @property
    def pixels(self) -> np.ndarray:
        return np.array([self.points[k] for k in sorted(self.points)], float)

    @property
    def n(self) -> int:
        return len(self.points)

    def as_dict(self) -> dict:
        return {"points": {f"{u},{v}": [float(p[0]), float(p[1])]
                           for (u, v), p in self.points.items()},
                "homography": np.asarray(self.homography).tolist(),
                "rms": float(self.rms), "max_residual": float(self.max_residual),
                "method": self.method,
                "plane_normal": (None if self.plane_normal is None
                                 else np.asarray(self.plane_normal).tolist()),
                "stats": self.stats}

    def as_observation(self):
        """Convert to :class:`calib.detect.GridObservation` (lazy import)."""
        from .detect import GridObservation
        uv = self.uv
        pix = self.pixels
        return GridObservation(
            points={tuple(k): np.asarray(v, float) for k, v in self.points.items()},
            homography=np.asarray(self.homography, float),
            n_lattice=len(self.points), rms=float(self.rms),
            max_residual=float(self.max_residual),
            big_markers=np.zeros((0, 2)), detector="localize-v1")


def _normalise_grid_input(obs_or_xy):
    """Return (uv (N,2), pixels (N,2), homography or None, meta)."""
    meta: dict = {}
    H = None
    if hasattr(obs_or_xy, "points") and hasattr(obs_or_xy, "homography"):
        pts = obs_or_xy.points
        H = np.asarray(obs_or_xy.homography, float)
        meta["source"] = "GridObservation"
        uv = np.array(sorted(pts.keys()), float)
        pix = np.array([pts[tuple(k)] for k in sorted(pts.keys())], float)
    elif isinstance(obs_or_xy, Mapping):
        if "points" in obs_or_xy:
            pts = obs_or_xy["points"]
            H = obs_or_xy.get("homography")
            uv = np.array(sorted(pts.keys()), float)
            pix = np.array([pts[tuple(k)] for k in sorted(pts.keys())], float)
            meta["source"] = "dict"
        else:
            uv = np.array(sorted(obs_or_xy.keys()), float)
            pix = np.array([obs_or_xy[tuple(k)] for k in sorted(obs_or_xy.keys())], float)
            meta["source"] = "dict"
    else:
        arr = np.asarray(obs_or_xy, float)
        if arr.ndim != 2:
            raise ValueError("obs_or_xy must be a GridObservation, dict or 2-D array")
        if arr.shape[1] == 4:
            uv = arr[:, :2].copy()
            pix = arr[:, 2:4].copy()
            meta["source"] = "Nx4"
        elif arr.shape[1] == 2:
            from . import plate
            uv = plate.lattice_uv()[: len(arr)].copy()
            pix = arr.copy()
            meta["source"] = "Nx2(lattice order)"
        elif arr.shape[1] == 3:
            raise ValueError("3-column input is ambiguous; pass (u, v, x, y) rows")
        else:
            raise ValueError(f"unsupported array shape {arr.shape}")
        if H is None and len(uv) >= 4:
            H, _ = cv2.findHomography(uv, pix, 0)
    if H is not None:
        H = np.asarray(H, float)
        if H.shape != (3, 3) or not np.isfinite(H).all():
            H = None
    if H is None and len(uv) >= 4:
        H, _ = cv2.findHomography(uv, pix, 0)
    return uv, pix, H, meta


def _local_scale(H: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """sqrt(|det J|) of the homography at each uv (pixels per board unit)."""
    h = np.asarray(H, float)
    u = uv[:, 0]
    v = uv[:, 1]
    den = h[2, 0] * u + h[2, 1] * v + h[2, 2]
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    # d(pixel)/d(u,v) of the projective map
    a = (h[0, 0] * den - (h[0, 0] * u + h[0, 1] * v + h[0, 2]) * h[2, 0]) / den ** 2
    b = (h[0, 1] * den - (h[0, 0] * u + h[0, 1] * v + h[0, 2]) * h[2, 1]) / den ** 2
    c = (h[1, 0] * den - (h[1, 0] * u + h[1, 1] * v + h[1, 2]) * h[2, 0]) / den ** 2
    d = (h[1, 1] * den - (h[1, 0] * u + h[1, 1] * v + h[1, 2]) * h[2, 1]) / den ** 2
    det = np.abs(a * d - b * c)
    return np.sqrt(np.maximum(det, 1e-12))


def refine_grid(gray: np.ndarray, obs_or_xy, method: str = "auto", *,
                K: Optional[np.ndarray] = None,
                kc: Optional[np.ndarray] = None,
                invert: Optional[bool] = None,
                radius_px: Optional[float | Mapping] = None,
                radius_over_pitch: float = RADIUS_OVER_PITCH,
                iterations: int = 2,
                include_off_lattice: bool = False,
                loo: bool = True,
                **kw) -> GridRefinement:
    """Refine a complete grid assignment with a chosen localiser.

    Parameters
    ----------
    gray : (H, W) array
        The frame.
    obs_or_xy : GridObservation | dict | (N,2) | (N,4) array
        The initial assignment.  ``GridObservation``/``dict`` give
        ``(u, v) -> (x, y)``; an ``(N,4)`` array is ``(u, v, x, y)``; an
        ``(N,2)`` array is interpreted in :func:`calib.plate.lattice_uv`
        order.  The initial positions only need to be good to ~1 px (they
        seed the localiser and the plane estimate).
    method : str
        Localiser (see :data:`METHODS`).  ``conic_perspective`` and ``auto``
        use the plane normal estimated from the grid homography (and the
        supplied ``K``/``kc``); without them they degrade to ``conic``.
    K, kc : array, optional
        Camera intrinsics/distortion.  Required for the undistorted fits
        (``conic``, ``conic_perspective``, and the best ``auto`` path).
    invert : bool, optional
        Polarity override.  Default: measured per marker from the radial
        profile (both polarities occur in the corpus).
    radius_px : float | dict, optional
        Marker radius override (px).  Default: measured per marker with a
        probe seeded from the local grid pitch
        (``radius_over_pitch * local pitch``), then the probe's own 50 %
        crossing.  A dict keyed by ``(u, v)`` overrides individual markers
        (e.g. the four large markers).
    radius_over_pitch : float
        Marker radius / marker pitch used to seed the radius probe when
        ``radius_px`` is not given (default 0.064, measured on the vendor
        corpus: r = 1.77 mm at a 27.72-28 mm pitch).  The probe scans up to
        2.5x the hint and retries wider if the edge is not found, so close
        range/large markers still work.
    iterations : int
        Homography/plane re-estimation passes (2 is enough; each pass
        re-localises every marker).
    loo : bool
        Also compute the leave-one-out homography residual per marker
        (``records[k]["loo_px"]``); costs one 40-point homography fit per
        marker.  Turn off for the live path.
    include_off_lattice : bool
        Also localise the off-lattice large marker ``(3.5, 3)``.  The
        production detector does not report it; the refined grid can, and
        the point is then available for validation (``calib.calibrate``
        ignores it because it has no object-point row).

    Returns
    -------
    GridRefinement
        ``points`` (uv -> pixel), ``records`` (uv -> per-marker dict),
        ``homography``, ``rms``/``max_residual`` of the final homography
        (in undistorted coordinates when ``K`` is given), ``plane_normal``
        and ``stats``.
    """
    gray = _as_gray(gray)
    loo_enabled = bool(loo)
    uv0, pix0, H, meta = _normalise_grid_input(obs_or_xy)
    if H is None:
        raise ValueError("need at least 4 grid points to fit a homography")
    if len(uv0) < 4:
        raise ValueError("need at least 4 grid points")
    pitch_px = float(np.median(_local_scale(H, uv0)))
    r_fixed = False
    if radius_px is None:
        r_base = max(2.0, radius_over_pitch * pitch_px)
        r_map = None
    elif isinstance(radius_px, Mapping):
        r_map = {tuple(float(a) for a in k): float(v) for k, v in radius_px.items()}
        r_base = float(np.median(list(r_map.values())))
    else:
        r_base = float(radius_px)
        r_map = None
        r_fixed = True

    big_key = (3.5, 3.0)
    cur: dict = {tuple(k): np.asarray(p, float) for k, p in zip(uv0, pix0)}
    if include_off_lattice and big_key not in cur:
        pred = cv2.perspectiveTransform(np.array([[[3.5, 3.0]]], np.float64), H).reshape(2)
        cur[big_key] = np.asarray(pred, float)
    keys_all = list(cur.keys())

    records: dict = {}
    points: dict = {}
    r_prev: dict = {}
    n_plane = None
    points_prev = None
    t_start = cv2.getTickCount()
    for it in range(max(1, int(iterations))):
        plane = None
        if method in ("conic_perspective", "auto") and K is not None and len(keys_all) >= 4:
            Uall = np.array(keys_all, float)
            Pall = np.array([cur[k] for k in keys_all], float)
            und = cv2.undistortPoints(Pall.reshape(-1, 1, 2).astype(np.float32),
                                      np.asarray(K, float),
                                      np.zeros(5) if kc is None else np.asarray(kc, float)
                                      ).reshape(-1, 2)
            Hn2, _ = cv2.findHomography(Uall, und, 0)
            if Hn2 is not None:
                n_plane = plane_normal_from_homography(Hn2)
        if n_plane is not None:
            plane = {"normal": np.asarray(n_plane, float)}
        new_pts: dict = {}
        new_rec: dict = {}
        h_img, w_img = gray.shape
        for key in keys_all:
            uv = np.array(key, float)
            if r_map is not None and key in r_map:
                r_m = r_map[key]
            elif key in r_prev:
                r_m = r_prev[key]
            elif r_fixed:
                r_m = r_base * (1.75 if key == big_key else 1.0)
            else:
                sc = float(_local_scale(H, uv.reshape(1, 2))[0])
                r_m = max(2.0, radius_over_pitch * sc)
                if key == big_key:
                    r_m *= 1.75
            x, y = cur[key]
            if not (-2.0 * r_m <= x <= w_img + 2.0 * r_m
                    and -2.0 * r_m <= y <= h_img + 2.0 * r_m):
                new_rec[key] = _result(method, method, x, y, ok=False,
                                       reason="outside_image",
                                       radius_px=float(r_m))
                cur[key] = np.array([x, y], float)
                continue
            res = localize_marker(gray, float(x), float(y), float(r_m),
                                  method=method, K=K, kc=kc, invert=invert,
                                  plane=plane, **kw)
            rec = dict(res)
            new_rec[key] = rec
            # the estimate (even a failed one) is the seed of the next pass
            cur[key] = np.array([rec["u"], rec["v"]], float)
            if rec["ok"]:
                new_pts[key] = np.array([rec["u"], rec["v"]], float)
                rr = rec.get("radius_px")
                if rr is not None and np.isfinite(rr) and float(rr) > 1.0:
                    r_prev[key] = float(np.clip(float(rr), 0.55 * r_m, 2.9 * r_m))
        keys = [k for k in new_pts if new_rec[k]["ok"]]
        if len(keys) < 6:
            records = new_rec
            break
        U = np.array(keys, float)
        P = np.array([new_pts[k] for k in keys], float)
        Hnew, _ = cv2.findHomography(U, P, 0)
        if Hnew is None:
            records = new_rec
            break
        # robust re-fit: drop gross outliers (a mis-assigned or merged blob),
        # then a least-squares homography over the inliers
        pred = cv2.perspectiveTransform(U.reshape(-1, 1, 2), Hnew).reshape(-1, 2)
        d = np.linalg.norm(pred - P, axis=1)
        keep = d < max(3.0 * _median(d), 1.0)
        if int(keep.sum()) >= 6:
            H2, _ = cv2.findHomography(U[keep], P[keep], 0)
            if H2 is not None:
                Hnew = H2
        H = Hnew
        points = {k: new_pts[k] for k in keys}
        records = new_rec
        if points_prev is not None:
            shared = [k for k in points if k in points_prev]
            if shared:
                moved = max(float(np.linalg.norm(points[k] - points_prev[k]))
                            for k in shared)
                if moved < 0.004:
                    break
        points_prev = dict(points)

    # ---- final residuals -------------------------------------------------
    keys = [k for k in points if records.get(k, {}).get("ok")]
    if keys:
        U = np.array(keys, float)
        P = np.array([points[k] for k in keys], float)
    else:
        U = np.zeros((0, 2))
        P = np.zeros((0, 2))
    res_px = np.zeros(len(keys))
    loo = np.full(len(keys), np.nan)
    if len(keys) >= 4:
        if K is not None:
            und = cv2.undistortPoints(P.reshape(-1, 1, 2).astype(np.float32),
                                      np.asarray(K, float),
                                      np.zeros(5) if kc is None else np.asarray(kc, float)
                                      ).reshape(-1, 2)
            Hu, _ = cv2.findHomography(U, und, 0)
            f = float(np.sqrt(abs(np.asarray(K, float)[0, 0] * np.asarray(K, float)[1, 1])))
            if Hu is not None:
                pred = cv2.perspectiveTransform(U.reshape(-1, 1, 2), Hu).reshape(-1, 2)
                res_px = np.linalg.norm(pred - und, axis=1) * f
                if loo_enabled:
                    for i in range(len(keys)):
                        sel = np.ones(len(keys), bool)
                        sel[i] = False
                        Hi, _ = cv2.findHomography(U[sel], und[sel], 0)
                        if Hi is None:
                            continue
                        pi = cv2.perspectiveTransform(U[i:i + 1].reshape(-1, 1, 2), Hi).reshape(2)
                        loo[i] = float(np.linalg.norm(pi - und[i]) * f)
        else:
            pred = cv2.perspectiveTransform(U.reshape(-1, 1, 2), H).reshape(-1, 2)
            res_px = np.linalg.norm(pred - P, axis=1)
        for i, k in enumerate(keys):
            records[k]["residual_px"] = float(res_px[i])
            records[k]["loo_px"] = float(loo[i])
    rms = float(np.sqrt(np.mean(res_px ** 2))) if len(res_px) else float("nan")
    mx = float(np.max(res_px)) if len(res_px) else float("nan")
    ok_recs = [records[k] for k in records if records[k]["ok"]]
    sig = np.array([r["sigma_px"] for r in ok_recs
                    if np.isfinite(r.get("sigma_px", np.nan))], float)
    elapsed = (cv2.getTickCount() - t_start) / cv2.getTickFrequency() * 1e3
    stats = {
        "source": meta.get("source"),
        "n": len(points), "n_ok": len(ok_recs), "n_requested": len(new_rec),
        "n_failed": len(new_rec) - len(ok_recs),
        "n_partial": int(sum(bool(r.get("partial")) for r in ok_recs)),
        "n_saturated": int(sum(bool(r.get("saturated")) for r in ok_recs)),
        "n_fallback": int(sum(bool(r.get("fallback")) for r in ok_recs)),
        "sigma_median": float(np.median(sig)) if sig.size else float("nan"),
        "sigma_p90": float(np.percentile(sig, 90)) if sig.size else float("nan"),
        "quality_median": float(np.median([r["quality"] for r in ok_recs])) if ok_recs else 0.0,
        "radius_median": float(np.median([r["radius_px"] for r in ok_recs
                                          if np.isfinite(r.get("radius_px", np.nan))]))
        if ok_recs else float("nan"),
        "pitch_px": pitch_px,
        "runtime_ms": elapsed,
        "runtime_ms_per_marker": elapsed / max(len(new_rec), 1),
        "method": method,
        "reasons": {r: int(sum(1 for xr in new_rec.values() if xr["reason"] == r))
                    for r in sorted({xr["reason"] for xr in new_rec.values()})},
    }
    return GridRefinement(points=points, records=records, homography=H,
                          rms=rms, max_residual=mx, method=method,
                          plane_normal=None if n_plane is None else np.asarray(n_plane, float),
                          stats=stats)


def refine_observation(gray: np.ndarray, obs, method: str = "auto", **kw):
    """Refine a :class:`calib.detect.GridObservation` and return a new one."""
    return refine_grid(gray, obs, method=method, **kw).as_observation()
