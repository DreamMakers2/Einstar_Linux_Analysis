"""Quality, coverage, stability and uncertainty diagnostics.

Stopping is deliberately conservative: covariance thresholds are necessary but
never sufficient.  A calibration is only accepted when it also has enough
views, distance/tilt/image coverage, observations in every camera, numerical
rank, parameter stability and an independent held-out check.
"""
from __future__ import annotations

import numpy as np

from . import plate
from .calibrate import CAMERAS, CalibrationResult, CameraIntrinsics, Observation, calibrate

ACCURACY_TARGETS = {
    "fast": {"max_sigma_fx_px": 1.25, "max_sigma_baseline_mm": 0.20,
             "max_sigma_rotation_deg": 0.35, "max_sigma_rgb_mm": 3.0,
             "max_heldout_rms_px": 0.8},
    "standard": {"max_sigma_fx_px": 0.75, "max_sigma_baseline_mm": 0.12,
                 "max_sigma_rotation_deg": 0.20, "max_sigma_rgb_mm": 1.5,
                 "max_heldout_rms_px": 0.5},
    "high": {"max_sigma_fx_px": 0.35, "max_sigma_baseline_mm": 0.06,
             "max_sigma_rotation_deg": 0.10, "max_sigma_rgb_mm": 0.75,
             "max_heldout_rms_px": 0.35},
}


def accuracy_preset(name: str = "standard") -> dict:
    if name not in ACCURACY_TARGETS:
        raise ValueError(f"unknown accuracy target {name!r}; choose {sorted(ACCURACY_TARGETS)}")
    return dict(ACCURACY_TARGETS[name])


MANDATORY = ("views", "distance_bins", "tilt_signs", "image_quadrants",
             "observations", "rank", "stability", "heldout",
             "sigma_fx", "sigma_baseline", "sigma_rotation", "sigma_rgb")


def baseline_sigma(result: CalibrationResult) -> float:
    """First-order sigma of |t_right - R_right t_left| (mm)."""
    if result.info.get("rebased_view") is not None:
        return float(result.info.get("baseline_sigma_mm", float("nan")))
    C = result.covariance
    if C is None:
        return float("nan")
    base = float(np.linalg.norm(result.baseline()))
    eps = 1e-4
    grad = np.zeros(6)  # left t (3, normally fixed), right t (3)
    for k in range(3):
        e = np.zeros(3); e[k] = eps
        extr = dict(result.extrinsics)
        Rl, tl = extr["left"]; extr["left"] = (Rl, tl + e)
        Rl2, tl2 = extr["left"]; Rr, tr = extr["right"]
        grad[k] = (np.linalg.norm(tr - Rr @ tl2) - base) / eps
        extr = dict(result.extrinsics)
        Rr, tr = extr["right"]; extr["right"] = (Rr, tr + e)
        Rl, tl = extr["left"]; Rr2, tr2 = extr["right"]
        grad[3 + k] = (np.linalg.norm(tr2 - Rr2 @ tl) - base) / eps
    # left is the gauge; only the right translation block is in the covariance
    Ct = C[30:33, 30:33]
    return float(np.sqrt(max(grad[3:] @ Ct @ grad[3:], 0.0)))


