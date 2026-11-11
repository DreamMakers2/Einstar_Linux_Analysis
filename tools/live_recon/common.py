"""Small shared helpers: paths, hashing, JSON, percentiles, Numpy IO.

Kept deliberately dependency-light (numpy + stdlib).  No scipy: the machine this
runs on has a LAPACK build where ``numpy.linalg.svd``/``lstsq`` on tall
matrices (tens of thousands of rows) takes minutes, so every fit in this package
uses a 3x3 covariance / normal-equation solve instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time

import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# paths / hashing
# ---------------------------------------------------------------------------

def repo_path(*parts: str) -> str:
    return os.path.join(REPO_ROOT, *parts)


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def file_fingerprint(path: str) -> dict:
    """sha256 + size + mtime, or an explicit 'missing' record."""
    if not os.path.exists(path):
        return {"path": path, "exists": False}
    st = os.stat(path)
    return {
        "path": path,
        "exists": True,
        "sha256": sha256_file(path),
        "bytes": int(st.st_size),
        "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
    }


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def _default(o):
    if isinstance(o, (np.floating,)):
        v = float(o)
        return v if np.isfinite(v) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, os.PathLike):
        return os.fspath(o)
    raise TypeError(f"not JSON serialisable: {type(o)!r}")


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=_default, allow_nan=False)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path: str):
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def pct(values, qs=(5.0, 50.0, 95.0)) -> list:
    a = np.asarray(values, dtype=np.float64).ravel()
    if a.size == 0:
        return [float("nan")] * len(qs)
    return [float(np.percentile(a, q)) for q in qs]


def p50(values) -> float:
    a = np.asarray(values, dtype=np.float64).ravel()
    return float(np.median(a)) if a.size else float("nan")


def rms(values) -> float:
    a = np.asarray(values, dtype=np.float64).ravel()
    return float(np.sqrt(np.mean(a * a))) if a.size else float("nan")


def finite(values) -> np.ndarray:
    a = np.asarray(values, dtype=np.float64).ravel()
    return a[np.isfinite(a)]


def robust_sigma(values) -> float:
    """1.4826 * MAD, a scale estimate that is not inflated by outliers."""
    a = finite(values)
    if a.size == 0:
        return float("nan")
    med = float(np.median(a))
    return float(1.4826 * np.median(np.abs(a - med)))


# ---------------------------------------------------------------------------
# geometry primitives (fast, no SVD)
# ---------------------------------------------------------------------------

def fit_plane(points_xyz: np.ndarray):
    """Least-squares plane through >=3 points via the 3x3 covariance.

    Returns ``(centroid, normal, residual_rms)`` with the normal oriented so
    that ``normal . centroid >= 0`` (i.e. pointing away from the camera origin
    for a surface in front of it).
    """
    p = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    if p.shape[0] < 3:
        return None
    centroid = p.mean(axis=0)
    c = p - centroid
    cov = (c.T @ c) / float(p.shape[0])
    w, v = np.linalg.eigh(cov)          # ascending eigenvalues
    normal = v[:, 0]
    if float(normal @ centroid) < 0.0:
        normal = -normal
    resid = c @ normal
    return centroid, normal, float(np.sqrt(np.mean(resid * resid)))


def fit_affine_xy(xy: np.ndarray, z: np.ndarray):
    """Solve z = a + b*x + c*y through the normal equations (3x3)."""
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    z = np.asarray(z, dtype=np.float64).ravel()
    if xy.shape[0] < 3:
        return None
    A = np.column_stack([np.ones(xy.shape[0]), xy])
    rhs = A.T @ z
    AtA = A.T @ A
    # a tiny ridge keeps a degenerate (all-same-x) design solvable
    coef = np.linalg.solve(AtA + 1e-9 * np.eye(3), rhs)
    return coef  # [a, b, c]


def angle_between(a, b) -> float:
    """Angle in degrees between two 3-vectors."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    c = float(a @ b) / (float(np.linalg.norm(a)) * float(np.linalg.norm(b)))
    return float(np.degrees(np.arccos(max(-1.0, min(1.0, c)))))


def rotation_angle_deg(Ra, Rb) -> float:
    """Rotation angle of Ra^T Rb in degrees."""
    R = np.asarray(Ra, dtype=np.float64).T @ np.asarray(Rb, dtype=np.float64)
    tr = float(np.trace(R))
    return float(np.degrees(np.arccos(max(-1.0, min(1.0, (tr - 1.0) / 2.0)))))


# ---------------------------------------------------------------------------
# environment record
# ---------------------------------------------------------------------------

def environment_record() -> dict:
    return {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }
