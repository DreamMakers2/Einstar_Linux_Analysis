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
NRIG = 39  # 27 intrinsics + 12 extrinsics


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
    information: Optional[np.ndarray] = None
    marginal_information: Optional[np.ndarray] = None

    def view_pose(self, original_view: int):
        """Return the pose of an original view id (handles internal remapping)."""
        remap = self.info.get("view_remap", {})
        idx = remap.get(int(original_view), int(original_view))
        return self.views[idx]

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
        """Marginal standard deviations in the packed parameter order.

        The covariance is the joint covariance over all free parameters,
        including the nuisance board poses.  The marginal standard deviation of
        a rig parameter is sqrt(diag(C)[i]); conditioning on the poses instead
        (the old behaviour) underestimates it and is not what stopping or view
        selection must use.
        """
        if self.covariance is None:
            return {}
        sd = np.sqrt(np.maximum(np.diag(self.covariance), 0.0))
        names = self.info.get("param_names") or _param_names(len(self.views))
        return {n: float(s) for n, s in zip(names, sd)}

    def _packer(self):
        return _Packer(len(self.views), self.info.get("free_kc", True))

    def vector(self) -> np.ndarray:
        """Pack the full parameter vector in the canonical order."""
        pk = self._packer()
        x = np.zeros(pk.n)
        for c in CAMERAS:
            x[pk.slices[c]] = self.cameras[c].as_vec()
        for c in ("right", "tex"):
            R, t = self.extrinsics[c]
            x[pk.extr[c]] = np.concatenate([rvec_from_R(R), t])
        for i, (R, t) in enumerate(self.views):
            x[pk.views.start + 6 * i: pk.views.start + 6 * (i + 1)] = np.concatenate(
                [rvec_from_R(R), t])
        return x

    @classmethod
    def from_vector(cls, x: np.ndarray, like: "CalibrationResult") -> "CalibrationResult":
        pk = like._packer()
        cams = {}
        for i, c in enumerate(CAMERAS):
            v = x[pk.slices[c]]
            cams[c] = CameraIntrinsics(float(v[0]), float(v[1]), float(v[2]),
                                       float(v[3]), v[4:9].copy())
        extr = {"left": (np.eye(3), np.zeros(3))}
        for c in ("right", "tex"):
            v = x[pk.extr[c]]
            extr[c] = (rodrigues(v[:3]), v[3:6].copy())
        views = []
        for i in range(len(like.views)):
            v = x[pk.views.start + 6 * i: pk.views.start + 6 * (i + 1)]
            views.append((rodrigues(v[:3]), v[3:6].copy()))
        return CalibrationResult(cams, extr, views, like.covariance,
                                 like.residual_rms, like.residual_max,
                                 like.n_observations, like.n_parameters,
                                 like.robust_loss, dict(like.info))

    def transform_covariance(self, J: np.ndarray, extra_var: float = 0.0,
                             extra_jac: Optional[np.ndarray] = None) -> np.ndarray:
        """Covariance under x' = f(x): C' = J C J^T (+ anchor term)."""
        if self.covariance is None:
            return None
        C = J @ self.covariance @ J.T
        if extra_var and extra_jac is not None:
            C = C + float(extra_var) * np.outer(extra_jac, extra_jac)
        return C

    def _numeric_transform_jacobian(self, fn, x0: np.ndarray,
                                    eps: float = 1e-6) -> np.ndarray:
        f0 = fn(x0)
        J = np.zeros((len(x0), len(x0)))
        for j in range(len(x0)):
            h = eps * max(1.0, abs(x0[j]))
            xp = x0.copy(); xp[j] += h
            J[:, j] = (fn(xp) - f0) / h
        return J

    def rebased(self, view: int = 0) -> "CalibrationResult":
        """Express every camera in the board frame of ``view``.

        A CCF record stores a world->camera pose; EXStar's world frame is the
        calibration board at a reference capture (its T is ~the working
        distance).  Our internal gauge is the left camera (left = identity).

        The rebase is a gauge transform: relative poses and their uncertainties
        are invariant, but the absolute extrinsic parameterisation changes and
        the left camera is no longer the identity.  The stored full covariance
        is therefore not a valid absolute covariance in the new gauge.  We keep
        the invariant relative sigmas in ``info`` and set ``covariance=None``
        rather than publishing a silently wrong matrix.
        """
        from .quality import baseline_sigma, relative_pose_sigma, rgb_alignment
        info = dict(self.info, rebased_view=view, reference_view=int(view),
                    covariance_note="rebased gauge: absolute covariance is not represented; "
                                     "relative sigmas preserved in info")
        try:
            info["baseline_sigma_mm"] = baseline_sigma(self)
            info["relative_sigma"] = {
                "ir": relative_pose_sigma(self, "left", "right"),
                "rgb": relative_pose_sigma(self, "left", "tex"),
                "rgb_alignment": rgb_alignment(self),
            }
        except Exception:
            pass
        Rv, tv = self.view_pose(view)
        extr = {c: (R @ Rv, R @ tv + t) for c, (R, t) in self.extrinsics.items()}
        views = [(Rv.T @ R, Rv.T @ (t - tv)) for R, t in self.views]
        return CalibrationResult(self.cameras, extr, views, None,
                                 self.residual_rms, self.residual_max,
                                 self.n_observations, self.n_parameters,
                                 self.robust_loss, info)

    def rescaled(self, factor: Optional[float] = None,
                 target_baseline: Optional[float] = None,
                 anchor_sigma_mm: float = 0.0,
                 factor_sigma: float = 0.0) -> "CalibrationResult":
        """Scale every translation, with the full covariance Jacobian.

        Only translation parameters are scaled; intrinsics, distortion and
        rotations are unchanged.  If ``target_baseline`` is given, the scale is
        estimated from the measured baseline, and the anchor uncertainty plus
        the scale/translation cross-covariance are propagated.  The old
        ``C * factor**2`` was wrong for every non-translation parameter.
        """
        b = float(np.linalg.norm(self.baseline()))
        s = float(factor) if factor is not None else float(target_baseline) / b
        x0 = self.vector()
        pk = self._packer()
        t_idx = []
        for c in ("right", "tex"):
            t_idx.extend(range(pk.extr[c].start + 3, pk.extr[c].start + 6))
        for i in range(len(self.views)):
            t_idx.extend(range(pk.views.start + 6 * i + 3, pk.views.start + 6 * i + 6))
        t_idx = np.array(t_idx, int)

        def fn(x):
            y = x.copy()
            y[t_idx] *= s
            return y

        J = self._numeric_transform_jacobian(fn, x0)
        # independent scale uncertainty (e.g. plate pitch) -> d x_t / d s = x_t/s
        extra_var = 0.0; extra_jac = None
        if factor_sigma:
            extra_var = float(factor_sigma) ** 2
            extra_jac = np.zeros(len(x0)); extra_jac[t_idx] = x0[t_idx]
        # anchor baseline: s = b0 / b(x), b0 may itself be uncertain
        if target_baseline is not None:
            jb = np.zeros(len(x0)); jb[t_idx] = x0[t_idx] / max(b, 1e-12)

            def fn_anchor(x):
                bb = float(np.linalg.norm(
                    CalibrationResult.from_vector(x, self).baseline()))
                y = x.copy(); y[t_idx] *= float(target_baseline) / bb
                return y
            J = self._numeric_transform_jacobian(fn_anchor, x0)
            extra_var = float(anchor_sigma_mm) ** 2
            extra_jac = jb if anchor_sigma_mm else None
        C = self.transform_covariance(J, extra_var, extra_jac)
        extr = {c: (R, t * s) for c, (R, t) in self.extrinsics.items()}
        views = [(R, t * s) for R, t in self.views]
        pitch0 = float(self.info.get("pitch_mm", plate.DEFAULT_PITCH_MM))
        return CalibrationResult(self.cameras, extr, views, C, self.residual_rms,
                                 self.residual_max, self.n_observations,
                                 self.n_parameters, self.robust_loss,
                                 dict(self.info, scale_factor=s,
                                      pitch_mm=pitch0 * s,
                                      scale_anchor_sigma_mm=anchor_sigma_mm,
                                      factor_sigma=factor_sigma))

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
            # OpenCV 5 requires float32 object points; float64 silently
            # raised and the except-fallback returned the factory K, turning
            # the per-camera initialisation into a no-op.
            op.append(obj[[_UV_INDEX[k] for k in idx]].astype(np.float32))
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
              fix_left_intrinsics: bool = False,
              robust_covariance: bool = True) -> CalibrationResult:
    """Calibrate the three cameras from grid observations.

    Views are renumbered internally to a compact range, so unused slots never
    enter the parameter vector or the covariance.  The returned covariance is
    the joint covariance over the free parameters; its top-left 39x39 block is
    the *marginal* rig covariance (board poses are nuisance parameters).
    """
    if not obs:
        raise ValueError("no observations")
    used = sorted(set(int(o.view) for o in obs))
    remap = {v: i for i, v in enumerate(used)}
    obs = [Observation(o.camera, remap[int(o.view)], o.uv) for o in obs]
    n_views = len(used)
    obj = plate.board_points_mm(board_pitch_mm)
    if init_cameras is None:
        init_cameras = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
                        for c in CAMERAS}
    cams0 = _initial_intrinsics(obs, init_cameras, obj, free_kc)
    views0, extr0 = _initial_poses(obs, cams0, obj)
    packer = _Packer(n_views, free_kc)
    x0 = _pack_initial(cams0, extr0, views0, packer)
    prob = _Problem(obs, obj, n_views, free_kc, robust_loss, f_scale, free_intrinsics)
    xf0 = x0[prob.free_idx]
    res = least_squares(lambda xf: prob.residuals_free(xf, x0), xf0, method="trf",
                        loss=robust_loss, f_scale=f_scale,
                        max_nfev=max_nfev, xtol=1e-10, ftol=1e-10, gtol=1e-10, verbose=0)
    x = prob.pack(res.x, x0)
    cams, extr, views = prob.unpack(x)
    r = prob.residuals(x)
    norms = np.linalg.norm(r.reshape(-1, 2), axis=1)
    rms = float(np.sqrt(np.mean(norms ** 2))) if len(norms) else float("nan")
    cov, cov_info = covariance(prob, x, r, free_intrinsics=free_intrinsics,
                               free_kc=free_kc, robust=robust_covariance,
                               f_scale=f_scale, return_info=True)
    info_mat = cov_info.pop("information", None)
    marg_info = cov_info.pop("marginal_information", None)
    return CalibrationResult(cams, extr, views, cov, rms,
                             float(np.max(norms)) if len(norms) else float("nan"),
                             int(len(norms)), packer.n, robust_loss,
                             info={"success": bool(res.success), "nfev": int(res.nfev),
                                   "cost": float(res.cost), "optimality": float(res.optimality),
                                   "message": str(res.message), "pitch_mm": board_pitch_mm,
                                   "pitch_source": plate.current_spec().pitch_source,
                                   "free_kc": free_kc, "free_intrinsics": free_intrinsics,
                                   "free_mask": prob.free_mask.tolist(),
                                   "param_names": packer.names(),
                                   "views_used": used,
                                   "view_remap": {int(k): int(v) for k, v in remap.items()},
                                   "covariance": cov_info},
                             information=info_mat,
                             marginal_information=marg_info)


