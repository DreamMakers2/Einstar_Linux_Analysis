"""Python replica of ``recon/src/stereo.cpp`` for the knobs the CLI does not expose.

Why this exists
---------------
``recon_cli`` exposes ``--scale``, ``--min-depth/--max-depth``, ``--block`` and
``--equalize``.  It does **not** expose the left/right consistency tolerance, the
SGBM uniqueness/speckle parameters or a preprocessing hook, so those cannot be
swept through the real binary.  This module reproduces the C++ matcher
line by line (same padded SGBM, same sub-pixel refinement, same L/R test, same
confidence formula) and is **validated against the real CLI** on the same frames
before any of its numbers are used: see ``experiments.json ->
replica_validation`` (mask IoU, median disparity difference on co-valid pixels).
The sweep itself never uses this code.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .geometry_py import ScaledGeometry


@dataclass
class PyStereoOptions:
    block_size: int = 5
    p1_factor: int = 8
    p2_factor: int = 96
    uniqueness_ratio: int = 10
    speckle_window: int = 100
    speckle_range: int = 2
    prefilter_cap: int = 63
    lr_tolerance_px: float = 1.0
    min_depth_mm: float = 150.0
    max_depth_mm: float = 800.0
    subpixel: bool = True
    preprocess: str = "none"       # none | clahe | log | gamma05 | inverse


def _preprocess(img: np.ndarray, mode: str) -> np.ndarray:
    if mode == "none":
        return img
    if mode == "clahe":
        return cv2.createCLAHE(3.0, (16, 16)).apply(img)
    if mode == "log":
        # log response: compresses the bright specular speckle, lifts shadows
        f = np.log1p(img.astype(np.float32) / 8.0)
        f = (f / max(float(f.max()), 1e-6)) * 255.0
        return np.clip(np.round(f), 0, 255).astype(np.uint8)
    if mode == "gamma05":
        lut = np.array([((i / 255.0) ** 0.5) * 255.0 for i in range(256)]).astype(np.uint8)
        return cv2.LUT(img, lut)
    if mode == "inverse":
        # inverse sensor response (linearise an assumed gamma 0.45 encoding)
        lut = np.array([((i / 255.0) ** (1.0 / 0.45)) * 255.0 for i in range(256)]).astype(np.uint8)
        return cv2.LUT(img, lut)
    raise ValueError(f"unknown preprocess {mode!r}")


def _match_padded(left: np.ndarray, right: np.ndarray, min_disp: int, num_disp: int,
                  o: PyStereoOptions, block: int) -> np.ndarray:
    pad_l = max(0, -min_disp)
    pad_r = max(0, min_disp)
    off_l = num_disp + pad_l
    off_r = num_disp + pad_r
    width = max(left.shape[1], right.shape[1]) + num_disp + max(pad_l, pad_r) + 2
    lpad = np.zeros((left.shape[0], width), np.uint8)
    rpad = np.zeros((right.shape[0], width), np.uint8)
    lpad[:, off_l:off_l + left.shape[1]] = left
    rpad[:, off_r:off_r + right.shape[1]] = right
    sgbm = cv2.StereoSGBM_create(
        minDisparity=0, numDisparities=int(num_disp), blockSize=int(block),
        P1=int(o.p1_factor * block * block), P2=int(o.p2_factor * block * block),
        disp12MaxDiff=-1, preFilterCap=int(o.prefilter_cap),
        uniquenessRatio=int(o.uniqueness_ratio),
        speckleWindowSize=int(o.speckle_window), speckleRange=int(o.speckle_range),
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
    d16 = sgbm.compute(lpad, rpad)
    d = d16[:, off_l:off_l + left.shape[1]].astype(np.float32) / 16.0
    d[d < 0] = -1.0
    d += float(min_disp)
    return d


def _refine_subpixel(d: np.ndarray) -> np.ndarray:
    src = d.copy()
    out = d.copy()
    c = src[1:-1, 1:-1]
    l, r = src[1:-1, :-2], src[1:-1, 2:]
    t, b = src[:-2, 1:-1], src[2:, 1:-1]
    offset = np.zeros_like(c)
    fits = np.zeros_like(c)
    den = l - 2 * c + r
    m = (l >= 0) & (r >= 0) & (den > 1e-6)
    offset = np.where(m, 0.5 * (l - r) / np.where(m, den, 1.0), 0.0)
    fits += m
    den2 = t - 2 * c + b
    m2 = (t >= 0) & (b >= 0) & (den2 > 1e-6)
    offset += np.where(m2, 0.5 * (t - b) / np.where(m2, den2, 1.0), 0.0)
    fits += m2
    ok = (fits > 0) & (c >= 0)
    off = np.where(ok, offset / np.maximum(fits, 1), 0.0)
    off = np.clip(off, -0.5 / 16.0, 0.5 / 16.0)
    block = out[1:-1, 1:-1]
    block[ok] = c[ok] + off[ok]
    out[1:-1, 1:-1] = block
    return out


def _sample_disparity(d: np.ndarray, x: np.ndarray, y: np.ndarray):
    """Bilinear sample with the C++ rule: any non-negligible invalid neighbour
    invalidates the sample."""
    h, w = d.shape
    inside = (x >= 0) & (y >= 0) & (x <= w - 1.001) & (y <= h - 1.001)
    xc = np.clip(x, 0, w - 1.001)
    yc = np.clip(y, 0, h - 1.001)
    x0 = np.floor(xc).astype(np.int32)
    y0 = np.floor(yc).astype(np.int32)
    fx = xc - x0
    fy = yc - y0
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    w00 = (1 - fx) * (1 - fy)
    w10 = fx * (1 - fy)
    w01 = (1 - fx) * fy
    w11 = fx * fy
    v00 = d[y0, x0]
    v10 = d[y0, x1]
    v01 = d[y1, x0]
    v11 = d[y1, x1]
    bad = ((w00 > 1e-4) & (v00 < 0)) | ((w10 > 1e-4) & (v10 < 0)) | \
          ((w01 > 1e-4) & (v01 < 0)) | ((w11 > 1e-4) & (v11 < 0))
    weight = w00 * (w00 > 1e-4) + w10 * (w10 > 1e-4) + \
             w01 * (w01 > 1e-4) + w11 * (w11 > 1e-4)
    s = w00 * np.where(w00 > 1e-4, v00, 0) + w10 * np.where(w10 > 1e-4, v10, 0) + \
        w01 * np.where(w01 > 1e-4, v01, 0) + w11 * np.where(w11 > 1e-4, v11, 0)
    have = inside & (~bad) & (weight > 0)
    value = np.where(have, s / np.maximum(weight, 1e-12), 0.0)
    return have, value


def compute_stereo_py(left_rect: np.ndarray, right_rect: np.ndarray,
                      valid_left: np.ndarray, valid_right: np.ndarray,
                      geom: ScaledGeometry,
                      o: PyStereoOptions | None = None) -> dict:
    """Same outputs as ``einstar::recon::compute_stereo`` (map values, 0=invalid)."""
    o = o or PyStereoOptions()
    block = max(3, o.block_size | 1)
    size = (geom.width, geom.height)
    scale = geom.scale
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    left = cv2.resize(left_rect, size, interpolation=interp)
    right = cv2.resize(right_rect, size, interpolation=interp)

    def scaled_mask(mask):
        s = cv2.resize(mask, size, interpolation=cv2.INTER_AREA)
        return (s >= 254).astype(np.uint8)

    mask_left = scaled_mask(valid_left)
    mask_right = scaled_mask(valid_right)
    if o.preprocess != "none":
        left = _preprocess(left, o.preprocess)
        right = _preprocess(right, o.preprocess)

    min_disp, num_disp = geom.window(o.min_depth_mm, o.max_depth_mm)
    disp_left = _match_padded(left, right, min_disp, num_disp, o, block)
    mirrored = _match_padded(cv2.flip(right, 1), cv2.flip(left, 1),
                             min_disp, num_disp, o, block)
    disp_right = cv2.flip(mirrored, 1)
    if o.subpixel:
        disp_left = _refine_subpixel(disp_left)
        disp_right = _refine_subpixel(disp_right)
    disp_right[mask_right == 0] = -1.0

    h, w = size[1], size[0]
    yy, xx = np.mgrid[0:h, 0:w]
    d = disp_left
    cand = (d > 0) & (mask_left > 0)
    ur = xx.astype(np.float64) - d.astype(np.float64)
    have, dr = _sample_disparity(disp_right, ur, yy.astype(np.float64))
    diff = np.abs(d.astype(np.float64) - dr)
    consistent = cand & have & (diff <= o.lr_tolerance_px)

    lo = float(min_disp)
    hi = float(min_disp + num_disp)
    hard_margin = max(2.0, 0.005 * num_disp)
    soft_margin = max(2.0, 0.02 * num_disp)
    censored = (d < lo + hard_margin) | (d > hi - hard_margin)
    d_out = d.astype(np.float64) + lo
    valid = consistent & (~censored) & (d_out > 0)

    left_f = left.astype(np.float32)
    mean = cv2.blur(left_f, (9, 9))
    mean_sq = cv2.blur(left_f * left_f, (9, 9))
    var = np.maximum(0.0, mean_sq.astype(np.float64) - mean.astype(np.float64) ** 2)
    texture = 1.0 - np.exp(-var / (2.0 * 6.0 * 6.0))
    sigma_lr = max(o.lr_tolerance_px / 3.0, 1e-3)
    lr_term = np.exp(-0.5 * (diff / sigma_lr) ** 2)
    border = np.clip((d - (lo + soft_margin)) / soft_margin, 0, 1) * \
        np.clip(((hi - soft_margin) - d) / soft_margin, 0, 1)
    conf = np.clip(border * (0.65 * texture + 0.35 * lr_term), 0, 1)
    conf[~valid] = 0.0

    disparity = np.zeros((h, w), np.float32)
    disparity[valid] = d_out[valid].astype(np.float32)
    confidence = np.where(valid, conf, 0.0).astype(np.float32)
    occluded = (cand & ~consistent)
    return {
        "disparity": disparity,
        "confidence": confidence.astype(np.float32),
        "valid": (valid.astype(np.uint8) * 255),
        "occluded": (occluded.astype(np.uint8) * 255),
        "min_disparity": int(min_disp),
        "num_disparities": int(num_disp),
        "valid_fraction": float(valid.mean()),
        "occluded_fraction": float(occluded.mean()),
    }


def depth_map_py(stereo: dict, geom: ScaledGeometry, R1: np.ndarray) -> np.ndarray:
    """Reproduce ``compute_depth``'s ``z_mm`` map (depth along the left camera z).

    ``recon_depth_mm.tiff`` stores ``pl[2]`` with ``pl = R1^T [x_r, y_r, z]`` in
    millimetres; this is the same quantity, so the harness can treat a replica
    run exactly like a CLI run.
    """
    disp = stereo["disparity"]
    v, u = np.nonzero(stereo["valid"] > 0)
    z_mm = np.zeros(disp.shape, np.float32)
    if len(v) == 0:
        return z_mm
    # stereo.cpp adds the search-window minimum to geometry.disp_offset, so the
    # physical disparity is (map value - disp_offset - min_disparity).
    offset = geom.disp_offset + float(stereo.get("min_disparity", 0))
    zr = geom.f * geom.baseline_mm / (disp[v, u].astype(np.float64) - offset)
    a = (u.astype(np.float64) - geom.cx) / geom.f
    b = (v.astype(np.float64) - geom.cy) / geom.f
    q = np.stack([a, b, np.ones_like(a)], axis=1) @ np.asarray(R1, np.float64)
    zl = zr * q[:, 2]
    good = np.isfinite(zl) & (zl > 50) & (zl < 5000)
    z_mm[v[good], u[good]] = zl[good].astype(np.float32)
    return z_mm