def relative_pose_sigma(result: CalibrationResult, a: str = "left",
                        b: str = "right") -> dict:
    """Marginal sigma of the relative rotation (deg) and translation (mm)."""
    if result.info.get("rebased_view") is not None:
        rs = result.info.get("relative_sigma", {})
        key = "ir" if (a, b) == ("left", "right") else "rgb"
        return dict(rs.get(key, {"rotation_deg": float("nan"), "translation_mm": float("nan")}))
    C = result.covariance
    if C is None:
        return {"rotation_deg": float("nan"), "translation_mm": float("nan")}
    # relative pose = (R_b R_a^T, t_b - R_b R_a^T t_a); left is identity
    R = result.extrinsics[b][0] @ result.extrinsics[a][0].T
    idx = 27 if b == "right" else 33  # b rvec block
    Cr = C[idx:idx + 3, idx:idx + 3]
    # small-angle covariance: trace of the rotation-vector covariance
    rot = float(np.degrees(np.sqrt(max(np.trace(Cr), 0.0))))
    j = np.zeros(6)
    # numeric translation gradient
    base = float(np.linalg.norm(result.baseline(a, b)))
    eps = 1e-4
    for k in range(3):
        e = np.zeros(3); e[k] = eps
        for jj, (cam, which) in enumerate(((a, 0), (b, 1))):
            extr = dict(result.extrinsics)
            R0, t0 = extr[cam]
            extr[cam] = (R0, t0 + e)
            Ra, ta = extr[a]; Rb, tb = extr[b]
            n = np.linalg.norm(tb - Rb @ Ra.T @ ta)
            j[3 * jj + k] = (n - base) / eps
    ia = 27 if a == "right" else (33 if a == "tex" else None)
    ib = 27 if b == "right" else (33 if b == "tex" else None)
    if ia is None and ib is not None:
        jb = j[3:6]; idxs = list(range(ib + 3, ib + 6))
        trans = float(np.sqrt(max(jb @ C[np.ix_(idxs, idxs)] @ jb, 0.0)))
    elif ib is None and ia is not None:
        ja = j[0:3]; idxs = list(range(ia + 3, ia + 6))
        trans = float(np.sqrt(max(ja @ C[np.ix_(idxs, idxs)] @ ja, 0.0)))
    elif ia is None and ib is None:
        trans = 0.0
    else:
        idxs = list(range(ia + 3, ia + 6)) + list(range(ib + 3, ib + 6))
        trans = float(np.sqrt(max(j @ C[np.ix_(idxs, idxs)] @ j, 0.0)))
    return {"rotation_deg": rot, "translation_mm": trans}


def rgb_alignment(result: CalibrationResult, distance_mm: float = 400.0) -> dict:
    """RGB-to-IR relative pose and its pixel effect at a working distance."""
    s = relative_pose_sigma(result, "left", "tex")
    R = result.extrinsics["tex"][0] @ result.extrinsics["left"][0].T
    t = result.baseline("left", "tex")
    f = result.cameras["left"].fx
    px = float(f * s["translation_mm"] / max(distance_mm, 1.0))
    return {"rotation_deg": s["rotation_deg"],
            "translation_mm": s["translation_mm"],
            "translation_norm_mm": float(np.linalg.norm(t)),
            "pixel_at_distance": px,
            "distance_mm": distance_mm}


def depth_uncertainty(result: CalibrationResult, distance_mm: float,
                      disparity_sigma_px: float = 0.2) -> dict:
    """Predicted depth sigma from fx, baseline and disparity noise.

    Rectified model Z = fx * B / d.  fx and B are correlated (both estimated
    from the same calibration), so the full 2x2 covariance is used.
    """
    C = result.covariance
    fx = result.cameras["left"].fx
    b = float(np.linalg.norm(result.baseline()))
    if C is None:
        return {"distance_mm": distance_mm, "sigma_mm": float("nan")}
    # indices: left fx = 0, right translation block = 30..32
    idx = [0, 30, 31, 32]
    Cf = C[np.ix_(idx, idx)]
    # disparity consistent with the nominal distance: d = fx*B/Z
    d = fx * b / distance_mm
    g = np.zeros(4)
    # dZ/dfx = B/d ; dZ/dB = fx/d ; (t direction)
    g[0] = b / d
    n = np.asarray(result.baseline()) / max(b, 1e-12)
    g[1:4] = fx / d * n
    var = float(g @ Cf @ g)
    # disparity noise
    var += (fx * b / (d * d)) ** 2 * disparity_sigma_px ** 2
    return {"distance_mm": distance_mm, "sigma_mm": float(np.sqrt(max(var, 0.0))),
            "disparity_sigma_px": disparity_sigma_px, "fx": fx, "baseline_mm": b}


def image_coverage(observations: list[Observation],
                   image_size: tuple[int, int] = (1280, 1024),
                   min_points: int = 5) -> dict:
    W, H = image_size
    out = {}
    for cam in CAMERAS:
        quads = np.zeros(4, int)
        n = 0
        views = set()
        for o in observations:
            if o.camera != cam:
                continue
            views.add(o.view)
            for uv in o.uv.values():
                x, y = float(uv[0]), float(uv[1])
                n += 1
                q = (1 if y >= H / 2 else 0) * 2 + (1 if x >= W / 2 else 0)
                quads[q] += 1
        out[cam] = {"n_points": n, "n_views": len(views),
                    "quadrants": [int(q) for q in quads],
                    "quadrants_covered": int(np.sum(quads >= min_points)),
                    "quadrant_fraction": float(np.mean(quads >= min_points))}
    return out


