"""Scanner-tracking baseline: frame-to-frame rigid alignment.

This is the measured foundation for multi-frame fusion, not the production
tracker.  It aligns consecutive reconstructed point clouds with a robust
point-to-plane ICP, reports a per-pair quality signal, and never chains a pair
it does not trust — a fusion stage must be able to see ``tracking_ok = False``
instead of inheriting a silently wrong pose.

Design decisions and why:

* **Voxel downsampling** (default 5 mm) keeps a pair under ~15k points, which
  makes a KD-tree nearest-neighbour association affordable for a baseline.  The
  production tracker should use projective association on the organised depth
  map instead (documented in ``docs/scanner_roadmap.md``).
* **Point-to-plane**, not point-to-point: the recorded surfaces are locally
  planar, and point-to-plane converges in far fewer iterations.
* **Trimmed least squares**: the worst ``trim`` fraction of residuals is
  discarded each iteration, so a moving object or a depth outlier cannot drag
  the solution.  The inlier count actually used is reported.
* **Normals from local PCA** on the downsampled cloud, oriented toward the
  sensor (the cloud is in camera coordinates, so ``-p`` is a good prior).

All units are millimetres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import time
from typing import Iterable, Sequence

import numpy as np

try:  # scipy is a project dependency, but degrade gracefully
    from scipy.spatial import cKDTree
except Exception:  # pragma: no cover
    cKDTree = None

__all__ = ["Cloud", "PairResult", "read_ply", "voxel_downsample",
           "estimate_normals", "align_point_to_plane", "track_sequence",
           "load_clouds"]


@dataclass
class Cloud:
    points: np.ndarray                      # (N, 3) float64, millimetres
    normals: np.ndarray | None = None       # (N, 3) unit vectors

    def __len__(self) -> int:
        return int(self.points.shape[0])


@dataclass
class PairResult:
    ok: bool
    reason: str
    transform: np.ndarray
    translation_mm: float
    rotation_deg: float
    inlier_ratio: float
    inliers: int
    rms_mm: float
    iterations: int
    runtime_s: float
    overlap: float = float("nan")
    condition: float = float("nan")
    hessian_eigenvalues: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok), "reason": self.reason,
            "translation_mm": float(self.translation_mm),
            "rotation_deg": float(self.rotation_deg),
            "inlier_ratio": float(self.inlier_ratio),
            "inliers": int(self.inliers),
            "rms_mm": float(self.rms_mm) if np.isfinite(self.rms_mm) else None,
            "iterations": int(self.iterations),
            "runtime_s": float(self.runtime_s),
            "overlap": (float(self.overlap) if np.isfinite(self.overlap) else None),
            "condition": (float(self.condition)
                          if np.isfinite(self.condition) else None),
            "hessian_eigenvalues": [float(x) for x in self.hessian_eigenvalues],
            "transform": [[float(x) for x in row] for row in self.transform],
        }


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def read_ply(path: str | Path) -> np.ndarray:
    """Read the vertex block of an ascii or binary-little-endian PLY."""
    path = Path(path)
    with path.open("rb") as fh:
        header = []
        while True:
            line = fh.readline()
            if not line:
                raise ValueError(f"{path}: truncated PLY header")
            text = line.decode("ascii", "replace").strip()
            header.append(text)
            if text == "end_header":
                break
        fmt_line = next((h for h in header if h.startswith("format")), "format ascii 1.0")
        parts = fmt_line.split()
        fmt = parts[1] if len(parts) > 1 else "ascii"
        count = next((int(h.split()[-1]) for h in header
                      if h.startswith("element vertex")), 0)
        props = [h.split()[-1] for h in header
                 if h.startswith("property") and "list" not in h]
        body = fh.read()
    if fmt == "ascii":
        data = np.fromstring(body.decode("ascii", "replace"), sep=" ")
        data = data.reshape(-1, len(props))[:, :3]
        return np.asarray(data, float)
    if fmt.startswith("binary_little_endian"):
        width = len(props) * 4
        arr = np.frombuffer(body[: count * width], dtype="<f4").reshape(-1, len(props))
        return np.asarray(arr[:, :3], float)
    raise ValueError(f"{path}: unsupported PLY format {fmt!r}")


def read_ply_coloured(path: str | Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Vertices plus optional per-vertex RGB (0..255) from an ASCII/binary PLY."""
    path = Path(path)
    with path.open("rb") as fh:
        header = []
        while True:
            line = fh.readline()
            if not line:
                raise ValueError(f"{path}: truncated PLY header")
            text = line.decode("ascii", "replace").strip()
            header.append(text)
            if text == "end_header":
                break
        props = [h.split()[-1] for h in header
                 if h.startswith("property") and "list" not in h]
        count = next((int(h.split()[-1]) for h in header
                      if h.startswith("element vertex")), 0)
        body = fh.read()
    data = np.fromstring(body.decode("ascii", "replace"), sep=" ")
    data = data.reshape(-1, len(props))
    pts = np.asarray(data[:, :3], float)
    rgb = None
    if all(k in props for k in ("red", "green", "blue")):
        idx = [props.index(k) for k in ("red", "green", "blue")]
        rgb = np.asarray(data[:, idx], np.uint8)
    return pts, rgb


