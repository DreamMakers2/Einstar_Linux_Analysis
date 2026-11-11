"""Candidate-profile export and the downstream reconstruction comparison.

Nothing here writes to ``~/einstar-private/einstar-profiles/`` or to any sweep
directory: every file produced lives under the study output directory and every
candidate profile carries ``validation: pending`` plus full provenance.

The reconstruction profile schema is the C++ backend's
``einstar.recon.profile/1`` (``recon/include/einstar/recon/profile.hpp``).  One
convention difference is handled explicitly and matters:

    profile JSON ``skew``  = K[0,1] in PIXELS            (C++ ``K()`` uses it raw)
    ``CameraIntrinsics.skew`` = K[0,1] / fx (dimensionless, MATLAB alpha_c)

so a profile is written as ``skew = cam.skew * cam.fx`` and read back as
``skew = json_skew / fx``.  Getting this backwards multiplies the skew by fx
(~1160) and costs ~10 px of RMS on live data.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import numpy as np

from calib.calibrate import CAMERAS, CameraIntrinsics

SCHEMA = "einstar.recon.profile/1"
JSON_NAMES = {"left": "ir_left", "right": "ir_right", "tex": "color"}


# --------------------------------------------------------------------------
# profile writers / readers
# --------------------------------------------------------------------------

def camera_to_json(cam: CameraIntrinsics, R: np.ndarray, T: np.ndarray) -> dict:
    return {
        "fx": float(cam.fx), "fy": float(cam.fy),
        "cx": float(cam.cx), "cy": float(cam.cy),
        "skew": float(cam.skew * cam.fx),          # absolute K[0,1] in pixels
        "dist": [float(x) for x in np.asarray(cam.kc, float)[:5]],
        "R": [float(x) for x in np.asarray(R, float).ravel()],
        "T": [float(x) for x in np.asarray(T, float).ravel()],
    }


def build_profile(cams, extr, pitch_mm: float, kind: str = "replacement",
                  color_status: str = "unknown",
                  provenance: list[str] | None = None,
                  extra: dict | None = None) -> dict:
    prof = {
        "schema": SCHEMA,
        "kind": kind,
        "image_width": 1280, "image_height": 1024,
        "units": "mm",
        "plate_pitch_mm": float(pitch_mm),
        "color_status": color_status,
        "validation": "pending",
        "cameras": {JSON_NAMES[c]: camera_to_json(cams[c], extr[c][0], extr[c][1])
                    for c in CAMERAS},
        "provenance": list(provenance or []),
    }
    if extra:
        prof.update(extra)
    return prof


def load_profile(path: str | Path) -> dict:
    return json.loads(Path(os.path.expanduser(str(path))).read_text())


def profile_to_cameras(prof: dict) -> tuple[dict, dict]:
    """Profile dict -> (CameraIntrinsics, extrinsics), applying the skew rule."""
    cams, extr = {}, {}
    for c in CAMERAS:
        e = prof["cameras"][JSON_NAMES[c]]
        fx = float(e["fx"])
        cams[c] = CameraIntrinsics(fx, float(e["fy"]), float(e["cx"]), float(e["cy"]),
                                   np.asarray(e["dist"], float)[:5],
                                   float(e["skew"]) / fx if fx else 0.0)
        extr[c] = (np.asarray(e["R"], float).reshape(3, 3),
                   np.asarray(e["T"], float).reshape(3))
    return cams, extr


def write_profile(prof: dict, path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(prof, indent=2, sort_keys=False)
    p.write_text(txt + "\n")
    return hashlib.sha256(txt.encode()).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_files(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


# --------------------------------------------------------------------------
# reconstruction
# --------------------------------------------------------------------------

def run_recon(recon_cli: str | Path, frames: str | Path, gid: int,
              profile: str | Path, outdir: str | Path,
              scale: float = 0.5, timeout: float = 300.0) -> dict:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(recon_cli), "reconstruct", "--frames", str(frames),
           "--id", str(int(gid)), "--profile", str(profile),
           "--out", str(out), "--scale", str(scale)]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    wall = time.time() - t0
    rep = out / "recon_report.json"
    data = json.loads(rep.read_text()) if rep.exists() else {}
    return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout[-2000:],
            "stderr": p.stderr[-2000:], "wall_s": wall,
            "report": data, "cloud": str(out / "recon_cloud.ply"),
            "report_path": str(rep)}


def read_ply(path: str | Path) -> np.ndarray:
    """Read the project's binary PLY (float32 x,y,z,confidence,sigma_mm)."""
    p = Path(path)
    with open(p, "rb") as f:
        nvert, props, header_end = None, [], None
        while True:
            line = f.readline()
            if not line:
                raise ValueError(f"{p}: truncated PLY header")
            s = line.decode("ascii", "replace").strip()
            if s.startswith("element vertex"):
                nvert = int(s.split()[-1])
            elif s.startswith("property"):
                props.append(s.split()[-1])
            elif s == "end_header":
                header_end = f.tell()
                break
        f.seek(header_end)
        raw = f.read()
    dt = np.dtype([(f"f{i}", "<f4") for i in range(len(props))])
    arr = np.frombuffer(raw, dtype=dt, count=nvert)
    xyz = np.stack([arr[f"f{i}"] for i in range(3)], axis=1).astype(np.float64)
    extra = {props[i]: arr[f"f{i}"].astype(np.float64) for i in range(3, len(props))}
    return xyz, extra


