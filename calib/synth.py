"""Synthetic ground truth for calibration experiments.

No physical claim rests on these scenes; they are used to measure estimator
behaviour (bias, covariance calibration, early stopping, sensitivity to bad
views) against a *known* truth, which the vendor corpus cannot provide.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from . import plate
from .calibrate import CAMERAS, CameraIntrinsics, Observation
from .geometry import project, project_world, rodrigues, rvec_from_R
from .adaptive import board_pose


@dataclass
class SyntheticScene:
    cameras: dict[str, CameraIntrinsics]
    extrinsics: dict[str, tuple[np.ndarray, np.ndarray]]
    views: list[tuple[np.ndarray, np.ndarray]]
    observations: list[Observation]
    pitch_mm: float
    noise_px: float
    outlier_frac: float
    truth: dict

    def ground_truth(self) -> dict:
        return self.truth


def make_camera(name: str, rng: np.random.Generator) -> CameraIntrinsics:
    fx = 1150.0 + rng.normal(0, 20)
    fy = fx * (1.0 + rng.normal(0, 0.001))
    cx = 640.0 + rng.normal(0, 8)
    cy = 512.0 + rng.normal(0, 8)
    dist = np.array([-0.16, 0.17, 0.0004, 0.0002, 0.02]) + rng.normal(0, 0.005, 5)
    return CameraIntrinsics(float(fx), float(fy), float(cx), float(cy), dist)


def make_scene(n_views: int = 25, pitch_mm: float = plate.DEFAULT_PITCH_MM,
               noise_px: float = 0.2, outlier_frac: float = 0.02,
               seed: int = 0, distance_range=(240.0, 560.0),
               tilt_max_deg: float = 25.0) -> SyntheticScene:
    rng = np.random.default_rng(seed)
    cams = {c: make_camera(c, rng) for c in CAMERAS}
    # rig: right ~158.5 mm along x, 22 deg toe-in; tex ~125 mm from left
    Rr = rodrigues(np.array([0.0, np.radians(22.0), 0.0]))
    tr = np.array([-158.5, 0.0, 30.0])
    Rt = rodrigues(np.array([0.0, np.radians(8.0), 0.0]))
    tt = np.array([-120.0, -20.0, 10.0])
    extr = {"left": (np.eye(3), np.zeros(3)), "right": (Rr, tr), "tex": (Rt, tt)}
    views = []
    obs = []
    for v in range(n_views):
        d = rng.uniform(*distance_range)
        tx = rng.uniform(-tilt_max_deg, tilt_max_deg)
        ty = rng.uniform(-tilt_max_deg, tilt_max_deg)
        roll = rng.choice([0.0, 90.0, 180.0, 270.0]) + rng.normal(0, 2)
        R, t = board_pose(d, tx, ty, roll)
        views.append((R, t))
        for c in CAMERAS:
            Rc, tc = extr[c]
            Rw = Rc @ R; tw = Rc @ t + tc
            Xc = plate.board_points_mm(pitch_mm) @ Rw.T + tw
            uv = project(cams[c].K, cams[c].kc, Xc)
            uv = uv + rng.normal(0, noise_px, uv.shape)
            if outlier_frac > 0:
                m = rng.random(len(uv)) < outlier_frac
                uv[m] += rng.normal(0, 8.0, (m.sum(), 2))
            uv = uv.astype(float)
            dct = {(float(u), float(v)): uv[i]
                   for i, (u, v) in enumerate(plate.lattice_uv())}
            obs.append(Observation(c, v, dct))
    truth = {
        "cameras": {c: cams[c].as_dict() for c in CAMERAS},
        "extrinsics": {c: {"R": extr[c][0].tolist(), "t": extr[c][1].tolist()}
                       for c in CAMERAS},
        "views": [{"R": R.tolist(), "t": t.tolist()} for R, t in views],
        "baseline_mm": float(np.linalg.norm(tr - Rr @ np.zeros(3))),
        "pitch_mm": pitch_mm,
    }
    return SyntheticScene(cams, extr, views, obs, pitch_mm, noise_px,
                          outlier_frac, truth)


def perturbed_init(scene: SyntheticScene, rng: np.random.Generator,
                   fx_sigma: float = 6.0, cx_sigma: float = 6.0) -> dict[str, CameraIntrinsics]:
    out = {}
    for c, cam in scene.cameras.items():
        out[c] = CameraIntrinsics(cam.fx + rng.normal(0, fx_sigma),
                                  cam.fy + rng.normal(0, fx_sigma),
                                  cam.cx + rng.normal(0, cx_sigma),
                                  cam.cy + rng.normal(0, cx_sigma),
                                  cam.kc.copy())
    return out
