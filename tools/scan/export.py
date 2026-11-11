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
           "load_project", "read_ply_mesh", "PROJECT_SCHEMA"]

PROJECT_SCHEMA = "einstar.scan.project/1"


def write_ply(path: str | Path, vertices: np.ndarray, faces: np.ndarray,
              comment: str = "", colors: np.ndarray | None = None) -> Path:
    """ASCII PLY with vertices, optional per-vertex RGB, and triangular faces."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    verts = np.asarray(vertices, float)
    tris = np.asarray(faces, np.int64)
    rgb = None if colors is None else np.asarray(colors, np.uint8).reshape(-1, 3)
    if rgb is not None and len(rgb) != len(verts):
        raise ValueError("colours and vertices must have the same length")
    with path.open("w", encoding="utf-8") as fh:
        fh.write("ply\nformat ascii 1.0\n")
        if comment:
            fh.write(f"comment {comment}\n")
        fh.write("comment units=mm\n")
        fh.write(f"element vertex {len(verts)}\n")
        fh.write("property float x\nproperty float y\nproperty float z\n")
        if rgb is not None:
            fh.write("property uchar red\nproperty uchar green\n"
                     "property uchar blue\n")
        fh.write(f"element face {len(tris)}\n")
        fh.write("property list uchar int vertex_indices\nend_header\n")
        if rgb is None:
            for x, y, z in verts:
                fh.write(f"{x:.4f} {y:.4f} {z:.4f}\n")
        else:
            for (x, y, z), (r, g, b) in zip(verts, rgb):
                fh.write(f"{x:.4f} {y:.4f} {z:.4f} {int(r)} {int(g)} {int(b)}\n")
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
                comment: str = "", colors: np.ndarray | None = None) -> dict:
    """Write the mesh in every requested format; returns the written paths."""
    from tools.fuse.tsdf import write_obj

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for fmt in formats:
        fmt = fmt.lower()
        if fmt == "obj":
            written["obj"] = str(write_obj_coloured(
                directory / f"{stem}.obj", vertices, faces, colors,
                comment=comment))
        elif fmt == "ply":
            written["ply"] = str(write_ply(directory / f"{stem}.ply", vertices,
                                           faces, comment=comment, colors=colors))
        elif fmt == "stl":
            written["stl"] = str(write_stl(directory / f"{stem}.stl", vertices, faces))
        else:
            raise ValueError(f"unknown export format {fmt!r}")
    return written


def write_obj_coloured(path: str | Path, vertices: np.ndarray, faces: np.ndarray,
                       colors: np.ndarray | None, comment: str = "") -> Path:
    """OBJ with extended per-vertex colour (``v x y z r g b``, 0..1)."""
    from tools.fuse.tsdf import write_obj

    if colors is None:
        return write_obj(path, vertices, faces, comment=comment)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    verts = np.asarray(vertices, float)
    tris = np.asarray(faces, np.int64)
    rgb = np.asarray(colors, float).reshape(-1, 3) / 255.0
    with path.open("w", encoding="utf-8") as fh:
        if comment:
            fh.write(f"# {comment}\n")
        fh.write(f"# {len(verts)} vertices {len(tris)} faces units=mm\n")
        for (x, y, z), (r, g, b) in zip(verts, rgb):
            fh.write(f"v {x:.4f} {y:.4f} {z:.4f} {r:.4f} {g:.4f} {b:.4f}\n")
        for a, b, c in tris + 1:
            fh.write(f"f {a} {b} {c}\n")
    return path


def read_ply_mesh(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Read the vertices and triangle faces of an ASCII PLY written here."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        header = []
        for line in fh:
            header.append(line.strip())
            if header[-1] == "end_header":
                break
        n_vertex = next((int(h.split()[-1]) for h in header
                         if h.startswith("element vertex")), 0)
        n_face = next((int(h.split()[-1]) for h in header
                       if h.startswith("element face")), 0)
        verts = np.empty((n_vertex, 3), float)
        for i in range(n_vertex):
            parts = fh.readline().split()
            verts[i] = [float(parts[0]), float(parts[1]), float(parts[2])]
        faces = np.empty((n_face, 3), np.int64)
        for i in range(n_face):
            parts = fh.readline().split()
            faces[i] = [int(parts[1]), int(parts[2]), int(parts[3])]
    return verts, faces


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
