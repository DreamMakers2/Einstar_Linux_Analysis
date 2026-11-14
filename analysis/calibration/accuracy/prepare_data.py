#!/usr/bin/env python
"""Re-detect the fresh-01 accepted views to recover *keyed* (u,v) observations.

Why this exists
---------------
``tools.wizard.session_io.ViewStore`` stores ``observation["image_points"]`` as
a *list* per camera.  That list is row-major ``(v, u)`` -- i.e. canonical label
``(i % 8, i // 8)`` -- only for a view in which the whole 8x5 marker field was
detected (40 markers); acceptance for those views required a verified
orientation witness, so the ordering is exact ground truth.  For a partial
view the list preserves the *detector's* assignment order instead, and the JSON
alone cannot say which lattice site each row belongs to.

The stored PNGs are lossless, so the labelling can be rebuilt: this script runs
``calib.detect.detect_grid_auto`` on every stored camera image and keeps the
``GridObservation``, whose ``points`` mapping is keyed by canonical ``(u, v)``.
A detection is only used when its identity witness verified
(``obs.identity_ok``), i.e. the three on-lattice large markers and the
off-lattice chiral marker all landed where the labelling predicts.

The 84 views whose stored lists are label-ordered double as the validator of
this re-detection: for those we can compare the recovered ``(u,v) -> pixel``
map against the stored ordering and prove the re-detection reproduces the
production labelling (see ``validation`` in the output).

Output (default ``data_fresh01.json`` next to this file):
    sessions[<name>] = {
      "root", "views": {view_id: {"cams": {cam: {"uv": [[u,v,x,y], ...],
                                                  "rms", "witness", "n"}},
                                  "stored_n": {...}, "pose": ..., "meta": ...}},
      "detections": {...summary...}, "validation": {...} }

Read-only on the session directory; nothing is written there.

Usage::

    OPENBLAS_NUM_THREADS=1 .venv/bin/python analysis/calibration/accuracy/prepare_data.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from calib import detect, plate                       # noqa: E402
from tools.wizard import session_io                   # noqa: E402

SESSIONS = {
    "fresh01": "<calibration-session>",
}
CAMS = ("left", "right", "tex")


def detect_view(store, record) -> dict:
    out = {}
    for cam, meta in (record.get("files") or {}).items():
        if cam not in CAMS:
            continue
        path = store.paths.root / str(meta.get("path"))
        gray = session_io.decode_frame_png(path.read_bytes())
        t0 = time.time()
        obs = detect.detect_grid_auto(gray)
        dt = time.time() - t0
        if obs is None:
            out[cam] = {"n": 0, "uv": [], "rms": None, "identity_ok": False,
                        "witness_score": -1, "detector": "none", "seconds": dt}
            continue
        pts = [[float(u), float(v), float(p[0]), float(p[1])]
               for (u, v), p in sorted(obs.points.items())]
        out[cam] = {"n": int(obs.n_lattice), "uv": pts,
                    "rms": float(obs.rms), "identity_ok": bool(obs.identity_ok),
                    "witness_score": int(obs.witness_score),
                    "detector": str(obs.detector), "seconds": dt}
    return out


def validate_against_stored(stored_lists: dict, det: dict) -> dict:
    """Compare keyed re-detection with the stored row-major labelling.

    Only for camera lists with exactly 40 stored rows (stored order == label
    order).  For each keyed point we ask which stored row is nearest; if the
    re-detection is correct that row index must equal ``v * 8 + u``.
    """
    rows = {"checked": 0, "label_matches": 0, "label_mismatches": 0,
            "dist_px": [], "mismatch_samples": [], "per_view": {}}
    for cam, obs in det.items():
        stored = stored_lists.get(cam) or []
        if len(stored) != 40:
            continue
        S = np.asarray(stored, float)
        for u, v, x, y in obs["uv"]:
            d = np.hypot(S[:, 0] - x, S[:, 1] - y)
            j = int(np.argmin(d))
            rows["checked"] += 1
            rows["dist_px"].append(float(d[j]))
            if j == int(v) * plate.GRID_COLS + int(u):
                rows["label_matches"] += 1
            else:
                rows["label_mismatches"] += 1
                if len(rows["mismatch_samples"]) < 10:
                    rows["mismatch_samples"].append(
                        {"camera": cam, "uv": [u, v], "nearest_row": j,
                         "dist_px": float(d[j])})
    d = np.asarray(rows["dist_px"], float)
    if d.size:
        rows.update({"dist_px_median": float(np.median(d)),
                     "dist_px_p95": float(np.percentile(d, 95)),
                     "dist_px_max": float(d.max())})
    rows["match_rate"] = (rows["label_matches"] / rows["checked"]
                          if rows["checked"] else None)
    return rows

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "data_fresh01.json"))
    ap.add_argument("--sessions", default="fresh01")
    ap.add_argument("--limit", type=int, default=0, help="debug: first N views")
    args = ap.parse_args(argv)

    data = {"schema": "einstar.accuracy-data/1",
            "detector": "calib.detect.detect_grid_auto (pattern-v1, centroid)",
            "pitch_mm": float(plate.DEFAULT_PITCH_MM),
            "sessions": {},
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for name in args.sessions.split(","):
        name = name.strip()
        root = SESSIONS[name]
        store = session_io.ViewStore(root)
        store.reload()
        records = list(store.records())
        if args.limit:
            records = records[:args.limit]
        views, val = {}, {"checked": 0, "label_matches": 0,
                          "label_mismatches": 0, "dist_px": [],
                          "mismatch_samples": []}
        t0 = time.time()
        for k, rec in enumerate(records):
            idx = int(rec["index"])
            obs = store.observation(idx)
            det = detect_view(store, rec)
            meta = {key: obs.get(key) for key in
                    ("distance_mm", "tilt_deg", "roll_deg", "yaw_deg",
                     "accepted", "assignment_ok", "quality", "solved",
                     "bootstrap_seed")}
            views[str(idx)] = {
                "cams": det,
                "stored_n": {c: len((obs.get("image_points") or {}).get(c) or []) for c in CAMS},
                "stored_accepted": bool(obs.get("accepted")),
                "stored_assignment_ok": bool(obs.get("assignment_ok")),
                "holdout_rms_px": obs.get("holdout_rms_px"),
                "pose": obs.get("pose"),
                "meta": meta,
            }
            v = validate_against_stored(obs.get("image_points") or {}, det)
            val["checked"] += v["checked"]
            val["label_matches"] += v["label_matches"]
            val["label_mismatches"] += v["label_mismatches"]
            if v["checked"]:
                val["dist_px"].extend(v["dist_px"])
            for s in v["mismatch_samples"]:
                if len(val["mismatch_samples"]) < 10:
                    val["mismatch_samples"].append({"view": idx, **s})
            if k % 20 == 0:
                print(f"[{name}] {k}/{len(records)} views, {time.time() - t0:.0f}s",
                      flush=True)
        d = np.asarray(val.pop("dist_px"), float)
        if d.size:
            val.update({"dist_px_median": float(np.median(d)),
                        "dist_px_p95": float(np.percentile(d, 95)),
                        "dist_px_max": float(d.max()),
                        "dist_px_n": int(d.size)})
        val["match_rate"] = (val["label_matches"] / val["checked"]
                             if val["checked"] else None)
        data["sessions"][name] = {
            "root": root,
            "n_views": len(views),
            "views": views,
            "validation": val,
            "seconds": time.time() - t0,
        }
        print(f"[{name}] done in {time.time() - t0:.0f}s; "
              f"label match {val['label_matches']}/{val['checked']}", flush=True)
    Path(args.out).write_text(json.dumps(data))
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
