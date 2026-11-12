"""Identifiability diagnostics: information spectrum, correlations, verdicts.

Two information matrices are used and they are not the same object:

* ``S`` (``Fit.marginal_information``) is the Schur complement of the rig block
  after eliminating the board poses -- the information the *data* carries about
  the rig, with the nuisance poses integrated out.  Its eigenvectors are the
  identifiable/weak/unobservable combinations.
* ``C_rr = S^-1`` (``Fit.rig_covariance``) is the marginal covariance of the rig
  parameters.  Parameter sigmas and correlations are read from it.

Both are reported at the factory solution (no fitting, so the answer cannot
depend on an optimiser) and at the fitted solution.
"""

from __future__ import annotations

import numpy as np

from typing import Optional

from calib import plate
from calib.calibrate import CAMERAS
from calib.quality import ACCURACY_TARGETS

from .models import N_RIG, INTR_NAMES, EXTR_NAMES, Fit, ModelSpec

#: classification thresholds, taken from the project's own accuracy presets
#: ``calib.quality.ACCURACY_TARGETS``: "well" = the high preset, "weak" = the
#: fast preset (anything looser than fast is treated as unobservable).
WELL = ACCURACY_TARGETS["high"]
FAST = ACCURACY_TARGETS["fast"]

#: intrinsic parameters whose sigma is directly in pixels
PX_PARAMS = {"fx", "fy", "cx", "cy"}
#: distortion parameters: converted to a pixel effect at the observed marker radius
DIST_PARAMS = {"k1", "k2", "p1", "p2", "k3"}


def rig_names() -> list[str]:
    out = []
    for c in CAMERAS:
        out += [f"{c}.{f}" for f in INTR_NAMES]
    for c in ("right", "tex"):
        out += [f"{c}.{f}" for f in EXTR_NAMES]
    return out


# --------------------------------------------------------------------------
# spectrum
# --------------------------------------------------------------------------

