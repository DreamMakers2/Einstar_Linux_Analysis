#!/usr/bin/env python3
"""Stage-1 selection from the full-corpus feature cache.

Reads ``features.jsonl`` (one row per complete group) and chooses a bounded,
diverse set of groups for stage-2 refinement.  Selection is purely geometric
and quality based; no images are read.

Eligibility (all must hold):
  * a factory-intrinsics pose exists (left IR, fallback right IR);
  * both IR cameras have the full lattice assignment
    (``assignment_ok == true``);
  * >= ``--min-markers`` lattice markers in each IR camera and >= the same
    number of shared IR markers;
  * pose reprojection RMS <= ``--max-rms`` px;
  * plate-ROI contrast/focus and whole-image saturation within the live
    guidance thresholds (IR: contrast >= 14, focus >= 150, saturation <= 0.02).

Diversity: candidates are grouped into (distance band x tilt band) bins and
selected round-robin by descending score, so the retained set spans the working
distance and viewing-angle range instead of clustering at the median pose.

Outputs in ``--out``:
  selection.jsonl      one row per selected group (provenance + pose summary)
  stage1_groups.txt    group ids, one per line (for `einstar-stream-frames
                       --groups-file`)
  selection_summary.json
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys

DISTANCE_BANDS_MM = ((300.0, 380.0), (380.0, 460.0), (460.0, 560.0),
                     (560.0, 700.0), (700.0, 1200.0))
TILT_BANDS_DEG = ((0.0, 15.0), (15.0, 30.0), (30.0, 50.0), (50.0, 90.0))


def tilt_deg(R):
    """Angle between the board normal and the camera optical axis."""
    if not R or len(R) != 9:
        return None
    # Board normal in the board frame is +Z; its camera-frame direction is the
    # third column of the row-major board->camera rotation, so the component on
    # the camera optical axis is R[2][2] = R[8].
    nz = min(1.0, max(0.0, abs(float(R[8]))))
    return math.degrees(math.acos(nz))


def band(value, bands, prefix):
    if value is None:
        return f"{prefix}:unknown"
    for index, (low, high) in enumerate(bands):
        if low <= value < high:
            return f"{prefix}:{index}:{low:g}-{high:g}"
    return f"{prefix}:{low:g}+" if value >= bands[-1][1] else f"{prefix}:low"


def qualify(row, args):
    counts = row.get("marker_counts") or {}
    left = int(counts.get("left", 0))
    right = int(counts.get("right", 0))
    pose = row.get("pose") or {}
    if not row.get("assignment_ok") or not pose:
        return None
    if left < args.min_markers or right < args.min_markers:
        return None
    shared = int(row.get("shared_markers_ir", 0))
    if shared < args.min_markers:
        return None
    rms = pose.get("reprojection_rms_px")
    if rms is None or float(rms) > args.max_rms:
        return None
    quality = row.get("quality") or {}
    for camera, min_focus in (("left", 150.0), ("right", 150.0)):
        metrics = quality.get(camera) or {}
        contrast = metrics.get("contrast")
        focus = metrics.get("focus")
        saturation = metrics.get("saturation")
        if contrast is None or float(contrast) < 14.0:
            return None
        if focus is None or float(focus) < min_focus:
            return None
        if saturation is not None and float(saturation) > 0.02:
            return None
    t = pose.get("t") or []
    if len(t) != 3:
        return None
    distance = math.sqrt(t[0] ** 2 + t[1] ** 2 + t[2] ** 2)
    if not (DISTANCE_BANDS_MM[0][0] <= distance < DISTANCE_BANDS_MM[-1][1]):
        return None
    R = pose.get("R") or []
    angle = tilt_deg(R)
    focus_mean = 0.0
    contrast_mean = 0.0
    for camera in ("left", "right"):
        metrics = quality.get(camera) or {}
        focus_mean += float(metrics.get("focus") or 0.0) / 2.0
        contrast_mean += float(metrics.get("contrast") or 0.0) / 2.0
    saturation_max = max(float((quality.get(camera) or {}).get("saturation")
                               or 0.0) for camera in ("left", "right"))
    score = (4.0 * shared + 0.5 * (left + right) - 8.0 * float(rms)
             + 0.02 * focus_mean + 0.1 * contrast_mean
             - 100.0 * saturation_max)
    return {
        "distance_mm": distance,
        "tilt_deg": angle,
        "rms": float(rms),
        "score": score,
        "bin": (band(distance, DISTANCE_BANDS_MM, "dist"),
                band(angle, TILT_BANDS_DEG, "tilt")),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True,
                        help="features.jsonl from extract_features.py")
    parser.add_argument("--out", required=True,
                        help="selection output directory")
    parser.add_argument("--count", type=int, default=1200,
                        help="number of groups to select (default 1200)")
    parser.add_argument("--min-markers", type=int, default=32,
                        help="minimum lattice markers per IR camera (default 32)")
    parser.add_argument("--max-rms", type=float, default=1.0,
                        help="maximum pose reprojection RMS in px (default 1.0)")
    args = parser.parse_args(argv)

    features = Path(args.features)
    if not features.is_file():
        print(f"features not found: {features}", file=sys.stderr)
        return 2
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)

    candidates_by_bin: dict[tuple, list] = defaultdict(list)
    total = 0
    eligible = 0
    rejected_reasons = Counter()
    with features.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1
            info = qualify(row, args)
            if info is None:
                rejected_reasons["not_eligible"] += 1
                continue
            eligible += 1
            candidates_by_bin[info["bin"]].append({
                "group": int(row["group"]),
                "device_group": int(row.get("device_group", -1)),
                "host_time_ns": int(row.get("host_time_ns", 0)),
                "marker_counts": row.get("marker_counts"),
                "shared_markers_ir": int(row.get("shared_markers_ir", 0)),
                "pose": row.get("pose"),
                **info,
            })
    for items in candidates_by_bin.values():
        items.sort(key=lambda item: item["score"], reverse=True)

    # Round-robin over (distance, tilt) bins, best score first.
    selected = []
    selected_groups = set()
    active_bins = [key for key, items in candidates_by_bin.items() if items]
    active_bins.sort()
    while len(selected) < args.count and active_bins:
        next_bins = []
        for key in active_bins:
            items = candidates_by_bin[key]
            while items and items[0]["group"] in selected_groups:
                items.pop(0)
            if not items:
                continue
            item = items.pop(0)
            selected.append(item)
            selected_groups.add(item["group"])
            if len(selected) >= args.count:
                break
            next_bins.append(key)
        active_bins = next_bins
    selected.sort(key=lambda item: item["group"])

    groups_path = output / "stage1_groups.txt"
    groups_path.write_text("".join(f"{item['group']}\n" for item in selected))
    selection_path = output / "selection.jsonl"
    with selection_path.open("w", encoding="utf-8") as handle:
        for item in selected:
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")

    by_distance = Counter(item["bin"][0].split(":")[1]
                          for item in selected)
    by_tilt = Counter(item["bin"][1].split(":")[1] for item in selected)
    summary = {
        "schema": "einstar-offline-stage1-selection/1",
        "features": str(features),
        "groups_in_corpus": total,
        "eligible_groups": eligible,
        "selected_groups": len(selected),
        "requested": args.count,
        "criteria": {
            "min_markers": args.min_markers,
            "max_rms_px": args.max_rms,
            "both_ir_full_assignment": True,
            "ir_contrast_min": 14.0,
            "ir_focus_min": 150.0,
            "ir_saturation_max": 0.02,
            "distance_bands_mm": DISTANCE_BANDS_MM,
            "tilt_bands_deg": TILT_BANDS_DEG,
        },
        "distance_band_histogram": dict(sorted(by_distance.items())),
        "tilt_band_histogram": dict(sorted(by_tilt.items())),
        "selection_path": str(selection_path),
        "groups_path": str(groups_path),
    }
    (output / "selection_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n")
    print(f"[select] corpus={total} eligible={eligible} "
          f"selected={len(selected)} -> {groups_path}")
    print(f"[select] distance bands {summary['distance_band_histogram']}")
    print(f"[select] tilt bands {summary['tilt_band_histogram']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