def view_diversity(result: CalibrationResult, image_size: tuple[int, int] = (1280, 1024)) -> dict:
    """Distance and tilt diversity, relative to the median board normal.

    The scanner can be mounted at an angle, so absolute world-axis tilt signs
    are not meaningful.  We subtract the median tilt (a global board/camera
    offset) and then require both signs and a useful range.
    """
    zs, tx, ty = [], [], []
    for R, t in result.views:
        zs.append(float(t[2]))
        n = R[:, 2]
        tx.append(float(np.degrees(np.arctan2(n[1], max(abs(n[2]), 1e-9)))))
        ty.append(float(np.degrees(np.arctan2(n[0], max(abs(n[2]), 1e-9)))))
    zs = np.array(zs); tx = np.array(tx); ty = np.array(ty)
    bins = np.floor((zs - 200.0) / 50.0).astype(int)
    txr = tx - float(np.median(tx)); tyr = ty - float(np.median(ty))
    return {"distances_mm": zs.tolist(),
            "distance_bins": sorted(set(int(b) for b in bins)),
            "distance_bin_count": int(len(set(bins))),
            "tilt_x_signs": [int(np.sum(txr > 5)), int(np.sum(txr < -5))],
            "tilt_y_signs": [int(np.sum(tyr > 5)), int(np.sum(tyr < -5))],
            "tilt_x_range_deg": float(tx.max() - tx.min()),
            "tilt_y_range_deg": float(ty.max() - ty.min()),
            "tilt_x_deg": tx.tolist(), "tilt_y_deg": ty.tolist(),
            "note": "signs are relative to the median tilt; absolute values are in tilt_*_deg"}


def parameter_stability(observations: list[Observation], result: CalibrationResult,
                        pitch: float, drop: int = 1,
                        max_nfev: int = 600) -> dict:
    """Recalibrate without the last ``drop`` views and compare rig parameters."""
    used = sorted(set(int(o.view) for o in observations))
    if len(used) <= drop + 2:
        return {"ok": False, "reason": "too few views"}
    keep = set(used[:-drop])
    obs = [o for o in observations if int(o.view) in keep]
    # use the same intrinsic initialisation as the original fit would have had
    from .calibrate import CameraIntrinsics
    init = {c: CameraIntrinsics(result.cameras[c].fx, result.cameras[c].fy,
                                result.cameras[c].cx, result.cameras[c].cy,
                                result.cameras[c].kc.copy()) for c in CAMERAS}
    try:
        r2 = calibrate(obs, pitch, init, free_kc=bool(result.info.get("free_kc", False)),
                       free_intrinsics=True, max_nfev=max_nfev)
    except Exception as exc:
        return {"ok": False, "reason": f"recalibration failed: {exc}"}
    # compare at the same metric scale (a rescaled/anchor result must not look
    # unstable merely because the subset fit has a different raw scale)
    try:
        target = float(np.linalg.norm(result.baseline()))
        if target > 1e-9 and np.linalg.norm(r2.baseline()) > 1e-9:
            r2 = r2.rescaled(target_baseline=target, anchor_sigma_mm=0.0)
    except Exception:
        pass
    # compare the rig vector in the common gauge (left identity)
    names = result.info.get("param_names") or []
    a = result.vector(); b = r2.vector()
    # only free rig parameters; view indices differ, so compare 0..38
    fm = np.asarray(result.info.get("free_mask", np.ones(len(a), bool)), bool)
    idx = np.where(fm[:39])[0]
    sd = np.sqrt(np.maximum(np.diag(result.covariance), 0.0)) if result.covariance is not None else np.ones(len(a))
    z = np.abs(a[idx] - b[idx]) / np.maximum(sd[idx], 1e-12)
    # for rotations, compare the relative angle rather than the raw rvec
    return {"ok": bool(np.nanmax(z) < 4.0), "max_z": float(np.nanmax(z)),
            "n_views_kept": len(keep), "n_views_dropped": len(used) - len(keep),
            "per_param": {names[i] if i < len(names) else str(i): float(v)
                          for i, v in zip(idx, z)}}