def _fd_step(prob: _Problem, x: np.ndarray, j: int) -> float:
    """Per-parameter finite-difference step.

    Linear parameters (focal length, principal point, translation) tolerate a
    larger step than rotations and distortion, where a fixed relative step can
    be lost in projection rounding.
    """
    pk = prob.packer
    for c in CAMERAS:
        sl = pk.slices[c]
        if sl.start <= j < sl.stop:
            off = j - sl.start
            return 1e-4 if off < 4 else 1e-7
    for c in ("right", "tex"):
        sl = pk.extr[c]
        if sl.start <= j < sl.stop:
            return 1e-7 if (j - sl.start) < 3 else 1e-4
    off = (j - pk.views.start) % 6
    return 1e-7 if off < 3 else 1e-4


def _numeric_jacobian(prob: _Problem, x: np.ndarray, f0: np.ndarray,
                      central: bool = True) -> np.ndarray:
    idx = prob.free_idx
    J = np.zeros((len(f0), len(idx)))
    xb = x.copy()
    for jj, j in enumerate(idx):
        h = _fd_step(prob, x, int(j))
        if central:
            xp = xb.copy(); xp[j] += h
            xm = xb.copy(); xm[j] -= h
            J[:, jj] = (prob.residuals(xp) - prob.residuals(xm)) / (2.0 * h)
        else:
            xp = xb.copy(); xp[j] += h
            J[:, jj] = (prob.residuals(xp) - f0) / h
    return J


