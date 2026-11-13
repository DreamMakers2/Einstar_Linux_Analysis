"""Solver backends for the joint 3-camera calibration.

Why this module exists
----------------------
``calib.calibrate.calibrate`` is the reference engine: SciPy
``least_squares(method="trf")`` with a *finite-difference* Jacobian and a
covariance built from a second central-difference Jacobian.  That is correct but
slow, and it offers no way to fix individual distortion coefficients, to add
priors, or to exchange the robust loss without reimplementing the model.

This module keeps ``calibrate.calibrate`` untouched and adds an equivalent
solver layer with one model, one analytic Jacobian and three backends:

``"scipy"``
    ``scipy.optimize.least_squares`` on the *same* residual vector, with the
    analytic Jacobian (``jac="analytic"``, default) or SciPy's own arithmetic
    differencing (``jac="fd"``, the current engine behaviour).  All four robust
    losses of SciPy are available.  Supports priors, arbitrary free/fixed
    intrinsics and per-coefficient distortion masks.
``"native"``
    The compiled Eigen-free Levenberg-Marquardt in ``calib/ceres/calib_ref``
    (Huber loss only, all-or-nothing distortion).  Parameters come from the
    binary; the covariance is evaluated analytically at the native solution with
    the *same* Huber-sandwich formula the binary uses (the binary itself only
    exports the variance diagonal, which is checked against this recomputation).
    Any failure (missing binary, timeout, non-finite output, model mismatch) is
    reported and falls back to ``"scipy"``.
``"opencv"``
    ``cv2.calibrateCamera`` per camera plus ``cv2.stereoCalibrate`` for the IR
    pair and the texture camera: an independent-fit baseline on the *same*
    observations.  No joint rig cost and no rig covariance, so it is a
    comparison point, not a replacement.
``"lm"``
    A small internal Levenberg-Marquardt with explicit IRLS weights.  It is the
    reference implementation for the *mathematically* clean combination of a
    robust loss with quadratic priors (SciPy applies its loss elementwise to
    every residual, including prior rows, which is not what one wants when a
    prior is strongly violated).  Also useful as a third opinion.

Model and parameter order are exactly ``calib.calibrate``'s:

    x = [left intr 9][right intr 9][tex intr 9][right extr 6][tex extr 6]
        [view0 6]...[viewN-1 6]
    intr = fx fy cx cy k1 k2 p1 p2 k3,  X_cam = R X_left + t for the rig,
    X_left = R_v X_board + t_v for the views, left camera = identity gauge.

Public API
----------
``Problem`` / ``make_problem``           observations + board + initial guess
``solve_joint(problem, ...) -> dict``    solve, covariance, residuals, diagnostics
``backend_report() -> dict``             backend availability + feature matrix
``solve_engine(problem, ...)``           thin wrapper over ``calibrate.calibrate``
``reconstruct_point(x, rig, depth_mm)``  triangulate a test point with a fitted rig
``point_sigma(x, cov, depth_mm)``        delta-method sigma of that point

The dict returned by ``solve_joint`` is JSON-friendly apart from ``result``
(a ``CalibrationResult``) and the numpy arrays; ``backend_report`` is pure JSON.

The measured comparison that motivates the defaults (runtime, covariance
calibration against Monte Carlo, robust-loss and prior ablations) is in the
accompanying benchmark report; this module makes no claim of its own beyond
reproducing the engine's model exactly.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

import cv2
import numpy as np
from scipy.optimize import least_squares

from . import calibrate as engine
from . import plate
from .calibrate import (CAMERAS, NRIG, CalibrationResult, CameraIntrinsics,
                        Observation, _Packer, _pack_initial)
from .geometry import project, rodrigues, rvec_from_R

__all__ = [
    "CAMERAS", "NRIG", "Problem", "make_problem", "solve_joint",
    "backend_report", "solve_engine", "reconstruct_point", "reconstruction_error", "point_sigma",
    "LOSSES", "NATIVE_BINARY",
]

# --------------------------------------------------------------------------
# constants and backend discovery
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
NATIVE_BINARY = Path(os.environ.get("EINSTAR_NATIVE_SOLVER",
                                    HERE / "ceres" / "calib_ref"))
LOSSES = ("none", "huber", "cauchy", "soft_l1")
_LAST_RUNS: list[dict] = []
_MAX_RUNS = 32


def _kc_mask(free_kc) -> np.ndarray:
    """Free/fixed mask over (k1, k2, p1, p2, k3).

    ``True``/``False`` keep the engine's all-or-nothing meaning; ``"k1k2"`` and
    ``"k1k2k3"`` are the reduced Brown-Conrady variants; a length-5 sequence is
    taken literally.
    """
    if free_kc is True:
        return np.ones(5, bool)
    if free_kc is False:
        return np.zeros(5, bool)
    if isinstance(free_kc, str):
        key = free_kc.lower().replace("_", "").replace(",", "")
        table = {"k1k2": [1, 1, 0, 0, 0],
                 "k1k2k3": [1, 1, 0, 0, 1],
                 "k1": [1, 0, 0, 0, 0],
                 "all": [1, 1, 1, 1, 1],
                 "none": [0, 0, 0, 0, 0],
                 "brown": [1, 1, 1, 1, 1]}
        if key not in table:
            raise ValueError(f"unknown free_kc spec {free_kc!r}; "
                             f"use True/False/'k1k2'/'k1k2k3' or 5 booleans")
        return np.array(table[key], bool)
    arr = np.asarray(free_kc)
    if arr.shape != (5,):
        raise ValueError("free_kc sequence must have length 5")
    return arr.astype(bool)


# --------------------------------------------------------------------------
# rotation helpers (analytic derivative, cv2.Rodrigues convention)
# --------------------------------------------------------------------------

def _skew(v: np.ndarray) -> np.ndarray:
    return np.array([[0.0, -v[2], v[1]],
                     [v[2], 0.0, -v[0]],
                     [-v[1], v[0], 0.0]])


def rodrigues_dR(rvec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``R = Exp([r]x)`` and ``dR/dr_i`` in the ``cv2.Rodrigues`` convention."""
    r = np.asarray(rvec, float).ravel()
    th = float(np.linalg.norm(r))
    K = _skew(r)
    K2 = K @ K
    eye = np.eye(3)
    if th < 1e-8:
        A, B = 1.0 - th * th / 6.0, 0.5 - th * th / 24.0
        dA, dB = -th / 3.0, -th / 12.0
    else:
        A, B = np.sin(th) / th, (1.0 - np.cos(th)) / (th * th)
        dA = (th * np.cos(th) - np.sin(th)) / (th * th)
        dB = (th * np.sin(th) - 2.0 * (1.0 - np.cos(th))) / (th ** 3)
    R = eye + A * K + B * K2
    dR = np.empty((3, 3, 3))
    for i in range(3):
        e = np.zeros(3)
        e[i] = 1.0
        Ei = _skew(e)
        s = r[i] / th if th > 1e-12 else 0.0
        dR[i] = dA * s * K + A * Ei + dB * s * K2 + B * (Ei @ K + K @ Ei)
    return R, dR


# --------------------------------------------------------------------------
# robust losses (SciPy's rho convention; weights are convention-independent)
# --------------------------------------------------------------------------

def _rho(z: np.ndarray, loss: str, delta: float):
    """Cost, first and second derivative of ``rho`` (SciPy definitions)."""
    a = np.abs(z)
    if loss in ("none", "linear"):
        return z * z, 2.0 * z, np.full_like(z, 2.0)
    if loss == "huber":
        inner = a <= delta
        rho = np.where(inner, z * z, 2.0 * delta * a - delta * delta)
        psi = np.where(inner, 2.0 * z, 2.0 * delta * np.sign(z))
        phi = np.where(inner, 2.0, 0.0)
        return rho, psi, phi
    if loss == "cauchy":
        d2 = delta * delta
        rho = d2 * np.log1p((z / delta) ** 2)
        psi = 2.0 * z / (1.0 + (z / delta) ** 2)
        phi = 2.0 * (1.0 - (z / delta) ** 2) / (1.0 + (z / delta) ** 2) ** 2
        return rho, psi, phi
    if loss == "soft_l1":
        d2 = delta * delta
        s = np.sqrt(1.0 + (z / delta) ** 2)
        rho = 2.0 * d2 * (s - 1.0)
        psi = 2.0 * z / s
        phi = 2.0 / s ** 3
        return rho, psi, phi
    raise ValueError(f"unknown loss {loss!r}; use one of {LOSSES}")


def _irls_weight(z: np.ndarray, loss: str, delta: float) -> np.ndarray:
    """``psi(z)/z`` — the IRLS weight SciPy applies to each residual."""
    if loss in ("none", "linear"):
        return np.ones_like(z)
    a = np.abs(z)
    if loss == "huber":
        return np.where(a <= delta, 1.0, delta / np.maximum(a, 1e-12))
    if loss == "cauchy":
        return 1.0 / (1.0 + (z / delta) ** 2)
    if loss == "soft_l1":
        return 1.0 / np.sqrt(1.0 + (z / delta) ** 2)
    raise ValueError(f"unknown loss {loss!r}")