def stopping_report(result: CalibrationResult,
                    observations: list[Observation] | None = None,
                    heldout_rms_px: float | None = None,
                    pitch: float = plate.DEFAULT_PITCH_MM,
                    min_views: int = 10,
                    min_distance_bins: int = 3,
                    min_quadrant_fraction: float = 0.75,
                    min_obs_per_camera: int = 300,
                    max_sigma_fx_px: float = 0.75,
                    max_sigma_baseline_mm: float = 0.12,
                    max_sigma_rotation_deg: float = 0.20,
                    max_sigma_rgb_mm: float = 1.5,
                    max_heldout_rms_px: float = 0.5,
                    max_information_condition: float = 1e7,
                    require_heldout: bool = True,
                    require_stability: bool = True,
                    compute_stability: bool = False) -> dict:
    """Explainable, conservative stopping decision.

    Covariance thresholds are necessary but never sufficient.  Every mandatory
    check is reported with its value, limit and pass flag; ``reasons_failed``
    is the exact explanation for a rejection.
    """
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    def add(name, value, limit, passed, kind="min", note=""):
        checks[name] = {"value": value, "limit": limit, "pass": bool(passed),
                        "kind": kind, "mandatory": name in MANDATORY, "note": note}
        if not passed:
            reasons.append(f"{name}: {value} vs {limit}")

    n_views = len(result.info.get("views_used", result.views))
    add("views", n_views, min_views, n_views >= min_views)

    # geometric diversity
    div = view_diversity(result)
    add("distance_bins", div["distance_bin_count"], min_distance_bins,
        div["distance_bin_count"] >= min_distance_bins)
    sign_ok = (all(s >= 1 for s in div["tilt_x_signs"])
               and all(s >= 1 for s in div["tilt_y_signs"])
               and div["tilt_x_range_deg"] >= 10.0
               and div["tilt_y_range_deg"] >= 10.0)
    add("tilt_signs", {"x": div["tilt_x_signs"], "y": div["tilt_y_signs"],
                       "range_x_deg": div["tilt_x_range_deg"],
                       "range_y_deg": div["tilt_y_range_deg"]},
        ">=1 each sign and >=10 deg range", sign_ok)

    # image coverage and per-camera observations
    cov = image_coverage(observations) if observations else None
    if cov is None:
        add("image_quadrants", None, min_quadrant_fraction, False, note="observations not supplied")
        add("observations", None, min_obs_per_camera, False, note="observations not supplied")
    else:
        qf = min(cov[c]["quadrant_fraction"] for c in CAMERAS)
        add("image_quadrants", qf, min_quadrant_fraction, qf >= min_quadrant_fraction)
        nmin = min(cov[c]["n_points"] for c in CAMERAS)
        add("observations", nmin, min_obs_per_camera, nmin >= min_obs_per_camera)

    # Numerical rank: use the Jacobian condition (the marginal information is
    # ill-conditioned by construction because nuisance poses are eliminated).
    ci = result.info.get("covariance", {}) or {}
    cond = float(ci.get("condition", float("inf")))
    rank_kept = int(ci.get("rank_kept", 0))
    n_free = int(ci.get("n_free", 0))
    rank_ok = cond <= max_information_condition and rank_kept == n_free and n_free > 0
    add("rank", {"condition": cond, "rank_kept": rank_kept, "n_free": n_free},
        max_information_condition, rank_ok, kind="max")

    # marginal uncertainty
    C = result.covariance
    if C is None:
        add("sigma_fx", float("nan"), max_sigma_fx_px, False)
        add("sigma_baseline", float("nan"), max_sigma_baseline_mm, False)
        add("sigma_rotation", float("nan"), max_sigma_rotation_deg, False)
        add("sigma_rgb", float("nan"), max_sigma_rgb_mm, False)
    else:
        sd = np.sqrt(np.maximum(np.diag(C), 0.0))
        fx_sig = max(float(sd[0]), float(sd[9]), float(sd[18]))
        add("sigma_fx", fx_sig, max_sigma_fx_px, fx_sig <= max_sigma_fx_px)
        bs = baseline_sigma(result)
        add("sigma_baseline", bs, max_sigma_baseline_mm, bs <= max_sigma_baseline_mm)
        rs = relative_pose_sigma(result, "left", "right")["rotation_deg"]
        add("sigma_rotation", rs, max_sigma_rotation_deg, rs <= max_sigma_rotation_deg)
        rgb = rgb_alignment(result)["translation_mm"]
        add("sigma_rgb", rgb, max_sigma_rgb_mm, rgb <= max_sigma_rgb_mm)

    # independent held-out validation
    if heldout_rms_px is None:
        add("heldout", None, max_heldout_rms_px, not require_heldout,
            note="missing independent held-out measurement" if require_heldout else "not required")
    else:
        add("heldout", float(heldout_rms_px), max_heldout_rms_px,
            float(heldout_rms_px) <= max_heldout_rms_px)

    # parameter stability under dropping the newest view(s)
    stab = None
    if compute_stability and observations is not None:
        stab = parameter_stability(observations, result, pitch)
        add("stability", stab.get("max_z"), 4.0, bool(stab.get("ok", False)),
            kind="max", note=stab.get("reason", ""))
    elif require_stability:
        add("stability", None, 4.0, False, note="not computed")

    ok = all(c["pass"] for c in checks.values() if c["mandatory"])
    return {"ok": bool(ok), "n_views": n_views,
            "baseline_mm": float(np.linalg.norm(result.baseline())),
            "baseline_sigma_mm": baseline_sigma(result),
            "checks": checks, "reasons_failed": reasons,
            "diversity": div, "coverage": cov, "stability": stab,
            "heldout_rms_px": heldout_rms_px,
            "note": "covariance thresholds are necessary but not sufficient"}


