"""Adaptive calibration session: guided continuous acquisition.

The session replaces EXStar's fixed 25-shot procedure with a state machine:

1. **Bootstrap** -- accept a few diverse views (no calibration exists yet).
2. **Core** -- after every accepted view, predict the task-space gain of the
   reachable candidates, accept the next frame only if it is informative and of
   good quality, and re-solve.
3. **Refinement** -- free the distortion coefficients once enough views exist,
   switch to a stricter acceptance threshold, and keep going while the predicted
   gain is meaningful.

Every rejection is explained.  The calibration itself is a batch joint
bundle adjustment (:mod:`calib.calibrate` or a pluggable faster backend); the
session only decides *which* observations enter it.  Nothing is written to the
scanner: :meth:`CalibrationSession.finish` returns a versioned host profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

from . import guidance, objective, plate
from .adaptive import candidate_pool
from .calibrate import CAMERAS, CalibrationResult, CameraIntrinsics, Observation, calibrate
from .detect import detect_grid_auto
from .quality import stopping_report

# ---------------------------------------------------------------------------
# policies
# ---------------------------------------------------------------------------

MODES = {
    "quick": {
        "bootstrap_views": 3, "kc_views": 10 ** 6, "min_rel_gain": 0.010,
        "stop_min_views": 5, "stop_min_gain": 0.002,
        "stop_min_delta_sigma_mm": 0.05, "stop_min_recent_gain": 0.02,
        "stop_min_recent_sigma_mm": 0.05,
        "free_kc": False, "robust_loss": "huber", "f_scale": 1.5,
        "solve_every": 1, "quality": {"min_markers": 20},
    },
    "standard": {
        "bootstrap_views": 4, "kc_views": 10, "min_rel_gain": 0.004,
        "stop_min_views": 8, "stop_min_gain": 0.0010,
        "stop_min_delta_sigma_mm": 0.015, "stop_min_recent_gain": 0.01,
        "stop_min_recent_sigma_mm": 0.015,
        "free_kc": True, "robust_loss": "huber", "f_scale": 1.0,
        "solve_every": 1, "quality": {},
    },
    "high": {
        "bootstrap_views": 5, "kc_views": 8, "min_rel_gain": 0.002,
        "stop_min_views": 12, "stop_min_gain": 0.0003,
        "stop_min_delta_sigma_mm": 0.005, "stop_min_recent_gain": 0.003,
        "stop_min_recent_sigma_mm": 0.005,
        "free_kc": True, "robust_loss": "huber", "f_scale": 0.8,
        "solve_every": 1, "quality": {"min_markers": 28, "max_saturation_frac": 0.01},
    },
}


# ---------------------------------------------------------------------------
# data types
# ---------------------------------------------------------------------------

@dataclass
class FrameSet:
    images: dict[str, np.ndarray]
    timestamp: float = 0.0
    camera_timestamps: Optional[dict] = None
    label: str = ""


@dataclass
class AcceptedView:
    view_id: int
    pose: tuple
    timestamp: float
    quality: dict
    predicted_gain: float
    holdout_rms_px: Optional[float]
    n_markers: dict
    label: str = ""

    def as_dict(self) -> dict:
        return {"view_id": self.view_id,
                "distance_mm": float(np.linalg.norm(self.pose[1])),
                "tilt_deg": float(np.degrees(np.arccos(np.clip(
                    abs(np.asarray(self.pose[0])[2, 2]), -1, 1)))),
                "timestamp": self.timestamp, "predicted_gain": self.predicted_gain,
                "holdout_rms_px": self.holdout_rms_px,
                "n_markers": self.n_markers, "label": self.label}


@dataclass
class Decision:
    accepted: bool
    reasons: list
    quality: dict
    predicted_gain: Optional[float]
    view_id: Optional[int] = None
    solved: bool = False
    status: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "reasons": list(self.reasons),
                "quality": self.quality, "predicted_gain": self.predicted_gain,
                "view_id": self.view_id, "solved": self.solved,
                "status": self.status}


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

class CalibrationSession:
    """Guided, adaptive three-camera calibration."""

    def __init__(self, mode: str = "standard",
                 factory: Optional[dict[str, "object"]] = None,
                 pitch_mm: float = plate.DEFAULT_PITCH_MM,
                 cameras: Optional[dict[str, CameraIntrinsics]] = None,
                 solve_fn: Optional[Callable] = None,
                 volume_points: Optional[np.ndarray] = None,
                 pool: Optional[Sequence] = None,
                 live: bool = False,
                 quality: Optional[dict] = None,
                 localize_method: str = "auto",
                 refine_off_lattice: bool = False,
                 seed: int = 0):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose {sorted(MODES)}")
        self.mode = mode
        self.policy = dict(MODES[mode])
        if quality:
            self.policy["quality"] = {**self.policy.get("quality", {}), **quality}
        self.pitch_mm = float(pitch_mm)
        self.live = bool(live)
        self.localize_method = localize_method
        self.refine_off_lattice = bool(refine_off_lattice)
        self.detect_seconds = 0.0
        self.refine_seconds = 0.0
        self.policy.setdefault("require_images", bool(live))
        self.rng = np.random.default_rng(seed)
        self.solve_fn = solve_fn or _default_solve
        self.points = objective.measurement_volume() if volume_points is None else np.asarray(volume_points, float)
        self.pool = list(pool) if pool is not None else candidate_pool(self.pitch_mm)
        self.factory = factory or {}
        self.cameras = cameras or {}
        if not self.cameras and self.factory:
            self.cameras = {c: CameraIntrinsics.from_ccf(self.factory[c])
                            for c in CAMERAS if c in self.factory}
        if not self.cameras:
            self.cameras = {c: CameraIntrinsics(1150.0, 1150.0, 640.0, 512.0,
                                                np.zeros(5)) for c in CAMERAS}
        self.obs: list[Observation] = []
        self.accepted: list[AcceptedView] = []
        self.rejected: list[dict] = []
        self.result: Optional[CalibrationResult] = None
        self.last_pose: Optional[tuple] = None
        self.last_timestamp: Optional[float] = None
        self._next_view = 0
        self._solves = 0
        self.solve_seconds = 0.0
        self._last_result: Optional[CalibrationResult] = None
        self._task_cache: dict = {}
        self.last_solve_error: Optional[str] = None
        self._task_history: list[float] = []
        self._sigma_history: list[float] = []
        self._stop_checked = False

    # -- observation helpers ------------------------------------------------
    def _detect(self, frames: FrameSet) -> dict[str, Observation]:
        grids = {}
        for c in CAMERAS:
            img = frames.images.get(c)
            if img is None:
                continue
            if img.ndim == 3:
                img = img[..., 0] if img.shape[2] == 1 else (
                    0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2])
            g = detect_grid_auto(img.astype(np.uint8))
            if g is not None:
                grids[c] = Observation(c, -1,
                                       {tuple(float(x) for x in k): np.asarray(v, float)
                                        for k, v in g.points.items()})
        return grids

    def _refine(self, frames: FrameSet, grids: dict) -> dict:
        try:
            from .localize import refine_grid
        except Exception:
            return grids
        out = {}
        for c, o in grids.items():
            img = frames.images.get(c)
            if img is None:
                out[c] = o
                continue
            cam = self.cameras.get(c)
            try:
                ref = refine_grid(img, dict(o.uv), method=self.localize_method,
                                  K=cam.K if cam is not None else None,
                                  kc=cam.kc if cam is not None else None,
                                  include_off_lattice=self.refine_off_lattice)
                pts = {tuple(float(x) for x in k): np.asarray(v, float)
                       for k, v in ref.points.items()}
                if len(pts) < 20 or not np.isfinite(np.asarray(list(pts.values()))).all():
                    out[c] = o
                    continue
                out[c] = Observation(c, -1, pts)
            except Exception:
                out[c] = o
        return out

    def _rig(self):
        if self.result is None:
            return None
        return {"right": self.result.extrinsics["right"],
                "tex": self.result.extrinsics["tex"]}

    # -- main entry ---------------------------------------------------------
    def ingest(self, frames: FrameSet, grids: Optional[dict] = None) -> Decision:
        if grids is None:
            t0 = __import__("time").time()
            grids = self._detect(frames)
            self.detect_seconds += __import__("time").time() - t0
        # A camera that cannot see the board from this pose (the colour sensor
        # at an image corner, a sensor blinded by a reflection) must not veto a
        # view the other cameras see well: its observations simply do not
        # constrain this view.  Overall colour participation is a session-level
        # coverage requirement, not a per-view one.
        optional = set()
        counts = {c: (len(o.uv) if o is not None and getattr(o, "uv", None) else 0)
                  for c, o in (grids or {}).items()}
        if (counts.get("left", 0) >= 16 and counts.get("right", 0) >= 16
                and counts.get("tex", 0) == 0):
            optional.add("tex")
        q = guidance.evaluate_frame(frames.images, grids,
                                    timestamp=frames.timestamp,
                                    previous_pose=self.last_pose,
                                    previous_timestamp=self.last_timestamp,
                                    camera_timestamps=frames.camera_timestamps,
                                    quality=self.policy.get("quality"),
                                    cameras=self.cameras,
                                    rig=self._rig(),
                                    require_images=bool(self.policy.get("require_images", True)),
                                    optional_cameras=optional)
        if self.result is None:
            gain = None
            dec = self._bootstrap_decision(q)
        else:
            gain = self._predict_gain(q.pose)
            new_bin = self._new_bin(q.pose)
            near = self._nearest_pose(q.pose)
            dec = guidance.acceptance(q, gain, nearest_pose=near, pose=q.pose,
                                      new_coverage_bin=new_bin,
                                      policy={"min_rel_gain": self.policy["min_rel_gain"]})
        if not dec["accept"]:
            self.rejected.append({"timestamp": frames.timestamp, "reasons": dec["reasons"],
                                  "quality": q.as_dict()})
            self.last_pose, self.last_timestamp = q.pose, frames.timestamp
            return Decision(False, dec["reasons"], q.as_dict(), gain,
                            None, False, self.status())
        # two-stage localisation (detector report §5): the live loop tracks the
        # fast centroid-family detector, and the *committed dwell frame* gets
        # the accurate conic/auto refinement before it enters the calibration
        if self.localize_method and self.localize_method != "none" and frames.images:
            t0 = __import__("time").time()
            grids = self._refine(frames, grids)
            self.refine_seconds += __import__("time").time() - t0
        # hold-out check *before* adding: reprojection of this view under the
        # current calibration is a genuine held-out observation
        holdout = None
        if self.result is not None and q.pose is not None:
            holdout = self._holdout_rms(grids)
        vid = self._next_view
        self._next_view += 1
        for c, o in grids.items():
            self.obs.append(Observation(c, vid, dict(o.uv)))
        self.accepted.append(AcceptedView(
            vid, q.pose if q.pose is not None else (np.eye(3), np.zeros(3)),
            frames.timestamp, q.as_dict(), float(gain or 0.0), holdout,
            q.n_markers, frames.label))
        self.last_pose, self.last_timestamp = q.pose, frames.timestamp
        solved = self._maybe_solve()
        return Decision(True, [], q.as_dict(), gain, vid, solved, self.status())

    def _bootstrap_decision(self, q: guidance.FrameQuality) -> dict:
        reasons = list(q.reasons)
        if q.pose is None:
            return {"accept": False, "reasons": reasons + ["no pose"]}
        need = int(self.policy["bootstrap_views"])
        if len(self.accepted) < need:
            # Bootstrap must never deadlock.  A real operator starts with the
            # plate held at one pose and a continuous capture creeps forward, so
            # requiring the bootstrap views to be *diverse* (the old 8 deg /
            # 40 mm rule) can block the first solve forever: observed on the
            # live sweep as 2 accepted views and zero solves.  Reject only a
            # near-duplicate of an already accepted view, using the same
            # separation rule as the ordinary acceptance path; any genuine
            # motion eventually passes it, so the phase always terminates.
            nearest = self._nearest_pose(q.pose) if self.accepted else None
            if nearest is not None:
                R0, t0 = nearest
                ang = np.degrees(np.arccos(np.clip(
                    (np.trace(np.asarray(q.pose[0]) @ np.asarray(R0).T) - 1) / 2,
                    -1, 1)))
                dist = abs(float(q.pose[1][2] - t0[2]))
                if ang < 1.5 and dist < 8.0:
                    reasons.append(
                        f"too similar to an accepted view "
                        f"(bootstrap duplicate, {ang:.1f} deg, {dist:.1f} mm)")
            return {"accept": not reasons, "reasons": reasons}
        return {"accept": False, "reasons": reasons + ["bootstrap complete"]}

    def _bootstrap_pose(self):
        """Pick the candidate farthest (in pose) from the accepted bootstrap set."""
        from .adaptive import ViewCandidate
        if not self.accepted:
            for c in self.pool:
                if abs(c.distance_mm - 400) < 1e-6 and c.tilt_x_deg == 0 and c.tilt_y_deg == 0:
                    return c
            return self.pool[0]
        # choose the *easiest* pose that is still diverse: the operator should
        # not be asked to jump to the far end of the pool for the second view
        cur = self.accepted[-1].pose
        # coverage-first bootstrap (research review §10): fill distance and
        # tilt-sign bins before optimising information, then take the easiest
        d_edges = (220.0, 300.0, 380.0, 460.0, 540.0, 620.0)

        def dbin(d):
            return int(np.clip(np.searchsorted(d_edges, d) - 1, 0, len(d_edges) - 2))

        def tilts(pose):
            n = np.asarray(pose[0])[:, 2]
            return (float(np.degrees(np.arctan2(n[0], -n[2]))),
                    float(np.degrees(np.arctan2(n[1], -n[2]))))

        occ_d = {dbin(float(np.linalg.norm(a.pose[1]))) for a in self.accepted}
        occ_t = set()
        for a in self.accepted:
            tx, ty = tilts(a.pose)
            occ_t.add((0 if abs(tx) < 5 else (1 if tx > 0 else -1),
                       0 if abs(ty) < 5 else (1 if ty > 0 else -1)))
        candidates, relaxed = [], []
        for c in self.pool:
            # diversity against *every* accepted view, not just the last one
            dmin = 1e9
            for a in self.accepted:
                R0, t0 = a.pose
                ang = np.degrees(np.arccos(np.clip(
                    (np.trace(np.asarray(c.R) @ np.asarray(R0).T) - 1) / 2, -1, 1)))
                du = abs(float(c.t[2] - t0[2]))
                dmin = min(dmin, ang / 8.0 + du / 40.0)
            eff = objective.pose_effort(cur[0], cur[1], c)
            new_d = int(dbin(c.distance_mm) not in occ_d)
            new_t = int((0 if abs(c.tilt_x_deg) < 5 else (1 if c.tilt_x_deg > 0 else -1),
                         0 if abs(c.tilt_y_deg) < 5 else (1 if c.tilt_y_deg > 0 else -1))
                        not in occ_t)
            relaxed.append((dmin, eff, c))
            if dmin >= 0.6:
                candidates.append((-(new_d * 2 + new_t), eff, c))
        if candidates:
            candidates.sort(key=lambda z: (z[0], z[1]))
            return candidates[0][2]
        # no fully diverse pose left: take the most distinct, then the easiest
        relaxed.sort(key=lambda z: (-z[0], z[1]))
        return relaxed[0][2]

    def _is_new(self, cand, min_deg: float = 3.0, min_mm: float = 12.0) -> bool:
        for a in self.accepted:
            R0, t0 = a.pose
            ang = np.degrees(np.arccos(np.clip(
                (np.trace(np.asarray(cand.R) @ np.asarray(R0).T) - 1) / 2, -1, 1)))
            if ang < min_deg and abs(float(cand.t[2] - t0[2])) < min_mm:
                return False
        return True

    def _nearest_pose(self, pose):
        if pose is None or not self.accepted:
            return None
        best, bd = None, 1e18
        for a in self.accepted:
            d = float(np.linalg.norm(np.asarray(a.pose[1]) - np.asarray(pose[1])))
            if d < bd:
                best, bd = a.pose, d
        return best

    def _new_bin(self, pose) -> bool:
        if pose is None:
            return False
        if not self.accepted:
            return True
        poses = [a.pose for a in self.accepted] + [pose]
        cov = guidance.coverage_bins(poses)
        # a new bin is one that was empty before this pose
        prev = guidance.coverage_bins([a.pose for a in self.accepted])
        return cov["occupied"] > prev["occupied"]

    def _predict_gain(self, pose) -> Optional[float]:
        if pose is None or self.result is None:
            return None
        cand = _CandidateFromPose(pose)
        try:
            s = objective.score_candidate(self.result, cand, self.points,
                                          "A", effort=0.0, cache=self._task_cache)
        except Exception:
            return None
        return None if s is None else float(s.gain / max(s.objective_before, 1e-30))

    def _holdout_rms(self, grids: dict[str, Observation]) -> Optional[float]:
        """RMS of this view's observed pixels under the current calibration."""
        if self.result is None or "left" not in grids:
            return None
        uv_idx = [(float(u), float(v)) for (u, v) in plate.lattice_uv()]
        obj = plate.board_points_mm(self.pitch_mm)
        o = grids["left"]
        keys = [k for k in uv_idx if k in o.uv]
        if len(keys) < 6:
            return None
        from .geometry import solve_pnp, project
        cam = self.cameras["left"]
        pts3 = np.asarray([obj[uv_idx.index(k)] for k in keys])
        pts2 = np.asarray([o.uv[k] for k in keys], float)
        p = solve_pnp(pts3, pts2, cam.K, cam.kc)
        if p is None:
            return None
        Xc = pts3 @ p.R.T + p.t
        pred = project(cam.K, cam.kc, Xc)
        return float(np.sqrt(np.mean(np.sum((pred - pts2) ** 2, axis=1))))

    # -- solving ------------------------------------------------------------
    def _maybe_solve(self) -> bool:
        n = len(self.accepted)
        every = int(self.policy.get("solve_every", 1))
        if n == 0:
            return False
        # a 1- or 2-view problem cannot identify intrinsics; wait for the
        # bootstrap minimum before the first solve
        if self.result is None and n < max(3, int(self.policy["bootstrap_views"])):
            return False
        if n % every != 0 and n != int(self.policy["bootstrap_views"]):
            return False
        kc_mode = self.policy["free_kc"]
        free_kc = kc_mode if n >= int(self.policy["kc_views"]) else False
        t0 = __import__("time").time()
        try:
            res = self.solve_fn(self.obs, self.pitch_mm, self.cameras, free_kc,
                                self.policy["robust_loss"], self.policy["f_scale"])
        except TypeError:
            res = calibrate(self.obs, self.pitch_mm, self.cameras,
                            free_kc=free_kc, robust_loss=self.policy["robust_loss"],
                            f_scale=self.policy["f_scale"], free_intrinsics=True)
        except Exception as exc:  # keep the session alive and explain
            self.last_solve_error = f"{type(exc).__name__}: {exc}"
            return False
        self.solve_seconds += __import__("time").time() - t0
        self._solves += 1
        prev = self.result
        self.result = res
        self._last_result = prev
        self.cameras = dict(res.cameras)
        self._task_cache = {}
        try:
            rep = objective.evaluate_task(res, "A", self.points, cache=self._task_cache)
            self._task_history.append(float(rep.value))
            self._sigma_history.append(float(np.median(rep.sigma_mm)))
        except Exception:
            pass
        return True

    def solve_now(self) -> bool:
        return self._maybe_solve()

    # -- guidance -----------------------------------------------------------
    def recommend(self, top: int = 3, effort_weight: float = 0.05) -> dict:
        if self.result is None:
            cand = self._bootstrap_pose()
            current = self.accepted[-1].pose if self.accepted else None
            ins = guidance.instruction(current, (cand.R, cand.t)) if current else {}
            return {"ready": True, "bootstrap": True, "target": {"label": cand.label},
                    "target_pose": (cand.R, cand.t),
                    "text": (("turn the plate to face the scanner; " if not current else "")
                             + ins.get("text", "")),
                    "instruction": ins, "alternatives": [],
                    "explanation": ["bootstrap: diverse views before the first solve"]}
        current = self.accepted[-1].pose if self.accepted else None
        pool = guidance.reachable(current, self.pool) if current is not None else self.pool
        pool = [c for c in pool if self._is_new(c)]
        if not pool:
            return {"ready": False, "reason": "no unused reachable informative pose",
                    "text": "you have covered the reachable poses; press finish",
                    "alternatives": []}
        cache = self._task_cache
        scored = []
        for c in pool:
            eff = objective.pose_effort(current[0], current[1], c) if current is not None else 0.0
            s = objective.score_candidate(self.result, c, self.points, "A",
                                          effort=eff, effort_weight=effort_weight,
                                          cache=cache)
            if s is not None:
                scored.append(s)
        if not scored:
            return {"ready": False, "reason": "no reachable informative pose",
                    "text": "hold still", "alternatives": []}
        # Lexicographic policy (evidence: an effort penalty inside the utility
        # cost ~0.03 mm at 25 views).  Keep every view within 10 % of the best
        # predicted gain and choose the *easiest* of those; effort only breaks
        # near-ties, it never trades away meaningful information.
        scored.sort(key=lambda s: s.gain, reverse=True)
        best_gain = scored[0].gain
        near = [s for s in scored if s.gain >= 0.9 * best_gain]
        near.sort(key=lambda s: s.effort)
        best = near[0]
        ins = guidance.instruction(current, (best.candidate.R, best.candidate.t)) if current else {}
        return {"ready": True, "target": best.as_dict(),
                "target_pose": (best.candidate.R, best.candidate.t),
                "text": ins.get("text", ""), "instruction": ins,
                "alternatives": [s.as_dict() for s in scored[1:top]],
                "explanation": self._explain(best), "effort_weight": effort_weight}

    def _explain(self, score) -> list:
        """Turn the predicted covariance change into operator language."""
        out = []
        if score.objective_before > 0:
            out.append(f"expected 3-D uncertainty reduction {100 * score.gain / score.objective_before:.1f}%")
        try:
            C0 = objective.rig_covariance_gn(self.result)
            C1 = score.cov_rig_after
            idx = np.where(objective.free_rig_mask(self.result))[0]
            sd0 = np.sqrt(np.maximum(np.diag(C0)[idx], 0))
            sd1 = np.sqrt(np.maximum(np.diag(C1)[idx], 0))
            rel = (sd0 - sd1) / np.maximum(sd0, 1e-12)
            names = _rig_names()
            blocks = {"intrinsics": rel[[i for i in range(len(idx)) if names[idx[i]].split(".")[0] in CAMERAS and names[idx[i]].split(".")[1] in ("fx", "fy", "cx", "cy")]],
                      "distortion": rel[[i for i in range(len(idx)) if names[idx[i]].split(".")[1] in ("k1", "k2", "p1", "p2", "k3")]],
                      "relative pose": rel[[i for i in range(len(idx)) if names[idx[i]].split(".")[1] in ("rx", "ry", "rz", "tx", "ty", "tz")]]}
            for k, v in blocks.items():
                if len(v) and np.mean(v) > 0.002:
                    out.append(f"tightens {k} by {100 * float(np.mean(v)):.2f}%")
        except Exception:
            pass
        if score.effort > 0:
            out.append(f"operator effort {score.effort:.2f} units")
        return out

    # -- status / stopping --------------------------------------------------
    def status(self) -> dict:
        st = {"mode": self.mode, "n_accepted": len(self.accepted),
              "n_rejected": len(self.rejected), "n_observations": len(self.obs),
              "solve_seconds": float(self.solve_seconds), "n_solves": self._solves,
              "accepted": [a.as_dict() for a in self.accepted],
              "last_rejection": self.rejected[-1] if self.rejected else None,
              "coverage": guidance.coverage(self.obs),
              "bins": guidance.coverage_bins([a.pose for a in self.accepted])}
        if self.result is not None:
            rep = objective.evaluate_task(self.result, "A", self.points, sigma_px=0.0,
                                          cache=self._task_cache)
            st["task"] = rep.as_dict()
            st["baseline_mm"] = float(np.linalg.norm(self.result.baseline()))
            st["rms_px"] = float(self.result.residual_rms)
        return st

    def stop_decision(self, require_heldout: bool = True) -> dict:
        if self.result is None:
            return {"stop": False, "reasons": ["no calibration yet"]}
        recent = [a.holdout_rms_px for a in self.accepted if a.holdout_rms_px is not None]
        heldout = float(np.median(recent[-4:])) if recent else None
        rep = stopping_report(self.result, self.obs, heldout_rms_px=heldout,
                              pitch=self.pitch_mm,
                              min_views=int(self.policy["stop_min_views"]),
                              require_heldout=require_heldout,
                              compute_stability=False, require_stability=False)
        # diminishing return: predicted marginal benefit in millimetres and the
        # measured improvement over the last solves
        rec = self.recommend(top=1)
        tgt = rec.get("target", {}) or {}
        best_gain = float(tgt.get("gain", 0.0) or 0.0)
        base = float(tgt.get("objective_before", 1.0) or 1.0)
        rel = best_gain / max(base, 1e-30)
        delta_sigma = float(tgt.get("delta_sigma_mm", 0.0) or 0.0)
        hist = self._task_history
        sh = self._sigma_history
        recent_gain = (hist[-4] - hist[-1]) / max(hist[-4], 1e-30) if len(hist) >= 4 else 1.0
        recent_sigma_drop = (sh[-4] - sh[-1]) if len(sh) >= 4 else float("inf")
        no_pose = not rec.get("ready", False)
        marginal = (no_pose
                    or (rel < float(self.policy["stop_min_gain"])
                        and delta_sigma < float(self.policy["stop_min_delta_sigma_mm"]))
                    or (0.0 <= recent_sigma_drop < float(self.policy["stop_min_recent_sigma_mm"])
                        and delta_sigma < 2.0 * float(self.policy["stop_min_delta_sigma_mm"])))
        ok = bool(rep["ok"]) and marginal
        return {"stop": ok, "checks": rep["checks"], "reasons_failed": rep["reasons_failed"],
                "heldout_rms_px": heldout, "best_relative_gain": rel,
                "predicted_delta_sigma_mm": delta_sigma,
                "recent_relative_gain": recent_gain,
                "recent_sigma_drop_mm": (None if recent_sigma_drop == float("inf")
                                         else float(recent_sigma_drop)),
                "gain_threshold": float(self.policy["stop_min_gain"]),
                "delta_sigma_threshold_mm": float(self.policy["stop_min_delta_sigma_mm"]),
                "recent_sigma_threshold_mm": float(self.policy["stop_min_recent_sigma_mm"]),
                "recent_gain_threshold": float(self.policy["stop_min_recent_gain"]),
                "n_views": len(self.accepted),
                "reason": ("meets quality; best remaining view changes the median "
                           f"3-D sigma by {delta_sigma * 1000:.1f} um "
                           f"(< {self.policy['stop_min_delta_sigma_mm'] * 1000:.0f} um)"
                           if ok else "not yet sufficient")}

    # -- result -------------------------------------------------------------
    def finish(self, profile_root: Optional[str] = None,
               serial: str = "unknown") -> dict:
        """Final solve; optionally persist a versioned host-side profile.

        The factory snapshot and every revision stay host-side; nothing is
        written to the scanner.
        """
        if self.result is None and self.obs:
            self.solve_now()
        if self.result is None:
            return {"ok": False, "reason": "no accepted views"}
        stop = self.stop_decision()
        from .pipeline import profile_from_result
        from .integration import result_summary
        prof = profile_from_result(self.result, factory=self.factory or None)
        out = {"ok": bool(stop["stop"]),
               "result": self.result, "summary": result_summary(self.result),
               "quality": stop, "profile": prof,
               "session": self.status()}
        if profile_root:
            try:
                from .profiles import ProfileStore
                store = ProfileStore(profile_root)
                rev = store.save_revision(
                    serial, prof,
                    meta={"mode": self.mode, "n_views": len(self.accepted),
                          "pitch_mm": self.pitch_mm,
                          "stopping_ok": bool(stop["stop"]),
                          "created_by": "calib.session",
                          "note": "host-side only; factory snapshot is immutable"},
                    covariance=self.result.covariance,
                    param_names=self.result.info.get("param_names"))
                out["revision"] = str(rev.root)
                out["profile_root"] = str(profile_root)
            except Exception as exc:
                out["profile_error"] = f"{type(exc).__name__}: {exc}"
        return out