def _psd_clip(C: np.ndarray, rel_tol: float = 1e-12) -> np.ndarray:
    C = 0.5 * (C + C.T)
    w, V = np.linalg.eigh(C)
    if w.size and w.min() < -rel_tol * max(1.0, w.max()):
        w = np.clip(w, 0.0, None)
        C = (V * w) @ V.T
        C = 0.5 * (C + C.T)
    return C


def covariance(prob: _Problem, x: np.ndarray, f: np.ndarray,
               sigma: Optional[float] = None,
               free_intrinsics: bool = True, free_kc: bool = True,
               robust: bool = True, f_scale: float = 1.0,
               rcond: float = 1e-10,
               return_info: bool = False):
    """Covariance of the free parameters.

    Two estimators are available:

    * plain Gauss-Newton: ``sigma^2 * pinv(J^T J)`` with a component-wise MAD
      scale;
    * robust sandwich for the Huber M-estimator:
      ``A^-1 B A^-1`` with ``A = J^T W J``, ``B = J^T W^2 diag(r^2) J``.  This
      is the appropriate first-order covariance when the fit is robust and the
      residual distribution has heavy tails.

    The pseudoinverse is SVD-based with a relative rank cutoff, and the result
    is clipped to the PSD cone.  A rank-deficient or strongly ill-conditioned
    problem is reported in ``info`` instead of silently producing tiny
    variances.
    """
    try:
        J = _numeric_jacobian(prob, x, f)
    except Exception as exc:
        if return_info:
            return None, {"error": f"jacobian failed: {exc}"}
        return None
    m, n = J.shape
    comps = np.asarray(f, float).ravel()
    if sigma is None:
        sigma = float(1.4826 * np.median(np.abs(comps - np.median(comps)))) if len(comps) else 1.0
        sigma = max(sigma, 1e-3)
    sv = np.linalg.svd(J, compute_uv=False)
    smax = float(sv[0]) if len(sv) else 0.0
    rank = int(np.sum(sv > max(smax * rcond, 1e-15))) if len(sv) else 0
    cond = float(smax / sv[rank - 1]) if rank else float("inf")
    info = {"n_residuals": int(m), "n_free": int(n), "rank": rank,
            "condition": cond, "sigma_px": float(sigma),
            "underdetermined": bool(m <= n), "robust": bool(robust),
            "rcond": float(rcond)}
    if m <= n:
        info["warning"] = "residual count <= free parameter count"
    try:
        # Work in the SVD basis of the (weighted) Jacobian.  Using pinv(H) with
        # the same rcond is WRONG: H eigenvalues are s^2, so a 1e-10 cutoff on
        # H drops directions with s/smax in [1e-5, 1e-10] that are kept in the
        # Jacobian rank.  That produced six zero-variance directions and made
        # the rig covariance singular.
        if robust and f_scale > 0:
            w = np.where(np.abs(comps) <= f_scale, 1.0,
                         f_scale / np.maximum(np.abs(comps), 1e-12))
            Jw = J * np.sqrt(w)[:, None]
            U, S, Vt = np.linalg.svd(Jw, full_matrices=False)
            keep = S > max(S[0] * rcond, 1e-15)
            Ainv = (Vt[keep].T / (S[keep] ** 2)) @ Vt[keep]
            # psi(r) = w*r; the sandwich meat is J^T diag(psi^2) J.
            # The published code used sqrt(w), i.e. w*r^2, which inflated the
            # covariance for outliers (conservative but wrong).
            Jwr = J * (w * comps)[:, None]
            B = Jwr.T @ Jwr
            cov = Ainv @ B @ Ainv
            A = J.T @ (w[:, None] * J)
            info["estimator"] = "huber_sandwich"
        else:
            U, S, Vt = np.linalg.svd(J, full_matrices=False)
            keep = S > max(S[0] * rcond, 1e-15)
            Hinv = (Vt[keep].T / (S[keep] ** 2)) @ Vt[keep]
            cov = Hinv * (sigma ** 2)
            A = J.T @ J
            info["estimator"] = "gauss_newton"
        info["rank_kept"] = int(keep.sum())
        # Marginal information of the rig after eliminating the board poses.
        # Compute the Schur complement from the (weighted) Jacobian, with the
        # pseudoinverse taken at the *singular-value* scale of Jv.  Taking it
        # from Hvv=Jv^T Jv with an H-scale rcond silently drops observable
        # directions and made inv(C_rr) disagree with the Schur complement.
        fm = np.asarray(prob.free_mask, bool)
        n_rig_free = int(fm[:NRIG].sum())
        Jw_all = J * np.sqrt(w)[:, None] if (robust and f_scale > 0) else J
        Jr = Jw_all[:, :n_rig_free]
        Jv = Jw_all[:, n_rig_free:]
        if Jv.shape[1]:
            Uv, Sv, Vtv = np.linalg.svd(Jv, full_matrices=False)
            keepv = Sv > max(Sv[0] * rcond, 1e-15)
            Hvv_inv = (Vtv[keepv].T / (Sv[keepv] ** 2)) @ Vtv[keepv]
            A_rr = Jr.T @ Jr
            A_rv = Jr.T @ Jv
            schur = A_rr - A_rv @ Hvv_inv @ A_rv.T
        else:
            schur = Jr.T @ Jr
        marg = np.zeros((NRIG, NRIG))
        rig_free = np.where(fm[:NRIG])[0]
        marg[np.ix_(rig_free, rig_free)] = schur
        info["marginal_information"] = marg
    except np.linalg.LinAlgError as exc:
        if return_info:
            return None, {"error": f"covariance failed: {exc}", **info}
        return None
    cov = _psd_clip(cov)
    info["trace"] = float(np.trace(cov))
    # full information matrix (free block), used by view selection to avoid
    # inverting an ill-conditioned covariance
    A_full = np.zeros((prob.packer.n, prob.packer.n))
    if 'A' in locals():
        A_full[np.ix_(prob.free_idx, prob.free_idx)] = A
    else:
        A_full[np.ix_(prob.free_idx, prob.free_idx)] = J.T @ J
    info["information"] = A_full
    info["eig_min"] = float(np.linalg.eigvalsh(cov).min()) if cov.size else 0.0
    full = np.zeros((prob.packer.n, prob.packer.n))
    ix = np.ix_(prob.free_idx, prob.free_idx)
    full[ix] = cov
    return (full, info) if return_info else full
