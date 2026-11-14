#!/usr/bin/env python
"""Shared helpers for the EXStar vendor-corpus refit (analysis only).

Conventions (identical to ``docs/calibration/model.md`` and ``calib.geometry``)::

    X_cam = R_cam @ X_world + T_cam
    (u, v) = K_cam, kc_cam applied to X_cam

For a vendor CCF, both IR records (and the colour record) are poses in one
vendor world frame, so the board pose (Rb, tb) is shared and every camera
projects ``R_cam (Rb X + tb) + T_cam``.

Nothing here writes outside the paths the callers pass.
"""
from __future__ import annotations

import json
import os
import sys

import cv2
import numpy as np
from scipy.optimize import least_squares

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from calib import plate  # noqa: E402
from calib.calibrate import CameraIntrinsics, Observation  # noqa: E402
from calib.geometry import epipolar_residual, fundamental  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "tools", "ccf"))
from decode_ccf import decode_record  # noqa: E402

CAMERAS = ("left", "right", "tex")
SIDE = {"Left": "left", "Right": "right", "Tex": "tex"}
PITCH = 28.0

NEW_DIR = ("<EXStar-install>/Shining3d/EXStar/res/"
           "Einscan-E10/200x150")
FACTORY_REPO = os.path.join(REPO, "sources/calibration/res_Einscan-E10_200x150")


# --------------------------------------------------------------------------
# CCF decoding
# --------------------------------------------------------------------------

def decode_ccf_set(directory: str, key_mode: str = "best") -> dict:
    """Decode one Left/Right/TexCCF.txt set into a camera model dict."""
    out = {}
    for side in ("Left", "Right", "Tex"):
        path = os.path.join(directory, f"{side}CCF.txt")
        data = open(path, "rb").read()
        rec = decode_record(data, path, key_mode)
        skew = float(rec.alpha) * float(rec.fx)
        out[SIDE[side]] = {
            "fx": float(rec.fx), "fy": float(rec.fy),
            "cx": float(rec.cx), "cy": float(rec.cy),
            "skew": skew,
            "kc": np.array(rec.kc, float),
            "R": rec.R.copy(), "T": rec.T.copy(),
            "K": rec.K.copy(),
            "kind": rec.kind, "path": path,
        }
    out["_key_mode"] = key_mode
    out["_dir"] = directory
    return out


def model_from_profile(path: str) -> dict:
    """Build the same camera-model dict from an einstar.recon.profile JSON."""
    p = json.load(open(path))
    out = {}
    for prof, cam in (("ir_left", "left"), ("ir_right", "right"), ("color", "tex")):
        c = p["cameras"][prof]
        out[cam] = {"fx": c["fx"], "fy": c["fy"], "cx": c["cx"], "cy": c["cy"],
                    "skew": float(c.get("skew", 0.0)),
                    "kc": np.array(c["dist"], float),
                    "R": np.array(c["R"], float).reshape(3, 3),
                    "T": np.array(c["T"], float),
                    "K": np.array([[c["fx"], float(c.get("skew", 0.0)) * c["fx"],
                                    c["cx"]],
                                   [0.0, c["fy"], c["cy"]], [0.0, 0.0, 1.0]]),
                    "kind": "profile", "path": path}
    out["_key_mode"] = "profile"
    out["_dir"] = path
    return out


# --------------------------------------------------------------------------
# projections / metrics
# --------------------------------------------------------------------------

def intrinsics(model: dict) -> dict:
    return {c: CameraIntrinsics(model[c]["fx"], model[c]["fy"],
                                model[c]["cx"], model[c]["cy"],
                                np.asarray(model[c]["kc"], float),
                                skew=float(model[c].get("skew", 0.0)))
            for c in CAMERAS}


def project(model: dict, cam: str, Rb: np.ndarray, tb: np.ndarray,
            X: np.ndarray) -> np.ndarray:
    m = model[cam]
    Xc = (np.asarray(X, float) @ Rb.T + tb.ravel()) @ m["R"].T + m["T"]
    K = m["K"]
    kc = np.asarray(m["kc"], float)
    from calib.geometry import distort
    z = Xc[:, 2]
    x, y = Xc[:, 0] / z, Xc[:, 1] / z
    xd, yd = distort(x, y, kc)
    u = K[0, 0] * xd + K[0, 1] * yd + K[0, 2]
    v = K[1, 1] * yd + K[1, 2]
    return np.stack([u, v], 1)


def obs_from_detections(path: str, views=None) -> list:
    """calibrate.Observation list from a detection inventory JSON."""
    det = json.load(open(path))
    out = []
    for r in det["images"]:
        if views is not None and r["index"] not in views:
            continue
        if not r.get("points"):
            continue
        uv = {}
        for k, (x, y) in r["points"].items():
            u, v = (float(t) for t in k.split(","))
            uv[(u, v)] = np.array([x, y], float)
        out.append(Observation(r["camera"], int(r["index"]) - 1, uv))
    return out


