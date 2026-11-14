#!/usr/bin/env python
"""Detect the 41-marker plate in the NEW EXStar 2026-11-14 calibration corpus.

Reads the private byte-verified copy of the vendor images
(``~/einstar-private/exstar-20261114``) with the repo detector
(``calib.detect.detect_grid_auto``) and writes a detection inventory JSON.

Usage
-----
    .venv/bin/python analysis/exstar_reference/detect_vendor.py \
        [--images ~/einstar-private/exstar-20261114] \
        [--out analysis/exstar_reference/vendor_20261114_detections.json]

Nothing outside ``--out`` is written; the NTFS mount is never touched.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from calib import detect  # noqa: E402

CAMERAS = (("Left", "left"), ("Right", "right"), ("Tex", "tex"))


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def index_of(path: str) -> int:
    return int(re.search(r"(\d+)\.bmp$", os.path.basename(path)).group(1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default="~/einstar-private/exstar-20261114")
    ap.add_argument("--out", default="analysis/exstar_reference/"
                                    "vendor_20261114_detections.json")
    ap.add_argument("--max-index", type=int, default=25)
    args = ap.parse_args()

    root = os.path.expanduser(args.images)
    out = {"schema": "einstar.exstar-vendor-detections/1",
           "images_dir": root,
           "detector": "calib.detect.detect_grid_auto (pattern-v1, centroid)",
           "pitch_mm": 28.0,
           "cameras": {}, "images": []}
    t_all = time.time()
    for side, cam in CAMERAS:
        files = sorted(glob.glob(os.path.join(root, f"image{side}*.bmp")),
                       key=index_of)
        files = [f for f in files if index_of(f) <= args.max_index]
        rows = {}
        for f in files:
            i = index_of(f)
            gray = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
            if gray is None:
                out["images"].append({"side": side, "camera": cam, "index": i,
                                      "error": "unreadable", "path": f})
                continue
            t0 = time.time()
            blobs = detect.detect_blobs(gray, invert=False, percentile=99.0)
            obs = detect.detect_grid_auto(gray)
            dt = time.time() - t0
            rec = {"side": side, "camera": cam, "index": i,
                   "file": os.path.basename(f), "sha256": sha256(f),
                   "shape": list(gray.shape), "n_blobs": len(blobs),
                   "detect_s": round(dt, 3)}
            if obs is None:
                rec.update({"n_lattice": 0, "rms": None, "max_residual": None,
                            "points": {}})
            else:
                rec.update({"n_lattice": int(obs.n_lattice),
                            "rms": float(obs.rms),
                            "max_residual": float(obs.max_residual),
                            "points": {f"{int(u)},{int(v)}": [float(x), float(y)]
                                       for (u, v), (x, y) in obs.points.items()}})
            out["images"].append(rec)
            rows[i] = rec["n_lattice"]
        out["cameras"][cam] = {
            "n_images": len(files),
            "n_lattice_per_view": rows,
            "n_lattice_mean": float(np.mean(list(rows.values()))) if rows else 0.0,
            "n_lattice_min": int(min(rows.values())) if rows else 0,
            "n_lattice_max": int(max(rows.values())) if rows else 0,
            "n_fully_detected_40": int(sum(v == 40 for v in rows.values())),
            "n_failed": int(sum(v < 30 for v in rows.values())),
        }
        print(f"{cam:6s} {len(files):3d} images  lattice mean "
              f"{out['cameras'][cam]['n_lattice_mean']:.2f}  "
              f"min {out['cameras'][cam]['n_lattice_min']}  "
              f"40/40 {out['cameras'][cam]['n_fully_detected_40']}")
    out["elapsed_s"] = round(time.time() - t_all, 2)
    os.makedirs(os.path.dirname(os.path.abspath(out["images_dir"])) or ".",
                exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print("wrote", args.out, f"({out['elapsed_s']} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
