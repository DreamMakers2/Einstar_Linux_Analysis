#!/usr/bin/env python3
"""Real-capture evaluation of the Einstar reconstruction engine.

What this tool is
-----------------
An **independent** re-implementation of the reconstruction front end in
Python/OpenCV (rectification + SGBM; no shared C++ code), run on the three
genuine private triplets in ``<capture-dir>`` and
compared with the C++ ``recon_cli`` when that backend is available.

It is *not* an accuracy measurement.  The scene is a box on a desk, not a
calibrated artefact, and the only calibration is the factory profile recovered
from another run.  Every number it produces is a **consistency** or
**repeatability** observation, labelled as such in the JSON and in
``docs/reconstruction/real_data_report.md``.  Physical accuracy needs an
independent measuring device; synthetic truth needs a rendered scene with known
geometry (the C++ synthetic backend covers the geometry chain, not yet SGBM).

What it measures
----------------
* per-frame coverage, depth percentiles, disparity, invalid/occluded maps;
* per-pixel agreement with the C++ output (when the backend is built);
* repeatability across the three frames (depth distributions, cross-frame
  depth disagreement, RANSAC plane fits on the dominant box-top face);
* ablations (scale, CLAHE, block size, depth range): coverage, depth median,
  plane RMS, runtime;
* confidence calibration (L-R residual and *independent* plane / cross-frame
  residuals per confidence bin);
* edge/occlusion diagnostics, point-cloud nearest-neighbour spacing;
* peak RSS and per-stage timing.

Privacy
-------
The private frames are only read.  Nothing derived from them is written into
the repository except *aggregate* numbers and histograms; point clouds stay in
``/tmp/einstar_recon``.  ``--check-no-private-export`` verifies that no image or
PLY exists under ``docs/``.

Usage
-----
    python analysis/reconstruction/evaluate_real.py
    python analysis/reconstruction/evaluate_real.py --quick      # no ablations
    python analysis/reconstruction/evaluate_real.py --cpp on     # require C++
    python analysis/reconstruction/evaluate_real.py --cpp-only

Exit codes: 0 success, 2 unusable input, 3 ``--cpp on`` but the backend is a
stub / missing, 4 private export found under ``docs/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

try:  # optional: point-cloud spacing only
    from scipy.spatial import cKDTree
except Exception:  # pragma: no cover
    cKDTree = None

# --------------------------------------------------------------------------
# constants (paths, stream mapping, declared defaults)
# --------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURE = Path("<capture-dir>")
DEFAULT_PROFILE = Path("<private-inputs>/einstar-profiles/factory-e10.json")
DEFAULT_TMP = Path("/tmp/einstar_recon")
DEFAULT_JSON = REPO / "analysis/reconstruction/real_data_metrics.json"
DEFAULT_FIGS = REPO / "analysis/reconstruction/figures"
CLI = REPO / "build/recon/recon_cli"

#: USB stream -> (calibration camera, needs 180 deg rotation).
#: This is a hardware fact (libeinstar reference + audit.md), not an
#: assumption: stream 0 is the RIGHT IR camera stored rotated, stream 1 the
#: LEFT IR camera as calibrated, stream 2 the colour camera stored rotated.
STREAM_MAP = {0: ("ir_right", True), 1: ("ir_left", False), 2: ("color", True)}


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def peak_rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def fnum(v: Any, nd: int = 4) -> Any:
    """JSON-safe float rounding."""
    if isinstance(v, (np.floating, float)):
        v = float(v)
        return None if not math.isfinite(v) else round(v, nd)
    if isinstance(v, (np.integer, int)):
        return int(v)
    return v


def json_default(o: Any) -> Any:
    """JSON fallback for numpy scalars/arrays (never used for images)."""
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def pcts(x: np.ndarray, qs=(5, 50, 95)) -> list[float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return [float("nan")] * len(qs)
    return [float(np.percentile(x, q)) for q in qs]


# --------------------------------------------------------------------------
# PGM + profile
# --------------------------------------------------------------------------

def read_pgm(path: Path) -> np.ndarray:
    """Binary (P5) 8-bit PGM with comments; mirrors recon/src/frame_set.cpp."""
    data = path.read_bytes()
    if data[:2] != b"P5":
        raise ValueError(f"{path}: not a binary PGM (P5)")
    pos, fields = 2, []
    while len(fields) < 3:
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            while data[pos:pos + 1] not in (b"\n", b""):
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos:pos + 1].isspace():
            pos += 1
        fields.append(data[start:pos])
    pos += 1
    w, h, maxv = int(fields[0]), int(fields[1]), int(fields[2])
    if w <= 0 or h <= 0 or maxv <= 0 or maxv > 255:
        raise ValueError(f"{path}: unsupported PGM dimensions/maxval")
    payload = data[pos:pos + w * h]
    if len(payload) != w * h:
        raise ValueError(f"{path}: truncated PGM payload")
    return np.frombuffer(payload, np.uint8).reshape(h, w).copy()


def load_profile(path: Path) -> dict[str, Any]:
    prof = json.loads(path.read_text())
    if prof.get("schema") != "einstar.recon.profile/1":
        raise ValueError(f"{path}: unexpected profile schema")
    return prof


def cam_matrix(c: dict[str, Any]) -> np.ndarray:
    return np.array([[c["fx"], c["skew"], c["cx"]],
                     [0.0, c["fy"], c["cy"]],
                     [0.0, 0.0, 1.0]], float)


def cam_dist(c: dict[str, Any]) -> np.ndarray:
    return np.array(c["dist"], float).reshape(1, 5)


def cam_pose(c: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    return np.array(c["R"], float).reshape(3, 3), np.array(c["T"], float)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

@dataclass
class Geometry:
    """Rectified left intrinsics at full and scaled resolution."""

    size_full: tuple[int, int]
    f_full: float
    cx_full: float
    cy_full: float
    baseline_mm: float
    disp_offset_full: float
    R1: np.ndarray
    map1x: np.ndarray
    map1y: np.ndarray
    map2x: np.ndarray
    map2y: np.ndarray
    valid_left: np.ndarray
    valid_right: np.ndarray
    scale: float = 0.5

    def scaled(self) -> tuple[float, float, float, float, int, int]:
        s = self.scale
        w, h = self.size_full
        return (self.f_full * s, self.cx_full * s, self.cy_full * s,
                self.disp_offset_full * s, int(round(w * s)), int(round(h * s)))

    def depth_at(self, disp: np.ndarray) -> np.ndarray:
        f, _, _, off, _, _ = self.scaled()
        with np.errstate(divide="ignore", invalid="ignore"):
            return f * self.baseline_mm / (disp - off)

    def disparity_at(self, z_mm: float) -> float:
        f, _, _, off, _, _ = self.scaled()
        return f * self.baseline_mm / z_mm + off


def build_geometry(profile: dict[str, Any], scale: float,
                   alpha: float = -1.0, border_erode: int = 5) -> Geometry:
    left = profile["cameras"]["ir_left"]
    right = profile["cameras"]["ir_right"]
    K1, D1 = cam_matrix(left), cam_dist(left)
    K2, D2 = cam_matrix(right), cam_dist(right)
    Rl, Tl = cam_pose(left)
    Rr, Tr = cam_pose(right)
    R_rel = Rr @ Rl.T                       # X_right = R_rel X_left + T_rel
    T_rel = Tr - R_rel @ Tl
    size = (int(profile["image_width"]), int(profile["image_height"]))
    # OpenCV 5 requires a (3,1) translation (a (3,) vector trips a gemm assert).
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        K1, D1, K2, D2, size, R_rel, T_rel.reshape(3, 1),
        flags=cv2.CALIB_ZERO_DISPARITY, alpha=alpha, newImageSize=size,
    )
    if not np.isfinite(P1).all() or P1[0, 0] <= 0:
        raise RuntimeError("stereoRectify produced invalid intrinsics")
    m1x, m1y = cv2.initUndistortRectifyMap(K1, D1, R1, P1, size, cv2.CV_32FC1)
    m2x, m2y = cv2.initUndistortRectifyMap(K2, D2, R2, P2, size, cv2.CV_32FC1)
    white = np.full((size[1], size[0]), 255, np.uint8)
    vl = cv2.remap(white, m1x, m1y, cv2.INTER_NEAREST,
                   borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    vr = cv2.remap(white, m2x, m2y, cv2.INTER_NEAREST,
                   borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    if border_erode > 0:
        k = np.ones((border_erode, border_erode), np.uint8)
        vl = cv2.erode(vl, k)
        vr = cv2.erode(vr, k)
    baseline = float(-P2[0, 3] / P2[0, 0])
    return Geometry(size_full=size, f_full=float(P1[0, 0]), cx_full=float(P1[0, 2]),
                    cy_full=float(P1[1, 2]), baseline_mm=baseline,
                    disp_offset_full=float(P1[0, 2] - P2[0, 2]),
                    R1=np.asarray(R1, float), map1x=m1x, map1y=m1y,
                    map2x=m2x, map2y=m2y, valid_left=vl, valid_right=vr,
                    scale=scale)


def load_triplet(capture: Path, frame_id: int) -> dict[str, Any]:
    """Load one triplet with the calibration stream mapping applied once."""
    out: dict[str, Any] = {"id": frame_id, "files": {}}
    for stream, (camera, rotate) in STREAM_MAP.items():
        if camera == "color":
            continue                      # factory colour record is stale
        path = capture / f"frame_{frame_id:06d}_cam{stream}.pgm"
        if not path.exists():
            raise FileNotFoundError(path)
        img = read_pgm(path)
        if rotate:
            img = cv2.rotate(img, cv2.ROTATE_180)
        out[camera] = img
        out["files"][camera] = {"path": str(path), "sha256": sha256_file(path)}
    # hash what the C++ hashes: decoded pixels of streams 0,1,2 in stream order
    h = hashlib.sha256()
    for s in (0, 1, 2):
        h.update(read_pgm(capture / f"frame_{frame_id:06d}_cam{s}.pgm").tobytes())
    out["pixel_sha256_stream_order"] = h.hexdigest()
    return out


def rectify_triplet(triplet: dict[str, Any], geom: Geometry
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    left = cv2.remap(triplet["ir_left"], geom.map1x, geom.map1y,
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    right = cv2.remap(triplet["ir_right"], geom.map2x, geom.map2y,
                      cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return left, right, geom.valid_left, geom.valid_right


def downsample(img: np.ndarray, scale: float, nearest: bool = False) -> np.ndarray:
    if scale == 1.0:
        return img
    w = int(round(img.shape[1] * scale))
    h = int(round(img.shape[0] * scale))
    interp = cv2.INTER_NEAREST if nearest else cv2.INTER_AREA
    return cv2.resize(img, (w, h), interpolation=interp)


# --------------------------------------------------------------------------
# stereo matching (independent implementation of the declared contract)
# --------------------------------------------------------------------------

@dataclass
class StereoConfig:
    """Declared defaults of einstar::recon::StereoOptions + search policy."""

    scale: float = 0.5
    block_size: int = 5
    p1_factor: int = 8
    p2_factor: int = 96
    uniqueness_ratio: int = 10
    speckle_window: int = 100
    speckle_range: int = 2
    equalize: bool = False
    min_depth_mm: float = 150.0
    max_depth_mm: float = 800.0
    lr_tolerance_px: float = 1.0
    min_disparity_alignment: int = 16
    search_margin_px: int = 0
    #: SGBM in OpenCV 5 labels a pixel valid only where the *whole* disparity
    #: window fits (x >= min_disp + num_disp).  The production contract is a
    #: padded search: replicate-pad both images on the left by the window
    #: width, match, then crop.  Without it a 640 px half-res frame with a
    #: 112..624 window keeps only 16 valid columns (2.5 %).
    padded_search: bool = True


def build_search(geom: Geometry, cfg: StereoConfig, width: int) -> dict[str, Any]:
    s = cfg.scale
    f = geom.f_full * s
    b = geom.baseline_mm
    off = geom.disp_offset_full * s
    align = cfg.min_disparity_alignment
    d_far = f * b / cfg.max_depth_mm + off
    d_near = f * b / cfg.min_depth_mm + off
    lo = max(0, int(math.floor((d_far - cfg.search_margin_px) / align) * align))
    hi = int(math.ceil((d_near + cfg.search_margin_px) / align) * align)
    num = int(math.ceil(max(hi - lo, align) / align) * align)
    return {
        "min_disparity": lo,
        "num_disparities": num,
        "disparity_max": lo + num,
        "near_depth_mm": fnum(f * b / max(lo + num - 1, 1), 2),
        "far_depth_mm": fnum(f * b / max(lo, 1), 2) if lo > 0 else None,
        "search_margin_px": cfg.search_margin_px,
        "padded_search": cfg.padded_search,
        "pad_px": (lo + num) if cfg.padded_search else 0,
        "width": width,
        "near_limit_clamped": bool(lo + num > width - cfg.block_size // 2),
    }


def _prep_pair(left: np.ndarray, right: np.ndarray, cfg: StereoConfig,
               pad: int) -> tuple[np.ndarray, np.ndarray]:
    if cfg.equalize:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        left = clahe.apply(left)
        right = clahe.apply(right)
    if pad > 0:
        left = cv2.copyMakeBorder(left, 0, 0, pad, 0, cv2.BORDER_REPLICATE)
        right = cv2.copyMakeBorder(right, 0, 0, pad, 0, cv2.BORDER_REPLICATE)
    return left, right


def _sgbm(cfg: StereoConfig, search: dict[str, Any]):
    bs = cfg.block_size
    return cv2.StereoSGBM_create(
        minDisparity=search["min_disparity"],
        numDisparities=search["num_disparities"],
        blockSize=bs,
        P1=cfg.p1_factor * bs * bs,
        P2=cfg.p2_factor * bs * bs,
        disp12MaxDiff=-1,                 # L-R done explicitly at sub-pixel
        preFilterCap=63,
        uniquenessRatio=cfg.uniqueness_ratio,
        speckleWindowSize=cfg.speckle_window,
        speckleRange=cfg.speckle_range,
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
    )


def _sgbm_reverse(right_pad: np.ndarray, left_pad: np.ndarray,
                  cfg: StereoConfig, search: dict[str, Any],
                  width: int) -> np.ndarray:
    """Right-to-left disparity by mirroring (SGBM always searches the left).

    The padded pair already carries ``pad`` replicated columns on the *left*;
    for the mirrored pass the pad must sit on the left of the mirrored image, so
    the pair is padded on the right first.  After matching, the result is
    mirrored back and cropped to the original width.
    """
    pad = search["pad_px"]
    w = width + pad
    rp = cv2.copyMakeBorder(right_pad, 0, 0, 0, pad, cv2.BORDER_REPLICATE)
    lp = cv2.copyMakeBorder(left_pad, 0, 0, 0, pad, cv2.BORDER_REPLICATE)
    d = _sgbm(cfg, search).compute(cv2.flip(rp, 1), cv2.flip(lp, 1))
    d = cv2.flip(d.astype(np.float32) / 16.0, 1)
    return d[:, pad:pad + width]


def match_pair(left_rect: np.ndarray, right_rect: np.ndarray, geom: Geometry,
               cfg: StereoConfig) -> dict[str, Any]:
    """Left->right and right->left SGBM with the same window."""
    t0 = time.perf_counter()
    left = downsample(left_rect, cfg.scale)
    right = downsample(right_rect, cfg.scale)
    h, w = left.shape
    search = build_search(geom, cfg, w)
    pad = search["pad_px"]
    lp, rp = _prep_pair(left, right, cfg, pad)
    t0b = time.perf_counter()
    dl = _sgbm(cfg, search).compute(lp, rp).astype(np.float32) / 16.0
    dl = dl[:, pad:pad + w]
    t1 = time.perf_counter()
    dr = _sgbm_reverse(rp, lp, cfg, search, w)
    t2 = time.perf_counter()
    md = search["min_disparity"]
    return {"left": left, "right": right, "disparity": dl, "disparity_right": dr,
            "valid_raw": dl > md - 0.5, "search": search,
            "timing": {"sgbm_left_s": t1 - t0b, "sgbm_right_s": t2 - t1,
                       "sgbm_total_s": t2 - t0b, "prep_s": t0b - t0}}


def lr_consistency(match: dict[str, Any], cfg: StereoConfig,
                   valid_left: np.ndarray, valid_right: np.ndarray) -> dict[str, Any]:
    """Sub-pixel left-right check + confidence.

    Confidence = LR-agreement factor x local-contrast factor.  The L-R factor
    is by construction a function of the residual (a tautological relation the
    report says so); the contrast factor is independent of it.
    """
    dl, dr = match["disparity"], match["disparity_right"]
    h, w = dl.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    xr = xx - dl
    md = match["search"]["min_disparity"]
    dr_valid = (dr > md - 0.5).astype(np.float32)
    x0 = np.floor(xr).astype(np.int32)
    ok = (x0 >= 0) & (x0 + 1 < w)
    fx = np.clip(xr - x0, 0.0, 1.0)
    x0c = np.clip(x0, 0, w - 2)
    d00, d01 = dr[yy.astype(np.int32), x0c], dr[yy.astype(np.int32), x0c + 1]
    dr_at = d00 * (1 - fx) + d01 * fx
    v00, v01 = dr_valid[yy.astype(np.int32), x0c], dr_valid[yy.astype(np.int32), x0c + 1]
    dr_ok = (v00 * (1 - fx) + v01 * fx) > 0.99
    dl_valid = match["valid_raw"] & (dl > 0)
    residual = np.abs(dl - dr_at)
    consistent = dl_valid & ok & dr_ok & (residual <= cfg.lr_tolerance_px)
    occluded = dl_valid & ok & dr_ok & (residual > cfg.lr_tolerance_px)
    xr_i = np.clip(np.rint(xr).astype(np.int32), 0, w - 1)
    source_ok = (valid_left > 0) & (valid_right[yy.astype(np.int32), xr_i] > 0)
    valid = consistent & source_ok

    lf = match["left"].astype(np.float32)
    mu = cv2.blur(lf, (7, 7))
    std = np.sqrt(np.maximum(cv2.blur(lf * lf, (7, 7)) - mu * mu, 0.0))
    tex_conf = np.clip((std - 1.5) / 6.5, 0.0, 1.0)
    lr_conf = np.clip(1.0 - residual / (2.0 * cfg.lr_tolerance_px), 0.0, 1.0)
    confidence = lr_conf * tex_conf
    confidence[~valid] = 0.0
    return {"residual": residual, "valid": valid, "occluded": occluded,
            "source_ok": source_ok, "confidence": confidence, "texture_std": std,
            "n_valid_raw": int(dl_valid.sum()), "n_consistent": int(consistent.sum()),
            "n_occluded": int(occluded.sum())}


def depth_map(match: dict[str, Any], lr: dict[str, Any], geom: Geometry,
              cfg: StereoConfig) -> dict[str, Any]:
    """Metric points in the LEFT IR camera frame.

    The disparity is measured in the rectified frame, so the range test uses
    the rectified depth; the stored z is the left-IR optical-axis depth
    ``(R1^T p_rect).z``, which is the convention of DepthResult::z_mm and of the
    C++ point cloud (they are the same number, not two frames).
    """
    dl = match["disparity"]
    f, cx, cy, _, w, h = geom.scaled()
    z_rect = geom.depth_at(dl)
    valid = (lr["valid"] & np.isfinite(z_rect)
             & (z_rect >= cfg.min_depth_mm) & (z_rect <= cfg.max_depth_mm))
    u = np.arange(w, dtype=np.float32)[None, :].repeat(h, axis=0)
    v = np.arange(h, dtype=np.float32)[:, None].repeat(w, axis=1)
    x = (u - cx) * z_rect / f
    y = (v - cy) * z_rect / f
    xyz = np.stack([x, y, z_rect], axis=-1).reshape(-1, 3) @ geom.R1   # R1^T p
    xyz = xyz.reshape(h, w, 3)
    z = np.where(valid, xyz[:, :, 2], 0.0).astype(np.float32)
    xyz[~valid] = 0.0
    return {"z": z, "z_rect": z_rect, "xyz": xyz, "valid": valid}


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def ransac_plane(points: np.ndarray, thresh_mm: float = 2.0, iters: int = 600,
                 seed: int = 0, min_inlier_frac: float = 0.05) -> dict[str, Any] | None:
    """Largest plane by RANSAC, refined by SVD on the inliers."""
    pts = np.asarray(points, float)
    pts = pts[np.isfinite(pts).all(1)]
    if len(pts) < 2000:
        return None
    rng = np.random.default_rng(seed)
    sample = pts[rng.choice(len(pts), min(len(pts), 60000), replace=False)]
    best_n, best_plane = 0, None
    for _ in range(iters):
        p0, p1, p2 = sample[rng.choice(len(sample), 3, replace=False)]
        n = np.cross(p1 - p0, p2 - p0)
        nn = np.linalg.norm(n)
        if nn < 1e-6:
            continue
        n = n / nn
        d = -float(n @ p0)
        cnt = int((np.abs(sample @ n + d) < thresh_mm).sum())
        if cnt > best_n:
            best_n, best_plane = cnt, (n, d)
    if best_plane is None:
        return None
    n, d = best_plane
    for _ in range(4):
        inl = np.abs(pts @ n + d) < thresh_mm
        if inl.sum() < 500:
            break
        q = pts[inl]
        c = q.mean(0)
        _, _, vt = np.linalg.svd(q - c, full_matrices=False)
        n = vt[2] / np.linalg.norm(vt[2])
        d = -float(n @ c)
    dist_all = pts @ n + d
    inl = np.abs(dist_all) < thresh_mm
    n_in = int(inl.sum())
    if n_in < max(500, min_inlier_frac * len(pts)):
        return None
    r = dist_all[inl]
    extent = pts[inl].max(0) - pts[inl].min(0)
    if np.sort(extent)[-2] < 30.0:            # degenerate sliver, not a face
        return None
    return {"normal": n, "offset_mm": d, "n_inliers": n_in,
            "inlier_fraction": float(n_in / len(pts)),
            "rms_mm": float(np.sqrt(np.mean(r * r))),
            "p95_abs_mm": float(np.percentile(np.abs(r), 95)),
            "thickness_p90_mm": float(np.percentile(r, 95) - np.percentile(r, 5)),
            "median_depth_mm": float(np.median(pts[inl][:, 2])),
            "extent_mm": extent.tolist(), "thresh_mm": thresh_mm}


def nn_spacing(points: np.ndarray, max_points: int = 30000, seed: int = 0) -> dict[str, Any]:
    pts = np.asarray(points, float)
    if cKDTree is None or len(pts) < 100:
        return {"available": False}
    rng = np.random.default_rng(seed)
    if len(pts) > max_points:
        pts = pts[rng.choice(len(pts), max_points, replace=False)]
    d, _ = cKDTree(pts).query(pts, k=2)
    d = d[:, 1]
    return {"available": True, "n": int(len(pts)),
            "median_mm": float(np.median(d)), "p05_mm": float(np.percentile(d, 5)),
            "p95_mm": float(np.percentile(d, 95)), "mean_mm": float(np.mean(d))}


def edge_diagnostics(z: np.ndarray, valid: np.ndarray, occluded: np.ndarray
                     ) -> dict[str, Any]:
    """Invalid / occluded fraction near depth discontinuities vs interior."""
    zz = np.where(valid, z, np.nan)
    gx = np.abs(np.diff(zz, axis=1, prepend=np.nan))
    gy = np.abs(np.diff(zz, axis=0, prepend=np.nan))
    jump = np.zeros_like(valid)
    jump[:, 1:] |= gx[:, 1:] > 15.0
    jump[1:, :] |= gy[1:, :] > 15.0
    jump &= valid
    hard = jump.astype(np.uint8)
    band = cv2.dilate(hard, np.ones((5, 5), np.uint8)) > 0
    interior = ~band
    out = {
        "depth_jump_threshold_mm": 15.0,
        "edge_pixels": int(hard.sum()),
        "edge_fraction": float(hard.mean()),
        "invalid_fraction_on_edges": fnum(float(np.mean(~valid[band]))) if band.any() else None,
        "invalid_fraction_interior": fnum(float(np.mean(~valid[interior]))) if interior.any() else None,
        "occluded_fraction": fnum(float(np.mean(occluded))),
        "occluded_fraction_of_matched": fnum(float(np.sum(occluded) /
                                                   max(np.sum(valid | occluded), 1))),
        "invalid_near_edge_curve": [],
    }
    for k in range(6):
        m = (hard > 0) if k == 0 else (cv2.dilate(hard, np.ones((2 * k + 1,) * 2, np.uint8)) > 0)
        out["invalid_near_edge_curve"].append(
            {"radius_px": k,
             "fraction_invalid": fnum(float(np.mean(~valid[m]))) if m.any() else None})
    return out


# --------------------------------------------------------------------------
# sparse ZNCC cross-check (independent of SGBM)
# --------------------------------------------------------------------------

def _parab(a: float, b: float, c: float) -> float:
    den = a - 2 * b + c
    return 0.0 if abs(den) < 1e-9 else 0.5 * (a - c) / den


def _ncc_strip(left: np.ndarray, right: np.ndarray, u: int, v: int, r: int,
               d_lo: int, d_hi: int) -> tuple[np.ndarray, np.ndarray]:
    """NCC of a left patch against every horizontal shift of the right image.

    Returns ``(scores, disparities)``; scores outside [d_lo, d_hi] are set to
    -2 so a caller's argmax cannot pick them.
    """
    tmpl = left[v - r:v + r + 1, u - r:u + r + 1].astype(np.float32)
    x0 = max(0, u - r - d_hi)
    x1 = min(right.shape[1], u - r + 1)
    strip = right[v - r:v + r + 1, x0:x1].astype(np.float32)
    if strip.shape[1] <= tmpl.shape[1] or tmpl.std() < 1e-6:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    scores = cv2.matchTemplate(strip, tmpl, cv2.TM_CCOEFF_NORMED).ravel().astype(np.float32)
    disp = u - (x0 + r + np.arange(scores.size)).astype(np.float32)
    scores[(disp < d_lo) | (disp > d_hi)] = -2.0
    return scores, disp


def sparse_zncc(left_rect: np.ndarray, right_rect: np.ndarray, geom: Geometry,
                cfg: StereoConfig, n_points: int = 300) -> dict[str, Any]:
    """Harris + ZNCC with uniqueness, at full resolution.

    Only verifies the stream mapping and the epipolar geometry; it does not
    feed the dense metrics.
    """
    t0 = time.perf_counter()
    # disparity is largest at the near limit; search the whole metric range
    d_lo = int(geom.disparity_at(cfg.max_depth_mm) / cfg.scale) - 12   # far end
    d_hi = int(geom.disparity_at(cfg.min_depth_mm) / cfg.scale) + 12   # near end
    d_lo = max(d_lo, 8)
    d_hi = min(d_hi, int(left_rect.shape[1] * 0.95))
    r = 7
    blur = cv2.GaussianBlur(left_rect, (5, 5), 0)
    corners = cv2.goodFeaturesToTrack(blur, n_points, 0.01, 12,
                                      blockSize=7, useHarrisDetector=True)
    if corners is None:
        return {"available": False, "n_matches": 0}
    pts = corners.reshape(-1, 2)
    scores, disps, dys = [], [], []
    for uu, vv in pts:
        u, v = int(round(uu)), int(round(vv))
        if u - r < 0 or v - r - 3 < 0 or u + r + 1 >= left_rect.shape[1] \
                or v + r + 3 >= left_rect.shape[0]:
            continue
        # 1) disparity at dy = 0 (the epipolar residual is measured, not
        #    searched jointly with the disparity: searching both at once lets
        #    a wrong row win on a repetitive speckle pattern).
        band0, disp0 = _ncc_strip(left_rect, right_rect, u, v, r, d_lo, d_hi)
        if band0.size == 0:
            continue
        k0 = int(np.argmax(band0))
        s0, d0 = float(band0[k0]), float(disp0[k0])
        if s0 < 0.55:
            continue
        far = np.abs(disp0 - d0) > 5
        second = float(band0[far].max()) if far.any() else -1.0
        if not (s0 > 1.10 * second and s0 > 0.6):
            continue
        # 2) vertical residual: score at the *same* disparity in neighbouring
        #    rows, then a parabola through the three samples.
        ys, vals = [], []
        for dy in (-2, -1, 0, 1, 2):
            if dy == 0:
                ys.append(0); vals.append(s0); continue
            b2, d2 = _ncc_strip(left_rect, right_rect, u, v + dy, r, d_lo, d_hi)
            if b2.size == 0:
                continue
            j = int(np.argmin(np.abs(d2 - d0)))
            ys.append(dy); vals.append(float(b2[j]))
        if len(ys) < 3:
            continue
        order = int(np.argmax(vals))
        dy = float(ys[order])
        if 0 < order < len(ys) - 1:
            dy += _parab(vals[order - 1], vals[order], vals[order + 1])
        ds = d0
        if 0 < k0 < band0.size - 1:
            ds += _parab(band0[k0 - 1], band0[k0], band0[k0 + 1])
        s = s0
        scores.append(s)
        disps.append(ds)
        dys.append(dy)
    scores, disps, dys = map(np.asarray, (scores, disps, dys))
    if scores.size == 0:
        return {"available": True, "n_corners": int(len(pts)), "n_matches": 0,
                "seconds": time.perf_counter() - t0}
    z = geom.f_full * geom.baseline_mm / np.maximum(disps - geom.disp_offset_full, 1e-9)
    return {"available": True, "n_corners": int(len(pts)),
            "n_matches": int(scores.size),
            "match_rate": float(scores.size / len(pts)),
            "score_median": float(np.median(scores)),
            "abs_dy_median_px": float(np.median(np.abs(dys))),
            "abs_dy_p90_px": float(np.percentile(np.abs(dys), 90)),
            "frac_abs_dy_lt1px": float(np.mean(np.abs(dys) < 1.0)),
            "disp_median_px": float(np.median(disps)),
            "disp_p05_px": float(np.percentile(disps, 5)),
            "disp_p95_px": float(np.percentile(disps, 95)),
            "search_px": [d_lo, d_hi],
            "depth_median_mm": float(np.median(z)),
            "depth_p05_mm": float(np.percentile(z, 5)),
            "depth_p95_mm": float(np.percentile(z, 95)),
            "seconds": time.perf_counter() - t0}


# --------------------------------------------------------------------------
# per-configuration evaluation
# --------------------------------------------------------------------------

def evaluate_once(triplet: dict[str, Any], geom: Geometry, cfg: StereoConfig,
                  with_arrays: bool = True) -> dict[str, Any]:
    t0 = time.perf_counter()
    left_rect, right_rect, vl, vr = rectify_triplet(triplet, geom)
    t1 = time.perf_counter()
    match = match_pair(left_rect, right_rect, geom, cfg)
    vl_s = downsample(vl, cfg.scale, nearest=True) > 0
    vr_s = downsample(vr, cfg.scale, nearest=True) > 0
    lr = lr_consistency(match, cfg, vl_s, vr_s)
    depth = depth_map(match, lr, geom, cfg)
    t2 = time.perf_counter()
    valid, z = depth["valid"], depth["z"]
    zv = z[valid]
    pts = depth["xyz"][valid]
    plane = ransac_plane(pts, seed=int(triplet["id"])) if valid.sum() > 5000 else None
    nn = nn_spacing(pts)
    edge = edge_diagnostics(z, valid, lr["occluded"])
    if plane is not None:
        res_plane = np.abs(depth["xyz"] @ plane["normal"] + plane["offset_mm"])
    else:
        res_plane = np.full_like(z, np.nan)
    out = {
        "computed_in_s": time.perf_counter() - t0,
        "timing": {"rectify_s": t1 - t0, "stereo_s": match["timing"]["sgbm_total_s"],
                   "stereo_left_s": match["timing"]["sgbm_left_s"],
                   "stereo_right_s": match["timing"]["sgbm_right_s"],
                   "depth_metrics_s": time.perf_counter() - t2},
        "search": match["search"],
        "grid": {"width": int(z.shape[1]), "height": int(z.shape[0])},
        "valid_fraction": float(valid.mean()),
        "valid_fraction_of_source": float(valid.sum() / max((vl_s & vr_s).sum(), 1)),
        "matched_fraction": float(lr["n_valid_raw"] / valid.size),
        "occluded_fraction": float(lr["n_occluded"] / valid.size),
        "depth_p05_mm": fnum(pcts(zv, (5,))[0]),
        "depth_p50_mm": fnum(pcts(zv, (50,))[0]),
        "depth_p95_mm": fnum(pcts(zv, (95,))[0]),
        "depth_sigma_random_p50_mm": fnum(
            float(np.median(zv ** 2 / (geom.f_full * cfg.scale * geom.baseline_mm) * 0.2))
            if zv.size else None),
        "disp_p50_px": fnum(float(np.median(match["disparity"][valid])) if valid.any() else None),
        "frac_valid_depth_in_range": fnum(float(np.mean(
            (zv >= cfg.min_depth_mm) & (zv <= cfg.max_depth_mm)))) if zv.size else 0.0,
        "plane": plane, "nn": nn, "edge": edge, "n_points": int(valid.sum()),
        "confidence": {"median": fnum(float(np.median(lr["confidence"][valid])))
                       if valid.any() else None,
                       "p95": fnum(float(np.percentile(lr["confidence"][valid], 95)))
                       if valid.any() else None},
    }
    if with_arrays:
        out["_arrays"] = {"z": z, "valid": valid, "confidence": lr["confidence"],
                          "lr_residual": lr["residual"], "plane_residual": res_plane}
    return out


def confidence_bins(a: dict[str, Any], b: dict[str, Any] | None,
                    edges=(0.0, 0.2, 0.4, 0.6, 0.8, 1.01)) -> dict[str, Any]:
    """Per-bin L-R residual, plane residual and cross-frame disagreement.

    The L-R residual *is* the confidence input, so that column is a tautology
    check; the plane residual and the cross-frame disagreement are
    independent of the confidence definition.
    """
    conf, valid = a["confidence"], a["valid"]
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = valid & (conf >= lo) & (conf < hi)
        row = {"bin": f"[{lo:.1f},{min(hi, 1.0):.1f})", "n": int(m.sum())}
        if m.any():
            row["lr_abs_p50_px"] = fnum(float(np.median(a["lr_residual"][m])))
            row["plane_abs_p50_mm"] = fnum(float(np.nanmedian(a["plane_residual"][m])))
            row["plane_abs_p95_mm"] = fnum(float(np.nanpercentile(a["plane_residual"][m], 95)))
            if b is not None and (m & b["valid"]).sum() > 50:
                mb = m & b["valid"]
                dz = np.abs(a["z"][mb] - b["z"][mb])
                row["crossframe_abs_dz_p50_mm"] = fnum(float(np.median(dz)))
                row["crossframe_abs_dz_p95_mm"] = fnum(float(np.percentile(dz, 95)))
        rows.append(row)
    return {"edges": list(edges), "rows": rows}


def crossframe(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    m = a["valid"] & b["valid"]
    n = int(m.sum())
    if n < 100:
        return {"n": n, "frames": None}
    dz = a["z"][m] - b["z"][m]
    return {"n": n, "abs_dz_p50_mm": fnum(float(np.median(np.abs(dz)))),
            "abs_dz_p95_mm": fnum(float(np.percentile(np.abs(dz), 95))),
            "bias_mm": fnum(float(np.median(dz))),
            "frac_within_2mm": fnum(float(np.mean(np.abs(dz) <= 2.0))),
            "frac_within_5mm": fnum(float(np.mean(np.abs(dz) <= 5.0))),
            "dz": dz}


def plane_repeatability(planes: list[dict[str, Any] | None]) -> dict[str, Any]:
    out: dict[str, Any] = {"planes": [], "pairs": []}
    ok = []
    for i, p in enumerate(planes):
        if p is None:
            out["planes"].append({"frame": i, "available": False})
            continue
        out["planes"].append({"frame": i, "available": True,
                              "normal": [fnum(x, 6) for x in p["normal"]],
                              "offset_mm": fnum(p["offset_mm"]),
                              "rms_mm": fnum(p["rms_mm"]),
                              "median_depth_mm": fnum(p["median_depth_mm"]),
                              "n_inliers": p["n_inliers"]})
        ok.append((i, p))
    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            fi, pi = ok[i]
            fj, pj = ok[j]
            n1, n2 = pi["normal"], pj["normal"]
            if np.dot(n1, n2) < 0:
                n2 = -n2
            ang = math.degrees(math.acos(float(np.clip(np.dot(n1, n2), -1, 1))))
            out["pairs"].append({
                "frames": [fi, fj],
                "normal_angle_deg": fnum(ang, 3),
                "offset_difference_mm": fnum(abs(abs(pi["offset_mm"]) - abs(pj["offset_mm"])), 3),
                "rms_difference_mm": fnum(abs(pi["rms_mm"] - pj["rms_mm"]), 3),
                "same_face": bool(ang < 10.0 and
                                  abs(pi["median_depth_mm"] - pj["median_depth_mm"]) < 40.0),
            })
    out["all_same_face"] = bool(out["pairs"]) and all(p["same_face"] for p in out["pairs"])
    return out


# --------------------------------------------------------------------------
# C++ backend
# --------------------------------------------------------------------------

def read_tiff_float(path: Path) -> np.ndarray | None:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is not None:
        return np.asarray(img, np.float32)
    try:
        from PIL import Image
        return np.asarray(Image.open(path), np.float32)
    except Exception:
        return None


def _poll_peak_rss(proc: subprocess.Popen, interval: float = 0.01) -> float:
    peak = 0.0
    status = Path(f"/proc/{proc.pid}/status")
    while proc.poll() is None:
        try:
            with open(status) as f:
                for line in f:
                    if line.startswith("VmHWM:"):
                        peak = max(peak, float(line.split()[1]) / 1024.0)
                        break
        except OSError:
            break
        time.sleep(interval)
    return peak


def run_cpp(frame_dir: Path, profile: Path, frame_id: int, outdir: Path,
            cfg: StereoConfig, cli: Path = CLI) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [str(cli), "reconstruct", "--frames", str(frame_dir), "--id", str(frame_id),
           "--profile", str(profile), "--out", str(outdir), "--scale", f"{cfg.scale}",
           "--min-depth", f"{cfg.min_depth_mm}", "--max-depth", f"{cfg.max_depth_mm}",
           "--block", str(cfg.block_size)]
    if cfg.equalize:
        cmd.append("--equalize")
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak = _poll_peak_rss(proc)
    out, err = proc.communicate()
    res: dict[str, Any] = {"command": cmd, "returncode": proc.returncode,
                           "stdout": out.strip(), "stderr": err.strip(),
                           "wall_s": time.perf_counter() - t0, "peak_rss_mb": peak}
    if proc.returncode != 0:
        res["status"] = "pending-stereo-stub" if "stereo backend not built" in (err + out) \
            else "failed"
        return res
    res["status"] = "ok"
    report_path = outdir / "recon_report.json"
    res["report"] = json.loads(report_path.read_text()) if report_path.exists() else None
    ply = outdir / "recon_cloud.ply"
    res["has_ply"] = ply.exists()
    res["ply_bytes"] = ply.stat().st_size if ply.exists() else 0
    z = read_tiff_float(outdir / "recon_depth_mm.tiff")
    if z is not None:
        res["depth_shape"] = list(z.shape)
        res["depth_valid_fraction"] = float(np.mean(z > 0))
        zv = z[z > 0]
        if zv.size:
            res["depth_p05_mm"], res["depth_p50_mm"], res["depth_p95_mm"] = [
                fnum(v) for v in pcts(zv)]
        res["_z"] = z
    return res


def compare_cpp(py: dict[str, Any], cpp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"status": cpp.get("status")}
    if cpp.get("status") != "ok":
        return out
    zc = cpp.get("_z")
    za = py.get("_arrays", {}).get("z")
    rep = cpp.get("report") or {}
    out["cpp_report"] = {
        "valid_fraction": (rep.get("depth") or {}).get("valid_fraction"),
        "points": (rep.get("cloud") or {}).get("points"),
        "timings": rep.get("timings"),
        "search": rep.get("stereo"),
        "frame_sha256": (rep.get("input") or {}).get("sha256"),
        "calibration_id": (rep.get("calibration") or {}).get("id"),
    }
    if zc is None or za is None:
        out["per_pixel"] = {"available": False, "reason": "depth map missing"}
        return out
    if zc.shape != za.shape:
        out["per_pixel"] = {"available": False, "reason": "shape mismatch",
                            "shapes": [list(zc.shape), list(za.shape)]}
        return out
    mp, mc = za > 0, zc > 0
    both = mp & mc
    row = {"available": True,
           "python_valid_fraction": fnum(float(mp.mean())),
           "cpp_valid_fraction": fnum(float(mc.mean())),
           "valid_agreement_iou": fnum(float(both.sum() / max((mp | mc).sum(), 1))),
           "n_both": int(both.sum())}
    if both.sum() > 100:
        dz = np.abs(za[both] - zc[both])
        row.update({"abs_dz_p50_mm": fnum(float(np.median(dz))),
                    "abs_dz_p95_mm": fnum(float(np.percentile(dz, 95))),
                    "bias_mm": fnum(float(np.median(za[both] - zc[both]))),
                    "frac_within_1mm": fnum(float(np.mean(dz <= 1.0))),
                    "frac_within_5mm": fnum(float(np.mean(dz <= 5.0)))})
    out["per_pixel"] = row
    out["point_count"] = {"python": int(mp.sum()),
                          "cpp": int((rep.get("cloud") or {}).get("points") or 0)}
    return out


# --------------------------------------------------------------------------
# figures (aggregate only; no imagery)
# --------------------------------------------------------------------------

def make_figures(frame_results: list[dict[str, Any]], results: dict[str, Any],
                 figdir: Path, dpi: int = 140) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figdir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.3})

    # 1. depth histogram per frame
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    for f in frame_results:
        a = f["_arrays"]["z"][f["_arrays"]["valid"]]
        if a.size:
            ax.hist(a, bins=120, range=(150, 800), histtype="step", density=True,
                    label=f"frame {f['id']} (n={a.size})")
            ax.axvline(f["depth_p50_mm"], ls=":", lw=0.8, alpha=0.6)
    ax.set_xlabel("depth in left IR camera frame (mm)")
    ax.set_ylabel("probability density")
    ax.set_title("Valid-pixel depth distribution (measured, aggregate)")
    ax.legend(loc="upper right", fontsize=7)
    fig.tight_layout()
    fig.savefig(figdir / "depth_histogram.png", dpi=dpi)
    plt.close(fig)
    written.append("depth_histogram.png")

    # 2. confidence calibration
    cal = results["confidence_calibration"]["frames"]
    fig, axes = plt.subplots(1, 3, figsize=(9.6, 3.2))
    for f in cal:
        rows = [r for r in f["rows"] if r["n"] > 50]
        x = [float(r["bin"].strip("[]").split(",")[0]) + 0.1 for r in rows]
        axes[0].plot(x, [r.get("lr_abs_p50_px") for r in rows], "o-", label=f"frame {f['frame']}")
        axes[1].plot(x, [r.get("plane_abs_p50_mm") for r in rows], "o-", label=f"frame {f['frame']}")
        axes[2].plot(x, [r.get("crossframe_abs_dz_p50_mm") for r in rows], "o-",
                     label=f"frame {f['frame']}")
    axes[0].set_ylabel("|L-R residual| p50 (px)")
    axes[0].set_title("tautological (confidence input)")
    axes[1].set_ylabel("plane |residual| p50 (mm)")
    axes[1].set_title("independent: plane fit")
    axes[2].set_ylabel("|dz| vs next frame p50 (mm)")
    axes[2].set_title("independent: repeatability")
    for ax in axes:
        ax.set_xlabel("confidence bin")
        ax.legend(fontsize=7)
    fig.suptitle("Confidence calibration on real frames (measured)", fontsize=10)
    fig.tight_layout()
    fig.savefig(figdir / "confidence_calibration.png", dpi=dpi)
    plt.close(fig)
    written.append("confidence_calibration.png")

    # 3. ablations
    abl = results["ablations"]["variants"]
    if abl:
        names = [a["name"] for a in abl]
        fig, axes = plt.subplots(1, 3, figsize=(10.4, 3.4))
        axes[0].barh(names, [a["mean_valid_fraction"] for a in abl], color="#3b7dd8")
        axes[0].set_xlabel("valid fraction"); axes[0].set_title("coverage")
        axes[1].barh(names, [a["mean_plane_rms_mm"] or 0 for a in abl], color="#d88a3b")
        axes[1].set_xlabel("plane RMS (mm)"); axes[1].set_title("largest-plane RMS")
        axes[2].barh(names, [a["mean_runtime_s"] for a in abl], color="#5aa469")
        axes[2].set_xlabel("runtime (s / frame)"); axes[2].set_title("runtime")
        for ax in axes:
            ax.invert_yaxis()
            ax.tick_params(labelsize=7)
        fig.suptitle("Ablations, mean over 3 frames (measured)", fontsize=10)
        fig.tight_layout()
        fig.savefig(figdir / "ablations.png", dpi=dpi)
        plt.close(fig)
        written.append("ablations.png")

    # 4. edge / occlusion
    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    for f in frame_results:
        curve = f["edge"]["invalid_near_edge_curve"]
        ax.plot([c["radius_px"] for c in curve], [c["fraction_invalid"] for c in curve],
                "o-", label=f"frame {f['id']}")
    ax.set_xlabel("dilation radius around depth discontinuity (px, half-res)")
    ax.set_ylabel("fraction invalid")
    ax.set_title("Invalidity near depth edges (measured)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figdir / "edge_occlusion.png", dpi=dpi)
    plt.close(fig)
    written.append("edge_occlusion.png")

    # 5. cross-frame agreement
    fig, ax = plt.subplots(figsize=(6.0, 3.4))
    for pair in results["repeatability"]["pairs"]:
        dz = pair.get("dz")
        if dz is not None and dz.size:
            ax.hist(np.abs(dz), bins=80, range=(0, 20), histtype="step", density=True,
                    label=f"frames {pair['frames'][0]}-{pair['frames'][1]} "
                          f"(p50 {pair['abs_dz_p50_mm']:.2f} mm)")
    ax.set_xlabel("|dz| at co-valid pixels (mm)")
    ax.set_ylabel("probability density")
    ax.set_title("Cross-frame depth agreement (measured, consistency only)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(figdir / "crossframe_agreement.png", dpi=dpi)
    plt.close(fig)
    written.append("crossframe_agreement.png")
    return written


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames-dir", type=Path, default=DEFAULT_CAPTURE)
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--ids", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--fig-dir", type=Path, default=DEFAULT_FIGS)
    ap.add_argument("--tmp-root", type=Path, default=DEFAULT_TMP)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--block", type=int, default=5)
    ap.add_argument("--equalize", action="store_true")
    ap.add_argument("--min-depth", type=float, default=150.0)
    ap.add_argument("--max-depth", type=float, default=800.0)
    ap.add_argument("--cpp", choices=("auto", "on", "off"), default="auto")
    ap.add_argument("--cpp-only", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip ablations and figures")
    ap.add_argument("--no-figures", action="store_true")
    ap.add_argument("--check-no-private-export", action="store_true")
    args = ap.parse_args(argv)

    for p in (args.frames_dir, args.profile):
        if not p.exists():
            print(f"missing input: {p}", file=sys.stderr)
            return 2
    args.tmp_root.mkdir(parents=True, exist_ok=True)

    cpp_available = CLI.exists()
    cpp_probe = None
    if args.cpp != "off" and cpp_available and not args.cpp_only:
        cpp_probe = run_cpp(args.frames_dir, args.profile, args.ids[0],
                            args.tmp_root / "probe",
                            StereoConfig(scale=args.scale, block_size=args.block,
                                         equalize=args.equalize,
                                         min_depth_mm=args.min_depth,
                                         max_depth_mm=args.max_depth))
        print(f"C++ backend: {cpp_probe['status']} "
              f"({cpp_probe.get('stderr') or cpp_probe.get('stdout')})")

    if args.cpp_only:
        if not args.out_json.exists():
            print("--cpp-only needs an existing JSON", file=sys.stderr)
            return 2
        old = json.loads(args.out_json.read_text())
        base = old["config"]
        runs = []
        for fid in args.ids:
            r = run_cpp(args.frames_dir, args.profile, fid, args.tmp_root / f"id{fid}",
                        StereoConfig(scale=base["scale"], block_size=base["block_size"],
                                     equalize=base["equalize"],
                                     min_depth_mm=base["min_depth_mm"],
                                     max_depth_mm=base["max_depth_mm"]))
            r.pop("_z", None)
            runs.append(r)
        old["cpp"] = {"status": "ok" if all(r["status"] == "ok" for r in runs)
                      else runs[0]["status"], "runs": runs}
        args.out_json.write_text(json.dumps(old, indent=2, default=json_default) + "\n")
        print("updated", args.out_json, "->", old["cpp"]["status"])
        return 0 if old["cpp"]["status"] == "ok" else 3

    cfg = StereoConfig(scale=args.scale, block_size=args.block, equalize=args.equalize,
                       min_depth_mm=args.min_depth, max_depth_mm=args.max_depth)
    profile = load_profile(args.profile)
    geom = build_geometry(profile, cfg.scale)

    results: dict[str, Any] = {
        "schema": "einstar.real_data_eval/1",
        "generated_utc": utc_now(),
        "labels": {
            "measured": "computed from the genuine frames by this harness",
            "consistency": "agreement between independent computations on the same "
                           "frames; not physical accuracy",
            "inferred": "model-based expectation or interpretation",
            "accuracy": "NOT measured: no calibrated artefact or independent metrology",
        },
        "environment": {"python": sys.version.split()[0], "numpy": np.__version__,
                        "opencv": cv2.__version__, "platform": platform.platform(),
                        "cpu_count": os.cpu_count()},
        "input": {"capture_dir": str(args.frames_dir), "profile": str(args.profile),
                  "profile_file_sha256": sha256_file(args.profile),
                  "profile_kind": profile.get("kind"),
                  "profile_color_status": profile.get("color_status"),
                  "ids": args.ids,
                  "stream_mapping": {str(k): {"camera": v[0], "rotate_180": v[1]}
                                     for k, v in STREAM_MAP.items()},
                  "frames": []},
        "config": asdict(cfg),
        "geometry": {"image_size": list(geom.size_full),
                     "f_full_px": fnum(geom.f_full, 6),
                     "f_scaled_px": fnum(geom.f_full * cfg.scale, 6),
                     "cx_full_px": fnum(geom.cx_full, 4),
                     "cy_full_px": fnum(geom.cy_full, 4),
                     "baseline_mm": fnum(geom.baseline_mm, 6),
                     "disp_offset_full_px": fnum(geom.disp_offset_full, 6),
                     "valid_left_fraction": fnum(float(np.mean(geom.valid_left > 0)))},
        "frames": [], "repeatability": {"pairs": [], "planes": []},
        "ablations": {"variants": []}, "confidence_calibration": {"frames": []},
        "edge_occlusion": [], "cloud": [], "performance": {},
        "cpp": {"status": "disabled" if args.cpp == "off" else
                ("unavailable" if not cpp_available else "pending"), "runs": []},
        "limitations": [],
    }
    if cpp_probe is not None:
        results["cpp"]["probe"] = {k: v for k, v in cpp_probe.items() if k != "_z"}

    # ---- per-frame evaluation ------------------------------------------------
    frame_results: list[dict[str, Any]] = []
    for fid in args.ids:
        t0 = time.perf_counter()
        trip = load_triplet(args.frames_dir, fid)
        t_load = time.perf_counter() - t0
        r = evaluate_once(trip, geom, cfg)
        r["id"] = fid
        r["timing"]["load_s"] = t_load
        r["timing"]["total_s"] = time.perf_counter() - t0
        r["rss_mb_after"] = fnum(rss_mb(), 1)
        r["input"] = {"files": trip["files"],
                      "pixel_sha256_stream_order": trip["pixel_sha256_stream_order"]}
        r["saturation_fraction_left"] = fnum(float(np.mean(trip["ir_left"] >= 255)))
        r["saturation_fraction_right"] = fnum(float(np.mean(trip["ir_right"] >= 255)))
        raw = trip["ir_left"]
        r["input"]["raw_stats_left"] = {
            "mean": fnum(float(raw.mean()), 2), "std": fnum(float(raw.std()), 2),
            "p01": int(np.percentile(raw, 1)), "p99": int(np.percentile(raw, 99))}
        frame_results.append(r)
        plane_rms = (r["plane"] or {}).get("rms_mm")
        print(f"frame {fid}: valid {r['valid_fraction']*100:.1f}%  depth p05/50/95 "
              f"{r['depth_p05_mm']:.1f}/{r['depth_p50_mm']:.1f}/{r['depth_p95_mm']:.1f} mm  "
              f"plane RMS {plane_rms if plane_rms is None else round(plane_rms, 2)} mm  "
              f"{r['timing']['total_s']:.2f}s")
        results["input"]["frames"].append({
            "id": fid, **r["input"],
            "saturation_fraction": {"left": r["saturation_fraction_left"],
                                    "right": r["saturation_fraction_right"]}})
    results["geometry"]["search"] = frame_results[0]["search"]

    # epipolar / stream-mapping verification (independent of SGBM)
    epi = []
    for fid in args.ids:
        trap = load_triplet(args.frames_dir, fid)
        lr_img, rr_img, _, _ = rectify_triplet(trap, geom)
        epi.append({"frame": fid, **sparse_zncc(lr_img, rr_img, geom, cfg)})
    results["epipolar_check"] = epi

    # cross-frame agreement + confidence calibration
    for i in range(len(frame_results)):
        for j in range(i + 1, len(frame_results)):
            a, b = frame_results[i], frame_results[j]
            c = crossframe(a["_arrays"], b["_arrays"])
            c["frames"] = [a["id"], b["id"]]
            results["repeatability"]["pairs"].append(c)
    results["repeatability"]["planes"] = plane_repeatability(
        [f["plane"] for f in frame_results])
    for idx, f in enumerate(frame_results):
        nxt = frame_results[idx + 1]["_arrays"] if idx + 1 < len(frame_results) else None
        cal = confidence_bins(f["_arrays"], nxt)
        cal["frame"] = f["id"]
        results["confidence_calibration"]["frames"].append(cal)

    # ---- ablations -----------------------------------------------------------
    def variant(name: str, **over) -> dict[str, Any]:
        vcfg = StereoConfig(**{**asdict(cfg), **over})
        rows = []
        t0 = time.perf_counter()
        for fid in args.ids:
            trip = load_triplet(args.frames_dir, fid)
            r = evaluate_once(trip, geom, vcfg, with_arrays=False)
            rows.append({"frame": fid, "valid_fraction": r["valid_fraction"],
                         "depth_p50_mm": r["depth_p50_mm"],
                         "occluded_fraction": r["occluded_fraction"],
                         "plane_rms_mm": (r["plane"] or {}).get("rms_mm"),
                         "runtime_s": r["computed_in_s"]})
        rms = [r["plane_rms_mm"] for r in rows if r["plane_rms_mm"] is not None]
        return {"name": name, "overrides": over,
                "mean_valid_fraction": fnum(float(np.mean([r["valid_fraction"] for r in rows]))),
                "mean_depth_p50_mm": fnum(float(np.mean([r["depth_p50_mm"] for r in rows]))),
                "mean_plane_rms_mm": fnum(float(np.mean(rms))) if rms else None,
                "mean_occluded_fraction": fnum(float(np.mean(
                    [r["occluded_fraction"] for r in rows]))),
                "mean_runtime_s": fnum(float(np.mean([r["runtime_s"] for r in rows]))),
                "frames": rows, "wall_s": round(time.perf_counter() - t0, 2)}

    if not args.quick:
        variants = [variant("base (0.5, block5, 150-800)"),
                    variant("scale 1.0", scale=1.0),
                    variant("equalize (CLAHE)", equalize=True),
                    variant("block 7", block_size=7),
                    variant("depth 250-600", min_depth_mm=250.0, max_depth_mm=600.0),
                    variant("depth 120-1200", min_depth_mm=120.0, max_depth_mm=1200.0)]
        results["ablations"]["variants"] = variants
        for v in variants:
            print(f"ablation {v['name']:34s} cov {v['mean_valid_fraction']*100:5.1f}%  "
                  f"med {v['mean_depth_p50_mm']:6.1f} mm  rms {v['mean_plane_rms_mm']}  "
                  f"{v['mean_runtime_s']:.2f}s")

    # ---- C++ -----------------------------------------------------------------
    if args.cpp != "off" and cpp_available and cpp_probe is not None \
            and cpp_probe["status"] == "ok":
        for f in frame_results:
            r = run_cpp(args.frames_dir, args.profile, f["id"],
                        args.tmp_root / f"id{f['id']}", cfg)
            comp = compare_cpp(f, r)
            r.pop("_z", None)
            results["cpp"]["runs"].append({"frame": f["id"], "status": r["status"],
                                           "wall_s": fnum(r["wall_s"]),
                                           "peak_rss_mb": fnum(r["peak_rss_mb"], 1),
                                           "stdout": r.get("stdout"),
                                           "comparison": comp})
        results["cpp"]["status"] = "ok" if all(
            x["status"] == "ok" for x in results["cpp"]["runs"]) else "failed"
    elif args.cpp == "on":
        results["cpp"]["status"] = "pending-stereo-stub"
        results["cpp"]["reason"] = (cpp_probe or {}).get("stderr", "recon_cli missing")

    # ---- performance ---------------------------------------------------------
    results["performance"] = {
        "peak_rss_mb": fnum(peak_rss_mb(), 1),
        "per_frame": [{"frame": f["id"], **f["timing"], "rss_after_mb": f["rss_mb_after"]}
                      for f in frame_results],
        "per_stage_mean": {
            k: fnum(float(np.mean([f["timing"].get(k, np.nan) for f in frame_results])))
            for k in ("rectify_s", "stereo_left_s", "stereo_right_s", "load_s",
                      "depth_metrics_s", "total_s")},
    }

    # ---- figures (before stripping the arrays) ------------------------------
    fig_names: list[str] = []
    if not (args.no_figures or args.quick):
        try:
            fig_names = make_figures(frame_results, results, args.fig_dir)
        except Exception as e:      # figures must never kill the metrics
            print(f"figures skipped: {e}", file=sys.stderr)

    # ---- consistency checks against the documented facts -------------------
    mean_cov = float(np.mean([f["valid_fraction"] for f in frame_results]))
    mean_in_range = float(np.mean([f["frac_valid_depth_in_range"] for f in frame_results]))
    med_p50 = float(np.median([f["depth_p50_mm"] for f in frame_results]))
    results["checks"] = {
        "stream_mapping_gives_horizontal_epipolars": {
            "value": epi[0].get("abs_dy_median_px"),
            "pass": bool(epi[0].get("abs_dy_median_px") is not None
                         and epi[0]["abs_dy_median_px"] < 2.0),
            "expected": "< 2 px median |dy|"},
        "median_disparity_450_600px": {
            "value": epi[0].get("disp_median_px"),
            "pass": bool(epi[0].get("disp_median_px") is not None
                         and 450 <= epi[0]["disp_median_px"] <= 600),
            "expected": "450..600 px (full-res)"},
        "coverage_near_40pct": {"value": fnum(mean_cov),
                                "pass": bool(0.20 <= mean_cov <= 0.55),
                                "expected": "0.2..0.55 (audit claims ~0.40)"},
        "median_depth_330_390mm": {"value": fnum(med_p50),
                                   "pass": bool(330 <= med_p50 <= 390),
                                   "expected": "330..390 mm"},
        "valid_depth_inside_150_800mm": {"value": fnum(mean_in_range),
                                         "pass": bool(mean_in_range > 0.98),
                                         "expected": "> 0.98"},
    }

    results["limitations"] = [
        "no calibrated artefact: every geometric number is consistency, not accuracy",
        "single unit, factory profile recovered from a different capture run",
        "the factory colour record is stale; colour is not evaluated",
        "SGBM is deterministic but texture-limited; coverage is not a quality score",
        "plane RMS mixes calibration residual, SGBM bias and real surface shape",
        "the C++ comparison is pending while the stereo backend is a fail-closed stub",
    ]

    # ---- strip private arrays, then serialise -------------------------------
    for p in results["repeatability"]["pairs"]:
        p.pop("dz", None)
    results["frames"] = [{k: v for k, v in f.items() if k != "_arrays"}
                         for f in frame_results]
    results["cloud"] = [{"frame": f["id"], "points": f["n_points"], "nn": f["nn"],
                         "plane": ({k: f["plane"][k] for k in
                                    ("rms_mm", "p95_abs_mm", "thickness_p90_mm",
                                     "inlier_fraction", "median_depth_mm")}
                                   if f["plane"] else None)}
                        for f in frame_results]
    results["edge_occlusion"] = [{"frame": f["id"], **f["edge"]} for f in frame_results]

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(results, indent=2, default=json_default) + "\n")
    print(f"\nwrote {args.out_json}")
    if fig_names:
        print("figures:", ", ".join(fig_names))

    if args.check_no_private_export:
        bad = [p for p in (REPO / "docs").rglob("*")
               if p.suffix.lower() in (".pgm", ".png", ".jpg", ".jpeg", ".tif",
                                       ".tiff", ".ply")
               and "reconstruction/figures" not in str(p)]
        if bad:
            print("private-looking exports under docs/:", *bad, sep="\n  ", file=sys.stderr)
            return 4
    if args.cpp == "on" and results["cpp"].get("status") != "ok":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