def plane_fit(xyz: np.ndarray, seed: int = 0, thresh_mm: float = 0.6,
              iters: int = 1200, max_points: int = 40000) -> dict:
    """Deterministic RANSAC plane + least-squares refit; the plate is the
    dominant planar surface in the 300-460 mm band."""
    z = xyz[:, 2]
    sel = (z > 250.0) & (z < 500.0)
    P = xyz[sel]
    if len(P) < 100:
        return {"ok": False, "reason": f"only {len(P)} points in the plate depth band"}
    rng = np.random.default_rng(seed)
    if len(P) > max_points:
        P = P[rng.choice(len(P), max_points, replace=False)]
    best = None
    for _ in range(iters):
        i = rng.choice(len(P), 3, replace=False)
        p0, p1, p2 = P[i]
        n = np.cross(p1 - p0, p2 - p0)
        nn = np.linalg.norm(n)
        if nn < 1e-9:
            continue
        n = n / nn
        d = -float(n @ p0)
        dist = np.abs(P @ n + d)
        inl = dist < thresh_mm
        c = int(inl.sum())
        if best is None or c > best[0]:
            best = (c, n, d, inl)
    if best is None:
        return {"ok": False, "reason": "no plane candidate"}
    c, n, d, inl = best
    for _ in range(4):
        Q = P[inl]
        cen = Q.mean(0)
        u, s, vt = np.linalg.svd(Q - cen, full_matrices=False)
        n = vt[2] / np.linalg.norm(vt[2])
        d = -float(n @ cen)
        dist = np.abs(P @ n + d)
        inl = dist < thresh_mm
        c = int(inl.sum())
        if c < 50:
            break
    Q = P[inl]
    dist = np.abs(Q @ n + d)
    # sign the normal towards the camera (z ~ +depth)
    if n[2] < 0:
        n, d = -n, -d
    return {"ok": True,
            "n_inliers": int(c), "inlier_frac_of_band": float(c / len(P)),
            "rms_mm": float(np.sqrt(np.mean(dist ** 2))),
            "median_abs_mm": float(np.median(dist)),
            "p95_abs_mm": float(np.percentile(dist, 95)),
            "plane_depth_mm": float(-d),
            "normal": [float(x) for x in n],
            "tilt_deg": float(np.degrees(np.arccos(min(1.0, abs(n[2]))))),
            "threshold_mm": float(thresh_mm)}


def nn_repeatability(A: np.ndarray, B: np.ndarray, max_points: int = 20000,
                     seed: int = 0) -> dict:
    """Symmetric nearest-neighbour distance between two clouds (mm)."""
    from scipy.spatial import cKDTree
    rng = np.random.default_rng(seed)
    if len(A) > max_points:
        A = A[rng.choice(len(A), max_points, replace=False)]
    if len(B) > max_points:
        B = B[rng.choice(len(B), max_points, replace=False)]
    if len(A) == 0 or len(B) == 0:
        return {"ok": False}
    t = cKDTree(B)
    d1, _ = t.query(A, k=1)
    t2 = cKDTree(A)
    d2, _ = t2.query(B, k=1)
    d = np.concatenate([d1, d2])
    return {"ok": True, "median_mm": float(np.median(d)),
            "p90_mm": float(np.percentile(d, 90)),
            "p95_mm": float(np.percentile(d, 95)),
            "n": int(len(d))}


def transform_cloud_to(xyz: np.ndarray, R_from: np.ndarray, t_from: np.ndarray,
                       R_to: np.ndarray, t_to: np.ndarray) -> np.ndarray:
    """Express a cloud taken at board pose (R_to,t_to) in the frame of (R_from,t_from).

    Board frame point Xb = R^T (Xc - t); so
    Xc' = R_from R_to^T (Xc - t_to) + t_from.
    """
    R = R_from @ R_to.T
    return (xyz - t_to) @ R.T + t_from