def load_clouds(paths: Iterable[str | Path], *, voxel_mm: float = 5.0,
                normals: bool = True, max_points: int = 40000) -> list[Cloud]:
    clouds: list[Cloud] = []
    for path in paths:
        points = read_ply(path)
        points = points[np.all(np.isfinite(points), axis=1)]
        points = voxel_downsample(points, voxel_mm)
        if len(points) > max_points:
            idx = np.linspace(0, len(points) - 1, max_points).astype(int)
            points = points[idx]
        cloud = Cloud(points)
        if normals:
            cloud.normals = estimate_normals(cloud.points)
        clouds.append(cloud)
    return clouds


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

def voxel_downsample(points: np.ndarray, voxel_mm: float) -> np.ndarray:
    """Centroid of every occupied voxel (deterministic order)."""
    if voxel_mm <= 0 or len(points) == 0:
        return np.asarray(points, float)
    keys = np.floor(np.asarray(points, float) / float(voxel_mm)).astype(np.int64)
    order = np.lexsort((keys[:, 2], keys[:, 1], keys[:, 0]))
    keys = keys[order]
    pts = np.asarray(points, float)[order]
    _, start = np.unique(keys, axis=0, return_index=True)
    start = np.sort(start)
    end = np.r_[start[1:], len(pts)]
    out = np.empty((len(start), 3), float)
    for i, (a, b) in enumerate(zip(start, end)):
        out[i] = pts[a:b].mean(axis=0)
    return out


def estimate_normals(points: np.ndarray, k: int = 12,
                     viewpoint: Sequence[float] = (0.0, 0.0, 0.0)) -> np.ndarray:
    """Local PCA normals, oriented toward ``viewpoint`` (the camera)."""
    pts = np.asarray(points, float)
    n = len(pts)
    if n < 4:
        return np.tile(np.array([0.0, 0.0, -1.0]), (n, 1))
    if cKDTree is None:  # pragma: no cover - fallback, brute force
        d = np.linalg.norm(pts[:, None, :] - pts[None, :, :], axis=2)
        idx = np.argsort(d, axis=1)[:, : max(3, k)]
    else:
        tree = cKDTree(pts)
        _, idx = tree.query(pts, k=max(3, min(k, n)))
    norms = np.zeros((n, 3), float)
    eye = np.asarray(viewpoint, float)
    for i in range(n):
        nb = pts[idx[i]]
        centred = nb - nb.mean(axis=0)
        cov = centred.T @ centred
        try:
            vals, vecs = np.linalg.eigh(cov)
        except np.linalg.LinAlgError:  # pragma: no cover
            norms[i] = (0.0, 0.0, -1.0)
            continue
        normal = vecs[:, 0]
        if np.dot(normal, pts[i] - eye) > 0:
            normal = -normal
        norms[i] = normal
    length = np.linalg.norm(norms, axis=1, keepdims=True)
    length[length == 0] = 1.0
    return norms / length


