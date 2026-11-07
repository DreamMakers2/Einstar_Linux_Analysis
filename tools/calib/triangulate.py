#!/usr/bin/env python3
"""Disparity -> 3-D triangulation and point-cloud export.

Three equivalent routes are provided and cross-checked by the validator:

1. ``Q`` matrix (the form returned by :mod:`rectify`),
2. the rectified projection matrices ``P1``/``P2`` (linear DLT),
3. the original camera models ``K, dist, R, T`` (DLT on the *undistorted*
   pixels, which is the only route that works for arbitrary camera pairs).

All 3-D results are millimetres in the frame named by the caller; the
rectified routes return points in the **rectified left camera** frame, whose
axes coincide with the left camera's axes rotated by ``R1``.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------
# triangulation
# --------------------------------------------------------------------------

def triangulate_dlt(P1: np.ndarray, P2: np.ndarray,
                    uv1: np.ndarray, uv2: np.ndarray) -> np.ndarray:
    """Linear (DLT) triangulation of corresponding pixels.

    ``P1``/``P2`` are 3x4 projection matrices acting on the *same* 3-D frame;
    ``uv1``/``uv2`` are the matching (N,2) pixel arrays.  Returns (N,3).
    """
    P1 = np.asarray(P1, float)
    P2 = np.asarray(P2, float)
    uv1 = np.asarray(uv1, float).reshape(-1, 2)
    uv2 = np.asarray(uv2, float).reshape(-1, 2)
    if uv1.shape != uv2.shape:
        raise ValueError("uv1 and uv2 must have the same shape")
    n = uv1.shape[0]
    out = np.empty((n, 3), float)
    for i in range(n):
        u1, v1 = uv1[i]
        u2, v2 = uv2[i]
        A = np.vstack([
            u1 * P1[2] - P1[0],
            v1 * P1[2] - P1[1],
            u2 * P2[2] - P2[0],
            v2 * P2[2] - P2[1],
        ])
        _, _, vt = np.linalg.svd(A)
        X = vt[-1]
        if abs(X[3]) < 1e-15:
            out[i] = np.nan
        else:
            out[i] = X[:3] / X[3]
    return out


def triangulate_rectified(rect, uv_left: np.ndarray, disp: np.ndarray) -> np.ndarray:
    """Disparity -> 3-D with the Q matrix (rectified left camera frame)."""
    return rect.disparity_to_xyz(disp, uv_left)


def depth_from_disparity(disp: np.ndarray, f: float, baseline: float) -> np.ndarray:
    """``Z = f * B / d`` (mm) for a rectified pair."""
    disp = np.asarray(disp, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return f * baseline / disp


def disparity_from_depth(depth: np.ndarray, f: float, baseline: float) -> np.ndarray:
    """``d = f * B / Z`` (px)."""
    depth = np.asarray(depth, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return f * baseline / depth


# --------------------------------------------------------------------------
# reprojection / error metrics
# --------------------------------------------------------------------------

def reproject(P: np.ndarray, xyz: np.ndarray) -> np.ndarray:
    X = np.asarray(xyz, float).reshape(-1, 3)
    h = np.concatenate([X, np.ones((X.shape[0], 1))], axis=1) @ np.asarray(P, float).T
    return h[:, :2] / h[:, 2:3]


def reprojection_errors(P1, P2, xyz, uv1, uv2) -> dict[str, np.ndarray]:
    e1 = np.linalg.norm(reproject(P1, xyz) - np.asarray(uv1, float), axis=1)
    e2 = np.linalg.norm(reproject(P2, xyz) - np.asarray(uv2, float), axis=1)
    return {"cam1_px": e1, "cam2_px": e2}


def stats(a: np.ndarray) -> dict[str, float]:
    """Compact summary of an error array."""
    a = np.asarray(a, float).ravel()
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "rms": float(np.sqrt(np.mean(a * a))),
        "p95": float(np.percentile(a, 95)),
        "max": float(a.max()),
    }


# --------------------------------------------------------------------------
# point-cloud export
# --------------------------------------------------------------------------

def xyz_to_ply(path: str | Path, xyz: np.ndarray,
               colors: np.ndarray | None = None,
               binary: bool = True,
               comment: str = "Einstar point cloud, mm") -> Path:
    """Write an (N,3) point array as a PLY file.

    ``colors`` is an optional (N,3) uint8 array.  A ``comment`` records the
    coordinate convention so the file is self-describing.
    """
    xyz = np.asarray(xyz, float).reshape(-1, 3)
    if colors is not None:
        colors = np.asarray(colors)
        if colors.shape != xyz.shape:
            raise ValueError("colors must be (N,3)")
        colors = np.clip(colors, 0, 255).astype(np.uint8)
    n = xyz.shape[0]
    path = Path(path)
    header = ["ply"]
    header.append("format binary_little_endian 1.0" if binary else "format ascii 1.0")
    for line in str(comment).splitlines():
        header.append(f"comment {line}")
    header.append(f"element vertex {n}")
    header += ["property float x", "property float y", "property float z"]
    if colors is not None:
        header += ["property uchar red", "property uchar green", "property uchar blue"]
    header.append("end_header")
    text = "\n".join(header) + "\n"
    if binary:
        with path.open("wb") as fh:
            fh.write(text.encode("ascii"))
            buf = np.empty(n, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4")])
            buf["x"], buf["y"], buf["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
            if colors is not None:
                buf2 = np.empty(n, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                          ("red", "u1"), ("green", "u1"), ("blue", "u1")])
                buf2["x"], buf2["y"], buf2["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
                buf2["red"], buf2["green"], buf2["blue"] = colors[:, 0], colors[:, 1], colors[:, 2]
                buf = buf2
            fh.write(buf.tobytes())
    else:
        with path.open("w", encoding="ascii") as fh:
            fh.write(text)
            if colors is None:
                for x, y, z in xyz:
                    fh.write(f"{x:.6f} {y:.6f} {z:.6f}\n")
            else:
                for (x, y, z), (r, g, b) in zip(xyz, colors):
                    fh.write(f"{x:.6f} {y:.6f} {z:.6f} {r} {g} {b}\n")
    return path


def read_ply_vertices(path: str | Path) -> np.ndarray:
    """Minimal reader for the files written by :func:`xyz_to_ply`."""
    path = Path(path)
    data = path.read_bytes()
    end = data.index(b"end_header\n") + len(b"end_header\n")
    header = data[:end].decode("ascii").splitlines()
    count = 0
    binary = False
    has_color = False
    fmt = ""
    for line in header:
        if line.startswith("format"):
            fmt = line
            binary = "binary" in line
        elif line.startswith("element vertex"):
            count = int(line.split()[-1])
        elif line.startswith("property uchar red"):
            has_color = True
    if not binary:
        vals = np.loadtxt(path, skiprows=len(header), ndmin=2)
        return np.asarray(vals, float)[:, :3]
    dtype = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if has_color:
        dtype += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    arr = np.frombuffer(data[end:end + count * np.dtype(dtype).itemsize], dtype=dtype)
    return np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(float)


# --------------------------------------------------------------------------
# dense disparity maps
# --------------------------------------------------------------------------

def disparity_map_to_cloud(rect, disp: np.ndarray,
                           valid: np.ndarray | None = None) -> np.ndarray:
    """Convert a dense disparity map (H,W, px) to (N,3) mm points.

    ``valid`` selects which pixels to keep (default: finite and positive
    disparity).
    """
    disp = np.asarray(disp, float)
    if disp.ndim != 2:
        raise ValueError("disparity map must be 2-D")
    h, w = disp.shape
    if (w, h) != tuple(rect.image_size):
        raise ValueError(f"disparity map {w}x{h} does not match rectification "
                         f"{rect.image_size}")
    vv, uu = np.mgrid[0:h, 0:w]
    ok = np.isfinite(disp) & (disp > 0)
    if valid is not None:
        ok &= np.asarray(valid, bool)
    uv = np.stack([uu[ok], vv[ok]], axis=1).astype(float)
    d = disp[ok]
    return rect.disparity_to_xyz(d, uv)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from calib_model import Calibration, StereoPair
    from rectify import bouguet_rectification

    ap = argparse.ArgumentParser(description="Triangulate a disparity map to a point cloud.")
    ap.add_argument("--disparity", type=Path, required=True,
                    help=".npy file with an (H,W) float disparity map in px")
    ap.add_argument("--source", choices=("ccf", "dump", "auto"), default="auto")
    ap.add_argument("--output", type=Path, required=True, help="output .ply")
    ap.add_argument("--ascii", action="store_true")
    args = ap.parse_args(argv)

    disp = np.load(args.disparity)
    calib = Calibration.load(args.source)
    rect = bouguet_rectification(StereoPair.from_calibration(calib, "left", "right"),
                                 calib.image_size)
    xyz = disparity_map_to_cloud(rect, disp)
    xyz_to_ply(args.output, xyz, binary=not args.ascii,
               comment="Einstar, mm, rectified left camera frame")
    print(f"{xyz.shape[0]} points -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
