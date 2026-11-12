"""Measure the per-group decision cost on a recorded frame sequence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default=str(Path.home() /
                     "einstar-private" / "sweep-b" / "frames"))
    ap.add_argument("--groups", type=int, default=30)
    ap.add_argument("--processes", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.processes:
        os.environ["EINSTAR_DETECT_PROCESSES"] = "1"
    from wizard.gui import controller_adapter
    src = controller_adapter.open_source("replay", frames=args.frames)
    ctl = controller_adapter.make_controller(
        factory_profile=str(Path.home() / "einstar-private" /
                            "einstar-profiles" / "factory-e10.json"),
        session_dir="/tmp/einstar-bench-detection", mode="standard",
        source_kind="replay")
    ctl.begin()
    groups = []
    for g in src:
        groups.append(g)
        if len(groups) >= args.groups + 3:
            break
    for g in groups[:3]:
        ctl.ingest(g)
    per = []
    t_all = time.perf_counter()
    for g in groups[3:]:
        t0 = time.perf_counter()
        d = ctl.ingest(g)
        per.append({"ms": 1000.0 * (time.perf_counter() - t0),
                    "accepted": bool(getattr(d, "accepted", False)),
                    "reasons": list(getattr(d, "reasons", []))})
    wall = time.perf_counter() - t_all
    per.sort(key=lambda x: x["ms"])
    out = {
        "schema": "einstar-bench-detection/1",
        "groups": len(per),
        "wall_s": wall,
        "per_group_ms": {
            "mean": 1000.0 * wall / max(1, len(per)),
            "p50": per[len(per) // 2]["ms"],
            "p95": per[min(len(per) - 1, int(0.95 * len(per)))]["ms"],
            "max": per[-1]["ms"],
        },
        "accepted": sum(1 for x in per if x["accepted"]),
        "roi_stats": getattr(ctl, "_roi_stats", None),
        "processes": bool(ctl._detect_processes),
        "detect_seconds": float(getattr(ctl.session, "detect_seconds", 0.0)),
        "refine_seconds": float(getattr(ctl.session, "refine_seconds", 0.0)),
    }
    ctl.close()
    text = json.dumps(out, indent=1, sort_keys=True)
    print(text)
    if args.out:
        p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
