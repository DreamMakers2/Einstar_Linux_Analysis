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


def detect_directory(directory: str | Path, auto_polarity: bool = True) -> DetectionSet:
    files = find_images(directory)
    grids = {}
    failures = []
    for key, path in files.items():
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            failures.append(path)
            continue
        obs = detect_grid_auto(img)
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
                        max_nfev: int = 1200) -> tuple[CalibrationResult, DetectionSet, dict]:
    det = detect_directory(directory)
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
    info = {"summary": result_summary(res), "detections": det.summary(),
            "pitch_mm": pitch_mm, "anchored": False}
    if anchor_baseline and factory and "left" in factory and "right" in factory:
        from .geometry import relative_pose
        R0, t0 = relative_pose(factory["left"].R(), factory["left"].T(),
                               factory["right"].R(), factory["right"].T())
        target = float(np.linalg.norm(t0))
        cur = float(np.linalg.norm(res.baseline()))
        if cur > 1e-9:
            s = target / cur
            res = res.rescaled(s)
            info.update({"anchored": True, "target_baseline_mm": target,
                         "scale": s, "measured_baseline_before_anchor_mm": cur,
                         "implied_pitch_mm": pitch_mm * s})
    return res, det, info


def profile_from_result(res: CalibrationResult, factory: dict[str, CCFRecord] | None = None):
    return to_ccf_records(res, factory)
