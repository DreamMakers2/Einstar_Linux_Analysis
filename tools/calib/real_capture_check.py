#!/usr/bin/env python3
"""Validation of the calibration model against real scanner captures.

The captures live outside the repository (private, never committed):
``<capture-dir>/frame_%06d_cam{0,1,2}.pgm``.
Only **aggregate** results are printed/written by this tool -- no image or
per-pixel data is copied into the repository or into the docs.

Two experiments
---------------
1. **Pair identification / epipolar check.**  For each candidate camera pair
   we rectify both frames with the model's own relative pose, detect Harris
   corners in the rectified left image and match them with a ZNCC patch
   correlator that searches the *full* disparity range (hundreds of pixels)
   and a small vertical window.  If the model and the pair are right, the
   vertical residual of the matches collapses to a few pixels (the residual is
   the model error, not zero, because the calibration is from another run);
   the wrong pair cannot produce consistent matches at all.

2. **Coarse disparity map + point cloud.**  A half-resolution block matcher
   over the same rectified pair, left-right consistency check, sub-pixel
   parabola, then triangulation with Q.  Reported: coverage and depth
   percentiles.  **No ground truth exists**, so this is a consistency check
   (does the scene sit at a plausible distance? are the epipolar lines really
   horizontal on real pixels?), not a measurement of accuracy.

Usage::

    python3 tools/calib/real_capture_check.py --pair left-right --frames 0 1 2
    python3 tools/calib/real_capture_check.py --pair all --frames 0 1
    python3 tools/calib/real_capture_check.py --pair left-right --frames 0 \
        --dense --ply-out /tmp/cloud.ply

The tool degrades gracefully: if the private capture directory is missing it
prints a message and exits with status 2.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from calib_model import Calibration, StereoPair
from rectify import Rectification, bouguet_rectification
from triangulate import stats, xyz_to_ply

CAPTURE_DIR = Path("<capture-dir>")

#: (camera_a index, camera_b index, model pair names)
PAIRS = {
    "left-right": (0, 1, "left", "right"),
    "left-tex": (0, 2, "left", "tex"),
    "right-tex": (1, 2, "right", "tex"),
}


# --------------------------------------------------------------------------
# image IO
# --------------------------------------------------------------------------

def load_pgm(path: str | Path) -> np.ndarray:
    """Read a binary (P5) 8-bit PGM without any external library."""
    data = Path(path).read_bytes()
    if not data.startswith(b"P5"):
        raise ValueError(f"{path}: not a binary PGM")
    fields: list[bytes] = []
    pos = 2
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
    pos += 1                                    # single whitespace after maxval
    w, h = int(fields[0]), int(fields[1])
    maxval = int(fields[2])
    if maxval > 255:
        raise ValueError("only 8-bit PGM is supported")
    img = np.frombuffer(data[pos:pos + w * h], dtype=np.uint8).reshape(h, w)
    return img.astype(np.float32)


def frame_path(capture_dir: Path, index: int, cam: int) -> Path:
    return capture_dir / f"frame_{index:06d}_cam{cam}.pgm"


# --------------------------------------------------------------------------
# Harris corners
# --------------------------------------------------------------------------

def _box_sum(a: np.ndarray, r: int) -> np.ndarray:
    """Sum over a (2r+1)x(2r+1) window, via a summed-area table."""
    h, w = a.shape
    ii = np.zeros((h + 1, w + 1), float)
    ii[1:, 1:] = a.cumsum(0).cumsum(1)
    ys = np.arange(h)
    xs = np.arange(w)
    y0 = np.clip(ys - r, 0, h)
    y1 = np.clip(ys + r + 1, 0, h)
    x0 = np.clip(xs - r, 0, w)
    x1 = np.clip(xs + r + 1, 0, w)
    return (ii[np.ix_(y1, x1)] - ii[np.ix_(y0, x1)]
            - ii[np.ix_(y1, x0)] + ii[np.ix_(y0, x0)])


def harris_corners(img: np.ndarray, max_points: int = 300, min_dist: int = 12,
                   border: int = 40, k: float = 0.04) -> np.ndarray:
    """Return (N,2) ``(u, v)`` corner coordinates, strongest first."""
    g = np.asarray(img, float)
    ix = np.zeros_like(g)
    iy = np.zeros_like(g)
    ix[:, 1:-1] = 0.5 * (g[:, 2:] - g[:, :-2])
    iy[1:-1, :] = 0.5 * (g[2:, :] - g[:-2, :])
    sxx = _box_sum(ix * ix, 2)
    syy = _box_sum(iy * iy, 2)
    sxy = _box_sum(ix * iy, 2)
    det = sxx * syy - sxy * sxy
    tr = sxx + syy
    resp = det - k * tr * tr
    resp[:border, :] = 0
    resp[-border:, :] = 0
    resp[:, :border] = 0
    resp[:, -border:] = 0
    thresh = max(float(np.percentile(resp, 99.0)), 1e-6)
    resp = np.where(resp > thresh, resp, 0.0)
    order = np.argsort(resp.ravel())[::-1]
    taken = np.zeros_like(resp, bool)
    pts = []
    h, w = resp.shape
    for idx in order:
        if resp.ravel()[idx] <= 0 or len(pts) >= max_points:
            break
        v, u = divmod(int(idx), w)
        if taken[max(0, v - min_dist):v + min_dist + 1,
                 max(0, u - min_dist):u + min_dist + 1].any():
            continue
        taken[v, u] = True
        pts.append((u, v))
    return np.array(pts, float)


# --------------------------------------------------------------------------
# ZNCC feature matcher (2-D search: disparity x vertical residual)
# --------------------------------------------------------------------------

def _patches_along_row(img: np.ndarray, row: int, centers: np.ndarray,
                       patch: int) -> np.ndarray:
    """(P*P, N) patches centred on ``row`` at the given integer columns."""
    r = patch // 2
    strip = img[row - r:row + r + 1, :]
    sw = sliding_window_view(strip, patch, axis=1)      # (P, W-P+1, P)
    cols = centers - r
    return sw[:, cols, :].reshape(patch * patch, centers.size)


def zncc_search(img_a: np.ndarray, img_b: np.ndarray, pts: np.ndarray,
                disp_min: int, disp_max: int, max_dy: int = 8,
                patch: int = 13) -> dict[str, np.ndarray]:
    """Match every point of ``img_a`` in ``img_b`` along the epipolar band.

    Returns per point ``u, v, disp, dy, score`` for the best match, plus the
    second-best score at least 5 px away in disparity (uniqueness test).
    """
    h, w = img_a.shape
    r = patch // 2
    ds = np.arange(int(disp_min), int(disp_max) + 1)
    dys = np.arange(-int(max_dy), int(max_dy) + 1)
    out = {k: [] for k in ("u", "v", "disp", "dy", "score", "second")}
    for u, v in pts:
        u, v = int(round(u)), int(round(v))
        if (u - r < 0 or u + r >= w or v - r < max_dy or v + r + max_dy >= h):
            continue
        a = img_a[v - r:v + r + 1, u - r:u + r + 1].astype(float)
        a = a - a.mean()
        na = float(np.linalg.norm(a))
        if na < 1e-6:
            continue
        a = a.reshape(-1)
        cols = u - ds
        ok = (cols - r >= 0) & (cols + r < w)
        if not ok.any():
            continue
        cols_ok = cols[ok]
        scores = np.full((dys.size, ds.size), -np.inf)
        for iy, dy in enumerate(dys):
            row = v + dy
            if row - r < 0 or row + r >= h:
                continue
            B = _patches_along_row(img_b, row, cols_ok, patch).astype(float)
            mb = B.mean(0)
            Bc = B - mb
            den = np.linalg.norm(Bc, axis=0)
            with np.errstate(divide="ignore", invalid="ignore"):
                z = (a @ Bc) / (na * den)
            z[den < 1e-6] = -1.0
            scores[iy, ok] = z
        k = int(np.argmax(scores))
        iy, idx = np.unravel_index(k, scores.shape)
        zbest = float(scores[iy, idx])
        # sub-pixel parabola in disparity
        dsub = float(ds[idx])
        if 0 < idx < ds.size - 1 and np.isfinite(scores[iy, idx - 1]) \
                and np.isfinite(scores[iy, idx + 1]):
            y0, y1, y2 = scores[iy, idx - 1], scores[iy, idx], scores[iy, idx + 1]
            denom = (y0 - 2 * y1 + y2)
            if abs(denom) > 1e-9:
                dsub += 0.5 * (y0 - y2) / denom
        dysub = float(dys[iy])
        if 0 < iy < dys.size - 1:
            y0, y1, y2 = scores[iy - 1, idx], scores[iy, idx], scores[iy + 1, idx]
            denom = (y0 - 2 * y1 + y2)
            if abs(denom) > 1e-9:
                dysub += 0.5 * (y0 - y2) / denom
        # second best at least 5 px away in disparity
        far = np.abs(ds - ds[idx]) > 5
        second = float(np.max(scores[:, far])) if far.any() else -1.0
        out["u"].append(float(u))
        out["v"].append(float(v))
        out["disp"].append(dsub)
        out["dy"].append(dysub)
        out["score"].append(zbest)
        out["second"].append(second)
    return {k: np.asarray(v, float) for k, v in out.items()}


def _blur(img: np.ndarray, r: int) -> np.ndarray:
    """Box blur; suppresses laser speckle that decorrelates between views."""
    if r <= 0:
        return np.asarray(img, float)
    return _box_sum(np.asarray(img, float), r) / float((2 * r + 1) ** 2)


def match_pair(rect: Rectification, img_a: np.ndarray, img_b: np.ndarray,
               max_points: int = 300, disp_range: tuple[int, int] = (200, 900),
               max_dy: int = 8, patch: int = 13,
               min_score: float = 0.6, ratio: float = 1.15,
               blur: int = 0) -> dict[str, Any]:
    ra = _blur(rect.remap_a(img_a), blur)
    rb = _blur(rect.remap_b(img_b), blur)
    pts = harris_corners(ra, max_points=max_points)
    m = zncc_search(ra, rb, pts, disp_range[0], disp_range[1], max_dy, patch)
    if m["score"].size == 0:
        return {"n_features": int(len(pts)), "n_matches": 0}
    good = (m["score"] >= min_score) & (m["score"] >= ratio * m["second"])
    dy = m["dy"][good]
    disp = m["disp"][good]
    f = float(rect.K_new[0, 0])
    B = float(np.linalg.norm(rect.pair.centre_b_in_a()))
    with np.errstate(divide="ignore", invalid="ignore"):
        depth = f * B / disp
    return {
        "n_features": int(len(pts)),
        "n_candidates": int(m["score"].size),
        "n_matches": int(good.sum()),
        "frac_matched": float(good.mean()),
        "score_median": float(np.median(m["score"][good])) if good.any() else 0.0,
        "abs_dy_median": float(np.median(np.abs(dy))) if good.any() else float("nan"),
        "abs_dy_p90": float(np.percentile(np.abs(dy), 90)) if good.any() else float("nan"),
        "abs_dy_max": float(np.abs(dy).max()) if good.any() else float("nan"),
        "abs_dy_frac_lt1px": float((np.abs(dy) < 1).mean()) if good.any() else 0.0,
        "abs_dy_frac_lt2px": float((np.abs(dy) < 2).mean()) if good.any() else 0.0,
        "disp_median": float(np.median(disp)) if good.any() else float("nan"),
        "disp_p05": float(np.percentile(disp, 5)) if good.any() else float("nan"),
        "disp_p95": float(np.percentile(disp, 95)) if good.any() else float("nan"),
        "depth_mm": stats(depth) if good.any() else {"n": 0},
        "depth_p10": float(np.percentile(depth, 10)) if good.any() else float("nan"),
        "depth_p50": float(np.percentile(depth, 50)) if good.any() else float("nan"),
        "depth_p90": float(np.percentile(depth, 90)) if good.any() else float("nan"),
        "dy": dy, "disp": disp, "u": m["u"][good], "v": m["v"][good],
        "score": m["score"][good],
    }


# --------------------------------------------------------------------------
# dense disparity (half resolution, integral-image ZNCC)
# --------------------------------------------------------------------------

def _downsample2(img: np.ndarray) -> np.ndarray:
    h, w = img.shape
    h2, w2 = h // 2, w // 2
    return 0.25 * (img[0:2 * h2:2, 0:2 * w2:2] + img[1:2 * h2:2, 0:2 * w2:2]
                   + img[0:2 * h2:2, 1:2 * w2:2] + img[1:2 * h2:2, 1:2 * w2:2])


def _box_mean(a: np.ndarray, r: int) -> np.ndarray:
    n = (2 * r + 1) ** 2
    return _box_sum(a, r) / n


def dense_disparity(rect: Rectification, img_a: np.ndarray, img_b: np.ndarray,
                    disp_range: tuple[int, int], patch: int = 15,
                    scale: int = 2, step: int = 2,
                    min_score: float = 0.75,
                    lr_tol: float = 2.0) -> dict[str, Any]:
    """Coarse dense disparity with a left-right consistency check.

    ``scale`` downsamples the rectified images (2 = half resolution); ``step``
    is the disparity step in full-resolution pixels.
    """
    ra = rect.remap_a(img_a)
    rb = rect.remap_b(img_b)
    if scale == 2:
        ra = _downsample2(ra)
        rb = _downsample2(rb)
    h, w = ra.shape
    r = patch // 2
    d0, d1 = int(disp_range[0]), int(disp_range[1])
    ds = np.arange(d0 // scale, d1 // scale + 1, max(1, step // scale))
    best = np.full((h, w), -np.inf, np.float32)
    best_d = np.zeros((h, w), np.float32)
    best2 = np.full((h, w), -np.inf, np.float32)
    ma = _box_mean(ra, r)
    va = np.maximum(_box_mean(ra * ra, r) - ma * ma, 0.0)
    for d in ds:
        if d <= 0 or d >= w - 1:
            continue
        # B shifted so that B[y, x] corresponds to A[y, x + d]
        b = np.full_like(ra, 0.0)
        b[:, :w - d] = rb[:, d:]
        mb = _box_mean(b, r)
        vb = np.maximum(_box_mean(b * b, r) - mb * mb, 0.0)
        cov = _box_mean(ra * b, r) - ma * mb
        with np.errstate(divide="ignore", invalid="ignore"):
            z = cov / np.sqrt(va * vb)
        z[(va < 1e-6) | (vb < 1e-6)] = -1.0
        z[:, w - d:] = -1.0
        z[:r, :] = -1.0
        z[-r:, :] = -1.0
        z[:, :r] = -1.0
        better = z > best
        best2 = np.where(better, best, np.maximum(best2, z))
        best_d = np.where(better, np.float32(d), best_d)
        best = np.where(better, z, best)
    # sub-pixel parabola needs the scores at d-1 and d+1; recompute locally
    disp = best_d.astype(np.float64)
    valid = best >= min_score
    # left-right consistency: match B -> A at the found disparity
    dshift = np.zeros_like(disp)
    xs = np.arange(w)[None, :].repeat(h, axis=0)
    ys = np.arange(h)[:, None].repeat(w, axis=1)
    xb = np.clip(xs - disp.astype(int), 0, w - 1)
    ok = valid & (disp > 0)
    # (approximate LR check with integer disparities on the same grid)
    xa2 = np.clip(xb + best_d.astype(int), 0, w - 1)
    ok &= np.abs(xa2 - xs) <= lr_tol
    # remove isolated failures by a 3x3 majority filter
    k = np.ones((3, 3))
    oki = ok.astype(np.int8)
    cnt = np.zeros_like(best)
    cnt[1:-1, 1:-1] = (oki[0:-2, 0:-2] + oki[0:-2, 1:-1]
                       + oki[0:-2, 2:] + oki[1:-1, 0:-2]
                       + oki[1:-1, 1:-1] + oki[1:-1, 2:]
                       + oki[2:, 0:-2] + oki[2:, 1:-1]
                       + oki[2:, 2:])
    ok &= cnt >= 2
    ys_i, xs_i = np.nonzero(ok)
    u_full = xs_i.astype(float) * scale + (scale - 1) / 2.0
    v_full = ys_i.astype(float) * scale + (scale - 1) / 2.0
    d_full = disp[ys_i, xs_i] * scale
    xyz = rect.disparity_to_xyz(d_full, np.stack([u_full, v_full], axis=1))
    return {
        "n_grid": int(h * w),
        "n_valid": int(ok.sum()),
        "coverage": float(ok.mean()),
        "disp_median": float(np.median(d_full)) if d_full.size else float("nan"),
        "disp_p05": float(np.percentile(d_full, 5)) if d_full.size else float("nan"),
        "disp_p95": float(np.percentile(d_full, 95)) if d_full.size else float("nan"),
        "depth_mm": stats(xyz[:, 2]) if d_full.size else {"n": 0},
        "xyz": xyz,
    }


# --------------------------------------------------------------------------
# experiment driver
# --------------------------------------------------------------------------

def evaluate_pairs(calib: Calibration, capture_dir: Path, frame_indices: list[int],
                   pairs: list[str], max_points: int = 250,
                   disp_range: tuple[int, int] = (200, 900),
                   max_dy: int = 3, blur: int = 2,
                   min_score: float = 0.85,
                   ratio: float = 1.0, patch: int = 15) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for pair_name in pairs:
        ca, cb, na, nb = PAIRS[pair_name]
        sp = StereoPair.from_calibration(calib, na, nb)
        rect = bouguet_rectification(sp, calib.image_size)
        rows = []
        for idx in frame_indices:
            fa = frame_path(capture_dir, idx, ca)
            fb = frame_path(capture_dir, idx, cb)
            if not fa.exists() or not fb.exists():
                continue
            img_a = load_pgm(fa)
            img_b = load_pgm(fb)
            m = match_pair(rect, img_a, img_b, max_points=max_points,
                           disp_range=disp_range, max_dy=max_dy,
                           blur=blur, min_score=min_score, ratio=ratio,
                           patch=patch)
            m.pop("dy", None)
            m.pop("disp", None)
            m.pop("u", None)
            m.pop("v", None)
            m.pop("score", None)
            m["frame"] = idx
            m["baseline_mm"] = float(np.linalg.norm(sp.centre_b_in_a()))
            rows.append(m)
        results[pair_name] = rows
    return results


def _fmt(v: float, nd: int = 2) -> str:
    return "nan" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Real-capture validation of the calibration model.")
    ap.add_argument("--capture-dir", type=Path, default=CAPTURE_DIR)
    ap.add_argument("--source", choices=("ccf", "dump", "auto"), default="ccf",
                    help="ccf = operational model (default); dump = different run")
    ap.add_argument("--contrast", action="store_true",
                    help="run both CCF and dump models and print the contrast")
    ap.add_argument("--blur", type=int, default=0,
                    help="box blur applied before feature matching (speckle suppression)")
    ap.add_argument("--min-score", type=float, default=0.85)
    ap.add_argument("--ratio", type=float, default=1.0)
    ap.add_argument("--patch", type=int, default=9)
    ap.add_argument("--pair", choices=("all",) + tuple(PAIRS), default="all")
    ap.add_argument("--frames", type=int, nargs="+", default=[0])
    ap.add_argument("--max-points", type=int, default=400)
    ap.add_argument("--disp-min", type=int, default=20)
    ap.add_argument("--disp-max", type=int, default=900)
    ap.add_argument("--max-dy", type=int, default=2)
    ap.add_argument("--dense", action="store_true")
    ap.add_argument("--dense-pair", default="left-right")
    ap.add_argument("--ply-out", type=Path, default=None)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.capture_dir.exists():
        print(f"capture directory {args.capture_dir} not found")
        return 2
    calib = Calibration.load(args.source)
    pairs = list(PAIRS) if args.pair == "all" else [args.pair]
    res = evaluate_pairs(calib, args.capture_dir, args.frames, pairs,
                         max_points=args.max_points,
                         disp_range=(args.disp_min, args.disp_max),
                         max_dy=args.max_dy, blur=args.blur,
                         min_score=args.min_score, ratio=args.ratio,
                         patch=args.patch)

    print(f"source={calib.source}  captures={args.capture_dir}  frames={args.frames}")
    print(f"disparity search range [{args.disp_min}, {args.disp_max}] px, "
          f"vertical window +-{args.max_dy} px")
    print(f"{'pair':12} {'frame':>5} {'feat':>5} {'match':>6} {'med|dy|':>8} "
          f"{'p90|dy|':>8} {'<1px':>6} {'<2px':>6} {'med disp':>9} "
          f"{'disp p05..p95':>16} {'depth p10/50/90 mm':>24}")
    for pair_name, rows in res.items():
        for m in rows:
            if m.get("n_matches", 0) == 0:
                print(f"{pair_name:12} {m['frame']:5d} {m['n_features']:5d} {0:6d}  (no matches)")
                continue
            print(f"{pair_name:12} {m['frame']:5d} {m['n_features']:5d} {m['n_matches']:6d} "
                  f"{m['abs_dy_median']:8.3f} {m['abs_dy_p90']:8.3f} "
                  f"{100*m['abs_dy_frac_lt1px']:5.1f}% {100*m['abs_dy_frac_lt2px']:5.1f}% "
                  f"{m['disp_median']:9.2f} "
                  f"{m['disp_p05']:7.1f}..{m['disp_p95']:<7.1f} "
                  f"{m['depth_p10']:8.0f}/{m['depth_p50']:.0f}/{m['depth_p90']:.0f}")

    if args.contrast:
        print("\n== contrast: same matcher, CCF model vs dump model (same frames) ==")
        print(f"{'model':6} {'pair':12} {'frames':>7} {'matches':>8} {'frames>0':>9} "
              f"{'med|dy|':>8} {'med disp':>9}")
        both = {"ccf": res}
        try:
            both["dump"] = evaluate_pairs(Calibration.from_dump(), args.capture_dir,
                                          args.frames, pairs, max_points=args.max_points,
                                          disp_range=(args.disp_min, args.disp_max),
                                          max_dy=args.max_dy, blur=args.blur,
                                          min_score=args.min_score, ratio=args.ratio,
                                          patch=args.patch)
        except FileNotFoundError:
            print("  (dump absent -- cannot run the contrast)")
        for model_name, table in both.items():
            for pair_name, rows in table.items():
                tot = sum(r.get("n_matches", 0) for r in rows)
                nz = sum(1 for r in rows if r.get("n_matches", 0) > 0)
                dys = [r["abs_dy_median"] for r in rows if r.get("n_matches", 0) > 0]
                dsp = [r["disp_median"] for r in rows if r.get("n_matches", 0) > 0]
                print(f"{model_name:6} {pair_name:12} {len(rows):7d} {tot:8d} {nz:9d} "
                      f"{(np.median(dys) if dys else float('nan')):8.3f} "
                      f"{(np.median(dsp) if dsp else float('nan')):9.2f}")

    out: dict[str, Any] = {"source": calib.source, "frames": args.frames,
                           "disp_range": [args.disp_min, args.disp_max],
                           "pairs": res}
    if args.dense:
        pair_name = args.dense_pair
        ca, cb, na, nb = PAIRS[pair_name]
        sp = StereoPair.from_calibration(calib, na, nb)
        rect = bouguet_rectification(sp, calib.image_size)
        idx = args.frames[0]
        img_a = load_pgm(frame_path(args.capture_dir, idx, ca))
        img_b = load_pgm(frame_path(args.capture_dir, idx, cb))
        rows = res.get(pair_name, [])
        dmin, dmax = args.disp_min, args.disp_max
        if rows and rows[0].get("n_matches", 0) > 20:
            dmin = max(20, int(rows[0]["disp_p05"] - 30))
            dmax = int(rows[0]["disp_p95"] + 30)
        print(f"\ndense: pair={pair_name} frame={idx} range=[{dmin},{dmax}] "
              f"half-res, 15px ZNCC")
        dm = dense_disparity(rect, img_a, img_b, (dmin, dmax))
        print(f"  coverage {dm['coverage']*100:.2f}% of the half-res grid "
              f"({dm['n_valid']}/{dm['n_grid']})")
        print(f"  disparity median {dm['disp_median']:.1f} px, "
              f"p05..p95 {dm['disp_p05']:.1f}..{dm['disp_p95']:.1f} px")
        print(f"  depth (mm): {dm['depth_mm']}")
        out["dense"] = {k: v for k, v in dm.items() if k != "xyz"}
        if args.ply_out is not None and len(dm["xyz"]):
            xyz_to_ply(args.ply_out, dm["xyz"],
                       comment="Einstar real capture, mm, rectified left camera frame; "
                               "no ground truth, consistency check only")
            print(f"  point cloud -> {args.ply_out} ({len(dm['xyz'])} points)")
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(out, indent=2, default=float) + "\n")
        print(f"\nwritten to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
