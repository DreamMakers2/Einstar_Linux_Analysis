#!/usr/bin/env python3
"""Answer the stage-1 coverage question from a feature cache.

Reports, over *every* complete group in the corpus (including groups the live
wizard rejected), how many have markers in both IR cameras and how many place
the plate centre in the top-right or bottom-right 3x3 image cell.

The plate centre is taken from the detected-marker centroid per camera
(``image_region.<cam>.centroid_px``), and, when a pose exists, from the
projected board origin (``pose.plate_centre_px``).  Both are reported.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np

CELLS = ("top-left", "top-centre", "top-right",
         "middle-left", "middle-centre", "middle-right",
         "bottom-left", "bottom-centre", "bottom-right")
CORNERS = ("top-right", "bottom-right")
FRAME_WIDTH = 1280
FRAME_HEIGHT = 1024


def load_rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def cell_of(row: dict, camera: str, source: str):
    region = (row.get("image_region") or {}).get(camera) or {}
    if source == "centroid":
        cell = region.get("cell")
        if cell:
            return int(cell[0]), int(cell[1]), region.get("cell_name")
        return None
    pose = row.get("pose") or {}
    centre = pose.get("plate_centre_px")
    if not centre:
        return None
    col = min(2, max(0, int(centre[0] * 3.0 / 1280.0)))
    row_index = min(2, max(0, int(centre[1] * 3.0 / 1024.0)))
    return col, row_index, CELLS[row_index * 3 + col]


def _profile_cameras(profile_path: str):
    """Factory intrinsics/extrinsics for both IR cameras (world = ir_left)."""
    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from calib.recon_profile import load
    from calib import geometry
    profile = load(profile_path)
    return {
        "geometry": geometry,
        "left": profile.camera("ir_left"),
        "right": profile.camera("ir_right"),
    }


def pose_centres(row: dict, cameras) -> dict[str, list[float]] | None:
    """Project the board origin into BOTH IR cameras from one PnP pose.

    The pose is board->left (preferred) or board->right; the factory profile
    supplies the rigid left/right relation (world = ir_left: R=I, T=0).
    Returns {"left": [x, y], "right": [x, y]} in pixels, or None.
    """
    pose = row.get("pose") or {}
    R = pose.get("R")
    t = pose.get("t")
    if not R or not t or len(t) != 3:
        return None
    geometry = cameras["geometry"]
    left = cameras["left"]
    right = cameras["right"]
    R_board = np.asarray(R, float).reshape(3, 3)
    t_board = np.asarray(t, float).reshape(3)
    R_right = np.asarray(right.R, float)
    T_right = np.asarray(right.T, float)
    if pose.get("camera") == "right":
        # board origin in right camera -> left/world coordinates
        origin_left = R_right.T @ (t_board - T_right)
        origin_right = t_board
    else:
        origin_left = t_board
        origin_right = R_right @ t_board + T_right
    centres = {}
    for name, camera in (("left", left), ("right", right)):
        origin = origin_left if name == "left" else origin_right
        projected = geometry.project(np.asarray(camera.K, float),
                                     np.asarray(camera.dist, float),
                                     origin.reshape(1, 3))
        centres[name] = [float(projected[0][0]), float(projected[0][1])]
    return centres


def _cell_from_pixel(centre):
    col = min(2, max(0, int(centre[0] * 3.0 / FRAME_WIDTH)))
    row = min(2, max(0, int(centre[1] * 3.0 / FRAME_HEIGHT)))
    return col, row, CELLS[row * 3 + col]


def summarise(features: Path, profile_path: str | None = None) -> dict:
    rows = 0
    both_ir_any = 0
    both_ir_12 = 0
    both_ir_full = 0
    camera_markers = {"left": 0, "right": 0, "tex": 0}
    camera_cells: dict[str, Counter] = {
        "left": Counter(), "right": Counter(), "tex": Counter()}
    pose_cells: dict[str, Counter] = {"left": Counter(), "right": Counter()}
    poses = 0
    corner_any = []
    corner_both = []
    centroid_corner_any = []
    cameras = _profile_cameras(profile_path) if profile_path else None
    pose_centre_px: dict[int, dict[str, list[float]]] = {}
    for row in load_rows(features):
        rows += 1
        counts = row.get("marker_counts") or {}
        left = int(counts.get("left", 0))
        right = int(counts.get("right", 0))
        tex = int(counts.get("tex", 0))
        for name, value in (("left", left), ("right", right), ("tex", tex)):
            if value > 0:
                camera_markers[name] += 1
        if left > 0 and right > 0:
            both_ir_any += 1
        if left >= 12 and right >= 12:
            both_ir_12 += 1
        if row.get("assignment_ok"):
            both_ir_full += 1
        pose = row.get("pose")
        if pose:
            poses += 1
        for camera in ("left", "right"):
            cell = cell_of(row, camera, "centroid")
            if cell is not None:
                camera_cells[camera][cell[2]] += 1
        centres = None
        if cameras is not None and pose:
            centres = pose_centres(row, cameras)
            if centres is not None:
                pose_centre_px[int(row["group"])] = centres
                for camera in ("left", "right"):
                    cell = _cell_from_pixel(centres[camera])
                    pose_cells[camera][cell[2]] += 1
        if left > 0 and right > 0:
            left_centroid = cell_of(row, "left", "centroid")
            right_centroid = cell_of(row, "right", "centroid")
            if (left_centroid and left_centroid[2] in CORNERS) or \
                    (right_centroid and right_centroid[2] in CORNERS):
                centroid_corner_any.append(row)
            if centres is not None:
                left_cell = _cell_from_pixel(centres["left"])
                right_cell = _cell_from_pixel(centres["right"])
                if left_cell[2] in CORNERS or right_cell[2] in CORNERS:
                    corner_any.append(row)
                if left_cell[2] in CORNERS and right_cell[2] in CORNERS:
                    corner_both.append(row)

    def entry(row, camera_names):
        pose = row.get("pose") or {}
        t = pose.get("t") or []
        centres = pose_centre_px.get(int(row["group"]))
        pose_cells_for_row = {}
        if centres is not None:
            pose_cells_for_row = {
                camera: _cell_from_pixel(centres[camera])[2]
                for camera in ("left", "right")}
        return {
            "group": int(row["group"]),
            "device_group": int(row.get("device_group", -1)),
            "host_time_ns": int(row.get("host_time_ns", 0)),
            "marker_counts": row.get("marker_counts"),
            "cells": pose_cells_for_row,
            "pose_centre_px": centres,
            "centroid_cells": {
                camera: (cell_of(row, camera, "centroid") or
                         [None, None, None])[2] for camera in camera_names},
            "pose_distance_mm": (float((t[0] ** 2 + t[1] ** 2 + t[2] ** 2)
                                      ** 0.5) if len(t) == 3 else None),
            "pose_rms_px": pose.get("reprojection_rms_px"),
        }

    def plausible(entry_row):
        distance = entry_row.get("pose_distance_mm")
        rms = entry_row.get("pose_rms_px")
        return (distance is not None and 200.0 <= float(distance) <= 800.0 and
                rms is not None and float(rms) <= 2.0)

    criteria = {"pose_distance_mm": [200.0, 800.0],
                "pose_reprojection_rms_px_max": 2.0}
    any_entries = [entry(row, ("left", "right")) for row in corner_any]
    both_entries = [entry(row, ("left", "right")) for row in corner_both]
    centroid_entries = [entry(row, ("left", "right"))
                        for row in centroid_corner_any]
    return {
        "schema": "einstar-offline-corner-report/1",
        "features": str(features),
        "groups": rows,
        "groups_with_markers": camera_markers,
        "both_ir_markers_any": both_ir_any,
        "both_ir_markers_min12": both_ir_12,
        "assignment_full_both_ir": both_ir_full,
        "groups_with_pose": poses,
        "image_cells_centroid": {
            camera: {name: int(count)
                     for name, count in sorted(camera_cells[camera].items())}
            for camera in camera_cells},
        "image_cells_pose_centre": {
            camera: {name: int(count)
                     for name, count in sorted(pose_cells[camera].items())}
            for camera in pose_cells},
        "corner_candidates_pose_any_ir": {
            "count": len(any_entries),
            "groups": any_entries,
        },
        "corner_candidates_pose_any_ir_plausible": {
            "count": sum(1 for item in any_entries if plausible(item)),
            "criteria": criteria,
            "groups": [item for item in any_entries if plausible(item)],
        },
        "corner_candidates_pose_both_ir": {
            "count": len(both_entries),
            "groups": both_entries,
        },
        "corner_candidates_pose_both_ir_plausible": {
            "count": sum(1 for item in both_entries if plausible(item)),
            "criteria": criteria,
            "groups": [item for item in both_entries if plausible(item)],
        },
        "corner_candidates_centroid_any_ir": {
            "count": len(centroid_entries),
            "groups": centroid_entries,
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True,
                        help="features.jsonl from extract_features.py")
    parser.add_argument("--json", default="",
                        help="write the full report JSON here")
    parser.add_argument("--top", type=int, default=10,
                        help="printed candidate rows (default 10)")
    parser.add_argument("--profile",
                        default="<private-inputs>/einstar-profiles/"
                                "factory-e10.json",
                        help="factory profile for per-camera board-origin "
                             "projection (--profile '' to skip)")
    args = parser.parse_args(argv)
    path = Path(args.features)
    if not path.is_file():
        print(f"features not found: {path}", file=sys.stderr)
        return 2
    report = summarise(path, args.profile or None)
    print(f"groups                         : {report['groups']}")
    print("groups with markers (left/right/tex): "
          f"{report['groups_with_markers']}")
    print(f"both IR markers (>0)           : {report['both_ir_markers_any']}")
    print(f"both IR markers (>=12)         : {report['both_ir_markers_min12']}")
    print(f"full assignment both IR        : "
          f"{report['assignment_full_both_ir']}")
    print(f"groups with pose               : {report['groups_with_pose']}")
    print("image cells (marker centroid):")
    for camera in ("left", "right", "tex"):
        print(f"  {camera:5s}: " +
              ", ".join(f"{name}={count}" for name, count in
                        report["image_cells_centroid"][camera].items()))
    print("image cells (projected plate centre):")
    for camera in ("left", "right"):
        print(f"  {camera:5s}: " +
              ", ".join(f"{name}={count}" for name, count in
                        report["image_cells_pose_centre"][camera].items()))
    for key, label in (("corner_candidates_pose_any_ir",
                        "corner (pose, any IR)"),
                       ("corner_candidates_pose_any_ir_plausible",
                        "corner (pose, any IR, plausible)"),
                       ("corner_candidates_pose_both_ir",
                        "corner (pose, both IR)"),
                       ("corner_candidates_pose_both_ir_plausible",
                        "corner (pose, both IR, plausible)"),
                       ("corner_candidates_centroid_any_ir",
                        "corner (centroid, any IR)")):
        section = report[key]
        print(f"{label:30s}: {section['count']}")
        for row in section["groups"][:args.top]:
            print(f"    group {row['group']:6d} dev {row['device_group']:6d} "
                  f"markers {row['marker_counts']} "
                  f"cells {row['cells']} dist "
                  f"{row['pose_distance_mm'] if row['pose_distance_mm'] is None else round(row['pose_distance_mm'], 1)} mm")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
        print(f"report -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
