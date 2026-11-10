#!/usr/bin/env python3
"""Export the decoded calibration to the reconstruction profile schema.

    python3 tools/calib/export_profile.py --source ccf \
        --out <private-inputs>/einstar-profiles/factory-e10.json

    python3 tools/calib/export_profile.py --result calibration_result.json \
        --kind replacement --out replacement.json

The profile is the C++ backend's only calibration input.  Camera order is
ir_left, ir_right, color; poses are world->camera in millimetres (OpenCV
convention) and the exported world frame is the left IR camera.  The nominal
plate pitch is recorded but never used to rescale the calibration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from calib_model import Calibration  # noqa: E402

SCHEMA = "einstar.recon.profile/1"
NAMES = {"left": "ir_left", "right": "ir_right", "tex": "color"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def camera_entry(cam) -> dict:
    k = cam.K
    return {
        "fx": float(k[0, 0]), "fy": float(k[1, 1]),
        "cx": float(k[0, 2]), "cy": float(k[1, 2]),
        "skew": float(k[0, 1]),
        "dist": [float(x) for x in cam.dist],
        "R": [float(x) for x in np.asarray(cam.R, float).ravel()],
        "T": [float(x) for x in np.asarray(cam.T, float).ravel()],
    }


def from_calibration(cal: Calibration, kind: str) -> dict:
    left = cal.cameras["left"]
    cameras = {}
    provenance = [
        f"source={cal.source}",
        f"key_mode={cal.provenance.get('key_mode', '')}",
        f"skew_mode={cal.provenance.get('skew_mode', '')}",
        f"world_frame=ir_left",
    ]
    for name, rec in (cal.provenance.get("sha256", {}) or {}).items():
        provenance.append(f"ccf_sha256:{name}={rec}")
    for src, dst in NAMES.items():
        cam = cal.cameras[src]
        # world frame = left IR camera: X_left = R_l X_w + T_l
        R_wc = np.asarray(cam.R, float)
        T_wc = np.asarray(cam.T, float)
        if src == "left":
            R = np.eye(3); T = np.zeros(3)
        else:
            R_l, T_l = np.asarray(left.R, float), np.asarray(left.T, float)
            R = R_wc @ R_l.T
            T = T_wc - R @ T_l
        cameras[dst] = {
            "fx": float(cam.K[0, 0]), "fy": float(cam.K[1, 1]),
            "cx": float(cam.K[0, 2]), "cy": float(cam.K[1, 2]),
            "skew": float(cam.K[0, 1]),
            "dist": [float(x) for x in cam.dist],
            "R": [float(x) for x in R.ravel()],
            "T": [float(x) for x in T.ravel()],
        }
    return {
        "schema": SCHEMA, "kind": kind,
        "image_width": cal.image_size[0], "image_height": cal.image_size[1],
        "units": "mm", "plate_pitch_mm": 28.0,
        # The shipped Tex CCF is the known stale/mixed-generation record
        # (17.66 mm from the joint fit); colour is skipped by default until a
        # replacement calibration validates it.
        "color_status": "stale",
        "cameras": cameras,
        "provenance": provenance,
    }


def from_result(path: Path) -> dict:
    data = json.loads(path.read_text())
    r = data.get("result", data)
    cams = r["cameras"]; extr = r["extrinsics"]
    cameras = {}
    for src, dst in NAMES.items():
        c = cams[src]
        R = np.asarray(extr[src]["R"], float).ravel()
        T = np.asarray(extr[src]["t"], float).ravel()
        cameras[dst] = {
            "fx": float(c["fx"]), "fy": float(c["fy"]),
            "cx": float(c["cx"]), "cy": float(c["cy"]),
            "skew": float(c.get("skew", 0.0)),
            "dist": [float(x) for x in c["kc"]],
            "R": [float(x) for x in R], "T": [float(x) for x in T],
        }
    info = data.get("info", {})
    provenance = [
        f"source={path}",
        f"result_sha256={sha256_file(path)}",
        f"pitch_mm={info.get('pitch_mm', r.get('info', {}).get('pitch_mm', ''))}",
        f"pitch_source={r.get('info', {}).get('pitch_source', '')}",
        f"anchored={info.get('anchored', '')}",
        "world_frame=ir_left",
    ]
    return {
        "schema": SCHEMA, "kind": "replacement",
        "image_width": 1280, "image_height": 1024, "units": "mm",
        "plate_pitch_mm": float(info.get("pitch_mm", 28.0)),
        # A replacement fit that included the colour camera is still not a
        # *validated* colour calibration: mark it unknown unless the caller
        # supplies evidence (--color-status ok, e.g. from a held-out colour
        # reprojection check).  The reconstruction engine skips colour for
        # anything other than "ok".
        "color_status": "unknown",
        "cameras": cameras, "provenance": provenance,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("ccf", "dump", "auto"), default="auto")
    ap.add_argument("--ccf-dir", type=Path, default=None)
    ap.add_argument("--result", type=Path, default=None,
                    help="calib.cli calibrate result JSON -> replacement profile")
    ap.add_argument("--kind", default=None)
    ap.add_argument("--color-status", choices=("ok", "stale", "unknown"), default=None,
                    help="override the colour-profile trust state (default: stale "
                         "for factory, unknown for a replacement fit)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.result:
        profile = from_result(args.result)
        if args.kind:
            profile["kind"] = args.kind
    else:
        cal = Calibration.load(args.source, **({"ccf_dir": args.ccf_dir} if args.ccf_dir else {}))
        profile = from_calibration(cal, args.kind or "factory")
    if args.color_status:
        profile["color_status"] = args.color_status
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(profile, indent=2))
    # quick geometric sanity check
    R = np.asarray(profile["cameras"]["ir_right"]["R"], float).reshape(3, 3)
    T = np.asarray(profile["cameras"]["ir_right"]["T"], float)
    print(f"wrote {args.out}  kind={profile['kind']}  det(R)={np.linalg.det(R):.9f}  "
          f"baseline={np.linalg.norm(T):.3f} mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