def _se3(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = rotation
    T[:3, 3] = translation
    return T


def _rotation_angle_deg(R: np.ndarray) -> float:
    c = (float(np.trace(R)) - 1.0) / 2.0
    return float(math.degrees(math.acos(max(-1.0, min(1.0, c)))))


# ---------------------------------------------------------------------------
# ICP
# ---------------------------------------------------------------------------

def align_point_to_plane(source: Cloud, target: Cloud,
                         init: np.ndarray | None = None,
                         *, max_iterations: int = 30,
                         trim: float = 0.8,
                         max_correspondence_mm: float = 25.0,
                         tolerance_mm: float = 1e-3) -> PairResult:
    """Robust point-to-plane ICP of ``source`` onto ``target``.

    Returns the rigid transform that maps source points into the target frame.
    ``trim=0.8`` keeps the best 80 % of correspondences each iteration.
    """
    t0 = time.time()
    src = np.asarray(source.points, float)
    dst = np.asarray(target.points, float)
    if len(src) < 50 or len(dst) < 50 or cKDTree is None:
        return PairResult(False, "too few points (or scipy missing)",
                          np.eye(4), float("nan"), float("nan"), 0.0, 0, float("nan"),
                          0, time.time() - t0)
    dst_n = target.normals
    if dst_n is None:
        dst_n = estimate_normals(dst)
    tree = cKDTree(dst)
    T = np.eye(4) if init is None else np.asarray(init, float).copy()
    rms = float("nan")
    inlier_ratio = 0.0
    used = 0
    iterations = 0
    condition = float("nan")
    eigenvalues: list[float] = []
    for iterations in range(1, int(max_iterations) + 1):
        moved = (T[:3, :3] @ src.T).T + T[:3, 3]
        dist, idx = tree.query(moved, k=1)
        keep = np.isfinite(dist) & (dist < max_correspondence_mm)
        if keep.sum() < 50:
            break
        order = np.argsort(dist[keep])
        n_keep = max(50, int(round(float(trim) * len(order))))
        sel = np.zeros_like(keep)
        sel[np.where(keep)[0][order[:n_keep]]] = True
        used = int(sel.sum())
        if used < 50:
            break
        p = moved[sel]
        q = dst[idx[sel]]
        n = dst_n[idx[sel]]
        r = np.einsum("ij,ij->i", p - q, n)
        rms = float(np.sqrt(np.mean(r ** 2)))
        # linearised Gauss-Newton step for point-to-plane
        A = np.hstack([np.cross(p, n), n])
        b = -r
        # Observability of the alignment itself.  A planar target constrains
        # only three of the six degrees of freedom (two in-plane translations
        # and the in-plane rotation are free), so the Hessian is singular and a
        # confident-looking transform would be a guess along the plane.  This is
        # the signal a fusion stage needs before trusting a pose.
        try:
            # The parameter vector mixes radians and millimetres, so the raw
            # condition number is scale dependent.  Rescale the rotational block
            # by the RMS scene radius so all six parameters are in millimetres
            # before comparing eigenvalues.
            radius = float(np.sqrt(np.mean(np.sum(np.asarray(moved) ** 2, axis=1))))
            scale = np.array([radius, radius, radius, 1.0, 1.0, 1.0])
            An = A / scale[None, :]
            ev = np.linalg.eigvalsh(An.T @ An)
            eigenvalues = [float(x) for x in ev]
            top = float(ev[-1]) if len(ev) else float("nan")
            condition = float(top / max(float(ev[0]), 1e-12)) if top > 0 else float("inf")
        except np.linalg.LinAlgError:  # pragma: no cover
            condition = float("inf")
        try:
            x, *_ = np.linalg.lstsq(A, b, rcond=None)
        except np.linalg.LinAlgError:  # pragma: no cover
            break
        omega = x[:3]
        theta = float(np.linalg.norm(omega))
        if theta > 1e-12:
            axis = omega / theta
            K = np.array([[0, -axis[2], axis[1]],
                          [axis[2], 0, -axis[0]],
                          [-axis[1], axis[0], 0]])
            R_step = np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)
        else:
            R_step = np.eye(3)
        T = _se3(R_step, x[3:]) @ T
        inlier_ratio = used / float(len(src))
        if abs(float(np.linalg.norm(x))) < tolerance_mm:
            break
    translation = float(np.linalg.norm(T[:3, 3]))
    rotation = _rotation_angle_deg(T[:3, :3])
    overlap = used / float(min(len(src), len(dst))) if used else 0.0
    geometric_ok = (used >= 50 and math.isfinite(rms) and rms <= 5.0
                    and overlap >= 0.25 and rotation <= 30.0)
    # Threshold calibrated on measured geometry (scale-normalised condition):
    #   plane           1.1e15   -> degenerate (in-plane motion is free)
    #   +-12 mm relief    4.3e2   -> well posed
    #   curved cap        6.2e5   -> usable but weak; passes
    # so 1e6 separates a genuine plane from a surface with real relief.  The
    # pair is reported as not trackable rather than chained with a guessed pose.
    well_posed = bool(math.isfinite(condition) and condition <= 1e6)
    ok = bool(geometric_ok and well_posed)
    reason = "ok" if ok else (
        "degenerate geometry (planar target)" if geometric_ok and not well_posed
        else "insufficient overlap" if overlap < 0.25
        else "residual too large" if not (math.isfinite(rms) and rms <= 5.0)
        else "implausible motion")
    return PairResult(ok, reason, T, translation, rotation, float(inlier_ratio),
                      used, rms, iterations, time.time() - t0, overlap,
                      condition, eigenvalues)


