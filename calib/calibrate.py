"""Multi-camera calibration: initialisation, robust bundle adjustment, covariance.

Model
-----
Three cameras (left IR, right IR, tex RGB) observe one planar marker plate.
The plate points are known in board coordinates (``calib.plate``).  The
parameters are

    intrinsics[c] = fx, fy, cx, cy, k1, k2, p1, p2, k3        (c = L, R, T)
    extrinsics    = (rvec, t) for R and T in the left frame   (L is identity)
    views[v]      = (rvec, t) board -> left camera

The residual for an observation of board point X in camera c and view v is
the 2-D reprojection error.  The left camera is the gauge (identity pose);
the board pitch fixes the metric scale.  The solver is Ceres-like in spirit:
Levenberg-Marquardt (scipy ``trf``) with a Huber loss on pixel residuals.

The vendor's own solver (``Sn3D_E10_camera_calibration`` /
``FUN_1800cc330``) uses Ceres with quaternion parameterisation; this module
uses a minimal-axis-angle parameterisation and the same reprojection model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional
import math

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from . import plate
from .geometry import distort, project, project_world, rodrigues, rvec_from_R, solve_pnp

CAMERAS = ("left", "right", "tex")


@dataclass
class CameraIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    kc: np.ndarray
    skew: float = 0.0

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.fx, self.skew * self.fx, self.cx],
                         [0.0, self.fy, self.cy],
                         [0.0, 0.0, 1.0]])

    @classmethod
    def from_ccf(cls, rec) -> "CameraIntrinsics":
        K = rec.K()
        return cls(fx=float(K[0, 0]), fy=float(K[1, 1]),
                   cx=float(K[0, 2]), cy=float(K[1, 2]), kc=rec.dist(),
                   skew=float(K[0, 1] / K[0, 0]) if K[0, 0] else 0.0)

    def as_vec(self) -> np.ndarray:
        return np.array([self.fx, self.fy, self.cx, self.cy, *self.kc], float)

    @classmethod
    def from_vec(cls, v: np.ndarray, skew: float = 0.0) -> "CameraIntrinsics":
        return cls(fx=float(v[0]), fy=float(v[1]), cx=float(v[2]), cy=float(v[3]),
                   kc=np.asarray(v[4:9], float), skew=skew)

    def as_dict(self) -> dict:
        return {"fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy,
                "skew": self.skew, "kc": [float(x) for x in self.kc]}


@dataclass
class Observation:
    camera: str
    view: int
    uv: dict[tuple[float, float], np.ndarray]


@dataclass
class CalibrationResult:
    cameras: dict[str, CameraIntrinsics]
    extrinsics: dict[str, tuple[np.ndarray, np.ndarray]]
    views: list[tuple[np.ndarray, np.ndarray]]
    covariance: Optional[np.ndarray]
    residual_rms: float
    residual_max: float
    n_observations: int
    n_parameters: int
    robust_loss: str = "huber"
    info: dict = field(default_factory=dict)

    def baseline(self, a: str = "left", b: str = "right") -> np.ndarray:
        Ra, ta = self.extrinsics[a]
        Rb, tb = self.extrinsics[b]
        R = Rb @ Ra.T
        t = tb - R @ ta
        return t

    def camera_centres(self) -> dict[str, np.ndarray]:
        out = {}
        for c, (R, t) in self.extrinsics.items():
            out[c] = -R.T @ t
        return out

    def param_std(self) -> dict[str, float]:
        if self.covariance is None:
            return {}
        sd = np.sqrt(np.maximum(np.diag(self.covariance), 0.0))
        names = _param_names(len(self.views))
        return {n: float(s) for n, s in zip(names, sd)}

    def rebased(self, view: int = 0) -> "CalibrationResult":
        """Express every camera in the board frame of ``view``.

        A CCF record stores a world->camera pose; EXStar's world frame is the
        calibration board at a reference capture (its T is ~the working
        distance).  Our internal gauge is the left camera (left = identity).
        This is a pure gauge transform: relative camera poses are unchanged.
        """
        Rv, tv = self.views[view]
        extr = {}
        for c, (R, t) in self.extrinsics.items():
            extr[c] = (R @ Rv, R @ tv + t)
        views = []
        for i, (R, t) in enumerate(self.views):
            Ri = Rv.T @ R
            ti = Rv.T @ (t - tv)
            views.append((Ri, ti))
        return CalibrationResult(self.cameras, extr, views, self.covariance,
                                 self.residual_rms, self.residual_max,
                                 self.n_observations, self.n_parameters,
                                 self.robust_loss, dict(self.info, rebased_view=view))

    def rescaled(self, factor: float) -> "CalibrationResult":
        """Scale every translation (rig and views) by ``factor``.

        Intrinsics, rotations and residuals are unchanged: a planar-target
        calibration determines translation only up to the target's metric
        scale.  Use this to anchor the result to a known baseline.
        """
        extr = {c: (R, t * factor) for c, (R, t) in self.extrinsics.items()}
        views = [(R, t * factor) for R, t in self.views]
        cov = None if self.covariance is None else self.covariance * (factor ** 2)
        return CalibrationResult(self.cameras, extr, views, cov, self.residual_rms,
                                 self.residual_max, self.n_observations,
                                 self.n_parameters, self.robust_loss,
                                 dict(self.info, scale_factor=factor))

    def as_dict(self) -> dict:
        return {
            "cameras": {c: i.as_dict() for c, i in self.cameras.items()},
            "extrinsics": {c: {"R": R.tolist(), "t": t.tolist()}
                           for c, (R, t) in self.extrinsics.items()},
            "views": [{"R": R.tolist(), "t": t.tolist()} for R, t in self.views],
            "residual_rms_px": self.residual_rms,
            "residual_max_px": self.residual_max,
            "n_observations": self.n_observations,
            "n_parameters": self.n_parameters,
            "baseline_mm": float(np.linalg.norm(self.baseline())),
            "param_std": self.param_std(),
            "info": self.info,
        }


# --------------------------------------------------------------------------
# parameter packing
# --------------------------------------------------------------------------

def _param_names(n_views: int) -> list[str]:
    names = []
    for c in CAMERAS:
        for f in ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3"):
            names.append(f"{c}.{f}")
    for c in ("right", "tex"):
        for f in ("rx", "ry", "rz", "tx", "ty", "tz"):
            names.append(f"{c}.{f}")
    for v in range(n_views):
        for f in ("rx", "ry", "rz", "tx", "ty", "tz"):
            names.append(f"view{v}.{f}")
    return names


class _Packer:
    def __init__(self, n_views: int, free_kc: bool = True):
        self.n_views = n_views
        self.free_kc = free_kc
        self.slices = {}
        i = 0
        for c in CAMERAS:
            self.slices[c] = slice(i, i + 9)
            i += 9
        self.extr = {}
        for c in ("right", "tex"):
            self.extr[c] = slice(i, i + 6)
            i += 6
        self.views = slice(i, i + 6 * n_views)
        i += 6 * n_views
        self.n = i

    def names(self) -> list[str]:
        return _param_names(self.n_views)


# --------------------------------------------------------------------------
# initial solution
# --------------------------------------------------------------------------

def _initial_intrinsics(obs: list[Observation], init: dict[str, CameraIntrinsics],
                        obj: np.ndarray, free_kc: bool) -> dict[str, CameraIntrinsics]:
    """Per-camera OpenCV calibration from the observations (factory K as guess)."""
    if not free_kc:
        return {c: init[c] for c in CAMERAS}
    out = {}
    for c in CAMERAS:
        views = [o for o in obs if o.camera == c]
        if len(views) < 3:
            out[c] = init[c]
            continue
        op, ip = [], []
        for o in sorted(views, key=lambda z: z.view):
            idx = [k for k in o.uv if k in _UV_INDEX]
            op.append(obj[[_UV_INDEX[k] for k in idx]])
            ip.append(np.array([o.uv[k] for k in idx], np.float32))
        K0 = init[c].K.copy()
        K0[0, 1] = 0.0
        D0 = init[c].kc.copy()
        flags = cv2_flags(free_kc)
        try:
            rms, K, D, rv, tv = cv2.calibrateCamera(op, ip, (1280, 1024), K0, D0, flags=flags)
            skew = float(init[c].skew)
            out[c] = CameraIntrinsics(fx=float(K[0, 0]), fy=float(K[1, 1]),
                                      cx=float(K[0, 2]), cy=float(K[1, 2]),
                                      kc=np.asarray(D, float).ravel()[:5], skew=skew)
        except cv2.error:
            out[c] = init[c]
    return out


def cv2_flags(free_kc: bool):
    import cv2
    flags = cv2.CALIB_USE_INTRINSIC_GUESS
    if not free_kc:
        flags |= cv2.CALIB_FIX_K1 | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3 | cv2.CALIB_ZERO_TANGENT_DIST
    return flags


_UV_INDEX = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}


def _initial_poses(obs: list[Observation], cams: dict[str, CameraIntrinsics],
                   obj: np.ndarray) -> tuple[list[tuple], dict[str, tuple]]:
    """Per-camera PnP poses, then rig extrinsics averaged across views.

    Views without a left observation are initialised from the right (or tex)
    pose and the averaged rig, not from a placeholder.
    """
    n_views = 1 + max((o.view for o in obs), default=0)
    per_cam = {c: {} for c in CAMERAS}
    for o in obs:
        keys = [k for k in o.uv if k in _UV_INDEX]
        if len(keys) < 6:
            continue
        idx = [_UV_INDEX[k] for k in keys]
        img = np.array([o.uv[k] for k in keys], np.float32)
        pose = solve_pnp(obj[idx], img, cams[o.camera].K, cams[o.camera].kc)
        if pose is not None:
            per_cam[o.camera][o.view] = pose

    def rig_from(c: str):
        Rs, ts = [], []
        for v, pc in per_cam[c].items():
            pl = per_cam["left"].get(v)
            if pl is None:
                continue
            Rcl = pc.R @ pl.R.T
            tcl = pc.t - Rcl @ pl.t
            Rs.append(Rcl); ts.append(tcl)
        if not Rs:
            return np.eye(3), np.zeros(3)
        A = np.mean(Rs, axis=0)
        U, _, Vt = np.linalg.svd(A)
        R = U @ np.diag([1.0, 1.0, np.linalg.det(U @ Vt)]) @ Vt
        return R, np.mean(ts, axis=0)

    extr = {"left": (np.eye(3), np.zeros(3))}
    for c in ("right", "tex"):
        extr[c] = rig_from(c)

    views = []
    for v in range(n_views):
        p = per_cam["left"].get(v)
        if p is not None:
            views.append((p.R, p.t))
            continue
        placed = False
        for c in ("right", "tex"):
            pc = per_cam[c].get(v)
            if pc is None:
                continue
            Rcl, tcl = extr[c]
            R = Rcl.T @ pc.R
            t = Rcl.T @ (pc.t - tcl)
            views.append((R, t))
            placed = True
            break
        if not placed:
            views.append((np.eye(3), np.array([0.0, 0.0, 400.0])))
    return views, extr


# --------------------------------------------------------------------------
# residuals
# --------------------------------------------------------------------------

class _Problem:
    def __init__(self, obs: list[Observation], obj: np.ndarray, n_views: int,
                 free_kc: bool, robust_loss: str, f_scale: float,
                 free_intrinsics: bool = True):
        self.obs = obs
        self.obj = obj
        self.n_views = n_views
        self.packer = _Packer(n_views, free_kc)
        self.free_kc = free_kc
        self.free_intrinsics = free_intrinsics
        mask = np.ones(self.packer.n, bool)
        for c in CAMERAS:
            sl = self.packer.slices[c]
            if not free_intrinsics:
                mask[sl.start:sl.start + 4] = False
            if not free_kc:
                mask[sl.start + 4:sl.stop] = False
        self.free_mask = mask
        self.free_idx = np.where(mask)[0]
        self.robust_loss = robust_loss
        self.f_scale = f_scale
        self.rows = []
        self._build_rows()

    def _build_rows(self):
        for oi, o in enumerate(self.obs):
            idx = [_UV_INDEX[k] for k in o.uv if k in _UV_INDEX]
            if not idx:
                continue
            uv = np.array([o.uv[tuple(plate.lattice_uv()[i])] for i in idx], float)
            self.rows.append((o.camera, o.view, np.array(idx, int), uv))

    def unpack(self, x: np.ndarray):
        p = self.packer
        cams = {}
        for c in CAMERAS:
            v = x[p.slices[c]]
            cams[c] = CameraIntrinsics(fx=v[0], fy=v[1], cx=v[2], cy=v[3],
                                       kc=v[4:9].copy())
        extr = {"left": (np.eye(3), np.zeros(3))}
        for c in ("right", "tex"):
            v = x[p.extr[c]]
            extr[c] = (rodrigues(v[:3]), v[3:6].copy())
        views = []
        for i in range(self.n_views):
            v = x[p.views.start + 6 * i: p.views.start + 6 * (i + 1)]
            views.append((rodrigues(v[:3]), v[3:6].copy()))
        return cams, extr, views

    def residuals(self, x: np.ndarray) -> np.ndarray:
        cams, extr, views = self.unpack(x)
        out = []
        for cam, view, idx, uv in self.rows:
            X = self.obj[idx]
            Rv, tv = views[view]
            Xl = X @ Rv.T + tv
            Rc, tc = extr[cam]
            Xc = Xl @ Rc.T + tc
            z = Xc[:, 2]
            if np.any(z <= 1e-6):
                z = np.maximum(z, 1e-6)
            K = cams[cam].K
            pred = project(K, cams[cam].kc, Xc)
            out.append((pred - uv).ravel())
        return np.concatenate(out) if out else np.zeros(0)

    def pack(self, xf: np.ndarray, x0: np.ndarray) -> np.ndarray:
        x = x0.copy()
        x[self.free_idx] = xf
        return x

    def residuals_free(self, xf: np.ndarray, x0: np.ndarray) -> np.ndarray:
        return self.residuals(self.pack(xf, x0))

    def jac_sparsity_free(self, S: lil_matrix) -> lil_matrix:
        return S[:, self.free_idx]

    def jac_sparsity(self) -> lil_matrix:
        m = sum(2 * len(r[2]) for r in self.rows)
        S = lil_matrix((m, self.packer.n), dtype=int)
        row = 0
        for cam, view, idx, uv in self.rows:
            n = 2 * len(idx)
            S[row:row + n, self.packer.slices[cam]] = 1
            if cam != "left":
                S[row:row + n, self.packer.extr[cam]] = 1
            S[row:row + n, self.packer.views.start + 6 * view:
              self.packer.views.start + 6 * (view + 1)] = 1
            row += n
        return S


def _pack_initial(cams, extr, views, packer: _Packer) -> np.ndarray:
    x = np.zeros(packer.n)
    for c in CAMERAS:
        x[packer.slices[c]] = cams[c].as_vec()
    for c in ("right", "tex"):
        R, t = extr[c]
        x[packer.extr[c]] = np.concatenate([rvec_from_R(R), t])
    for i, (R, t) in enumerate(views):
        x[packer.views.start + 6 * i: packer.views.start + 6 * (i + 1)] = np.concatenate([rvec_from_R(R), t])
    return x


# --------------------------------------------------------------------------
# main API
# --------------------------------------------------------------------------

def calibrate(obs: list[Observation],
              board_pitch_mm: float = plate.DEFAULT_PITCH_MM,
              init_cameras: Optional[dict[str, CameraIntrinsics]] = None,
              free_kc: bool = True,
              robust_loss: str = "huber",
              f_scale: float = 1.0,
              max_nfev: int = 1200,
              free_intrinsics: bool = True,
              fix_left_intrinsics: bool = False) -> CalibrationResult:
    """Calibrate the three cameras from grid observations.

    ``obs`` needs one :class:`Observation` per (camera, view).  Missing cameras
    or views are allowed; the solver uses whatever is present, but the result
    is only meaningful when all three cameras see several common views.
    """
    obj = plate.board_points_mm(board_pitch_mm)
    n_views = 1 + max((o.view for o in obs), default=0)
    if init_cameras is None:
        init_cameras = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
                        for c in CAMERAS}
    cams0 = _initial_intrinsics(obs, init_cameras, obj, free_kc)
    views0, extr0 = _initial_poses(obs, cams0, obj)
    packer = _Packer(n_views, free_kc)
    x0 = _pack_initial(cams0, extr0, views0, packer)
    prob = _Problem(obs, obj, n_views, free_kc, robust_loss, f_scale, free_intrinsics)
    xf0 = x0[prob.free_idx]
    # Dense 2-point Jacobian: the sparsity-grouped Jacobian silently produced a
    # wrong step for this problem, so correctness wins over the small speed gain.
    res = least_squares(lambda xf: prob.residuals_free(xf, x0), xf0, method="trf",
                        loss=robust_loss, f_scale=f_scale,
                        max_nfev=max_nfev, xtol=1e-10, ftol=1e-10, gtol=1e-10, verbose=0)
    x = prob.pack(res.x, x0)
    cams, extr, views = prob.unpack(x)
    r = prob.residuals(x)
    # per-point norms
    norms = np.linalg.norm(r.reshape(-1, 2), axis=1)
    rms = float(np.sqrt(np.mean(norms ** 2))) if len(norms) else float("nan")
    # res.fun is the robustified residual; the covariance needs the raw one.
    cov = covariance(prob, x, r, free_intrinsics=free_intrinsics, free_kc=free_kc)
    return CalibrationResult(cams, extr, views, cov, rms,
                             float(np.max(norms)) if len(norms) else float("nan"),
                             int(len(norms)), packer.n, robust_loss,
                             info={"success": bool(res.success), "nfev": int(res.nfev),
                                   "cost": float(res.cost), "optimality": float(res.optimality),
                                   "message": str(res.message), "pitch_mm": board_pitch_mm,
                                   "free_kc": free_kc,
                                   "views_used": sorted(int(v) for v in set(o.view for o in obs))})


def _numeric_jacobian(prob: _Problem, x: np.ndarray, f0: np.ndarray,
                      eps: float = 1e-7) -> np.ndarray:
    idx = prob.free_idx
    J = np.zeros((len(f0), len(idx)))
    xb = x.copy()
    for jj, j in enumerate(idx):
        h = eps * max(1.0, abs(x[j]))
        xp = xb.copy(); xp[j] += h
        J[:, jj] = (prob.residuals(xp) - f0) / h
    return J


def covariance(prob: _Problem, x: np.ndarray, f: np.ndarray,
               sigma: Optional[float] = None,
               free_intrinsics: bool = True, free_kc: bool = True) -> Optional[np.ndarray]:
    """Parameter covariance from the Gauss-Newton approximation."""
    try:
        J = _numeric_jacobian(prob, x, f)
    except Exception:
        return None
    m, n = J.shape
    if m <= n:
        return None
    H = J.T @ J
    # robust scale: use the median absolute residual if not supplied
    if sigma is None:
        # Per-COMPONENT robust scale.  Using the norm of 2-D residuals and the
        # 0.6745 Gaussian factor underestimates sigma by ~20 %, which makes the
        # reported covariance over-confident; the component-wise MAD is the
        # right estimator here.
        comps = np.asarray(f, float).ravel()
        sigma = float(1.4826 * np.median(np.abs(comps - np.median(comps)))) if len(comps) else 1.0
        sigma = max(sigma, 1e-3)
    try:
        cov = np.linalg.pinv(H, rcond=1e-12) * (sigma ** 2)
    except np.linalg.LinAlgError:
        return None
    full = np.zeros((prob.packer.n, prob.packer.n))
    ix = np.ix_(prob.free_idx, prob.free_idx)
    full[ix] = cov
    return full
