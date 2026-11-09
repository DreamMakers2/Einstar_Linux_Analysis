"""Directory-level calibration pipeline (the user-facing entry point)."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
import re
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from . import plate
from .calibrate import CAMERAS, CalibrationResult, CameraIntrinsics, Observation, calibrate
from .ccf import CCFRecord, load as load_ccf
from .detect import GridObservation, detect_grid_auto
from .integration import to_ccf_records, result_summary


SIDE_TO_CAM = {"Left": "left", "Right": "right", "Tex": "tex",
               "left": "left", "right": "right", "tex": "tex", "color": "tex"}


@dataclass
class DetectionSet:
    grids: dict[tuple[str, int], GridObservation]
    failures: list[str]

    def observations(self) -> list[Observation]:
        out = []
        for (side, idx), g in sorted(self.grids.items()):
            cam = SIDE_TO_CAM[side]
            out.append(Observation(cam, idx - 1,
                                   {(float(u), float(v)): xy
                                    for (u, v), xy in g.points.items()}))
        return out

    def summary(self) -> dict:
        per = {}
        for (side, idx), g in self.grids.items():
            per.setdefault(side, []).append(g.n_lattice)
        return {"n_grids": len(self.grids), "failures": self.failures,
                "n_lattice": per}


def find_images(directory: str | Path) -> dict[tuple[str, int], str]:
    """Match ``imageLeft12.bmp`` / ``image_right_12.png``-style names."""
    d = Path(directory)
    out = {}
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in (".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pgm"):
            continue
        m = re.search(r"(left|right|tex|color)[_-]?(\d+)", p.name, re.I)
        if not m:
            continue
        side = m.group(1).capitalize()
        side = {"Left": "Left", "Right": "Right", "Tex": "Tex", "Color": "Tex"}[side]
        out[(side, int(m.group(2)))] = str(p)
    return out


def detect_directory(directory: str | Path, auto_polarity: bool = True,
                     refine_mode: str = "centroid") -> DetectionSet:
    files = find_images(directory)
    grids = {}
    failures = []
    for key, path in files.items():
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            failures.append(path)
            continue
        obs = detect_grid_auto(img, refine_mode=refine_mode)
        if obs is None or obs.n_lattice < 25:
            failures.append(path)
            continue
        grids[key] = obs
    return DetectionSet(grids, failures)


def calibrate_directory(directory: str | Path,
                        factory_ccf_dir: Optional[str | Path] = None,
                        pitch_mm: float = plate.DEFAULT_PITCH_MM,
                        free_kc: bool = False,
                        anchor_baseline: bool = True,
                        max_nfev: int = 1200,
                        refine_mode: str = "centroid",
                        heldout: bool = True,
                        anchor_sigma_mm: float = 0.05) -> tuple[CalibrationResult, DetectionSet, dict]:
    det = detect_directory(directory, refine_mode=refine_mode)
    obs = det.observations()
    if not obs:
        raise RuntimeError("no usable calibration grids found")
    factory = {}
    init = None
    if factory_ccf_dir is not None:
        fdir = Path(factory_ccf_dir)
        for name, cam in (("LeftCCF.txt", "left"), ("RightCCF.txt", "right"),
                          ("TexCCF.txt", "tex")):
            p = fdir / name
            if p.exists():
                factory[cam] = load_ccf(p)
        if factory:
            init = {c: CameraIntrinsics.from_ccf(r) for c, r in factory.items()}
    if init is None:
        init = {c: CameraIntrinsics(1150.0, 1150.0, 640.0, 512.0, np.zeros(5))
                for c in CAMERAS}
        for c in CAMERAS:
            if c not in init:
                init[c] = CameraIntrinsics(1150.0, 1150.0, 640.0, 512.0, np.zeros(5))
    res = calibrate(obs, pitch_mm, init, free_kc=free_kc,
                    free_intrinsics=True, max_nfev=max_nfev)
    # reject whole views whose detections are grossly inconsistent with the
    # fitted rig; a single bad grid can otherwise dominate the covariance
    from .evaluate import per_view_errors
    pv = per_view_errors(res, obs, pitch_mm)
    if pv:
        med = float(np.median([d["rms_px"] for d in pv.values()]))
        bad = {v for v, d in pv.items() if d["rms_px"] > max(2.0, 3.0 * med)}
    else:
        bad = set()
    if bad:
        obs = [o for o in obs if o.view not in bad]
        res = calibrate(obs, pitch_mm, init, free_kc=free_kc,
                        free_intrinsics=True, max_nfev=max_nfev)
    info = {"summary": result_summary(res), "detections": det.summary(),
            "pitch_mm": pitch_mm, "anchored": False,
            "plate": plate.plate_summary(),
            "rejected_views": sorted(int(v) for v in bad)}
    if anchor_baseline and factory and "left" in factory and "right" in factory:
        from .geometry import relative_pose
        R0, t0 = relative_pose(factory["left"].R(), factory["left"].T(),
                               factory["right"].R(), factory["right"].T())
        target = float(np.linalg.norm(t0))
        cur = float(np.linalg.norm(res.baseline()))
        if cur > 1e-9:
            res = res.rescaled(target_baseline=target,
                               anchor_sigma_mm=float(anchor_sigma_mm))
            info.update({"anchored": True, "target_baseline_mm": target,
                         "scale": res.info.get("scale_factor"),
                         "anchored_sigma_mm": float(anchor_sigma_mm),
                         "measured_baseline_before_anchor_mm": cur,
                         "implied_pitch_mm": pitch_mm * res.info.get("scale_factor", 1.0)})
            info["summary"] = result_summary(res)
    # frame-invariant consistency guard against the factory CCF records.  The
    # shipped Tex record can be mixed-generation/stale; never fit to it blindly.
    if factory:
        from .ccf import consistency, relative_pose as ccf_relative
        cons = {}
        for cam in ("right", "tex"):
            if "left" in factory and cam in factory:
                Rf, tf = ccf_relative(factory["left"], factory[cam])
                Re = res.extrinsics[cam][0] @ res.extrinsics["left"][0].T
                te = res.baseline("left", cam)
                cons[cam] = consistency(factory["left"], factory[cam],
                                        expected=(Re, te),
                                        tau_translation_mm=2.0 if cam == "right" else 3.0,
                                        tau_rotation_deg=0.3)
        info["factory_consistency"] = cons
        if cons.get("tex", {}).get("ok") is False:
            info["tex_consistency_warning"] = (
                "shipped Tex CCF relative translation disagrees with this fit by "
                f"{cons['tex'].get('delta_translation_mm'):.2f} mm; the record is "
                "likely mixed-generation/stale. Do not fit to it without re-checking.")

    # independent held-out check: fit without one view, predict it from the rig
    if heldout:
        from .evaluate import heldout_reprojection
        used = sorted(set(int(o.view) for o in obs))
        candidates = [v for v in used
                      if any(o.view == v and o.camera == "left" for o in obs)]
        if len(candidates) > 4:
            hv = candidates[-1]
            tr = [o for o in obs if int(o.view) != hv]
            rt = calibrate(tr, pitch_mm, init, free_kc=free_kc,
                           free_intrinsics=True, max_nfev=max_nfev)
            h = heldout_reprojection(obs, rt, pitch_mm, hv)
            info["heldout_rms_px"] = h.get("rms_px")
            info["heldout_view"] = hv
            info["heldout"] = h
    return res, det, info


def profile_from_result(res: CalibrationResult, factory: dict[str, CCFRecord] | None = None):
    return to_ccf_records(res, factory)
