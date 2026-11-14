#!/usr/bin/env python
"""Positive control: how sensitive is the held-out metric to a rig error?

The report compares arms using the median held-out ``rms_cross`` (right + colour
reprojection given a left-anchored pose).  If that statistic could not see a
0.05 deg stereo rotation error, "no difference between arms" would be
uninformative.  This script fits one model (60 full-board views), then perturbs
the fitted right extrinsic by a known rotation / baseline shift and by the
colour translation, and records the held-out statistic each time.  It writes
``sensitivity.json`` next to this file; :mod:`report` embeds the table.

Usage::

    OPENBLAS_NUM_THREADS=1 .venv/bin/python analysis/calibration/accuracy/sensitivity.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import experiment as X                                   # noqa: E402
from calib import geometry                               # noqa: E402

ROTATIONS_DEG = (0.01, 0.02, 0.05, 0.1, 0.2)
BASELINE_MM = (0.02, 0.05, 0.1, 0.2, 0.5)
TEX_MM = (0.1, 0.5, 1.0, 2.0)


def median_holdout(res, pool, test):
    vals = [h["rms_cross"] for h in X.evaluate_holdout(res, pool, test) if h.get("valid")]
    return float(np.median(vals)), float(np.percentile(vals, 95))


def main() -> int:
    pool, _ = X.load_dataset(HERE / "data_fresh01.json", "fresh01", 12, 3.0)
    cams, _ = X.EX.profile_to_cameras(X.EX.load_profile(X.FACTORY))
    full = sorted(v for v, r in pool.items() if r["full"])
    train, test = full[:60], full[60:80]
    t0 = time.time()
    res, met, _ = X.fit(pool, train, cams)
    base_med, base_p95 = median_holdout(res, pool, test)
    out = {
        "schema": "einstar.partial-vs-full-sensitivity/1",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "train_views": len(train), "test_views": len(test),
        "fit_train_rms_px": met["train_rms_px"],
        "baseline": {"median": base_med, "p95": base_p95},
        "right_rotation_deg": [], "right_baseline_mm": [], "tex_translation_mm": [],
        "seconds": None,
    }
    R, t = res.extrinsics["right"]
    for deg in ROTATIONS_DEG:
        th = np.radians(deg)
        res.extrinsics["right"] = (geometry.rodrigues(np.array([th, 0.0, 0.0])) @ R, t)
        m, p = median_holdout(res, pool, test)
        out["right_rotation_deg"].append({"deg": deg, "median": m, "p95": p,
                                          "delta_median": m - base_med})
    res.extrinsics["right"] = (R, t)
    for mm in BASELINE_MM:
        res.extrinsics["right"] = (R, t + np.array([mm, 0.0, 0.0]))
        m, p = median_holdout(res, pool, test)
        out["right_baseline_mm"].append({"mm": mm, "median": m, "p95": p,
                                         "delta_median": m - base_med})
    res.extrinsics["right"] = (R, t)
    Rt, tt = res.extrinsics["tex"]
    for mm in TEX_MM:
        res.extrinsics["tex"] = (Rt, tt + np.array([mm, 0.0, 0.0]))
        m, p = median_holdout(res, pool, test)
        out["tex_translation_mm"].append({"mm": mm, "median": m, "p95": p,
                                          "delta_median": m - base_med})
    res.extrinsics["tex"] = (Rt, tt)
    out["seconds"] = time.time() - t0
    (HERE / "sensitivity.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("baseline", "right_rotation_deg",
                                         "right_baseline_mm", "tex_translation_mm")},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
