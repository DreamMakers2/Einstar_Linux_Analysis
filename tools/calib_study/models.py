"""A thin, configurable wrapper around the calibration model in ``calib``.

Why this module exists
----------------------
``calib.calibrate.calibrate`` exposes exactly two switches that act on *all*
cameras at once:

    free_intrinsics : fx, fy, cx, cy   (every camera)
    free_kc         : k1, k2, p1, p2, k3 (every camera)

and it never varies ``skew`` (``_Problem.unpack`` builds ``CameraIntrinsics``
without one, so the projection always uses K[0,1] = 0).  The model ladder this
study needs (free only the left IR intrinsics, free only k1/k2, free the skew,
fix the stereo extrinsics, exclude the colour camera) therefore cannot be
expressed with those flags.

``Problem`` below is a re-implementation of ``calib.calibrate._Problem`` with

  * a per-camera free mask over fx, fy, cx, cy, skew(px), k1, k2, p1, p2, k3,
  * per-camera free masks for the rig extrinsics,
  * the identical residual model (``calib.geometry.distort``/``project``
    convention), the identical parameter meaning and the identical robust
    Huber covariance estimator (SVD pseudoinverse + sandwich + Schur marginal
    information over the rig prefix).

It is *verified* against ``calib.calibrate`` (see ``verify_against_calib``):
with skew forced to zero and every camera treated identically, the two produce
the same solution to the solver tolerance.

Skew convention
---------------
Internally ``CameraIntrinsics.skew`` is the dimensionless MATLAB ``alpha_c``
and K[0,1] = skew * fx (the project's ``bouguet`` mode).  The reconstruction
profile JSON stores K[0,1] in pixels, so the conversion is explicit at the
profile boundary (``profiles.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Iterable, Optional

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from calib import plate
from calib.calibrate import (CAMERAS, CameraIntrinsics, Observation,
                             CalibrationResult, _initial_intrinsics, _initial_poses)
from calib.geometry import rodrigues, rvec_from_R

#: per-camera intrinsic parameter names, in packed order
INTR_NAMES = ("fx", "fy", "cx", "cy", "skew_px", "k1", "k2", "p1", "p2", "k3")
N_INTR = len(INTR_NAMES)
EXTR_NAMES = ("rx", "ry", "rz", "tx", "ty", "tz")
N_RIG = len(CAMERAS) * N_INTR + 2 * 6      # 42


# --------------------------------------------------------------------------
# parameter specification
# --------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """Which parameters are free.  Anything absent is held at its start value."""

    free_intr: dict[str, set] = field(default_factory=dict)
    free_extr: dict[str, set] = field(default_factory=dict)
    free_views: bool = True
    label: str = ""

    def intr(self, camera: str) -> set:
        return set(self.free_intr.get(camera, ()))

    def extr(self, camera: str) -> set:
        return set(self.free_extr.get(camera, ())) if camera != "left" else set()

    def summary(self) -> dict:
        return {
            "label": self.label,
            "free_intrinsics": {c: sorted(self.intr(c)) for c in CAMERAS},
            "free_extrinsics": {c: sorted(self.extr(c)) for c in ("right", "tex")},
            "free_views": bool(self.free_views),
            "n_free_rig": sum(len(self.intr(c)) for c in CAMERAS)
                          + sum(len(self.extr(c)) for c in ("right", "tex")),
        }


#: the model ladder of the study.  ``None`` = held at the factory value.
def model_ladder(include_tex: bool = True) -> dict[str, ModelSpec]:
    lin = {"fx", "fy", "cx", "cy"}
    k12 = {"k1", "k2"}
    kc5 = {"k1", "k2", "p1", "p2", "k3"}
    spec = {}
    spec["M0"] = ModelSpec({}, {}, True, "M0 factory profile (no fitting)")
    spec["M1"] = ModelSpec({"left": set(lin)}, {}, True,
                           "M1 ir_left linear intrinsics")
    spec["M2"] = ModelSpec({"left": set(lin), "right": set(lin)}, {}, True,
                           "M2 both IR linear intrinsics")
    spec["M3"] = ModelSpec({"left": lin | k12, "right": lin | k12}, {}, True,
                           "M3 + radial k1,k2 (both IR)")
    spec["M4"] = ModelSpec({"left": lin | kc5 | {"skew_px"},
                            "right": lin | kc5 | {"skew_px"}}, {}, True,
                           "M4 + full kc5 and skew (both IR)")
    spec["M5"] = ModelSpec({"left": lin | kc5 | {"skew_px"},
                            "right": lin | kc5 | {"skew_px"}},
                           {"right": set(EXTR_NAMES)}, True,
                           "M5 + stereo extrinsics (right vs left)")
    tex_intr = {"fx", "fy", "cx", "cy", "skew_px"} if include_tex else set()
    tex_extr = set(EXTR_NAMES) if include_tex else set()
    spec["M6"] = ModelSpec({"left": lin | kc5 | {"skew_px"},
                            "right": lin | kc5 | {"skew_px"},
                            "tex": tex_intr},
                           {"right": set(EXTR_NAMES), "tex": tex_extr}, True,
                           "M6 + colour intrinsics and extrinsic")
    return spec


# --------------------------------------------------------------------------
# packer
# --------------------------------------------------------------------------

class Packer:
    def __init__(self, n_views: int):
        self.n_views = int(n_views)
        self.slices = {}
        i = 0
        for c in CAMERAS:
            self.slices[c] = slice(i, i + N_INTR)
            i += N_INTR
        self.extr = {}
        for c in ("right", "tex"):
            self.extr[c] = slice(i, i + 6)
            i += 6
        self.views = slice(i, i + 6 * self.n_views)
        i += 6 * self.n_views
        self.n = i
        self.n_rig = N_RIG

    def names(self) -> list[str]:
        out = []
        for c in CAMERAS:
            out += [f"{c}.{f}" for f in INTR_NAMES]
        for c in ("right", "tex"):
            out += [f"{c}.{f}" for f in EXTR_NAMES]
        for v in range(self.n_views):
            out += [f"view{v}.{f}" for f in EXTR_NAMES]
        return out


def intr_from_slots(v: np.ndarray) -> CameraIntrinsics:
    fx, fy, cx, cy, skew_px = (float(v[0]), float(v[1]), float(v[2]),
                              float(v[3]), float(v[4]))
    kc = np.asarray(v[5:10], float)
    return CameraIntrinsics(fx=fx, fy=fy, cx=cx, cy=cy, kc=kc,
                            skew=skew_px / fx if fx else 0.0)


def slots_from_intr(cam: CameraIntrinsics) -> np.ndarray:
    return np.array([cam.fx, cam.fy, cam.cx, cam.cy, cam.skew * cam.fx,
                     *np.asarray(cam.kc, float)[:5]], float)


# --------------------------------------------------------------------------
# problem
# --------------------------------------------------------------------------

class _Rows:
    """Flat, vectorised description of the observation set."""

    __slots__ = ("row_cam", "row_view", "obj", "uv", "row_of_point",
                 "n_points", "cam_idx", "n_views", "counts")


def _build_rows(obs: list[Observation], obj: np.ndarray) -> _Rows:
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    r = _Rows()
    cams = {c: i for i, c in enumerate(CAMERAS)}
    rows = []
    for o in obs:
        keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_index]
        if not keys:
            continue
        idx = np.array([uv_index[(float(k[0]), float(k[1]))] for k in keys], int)
        uv = np.array([o.uv[k] for k in keys], float)
        rows.append((cams[o.camera], int(o.view), idx, uv))
    n_views = 1 + max((r[1] for r in rows), default=0)
    r.n_views = n_views
    r.row_cam = np.array([x[0] for x in rows], int)
    r.row_view = np.array([x[1] for x in rows], int)
    r.counts = np.array([len(x[2]) for x in rows], int)
    r.obj = np.concatenate([x[2] for x in rows]) if rows else np.zeros(0, int)
    r.uv = np.concatenate([x[3] for x in rows]) if rows else np.zeros((0, 2))
    r.row_of_point = np.repeat(np.arange(len(rows)), r.counts)
    r.n_points = len(r.obj)
    r.cam_idx = np.array([cams[c] for c in CAMERAS])
    return r


class Problem:
    """Reprojection bundle adjustment with an arbitrary per-parameter free mask."""

    def __init__(self, obs: list[Observation], spec: ModelSpec,
                 pitch_mm: float = plate.DEFAULT_PITCH_MM):
        self.obs = obs
        used = sorted(set(int(o.view) for o in obs))
        self.view_ids = used
        self.remap = {v: i for i, v in enumerate(used)}
        self.spec = spec
        self.pitch_mm = float(pitch_mm)
        self.obj = plate.board_points_mm(self.pitch_mm)
        remapped = [Observation(o.camera, self.remap[int(o.view)], o.uv) for o in obs]
        self.rows = _build_rows(remapped, self.obj)
        self.packer = Packer(self.rows.n_views)
        self.free_mask = self._mask()
        self.free_idx = np.where(self.free_mask)[0]

    def _mask(self) -> np.ndarray:
        p = self.packer
        m = np.zeros(p.n, bool)
        for ci, c in enumerate(CAMERAS):
            names = set(self.spec.intr(c))
            for j, n in enumerate(INTR_NAMES):
                if n in names:
                    m[p.slices[c].start + j] = True
            # a free skew without free fx is meaningless in the alpha_c gauge
            if "fx" not in names:
                m[p.slices[c].start + 4] = False
        for c in ("right", "tex"):
            for j, n in enumerate(EXTR_NAMES):
                if n in self.spec.extr(c):
                    m[p.extr[c].start + j] = True
        if self.spec.free_views:
            m[p.views] = True
        return m

    # -- parameter <-> state ---------------------------------------------
    def unpack(self, x: np.ndarray):
        p = self.packer
        cams = {c: intr_from_slots(x[p.slices[c]]) for c in CAMERAS}
        extr = {"left": (np.eye(3), np.zeros(3))}
        for c in ("right", "tex"):
            v = x[p.extr[c]]
            extr[c] = (rodrigues(v[:3]), v[3:6].copy())
        views = []
        for i in range(p.n_views):
            v = x[p.views.start + 6 * i: p.views.start + 6 * (i + 1)]
            views.append((rodrigues(v[:3]), v[3:6].copy()))
        return cams, extr, views

    def pack_state(self, cams, extr, views) -> np.ndarray:
        p = self.packer
        x = np.zeros(p.n)
        for c in CAMERAS:
            x[p.slices[c]] = slots_from_intr(cams[c])
        for c in ("right", "tex"):
            R, t = extr[c]
            x[p.extr[c]] = np.concatenate([rvec_from_R(R), t])
        for i, (R, t) in enumerate(views):
            x[p.views.start + 6 * i: p.views.start + 6 * (i + 1)] = np.concatenate(
                [rvec_from_R(R), t])
        return x

    # -- residuals -------------------------------------------------------
    def residuals(self, x: np.ndarray) -> np.ndarray:
        cams, extr, views = self.unpack(x)
        r = self.rows
        if r.n_points == 0:
            return np.zeros(0)
        fx = np.empty(len(CAMERAS)); fy = np.empty(len(CAMERAS))
        cx = np.empty(len(CAMERAS)); cy = np.empty(len(CAMERAS))
        sk = np.empty(len(CAMERAS)); kc = np.empty((len(CAMERAS), 5))
        for i, c in enumerate(CAMERAS):
            g = cams[c]
            fx[i], fy[i], cx[i], cy[i] = g.fx, g.fy, g.cx, g.cy
            sk[i] = g.skew * g.fx
            kc[i] = np.asarray(g.kc, float)[:5]
        Rv = np.stack([v[0] for v in views]) if views else np.zeros((0, 3, 3))
        tv = np.stack([v[1] for v in views]) if views else np.zeros((0, 3))
        Rc = np.stack([extr[c][0] for c in CAMERAS])
        tc = np.stack([extr[c][1] for c in CAMERAS])
        Rcr = Rc[r.row_cam]
        Rcomb = np.einsum("mij,mjk->mik", Rcr, Rv[r.row_view])
        tcomb = np.einsum("mij,mj->mi", Rcr, tv[r.row_view]) + tc[r.row_cam]
        X = self.obj[r.obj]
        Xc = np.einsum("mij,mj->mi", Rcomb[r.row_of_point], X) + tcomb[r.row_of_point]
        z = np.maximum(Xc[:, 2], 1e-6)
        a = Xc[:, 0] / z
        b = Xc[:, 1] / z
        ci = r.row_cam[r.row_of_point]
        r2 = a * a + b * b
        k1, k2 = kc[ci, 0], kc[ci, 1]
        p1, p2, k3 = kc[ci, 2], kc[ci, 3], kc[ci, 4]
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        xd = a * radial + 2.0 * p1 * a * b + p2 * (r2 + 2.0 * a * a)
        yd = b * radial + p1 * (r2 + 2.0 * b * b) + 2.0 * p2 * a * b
        u = fx[ci] * xd + sk[ci] * yd + cx[ci]
        v = fy[ci] * yd + cy[ci]
        return np.stack([u - r.uv[:, 0], v - r.uv[:, 1]], axis=1).ravel()

    def pack(self, xf: np.ndarray, x0: np.ndarray) -> np.ndarray:
        x = x0.copy()
        x[self.free_idx] = xf
        return x

    def jac_sparsity(self) -> lil_matrix:
        p = self.packer
        rows = self.rows
        S = lil_matrix((2 * rows.n_points, p.n), dtype=int)
        for m, (cam, view) in enumerate(zip(rows.row_cam, rows.row_view)):
            lo = 2 * int(rows.counts[:m].sum())
            hi = lo + 2 * int(rows.counts[m])
            c = CAMERAS[cam]
            S[lo:hi, p.slices[c]] = 1
            if c != "left":
                S[lo:hi, p.extr[c]] = 1
            S[lo:hi, p.views.start + 6 * int(view):
              p.views.start + 6 * (int(view) + 1)] = 1
        return S

    def jac_sparsity_free(self) -> lil_matrix:
        return self.jac_sparsity()[:, self.free_idx]


def _fd_step(prob: Problem, j: int) -> float:
    """Per-parameter finite-difference step (matches calibrate._fd_step scale)."""
    p = prob.packer
    for c in CAMERAS:
        sl = p.slices[c]
        if sl.start <= j < sl.stop:
            off = j - sl.start
            name = INTR_NAMES[off]
            if name in ("fx", "fy", "cx", "cy"):
                return 1e-4
            if name == "skew_px":
                return 1e-2          # pixels of K[0,1]; 1e-2 px is well resolved
            return 1e-7
    for c in ("right", "tex"):
        sl = p.extr[c]
        if sl.start <= j < sl.stop:
            return 1e-7 if (j - sl.start) < 3 else 1e-4
    off = (j - p.views.start) % 6
    return 1e-7 if off < 3 else 1e-4


def numeric_jacobian(prob: Problem, x: np.ndarray, f0: np.ndarray,
                     central: bool = True) -> np.ndarray:
    idx = prob.free_idx
    J = np.zeros((len(f0), len(idx)))
    xb = x.copy()
    for jj, j in enumerate(idx):
        h = _fd_step(prob, int(j))
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
        C = (V * np.clip(w, 0.0, None)) @ V.T
        C = 0.5 * (C + C.T)
    return C


# --------------------------------------------------------------------------
# fit
# --------------------------------------------------------------------------

@dataclass
class Fit:
    spec: ModelSpec
    x: np.ndarray
    cams: dict
    extr: dict
    views: list
    rms: float
    rms_max: float
    n_obs: int
    n_free: int
    n_residuals: int
    n_views: int
    covariance: Optional[np.ndarray]       # full square, fixed entries zero
    rig_covariance: np.ndarray             # 42x42 marginal rig block
    marginal_information: np.ndarray       # 42x42 Schur complement
    info: dict
    wall_s: float
    pitch_mm: float
    view_ids: list
    param_names: list

    def param_std(self) -> dict[str, float]:
        sd = np.sqrt(np.maximum(np.diag(self.rig_covariance), 0.0))
        names = self.param_names[:N_RIG]
        return {n: float(s) for n, s in zip(names, sd)}



def fit(obs: list[Observation], spec: ModelSpec,
        init: dict[str, CameraIntrinsics],
        init_extr: Optional[dict] = None,
        init_views: Optional[list] = None,
        pitch_mm: float = plate.DEFAULT_PITCH_MM,
        robust_loss: str = "huber", f_scale: float = 1.0,
        max_nfev: int = 400, robust_covariance: bool = True,
        tr_solver: str = "lsmr", x_scale="jac",
        compute_covariance: bool = True) -> Fit:
    t0 = time.time()
    prob = Problem(obs, spec, pitch_mm)
    obj = prob.obj
    remap = prob.remap
    obs_r = [Observation(o.camera, remap[int(o.view)], o.uv) for o in obs]
    if init_views is None or init_extr is None:
        views_fb, extr_fb = _initial_poses(obs_r, init, obj)
    views0 = list(init_views) if init_views is not None else views_fb
    extr0 = dict(init_extr) if init_extr is not None else extr_fb
    cams0 = {c: CameraIntrinsics(init[c].fx, init[c].fy, init[c].cx, init[c].cy,
                                 np.asarray(init[c].kc, float).copy(), init[c].skew)
             for c in CAMERAS}
    x0 = prob.pack_state(cams0, extr0, views0)
    xf0 = x0[prob.free_idx]
    if len(prob.free_idx) == 0:
        x = x0
        r = prob.residuals(x)
        cov = np.zeros((prob.packer.n, prob.packer.n))
        rigC = np.zeros((N_RIG, N_RIG))
        S = np.zeros((N_RIG, N_RIG))
        info = {"success": True, "nfev": 0, "message": "no free parameters"}
        free = 0
    else:
        res = least_squares(lambda xf: prob.residuals(prob.pack(xf, x0)), xf0,
                            method="trf", loss=robust_loss, f_scale=f_scale,
                            max_nfev=max_nfev, xtol=1e-10, ftol=1e-10, gtol=1e-10,
                            verbose=0, jac_sparsity=prob.jac_sparsity_free(),
                            tr_solver=tr_solver, x_scale=x_scale)
        x = prob.pack(res.x, x0)
        r = prob.residuals(x)
        free = len(prob.free_idx)
        if compute_covariance:
            cov, rigC, S, info = covariance(prob, x, r, free, f_scale=f_scale,
                                            robust=robust_covariance)
        else:
            cov = None; rigC = np.zeros((N_RIG, N_RIG)); S = np.zeros((N_RIG, N_RIG))
            info = {}
        info = dict(info, success=bool(res.success), nfev=int(res.nfev),
                    cost=float(res.cost), optimality=float(res.optimality),
                    message=str(res.message))
    norms = np.linalg.norm(r.reshape(-1, 2), axis=1)
    rms = float(np.sqrt(np.mean(norms ** 2))) if len(norms) else float("nan")
    cams, extr, views = prob.unpack(x)
    wall = time.time() - t0
    return Fit(spec=spec, x=x, cams=cams, extr=extr, views=views, rms=rms,
               rms_max=float(norms.max()) if len(norms) else float("nan"),
               n_obs=int(len(norms)), n_free=int(free), n_residuals=int(len(r)),
               n_views=prob.rows.n_views, covariance=cov,
               rig_covariance=rigC, marginal_information=S,
               info=dict(info, pitch_mm=pitch_mm, free_mask=prob.free_mask.tolist(),
                         view_ids=list(prob.view_ids),
                         views_remap={int(k): int(v) for k, v in prob.remap.items()}),
               wall_s=wall, pitch_mm=pitch_mm,
               view_ids=list(prob.view_ids), param_names=prob.packer.names())


def covariance(prob: Problem, x: np.ndarray, f: np.ndarray, n_free: int,
               f_scale: float = 1.0, robust: bool = True,
               rcond: float = 1e-10):
    """Robust sandwich covariance + Schur marginal information (as calibrate)."""
    J = numeric_jacobian(prob, x, f)
    m, n = J.shape
    comps = np.asarray(f, float).ravel()
    sigma = float(1.4826 * np.median(np.abs(comps - np.median(comps)))) if len(comps) else 1.0
    sigma = max(sigma, 1e-3)
    sv = np.linalg.svd(J, compute_uv=False)
    smax = float(sv[0]) if len(sv) else 0.0
    rank = int(np.sum(sv > max(smax * rcond, 1e-15))) if len(sv) else 0
    cond = float(smax / sv[rank - 1]) if rank else float("inf")
    info = {"n_residuals": int(m), "n_free": int(n), "rank": rank,
            "condition": cond, "sigma_px": float(sigma), "robust": bool(robust)}
    if robust and f_scale > 0:
        w = np.where(np.abs(comps) <= f_scale, 1.0,
                     f_scale / np.maximum(np.abs(comps), 1e-12))
    else:
        w = np.ones_like(comps)
    Jw = J * np.sqrt(w)[:, None]
    U, S, Vt = np.linalg.svd(Jw, full_matrices=False)
    keep = S > max(S[0] * rcond, 1e-15)
    Ainv = (Vt[keep].T / (S[keep] ** 2)) @ Vt[keep]
    if robust and f_scale > 0:
        Jwr = J * (w * comps)[:, None]
        B = Jwr.T @ Jwr
        covf = _psd_clip(Ainv @ B @ Ainv)
        A = J.T @ (w[:, None] * J)
        info["estimator"] = "huber_sandwich"
    else:
        covf = _psd_clip(Ainv * (sigma ** 2))
        A = J.T @ J
        info["estimator"] = "gauss_newton"
    info["rank_kept"] = int(keep.sum())
    # marginal rig information after eliminating the board poses
    fm = np.asarray(prob.free_mask, bool)
    n_rig_free = int(fm[:N_RIG].sum())
    Jr = Jw[:, :n_rig_free]
    Jv = Jw[:, n_rig_free:]
    if Jv.shape[1]:
        Uv, Sv, Vtv = np.linalg.svd(Jv, full_matrices=False)
        keepv = Sv > max(Sv[0] * rcond, 1e-15)
        Hvv_inv = (Vtv[keepv].T / (Sv[keepv] ** 2)) @ Vtv[keepv]
        A_rr = Jr.T @ Jr
        A_rv = Jr.T @ Jv
        schur = _psd_clip(A_rr - A_rv @ Hvv_inv @ A_rv.T)
    else:
        schur = Jr.T @ Jr
    full = np.zeros((prob.packer.n, prob.packer.n))
    full[np.ix_(prob.free_idx, prob.free_idx)] = covf
    rigC = np.zeros((N_RIG, N_RIG))
    rig_free = np.where(fm[:N_RIG])[0]
    rigC[np.ix_(rig_free, rig_free)] = full[np.ix_(rig_free, rig_free)]
    S = np.zeros((N_RIG, N_RIG))
    S[np.ix_(rig_free, rig_free)] = schur
    info["trace"] = float(np.trace(rigC))
    return full, rigC, S, info


# --------------------------------------------------------------------------
# evaluation helpers
# --------------------------------------------------------------------------

def _per_view_obs(obs):
    by_view = {}
    for o in obs:
        by_view.setdefault(int(o.view), {})[o.camera] = o
    return by_view


def solve_view_pose(fit_, per_cam, obj, uv_index, init=None):
    """Board pose of ONE view from all of its cameras, rig fixed.

    Used for held-out views: the pose is a nuisance parameter, so estimating it
    from the held-out view itself is legitimate and is exactly what a downstream
    user would do.
    """
    from calib.geometry import solve_pnp, rodrigues, rvec_from_R
    from scipy.optimize import least_squares
    if init is None:
        best = max(per_cam.values(), key=lambda o: len(o.uv))
        keys = [k for k in best.uv if (float(k[0]), float(k[1])) in uv_index]
        if len(keys) < 6:
            return None
        idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
        g = fit_.cams[best.camera]
        p = solve_pnp(obj[idx], np.array([best.uv[k] for k in keys]),
                      g.K, np.asarray(g.kc, float))
        if p is None:
            return None
        init = (p.R, p.t)
    R0, t0 = init

    def res(v):
        R = rodrigues(v[:3])
        t = v[3:6]
        out = []
        for c, o in per_cam.items():
            keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_index]
            if not keys:
                continue
            idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
            X = obj[idx]
            Rc, tc = fit_.extr[c]
            Xc = (X @ R.T + t) @ Rc.T + tc
            g = fit_.cams[c]
            z = np.maximum(Xc[:, 2], 1e-6)
            a, b = Xc[:, 0] / z, Xc[:, 1] / z
            k1, k2, p1, p2, k3 = np.asarray(g.kc, float)[:5]
            r2 = a * a + b * b
            radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
            xd = a * radial + 2 * p1 * a * b + p2 * (r2 + 2 * a * a)
            yd = b * radial + p1 * (r2 + 2 * b * b) + 2 * p2 * a * b
            u = g.fx * xd + g.skew * g.fx * yd + g.cx
            vv = g.fy * yd + g.cy
            uv = np.array([o.uv[k] for k in keys], float)
            out.append(np.stack([u - uv[:, 0], vv - uv[:, 1]], 1).ravel())
        return np.concatenate(out) if out else np.zeros(0)

    x0 = np.concatenate([rvec_from_R(R0), t0])
    ls = least_squares(res, x0, method="lm", max_nfev=200, xtol=1e-12, ftol=1e-12)
    return rodrigues(ls.x[:3]), ls.x[3:6].copy()


def evaluate(fit_: Fit, obs: list[Observation], solve_poses: bool = False) -> dict:
    """Per-camera and per-view reprojection RMS for a fitted rig.

    ``solve_poses=True`` estimates the board pose of every view in ``obs`` from
    that view alone (the held-out procedure); otherwise the fit's own poses are
    used and views it never saw are skipped.
    """
    remap = fit_.info.get("views_remap", {})
    obj = plate.board_points_mm(fit_.pitch_mm)
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    per_cam, per_view = {}, {}
    alln = []
    for v, per in sorted(_per_view_obs(obs).items()):
        if solve_poses or int(v) not in remap:
            pose = solve_view_pose(fit_, per, obj, uv_index)
            if pose is None:
                continue
        else:
            pose = fit_.views[remap[int(v)]]
        for c, o in per.items():
            keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_index]
            if not keys:
                continue
            idx = np.array([uv_index[(float(k[0]), float(k[1]))] for k in keys], int)
            uv = np.array([o.uv[k] for k in keys], float)
            X = obj[idx]
            Rv, tv = pose
            Rc, tc = fit_.extr[c]
            Xc = (X @ Rv.T + tv) @ Rc.T + tc
            g = fit_.cams[c]
            z = np.maximum(Xc[:, 2], 1e-6)
            a, b = Xc[:, 0] / z, Xc[:, 1] / z
            k1, k2, p1, p2, k3 = np.asarray(g.kc, float)[:5]
            r2 = a * a + b * b
            radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
            xd = a * radial + 2 * p1 * a * b + p2 * (r2 + 2 * a * a)
            yd = b * radial + p1 * (r2 + 2 * b * b) + 2 * p2 * a * b
            u = g.fx * xd + g.skew * g.fx * yd + g.cx
            vv = g.fy * yd + g.cy
            e = np.linalg.norm(np.stack([u - uv[:, 0], vv - uv[:, 1]], 1), axis=1)
            per_cam.setdefault(c, []).extend(e.tolist())
            per_view.setdefault(int(v), []).extend(e.tolist())
            alln.extend(e.tolist())

    def _stat(d):
        out = {}
        for k, v in d.items():
            v = np.asarray(v, float)
            out[k] = {"rms": float(np.sqrt(np.mean(v ** 2))), "median": float(np.median(v)),
                      "n": int(len(v))}
        return out
    a = np.asarray(alln, float)
    return {"rms": float(np.sqrt(np.mean(a ** 2))) if len(a) else float("nan"),
            "median": float(np.median(a)) if len(a) else float("nan"),
            "p95": float(np.percentile(a, 95)) if len(a) else float("nan"),
            "n": int(len(a)), "per_camera": _stat(per_cam), "per_view": _stat(per_view)}


def rig_residuals(fit_: Fit, obs: list[Observation],
                  source: str = "left") -> dict:
    """Predict every other camera's markers from a pose fitted on ``source`` only.

    This is the held-out test that does not let the evaluated camera influence
    the pose: the board pose of each view is estimated from ``source`` alone and
    the other cameras are predicted through the rig extrinsics.  It therefore
    tests the intrinsics *and* the rig, not the nuisance pose.
    """
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    remap = fit_.info.get("views_remap", {})
    by_view: dict[int, dict] = {}
    for o in obs:
        by_view.setdefault(int(o.view), {})[o.camera] = o
    obj = plate.board_points_mm(fit_.pitch_mm)
    per_cam: dict[str, list] = {}
    from calib.geometry import solve_pnp
    for v, per in sorted(by_view.items()):
        src = per.get(source)
        if src is None:
            continue
        keys = [k for k in src.uv if (float(k[0]), float(k[1])) in uv_index]
        if len(keys) < 6:
            continue
        idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
        g0 = fit_.cams[source]
        pose = solve_pnp(obj[idx], np.array([src.uv[k] for k in keys]),
                         g0.K, np.asarray(g0.kc, float))
        if pose is None:
            continue
        for c, o in per.items():
            if c == source:
                continue
            keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_index]
            if not keys:
                continue
            idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
            X = obj[idx]
            Rc, tc = fit_.extr[c]
            Xc = (X @ pose.R.T + pose.t) @ Rc.T + tc
            g = fit_.cams[c]
            z = np.maximum(Xc[:, 2], 1e-6)
            a, b = Xc[:, 0] / z, Xc[:, 1] / z
            k1, k2, p1, p2, k3 = np.asarray(g.kc, float)[:5]
            r2 = a * a + b * b
            radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
            xd = a * radial + 2 * p1 * a * b + p2 * (r2 + 2 * a * a)
            yd = b * radial + p1 * (r2 + 2 * b * b) + 2 * p2 * a * b
            u = g.fx * xd + g.skew * g.fx * yd + g.cx
            vv = g.fy * yd + g.cy
            uv = np.array([o.uv[k] for k in keys], float)
            e = np.linalg.norm(np.stack([u - uv[:, 0], vv - uv[:, 1]], 1), axis=1)
            per_cam.setdefault(c, []).extend(e.tolist())
    out = {}
    for c, vals in per_cam.items():
        a = np.asarray(vals, float)
        out[c] = {"rms": float(np.sqrt(np.mean(a ** 2))), "median": float(np.median(a)),
                  "p95": float(np.percentile(a, 95)), "n": int(len(a))}
    return out


def pnp_residuals(fit_: Fit, obs: list[Observation]) -> dict:
    """Per-camera PnP residual with that camera's own intrinsics and its own pose.

    This isolates the *intrinsics* from the rig: each camera gets its own pose,
    so the number says how well the camera model describes its own images and
    says nothing about the stereo consistency.
    """
    from calib.geometry import solve_pnp
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    obj = plate.board_points_mm(fit_.pitch_mm)
    per_cam: dict[str, list] = {}
    for o in obs:
        keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_index]
        if len(keys) < 6:
            continue
        idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
        g = fit_.cams[o.camera]
        img = np.array([o.uv[k] for k in keys], float)
        p = solve_pnp(obj[idx], img, g.K, np.asarray(g.kc, float))
        if p is None:
            continue
        pred = (obj[idx] @ p.R.T + p.t)
        z = np.maximum(pred[:, 2], 1e-6)
        a, b = pred[:, 0] / z, pred[:, 1] / z
        k1, k2, p1, p2, k3 = np.asarray(g.kc, float)[:5]
        r2 = a * a + b * b
        radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        xd = a * radial + 2 * p1 * a * b + p2 * (r2 + 2 * a * a)
        yd = b * radial + p1 * (r2 + 2 * b * b) + 2 * p2 * a * b
        u = g.fx * xd + g.skew * g.fx * yd + g.cx
        v = g.fy * yd + g.cy
        e = np.linalg.norm(np.stack([u - img[:, 0], v - img[:, 1]], 1), axis=1)
        per_cam.setdefault(o.camera, []).extend(e.tolist())
    out = {}
    for c, vals in per_cam.items():
        a = np.asarray(vals, float)
        out[c] = {"rms": float(np.sqrt(np.mean(a ** 2))),
                  "median": float(np.median(a)), "n": int(len(a))}
    return out


def rig_pose_delta(fit_: Fit, reference: dict) -> dict:
    """Difference between the fitted and the reference rig extrinsics."""
    out = {}
    for c in ("right", "tex"):
        R, t = fit_.extr[c]
        R0, t0 = reference[c]
        dR = R @ R0.T
        ang = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(dR) - 1.0) / 2.0))))
        out[c] = {"rotation_deg": float(ang),
                  "translation_mm": float(np.linalg.norm(t - t0)),
                  "baseline_mm": float(np.linalg.norm(t)),
                  "baseline_ref_mm": float(np.linalg.norm(t0))}
    return out


# --------------------------------------------------------------------------
# verification against calib.calibrate
# --------------------------------------------------------------------------

def verify_against_calib(obs: list[Observation], init: dict[str, CameraIntrinsics],
                         pitch_mm: float) -> dict:
    """Fit the same problem with ``calib.calibrate`` and with ``fit``.

    The variant both can express: every camera's linear intrinsics and full kc5
    free, no skew, no extrinsic freedom beyond what ``calibrate`` always has.
    ``calibrate`` always frees the right/tex extrinsics, so the wrapper does the
    same here.
    """
    from calib.calibrate import calibrate
    lin = {"fx", "fy", "cx", "cy"}
    kc5 = {"k1", "k2", "p1", "p2", "k3"}
    spec = ModelSpec({c: lin | kc5 for c in CAMERAS},
                     {"right": set(EXTR_NAMES), "tex": set(EXTR_NAMES)},
                     True, "verify")
    mine = fit(obs, spec, init, pitch_mm=pitch_mm)
    ref = calibrate(obs, board_pitch_mm=pitch_mm, init_cameras=init,
                    free_kc=True, free_intrinsics=True)
    out = {"wrapper_rms": mine.rms, "calibrate_rms": ref.residual_rms,
           "rms_diff_px": abs(mine.rms - ref.residual_rms),
           "max_intrinsic_diff": 0.0, "max_baseline_diff_mm": 0.0}
    for c in CAMERAS:
        a, b = mine.cams[c], ref.cameras[c]
        out["max_intrinsic_diff"] = max(
            out["max_intrinsic_diff"],
            abs(a.fx - b.fx), abs(a.fy - b.fy), abs(a.cx - b.cx),
            abs(a.cy - b.cy), float(np.max(np.abs(np.asarray(a.kc) - np.asarray(b.kc)))))
    out["max_baseline_diff_mm"] = float(
        abs(np.linalg.norm(mine.extr["right"][1]) - np.linalg.norm(ref.extrinsics["right"][1])))
    out["note"] = ("identical model; the wrapper adds a per-parameter free mask "
                   "and a pixel-space skew slot")
    return out


def linearise(obs: list[Observation], spec: ModelSpec,
              cams: dict[str, CameraIntrinsics],
              extr: Optional[dict] = None,
              views: Optional[list] = None,
              pitch_mm: float = plate.DEFAULT_PITCH_MM,
              f_scale: float = 1.0, robust_covariance: bool = True) -> Fit:
    """Evaluate the information matrix at a *given* state without optimising.

    Used for "the identifiability at the factory solution": the rig is held at
    the factory values, the board poses are PnP solutions at those intrinsics,
    and only the Jacobian/covariance is computed.  No parameter is fitted, so
    the answer cannot depend on an optimiser or on a starting point.
    """
    t0 = time.time()
    prob = Problem(obs, spec, pitch_mm)
    remap = prob.remap
    obs_r = [Observation(o.camera, remap[int(o.view)], o.uv) for o in obs]
    views0 = list(views) if views is not None else _initial_poses(obs_r, cams, prob.obj)[0]
    extr0 = dict(extr) if extr is not None else _initial_poses(obs_r, cams, prob.obj)[1]
    x = prob.pack_state(cams, extr0, views0)
    r = prob.residuals(x)
    cov, rigC, S, info = covariance(prob, x, r, len(prob.free_idx),
                                    f_scale=f_scale, robust=robust_covariance)
    norms = np.linalg.norm(r.reshape(-1, 2), axis=1)
    cams_u, extr_u, views_u = prob.unpack(x)
    return Fit(spec=spec, x=x, cams=cams_u, extr=extr_u, views=views_u,
               rms=float(np.sqrt(np.mean(norms ** 2))) if len(norms) else float("nan"),
               rms_max=float(norms.max()) if len(norms) else float("nan"),
               n_obs=int(len(norms)), n_free=int(len(prob.free_idx)),
               n_residuals=int(len(r)), n_views=prob.rows.n_views,
               covariance=cov, rig_covariance=rigC, marginal_information=S,
               info=dict(info, linearised=True, nfev=0,
                         pitch_mm=pitch_mm, free_mask=prob.free_mask.tolist(),
                         view_ids=list(prob.view_ids),
                         views_remap={int(k): int(v) for k, v in prob.remap.items()}),
               wall_s=time.time() - t0, pitch_mm=pitch_mm,
               view_ids=list(prob.view_ids), param_names=prob.packer.names())