def _board_xyz(uv_keys) -> np.ndarray:
    obj = plate.board_points_mm(PITCH)
    idx = {(float(u), float(v)): i
           for i, (u, v) in enumerate(plate.lattice_uv())}
    return np.array([obj[idx[k]] for k in uv_keys], float)


def solve_view_pose(model: dict, view_obs: dict, init=None,
                    pose_cams=("left", "right")) -> dict:
    """Board pose (Rb, tb) in the vendor world frame from one view's obs.

    ``view_obs`` maps camera -> dict[(u,v)] -> pixel.  All cameras share the pose;
    6 DoF are fitted against every visible camera.  Returns the pose, per-camera
    residuals and the RMS.
    """
    cams_all = [c for c in CAMERAS if c in view_obs and len(view_obs[c]) >= 6]
    cams = [c for c in pose_cams if c in cams_all] or cams_all
    if not cams_all:
        return {"ok": False, "reason": "no camera with >=6 points"}
    # common key set is not required: each camera contributes its own points
    if init is None:
        cam0 = "left" if "left" in cams else cams[0]
        keys0 = sorted(view_obs[cam0])
        X0 = _board_xyz(keys0)
        uv0 = np.array([view_obs[cam0][k] for k in keys0], float)
        m0 = model[cam0]
        K0 = np.array([[m0["fx"], m0["skew"] * m0["fx"], m0["cx"]],
                       [0, m0["fy"], m0["cy"]], [0, 0, 1]], float)
        ok, rvec, tvec = cv2.solvePnP(X0, uv0, K0, np.asarray(m0["kc"], float),
                                      flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            return {"ok": False, "reason": "initial PnP failed"}
        Rb0, _ = cv2.Rodrigues(rvec)
        tb0 = tvec.ravel()
        # board->cam0 == board->world through cam0's pose
        R = m0["R"]; T = m0["T"]
        init = (R.T @ Rb0, R.T @ (tb0 - T))

    data = []
    for c in cams:
        keys = sorted(view_obs[c])
        data.append((c, _board_xyz(keys),
                     np.array([view_obs[c][k] for k in keys], float)))
    data_all = []
    for c in cams_all:
        keys = sorted(view_obs[c])
        data_all.append((c, _board_xyz(keys),
                         np.array([view_obs[c][k] for k in keys], float)))

    def resid(p):
        Rb = cv2.Rodrigues(p[:3])[0]
        tb = p[3:6]
        return np.concatenate([(project(model, c, Rb, tb, X) - uv).ravel()
                               for c, X, uv in data])

    p0 = np.concatenate([cv2.Rodrigues(np.asarray(init[0]))[0].ravel(),
                         np.asarray(init[1]).ravel()])
    sol = least_squares(resid, p0, method="lm", xtol=1e-12, ftol=1e-12,
                        max_nfev=200)
    Rb = cv2.Rodrigues(sol.x[:3])[0]
    tb = sol.x[3:6]
    per_cam, allres = {}, []
    for c, X, uv in data_all:
        e = np.linalg.norm(project(model, c, Rb, tb, X) - uv, axis=1)
        per_cam[c] = {"n": int(len(e)), "rms_px": float(np.sqrt(np.mean(e ** 2))),
                      "max_px": float(e.max())}
        allres.append(e)
    e = np.concatenate(allres)
    return {"ok": True, "Rb": Rb, "tb": tb,
            "rms_px": float(np.sqrt(np.mean(e ** 2))),
            "n": int(len(e)), "per_cam": per_cam,
            "residuals": {c: np.linalg.norm(project(model, c, Rb, tb, X) - uv, axis=1)
                          for c, X, uv in data_all}}


def evaluate_model(model: dict, obs: list, tag: str = "") -> dict:
    """Per-view pose fit with the model FIXED; returns residual statistics."""
    by_view = {}
    for o in obs:
        by_view.setdefault(int(o.view), {})[o.camera] = o.uv
    rows, resid = {}, []
    for v in sorted(by_view):
        r = solve_view_pose(model, by_view[v])
        if not r["ok"]:
            rows[v] = r
            continue
        rows[v] = {"rms_px": r["rms_px"], "n": r["n"],
                   "per_cam": r["per_cam"]}
        resid.append(r["residuals"])
    per_cam = {}
    for c in CAMERAS:
        e = np.concatenate([d[c] for d in resid if c in d])
        per_cam[c] = {"n": int(len(e)),
                      "rms_px": float(np.sqrt(np.mean(e ** 2))),
                      "median_px": float(np.median(e)),
                      "p90_px": float(np.percentile(e, 90)),
                      "max_px": float(e.max())}
    e_ir = np.concatenate([np.concatenate([d[c] for c in ("left", "right")
                                           if c in d]) for d in resid])
    e_all = np.concatenate([np.concatenate(list(d.values())) for d in resid])
    return {"tag": tag, "pose_fitted_on": "left+right (IR pair)",
            "model_dir": model.get("_dir", ""),
            "key_mode": model.get("_key_mode", ""),
            "n_views": len([r for r in rows.values() if r.get("rms_px") is not None]),
            "n_points": int(len(e_all)),
            "rms_px_ir_pair": float(np.sqrt(np.mean(e_ir ** 2))),
            "median_px": float(np.median(e_all)),
            "p90_px": float(np.percentile(e_all, 90)),
            "rms_px_all_cams": float(np.sqrt(np.mean(e_all ** 2))),
            "per_cam": per_cam,
            "views": {str(k): v for k, v in rows.items()}}


# --------------------------------------------------------------------------
# rig metrics and epipolar
# --------------------------------------------------------------------------

def rig_metrics(model: dict) -> dict:
    out = {}
    for a, b in (("left", "right"), ("left", "tex")):
        Ra, Ta = model[a]["R"], model[a]["T"]
        Rb, Tb = model[b]["R"], model[b]["T"]
        Rrel = Rb @ Ra.T
        trel = Tb - Rrel @ Ta
        out[f"{b}_from_{a}"] = {
            "baseline_mm": float(np.linalg.norm(trel)),
            "t_rel": trel.tolist(),
            "rel_rot_deg": float(np.degrees(np.arccos(np.clip(
                (np.trace(Rrel) - 1) / 2, -1, 1)))),
        }
    return out


def undistort_px(model: dict, cam: str, uv: np.ndarray,
                 tol: float = 1e-12, iters: int = 60) -> np.ndarray:
    """Undistorted ('ideal') pixel coordinates for the Sampson test.

    ``cv2.undistortPoints`` stops after five fixed-point steps; with this lens
    (k1=-0.162, k2=+0.173, k3=+0.018) that leaves a heavy tail on wide-angle
    points, so the inversion is iterated to convergence here and the round trip
    is asserted.
    """
    m = model[cam]
    K = np.array([[m["fx"], m["skew"] * m["fx"], m["cx"]],
                  [0, m["fy"], m["cy"]], [0, 0, 1]], float)
    kc = np.asarray(m["kc"], float)
    k1, k2, p1, p2, k3 = kc[:5]
    uv = np.asarray(uv, float).reshape(-1, 2)
    # pixel -> normalised distorted
    xd = np.linalg.solve(K, np.column_stack([uv, np.ones(len(uv))]).T).T[:, :2]
    x = xd.copy()
    for _ in range(iters):
        r2 = np.sum(x * x, 1)
        radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        dx = 2.0 * p1 * x[:, 0] * x[:, 1] + p2 * (r2 + 2.0 * x[:, 0] ** 2)
        dy = p1 * (r2 + 2.0 * x[:, 1] ** 2) + 2.0 * p2 * x[:, 0] * x[:, 1]
        xn = (xd - np.column_stack([dx, dy])) / radial[:, None]
        if np.max(np.abs(xn - x)) < tol:
            x = xn
            break
        x = xn
    # round-trip check
    from calib.geometry import distort
    xd2 = distort(x[:, 0], x[:, 1], kc)
    err = np.max(np.abs(np.column_stack(xd2) - xd))
    if err > 1e-9:
        raise RuntimeError(f"undistortion did not converge for {cam}: {err:.3e}")
    return (K @ np.column_stack([x, np.ones(len(x))]).T).T[:, :2]


def sampson(model: dict, obs: list, pair=("left", "right"),
            undistort: bool = True) -> dict:
    a, b = pair
    by_view = {}
    for o in obs:
        by_view.setdefault(int(o.view), {})[o.camera] = o.uv
    Rrel = model[b]["R"] @ model[a]["R"].T
    trel = model[b]["T"] - Rrel @ model[a]["T"]
    Ka = np.array([[model[a]["fx"], 0, model[a]["cx"]],
                   [0, model[a]["fy"], model[a]["cy"]], [0, 0, 1]], float)
    Kb = np.array([[model[b]["fx"], 0, model[b]["cx"]],
                   [0, model[b]["fy"], model[b]["cy"]], [0, 0, 1]], float)
    F = fundamental(Ka, Kb, Rrel, trel)
    errs, per_view = [], {}
    for v in sorted(by_view):
        d = by_view[v]
        if a not in d or b not in d:
            continue
        keys = [k for k in d[a] if k in d[b]]
        if len(keys) < 4:
            continue
        x1 = np.array([d[a][k] for k in keys], float)
        x2 = np.array([d[b][k] for k in keys], float)
        if undistort:
            x1 = undistort_px(model, a, x1)
            x2 = undistort_px(model, b, x2)
        e = epipolar_residual(F, x1, x2)
        per_view[v] = {"n": int(len(e)), "median_px": float(np.median(e)),
                       "rms_px": float(np.sqrt(np.mean(e ** 2)))}
        errs.extend(e.tolist())
    e = np.array(errs)
    return {"pair": f"{a}-{b}", "undistorted": bool(undistort),
            "n": int(len(e)), "median_px": float(np.median(e)),
            "rms_px": float(np.sqrt(np.mean(e ** 2))),
            "p90_px": float(np.percentile(e, 90)),
            "per_view": per_view}


def save_json(obj, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def conv(o):
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(type(o))

    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, default=conv)
    print("wrote", path)