# --------------------------------------------------------------------------
# problem definition
# --------------------------------------------------------------------------

@dataclass
class Problem:
    """One joint calibration problem, independent of the backend.

    ``observations`` are ``calibrate.Observation`` objects; view ids are
    renumbered to a compact range exactly like ``calibrate.calibrate`` does.
    ``rows`` is the flat residual layout: ``(cam_index, view_index, point_idx,
    uv)``, one entry per (camera, view) detection group.
    """

    observations: list
    pitch_mm: float = plate.DEFAULT_PITCH_MM
    init_cameras: Optional[dict] = None
    obj: Optional[np.ndarray] = None
    image_size: tuple = (1280, 1024)
    rows: list = field(default_factory=list)
    n_views: int = 0
    x0: Optional[np.ndarray] = None
    packer: Optional[_Packer] = None
    names: list = field(default_factory=list)

    # -- construction ----------------------------------------------------
    @property
    def n(self) -> int:
        return self.packer.n

    @property
    def m(self) -> int:
        return int(sum(2 * len(r[3]) for r in self.rows))

    def build(self) -> "Problem":
        if not self.observations:
            raise ValueError("no observations")
        used = sorted({int(o.view) for o in self.observations})
        remap = {v: i for i, v in enumerate(used)}
        obs = [Observation(o.camera, remap[int(o.view)], o.uv)
               for o in self.observations]
        self.observations = obs
        self.n_views = len(used)
        self.view_ids = used
        self.view_remap = remap
        if self.obj is None:
            self.obj = plate.board_points_mm(self.pitch_mm)
        self.obj = np.asarray(self.obj, float)
        if self.init_cameras is None:
            self.init_cameras = {c: CameraIntrinsics(1000.0, 1000.0, 640.0,
                                                    512.0, np.zeros(5))
                                 for c in CAMERAS}
        uv_index = engine._uv_index()
        self.rows = []
        for o in obs:
            idx = [uv_index[k] for k in o.uv if k in uv_index]
            if not idx:
                continue
            uv = np.array([o.uv[tuple(plate.lattice_uv()[i])] for i in idx], float)
            self.rows.append((CAMERAS.index(o.camera), int(o.view),
                              np.array(idx, int), uv))
        self.packer = _Packer(self.n_views, True)
        self.names = self.packer.names()
        # block column indices (full parameter space) for the analytic Jacobian
        self._cols_of_row = []
        for cam, view, idx, uv in self.rows:
            cols = list(range(9 * cam, 9 * cam + 9))
            if cam != 0:
                cols += list(range(27 + 6 * (cam - 1), 27 + 6 * cam))
            cols += list(range(39 + 6 * view, 39 + 6 * view + 6))
            self._cols_of_row.append(np.array(cols, int))
        return self

    def start(self, free_kc=True, free_intrinsics: bool = True,
              fix_left_intrinsics: bool = False) -> np.ndarray:
        """The engine's own initial guess (OpenCV intrinsics + PnP rig/poses).

        Cached per distortion-model choice, because that choice changes whether
        OpenCV estimates the distortion coefficients during initialisation.
        """
        key = bool(np.any(_kc_mask(free_kc)))
        if self.x0 is not None and getattr(self, "_x0_key", None) == key:
            return self.x0.copy()
        cams0 = engine._initial_intrinsics(self.observations, self.init_cameras,
                                          self.obj, key)
        views0, extr0 = engine._initial_poses(self.observations, cams0, self.obj)
        self.x0 = _pack_initial(cams0, extr0, views0, self.packer)
        self._x0_key = key
        return self.x0.copy()

    # -- parameter masks -------------------------------------------------
    def free_mask(self, free_kc=True, free_intrinsics: bool = True,
                  fix_left_intrinsics: bool = False) -> np.ndarray:
        mask = np.ones(self.packer.n, bool)
        kc = _kc_mask(free_kc)
        for i, c in enumerate(CAMERAS):
            base = 9 * i
            if fix_left_intrinsics and c == "left":
                # the gauge camera's whole intrinsic block is held
                mask[base:base + 9] = False
                continue
            if not free_intrinsics:
                mask[base:base + 4] = False
            mask[base + 4:base + 9] = kc
        return mask

    # -- evaluation ------------------------------------------------------
    def _poses(self, x: np.ndarray):
        Rv = np.empty((self.n_views, 3, 3))
        dRv = np.empty((self.n_views, 3, 3, 3))
        tv = np.empty((self.n_views, 3))
        for v in range(self.n_views):
            p = x[39 + 6 * v: 45 + 6 * v]
            Rv[v], dRv[v] = rodrigues_dR(p[:3])
            tv[v] = p[3:6]
        Rc = [np.eye(3)]
        dRc = [np.zeros((3, 3, 3))]
        tc = [np.zeros(3)]
        for c in (1, 2):
            R, dR = rodrigues_dR(x[27 + 6 * (c - 1): 30 + 6 * (c - 1)])
            Rc.append(R)
            dRc.append(dR)
            tc.append(x[30 + 6 * (c - 1): 33 + 6 * (c - 1)])
        return x[:27].reshape(3, 9), Rv, tv, dRv, Rc, tc, dRc

    def evaluate(self, x: np.ndarray, free_idx: Optional[np.ndarray] = None):
        """Residual vector (m,) and, if ``free_idx`` is given, dense Jacobian."""
        cams, Rv, tv, dRv, Rc, tc, dRc = self._poses(x)
        want_j = free_idx is not None
        nf = len(free_idx) if want_j else 0
        colmap = np.full(self.n, -1, int)
        if want_j:
            colmap[free_idx] = np.arange(nf)
        r = np.empty(self.m)
        J = np.zeros((self.m, nf)) if want_j else None
        r0 = 0
        for b, (cam, view, idx, uv) in enumerate(self.rows):
            X = self.obj[idx]
            Xl = X @ Rv[view].T + tv[view]
            Xc = Xl @ Rc[cam].T + tc[cam]
            fx, fy, cx, cy, k1, k2, p1, p2, k3 = cams[cam]
            z = Xc[:, 2]
            iz = 1.0 / z
            xd_ = Xc[:, 0] * iz
            yd_ = Xc[:, 1] * iz
            r2 = xd_ * xd_ + yd_ * yd_
            rad = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
            dr2 = k1 + 2.0 * k2 * r2 + 3.0 * k3 * r2 ** 2
            xd = xd_ * rad + 2.0 * p1 * xd_ * yd_ + p2 * (r2 + 2.0 * xd_ * xd_)
            yd = yd_ * rad + p1 * (r2 + 2.0 * yd_ * yd_) + 2.0 * p2 * xd_ * yd_
            k = len(idx)
            rr = np.empty((k, 2))
            rr[:, 0] = fx * xd + cx - uv[:, 0]
            rr[:, 1] = fy * yd + cy - uv[:, 1]
            r[r0:r0 + 2 * k] = rr.ravel()
            if not want_j:
                r0 += 2 * k
                continue
            dxd_dx = rad + xd_ * 2.0 * xd_ * dr2 + 2.0 * p1 * yd_ + 6.0 * p2 * xd_
            dxd_dy = xd_ * 2.0 * yd_ * dr2 + 2.0 * p1 * xd_ + 2.0 * p2 * yd_
            dyd_dx = yd_ * 2.0 * xd_ * dr2 + 2.0 * p1 * xd_ + 2.0 * p2 * yd_
            dyd_dy = rad + yd_ * 2.0 * yd_ * dr2 + 6.0 * p1 * yd_ + 2.0 * p2 * xd_
            duv = np.empty((k, 2, 3))
            duv[:, 0, 0] = fx * dxd_dx * iz
            duv[:, 0, 1] = fx * dxd_dy * iz
            duv[:, 0, 2] = -(fx * (dxd_dx * xd_ + dxd_dy * yd_)) * iz
            duv[:, 1, 0] = fy * dyd_dx * iz
            duv[:, 1, 1] = fy * dyd_dy * iz
            duv[:, 1, 2] = -(fy * (dyd_dx * xd_ + dyd_dy * yd_)) * iz
            cols_full = self._cols_of_row[b]
            cols = colmap[cols_full]
            keep = cols >= 0
            cols = cols[keep]
            ncf = len(cols_full)
            blk = np.zeros((2 * k, ncf))
            # intrinsics: fx fy cx cy k1 k2 p1 p2 k3
            blk[0::2, 0] = xd
            blk[1::2, 1] = yd
            blk[0::2, 2] = 1.0
            blk[1::2, 3] = 1.0
            dxd_dk = np.stack([xd_ * r2, xd_ * r2 ** 2, 2.0 * xd_ * yd_,
                               r2 + 2.0 * xd_ * xd_, xd_ * r2 ** 3], axis=1)
            dyd_dk = np.stack([yd_ * r2, yd_ * r2 ** 2,
                               r2 + 2.0 * yd_ * yd_, 2.0 * xd_ * yd_,
                               yd_ * r2 ** 3], axis=1)
            blk[0::2, 4:9] = fx * dxd_dk
            blk[1::2, 4:9] = fy * dyd_dk
            off = 9
            if cam != 0:
                dXc_dr = np.einsum("icq,kq->ikc", dRc[cam], Xl)
                for i in range(3):
                    blk[0::2, off + i] = np.einsum("kc,kc->k", duv[:, 0, :],
                                                   dXc_dr[i])
                    blk[1::2, off + i] = np.einsum("kc,kc->k", duv[:, 1, :],
                                                   dXc_dr[i])
                blk[0::2, off + 3:off + 6] = duv[:, 0, :]
                blk[1::2, off + 3:off + 6] = duv[:, 1, :]
                off += 6
            dXl_dv = np.einsum("icq,kq->ikc", dRv[view], X)
            dXc_dv = np.einsum("rc,ikc->ikr", Rc[cam], dXl_dv)
            for i in range(3):
                blk[0::2, off + i] = np.einsum("kc,kc->k", duv[:, 0, :],
                                               dXc_dv[i])
                blk[1::2, off + i] = np.einsum("kc,kc->k", duv[:, 1, :],
                                               dXc_dv[i])
            blk[0::2, off + 3:off + 6] = duv[:, 0, :] @ Rc[cam]
            blk[1::2, off + 3:off + 6] = duv[:, 1, :] @ Rc[cam]
            if keep.any():
                J[r0:r0 + 2 * k][:, cols] = blk[:, keep]
            r0 += 2 * k
        return r, J

    def residuals(self, x: np.ndarray) -> np.ndarray:
        return self.evaluate(x, None)[0]

    def jacobian(self, x: np.ndarray, free_idx: np.ndarray) -> np.ndarray:
        return self.evaluate(x, free_idx)[1]

    # -- unpacking -------------------------------------------------------
    def unpack(self, x: np.ndarray) -> tuple[dict, dict, list]:
        cams = {}
        for i, c in enumerate(CAMERAS):
            v = x[9 * i: 9 * i + 9]
            cams[c] = CameraIntrinsics(float(v[0]), float(v[1]), float(v[2]),
                                       float(v[3]), v[4:9].copy())
        extr = {"left": (np.eye(3), np.zeros(3))}
        for c, s in (("right", 27), ("tex", 33)):
            extr[c] = (rodrigues(x[s:s + 3]), x[s + 3:s + 6].copy())
        views = [(rodrigues(x[39 + 6 * v: 42 + 6 * v]),
                  x[42 + 6 * v: 45 + 6 * v].copy()) for v in range(self.n_views)]
        return cams, extr, views