def quality_report(result: CalibrationResult,
                   observations: list[Observation] | None = None,
                   heldout_rms_px: float | None = None,
                   target: str = "standard",
                   distances_mm=(300.0, 400.0, 500.0),
                   pitch: float = plate.DEFAULT_PITCH_MM,
                   compute_stability: bool = False,
                   stopping: dict | None = None,
                   bootstrap: dict | None = None) -> dict:
    """Complete, human-readable calibration-quality report.

    Every number is either measured, propagated from the covariance, or a
    model prediction; the report never upgrades synthetic/reprojection evidence
    into a physical-accuracy claim.
    """
    preset = accuracy_preset(target)
    stop = stopping or stopping_report(result, observations,
                                       heldout_rms_px=heldout_rms_px,
                                       pitch=pitch,
                                       compute_stability=compute_stability,
                                       **preset)
    ir = relative_pose_sigma(result, "left", "right")
    rgb = rgb_alignment(result)
    depth = [depth_uncertainty(result, float(d)) for d in distances_mm]
    cov = result.info.get("covariance", {}) or {}
    status = "pass" if stop["ok"] else "fail"
    pitch = float(result.info.get("pitch_mm", plate.DEFAULT_PITCH_MM))
    pitch_source = result.info.get("pitch_source")
    if pitch_source is None:
        pitch_source = plate.current_spec().pitch_source
    src = str(pitch_source).lower()
    # A pitch can be validated by an artefact measurement ("caliper"/"measured")
    # or by the vendor's own specification recovered from the binaries
    # ("vendor_model", see docs/calibration/plate_spec_recovered.md).  The two
    # are kept distinct: the vendor model is the authority for reconstructing
    # EXStar's geometry, not a measurement of the physical plate.
    artefact_validated = any(k in src for k in ("caliper", "measured", "certified",
                                                "calibrated length")) \
        and "unvalidated" not in src and "unresolved" not in src
    vendor_validated = ("vendor_model" in src or "vendor model" in src) \
        and "unvalidated" not in src and "unresolved" not in src
    pitch_validated = bool(artefact_validated or vendor_validated)
    physical_scale = {
        "pitch_mm": pitch,
        "pitch_source": pitch_source,
        "validated": bool(pitch_validated),
        "artefact_validated": bool(artefact_validated),
        "vendor_geometry": bool(vendor_validated),
        "note": ("" if pitch_validated else
                 "metric scale is anchored to the factory baseline / plate pitch; "
                 "an unresolved pitch is a scale-gauge uncertainty that the pixel "
                 "covariance cannot see"),
    }
    warnings = list(stop.get("reasons_failed", []))
    if not pitch_validated:
        warnings.append("pitch unresolved: absolute metric scale is not physically validated")
    if result.info.get("free_kc") is False:
        warnings.append("distortion fixed: a biased factory kc is a model error outside the covariance")
    if int((cov or {}).get("rank_kept", 0)) == 0:
        warnings.append("zero rank: no identifiable parameters")
    if bootstrap and bootstrap.get("ok") and bootstrap.get("ratio_fx", 1.0) < 0.75:
        warnings.append("view-bootstrap spread exceeds the single-fit sigma: reported uncertainty is optimistic")
    return {
        "schema": "einstar-calibration-quality/1",
        "status": status,
        "target": target,
        "target_limits": preset,
        "baseline_mm": stop["baseline_mm"],
        "baseline_sigma_mm": stop["baseline_sigma_mm"],
        "ir_relative": ir,
        "rgb_alignment": rgb,
        "depth_prediction": depth,
        "residual_rms_px": result.residual_rms,
        "residual_max_px": result.residual_max,
        "n_observations": result.n_observations,
        "n_free_parameters": cov.get("n_free"),
        "jacobian_condition": cov.get("condition"),
        "rank_kept": cov.get("rank_kept"),
        "covariance_estimator": cov.get("estimator"),
        "heldout_rms_px": heldout_rms_px,
        "stopping": stop,
        "physical_scale": physical_scale,
        "bootstrap": bootstrap,
        "warnings": warnings,
        "note": "reprojection, relative geometry and propagated uncertainty only; "
                "no physical-accuracy claim without an artefact measurement",
    }