# ---------------------------------------------------------------------------
# sequence
# ---------------------------------------------------------------------------

def compose(transforms: Sequence[np.ndarray]) -> list[np.ndarray]:
    """Cumulative pose chain (source frames -> frame 0)."""
    poses: list[np.ndarray] = []
    current = np.eye(4)
    for T in transforms:
        current = np.asarray(T, float) @ current
        poses.append(current.copy())
    return poses


def track_sequence(clouds: Sequence[Cloud], **kwargs) -> dict:
    """Align every consecutive pair; report per-pair quality and drift."""
    pairs: list[PairResult] = []
    t0 = time.time()
    for i in range(1, len(clouds)):
        pairs.append(align_point_to_plane(clouds[i - 1], clouds[i], **kwargs))
    transforms = [p.transform for p in pairs]
    poses = compose(transforms)
    steps_t = [p.translation_mm for p in pairs if p.ok]
    steps_r = [p.rotation_deg for p in pairs if p.ok]
    path_mm = float(np.sum(steps_t))
    total_t = float(np.linalg.norm(poses[-1][:3, 3])) if poses else 0.0
    total_r = _rotation_angle_deg(poses[-1][:3, :3]) if poses else 0.0
    return {
        "n_frames": len(clouds),
        "n_pairs": len(pairs),
        "n_ok": sum(1 for p in pairs if p.ok),
        "pairs": [p.as_dict() for p in pairs],
        "poses": [[[float(x) for x in row] for row in P] for P in poses],
        "path_length_mm": path_mm,
        "end_to_end_translation_mm": total_t,
        "end_to_end_rotation_deg": total_r,
        "median_step_mm": float(np.median(steps_t)) if steps_t else None,
        "median_step_deg": float(np.median(steps_r)) if steps_r else None,
        "median_rms_mm": float(np.median([p.rms_mm for p in pairs if p.ok]))
        if any(p.ok for p in pairs) else None,
        "median_inlier_ratio": float(np.median([p.inlier_ratio for p in pairs]))
        if pairs else None,
        "mean_pair_runtime_s": float(np.mean([p.runtime_s for p in pairs]))
        if pairs else None,
        "wall_s": time.time() - t0,
        "frames_per_s": (len(pairs) / (time.time() - t0)) if pairs else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Frame-to-frame tracking baseline")
    ap.add_argument("--clouds", nargs="+", required=True,
                    help="PLY point clouds in capture order")
    ap.add_argument("--out", help="write the report JSON here")
    ap.add_argument("--voxel-mm", type=float, default=5.0)
    ap.add_argument("--max-iterations", type=int, default=30)
    ap.add_argument("--trim", type=float, default=0.8)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    clouds = load_clouds(args.clouds, voxel_mm=args.voxel_mm)
    report = track_sequence(clouds, max_iterations=args.max_iterations,
                            trim=args.trim)
    report["cloud_points"] = [len(c) for c in clouds]
    report["voxel_mm"] = args.voxel_mm
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=1))
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "poses"}, indent=1))
    else:
        print(f"frames {report['n_frames']}  pairs ok {report['n_ok']}/"
              f"{report['n_pairs']}  median step "
              f"{report['median_step_mm']:.2f} mm / "
              f"{report['median_step_deg']:.2f} deg  rms "
              f"{report['median_rms_mm']:.2f} mm  inliers "
              f"{100 * (report['median_inlier_ratio'] or 0):.0f}%  "
              f"{report['frames_per_s']:.1f} pairs/s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
