"""Projective point-to-plane ICP on the engine's depth maps.

The cloud-based tracker (`tools/track/baseline.py`) associates points through a
KD-tree over voxel-downsampled clouds.  That throws away the image structure the
reconstruction already provides and makes each correspondence a search.  The
depth map is an organised surface: a point projects to a *pixel*, so the
correspondence is a lookup, the target normal comes from adjacent depth samples,
and the whole step is vectorised.

This module implements that path and measures it against the cloud baseline with
the same cross-frame consistency metric, so the choice is evidence-based.

Depth maps are the engine's `recon_depth_mm.tiff` (float32 millimetres, stored
along the left IR camera axes after the rectification rotation), so the
intrinsics are the left IR camera's and the depth is the same `z_mm` the PLY
carries.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Sequence

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

__all__ = ["DepthFrame", "load_depth", "normals_from_depth",
           "align_projective", "surface_consistency"]


@dataclass
class DepthFrame:
    depth_mm: np.ndarray        # (H, W) float32, <= 0 = invalid
    K: np.ndarray               # 3x3
    confidence: np.ndarray | None = None

    @property
    def valid(self) -> np.ndarray:
        return np.isfinite(self.depth_mm) & (self.depth_mm > 0)

    def points(self) -> np.ndarray:
        """(N, 3) points in camera coordinates, plus their pixel coordinates."""
        h, w = self.depth_mm.shape
        v, u = np.nonzero(self.valid)
        z = self.depth_mm[v, u].astype(np.float64)
        fx, fy = float(self.K[0, 0]), float(self.K[1, 1])
        cx, cy = float(self.K[0, 2]), float(self.K[1, 2])
        x = (u - cx) * z / fx
        y = (v - cy) * z / fy
        return np.stack([x, y, z], axis=1), np.stack([u, v], axis=1)


def intrinsics_from_report(report: dict) -> np.ndarray:
    """K for the depth map from the reconstruction report's ``rectified`` block.

    The depth map lives on the *scaled rectified* grid, so the full-resolution
    calibration intrinsics are wrong for it by the scale factor (and by the
    rectification itself).  Using them silently produced nonsense rays - a
    320-pixel-wide map with cx=624 puts every pixel left of the principal
    point - which is why this block exists.
    """
    rect = (report or {}).get("rectified") or {}
    f = rect.get("f")
    cx = rect.get("cx")
    cy = rect.get("cy")
    if f is None or cx is None or cy is None:
        raise ValueError("report has no rectified intrinsics")
    return np.array([[float(f), 0.0, float(cx)],
                     [0.0, float(f), float(cy)],
                     [0.0, 0.0, 1.0]])


def load_depth(path, K: np.ndarray | None = None, confidence_path=None, *,
               report=None) -> DepthFrame:
    if cv2 is None:  # pragma: no cover
        raise RuntimeError("OpenCV is required to read the depth maps")
    if K is None:
        import json as _json
        rep = report
        if rep is None:
            sibling = Path(path).parent / "recon_report.json"
            if not sibling.is_file():
                raise ValueError("load_depth needs K or a recon_report.json")
            rep = _json.loads(sibling.read_text())
        K = intrinsics_from_report(rep)
    depth = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if depth is None:
        raise ValueError(f"cannot read {path}")
    if depth.ndim != 2:
        depth = depth[..., 0]
    depth = np.asarray(depth, np.float32)
    if float(np.nanmax(depth)) < 1e-6:
        raise ValueError(f"{path}: empty depth map")
    conf = None
    if confidence_path is not None:
        conf = cv2.imread(str(confidence_path), cv2.IMREAD_UNCHANGED)
        if conf is not None:
            if conf.ndim != 2:
                conf = conf[..., 0]
            conf = np.asarray(conf, np.float32)
    return DepthFrame(depth, np.asarray(K, float), conf)


def normals_from_depth(depth: np.ndarray, K: np.ndarray) -> np.ndarray:
    """Per-pixel normals from central differences (zeros where invalid)."""
    d = np.asarray(depth, np.float32)
    h, w = d.shape
    fx, fy = float(K[0, 0]), float(K[1, 1])
    valid = np.isfinite(d) & (d > 0)
    dd = np.where(valid, d, np.nan)
    dzdx = np.full_like(dd, np.nan)
    dzdy = np.full_like(dd, np.nan)
    dzdx[:, 1:-1] = (dd[:, 2:] - dd[:, :-2]) * 0.5 * fx / np.maximum(dd[:, 1:-1], 1e-6)
    dzdy[1:-1, :] = (dd[2:, :] - dd[:-2, :]) * 0.5 * fy / np.maximum(dd[1:-1, :], 1e-6)
    n = np.stack([-dzdx, -dzdy, np.ones_like(dd)], axis=-1)
    length = np.linalg.norm(n, axis=-1, keepdims=True)
    length[~np.isfinite(length)] = 1.0
    usable = np.isfinite(n).all(axis=-1) & np.isfinite(length[..., 0])
    usable &= (length[..., 0] > 0)
    n = np.where(usable[..., None], n, 0.0)
    n[~valid] = 0.0
    n[~usable] = 0.0
    return n.astype(np.float32)


def _se3(R, t):
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def align_projective(source: DepthFrame, target: DepthFrame,
                     init: np.ndarray | None = None, *,
                     max_iterations: int = 20,
                     trim: float = 0.85,
                     distance_threshold_mm: float = 20.0,
                     tolerance_mm: float = 1e-4,
                     max_points: int = 0) -> dict:
    """Projective point-to-plane ICP of ``source`` onto ``target``."""
    t0 = time.time()
    h, w = source.depth_mm.shape
    if (target.depth_mm.shape != (h, w)):
        raise ValueError("source and target depth maps must have the same size")
    K = source.K
    pts, uv = source.points()
    if max_points and len(pts) > int(max_points):
        # stride sampling: the plane/rotation estimate does not need every pixel,
        # and the cost is linear in the point count (measured: 409 ms at scale
        # 0.5 with 115k points against ~100 ms with 29k)
        stride = int(np.ceil(len(pts) / float(max_points)))
        pts = pts[::stride]
        uv = uv[::stride]
    if len(pts) < 500:
        return {"ok": False, "reason": "too few valid pixels", "transform": np.eye(4),
                "rms_mm": float("nan"), "inliers": 0, "iterations": 0,
                "runtime_s": time.time() - t0, "condition": float("inf")}
    target_normals = normals_from_depth(target.depth_mm, K)
    T = np.eye(4) if init is None else np.asarray(init, float).copy()
    fx, fy = float(K[0, 0]), float(K[1, 1])
    cx, cy = float(K[0, 2]), float(K[1, 2])
    rms = float("nan")
    used = 0
    condition = float("inf")
    iterations = 0
    for iterations in range(1, int(max_iterations) + 1):
        moved = (T[:3, :3] @ pts.T).T + T[:3, 3]
        z = moved[:, 2]
        front = z > 1.0
        if front.sum() < 500:
            break
        u = np.rint(fx * moved[:, 0] / np.where(front, z, 1.0) + cx).astype(np.int64)
        v = np.rint(fy * moved[:, 1] / np.where(front, z, 1.0) + cy).astype(np.int64)
        inside = front & (u >= 1) & (u < w - 1) & (v >= 1) & (v < h - 1)
        if inside.sum() < 500:
            break
        idx = np.where(inside)[0]
        tu, tv = u[idx], v[idx]
        target_depth = target.depth_mm[tv, tu]
        has = np.isfinite(target_depth) & (target_depth > 0)
        idx, tu, tv = idx[has], tu[has], tv[has]
        target_depth = target_depth[has]
        if len(idx) < 500:
            break
        p = moved[idx]
        q = np.stack([(tu - cx) * target_depth / fx,
                      (tv - cy) * target_depth / fy,
                      target_depth], axis=1)
        n = target_normals[tv, tu]
        good = np.linalg.norm(n, axis=1) > 0.5
        idx, p, q, n = idx[good], p[good], q[good], n[good]
        if len(idx) < 500:
            break
        r = np.einsum("ij,ij->i", p - q, n)
        keep = np.abs(r) < distance_threshold_mm
        if keep.sum() < 500:
            break
        p, q, n, r = p[keep], q[keep], n[keep], r[keep]
        order = np.argsort(np.abs(r))
        n_keep = max(200, int(round(float(trim) * len(order))))
        sel = order[:n_keep]
        p, q, n, r = p[sel], q[sel], n[sel], r[sel]
        used = len(p)
        rms = float(np.sqrt(np.mean(r ** 2)))
        A = np.hstack([np.cross(p, n), n])
        radius = float(np.sqrt(np.mean(np.sum(p ** 2, axis=1))))
        scale = np.array([radius, radius, radius, 1.0, 1.0, 1.0])
        An = A / scale[None, :]
        try:
            ev = np.linalg.eigvalsh(An.T @ An)
            condition = float(ev[-1] / max(ev[0], 1e-12)) if ev[-1] > 0 else float("inf")
        except np.linalg.LinAlgError:  # pragma: no cover
            condition = float("inf")
        try:
            x, *_ = np.linalg.lstsq(A, -r, rcond=None)
        except np.linalg.LinAlgError:  # pragma: no cover
            break
        omega = x[:3]
        theta = float(np.linalg.norm(omega))
        if theta > 1e-12:
            axis = omega / theta
            Kx = np.array([[0, -axis[2], axis[1]],
                           [axis[2], 0, -axis[0]],
                           [-axis[1], axis[0], 0]])
            R_step = np.eye(3) + math.sin(theta) * Kx + (1 - math.cos(theta)) * (Kx @ Kx)
        else:
            R_step = np.eye(3)
        T = _se3(R_step, x[3:]) @ T
        if abs(float(np.linalg.norm(x))) < tolerance_mm:
            break
    translation = float(np.linalg.norm(T[:3, 3]))
    c = (float(np.trace(T[:3, :3])) - 1.0) / 2.0
    rotation = float(math.degrees(math.acos(max(-1.0, min(1.0, c)))))
    ok = bool(used >= 500 and math.isfinite(rms) and rms <= 5.0 and rotation <= 30.0
              and math.isfinite(condition) and condition <= 1e6)
    return {"ok": ok, "reason": "ok" if ok else "failed", "transform": T,
            "translation_mm": translation, "rotation_deg": rotation,
            "rms_mm": rms, "inliers": int(used), "iterations": int(iterations),
            "runtime_s": time.time() - t0, "condition": condition}


def surface_consistency(frames: Sequence[DepthFrame],
                        poses: Sequence[np.ndarray], *,
                        sample: int = 1500) -> float:
    """Median distance from each frame's points to the union of the others."""
    from scipy.spatial import cKDTree
    clouds = []
    for frame, pose in zip(frames, poses):
        pts, _ = frame.points()
        P = np.asarray(pose, float)
        clouds.append((P[:3, :3] @ pts.T).T + P[:3, 3])
    medians = []
    for i, pts in enumerate(clouds):
        other = np.vstack([c for j, c in enumerate(clouds) if j != i])
        tree = cKDTree(other)
        sel = pts[np.linspace(0, len(pts) - 1, min(sample, len(pts))).astype(int)]
        d, _ = tree.query(sel, k=1)
        medians.append(float(np.median(d)))
    return float(np.median(medians))
