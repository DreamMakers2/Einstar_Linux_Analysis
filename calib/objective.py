"""Task-space (3-D measurement) uncertainty and information-driven view scoring.

The calibration exists to make *3-D measurements* accurate, not to minimise the
covariance of intrinsic parameters.  This module therefore propagates the rig
covariance to the covariance of a reconstructed 3-D point at sample positions
inside the scanner's usable measurement volume, and scores candidate views by
the reduction of that **task** uncertainty.

Model
-----
A future measurement uses the stereo IR pair (left/right).  For a surface point
``X`` (left-camera frame) the calibrated pipeline undistorts the two pixel
observations with the estimated ``K, kc`` and triangulates the rays.  Holding the
observed pixels fixed, the reconstructed point is a function ``X_hat(theta)`` of
the rig parameters ``theta`` (27 intrinsics + 12 relative-pose values).  The
first-order sensitivity ``G = dX_hat/dtheta`` is computed by finite differences
of the actual triangulation, so the effect of focal length, principal point,
distortion and the relative pose all enter with their correct units.

For a rig covariance ``Sigma_theta`` the 3-D covariance at ``X`` is

    Sigma_X = G Sigma_theta G^T + Sigma_noise

where the second term is the observation-noise floor (pixel noise propagated
through the same triangulation; it does not depend on the calibration and is
therefore constant for view *selection* but is included in reported numbers).

Candidate views are scored by the predicted task objective after adding the
view's information to the marginal rig information (the candidate's six board
pose parameters are eliminated by their Schur complement, exactly as in
:mod:`calib.adaptive`).  Four objectives are provided:

* ``A``     mean trace(Sigma_X)          -- average 3-D variance
* ``D``     mean log det(Sigma_X)        -- volume of the uncertainty ellipsoid
* ``E``     max  lambda_max(Sigma_X)     -- worst-case direction
* ``depth`` mean Var(Z) / Z^2 (relative) -- the scanner's depth accuracy
* ``surface`` mean of the two in-plane variances (lateral surface accuracy)

A-optimality on the task has a useful exact form: for fixed ``G``,
``trace(G C G^T) = trace(C M)`` with ``M = sum_p G_p^T G_p`` (or a weighted
version), so the candidate ranking is a cheap 39x39 linear functional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import cv2
import numpy as np

from . import plate
from .adaptive import (ViewCandidate, _inv_spd, candidate_jacobian, free_rig_mask,
                       rig_covariance, rig_vector)
from .calibrate import CAMERAS, CalibrationResult

NRIG = 39


# ---------------------------------------------------------------------------
# measurement volume
# ---------------------------------------------------------------------------

def measurement_volume(z_range: tuple[float, float] = (200.0, 600.0),
                       half_field: float = 0.35,
                       n_z: int = 5, n_xy: int = 3,
                       depths: Optional[Sequence[float]] = None,
                       lateral: Optional[Sequence[float]] = None) -> np.ndarray:
    """Sample points in the left-camera frame.

    ``half_field`` is the half field of view as a fraction of the distance
    (0.35 -> +-35 % of z in x and y, i.e. a cone slightly inside the 1280x1024
    field).  The default grid covers 200-600 mm, the scanner's working range.
    """
    zs = np.asarray(depths if depths is not None else
                    np.linspace(z_range[0], z_range[1], int(n_z)), float)
    lat = np.asarray(lateral if lateral is not None else
                     np.linspace(-1.0, 1.0, int(n_xy)), float)
    pts = []
    for z in zs:
        for fx in lat:
            for fy in lat:
                pts.append([half_field * float(fx) * z,
                            half_field * float(fy) * z, float(z)])
    return np.asarray(pts, float)


# ---------------------------------------------------------------------------
# rig / projection helpers
# ---------------------------------------------------------------------------

def _split_rig(x: np.ndarray):
    cams, extr = {}, {}
    for i, c in enumerate(CAMERAS):
        v = x[9 * i:9 * i + 9]
        cams[c] = (np.array([[v[0], 0.0, v[2]], [0.0, v[1], v[3]], [0.0, 0.0, 1.0]]),
                   np.asarray(v[4:9], float))
    for i, c in enumerate(("right", "tex")):
        v = x[27 + 6 * i:33 + 6 * i]
        R = cv2.Rodrigues(np.asarray(v[:3], float))[0]
        extr[c] = (R, np.asarray(v[3:6], float))
    extr["left"] = (np.eye(3), np.zeros(3))
    return cams, extr


def _pixels(cams, extr, cam: str, X: np.ndarray) -> np.ndarray:
    R, t = extr[cam]
    K, kc = cams[cam]
    Xc = np.asarray(X, float) @ R.T + t
    z = np.maximum(Xc[:, 2], 1e-9)
    x, y = Xc[:, 0] / z, Xc[:, 1] / z
    k1, k2, p1, p2, k3 = kc
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    return np.stack([K[0, 0] * xd + K[0, 2], K[1, 1] * yd + K[1, 2]], axis=1)


def _ray(cams, extr, cam: str, uv: np.ndarray):
    K, kc = cams[cam]
    n = cv2.undistortPoints(np.asarray(uv, float).reshape(-1, 1, 2), K, kc).reshape(-1, 2)
    R, t = extr[cam]
    d = (n[:, 0:1] * R[0][None, :] + n[:, 1:2] * R[1][None, :] + R[2][None, :])
    return -R.T @ t, d


def _triangulate(cams, extr, uv_l: np.ndarray, uv_r: np.ndarray) -> np.ndarray:
    CL, dL = _ray(cams, extr, "left", uv_l)
    CR, dR = _ray(cams, extr, "right", uv_r)
    w0 = CL - CR
    a = np.einsum("ij,ij->i", dL, dL)
    b = np.einsum("ij,ij->i", dL, dR)
    c = np.einsum("ij,ij->i", dR, dR)
    d = dL @ w0
    e = dR @ w0
    den = np.maximum(a * c - b * b, 1e-12)
    s = (b * e - c * d) / den
    t = (a * e - b * d) / den
    return 0.5 * ((CL + s[:, None] * dL) + (CR + t[:, None] * dR))


def triangulate(rig_vec: np.ndarray, uv_l: np.ndarray, uv_r: np.ndarray) -> np.ndarray:
    """Triangulate pixel correspondences with a rig vector (left frame)."""
    cams, extr = _split_rig(rig_vec)
    return _triangulate(cams, extr, uv_l, uv_r)


# ---------------------------------------------------------------------------
# task sensitivity
# ---------------------------------------------------------------------------

def task_sensitivity(x_rig: np.ndarray, points: np.ndarray,
                     step_rel: float = 1e-6) -> np.ndarray:
    """``G[p, i, j] = d X_p_i / d theta_j`` at ``points`` (left frame, mm)."""
    cams, extr = _split_rig(x_rig)
    uv_l = _pixels(cams, extr, "left", points)
    uv_r = _pixels(cams, extr, "right", points)
    base = _triangulate(cams, extr, uv_l, uv_r)
    G = np.zeros((len(points), 3, NRIG))
    for j in range(NRIG):
        h = step_rel * max(1.0, abs(float(x_rig[j])))
        xp = np.array(x_rig, float, copy=True)
        xp[j] += h
        cams_p, extr_p = _split_rig(xp)
        Xp = _triangulate(cams_p, extr_p, uv_l, uv_r)
        G[:, :, j] = (Xp - base) / h
    return G


def task_metric(G: np.ndarray, weights: Optional[np.ndarray] = None) -> np.ndarray:
    """``M = sum_p w_p G_p^T G_p`` (39x39) for A-optimal task scoring."""
    w = np.ones(len(G)) if weights is None else np.asarray(weights, float)
    return np.einsum("i,inj,ink->jk", w, G, G)


def noise_covariance(x_rig: np.ndarray, points: np.ndarray,
                     sigma_px: float = 0.2) -> np.ndarray:
    """Observation-noise contribution to the 3-D covariance (N,3,3).

    Propagates isotropic pixel noise through the triangulation Jacobian.  This
    is a constant floor for view selection and is reported alongside the
    calibration contribution.
    """
    cams, extr = _split_rig(x_rig)
    uv_l = _pixels(cams, extr, "left", points)
    uv_r = _pixels(cams, extr, "right", points)
    out = np.zeros((len(points), 3, 3))
    h = 1e-4
    base = _triangulate(cams, extr, uv_l, uv_r)
    for ax in range(2):
        for cam, uv in (("l", uv_l), ("r", uv_r)):
            for sign in (+1, -1):
                uvp = uv.copy(); uvr = uv_r.copy(); uvl = uv_l.copy()
                if cam == "l":
                    uvl[:, ax] += sign * h
                else:
                    uvr[:, ax] += sign * h
                d = (_triangulate(cams, extr, uvl, uvr) - base) / (sign * h)
                out += sigma_px ** 2 * np.einsum("pi,pj->pij", d, d)
    return out


# ---------------------------------------------------------------------------
# objectives
# ---------------------------------------------------------------------------

def objective_from_cov(cov: np.ndarray, kind: str = "A",
                       points: Optional[np.ndarray] = None,
                       weights: Optional[np.ndarray] = None) -> float:
    """Scalar task objective from per-point 3x3 covariances (N,3,3)."""
    w = np.ones(len(cov)) if weights is None else np.asarray(weights, float)
    w = w / max(w.sum(), 1e-12)
    if kind == "A":
        return float(np.sum(w * np.einsum("pii->p", cov)))
    if kind == "D":
        sign, logdet = np.linalg.slogdet(cov)
        vals = np.where(sign > 0, logdet, np.inf)
        return float(np.sum(w * vals))
    if kind == "E":
        return float(np.max(np.linalg.eigvalsh(cov)[:, -1]))
    if kind == "depth":
        z = points[:, 2] if points is not None else np.ones(len(cov))
        return float(np.sum(w * cov[:, 2, 2] / np.maximum(z, 1e-9) ** 2))
    if kind == "surface":
        return float(np.sum(w * (cov[:, 0, 0] + cov[:, 1, 1])))
    raise ValueError(f"unknown task objective {kind!r}")


def rig_covariance_gn(result: CalibrationResult) -> np.ndarray:
    """Gauss-Newton (Fisher) marginal rig covariance in mm/px units.

    ``sigma^2 * inv(marginal_information)`` on the free rig block.  This is the
    covariance implied by the Fisher information used for experimental design;
    the robust sandwich in :mod:`calib.calibrate` is a *conservative inference*
    covariance and is deliberately not used for design (mixing the two made a
    single view appear to remove tens of percent of the variance).
    """
    free = free_rig_mask(result)
    idx = np.where(free)[0]
    out = np.zeros((NRIG, NRIG))
    marg = getattr(result, "marginal_information", None)
    if marg is None:
        return rig_covariance(result)
    sig2 = _observation_variance(result)
    sub = np.array(marg, copy=True)[np.ix_(idx, idx)]
    out[np.ix_(idx, idx)] = sig2 * _inv_spd(sub)
    return out


def rig_covariance_robust(result: CalibrationResult) -> np.ndarray:
    """Robust sandwich marginal covariance (conservative diagnostics)."""
    return rig_covariance(result)


def _rig_cov_from_information(H: np.ndarray, free: np.ndarray) -> np.ndarray:
    idx = np.where(free)[0]
    out = np.zeros((NRIG, NRIG))
    if len(idx) == 0:
        return out
    out[np.ix_(idx, idx)] = _inv_spd(H[np.ix_(idx, idx)])
    return out


def task_covariance_from_G(G: np.ndarray, C: np.ndarray) -> np.ndarray:
    """Per-point 3x3 covariance from point sensitivities and a rig covariance."""
    return np.einsum("pij,jk,plk->pil", G, C, G)


def task_covariance(result: CalibrationResult, points: np.ndarray,
                    G: Optional[np.ndarray] = None,
                    sigma_px: float = 0.0) -> np.ndarray:
    """Per-point 3x3 covariance from the calibration rig covariance."""
    free = free_rig_mask(result)
    C = rig_covariance(result)
    if G is None:
        G = task_sensitivity(rig_vector(result), points)
    idx = np.where(free)[0]
    cov = task_covariance_from_G(G[:, :, idx], C[np.ix_(idx, idx)])
    if sigma_px > 0:
        cov = cov + noise_covariance(rig_vector(result), points, sigma_px)
    return cov


@dataclass
class TaskReport:
    points: np.ndarray
    cov: np.ndarray
    kind: str
    value: float
    sigma_mm: np.ndarray
    depth_sigma_mm: np.ndarray
    G: Optional[np.ndarray] = None
    free: Optional[np.ndarray] = None
    M: Optional[np.ndarray] = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        s = np.sqrt(np.maximum(np.einsum("pii->p", self.cov), 0.0))
        return {"kind": self.kind, "value": self.value, "n_points": int(len(self.points)),
                "sigma_mm_median": float(np.median(s)),
                "sigma_mm_max": float(np.max(s)),
                "depth_sigma_mm_median": float(np.median(self.depth_sigma_mm)),
                "depth_sigma_mm_max": float(np.max(self.depth_sigma_mm)),
                "points": self.points.tolist(),
                "sigma_mm": s.tolist(),
                "meta": self.meta}


def evaluate_task(result: CalibrationResult, kind: str = "A",
                  points: Optional[np.ndarray] = None,
                  sigma_px: float = 0.0,
                  use_cache: bool = True,
                  cache: Optional[dict] = None,
                  covariance: str = "gn") -> TaskReport:
    if points is None:
        points = measurement_volume()
    x = rig_vector(result)
    free = free_rig_mask(result)
    G = None
    if cache is not None and "G" in cache:
        G = cache["G"]
    if G is None:
        G = task_sensitivity(x, points)
    C = rig_covariance_gn(result) if covariance == "gn" else rig_covariance(result)
    idx = np.where(free)[0]
    cov = task_covariance_from_G(G[:, :, idx], C[np.ix_(idx, idx)])
    if sigma_px > 0:
        cov = cov + noise_covariance(x, points, sigma_px)
    val = objective_from_cov(cov, kind, points=points)
    depth_sig = np.sqrt(np.maximum(cov[:, 2, 2], 0.0))
    sigma = np.sqrt(np.maximum(np.einsum("pii->p", cov), 0.0))
    M = task_metric(G)
    if cache is not None:
        cache["G"] = G
        cache["points"] = points
        cache["M"] = M
    return TaskReport(points=points, cov=cov, kind=kind, value=val,
                      sigma_mm=sigma, depth_sigma_mm=depth_sig, G=G,
                      free=free, M=M,
                      meta={"volume": {"z": [float(points[:, 2].min()),
                                             float(points[:, 2].max())],
                                       "n": int(len(points))},
                            "covariance": covariance})


# ---------------------------------------------------------------------------
# candidate scoring
# ---------------------------------------------------------------------------

@dataclass
class CandidateScore:
    candidate: ViewCandidate
    gain: float
    utility: float
    objective_before: float
    objective_after: float
    cov_rig_after: np.ndarray
    n_markers: int
    effort: float
    sigma_before_mm: float = 0.0
    sigma_after_mm: float = 0.0
    reasons: list = field(default_factory=list)

    @property
    def delta_sigma_mm(self) -> float:
        return max(0.0, self.sigma_before_mm - self.sigma_after_mm)

    def as_dict(self) -> dict:
        return {"label": self.candidate.label, "distance_mm": self.candidate.distance_mm,
                "tilt_x_deg": self.candidate.tilt_x_deg,
                "tilt_y_deg": self.candidate.tilt_y_deg,
                "roll_deg": self.candidate.roll_deg, "gain": self.gain,
                "utility": self.utility, "effort": self.effort,
                "n_markers": self.n_markers,
                "objective_before": self.objective_before,
                "objective_after": self.objective_after,
                "sigma_before_mm": self.sigma_before_mm,
                "sigma_after_mm": self.sigma_after_mm,
                "delta_sigma_mm": self.delta_sigma_mm,
                "reasons": self.reasons}


def _candidate_joint_jacobian(x_rig, R, t, pitch, free, min_stereo=8,
                              image_size=(1280, 1024), margin=4.0):
    """Candidate Jacobian with non-visible residuals masked out.

    ``candidate_jacobian`` emits all 40 lattice points for all three cameras.
    A predicted residual for a marker outside the image is not an observation
    and must not contribute information, so those rows are zeroed.  The view is
    rejected unless at least ``min_stereo`` markers are inside *both* IR
    images and the four large markers are inside the left image.
    """
    J = candidate_jacobian(x_rig, R, t, pitch)
    if J is None:
        return None
    cams, extr = _split_rig(x_rig)
    obj = plate.board_points_mm(pitch)
    Xl = obj @ np.asarray(R, float).T + np.asarray(t, float)
    vis = {}
    w, h = image_size
    for c in CAMERAS:
        uv = _pixels(cams, extr, c, Xl)
        vis[c] = ((uv[:, 0] > margin) & (uv[:, 0] < w - margin) &
                  (uv[:, 1] > margin) & (uv[:, 1] < h - margin))
    stereo = vis["left"] & vis["right"]
    if int(stereo.sum()) < min_stereo:
        return None
    # in the corpus the plates always show the 4 large markers; require the
    # three on-lattice ones in the left image so the orientation code is
    # available (the off-lattice marker is not part of the lattice array).
    big_idx = [i for i, (u, v) in enumerate(plate.lattice_uv())
               if (float(u), float(v)) in {(2.0, 1.0), (4.0, 1.0), (5.0, 1.0)}]
    if not all(vis["left"][i] for i in big_idx):
        return None
    rows = []
    n_per_cam = 2 * len(obj)
    for ci, c in enumerate(CAMERAS):
        blk = J[ci * n_per_cam:(ci + 1) * n_per_cam]
        m = np.repeat(vis[c], 2)
        blk = blk.copy()
        blk[~m] = 0.0
        rows.append(blk)
    return np.vstack(rows)


def _observation_variance(result: CalibrationResult, sigma_px: float = 0.0) -> float:
    """Effective per-pixel variance used to scale candidate information.

    The stored rig covariance is ``sigma^2 (J^T J)^-1`` (or the robust
    sandwich analogue), so a candidate view's ``J^T J`` must be divided by the
    same ``sigma^2`` before it is added to ``inv(C_rig)``.  Without this factor
    (the historical behaviour of ``calib.adaptive``) the candidate information
    is mis-weighted by the residual scale, which is ~0.13 px^2 here.
    """
    if sigma_px > 0:
        return float(sigma_px) ** 2
    ci = result.info.get("covariance", {}) or {}
    s = float(ci.get("sigma_px", 0.0) or 0.0)
    if s <= 0:
        s = float(result.residual_rms or 1.0)
    return max(s * s, 1e-12)


def score_candidate(result: CalibrationResult, cand: ViewCandidate,
                    points: Optional[np.ndarray] = None,
                    kind: str = "A", sigma_px: float = 0.0,
                    cache: Optional[dict] = None,
                    effort: float = 0.0,
                    effort_weight: float = 0.05,
                    pitch: float = plate.DEFAULT_PITCH_MM) -> Optional[CandidateScore]:
    if points is None:
        points = measurement_volume()
    x = rig_vector(result)
    free = free_rig_mask(result)
    idx = np.where(free)[0]
    C = rig_covariance_gn(result)
    if cache is not None and "G" in cache:
        G = cache["G"]; M = cache.get("M")
    else:
        G = task_sensitivity(x, points)
        M = task_metric(G)
        if cache is not None:
            cache.update(G=G, M=M, points=points)
    cov_before = np.einsum("pij,jk,plk->pil", G[:, :, idx], C[np.ix_(idx, idx)],
                           G[:, :, idx])
    obj_before = objective_from_cov(cov_before, kind, points=points)
    J = _candidate_joint_jacobian(x, cand.R, cand.t, pitch, free)
    if J is None:
        return None
    Jr = J[:, :NRIG][:, idx]
    Jv = J[:, NRIG:]
    Hv = Jv.T @ Jv
    try:
        Hv_inv = np.linalg.pinv(Hv, rcond=1e-10)
    except np.linalg.LinAlgError:
        return None
    H_add = Jr.T @ Jr - Jr.T @ Jv @ Hv_inv @ Jv.T @ Jr
    H_add = H_add / _observation_variance(result, sigma_px)
    wa, Va = np.linalg.eigh(0.5 * (H_add + H_add.T))
    H_add = (Va * np.clip(wa, 0.0, None)) @ Va.T
    # Prior information must be on the same footing as the candidate's J^T J.
    # inv(robust sandwich covariance) is *not*: the sandwich deliberately
    # inflates C, so using its inverse made a single view appear to remove
    # tens of percent of the variance.  The stored marginal information is
    # A = J^T W J of the accepted views; dividing by the same sigma^2 used for
    # the candidate gives a consistent information budget.
    sig2 = _observation_variance(result, sigma_px)
    marg = getattr(result, "marginal_information", None)
    if marg is not None:
        H0 = np.array(marg, copy=True)[np.ix_(idx, idx)] / sig2
    else:
        H0 = _inv_spd(C[np.ix_(idx, idx)])
    C1 = _inv_spd(H0 + H_add)
    cov_after = np.einsum("pij,jk,plk->pil", G[:, :, idx], C1, G[:, :, idx])
    obj_after = objective_from_cov(cov_after, kind, points=points)
    gain = float(obj_before - obj_after)
    rel = gain / max(abs(obj_before), 1e-30)
    util = float(rel / (1.0 + effort_weight * max(effort, 0.0)))
    n_markers = int(len(plate.lattice_uv()))
    full = np.zeros_like(C)
    full[np.ix_(idx, idx)] = C1
    sig_b = np.sqrt(np.maximum(np.einsum("pii->p", cov_before), 0.0))
    sig_a = np.sqrt(np.maximum(np.einsum("pii->p", cov_after), 0.0))
    return CandidateScore(cand, gain, util, float(obj_before), float(obj_after),
                          full, n_markers, float(effort),
                          float(np.median(sig_b)), float(np.median(sig_a)))


def rank_candidates(result: CalibrationResult,
                    candidates: Iterable[ViewCandidate],
                    points: Optional[np.ndarray] = None,
                    kind: str = "A", sigma_px: float = 0.0,
                    effort_fn=None, effort_weight: float = 0.05,
                    pitch: float = plate.DEFAULT_PITCH_MM,
                    top: int = 10) -> list[CandidateScore]:
    """Rank candidates by relative task-objective gain per unit effort."""
    if points is None:
        points = measurement_volume()
    cache: dict = {}
    scored = []
    for c in candidates:
        e = float(effort_fn(c)) if effort_fn is not None else 0.0
        s = score_candidate(result, c, points, kind, sigma_px, cache,
                            effort=e, effort_weight=effort_weight, pitch=pitch)
        if s is not None:
            scored.append(s)
    scored.sort(key=lambda s: s.utility, reverse=True)
    return scored[:top]


# ---------------------------------------------------------------------------
# effort model (operator cost of reaching a viewpoint)
# ---------------------------------------------------------------------------

def pose_effort(current_R: np.ndarray, current_t: np.ndarray,
                cand: ViewCandidate) -> float:
    """Dimensionless operator effort to move from one board pose to another.

    The scanner is hand-held; moving it to a new viewpoint costs roughly a
    fixed repositioning plus translation and rotation.  The weights are a
    *design choice* (documented, not measured): 150 mm and 25 deg are treated
    as one "unit" of effort, the base cost is 0.15.
    """
    Cc = -np.asarray(current_R, float).T @ np.asarray(current_t, float)
    Ct = -np.asarray(cand.R, float).T @ np.asarray(cand.t, float)
    dtrans = float(np.linalg.norm(Ct - Cc))
    dR = np.asarray(cand.R, float) @ np.asarray(current_R, float).T
    ang = float(np.degrees(np.arccos(np.clip((np.trace(dR) - 1.0) / 2.0, -1.0, 1.0))))
    # lateral (in the current camera frame) is what the operator has to move
    dcam = np.asarray(current_R, float) @ (Ct - Cc)
    lateral = float(np.linalg.norm(dcam[:2]))
    depth = abs(float(dcam[2]))
    return 0.15 + lateral / 150.0 + depth / 150.0 + ang / 25.0