def make_problem(observations: Sequence[Observation],
                 pitch_mm: float = plate.DEFAULT_PITCH_MM,
                 init_cameras: Optional[Mapping[str, CameraIntrinsics]] = None,
                 obj: Optional[np.ndarray] = None,
                 image_size: tuple = (1280, 1024)) -> Problem:
    """Build a :class:`Problem` from ``calibrate.Observation`` objects."""
    return Problem(list(observations), float(pitch_mm),
                   dict(init_cameras) if init_cameras is not None else None,
                   obj, tuple(image_size)).build()


# --------------------------------------------------------------------------
# priors
# --------------------------------------------------------------------------

def _prior_spec(problem: Problem, priors, x0: np.ndarray,
                free_idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resolve a prior spec into (free positions, means, sigmas).

    ``priors`` is ``None``, or a mapping with any of

    ``{"intrinsics": sigma}``   fx, fy, cx, cy of every camera
    ``{"distortion": sigma}``   k1, k2, p1, p2, k3 of every camera
    ``{"extrinsics": sigma}``   rig rvec/t of right and tex
    ``{"views": sigma}``        per-view rvec/t (rarely wanted)
    ``{"parameters": {name: sigma | (mean, sigma)}}``

    A prior mean defaults to the initial value at that parameter (i.e. the CCF
    record on the vendor corpus, the OpenCV/PnP start otherwise).
    """
    if not priors:
        return (np.zeros(0, int), np.zeros(0), np.zeros(0))
    names = problem.names
    name_to_full = {n: i for i, n in enumerate(names)}
    full_to_free = {int(f): k for k, f in enumerate(free_idx)}
    want: dict[str, tuple[float, float]] = {}

    def add(prefixes, sigma, mean=None):
        for n in names:
            if any(n.startswith(p) for p in prefixes):
                want[n] = (float(mean) if mean is not None else None, float(sigma))

    if "intrinsics" in priors:
        v = priors["intrinsics"]
        sig = v if np.isscalar(v) else v.get("sigma")
        add([f"{c}.{f}" for c in CAMERAS for f in ("fx", "fy", "cx", "cy")], sig)
    if "distortion" in priors:
        v = priors["distortion"]
        sig = v if np.isscalar(v) else v.get("sigma")
        add([f"{c}.{f}" for c in CAMERAS for f in ("k1", "k2", "p1", "p2", "k3")],
            sig)
    if "extrinsics" in priors:
        v = priors["extrinsics"]
        sig = v if np.isscalar(v) else v.get("sigma")
        add([f"{c}.{f}" for c in ("right", "tex")
             for f in ("rx", "ry", "rz", "tx", "ty", "tz")], sig)
    if "views" in priors:
        v = priors["views"]
        sig = v if np.isscalar(v) else v.get("sigma")
        add(["view"], sig)
    for n, v in (priors.get("parameters") or {}).items():
        if isinstance(v, (tuple, list)):
            want[n] = (float(v[0]), float(v[1]))
        else:
            want[n] = (None, float(v))

    pos, mean, sig = [], [], []
    for n, (mu, s) in want.items():
        if n not in name_to_full or s is None or not np.isfinite(s) or s <= 0:
            continue
        full = name_to_full[n]
        if full not in full_to_free:
            continue
        pos.append(full_to_free[full])
        mean.append(mu if mu is not None else float(x0[full]))
        sig.append(s)
    if not pos:
        return (np.zeros(0, int), np.zeros(0), np.zeros(0))
    order = np.argsort(pos)
    return (np.asarray(pos, int)[order], np.asarray(mean)[order],
            np.asarray(sig)[order])


def _prior_info(priors) -> dict:
    if not priors:
        return {}
    out = {}
    for k, v in priors.items():
        if k == "parameters":
            out[k] = {n: (v2 if np.isscalar(v2) else list(v2))
                      for n, v2 in v.items()}
        else:
            out[k] = v if np.isscalar(v) else dict(v)
    return out


# --------------------------------------------------------------------------
# covariance
# --------------------------------------------------------------------------

def _sigma_from_residuals(r: np.ndarray) -> float:
    if len(r) == 0:
        return 1.0
    mad = 1.4826 * np.median(np.abs(r - np.median(r)))
    return max(float(mad), 1e-3)


def covariance_from_jacobian(J: np.ndarray, r: np.ndarray, free_idx: np.ndarray,
                            n_full: int, robust: str = "huber",
                            f_scale: float = 1.0,
                            estimator: str = "sandwich",
                            prior_pos: Optional[np.ndarray] = None,
                            prior_sigma: Optional[np.ndarray] = None,
                            rcond: float = 1e-10) -> tuple[np.ndarray, dict]:
    """Covariance of the free parameters from an analytic Jacobian.

    ``estimator="sandwich"`` is the engine's default (Huber sandwich
    ``A^-1 B A^-1`` with ``psi(r) = w r``); ``"gauss_newton"`` is
    ``sigma_MAD^2 (J^T J)^-1``.  Prior rows enter ``A`` only: they are treated
    as deterministic regularisation, so they do not contribute to the meat.
    """
    m, nf = J.shape
    delta = float(f_scale)
    if robust in ("none", "linear") or delta <= 0:
        w = np.ones(m)
        psi_r = r.copy()
    else:
        w = _irls_weight(r / delta, robust, 1.0) if robust else np.ones(m)
        psi_r = w * r
    Jw = J * np.sqrt(w)[:, None]
    rows = [Jw]
    if prior_pos is not None and len(prior_pos):
        P = np.zeros((len(prior_pos), nf))
        P[np.arange(len(prior_pos)), prior_pos] = 1.0 / prior_sigma
        rows.append(P)
    Jaug = np.vstack(rows) if len(rows) > 1 else Jw
    U, S, Vt = np.linalg.svd(Jaug, full_matrices=False)
    smax = float(S[0]) if len(S) else 0.0
    keep = S > max(smax * rcond, 1e-15)
    Ainv = (Vt[keep].T / (S[keep] ** 2)) @ Vt[keep]
    info = {
        "n_residuals": int(m), "n_free": int(nf),
        "rank": int(keep.sum()), "condition": float(smax / S[keep][-1])
        if keep.any() else float("inf"),
        "sigma_px": _sigma_from_residuals(r), "robust": robust,
        "rcond": float(rcond),
        "n_prior_rows": int(0 if prior_pos is None else len(prior_pos)),
    }
    if estimator == "sandwich" and robust not in ("none", "linear"):
        Jwr = J * psi_r[:, None]
        B = Jwr.T @ Jwr
        cov = Ainv @ B @ Ainv
        info["estimator"] = "robust_sandwich"
    elif estimator == "sandwich":
        Jwr = J * r[:, None]
        B = Jwr.T @ Jwr
        cov = Ainv @ (B * 1.0) @ Ainv
        info["estimator"] = "empirical_sandwich"
    else:
        cov = Ainv * info["sigma_px"] ** 2
        info["estimator"] = "gauss_newton"
    cov = 0.5 * (cov + cov.T)
    wv, Vc = np.linalg.eigh(cov)
    if wv.size and wv.min() < -1e-12 * max(1.0, wv.max()):
        cov = (Vc * np.clip(wv, 0.0, None)) @ Vc.T
        cov = 0.5 * (cov + cov.T)
    info["eig_min"] = float(np.linalg.eigvalsh(cov).min()) if cov.size else 0.0
    full = np.zeros((n_full, n_full))
    full[np.ix_(free_idx, free_idx)] = cov
    info["trace"] = float(np.trace(cov))
    # ---- information matrices, for calib.adaptive / calib.objective
    # A_free = V S^2 V^T reproduces the (weighted, prior-augmented) normal matrix
    # from the SVD already performed; the rig marginal information is its Schur
    # complement over the board poses (identical to inv of the rig block of
    # A^-1, and what calibrate.covariance reports).
    A_free = (Vt[keep].T * (S[keep] ** 2)) @ Vt[keep]
    information = np.zeros((n_full, n_full))
    information[np.ix_(free_idx, free_idx)] = A_free
    n_rig = int(np.sum(free_idx < NRIG))
    marginal = np.zeros((NRIG, NRIG))
    rig_free = free_idx[free_idx < NRIG]
    if n_rig:
        if n_rig < nf:
            Arr = A_free[:n_rig, :n_rig]
            Arv = A_free[:n_rig, n_rig:]
            Avv = A_free[n_rig:, n_rig:]
            try:
                schur = Arr - Arv @ np.linalg.pinv(Avv, rcond=1e-12) @ Arv.T
            except np.linalg.LinAlgError:
                schur = Arr
        else:
            schur = A_free[:n_rig, :n_rig]
        marginal[np.ix_(rig_free, rig_free)] = 0.5 * (schur + schur.T)
    info["information"] = information
    info["marginal_information"] = marginal
    return full, info


# --------------------------------------------------------------------------
# derived quantities: baseline, relative rotation, 3-D point
# --------------------------------------------------------------------------

def _baseline_vec(x: np.ndarray) -> np.ndarray:
    return np.asarray(x[30:33], float)


def baseline_mm(x: np.ndarray) -> float:
    return float(np.linalg.norm(_baseline_vec(x)))


def relative_rotation_deg(x: np.ndarray) -> float:
    """Angle of ``R_right @ R_left^T`` (left is the identity gauge)."""
    rv = rvec_from_R(rodrigues(x[27:30]))
    return float(np.degrees(np.linalg.norm(rv)))


def _undistort_normalized(u: float, v: float, K: np.ndarray,
                          kc: np.ndarray) -> np.ndarray:
    """Invert the Brown-Conrady model by Newton (deterministic, smooth)."""
    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[0, 2], K[1, 2]
    xd = (u - cx) / fx
    yd = (v - cy) / fy
    k1, k2, p1, p2, k3 = np.asarray(kc, float).ravel()[:5]
    x, y = xd, yd
    for _ in range(30):
        r2 = x * x + y * y
        rad = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        dr2 = k1 + 2.0 * k2 * r2 + 3.0 * k3 * r2 ** 2
        gx = x * rad + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x) - xd
        gy = y * rad + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y - yd
        a11 = rad + x * 2.0 * x * dr2 + 2.0 * p1 * y + 4.0 * p2 * x
        a12 = x * 2.0 * y * dr2 + 2.0 * p1 * x + 2.0 * p2 * y
        a21 = y * 2.0 * x * dr2 + 2.0 * p1 * x + 2.0 * p2 * y
        a22 = rad + y * 2.0 * y * dr2 + 4.0 * p1 * y + 2.0 * p2 * x
        det = a11 * a22 - a12 * a21
        if abs(det) < 1e-18:
            break
        dx = (a22 * gx - a12 * gy) / det
        dy = (-a21 * gx + a11 * gy) / det
        x -= dx
        y -= dy
        if max(abs(dx), abs(dy)) < 1e-14:
            break
    return np.array([x, y])


def _projection_matrix(cams: dict, extr: dict, cam: str,
                       normalized: bool = True) -> np.ndarray:
    """``[R|t]`` (normalized image coords) or ``K[R|t]`` (pixels)."""
    R, t = extr[cam]
    P = np.hstack([np.asarray(R, float), np.asarray(t, float).reshape(3, 1)])
    if normalized:
        return P
    return np.asarray(cams[cam].K, float) @ P


def reconstruct_point(x: np.ndarray, depth_mm: float = 450.0,
                      problem: Optional[Problem] = None,
                      source_x: Optional[np.ndarray] = None,
                      lateral=(0.0, 0.0),
                      noise_px: float = 0.0,
                      rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """Triangulate the test point ``(lx, ly, depth)`` in the left camera frame.

    The point is projected into the left and right cameras through
    ``source_x`` (the *true* calibration by default: ``source_x=x``), optionally
    with Gaussian pixel noise, and re-triangulated with the rig in ``x``.  With
    ``source_x is x`` and no noise the result is the point itself; the
    sensitivity of this map to ``x`` is what a calibration error does to a
    reconstructed point at that depth (see :func:`point_sigma`).
    """
    if problem is not None:
        cams, extr, _ = problem.unpack(x)
    else:
        cams, extr = _unpack_rig(x)
    if source_x is None:
        src_cams, src_extr = cams, extr
    elif problem is not None:
        src_cams, src_extr, _ = problem.unpack(source_x)
    else:
        src_cams, src_extr = _unpack_rig(source_x)
    P = np.array([float(lateral[0]), float(lateral[1]), float(depth_mm)])
    uv = {}
    for cam in ("left", "right"):
        Rs, ts = src_extr[cam]
        Xc = Rs @ P + ts
        uv[cam] = project(src_cams[cam].K, src_cams[cam].kc,
                          Xc.reshape(1, 3))[0].astype(float)
        if noise_px > 0.0:
            if rng is None:
                raise ValueError("noise_px>0 needs an rng")
            uv[cam] = uv[cam] + rng.normal(0.0, noise_px, 2)
    rays = {cam: _undistort_normalized(uv[cam][0], uv[cam][1], cams[cam].K,
                                       cams[cam].kc)
            for cam in ("left", "right")}
    # linear triangulation (DLT) in the left frame
    A = []
    for cam in ("left", "right"):
        Pp = _projection_matrix(cams, extr, cam)
        xn = np.array([rays[cam][0], rays[cam][1], 1.0])
        A.append(xn[0] * Pp[2] - Pp[0])
        A.append(xn[1] * Pp[2] - Pp[1])
    A = np.vstack(A)
    _, _, Vt = np.linalg.svd(A)
    Ph = Vt[-1]
    if abs(Ph[3]) < 1e-15:
        return Ph[:3]
    return Ph[:3] / Ph[3]


def reconstruction_error(x: np.ndarray, source_x: Optional[np.ndarray] = None,
                         depths: Sequence[float] = (200, 300, 400, 500, 600),
                         lateral_half_mm: float = 60.0,
                         noise_px: float = 0.0,
                         rng: Optional[np.random.Generator] = None,
                         problem: Optional[Problem] = None) -> dict:
    """RMS 3-D reconstruction error over a lateral grid at each depth.

    ``source_x`` is the truth used to synthesise the pixel correspondences;
    ``x`` is the calibration used to triangulate.  Reported per depth: the 3-D
    RMS error over the 3x3 lateral grid, its depth (z) component, and the
    lateral (xy) component — all in mm.
    """
    lat = np.linspace(-lateral_half_mm, lateral_half_mm, 3)
    out = {}
    for d in depths:
        errs = []
        for lx in lat:
            for ly in lat:
                P = reconstruct_point(x, float(d), problem,
                                      source_x=source_x, lateral=(lx, ly),
                                      noise_px=noise_px, rng=rng)
                errs.append(P - np.array([lx, ly, float(d)]))
        errs = np.asarray(errs)
        out[float(d)] = {
            "rms_3d_mm": float(np.sqrt(np.mean(np.sum(errs ** 2, axis=1)))),
            "rms_z_mm": float(np.sqrt(np.mean(errs[:, 2] ** 2))),
            "rms_xy_mm": float(np.sqrt(np.mean(np.sum(errs[:, :2] ** 2, axis=1)))),
            "bias_z_mm": float(np.mean(errs[:, 2])),
            "bias_xy_mm": float(np.sqrt(np.mean(np.sum(errs[:, :2] ** 2, axis=1))))}
    return out


def _unpack_rig(x: np.ndarray) -> tuple[dict, dict]:
    cams = {}
    for i, c in enumerate(CAMERAS):
        v = x[9 * i: 9 * i + 9]
        cams[c] = CameraIntrinsics(float(v[0]), float(v[1]), float(v[2]),
                                   float(v[3]), v[4:9].copy())
    extr = {"left": (np.eye(3), np.zeros(3))}
    for c, s in (("right", 27), ("tex", 33)):
        extr[c] = (rodrigues(x[s:s + 3]), np.asarray(x[s + 3:s + 6], float))
    return cams, extr


def _numeric_jac(fn, x: np.ndarray, idx, h: float = 1e-6) -> np.ndarray:
    f0 = np.asarray(fn(x), float).ravel()
    J = np.zeros((len(f0), len(idx)))
    for j, k in enumerate(idx):
        hh = h * max(1.0, abs(x[k]))
        xp = x.copy()
        xm = x.copy()
        xp[k] += hh
        xm[k] -= hh
        J[:, j] = (np.asarray(fn(xp), float).ravel() -
                   np.asarray(fn(xm), float).ravel()) / (2.0 * hh)
    return J


def point_sigma(x: np.ndarray, cov: Optional[np.ndarray], depth_mm: float = 450.0,
                problem: Optional[Problem] = None) -> dict:
    """Delta-method sigma of the triangulated test point (mm)."""
    if cov is None:
        return {}
    Cr = np.asarray(cov, float)[:NRIG, :NRIG]
    idx = list(range(27)) + list(range(27, 39))
    xs = np.asarray(x, float).copy()   # frozen correspondence source
    J = _numeric_jac(lambda z: reconstruct_point(z, depth_mm, problem,
                                                 source_x=xs), x, idx, h=1e-6)
    Cp = J @ Cr[np.ix_(idx, idx)] @ J.T
    sd = np.sqrt(np.maximum(np.diag(Cp), 0.0))
    return {"depth_mm": float(depth_mm), "sigma_x_mm": float(sd[0]),
            "sigma_y_mm": float(sd[1]), "sigma_z_mm": float(sd[2]),
            "sigma_total_mm": float(np.sqrt(np.sum(sd ** 2))),
            "sigma_radial_mm": float(np.hypot(sd[0], sd[1]))}


def _derived(x: np.ndarray, cov: Optional[np.ndarray],
             depths=(300.0, 450.0, 600.0),
             problem: Optional[Problem] = None) -> dict:
    out = {"baseline_mm": baseline_mm(x),
           "relative_rotation_deg": relative_rotation_deg(x)}
    if cov is None:
        out["points"] = {}
        return out
    Cr = np.asarray(cov, float)[:NRIG, :NRIG]
    # baseline: |t_right|
    t = _baseline_vec(x)
    nb = float(np.linalg.norm(t))
    jb = t / nb if nb > 0 else np.zeros(3)
    sd_b = float(np.sqrt(max(jb @ Cr[30:33, 30:33] @ jb, 0.0)))
    # relative rotation angle: numerical jacobian over the right rvec
    jr = _numeric_jac(lambda z: np.array([relative_rotation_deg(z)]), x,
                      [27, 28, 29], h=1e-6)[0]
    sd_r = float(np.sqrt(max(jr @ Cr[27:30, 27:30] @ jr, 0.0)))
    out.update({"baseline_sigma_mm": sd_b,
                "relative_rotation_sigma_deg": sd_r,
                "points": {float(d): point_sigma(x, cov, d, problem)
                           for d in depths}})
    return out


# --------------------------------------------------------------------------
# internal Levenberg-Marquardt (IRLS weights, exact prior handling)
# --------------------------------------------------------------------------

def _solve_lm(problem: Problem, x0: np.ndarray, free_idx: np.ndarray,
              robust: str, f_scale: float,
              prior_pos: np.ndarray, prior_mean: np.ndarray,
              prior_sigma: np.ndarray,
              max_iter: int = 200, tol: float = 1e-10) -> dict:
    x = x0.copy()
    nf = len(free_idx)
    npr = len(prior_pos)
    delta = float(f_scale) if f_scale > 0 else 1.0

    def eval_all(xx):
        r, J = problem.evaluate(xx, free_idx)
        cost = float(np.sum(_rho(r / delta, robust, 1.0)[0]))
        rows = [J]
        rr = [r]
        if npr:
            pr = (xx[free_idx][prior_pos] - prior_mean) / prior_sigma
            rows.append(np.eye(nf)[prior_pos] / prior_sigma[:, None])
            rr.append(pr)
            cost += float(pr @ pr)
        return np.concatenate(rr), np.vstack(rows), cost

    r, J, cost = eval_all(x)
    lam = 1e-4
    nu = 2.0
    nit = 0
    status = "max_iter"
    for nit in range(1, max_iter + 1):
        w = np.ones(len(r))
        if robust not in ("none", "linear"):
            w[:len(r) - npr] = _irls_weight(r[:len(r) - npr] / delta, robust, 1.0)
        Jw = J * np.sqrt(w)[:, None]
        rw = r * np.sqrt(w)
        A = Jw.T @ Jw
        g = Jw.T @ rw
        if np.max(np.abs(g)) < tol:
            status = "converged_gradient"
            break
        accepted = False
        for _ in range(20):
            Ad = A + lam * np.diag(np.maximum(np.diag(A), 1e-12))
            try:
                step = -np.linalg.solve(Ad, g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            xt = x.copy()
            xt[free_idx] += step
            rt, Jt, ct = eval_all(xt)
            if np.isfinite(ct) and ct < cost:
                rel = (cost - ct) / max(cost, 1e-300)
                x, r, J, cost = xt, rt, Jt, ct
                lam = max(lam / 3.0, 1e-12)
                accepted = True
                break
            lam *= 10.0
            if lam > 1e16:
                break
        if not accepted:
            status = "no_descent"
            break
        if np.max(np.abs(step)) < tol:
            status = "converged_step"
            break
        if rel < 1e-14:
            status = "converged_cost"
            break
    r, J, cost = eval_all(x)
    w = np.ones(len(r))
    if robust not in ("none", "linear"):
        w[:len(r) - npr] = _irls_weight(r[:len(r) - npr] / delta, robust, 1.0)
    Jw = J * np.sqrt(w)[:, None]
    g_final = Jw.T @ (r * np.sqrt(w))
    return {"x": x, "residuals": r[:problem.m], "cost": cost, "nit": nit,
            "status": status, "nfev": nit,
            "grad_inf": float(np.max(np.abs(g_final))) if len(g_final) else 0.0}


def _refine_poses(problem: Problem, x: np.ndarray, max_iter: int = 40,
                  robust: str = "none", f_scale: float = 1.0) -> np.ndarray:
    """Re-optimise only the per-view poses, holding the rig fixed.

    A parameter-only estimate (e.g. OpenCV's independent per-camera fits) must
    be scored with poses that are optimal *for those parameters*; otherwise the
    comparison measures the pose initialiser, not the calibration.  This is the
    same joint cost with the 39 rig parameters held.
    """
    free_idx = np.arange(39, problem.n)
    npr = np.zeros(0, int)
    out = _solve_lm(problem, np.asarray(x, float).copy(), free_idx,
                    robust, f_scale, npr, np.zeros(0), np.zeros(0),
                    max_iter=max_iter)
    return out["x"]


# --------------------------------------------------------------------------
# native backend
# --------------------------------------------------------------------------

def _native_available() -> bool:
    return NATIVE_BINARY.is_file() and os.access(NATIVE_BINARY, os.X_OK)


def _native_body(problem: Problem) -> list[str]:
    """Static part of the text problem (points and observations), cached."""
    body = getattr(problem, "_native_body_cache", None)
    if body is not None:
        return body
    body = []
    for i, X in enumerate(problem.obj):
        body.append("point %d %.17g %.17g %.17g" % (i, X[0], X[1], X[2]))
    for cam, view, idx, uv in problem.rows:
        for j in range(len(idx)):
            body.append("obs %d %d %d %.17g %.17g"
                        % (cam, view, int(idx[j]), uv[j, 0], uv[j, 1]))
    problem._native_body_cache = body
    return body


def _write_native_problem(path: str, problem: Problem, x: np.ndarray,
                          huber_delta: float, free_distortion: bool) -> None:
    lines = ["# einstar calibration problem v1 (written by calib.solvers)",
             f"n_cameras {len(CAMERAS)}",
             f"n_views {problem.n_views}",
             f"n_points {len(problem.obj)}",
             f"pitch_mm {problem.pitch_mm!r}",
             f"huber_delta {huber_delta!r}",
             f"free_distortion {1 if free_distortion else 0}"]
    for i in range(3):
        v = x[9 * i: 9 * i + 9]
        lines.append("intrinsics %d %s" % (i, " ".join(f"{t:.17g}" for t in v)))
    for i, s in enumerate((27, 33), start=1):
        v = x[s:s + 6]
        lines.append("extrinsics %d %s" % (i, " ".join(f"{t:.17g}" for t in v)))
    for v in range(problem.n_views):
        p = x[39 + 6 * v: 45 + 6 * v]
        lines.append("view %d %s" % (v, " ".join(f"{t:.17g}" for t in p)))
    lines.extend(_native_body(problem))
    Path(path).write_text("\n".join(lines) + "\n")


def _run_native(problem: Problem, x0: np.ndarray, huber_delta: float,
                free_distortion: bool, max_iter: int = 100,
                timeout_s: float = 60.0) -> dict:
    exe = NATIVE_BINARY
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="einstar_native_")
    os.close(fd)
    try:
        _write_native_problem(path, problem, x0, huber_delta, free_distortion)
        t0 = time.perf_counter()
        proc = subprocess.run([str(exe), path, "--max-iter", str(int(max_iter))],
                              capture_output=True, text=True,
                              timeout=float(timeout_s))
        wall = time.perf_counter() - t0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(f"{exe.name} exit {proc.returncode}: "
                           f"{(proc.stderr or '')[-400:]}")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"{exe.name} produced no output")
    out = json.loads(lines[-1])
    out["wall_s"] = wall
    out["binary"] = exe.name
    return out


# --------------------------------------------------------------------------
# OpenCV backend
# --------------------------------------------------------------------------

def _opencv_flags(free_kc, free_intrinsics: bool, fix_left: bool,
                  cam: str) -> int:
    flags = cv2.CALIB_USE_INTRINSIC_GUESS
    if not free_intrinsics or (fix_left and cam == "left"):
        flags |= (cv2.CALIB_FIX_FOCAL_LENGTH | cv2.CALIB_FIX_PRINCIPAL_POINT |
                  cv2.CALIB_FIX_K1 | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3 |
                  cv2.CALIB_ZERO_TANGENT_DIST)
        return flags
    mask = _kc_mask(free_kc)
    if not mask[0]:
        flags |= cv2.CALIB_FIX_K1
    if not mask[1]:
        flags |= cv2.CALIB_FIX_K2
    if not mask[2] or not mask[3]:
        flags |= cv2.CALIB_ZERO_TANGENT_DIST
    if not mask[4]:
        flags |= cv2.CALIB_FIX_K3
    return flags


def _solve_opencv(problem: Problem, x0: np.ndarray, free_kc, free_intrinsics: bool,
                  fix_left: bool) -> dict:
    per_cam = {}
    cam_std = {}
    obj = np.asarray(problem.obj, np.float32)
    for ci, cam in enumerate(CAMERAS):
        rows = [r for r in problem.rows if r[0] == ci]
        if len(rows) < 3:
            per_cam[cam] = {}
            continue
        op = [obj[r[2]] for r in sorted(rows, key=lambda z: z[1])]
        ip = [r[3].astype(np.float32) for r in sorted(rows, key=lambda z: z[1])]
        K0 = np.array([[x0[9 * ci], 0.0, x0[9 * ci + 2]],
                       [0.0, x0[9 * ci + 1], x0[9 * ci + 3]],
                       [0.0, 0.0, 1.0]])
        D0 = x0[9 * ci + 4: 9 * ci + 9].copy().reshape(1, 5)
        flags = _opencv_flags(free_kc, free_intrinsics, fix_left, cam)
        try:
            rms, K, D, rvecs, tvecs, sdi, sde, _ = cv2.calibrateCameraExtended(
                op, ip, problem.image_size, K0, D0, flags=flags)
        except cv2.error:
            per_cam[cam] = {}
            continue
        poses = {}
        for r, (rv, tv) in zip(sorted(rows, key=lambda z: z[1]), zip(rvecs, tvecs)):
            poses[int(r[1])] = (np.asarray(rv, float).ravel(),
                                np.asarray(tv, float).ravel())
        per_cam[cam] = {"K": np.asarray(K, float), "D": np.asarray(D, float).ravel()[:5],
                        "poses": poses, "rms": float(rms)}
        cam_std[cam] = np.asarray(sdi, float).ravel()
    # rig: stereoCalibrate (left,right) and (left,tex) with fixed intrinsics
    ex = {"right": (np.zeros(3), np.zeros(3)), "tex": (np.zeros(3), np.zeros(3))}
    ex_std = {}
    left = per_cam.get("left")
    for cam in ("right", "tex"):
        other = per_cam.get(cam)
        if not left or not other:
            continue
        common = sorted(set(left["poses"]) & set(other["poses"]))
        if len(common) < 3:
            continue
        # build per-view object/image point lists from the problem rows.
        # Partial detections differ per camera, so the two image lists must be
        # restricted to the points both cameras actually saw in that view.
        obj_l, img_l, img_r = [], [], []
        ci = CAMERAS.index(cam)
        for v in common:
            r0 = next(rr for rr in problem.rows if rr[1] == v and rr[0] == 0)
            r1 = next(rr for rr in problem.rows if rr[1] == v and rr[0] == ci)
            lut0 = {int(p): j for j, p in enumerate(r0[2])}
            lut1 = {int(p): j for j, p in enumerate(r1[2])}
            pts = sorted(set(lut0) & set(lut1))
            if len(pts) < 6:
                continue
            obj_l.append(obj[pts])
            img_l.append(r0[3][[lut0[p] for p in pts]].astype(np.float32))
            img_r.append(r1[3][[lut1[p] for p in pts]].astype(np.float32))
        if len(obj_l) < 3:
            continue
        Kl = left["K"].copy(); Dl = left["D"].copy()
        Kr = other["K"].copy(); Dr = other["D"].copy()
        try:
            (rms, Kl, Dl, Kr, Dr, R, T, E, F, _rv, _tv,
             _pve) = cv2.stereoCalibrateExtended(
                obj_l, img_l, img_r, Kl, Dl, Kr, Dr, problem.image_size,
                np.eye(3), np.zeros((3, 1)),
                flags=cv2.CALIB_FIX_INTRINSIC,
                criteria=(cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS, 200, 1e-12))
        except cv2.error:
            continue
        rv, _ = cv2.Rodrigues(R)
        ex[cam] = (rv.ravel(), np.asarray(T, float).ravel())
        ex_std[cam] = None
    # assemble the packed vector
    x = x0.copy()
    for ci, cam in enumerate(CAMERAS):
        pc = per_cam.get(cam)
        if not pc:
            continue
        x[9 * ci] = pc["K"][0, 0]
        x[9 * ci + 1] = pc["K"][1, 1]
        x[9 * ci + 2] = pc["K"][0, 2]
        x[9 * ci + 3] = pc["K"][1, 2]
        x[9 * ci + 4: 9 * ci + 9] = pc["D"]
    for i, cam in enumerate(("right", "tex")):
        s = 27 + 6 * i
        x[s:s + 3], x[s + 3:s + 6] = ex[cam]
    for v in range(problem.n_views):
        for cam in ("left", "right", "tex"):
            pc = per_cam.get(cam)
            if pc and v in pc["poses"]:
                rv, tv = pc["poses"][v]
                if cam != "left":
                    Rc, tc = rodrigues(ex[cam][0]), ex[cam][1]
                    rv = rvec_from_R(Rc @ rodrigues(rv))
                    tv = Rc @ tv + tc
                x[39 + 6 * v: 42 + 6 * v] = rv
                x[42 + 6 * v: 45 + 6 * v] = tv
                break
    return {"x": x, "per_camera_rms": {c: per_cam[c].get("rms")
                                       for c in per_cam if per_cam[c]},
            "per_camera_std": {c: cam_std[c].tolist() for c in cam_std},
            "rig_std": ex_std}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def solve_joint(problem: Problem | Sequence[Observation],
                backend: str = "scipy",
                robust: str = "huber",
                f_scale: float = 1.0,
                priors: Optional[dict] = None,
                free_kc=True,
                free_intrinsics: bool = True,
                fix_left_intrinsics: bool = False,
                jac: str = "analytic",
                covariance: bool = True,
                covariance_estimator: str = "sandwich",
                max_nfev: int = 1200,
                max_iter: int = 100,
                timeout_s: float = 60.0,
                allow_fallback: bool = True,
                depths: Sequence[float] = (300.0, 450.0, 600.0),
                x0: Optional[np.ndarray] = None) -> dict:
    """Solve the joint calibration problem and return parameters + covariance.

    Parameters
    ----------
    problem : Problem or list[Observation]
        Either a :class:`Problem` from :func:`make_problem` or the observation
        list directly (a default board pitch and start are then used).
    backend : {"scipy", "native", "opencv", "lm"}
        See the module docstring.  ``"native"`` falls back to ``"scipy"`` on any
        failure unless ``allow_fallback=False``.
    robust : {"none", "huber", "cauchy", "soft_l1"}
        ``"none"`` = plain least squares.  The native backend only supports
        ``"huber"`` and ``"none"``-equivalent behaviour is not offered (it always
        applies Huber); use SciPy or LM for the other losses.
    f_scale : float
        Loss scale in *residual* units (pixels), matching
        ``least_squares(loss=..., f_scale=...)`` and ``HuberLoss(delta)``.
    priors : dict or None
        See :func:`_prior_spec`.  Example
        ``{"intrinsics": 30.0, "distortion": 0.02}``.
    free_kc : bool | "k1k2" | "k1k2k3" | 5 bools
        Which distortion coefficients are estimated.
    free_intrinsics, fix_left_intrinsics : bool
        Hold ``fx,fy,cx,cy`` (all cameras / only the left gauge camera).
    jac : {"analytic", "fd"}
        Jacobian used by the SciPy backend only.
    covariance : bool
        Evaluate the covariance (costs one Jacobian and one SVD).
    covariance_estimator : {"sandwich", "gauss_newton"}
        ``"sandwich"`` matches the engine's robust covariance.
    depths : sequence of float
        3-D test depths (mm) for the reconstruction-sigma report.

    Returns
    -------
    dict with keys
        ``backend`` (the backend that actually produced the answer),
        ``requested_backend``, ``fallback``, ``fallback_reason``,
        ``x`` (full packed vector), ``cameras``, ``extrinsics``, ``views``,
        ``residuals`` (m,), ``residual_rms``, ``residual_max``,
        ``covariance`` (n x n, zeros for held parameters), ``param_std``,
        ``derived`` (baseline / relative rotation / point sigmas),
        ``diagnostics``, ``timings``, ``result`` (``CalibrationResult``).
    """
    t_start = time.perf_counter()
    if not isinstance(problem, Problem):
        problem = make_problem(list(problem))
    backend = str(backend).lower()
    if backend not in ("scipy", "native", "opencv", "lm"):
        raise ValueError(f"unknown backend {backend!r}")
    robust = "none" if robust in (None, "linear") else str(robust).lower()
    if robust not in LOSSES:
        raise ValueError(f"unknown robust loss {robust!r}; use {LOSSES}")
    fallback = False
    fallback_reason = None
    native_meta = None

    x_start = problem.start(free_kc, free_intrinsics, fix_left_intrinsics) \
        if x0 is None else np.asarray(x0, float).copy()
    mask = problem.free_mask(free_kc, free_intrinsics, fix_left_intrinsics)
    free_idx = np.where(mask)[0]
    prior_pos, prior_mean, prior_sigma = _prior_spec(problem, priors, x_start,
                                                     free_idx)

    def _solve_scipy():
        xf0 = x_start[free_idx]
        m = problem.m

        def fun(xf):
            x = x_start.copy()
            x[free_idx] = xf
            r = problem.residuals(x)
            if len(prior_pos):
                pr = (xf[prior_pos] - prior_mean) / prior_sigma
                r = np.concatenate([r, pr])
            return r

        def jacf(xf):
            x = x_start.copy()
            x[free_idx] = xf
            J = problem.jacobian(x, free_idx)
            if len(prior_pos):
                P = np.zeros((len(prior_pos), len(free_idx)))
                P[np.arange(len(prior_pos)), prior_pos] = 1.0 / prior_sigma
                J = np.vstack([J, P])
            return J

        kwargs = dict(method="trf", loss=("linear" if robust == "none" else robust),
                      f_scale=float(f_scale), max_nfev=int(max_nfev),
                      xtol=1e-10, ftol=1e-10, gtol=1e-10, verbose=0)
        if jac == "analytic":
            kwargs["jac"] = jacf
            kwargs["tr_solver"] = "exact"
            res = least_squares(fun, xf0, **kwargs)
        else:
            res = least_squares(fun, xf0, **kwargs)
        x = x_start.copy()
        x[free_idx] = res.x
        return {"x": x, "status": str(res.message), "nfev": int(res.nfev),
                "nit": int(getattr(res, "njev", 0) or 0),
                "cost": float(res.cost), "optimality": float(res.optimality),
                "success": bool(res.success)}

    t_solve0 = time.perf_counter()
    if backend == "native":
        if not _native_available():
            if not allow_fallback:
                raise FileNotFoundError(f"native solver {NATIVE_BINARY} missing")
            fallback, fallback_reason = True, "binary missing"
        elif not _kc_mask(free_kc).all() or not free_intrinsics or \
                fix_left_intrinsics or len(prior_pos):
            if not allow_fallback:
                raise ValueError("native backend cannot express this configuration")
            fallback = True
            if not _kc_mask(free_kc).all():
                fallback_reason = ("native backend needs all five distortion "
                                   "coefficients free: its dense normal equations "
                                   "and covariance are singular when a coefficient "
                                   "is held, so the parameters would be fine but "
                                   "the covariance would not")
            else:
                fallback_reason = ("native backend supports Huber only, free "
                                   "intrinsics and no priors")
        elif robust not in ("huber", "none"):
            if allow_fallback:
                fallback = True
                fallback_reason = f"native backend has no {robust!r} loss"
            else:
                raise ValueError(f"native backend has no {robust!r} loss")
        if not fallback:
            try:
                out = _run_native(problem, x_start, float(f_scale),
                                  bool(_kc_mask(free_kc).all()),
                                  max_iter=max_iter, timeout_s=timeout_s)
                xn = np.asarray(out["x"], float)
                if xn.shape != (problem.n,) or not np.all(np.isfinite(xn)):
                    raise RuntimeError("native returned a malformed parameter vector")
                r_chk = problem.residuals(xn)
                rms_chk = float(np.sqrt(np.mean(np.sum(
                    r_chk.reshape(-1, 2) ** 2, axis=1))))
                if not np.isfinite(rms_chk) or abs(rms_chk - out["rms_px"]) > \
                        1e-6 * max(1.0, rms_chk):
                    raise RuntimeError(
                        f"native/model mismatch: rms {out['rms_px']:.9g} vs "
                        f"recomputed {rms_chk:.9g}")
                x = xn
                native_meta = {"iterations": out.get("iterations"),
                               "status": out.get("status"),
                               "lambda": out.get("lambda"),
                               "t_total_s": out.get("t_total_s"),
                               "t_jac_s": out.get("t_jac_s"),
                               "wall_s": out.get("wall_s"),
                               "binary": out.get("binary"),
                               "rms_px": out.get("rms_px"),
                               "cov_diag": out.get("cov_diag"),
                               "max_px": out.get("max_px")}
                solved = {"x": x, "status": f"native:{out.get('status')}",
                          "nfev": None, "nit": out.get("iterations"),
                          "cost": out.get("cost"), "optimality": None,
                          "success": True}
            except Exception as exc:  # noqa: BLE001 - deliberate fallback
                if not allow_fallback:
                    raise
                fallback = True
                fallback_reason = f"{type(exc).__name__}: {exc}"
                solved = None
        else:
            solved = None
        if fallback:
            solved = _solve_scipy()
            backend_used = "scipy"
            x = solved["x"]
        else:
            backend_used = "native"
    elif backend == "opencv":
        if len(prior_pos):
            raise ValueError("opencv backend does not support priors")
        ov = _solve_opencv(problem, x_start, free_kc, free_intrinsics,
                           fix_left_intrinsics)
        x = _refine_poses(problem, ov["x"])
        solved = {"x": x, "status": "opencv", "nfev": None, "nit": None,
                  "cost": None, "optimality": None, "success": True}
        backend_used = "opencv"
        native_meta = {"per_camera_rms": ov["per_camera_rms"],
                       "per_camera_std": ov["per_camera_std"]}
    elif backend == "lm":
        lm = _solve_lm(problem, x_start, free_idx, robust, f_scale,
                       prior_pos, prior_mean, prior_sigma, max_iter=max_iter)
        x = lm["x"]
        # A "no_descent" step at a negligible gradient is a stationary
        # point, not a failed fit.  It occurred intermittently on the synthetic
        # free-kc fixture (|g|inf 4e-7..9e-6) and caused a silent expensive
        # SciPy fallback; treat that case as converged and keep the fast path.
        stationary = (lm["status"] == "no_descent"
                      and float(lm["grad_inf"]) <= 1e-5)
        lm_status = "converged_stationary" if stationary else lm["status"]
        solved = {"x": x, "status": lm_status, "nfev": lm["nfev"],
                  "nit": lm["nit"], "cost": lm["cost"],
                  "optimality": lm["grad_inf"],
                  "success": (lm_status.startswith("converged") or stationary)}
        backend_used = "lm"
        if not solved["success"] and allow_fallback:
            # a non-converged internal LM must never be published silently
            fallback = True
            fallback_reason = f"lm did not converge ({lm['status']}, " \
                              f"|g|inf={lm['grad_inf']:.3g})"
            solved = _solve_scipy()
            x = solved["x"]
            backend_used = "scipy"
    else:
        solved = _solve_scipy()
        backend_used = "scipy"
        x = solved["x"]
    t_solve = time.perf_counter() - t_solve0

    # ---- residuals, diagnostics
    r = problem.residuals(x)
    norms = np.linalg.norm(r.reshape(-1, 2), axis=1)
    rms = float(np.sqrt(np.mean(norms ** 2))) if len(norms) else float("nan")
    rmax = float(np.max(norms)) if len(norms) else float("nan")

    diag = {"n_residuals": int(problem.m), "n_free": int(len(free_idx)),
            "n_views": int(problem.n_views), "n_parameters": int(problem.n),
            "sigma_px": _sigma_from_residuals(r),
            "robust": robust, "f_scale": float(f_scale),
            "free_kc": _kc_mask(free_kc).tolist(),
            "free_intrinsics": bool(free_intrinsics),
            "fix_left_intrinsics": bool(fix_left_intrinsics),
            "solver_status": solved.get("status"),
            "nfev": solved.get("nfev"), "nit": solved.get("nit"),
            "cost": solved.get("cost"), "success": solved.get("success"),
            "x0_source": "engine" if x0 is None else "caller"}
    if native_meta:
        diag["native"] = native_meta

    cov = None
    cov_info = {}
    info_mat = None
    marg_info = None
    t_cov0 = time.perf_counter()
    if covariance:
        J = problem.jacobian(x, free_idx)
        cov, cov_info = covariance_from_jacobian(
            J, r, free_idx, problem.n, robust=robust,
            f_scale=float(f_scale), estimator=covariance_estimator,
            prior_pos=prior_pos, prior_sigma=prior_sigma)
        info_mat = cov_info.pop("information", None)
        marg_info = cov_info.pop("marginal_information", None)
        diag.update({k: v for k, v in cov_info.items() if k != "sigma_px"})
        if native_meta and native_meta.get("cov_diag") is not None:
            cd = np.asarray(native_meta["cov_diag"], float)
            if cd.shape == (problem.n,):
                cd = cd[free_idx]
            mine = np.diag(cov)[free_idx]
            ok = (cd.shape == mine.shape) and np.all(mine > 0)
            if ok:
                ratio = cd / mine
                diag["native_cov_diag_ratio"] = {
                    "median": float(np.median(ratio)),
                    "p05": float(np.percentile(ratio, 5)),
                    "p95": float(np.percentile(ratio, 95)),
                    "max_rel_dev": float(np.max(np.abs(ratio - 1.0)))}
    t_cov = time.perf_counter() - t_cov0

    derived = _derived(x, cov, depths, problem)
    cams, extr, views = problem.unpack(x)
    sd = {}
    if cov is not None:
        v = np.sqrt(np.maximum(np.diag(cov), 0.0))
        sd = {n: float(s) for n, s in zip(problem.names, v)}

    info = {"success": bool(solved.get("success")),
            "backend": backend_used, "pitch_mm": problem.pitch_mm,
            "free_kc": free_kc, "free_intrinsics": free_intrinsics,
            "fix_left_intrinsics": fix_left_intrinsics,
            "free_mask": mask.tolist(), "param_names": problem.names,
            "views_used": getattr(problem, "view_ids", list(range(problem.n_views))),
            "view_remap": {int(k): int(v) for k, v in
                           getattr(problem, "view_remap", {}).items()},
            "covariance": {k: v for k, v in cov_info.items()},
            "priors": _prior_info(priors),
            "solver": {"backend": backend_used, "status": solved.get("status"),
                       "nfev": solved.get("nfev"), "nit": solved.get("nit"),
                       "cost": solved.get("cost")}}
    result = CalibrationResult(
        cams, extr, views, cov, rms, rmax, int(len(norms)), problem.n,
        robust, info=info, information=info_mat,
        marginal_information=marg_info)

    timings = {"total_s": time.perf_counter() - t_start,
               "solve_s": t_solve, "covariance_s": t_cov}
    out = {
        "backend": backend_used, "requested_backend": backend,
        "fallback": bool(fallback), "fallback_reason": fallback_reason,
        "x": x, "cameras": {c: cams[c].as_dict() for c in CAMERAS},
        "extrinsics": {c: {"R": extr[c][0].tolist(), "t": extr[c][1].tolist()}
                       for c in CAMERAS},
        "views": [{"R": R.tolist(), "t": t.tolist()} for R, t in views],
        "residuals": r, "residual_rms": rms, "residual_max": rmax,
        "covariance": cov, "param_std": sd, "derived": derived,
        "diagnostics": diag, "timings": timings, "priors": _prior_info(priors),
        "robust": robust, "f_scale": float(f_scale), "free_kc": free_kc,
        "result": result, "n_views": problem.n_views,
        "param_names": problem.names,
    }
    _LAST_RUNS.append({"backend": backend_used,
                       "requested": backend,
                       "fallback": bool(fallback),
                       "reason": fallback_reason,
                       "n_views": problem.n_views,
                       "m": int(problem.m),
                       "solve_s": t_solve,
                       "total_s": timings["total_s"],
                       "rms_px": rms})
    del _LAST_RUNS[:-_MAX_RUNS]
    return out


def solve_engine(problem: Problem | Sequence[Observation],
                 free_kc: bool = True, robust_loss: str = "huber",
                 f_scale: float = 1.0, max_nfev: int = 1200,
                 free_intrinsics: bool = True,
                 fix_left_intrinsics: bool = False) -> dict:
    """Run the unmodified reference engine (``calibrate.calibrate``).

    Provided so a benchmark can compare against the shipping code path without
    importing its private helpers.
    """
    t0 = time.perf_counter()
    if isinstance(problem, Problem):
        obs, pitch, init = problem.observations, problem.pitch_mm, problem.init_cameras
    else:
        obs = list(problem)
        pitch = plate.DEFAULT_PITCH_MM
        init = None
    res = engine.calibrate(obs, pitch, init, free_kc=free_kc,
                           robust_loss=robust_loss, f_scale=f_scale,
                           max_nfev=max_nfev, free_intrinsics=free_intrinsics,
                           fix_left_intrinsics=fix_left_intrinsics)
    return {"backend": "engine", "x": res.vector(), "result": res,
            "residual_rms": res.residual_rms,
            "residual_max": res.residual_max,
            "param_std": res.param_std(), "covariance": res.covariance,
            "timings": {"total_s": time.perf_counter() - t0},
            "derived": {"baseline_mm": float(np.linalg.norm(res.baseline())),
                        "relative_rotation_deg": float(np.degrees(np.linalg.norm(
                            rvec_from_R(res.extrinsics["right"][0])))),
                        "points": {}}}


def backend_report() -> dict:
    """Availability and feature matrix of every backend, plus recent runs."""
    import scipy
    native = _native_available()
    native_version = None
    if native:
        try:
            out = subprocess.run([str(NATIVE_BINARY)],
                                 capture_output=True, text=True, timeout=5)
            native_version = (out.stderr or out.stdout or "").strip()[:200]
        except Exception:  # noqa: BLE001
            native_version = None
    try:
        cv_ver = cv2.__version__
    except Exception:  # noqa: BLE001
        cv_ver = None
    return {
        "python": {"scipy": scipy.__version__, "numpy": np.__version__,
                   "opencv": cv_ver},
        "native": {"available": native, "binary": str(NATIVE_BINARY),
                   "sha256": _sha256(NATIVE_BINARY) if native else None,
                   "usage": native_version},
        "backends": {
            "scipy": {"available": True, "losses": list(LOSSES),
                      "priors": True, "free_kc_mask": True,
                      "fixed_intrinsics": True, "covariance": True,
                      "jacobian": ["analytic", "fd"]},
            "native": {"available": native,
                       "losses": ["huber"] if native else [],
                       "priors": False, "free_kc_mask": False,
                       "fixed_intrinsics": False, "covariance": "diagonal "
                       "(full matrix recomputed analytically in Python)",
                       "jacobian": ["analytic (C++)"]},
            "opencv": {"available": cv_ver is not None, "losses": [],
                       "priors": False, "free_kc_mask": True,
                       "fixed_intrinsics": True, "covariance": False,
                       "jacobian": []},
            "lm": {"available": True, "losses": list(LOSSES), "priors": True,
                   "free_kc_mask": True, "fixed_intrinsics": True,
                   "covariance": True, "jacobian": ["analytic"]},
        },
        "runs": list(_LAST_RUNS),
    }


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
