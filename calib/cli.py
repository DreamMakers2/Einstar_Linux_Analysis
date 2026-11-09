#!/usr/bin/env python3
"""Command-line entry point for the Linux-native Einstar calibration engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

from . import plate
from .adaptive import candidate_pool, plan_views, stopping_report, rig_covariance
from .calibrate import CameraIntrinsics
from .ccf import load as load_ccf
from .pipeline import calibrate_directory, detect_directory
from .profiles import ProfileStore
from .workflow import exstar_sequence, parse_rapid_config


def cmd_detect(args):
    det = detect_directory(args.images, refine_mode=args.refine_mode)
    print(json.dumps(det.summary(), indent=2))
    return 0


def cmd_calibrate(args):
    res, det, info = calibrate_directory(
        args.images, args.factory, pitch_mm=args.pitch,
        free_kc=args.free_distortion, anchor_baseline=not args.no_anchor,
        max_nfev=args.max_nfev, refine_mode=args.refine_mode)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"result": res.as_dict(), "info": info,
               "stopping": stopping_report(res),
               "detections": det.summary()}
    out.write_text(json.dumps(payload, indent=2, default=float))
    print(json.dumps(info["summary"], indent=2))
    print(json.dumps(payload["stopping"], indent=2))
    if args.profile:
        factory = {}
        if args.factory:
            for name, cam in (("LeftCCF.txt", "left"), ("RightCCF.txt", "right"),
                              ("TexCCF.txt", "tex")):
                p = Path(args.factory) / name
                if p.exists():
                    factory[cam] = load_ccf(p)
        recs = __import__("calib.integration", fromlist=["to_ccf_records"]).to_ccf_records(res, factory)
        store = ProfileStore(args.profile)
        serial = args.serial
        if args.factory:
            try:
                store.snapshot_factory(serial, {
                    "left": Path(args.factory) / "LeftCCF.txt",
                    "right": Path(args.factory) / "RightCCF.txt",
                    "tex": Path(args.factory) / "TexCCF.txt"})
            except FileExistsError:
                pass
        rev = store.save_revision(serial, recs, {"info": info}, res.covariance,
                                  res.param_std() and list(res.param_std().keys()))
        print(f"profile revision {rev.index} at {rev.root}")
    return 0


def cmd_plan(args):
    data = json.loads(Path(args.result).read_text())
    res = data["result"]
    # Rebuild a minimal result object from the JSON
    from .calibrate import CalibrationResult, CameraIntrinsics
    cams = {c: CameraIntrinsics(**{k: (np.array(v["kc"]) if k == "kc" else v[k])
                                  for k, v in cam.items() if k in ("fx", "fy", "cx", "cy", "kc", "skew")})
            for c, cam in res["cameras"].items()}
    extr = {c: (np.array(v["R"]), np.array(v["t"])) for c, v in res["extrinsics"].items()}
    views = [(np.array(v["R"]), np.array(v["t"])) for v in res["views"]]
    cov = None
    rr = CalibrationResult(cams, extr, views, cov, res["residual_rms_px"],
                           res["residual_max_px"], res["n_observations"],
                           res["n_parameters"])
    plan = plan_views(candidate_pool(args.pitch), rr, args.pitch, n=args.n)
    print(json.dumps({"selected": plan["selected"],
                      "sigma": {k: v for k, v in list(plan["sigma"].items())[:8]}},
                     indent=2))
    return 0


def cmd_workflow(args):
    cfg = parse_rapid_config(args.xml)
    print(json.dumps({"groups": [g.__dict__ for g in cfg.groups],
                      "n_steps": cfg.n_steps, "n_images": cfg.n_images,
                      "exposure": cfg.exposure}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="detect grids in a directory of images")
    d.add_argument("images")
    d.add_argument("--refine-mode", choices=("centroid", "ellipse"), default="centroid")
    d.set_defaults(func=cmd_detect)

    c = sub.add_parser("calibrate", help="calibrate from a directory of images")
    c.add_argument("images")
    c.add_argument("--factory", default=None, help="directory with LeftCCF.txt/...")
    c.add_argument("--pitch", type=float, default=plate.DEFAULT_PITCH_MM)
    c.add_argument("--free-distortion", action="store_true")
    c.add_argument("--no-anchor", action="store_true")
    c.add_argument("--max-nfev", type=int, default=1200)
    c.add_argument("--out", default="calibration_result.json")
    c.add_argument("--profile", default=None, help="host-side profile root")
    c.add_argument("--serial", default="unknown")
    c.add_argument("--refine-mode", choices=("centroid", "ellipse"), default="centroid")
    c.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("plan", help="recommend next views from a calibration result")
    p.add_argument("result")
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--pitch", type=float, default=plate.DEFAULT_PITCH_MM)
    p.set_defaults(func=cmd_plan)

    w = sub.add_parser("workflow", help="parse the EXStar workflow XML")
    w.add_argument("xml")
    w.set_defaults(func=cmd_workflow)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