def _default_solve(obs, pitch, cameras, free_kc, robust_loss, f_scale):
    try:
        from . import solvers
        if hasattr(solvers, "solve_joint"):
            out = solvers.solve_joint(obs, pitch_mm=pitch, init_cameras=cameras,
                                      free_kc=free_kc, robust=robust_loss,
                                      f_scale=f_scale, backend="lm")
            if out.get("result") is not None:
                return out["result"]
    except Exception:
        pass
    return calibrate(obs, pitch, cameras, free_kc=free_kc, robust_loss=robust_loss,
                     f_scale=f_scale, free_intrinsics=True)


class _CandidateFromPose:
    """Adapt a concrete (R,t) to the ViewCandidate interface."""

    def __init__(self, pose):
        self.R, self.t = pose
        self.distance_mm = float(np.linalg.norm(self.t))
        n = np.asarray(self.R)[:, 2]
        self.tilt_x_deg = float(np.degrees(np.arctan2(n[0], abs(n[2]))))
        self.tilt_y_deg = float(np.degrees(np.arctan2(n[1], abs(n[2]))))
        self.roll_deg = 0.0
        self.label = "current"


def _rig_names():
    names = []
    for c in CAMERAS:
        for f in ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"):
            names.append(f"{c}.{f}")
    for c in ("right", "tex"):
        for f in ("rx", "ry", "rz", "tx", "ty", "tz"):
            names.append(f"{c}.{f}")
    return names
