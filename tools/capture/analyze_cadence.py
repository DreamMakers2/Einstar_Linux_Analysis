#!/usr/bin/env python3
"""Summarize cadence_measure CSV output: per-camera frames, group cadence, bytes/s, loss.

Usage: python3 analyze_cadence.py RUN.csv [RUN.csv ...]
"""
from __future__ import annotations

import collections
import csv
import statistics
import sys


def load(path: str) -> list[dict]:
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        for k in ("host_ns", "device_ts", "frame_id", "size", "camera", "sub"):
            r[k] = int(r[k])
    return rows


def summarize(path: str) -> dict:
    rows = load(path)
    if not rows:
        return {"file": path, "frames": 0}
    by_cam = collections.defaultdict(list)
    groups = collections.defaultdict(list)
    for r in rows:
        by_cam[r["camera"]].append(r)
        groups[r["device_ts"]].append(r)
    first = {}
    for r in rows:
        first.setdefault(r["device_ts"], r["host_ns"])
    order = sorted(first)
    intervals = [(first[b] - first[a]) / 1e6 for a, b in zip(order, order[1:])]
    t0 = min(r["host_ns"] for r in rows)
    t1 = max(r["host_ns"] for r in rows)
    wall = (t1 - t0) / 1e9
    sizes = collections.Counter(len(v) for v in groups.values())
    # Frame ids are slot counters: both IR cameras share a slot id, the colour
    # camera rides the last slot.  Loss shows up as a gap in the slot sequence.
    slots = sorted({r["frame_id"] for r in rows})
    return {
        "file": path,
        "frames": len(rows),
        "wall_s": round(wall, 3),
        "fps": round(len(rows) / wall, 2),
        "MB_per_s": round(len(rows) * 1310720 / wall / 1e6, 2),
        "groups": len(groups),
        "frames_per_group": dict(sizes),
        "group_period_ms_median": round(statistics.median(intervals), 1) if intervals else 0,
        "per_camera": {c: len(v) for c, v in sorted(by_cam.items())},
        "slot_ids": len(slots),
    }


def main(argv: list[str]) -> int:
    for path in argv[1:]:
        s = summarize(path)
        print(f"{path}: {s}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
