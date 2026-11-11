"""Export and project persistence for scans.

A scan is only useful if the result can leave the application: this module
writes the mesh to the formats people actually open (OBJ, PLY, STL) and saves a
*project* — the configuration, the per-frame poses and statuses, and the mesh
path — so a scan can be reopened, re-fused at a different resolution, or
compared later without re-running the reconstruction.

All writers are ASCII and dependency-free; the numbers are millimetres, which
is what the reconstruction produces.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Mapping

import numpy as np

__all__ = ["write_ply", "write_stl", "export_mesh", "save_project",
           "load_project", "PROJECT_SCHEMA"]

PROJECT_SCHEMA = "einstar.scan.project/1"


def write_ply(path: str | Path, vertices: np.ndarray, faces: np.ndarray,
              comment: str = "") -> Path:
    """ASCII PLY with vertices and triangular faces (units: mm)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    verts = np.asarray(vertices, float)
    tris = np.asarray(faces, np.int64)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("ply\nformat ascii 1.0\n")
        if comment:
            fh.write(f"comment {comment}\n")
        fh.write("comment units=mm\n")
        fh.write(f"element vertex {len(verts)}\n")
        fh.write("property float x\nproperty float y\nproperty float z\n")
        fh.write(f"element face {len(tris)}\n")
        fh.write("property list uchar int vertex_indices\nend_header\n")
        for x, y, z in verts:
            fh.write(f"{x:.4f} {y:.4f} {z:.4f}\n")
        for a, b, c in tris:
            fh.write(f"3 {a} {b} {c}\n")
    return path


def write_stl(path: str | Path, vertices: np.ndarray,
              faces: np.ndarray) -> Path:
    """ASCII STL (facet normals computed from the triangles, units: mm)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    verts = np.asarray(vertices, float)
    tris = np.asarray(faces, np.int64)
    tri = verts[tris]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    length[length == 0] = 1.0
    normals = normals / length
    with path.open("w", encoding="utf-8") as fh:
        fh.write("solid einstar\n")
        for n, t in zip(normals, tri):
            fh.write(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n")
            fh.write("    outer loop\n")
            for v in t:
                fh.write(f"      vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}\n")
            fh.write("    endloop\n  endfacet\n")
        fh.write("endsolid einstar\n")
    return path


def export_mesh(directory: str | Path, vertices: np.ndarray, faces: np.ndarray,
                *, stem: str = "scan", formats=("obj", "ply", "stl"),
                comment: str = "") -> dict:
    """Write the mesh in every requested format; returns the written paths."""
    from tools.fuse.tsdf import write_obj

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for fmt in formats:
        fmt = fmt.lower()
        if fmt == "obj":
            written["obj"] = str(write_obj(directory / f"{stem}.obj", vertices,
                                           faces, comment=comment))
        elif fmt == "ply":
            written["ply"] = str(write_ply(directory / f"{stem}.ply", vertices,
                                           faces, comment=comment))
        elif fmt == "stl":
            written["stl"] = str(write_stl(directory / f"{stem}.stl", vertices, faces))
        else:
            raise ValueError(f"unknown export format {fmt!r}")
    return written


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------

def save_project(path: str | Path, *, config, report: Mapping,
                 mesh: Mapping | None = None, extra: Mapping | None = None) -> Path:
    """Persist a scan project: configuration, poses, statuses and mesh paths."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": PROJECT_SCHEMA,
        "config": {k: (str(v) if isinstance(v, Path) else v)
                   for k, v in asdict(config).items()},
        "report": {k: v for k, v in report.items() if not k.startswith("_")},
        "mesh": dict(mesh or {}),
    }
    if extra:
        doc["extra"] = dict(extra)
    path.write_text(json.dumps(doc, indent=1))
    return path


def load_project(path: str | Path) -> dict:
    """Read a project and check that it is one."""
    path = Path(path)
    doc = json.loads(path.read_text())
    schema = doc.get("schema")
    if schema != PROJECT_SCHEMA:
        raise ValueError(f"{path}: not a scan project (schema {schema!r})")
    return doc


def project_clouds_available(project: Mapping) -> bool:
    """Whether the frames referenced by a project are still on disk."""
    frames = (project.get("config") or {}).get("frames_dir")
    return bool(frames) and Path(str(frames)).is_dir()
