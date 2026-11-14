#!/usr/bin/env python3
"""Measure the actual 3-D plate display latency, not its refresh rate.

Two distinct costs matter to the operator:

* ``snapshot()`` on the GUI thread -- how long the window spends building the
  state it repaints from;
* rendering the plate widget from the newest pose -- projection, painting and
  Qt image creation.

The measurement runs offscreen, takes the *newest published pose* and reports
p50/p95/max in milliseconds plus the pose age (``pose_age_s``).  It does not
touch a scanner and reads a recorded session read-only.

    .venv/bin/python tools/wizard/bench_display.py \
        --views-dir ~/einstar-calibration-fresh-01 \
        --factory ~/einstar-private/einstar-profiles/factory-e10.json \
        --views 20 --out analysis/calibration/performance/display_latency.json
"""
from __future__ import annotations

import os
import sys

# Running this file puts tools/wizard on sys.path[0], where ``types.py``
# shadows the standard-library ``types`` module (argparse imports it).  Remove
# that entry before importing anything else.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv else ""
sys.path[:] = [entry for entry in sys.path
               if entry not in ("", ".", _SCRIPT_DIR, "tools/wizard")
               and os.path.abspath(entry or ".") != _SCRIPT_DIR]
from pathlib import Path

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import json
import statistics
import tempfile
import time


def _summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(values),
            "p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[min(len(ordered) - 1,
                                        int(0.95 * len(ordered)))], 3),
            "max_ms": round(ordered[-1], 3),
            "mean_ms": round(statistics.fmean(ordered), 3)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--views-dir", required=True)
    ap.add_argument("--factory", required=True)
    ap.add_argument("--views", type=int, default=20)
    ap.add_argument("--holds", type=int, default=2)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from tools.wizard import controller
    from tools.wizard.gui.plate3d import PlatePreview
    from tools.wizard.real_view_source import RealViewSource

    source = RealViewSource(args.views_dir, select=args.views,
                            holds=args.holds, hold_repeats=4)
    plate = PlatePreview()
    plate.resize(360, 280)
    snapshot_ms: list[float] = []
    render_ms: list[float] = []
    pose_ms: list[float] = []
    ages: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        ctl = controller.WizardController(
            factory_profile=args.factory, session_dir=tmp, mode="high",
            source=source.info, ring_keep=2, async_solve=True)
        ctl.begin()
        for group in source:
            ctl.ingest(group)
            app.processEvents()
            started = time.perf_counter()
            snap = ctl.snapshot()
            snapshot_ms.append((time.perf_counter() - started) * 1000.0)
            age = snap.get("pose_age_s")
            if age is not None:
                ages.append(float(age) * 1000.0)
            pose = snap.get("pose_rt")
            if pose:
                started = time.perf_counter()
                plate.set_pose(pose.get("R"), pose.get("t"))
                plate.render_image(360, 280)
                render_ms.append((time.perf_counter() - started) * 1000.0)
                started = time.perf_counter()
                plate.set_intrinsics(1150.0, 1150.0, 640.0, 512.0)
                pose_ms.append((time.perf_counter() - started) * 1000.0)
        ctl.cancel()

    result = {
        "schema": "einstar.display-latency/1",
        "views": int(args.views),
        "accepted": len(source.selected),
        "snapshot": _summary(snapshot_ms),
        "plate_render": _summary(render_ms),
        "pose_age": _summary(ages),
        "note": ("offscreen Qt; snapshot() is the GUI-thread state rebuild and "
                 "plate_render() is set_pose + render_image(360x280).  Pose age "
                 "is time since the last decided pose, not display latency."),
    }
    text = json.dumps(result, indent=1)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
