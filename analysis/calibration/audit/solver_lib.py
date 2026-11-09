"""Shared pieces for the solver-backend benchmark (SciPy vs OpenCV vs C++/Ceres).

Read-only with respect to the engine: it imports ``calib.calibrate`` helpers so
the analytic Jacobian reproduces the engine's residual definition exactly, but
it never modifies ``calib/calibrate.py``.

Parameter convention (identical to ``calib.calibrate._Packer``)::

    x[0:9]    left  intrinsics  fx fy cx cy k1 k2 p1 p2 k3
    x[9:18]   right intrinsics
    x[18:27]  tex   intrinsics
    x[27:33]  right extrinsics rvec t   (left -> right:  X_r = R X_l + t)
    x[33:39]  tex   extrinsics rvec t
    x[39:]    view poses, 6 per view    (board -> left:    X_l = R X_b + t)

The left camera is the gauge (identity pose); metric scale comes from the board
pitch, so the board poses are free and the rigid rig has 39 free parameters.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from calib import calibrate as engine
from calib import ccf, plate
from calib.calibrate import CAMERAS, CameraIntrinsics, Observation
from calib.geometry import project

ROOT = Path(__file__).resolve().parents[3]
VENDOR_PKL = ROOT / "analysis/calibration/engine/vendor_detections.pkl"
CCF_DIR = ROOT / "sources/calibration/res_Einscan-E10_200x150"
IMG_SIZE = (1280, 1024)  # engine hard-codes this for the OpenCV initialisation

CAM_KEY = {"Left": "left", "Right": "right", "Tex": "tex"}


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

@dataclass
class Case:
    name: str
    obj: np.ndarray                  # (P,3) board points, mm
    rows: list                      # [(cam_idx, view_idx, idx (K,), uv (K,2))]
    n_views: int
    init: dict                      # name -> CameraIntrinsics initial guess
    truth: Optional[dict] = None    # synthetic ground truth, else None
    reference: Optional[dict] = None  # CCF record for the vendor corpus
    pitch_mm: float = plate.DEFAULT_PITCH_MM

    @property
    def m(self) -> int:
        return int(sum(2 * len(r[2]) for r in self.rows))

    @property
    def n(self) -> int:
        return 39 + 6 * self.n_views


def _rows_from_observations(obs: list[Observation]) -> tuple[list, int]:
    """Build residual rows exactly like ``calibrate._Problem._build_rows``."""
    used = sorted({int(o.view) for o in obs})
    remap = {v: i for i, v in enumerate(used)}
    rows = []
    for o in obs:
        idx = [engine._UV_INDEX[k] for k in o.uv if k in engine._UV_INDEX]
        if not idx:
            continue
        uv = np.array([o.uv[tuple(plate.lattice_uv()[i])] for i in idx], float)
        rows.append((CAMERAS.index(o.camera), remap[int(o.view)],
                     np.array(idx, int), uv))
    return rows, len(used)


def load_vendor(name: str = "vendor25", max_views: int | None = None) -> Case:
    raw = pickle.loads(VENDOR_PKL.read_bytes())
    obs = []
    views = sorted({int(k[1]) for k in raw})
    if max_views is not None:
        views = views[:max_views]
    for (cam, view), uv in raw.items():
        if int(view) not in views:
            continue
        obs.append(Observation(CAM_KEY[cam], int(view),
                               {tuple(k): np.asarray(v, float) for k, v in uv.items()}))
    rows, n_views = _rows_from_observations(obs)
    init = {}
    for n in ("Left", "Right", "Tex"):
        rec = ccf.load(CCF_DIR / f"{n}CCF.txt", ir=(n != "Tex"))
        init[CAM_KEY[n]] = CameraIntrinsics.from_ccf(rec)
    ref = {n: ccf.load(CCF_DIR / f"{n}CCF.txt", ir=(n != "Tex"))
           for n in ("Left", "Right", "Tex")}
    obj = plate.board_points_mm(plate.DEFAULT_PITCH_MM)
    return Case(name, obj, rows, n_views, init, None, ref, plate.DEFAULT_PITCH_MM)


def load_synth(n_views: int = 12, seed: int = 0, noise_px: float = 0.2,
               outlier_frac: float = 0.02, name: str | None = None) -> Case:
    from calib import synth
    sc = synth.make_scene(n_views=n_views, seed=seed, noise_px=noise_px,
                          outlier_frac=outlier_frac)
    rows, nv = _rows_from_observations(sc.observations)
    init = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
            for c in CAMERAS}
    return Case(name or f"synth{n_views}", plate.board_points_mm(sc.pitch_mm),
                rows, nv, init, sc.truth, None, sc.pitch_mm)


# ---------------------------------------------------------------------------
# Rodrigues with analytic derivative
# ---------------------------------------------------------------------------

def _skew(v: np.ndarray) -> np.ndarray:
    return np.array([[0.0, -v[2], v[1]],
                     [v[2], 0.0, -v[0]],
                     [-v[1], v[0], 0.0]])


def rodrigues_dR(rvec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """R = Exp([r]x) and dR/dr_i (3,3,3), same convention as cv2.Rodrigues."""
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
        e = np.zeros(3); e[i] = 1.0
        Ei = _skew(e)
        s = r[i] / th if th > 1e-12 else 0.0
        dR[i] = dA * s * K + A * Ei + dB * s * K2 + B * (Ei @ K + K @ Ei)
    return R, dR


# ---------------------------------------------------------------------------
# residual + analytic Jacobian
# ---------------------------------------------------------------------------

class FastProblem:
    """Analytic residual/Jacobian for the engine's exact model (skew = 0)."""

    def __init__(self, case: Case):
        self.case = case
        self.rows = case.rows
        self.obj = np.asarray(case.obj, float)
        self.n_views = case.n_views
        self.n = case.n
        self.m = case.m
        # fixed sparsity pattern (free_mask all true here)
        self.free_idx = np.arange(self.n)
        self.row_offsets = np.cumsum([0] + [2 * len(r[2]) for r in self.rows])
        self.cols_of_block = []
        for cam, view, idx, uv in self.rows:
            cols = list(range(9 * cam, 9 * cam + 9))
            if cam != 0:
                cols += list(range(27 + 6 * (cam - 1), 27 + 6 * cam))
            cols += list(range(39 + 6 * view, 39 + 6 * view + 6))
            self.cols_of_block.append(np.array(cols, int))

    # -- helpers ---------------------------------------------------------
    def unpack(self, x: np.ndarray):
        cams = [x[9 * c:9 * c + 9] for c in range(3)]
        extr = [None, x[27:33], x[33:39]]
        views = [x[39 + 6 * v: 39 + 6 * v + 6] for v in range(self.n_views)]
        return cams, extr, views

    def _poses(self, x: np.ndarray):
        cams, extr, views = self.unpack(x)
        Rv = np.empty((self.n_views, 3, 3)); tv = np.empty((self.n_views, 3))
        dRv = np.empty((self.n_views, 3, 3, 3))
        for v, p in enumerate(views):
            Rv[v], dRv[v] = rodrigues_dR(p[:3])
            tv[v] = p[3:6]
        Rc = [np.eye(3)]; tc = [np.zeros(3)]
        dRc = [np.zeros((3, 3, 3))]
        for c in (1, 2):
            R, dR = rodrigues_dR(extr[c][:3])
            Rc.append(R); dRc.append(dR); tc.append(extr[c][3:6])
        return cams, Rv, tv, dRv, Rc, tc, dRc

    # -- residual --------------------------------------------------------
    def residuals(self, x: np.ndarray) -> np.ndarray:
        cams, Rv, tv, dRv, Rc, tc, dRc = self._poses(x)
        out = np.empty(self.m)
        for b, (cam, view, idx, uv) in enumerate(self.rows):
            X = self.obj[idx]
            Xl = X @ Rv[view].T + tv[view]
            Xc = Xl @ Rc[cam].T + tc[cam]
            pred = project(self._K(cams[cam]), cams[cam][4:9], Xc)
            out[self.row_offsets[b]:self.row_offsets[b + 1]] = (pred - uv).ravel()
        return out

    @staticmethod
    def _K(p):
        return np.array([[p[0], 0.0, p[2]], [0.0, p[1], p[3]], [0.0, 0.0, 1.0]])

    # -- jacobian --------------------------------------------------------
    def jacobian(self, x: np.ndarray):
        cams, Rv, tv, dRv, Rc, tc, dRc = self._poses(x)
        rows_i, cols_i, vals = [], [], []
        for b, (cam, view, idx, uv) in enumerate(self.rows):
            X = self.obj[idx]                      # (K,3)
            Xl = X @ Rv[view].T + tv[view]         # (K,3)
            Xc = Xl @ Rc[cam].T + tc[cam]          # (K,3)
            z = Xc[:, 2]
            xd_ = Xc[:, 0] / z
            yd_ = Xc[:, 1] / z
            k = len(idx)
            cols = self.cols_of_block[b]
            blk = np.zeros((2 * k, len(cols)))

            fx, fy, cx, cy = cams[cam][:4]
            k1, k2, p1, p2, k3 = cams[cam][4:9]
            r2 = xd_ * xd_ + yd_ * yd_
            rad = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
            dr2 = k1 + 2.0 * k2 * r2 + 3.0 * k3 * r2 ** 2
            # distortion value and d(xd,yd)/d(x,y)
            xd = xd_ * rad + 2.0 * p1 * xd_ * yd_ + p2 * (r2 + 2.0 * xd_ * xd_)
            yd = yd_ * rad + p1 * (r2 + 2.0 * yd_ * yd_) + 2.0 * p2 * xd_ * yd_
            dxd_dx = rad + xd_ * 2.0 * xd_ * dr2 + 2.0 * p1 * yd_ + 4.0 * p2 * xd_
            dxd_dy = xd_ * 2.0 * yd_ * dr2 + 2.0 * p1 * xd_ + 2.0 * p2 * yd_
            dyd_dx = yd_ * 2.0 * xd_ * dr2 + 2.0 * p1 * xd_ + 2.0 * p2 * yd_
            dyd_dy = rad + yd_ * 2.0 * yd_ * dr2 + 4.0 * p1 * yd_ + 2.0 * p2 * xd_

            # d(u,v)/dXc  (K,2,3)
            dz = 1.0 / z
            duv_dXc = np.zeros((k, 2, 3))
            duv_dXc[:, 0, 0] = fx * dxd_dx * dz
            duv_dXc[:, 0, 1] = fx * dxd_dy * dz
            duv_dXc[:, 0, 2] = -(fx * (dxd_dx * xd_ + dxd_dy * yd_)) * dz
            duv_dXc[:, 1, 0] = fy * dyd_dx * dz
            duv_dXc[:, 1, 1] = fy * dyd_dy * dz
            duv_dXc[:, 1, 2] = -(fy * (dyd_dx * xd_ + dyd_dy * yd_)) * dz

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
                # Xc = Rc @ Xl + tc with Rc = Exp(rvec): dXc/dr_i = dRc_i @ Xl
                dXc_dr = np.einsum("icq,kq->ikc", dRc[cam], Xl)
                for i in range(3):
                    blk[0::2, off + i] = np.einsum("kc,kc->k", duv_dXc[:, 0, :],
                                                   dXc_dr[i])
                    blk[1::2, off + i] = np.einsum("kc,kc->k", duv_dXc[:, 1, :],
                                                   dXc_dr[i])
                blk[0::2, off + 3:off + 6] = duv_dXc[:, 0, :]
                blk[1::2, off + 3:off + 6] = duv_dXc[:, 1, :]
                off += 6

            # view: dXl/dr_i = dRv_i @ X ; dXc/dr_i = Rc @ (dXl/dr_i)
            dXl_dv = np.einsum("icq,kq->ikc", dRv[view], X)
            dXc_dv = np.einsum("rc,ikc->ikr", Rc[cam], dXl_dv)
            for i in range(3):
                blk[0::2, off + i] = np.einsum("kc,kc->k", duv_dXc[:, 0, :],
                                               dXc_dv[i])
                blk[1::2, off + i] = np.einsum("kc,kc->k", duv_dXc[:, 1, :],
                                               dXc_dv[i])
            blk[0::2, off + 3:off + 6] = duv_dXc[:, 0, :] @ Rc[cam]
            blk[1::2, off + 3:off + 6] = duv_dXc[:, 1, :] @ Rc[cam]

            r0 = self.row_offsets[b]
            rows_i.append(np.repeat(np.arange(r0, r0 + 2 * k), len(cols)))
            cols_i.append(np.tile(cols, 2 * k))
            vals.append(blk.ravel())
        from scipy.sparse import coo_matrix
        return coo_matrix((np.concatenate(vals),
                           (np.concatenate(rows_i), np.concatenate(cols_i))),
                          shape=(self.m, self.n)).tocsr()


