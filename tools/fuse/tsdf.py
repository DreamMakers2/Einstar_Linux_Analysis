"""Multi-frame fusion: confidence-weighted TSDF and mesh extraction.

The reconstruction engine produces one point cloud per frame and the tracking
baseline produces the pose of each frame.  This module fuses them into a single
surface.

Why a TSDF and not a point-cloud union: repeated observations of the same
surface must *average* (reducing noise), and a volumetric surface is what makes
meshing and later refinement possible.  The weight of each update is the
per-point confidence, so a noisy grazing observation cannot outvote a clean
frontal one.

The grid is a dense axis-aligned volume around the fused data.  At the scales
this scanner works at (0.3-0.8 m, objects tens of centimetres) a 2 mm grid over
a 0.5 m cube is 250^3 = 15.6 M voxels, about 125 MB for the two float32 arrays;
the CLI therefore has a hard default cap and reports the allocation before it
runs.  Sparse hashing is the production path (`docs/scanner_roadmap.md`).

Units are millimetres throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time
from typing import Iterable, Sequence

import numpy as np

try:
    from scipy.spatial import cKDTree
except Exception:  # pragma: no cover
    cKDTree = None

__all__ = ["Volume", "make_volume", "integrate", "extract_mesh", "write_obj",
           "fuse_clouds"]


@dataclass
class Volume:
    origin: np.ndarray            # world coordinate of voxel (0,0,0)
    voxel_mm: float
    shape: tuple[int, int, int]
    tsdf: np.ndarray              # float32, weighted mean signed distance
    weight: np.ndarray            # float32, accumulated confidence

    @property
    def n_voxels(self) -> int:
        return int(np.prod(self.shape))

    def memory_mb(self) -> float:
        return float(self.tsdf.nbytes + self.weight.nbytes) / 1e6


def _neighbour_offsets(radius: int) -> np.ndarray:
    """Offsets inside a sphere of ``radius`` voxels.

    A cube would be 343 taps at radius 3 where the sphere needs 123, and the
    corner taps are outside the truncation band anyway.  The band is bounded by
    |s| < trunc, so the spherical set loses nothing and is ~2.8x cheaper.
    """
    r = int(max(1, radius))
    axis = np.arange(-r, r + 1)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    flat = grid.reshape(-1, 3)
    inside = np.sum(flat.astype(float) ** 2, axis=1) <= (r * r + r * 0.5)
    return flat[inside]


def make_volume(points: Sequence[np.ndarray], voxel_mm: float = 2.0,
                margin_mm: float = 20.0, max_voxels: int = 40_000_000) -> Volume:
    """Allocate a volume that contains every point plus a margin."""
    pts = np.vstack([np.asarray(p, float) for p in points])
    if len(pts) == 0:
        raise ValueError("no points to bound")
    lo = pts.min(axis=0) - margin_mm
    hi = pts.max(axis=0) + margin_mm
    shape = np.maximum(np.ceil((hi - lo) / voxel_mm).astype(int) + 1, 3)
    if int(np.prod(shape)) > max_voxels:
        raise ValueError(
            f"volume {tuple(int(x) for x in shape)} = {int(np.prod(shape))} "
            f"voxels exceeds the {max_voxels} cap; increase voxel_mm")
    # +trunc everywhere: "unknown" reads as outside, so an unobserved region can
    # never mesh as a surface.  Values are signed distances in millimetres.
    tsdf = np.ones(tuple(int(x) for x in shape), np.float32)
    weight = np.zeros_like(tsdf)
    return Volume(np.asarray(lo, float), float(voxel_mm),
                  tuple(int(x) for x in shape), tsdf, weight)


def observed_fraction(volume: Volume, points: np.ndarray) -> float:
    """Fraction of ``points`` whose voxel has already been observed.

    A scanner should tell the operator how much *new* surface a frame brings;
    a frame that only revisits observed voxels adds nothing.  This is a voxel
    lookup, not a surface-distance test, so it is cheap and deterministic.
    """
    pts = np.asarray(points, float).reshape(-1, 3)
    if len(pts) == 0:
        return 0.0
    idx = np.floor((pts - volume.origin) / float(volume.voxel_mm)).astype(np.int64)
    shape = np.asarray(volume.tsdf.shape, np.int64)
    inside = np.all((idx >= 0) & (idx < shape), axis=1)
    if not inside.any():
        return 0.0
    idx = idx[inside]
    seen = volume.weight[idx[:, 0], idx[:, 1], idx[:, 2]] > 0
    return float(seen.mean())


def integrate(volume: Volume, points: np.ndarray, normals: np.ndarray,
              *, trunc_mm: float | None = None, confidence: np.ndarray | None = None,
              max_weight: float = 64.0) -> dict:
    """Splat one cloud (already transformed into the volume frame) into ``volume``.

    Each point writes ``s = (v - p) . n`` into voxels within ``trunc_mm`` of the
    surface, with the point's confidence as the weight.  Voxels accumulate a
    weighted mean, so repeated observations average their noise.
    """
    t0 = time.time()
    pts = np.asarray(points, float)
    nrm = np.asarray(normals, float)
    if len(pts) == 0:
        return {"points": 0, "updates": 0, "seconds": 0.0}
    if len(nrm) != len(pts):
        raise ValueError("points and normals must be the same length")
    trunc = float(trunc_mm if trunc_mm is not None else 3.0 * volume.voxel_mm)
    if confidence is None:
        conf = np.ones(len(pts), np.float32)
    else:
        conf = np.clip(np.asarray(confidence, float), 0.0, 1.0).astype(np.float32)
    v = float(volume.voxel_mm)
    radius = int(max(1, math.ceil(trunc / v)))
    offsets = _neighbour_offsets(radius)
    base = np.floor((pts - volume.origin) / v).astype(np.int64)
    shape = np.asarray(volume.shape, np.int64)
    # 1. collect the voxels within the truncation band of any point (cheap
    #    kernel sweep; only the index set is kept)
    candidates = []
    for off in offsets:
        idx = base + off
        inside = np.all((idx >= 0) & (idx < shape), axis=1)
        if np.any(inside):
            candidates.append(idx[inside])
    if not candidates:
        return {"points": int(len(pts)), "updates": 0, "seconds": 0.0,
                "trunc_mm": trunc, "radius_voxels": radius}
    flat = np.ravel_multi_index(np.vstack(candidates).T, volume.shape)
    uniq = np.unique(flat)
    idx = np.stack(np.unravel_index(uniq, volume.shape), axis=1)
    centre = volume.origin + (idx.astype(np.float64) + 0.5) * v
    # 2. signed distance from each voxel to the NEAREST point, evaluated on that
    #    point's tangent plane.  Splatting every point's plane instead (the
    #    usual point-cloud trick) biases a convex surface inward by up to the
    #    splat radius - measured 2.1 mm on a 40 mm sphere at 2 mm voxels - which
    #    nearest-point evaluation avoids by construction.
    if cKDTree is None:  # pragma: no cover
        raise RuntimeError("scipy is required for fusion")
    tree = cKDTree(pts)
    dist, near = tree.query(centre, k=1)
    s = np.einsum("ij,ij->i", centre - pts[near], nrm[near])
    keep = (dist < 2.0 * trunc) & (np.abs(s) < trunc)
    if not np.any(keep):
        return {"points": int(len(pts)), "updates": 0,
                "seconds": float(time.time() - t0), "trunc_mm": trunc,
                "radius_voxels": radius}
    uniq = uniq[keep]
    s = s[keep].astype(np.float64)
    c = conf[near[keep]].astype(np.float64)
    tsdf_flat = volume.tsdf.reshape(-1)
    weight_flat = volume.weight.reshape(-1)
    old_w = weight_flat[uniq].astype(np.float64)
    old_t = tsdf_flat[uniq].astype(np.float64)
    new_w = old_w + c
    tsdf_flat[uniq] = np.clip((old_t * old_w + s * c) / np.maximum(new_w, 1e-12),
                              -trunc, trunc).astype(np.float32)
    weight_flat[uniq] = np.minimum(new_w, float(max_weight)).astype(np.float32)
    updates = int(len(uniq))
    return {"points": int(len(pts)), "updates": int(updates),
            "seconds": float(time.time() - t0), "trunc_mm": trunc,
            "radius_voxels": radius}


def extract_mesh(volume: Volume, *, level: float = 0.0, fill_unobserved: bool = True,
                 points: np.ndarray | None = None,
                 normals: np.ndarray | None = None,
                 max_band_mm: float | None = None,
                 trim_border: bool = True):
    """Marching cubes on the fused field; returns (vertices_mm, faces)."""
    try:
        from skimage import measure
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("scikit-image is required for meshing") from exc
    field = np.asarray(volume.tsdf, np.float32).copy()
    if fill_unobserved:
        # unobserved voxels must read as clearly outside, otherwise a hole in
        # the data meshes as a surface at level 0
        field[volume.weight <= 0] = float(np.max(field[volume.weight > 0])) \
            if np.any(volume.weight > 0) else 1.0
    if not (field.min() < level < field.max()):
        raise ValueError("level is outside the fused field; nothing to mesh")
    verts, faces, _, _ = measure.marching_cubes(
        field, level=float(level), spacing=(volume.voxel_mm,) * 3)
    # marching_cubes places vertices at integer sample coordinates, while the
    # field is sampled at voxel CENTRES (origin + (idx + 0.5) * voxel), so the
    # half-voxel offset has to be put back or the whole mesh shifts inward.
    verts = (np.asarray(verts, float) + 0.5 * volume.voxel_mm
             + np.asarray(volume.origin, float))
    faces = np.asarray(faces, np.int32)
    # The volume is only *observed* in a band of +-trunc around the points;
    # everywhere else is unknown and reads as outside.  That leaves a spurious
    # shell at the inner edge of the band (measured: a 40 mm sphere meshed 2.4 mm
    # small).  Keep only triangles whose vertices lie within the measured band
    # of the real surface, which removes the shell and nothing else.
    if trim_border and len(verts):
        # Point-cloud fusion knows nothing beyond the observed region, so
        # marching cubes closes an open surface with a wall at its rim.  That
        # wall is invented geometry: drop triangles that touch the border of the
        # observed band, keeping the measured surface (and reporting an open
        # mesh) instead of a sealed box around the data.
        try:
            from scipy import ndimage as _ndi
            obs = volume.weight > 0
            interior = _ndi.binary_erosion(obs, structure=np.ones((3, 3, 3), bool),
                                           border_value=0)
            idx = np.floor((verts - volume.origin) / volume.voxel_mm).astype(np.int64)
            idx = np.clip(idx, 0, np.asarray(volume.shape) - 1)
            inside = interior[idx[:, 0], idx[:, 1], idx[:, 2]]
            if np.any(inside):
                keep = np.all(inside[faces], axis=1)
                faces = faces[keep]
                used = np.unique(faces) if len(faces) else np.zeros(0, int)
                remap = -np.ones(len(verts), int)
                remap[used] = np.arange(len(used))
                verts = verts[used]
                faces = remap[faces] if len(faces) else faces.reshape(0, 3)
        except Exception:
            pass
    if points is not None and normals is not None and len(verts):
        band = float(max_band_mm if max_band_mm is not None
                     else 0.75 * float(np.max(np.abs(volume.tsdf[volume.weight > 0])))
                     if np.any(volume.weight > 0) else volume.voxel_mm)
        tree = cKDTree(np.asarray(points, float))
        _, near = tree.query(verts, k=1)
        s = np.einsum("ij,ij->i", verts - np.asarray(points, float)[near],
                      np.asarray(normals, float)[near])
        good = np.abs(s) <= band
        if np.any(good):
            keep = np.all(good[faces], axis=1)
            faces = faces[keep]
            used = np.unique(faces) if len(faces) else np.zeros(0, int)
            remap = -np.ones(len(verts), int)
            remap[used] = np.arange(len(used))
            verts = verts[used]
            faces = remap[faces] if len(faces) else faces.reshape(0, 3)
    return verts, faces


def write_obj(path: str | Path, vertices: np.ndarray, faces: np.ndarray,
              comment: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        if comment:
            fh.write(f"# {comment}\n")
        fh.write(f"# {len(vertices)} vertices {len(faces)} faces units=mm\n")
        for x, y, z in np.asarray(vertices, float):
            fh.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
        for a, b, c in np.asarray(faces, np.int64) + 1:
            fh.write(f"f {a} {b} {c}\n")
    return path


def fuse_clouds(clouds: Sequence, poses: Sequence[np.ndarray] | None = None,
                *, voxel_mm: float = 2.0, trunc_mm: float | None = None,
                ok: Sequence[bool] | None = None,
                mesh: bool = True, max_voxels: int = 40_000_000) -> dict:
    """Fuse points + normals (camera frame) with poses (camera -> world).

    ``ok`` marks which frames may be integrated: a frame whose tracking failed
    must never contribute, or the model absorbs a wrong pose.
    """
    world_points: list[np.ndarray] = []
    world_normals: list[np.ndarray] = []
    used = 0
    for i, cloud in enumerate(clouds):
        if ok is not None and not bool(ok[i]):
            continue
        P = np.asarray(cloud.points, float)
        N = getattr(cloud, "normals", None)
        if N is None:
            continue
        T = np.eye(4) if poses is None else np.asarray(poses[min(i, len(poses) - 1)], float)
        world_points.append((T[:3, :3] @ P.T).T + T[:3, 3])
        world_normals.append((T[:3, :3] @ np.asarray(N, float).T).T)
        used += 1
    if not world_points:
        raise ValueError("no integrable frames")
    volume = make_volume(world_points, voxel_mm=voxel_mm, max_voxels=max_voxels)
    stats = []
    for P, N in zip(world_points, world_normals):
        stats.append(integrate(volume, P, N, trunc_mm=trunc_mm))
    out = {"frames_integrated": used,
           "voxels": volume.n_voxels, "voxel_mm": volume.voxel_mm,
           "shape": list(volume.shape), "memory_mb": volume.memory_mb(),
           "observed_voxels": int((volume.weight > 0).sum()),
           "integrate_seconds": float(sum(s["seconds"] for s in stats)),
           "updates": int(sum(s["updates"] for s in stats))}
    if mesh:
        t0 = time.time()
        all_points = np.vstack(world_points)
        all_normals = np.vstack(world_normals)
        verts, faces = extract_mesh(volume, points=all_points,
                                    normals=all_normals)
        out.update(vertices=int(len(verts)), faces=int(len(faces)),
                   mesh_seconds=float(time.time() - t0))
        out["_mesh"] = (verts, faces)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Fuse point clouds into a TSDF mesh")
    ap.add_argument("--clouds", nargs="+", required=True)
    ap.add_argument("--poses", help="JSON with a list of 4x4 pose matrices")
    ap.add_argument("--out", required=True, help="output .obj")
    ap.add_argument("--voxel-mm", type=float, default=2.0)
    ap.add_argument("--trunc-mm", type=float, default=None)
    ap.add_argument("--report", help="write a JSON report here")
    args = ap.parse_args(argv)

    from tools.track.baseline import load_clouds
    clouds = load_clouds(args.clouds)
    poses = None
    if args.poses:
        poses = [np.asarray(p, float) for p in json.loads(Path(args.poses).read_text())]
    report = fuse_clouds(clouds, poses, voxel_mm=args.voxel_mm,
                         trunc_mm=args.trunc_mm)
    verts, faces = report.pop("_mesh")
    write_obj(args.out, verts, faces, comment="einstar fuse")
    report["obj"] = str(args.out)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
