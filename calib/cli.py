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


def _pitch_source(pitch: float) -> str:
    """Human-readable provenance for a pitch value (see calib/plate.py)."""
    for name, value in plate.PITCH_SOURCES_MM.items():
        if abs(float(pitch) - float(value)) < 1e-9:
            return name
    return "cli (unvalidated)"


def _apply_plate(args):
    cols = int(getattr(args, "cols", plate.GRID_COLS))
    rows = int(getattr(args, "rows", plate.GRID_ROWS))
    pitch = float(getattr(args, "pitch", plate.VENDOR_PITCH_MM))
    spec = plate.PlateSpec(cols, rows, pitch_mm=pitch,
                           pitch_source=_pitch_source(pitch))
    plate.use_spec(spec)
    return plate.current_spec()


def cmd_detect(args):
    _apply_plate(args)
    det = detect_directory(args.images, refine_mode=args.refine_mode)
    print(json.dumps(det.summary(), indent=2))
    return 0


def cmd_calibrate(args):
    _apply_plate(args)
    res, det, info = calibrate_directory(
        args.images, args.factory, pitch_mm=args.pitch,
        free_kc=args.free_distortion, anchor_baseline=not args.no_anchor,
        max_nfev=args.max_nfev, refine_mode=args.refine_mode)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    obs = det.observations()
    from .quality import accuracy_preset
    stop = stopping_report(res, obs,
                           heldout_rms_px=info.get("heldout_rms_px"),
                           pitch=args.pitch, compute_stability=True,
                           **accuracy_preset(args.target))
    from .quality import bootstrap_sigma, quality_report
    boot = bootstrap_sigma(obs, res, args.pitch, n_boot=6, drop=4,
                           seed=0) if args.bootstrap else None
    if boot:
        info["bootstrap"] = boot
    quality = quality_report(res, obs, heldout_rms_px=info.get("heldout_rms_px"),
                             target=args.target, pitch=args.pitch,
                             stopping=stop, bootstrap=boot)
    payload = {"result": res.as_dict(), "info": info,
               "stopping": stop, "quality": quality,
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
        meta = {"info": info, "quality": quality,
                "validation": {"status": quality["status"],
                               "heldout_rms_px": info.get("heldout_rms_px"),
                               "target": args.target},
                "pitch_mm": args.pitch,
                "engine": "calib"}
        rev = store.save_revision(serial, recs, meta, res.covariance,
                                  res.param_std() and list(res.param_std().keys()))
        store.activate(serial, rev.index)
        print(f"profile revision {rev.index} at {rev.root} (active)")
    return 0


def cmd_plan(args):
    data = json.loads(Path(args.result).read_text())
    res = data["result"]
    # Rebuild a minimal result object from the JSON
    from .calibrate import CalibrationResult, CameraIntrinsics
    cams = {c: CameraIntrinsics(**{k: (np.array(val) if k == "kc" else val)
                                  for k, val in cam.items()
                                  if k in ("fx", "fy", "cx", "cy", "kc", "skew")})
            for c, cam in res["cameras"].items()}
    extr = {c: (np.array(v["R"]), np.array(v["t"])) for c, v in res["extrinsics"].items()}
    views = [(np.array(v["R"]), np.array(v["t"])) for v in res["views"]]
    cov = None
    info = dict(res.get("info", {}))
    if getattr(args, "profile", None):
        store = ProfileStore(args.profile)
        active = store.device_dir(args.serial) / "active"
        if active.exists():
            rev = store.revisions(args.serial)[int(active.read_text().strip()) - 1]
        else:
            revs = store.revisions(args.serial)
            if not revs:
                raise SystemExit("no profile revisions; run calibrate --profile first")
            rev = revs[-1]
        z = np.load(rev.root / "covariance.npz", allow_pickle=True)
        cov = z["cov"]
        info = dict(rev.meta.get("info", {}))
        info.setdefault("free_mask", [True] * cov.shape[0])
    elif res.get("covariance_available") is not None:
        pass
    rr = CalibrationResult(cams, extr, views, cov, res["residual_rms_px"],
                           res["residual_max_px"], res["n_observations"],
                           res["n_parameters"], info=info)
    if cov is None:
        print("warning: no covariance loaded; pass --profile for a meaningful plan", file=sys.stderr)
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
    d.add_argument("--refine-mode", choices=("centroid", "ellipse", "edge"), default="centroid")
    d.add_argument("--cols", type=int, default=8)
    d.add_argument("--rows", type=int, default=5)
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
    c.add_argument("--refine-mode", choices=("centroid", "ellipse", "edge"), default="centroid")
    c.add_argument("--target", choices=("fast", "standard", "high"), default="standard")
    c.add_argument("--bootstrap", action="store_true",
                   help="6-fit view-bootstrap sigma check (slower, conservative)")
    c.add_argument("--cols", type=int, default=8)
    c.add_argument("--rows", type=int, default=5)
    c.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("plan", help="recommend next views from a calibration result")
    p.add_argument("result")
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--pitch", type=float, default=plate.DEFAULT_PITCH_MM)
    p.add_argument("--profile", default=None)
    p.add_argument("--serial", default="unknown")
    p.set_defaults(func=cmd_plan)

    w = sub.add_parser("workflow", help="parse the EXStar workflow XML")
    w.add_argument("xml")
    w.set_defaults(func=cmd_workflow)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