def scaled_spectrum(S: np.ndarray, names: list[str], free: np.ndarray,
                    top: int = 10) -> dict:
    """Eigen-spectrum of the column-normalised marginal information.

    The raw information matrix mixes parameter units (px, mm, rad, k1), so its
    condition number is meaningless.  Normalising by the diagonal
    (``S_s = D S D``, ``D = diag(1/sqrt(S_ii))``) makes the eigenvalues
    dimensionless and unit-invariant; ``1 - lambda`` is the fraction of a
    parameter's own information that is *not* available in that direction.
    """
    idx = np.where(free)[0]
    if len(idx) < 2:
        return {"n_free_rig": int(len(idx)), "note": "too few free rig parameters"}
    Sf = S[np.ix_(idx, idx)]
    d = np.sqrt(np.maximum(np.diag(Sf), 0.0))
    good = d > 0
    idx = idx[good]; d = d[good]
    Sf = S[np.ix_(idx, idx)]
    if len(idx) < 2:
        return {"n_free_rig": int(len(idx)), "note": "too few positive diagonal entries"}
    D = 1.0 / d
    Ss = Sf * np.outer(D, D)
    Ss = 0.5 * (Ss + Ss.T)
    w, V = np.linalg.eigh(Ss)
    order = np.argsort(w)[::-1]
    w = w[order]; V = V[:, order]
    nm = [names[i] for i in idx]
    out = {
        "n_free_rig": int(len(idx)),
        "eigenvalues": [float(x) for x in w],
        "condition_number_scaled": float(w[0] / w[-1]) if w[-1] > 0 else float("inf"),
        "condition_number_scaled_top": float(w[0] / w[len(w) // 2]) if w[len(w) // 2] > 0 else float("inf"),
        "trace_scaled": float(w.sum()),
        "directions": [],
    }
    for k in range(min(top, len(w))):
        v = V[:, k]
        j = np.argsort(np.abs(v))[::-1][:6]
        out["directions"].append({
            "rank": k,
            "eigenvalue": float(w[k]),
            "singular_value_equivalent": float(np.sqrt(max(w[k], 0.0))),
            "explained_fraction": float(w[k] / w.sum()) if w.sum() else float("nan"),
            "components": [{"param": nm[i], "component": float(v[i]),
                            "sigma_scaled": float(v[i] / d[i])} for i in j],
        })
    return out


def correlations(C: np.ndarray, names: list[str], free: np.ndarray,
                 min_abs: float = 0.5, top: int = 20) -> dict:
    idx = np.where(free)[0]
    if len(idx) < 2:
        return {"pairs": []}
    Cf = C[np.ix_(idx, idx)]
    d = np.sqrt(np.maximum(np.diag(Cf), 0.0))
    good = d > 0
    idx = idx[good]; d = d[good]
    Cf = C[np.ix_(idx, idx)]
    D = 1.0 / d
    R = Cf * np.outer(D, D)
    np.fill_diagonal(R, 1.0)
    pairs = []
    for i in range(len(idx)):
        for j in range(i + 1, len(idx)):
            if abs(R[i, j]) >= min_abs:
                pairs.append((abs(float(R[i, j])), names[idx[i]], names[idx[j]],
                              float(R[i, j])))
    pairs.sort(key=lambda z: -z[0])
    out = {"n_pairs_above_threshold": len(pairs),
           "threshold": float(min_abs),
           "pairs": [{"a": a, "b": b, "r": r} for _, a, b, r in pairs[:top]]}
    # the largest eigenvalue of the correlation matrix (= worst collinearity)
    w = np.linalg.eigvalsh(R)
    out["corr_eig_max"] = float(w.max())
    out["corr_eig_min"] = float(w.min())
    out["corr_condition"] = float(w.max() / w.min()) if w.min() > 0 else float("inf")
    return out


# --------------------------------------------------------------------------
# per-parameter verdicts
# --------------------------------------------------------------------------

def _pixel_effect(fit: Fit, obs, param_index: int, sigma: float,
                  names: list[str]) -> Optional[float]:
    """Max |Δuv| over the observed markers when one parameter moves by 1 sigma."""
    x = fit.x.copy()
    x[param_index] += sigma
    base = _project_all(fit, obs)
    pert = _project_all(fit, obs, x)
    if not base:
        return None
    d = [float(np.max(np.linalg.norm(a - b, axis=1)))
         for a, b in zip(base, pert) if len(a)]
    return max(d) if d else None


def _project_all(fit: Fit, obs, x=None):
    from .models import Problem
    prob = Problem(obs, ModelSpec({}, {}, False, "project"), fit.pitch_mm)
    if x is None:
        cams, extr, views = fit.cams, fit.extr, fit.views
    else:
        cams, extr, views = prob.unpack(x)
    remap = fit.info.get("views_remap", {})
    uv_index = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    obj = plate.board_points_mm(fit.pitch_mm)
    out = []
    for o in obs:
        vi = remap.get(int(o.view))
        if vi is None or vi >= len(views):
            out.append(np.zeros((0, 2)))
            continue
        keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_index]
        if not keys:
            out.append(np.zeros((0, 2)))
            continue
        idx = [uv_index[(float(k[0]), float(k[1]))] for k in keys]
        X = obj[idx]
        Rv, tv = views[vi]
        Rc, tc = extr[o.camera]
        Xc = (X @ Rv.T + tv) @ Rc.T + tc
        g = cams[o.camera]
        z = np.maximum(Xc[:, 2], 1e-6)
        a, b = Xc[:, 0] / z, Xc[:, 1] / z
        k1, k2, p1, p2, k3 = np.asarray(g.kc, float)[:5]
        r2 = a * a + b * b
        radial = 1.0 + k1 * r2 + k2 * r2 ** 2 + k3 * r2 ** 3
        xd = a * radial + 2 * p1 * a * b + p2 * (r2 + 2 * a * a)
        yd = b * radial + p1 * (r2 + 2 * b * b) + 2 * p2 * a * b
        u = g.fx * xd + g.skew * g.fx * yd + g.cx
        v = g.fy * yd + g.cy
        out.append(np.stack([u, v], 1))
    return out


def parameter_table(fit: Fit, obs, free_mask: np.ndarray) -> list[dict]:
    """One row per rig parameter: sigma, 1-sigma pixel effect, verdict."""
    names = rig_names()
    sig = np.sqrt(np.maximum(np.diag(fit.rig_covariance), 0.0))
    rows = []
    for i, n in enumerate(names):
        cam, base = n.split(".")
        free = bool(free_mask[i]) if i < len(free_mask) else False
        row = {"param": n, "camera": cam, "name": base, "free": free,
               "sigma": float(sig[i]) if free else 0.0,
               "value": _value_of(fit, cam, base)}
        if not free:
            row.update(verdict="fixed", effect_px=None,
                       note="held at the factory / start value")
            rows.append(row); continue
        if base in PX_PARAMS or base == "skew_px":
            eff = float(sig[i])
            row["effect_px"] = eff
            row["effect_basis"] = "1 sigma in pixels"
        elif base in DIST_PARAMS:
            eff = _pixel_effect(fit, obs, i, sig[i], names)
            row["effect_px"] = eff
            row["effect_basis"] = "max |duv| over observed markers"
        elif base in ("rx", "ry", "rz"):
            eff = float(np.degrees(sig[i]))
            row["effect_px"] = None
            row["effect_deg"] = eff
            row["effect_basis"] = "1 sigma in degrees"
        else:
            eff = float(sig[i])
            row["effect_px"] = None
            row["effect_mm"] = eff
            row["effect_basis"] = "1 sigma in millimetres"
        row["verdict"] = _verdict(base, row)
        rows.append(row)
    return rows


def _value_of(fit: Fit, cam: str, base: str):
    if base in PX_PARAMS or base in DIST_PARAMS or base == "skew_px":
        g = fit.cams[cam]
        if base == "skew_px":
            return float(g.skew * g.fx)
        if base in PX_PARAMS:
            return float(getattr(g, base))
        j = {"k1": 0, "k2": 1, "p1": 2, "p2": 3, "k3": 4}[base]
        return float(g.kc[j])
    R, t = fit.extr[cam]
    k = {"rx": 0, "ry": 1, "rz": 2, "tx": 0, "ty": 1, "tz": 2}[base]
    if base.startswith("r"):
        from calib.geometry import rvec_from_R
        return float(rvec_from_R(R)[k])
    return float(t[k])


def _verdict(base: str, row: dict) -> str:
    if base in ("fx", "fy", "cx", "cy"):
        e = row["effect_px"]
        if e <= WELL["max_sigma_fx_px"]:
            return "constrained"
        if e <= FAST["max_sigma_fx_px"]:
            return "weak"
        return "unobservable"
    if base == "skew_px":
        e = row["effect_px"]
        return "constrained" if e <= 0.02 else ("weak" if e <= 0.1 else "unobservable")
    if base in DIST_PARAMS:
        e = row["effect_px"]
        if e is None:
            return "unknown"
        return "constrained" if e <= 0.10 else ("weak" if e <= 0.50 else "unobservable")
    if base in ("rx", "ry", "rz"):
        e = row["effect_deg"]
        return "constrained" if e <= WELL["max_sigma_rotation_deg"] else (
            "weak" if e <= FAST["max_sigma_rotation_deg"] else "unobservable")
    e = row["effect_mm"]
    # the baseline (a translation norm) is the calibrated quantity that matters
    return "constrained" if e <= WELL["max_sigma_baseline_mm"] else (
        "weak" if e <= FAST["max_sigma_baseline_mm"] else "unobservable")


def summarise_verdicts(rows: list[dict]) -> dict:
    out: dict[str, list[str]] = {"constrained": [], "weak": [], "unobservable": [],
                                 "fixed": [], "unknown": []}
    for r in rows:
        out.setdefault(r["verdict"], []).append(r["param"])
    return {k: v for k, v in out.items()}


# --------------------------------------------------------------------------
# depth / focal trade-off
# --------------------------------------------------------------------------

def f_vs_depth_correlation(fit: Fit, obs, free_mask: np.ndarray) -> dict:
    """Correlation between a focal length and the mean plate distance.

    ``dbar`` is the mean over views of the board-origin z in the left camera; it
    is a linear functional of the free view translations, so its covariance with
    ``fx`` follows from the joint covariance of the free parameters without any
    refit.
    """
    names = fit.param_names
    n = len(names)
    if fit.covariance is None:
        return {"note": "no covariance available for this model"}
    sig = np.sqrt(np.maximum(np.diag(fit.covariance), 0.0))
    # dbar = mean over used views of view.tz
    remap = fit.info.get("views_remap", {})
    used = sorted(set(remap[int(o.view)] for o in obs if int(o.view) in remap))
    if not used:
        return {}
    w = np.zeros(n)
    for vi in used:
        w[names.index(f"view{vi}.tz")] = 1.0 / len(used)
    sd_bar = float(np.sqrt(max(w @ fit.covariance @ w, 0.0)))
    out = {"n_views_in_dbar": len(used), "sigma_dbar_mm": sd_bar}
    for cam in CAMERAS:
        for f in ("fx", "fy"):
            nm = f"{cam}.{f}"
            if nm not in names:
                continue
            i = names.index(nm)
            if not free_mask[i] or sig[i] <= 0 or sd_bar <= 0:
                continue
            cov = float(fit.covariance[i] @ w)
            r = cov / (sig[i] * sd_bar)
            out[f"corr_{nm}_dbar"] = float(r)
            out[f"sigma_{nm}_px"] = float(sig[i])
            # 1 sigma of f in relative terms == the same relative depth scale error
            out[f"rel_sigma_{nm}"] = float(sig[i] / getattr(fit.cams[cam], f))
    return out


def profile_table(scales, rms, z_mean, n_residuals, sigma_px) -> dict:
    """Turn a fixed-focal sweep into a profile-likelihood table."""
    rms = np.asarray(rms, float)
    z = np.asarray(z_mean, float)
    ok = np.isfinite(rms)
    if not ok.any():
        return {"note": "all fixed-focal fits failed"}
    k = int(np.nanargmin(rms))
    out = {"scales": [float(s) for s in scales],
           "rms_px": [float(x) for x in rms],
           "mean_depth_mm": [float(x) for x in z],
           "best_scale": float(scales[k]),
           "best_rms_px": float(rms[k]),
           "depth_over_focal_ratio": [float(z[i] / scales[i]) for i in range(len(scales))],
           "delta_rms_per_percent": [],
           "chi2_per_percent": []}
    s0 = np.asarray(scales, float)
    for i in range(len(s0)):
        out["delta_rms_per_percent"].append(
            float((rms[i] - rms[k]) / max(abs(s0[i] - s0[k]) * 100.0, 1e-12)))
        chi2 = float(n_residuals * (rms[i] ** 2 - rms[k] ** 2) / max(sigma_px ** 2, 1e-12))
        out["chi2_per_percent"].append(float(chi2 / max(abs(s0[i] - s0[k]) * 100.0, 1e-12)))
    # curvature-based 1-sigma on the common focal scale: chi2 = chi2_min + 1
    try:
        p = np.polyfit(s0[ok], (rms ** 2)[ok], 2)
        # rms^2(s) = a s^2 + b s + c ; chi2(s) = N (rms^2 - rms_min^2)/sigma^2
        a = p[0]
        chi2 = n_residuals * (np.polyval(p, s0) - np.min(np.polyval(p, s0[ok]))) / sigma_px ** 2
        # sigma_s = 1/sqrt(a * N / sigma^2) for a parabola chi2 = aN/sigma^2 (s-s0)^2
        out["sigma_scale_from_curvature"] = float(1.0 / np.sqrt(max(a * n_residuals / sigma_px ** 2, 1e-30)))
        out["sigma_scale_statistical_percent"] = float(100 * out["sigma_scale_from_curvature"])
    except Exception:
        pass
    return out