def bootstrap_sigma(observations: list[Observation], result: CalibrationResult,
                    pitch: float = plate.DEFAULT_PITCH_MM,
                    n_boot: int = 6, drop: int = 4, seed: int = 0,
                    max_nfev: int = 600) -> dict:
    """View-resampling spread compared with the single-fit sigma.

    A single-fit covariance does not include view-composition variability; this
    jackknife/bootstrap estimates it directly.  It is intentionally small
    (6 fits) so it can run in a profile check, not in a real-time loop.
    """
    import numpy as _np
    views = sorted(set(int(o.view) for o in observations))
    if len(views) <= drop + 3:
        return {"ok": False, "reason": "too few views"}
    init = {c: CameraIntrinsics(result.cameras[c].fx, result.cameras[c].fy,
                                result.cameras[c].cx, result.cameras[c].cy,
                                result.cameras[c].kc.copy()) for c in CAMERAS}
    rng = _np.random.default_rng(seed)
    fx, base = [], []
    for _ in range(n_boot):
        pick = set(rng.choice(views, size=len(views) - drop, replace=False).tolist())
        obs = [o for o in observations if int(o.view) in pick]
        try:
            r = calibrate(obs, pitch, init, free_kc=bool(result.info.get("free_kc", False)),
                          free_intrinsics=True, max_nfev=max_nfev)
        except Exception:
            continue
        fx.append([r.cameras[c].fx for c in CAMERAS])
        base.append(float(_np.linalg.norm(r.baseline())))
    if len(fx) < 3:
        return {"ok": False, "reason": f"only {len(fx)} successful bootstraps"}
    fx = _np.asarray(fx); base = _np.asarray(base)
    sd = _np.sqrt(_np.diag(result.covariance)) if result.covariance is not None else None
    rep_fx = float(_np.mean([sd[0], sd[9], sd[18]])) if sd is not None else float("nan")
    rep_base = baseline_sigma(result)
    out = {
        "ok": True, "n_boot": int(len(fx)), "drop": int(drop),
        "sigma_fx_bootstrap": float(_np.mean(fx.std(axis=0, ddof=1))),
        "sigma_baseline_bootstrap_mm": float(base.std(ddof=1)),
        "reported_sigma_fx_px": rep_fx,
        "reported_sigma_baseline_mm": rep_base,
        "ratio_fx": float(rep_fx / max(_np.mean(fx.std(axis=0, ddof=1)), 1e-12)),
        "ratio_baseline": float(rep_base / max(base.std(ddof=1), 1e-12)),
        "note": "ratio < 1 means the single-fit sigma underestimates view-composition spread",
    }
    return out
