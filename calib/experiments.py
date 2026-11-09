"""End-to-end experiments: adaptive vs fixed EXStar-style sequences."""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from . import plate
from .adaptive import candidate_pool, plan_views, rig_vector, select_next, rig_covariance
from .calibrate import CAMERAS, Observation, calibrate
from .evaluate import intrinsic_errors, rig_errors
from .geometry import project
from .synth import SyntheticScene, make_scene, perturbed_init


def observe_pose(scene: SyntheticScene, R: np.ndarray, t: np.ndarray,
                 rng: np.random.Generator, noise_px: Optional[float] = None,
                 outlier_frac: Optional[float] = None) -> list[Observation]:
    noise_px = scene.noise_px if noise_px is None else noise_px
    outlier_frac = scene.outlier_frac if outlier_frac is None else outlier_frac
    out = []
    for c in CAMERAS:
        Rc, tc = scene.extrinsics[c]
        Rw = Rc @ R; tw = Rc @ t + tc
        Xc = plate.board_points_mm(scene.pitch_mm) @ Rw.T + tw
        uv = project(scene.cameras[c].K, scene.cameras[c].kc, Xc)
        uv = uv + rng.normal(0, noise_px, uv.shape)
        if outlier_frac:
            m = rng.random(len(uv)) < outlier_frac
            uv[m] += rng.normal(0, 8.0, (m.sum(), 2))
        dct = {(float(u), float(v)): uv[i]
               for i, (u, v) in enumerate(plate.lattice_uv())}
        out.append(Observation(c, -1, dct))
    return out


def _run(scene: SyntheticScene, obs: list[Observation], init: dict,
         pitch: float, free_kc: bool = False):
    # renumber views to a compact range
    views = sorted(set(o.view for o in obs))
    remap = {v: i for i, v in enumerate(views)}
    obs2 = [Observation(o.camera, remap[o.view], o.uv) for o in obs]
    res = calibrate(obs2, pitch, init, free_kc=free_kc, free_intrinsics=True,
                    max_nfev=800)
    return res, obs2


def run_adaptive_vs_fixed(n_trials: int = 3, seed_views: int = 4,
                          budget: int = 8, noise_px: float = 0.2,
                          outlier_frac: float = 0.02, free_kc: bool = False,
                          seed0: int = 7000) -> dict:
    rows = []
    for trial in range(n_trials):
        rng = np.random.default_rng(seed0 + trial)
        scene = make_scene(n_views=25, noise_px=noise_px,
                           outlier_frac=outlier_frac, seed=seed0 + trial)
        init = perturbed_init(scene, rng)
        # fixed: the first `budget` scene views (stand-in for EXStar's fixed 5x5)
        fixed_obs = [o for o in scene.observations if o.view < budget]
        res_f, obs_f = _run(scene, fixed_obs, init, scene.pitch_mm, free_kc)
        # adaptive: seed views + greedy information selection
        seed_views_idx = list(range(seed_views))
        obs_a = [o for o in scene.observations if o.view in seed_views_idx]
        res_a, obs_a2 = _run(scene, obs_a, init, scene.pitch_mm, free_kc)
        pool = candidate_pool(scene.pitch_mm)
        chosen = []
        n_add = budget - seed_views
        timeline = []
        for step in range(n_add):
            best = select_next(pool, rig_vector(res_a), rig_covariance(res_a),
                               scene.pitch_mm, used={c.label for c in chosen})
            if best is None:
                break
            cand, gain, _ = best
            chosen.append(cand)
            new_obs = observe_pose(scene, cand.R, cand.t, rng)
            view_id = 1000 + step
            for o in new_obs:
                o.view = view_id
            obs_a = obs_a + new_obs
            res_a, obs_a2 = _run(scene, obs_a, init, scene.pitch_mm, free_kc)
            timeline.append({"step": step, "label": cand.label, "gain": gain,
                             "rms": res_a.residual_rms,
                             "baseline": float(np.linalg.norm(res_a.baseline()))})
        def metrics(res):
            ie = intrinsic_errors(res.cameras, scene.cameras)
            re = rig_errors(res, scene.truth["extrinsics"], scene.truth["baseline_mm"])
            return {"fx_abs": [abs(ie[c]["fx"]) for c in CAMERAS],
                    "baseline_err": re["baseline_mm"],
                    "rot_err": re["right"]["rot_deg"],
                    "rms": res.residual_rms,
                    "n_views": len(set(o.view for o in obs_a2))}
        rows.append({"trial": trial, "fixed": metrics(res_f),
                     "adaptive": metrics(res_a), "timeline": timeline,
                     "chosen": [c.label for c in chosen]})
    return {"rows": rows, "summary": _summ(rows)}


def _summ(rows):
    out = {}
    for key in ("fixed", "adaptive"):
        vals = [r[key] for r in rows]
        out[key] = {
            "fx_abs_med": float(np.median([np.median(v["fx_abs"]) for v in vals])),
            "baseline_abs_med_mm": float(np.median([abs(v["baseline_err"]) for v in vals])),
            "rot_err_med_deg": float(np.median([v["rot_err"] for v in vals])),
            "rms_med": float(np.median([v["rms"] for v in vals])),
            "n_views_med": float(np.median([v["n_views"] for v in vals])),
        }
    return out