# ---------------------------------------------------------------------------
# engine initialisation (re-used, not modified)
# ---------------------------------------------------------------------------

def engine_start(case: Case, free_kc: bool = True):
    """Reproduce ``calibrate()``'s x0 and _Problem so runs share one start."""
    obs = []
    for cam, view, idx, uv in case.rows:
        d = {tuple(plate.lattice_uv()[i]): uv[j] for j, i in enumerate(idx)}
        obs.append(Observation(CAMERAS[cam], view, d))
    cams0 = engine._initial_intrinsics(obs, case.init, case.obj, free_kc)
    views0, extr0 = engine._initial_poses(obs, cams0, case.obj)
    packer = engine._Packer(case.n_views, free_kc)
    x0 = engine._pack_initial(cams0, extr0, views0, packer)
    prob = engine._Problem(obs, case.obj, case.n_views, free_kc, "huber", 1.0, True)
    return x0, prob, packer


def unpack_rig(x: np.ndarray, n_views: int) -> dict:
    """Readable rig parameters from the packed vector."""
    out = {"cameras": {}}
    for i, c in enumerate(CAMERAS):
        v = x[9 * i:9 * i + 9]
        out["cameras"][c] = {"fx": float(v[0]), "fy": float(v[1]),
                             "cx": float(v[2]), "cy": float(v[3]),
                             "kc": [float(t) for t in v[4:9]]}
    for c, s in (("right", 27), ("tex", 33)):
        out[c] = {"rvec": x[s:s + 3].tolist(), "t": x[s + 3:s + 6].tolist()}
    out["views"] = [{"rvec": x[39 + 6 * v:42 + 6 * v].tolist(),
                     "t": x[42 + 6 * v:45 + 6 * v].tolist()}
                    for v in range(n_views)]
    return out


def baseline_mm(x: np.ndarray) -> np.ndarray:
    """Rig baseline in the left frame: t_right (left is identity)."""
    return np.asarray(x[30:33], float)


def rms_of(r: np.ndarray) -> tuple[float, float]:
    n = np.linalg.norm(np.asarray(r).reshape(-1, 2), axis=1)
    return float(np.sqrt(np.mean(n ** 2))), float(np.max(n))
