"""Classify the real session's accepted frames with the legacy and new rules.

Read-only: parses ``views.jsonl`` (E1V1 journal) and the stored PNGs under the
session directory, re-runs the marker detector, and compares how the legacy
controller (full-frame saturation/p99, from git HEAD) and the current
controller (padded plate bbox, marker disks masked) classify the same frame.

This is a metering comparison on *accepted* frames.  It cannot reproduce the
rejected/fallback frames that caused the live hunting; that is what
``replay_compare.py``'s loss model is for.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import struct
import sys

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from calib.detect import detect_grid_auto          # noqa: E402
from tools.wizard import exposure as new            # noqa: E402


def _load_legacy():
    spec = importlib.util.spec_from_file_location(
        "_legacy_exposure", HERE / "_legacy_exposure.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_journal(path: Path) -> list[dict]:
    data = path.read_bytes()
    out: list[dict] = []
    off = 0
    while off + 12 <= len(data):
        if data[off:off + 4] != b"E1V1":
            raise ValueError(f"bad record magic at offset {off}")
        length, _crc = struct.unpack("<II", data[off + 4:off + 12])
        out.append(json.loads(data[off + 12:off + 12 + length]))
        off += 12 + length
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", nargs="?",
                    default="<calibration-session>")
    ap.add_argument("--stride", type=int, default=6,
                    help="sample every Nth accepted view")
    ap.add_argument("--out", default=str(HERE / "real_frame_classification.json"))
    args = ap.parse_args(argv)
    legacy = _load_legacy()
    base = Path(args.session)
    records = read_journal(base / "views.jsonl")
    rows = []
    counts_old: dict[str, int] = {}
    counts_new: dict[str, int] = {}
    for index in range(0, len(records), max(1, args.stride)):
        record = records[index]
        for camera, policy_new, policy_old, label in (
                ("left", new.IR_POLICY, legacy.IR_POLICY, "ir"),
                ("tex", new.RGB_POLICY, legacy.RGB_POLICY, "rgb")):
            path = base / record["files"][camera]["path"]
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            observation = detect_grid_auto(np.ascontiguousarray(image))
            if observation is None or len(observation.points) < 4:
                continue
            points = np.asarray(list(observation.points.values()), float)
            box = new.plate_bbox(points)
            if box is None:
                continue
            full = new.FrameStats.from_image(image)
            roi = new.FrameStats.from_image(image, bbox=box, exclude=points)
            old_stats = legacy.FrameStats(mean=roi.mean, p99=full.p99,
                                          saturation=full.saturation,
                                          contrast=roi.contrast)
            old_key = legacy.AutoExposure._classify(old_stats, policy_old)
            new_key = new.AutoExposure._classify(roi, policy_new)[0]
            counts_old[f"{label}:{old_key}"] = (
                counts_old.get(f"{label}:{old_key}", 0) + 1)
            counts_new[f"{label}:{new_key}"] = (
                counts_new.get(f"{label}:{new_key}", 0) + 1)
            rows.append({"view": record["index"], "cam": label,
                         "mean": round(roi.mean, 1),
                         "full_sat": round(full.saturation, 4),
                         "full_p99": full.p99,
                         "roi_sat_excl": round(roi.saturation, 4),
                         "roi_p95": round(roi.p95, 1),
                         "old": old_key, "new": new_key})
    disagreements = sum(1 for row in rows if row["old"] != row["new"])
    payload = {"session": str(base), "stride": args.stride,
               "sampled_views": len({row["view"] for row in rows}),
               "pairs": len(rows), "old": counts_old, "new": counts_new,
               "disagreements": disagreements, "rows": rows}
    Path(args.out).write_text(json.dumps(payload, indent=1) + "\n",
                              encoding="utf-8")
    print(f"pairs={len(rows)} disagreements={disagreements} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
