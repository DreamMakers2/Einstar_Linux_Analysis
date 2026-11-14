"""Identifiability, marginal uncertainty and the observability gate.

Everything here is computed from the *marginal* information of the rig
parameters — the board poses are eliminated by their Schur complement
(:func:`calib.adaptive.rig_information`), so a parameter's 1-σ is the
uncertainty after the nuisance poses are integrated out, not conditioned on.

Why this module exists
----------------------
Reprojection RMS cannot select a calibration: three profiles with
indistinguishable residual moved the reconstructed plate by 5.3 mm
(``docs/reconstruction/live_bringup_2026-11-11.md`` §7c).  The decisions this
module supports are therefore

* per-parameter 1-σ and a verdict ``constrained | weak | unobservable``
  against :data:`calib.quality.ACCURACY_TARGETS`;
* the scaled information spectrum (condition number, rank, eigen tail) and the
  weakest identifiable combination of parameters;
* named gates for the stereo rotation, the baseline, colour (only when colour
  views exist) and model sensitivity (held-out prediction with and without a
  free distortion model — never training RMS);
* the plan §5 staged-release policy: which parameter block may be freed next.

Numerical conventions
---------------------
The raw information matrix mixes units (px, mm, rad, k1), so its condition
number is meaningless.  As in ``tools/calib_study/identify.py`` the
*scaled* spectrum is the eigenvalue spectrum of ``S_s = D S D`` with
``D = diag(1/sqrt(S_ii))``; ``1 - λ`` is the fraction of a parameter's own
information that is missing in that direction.  All conditions here are that
scaled condition number.

Cost
----
:func:`identifiability` and :func:`observability_gate` are pure linear algebra
on a 39×39 (plus one 189×189 read for pose correlations) and cost a few
milliseconds: measured on a 40-view calibration result, ``identifiability`` is
1.09 ms (median of 5, max 1.26 ms) and ``observability_gate`` 2.97 ms (max
3.13 ms), and both return their own ``runtime_ms``.  :func:`model_sensitivity` is a
real re-fit and is therefore *not* on the live path — it is computed once at
session end and passed to the gate.
"""
from __future__ import annotations

import copy
import hashlib

from typing import Any, Mapping, Sequence

import numpy as np

from calib import plate
from calib.adaptive import NRIG, _inv_spd, free_rig_mask, rig_information
from calib.calibrate import CAMERAS, CalibrationResult, CameraIntrinsics, Observation, calibrate
from calib.quality import ACCURACY_TARGETS, accuracy_preset, baseline_sigma, rgb_alignment

_INTR_NAMES = ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3")
_EXTR_NAMES = ("rx", "ry", "rz", "tx", "ty", "tz")

#: canonical rig parameter names, identical to ``calib.calibrate._param_names``
RIG_NAMES: tuple[str, ...] = tuple(
    [f"{c}.{f}" for c in CAMERAS for f in _INTR_NAMES]
    + [f"{c}.{f}" for c in ("right", "tex") for f in _EXTR_NAMES])

assert len(RIG_NAMES) == NRIG == 39

#: plan §5 staged release order
RELEASE_STAGES: tuple[str, ...] = ("ir_intrinsics", "ir_radial", "ir_kc5",
                                   "stereo_extrinsic", "colour")

#: plan §4 example accuracy budgets (coefficients / degrees / mm), for reference
PLAN_BUDGETS = {
    "fx": 0.05 / 100.0,     # relative -> px at the end of _budget_table
    "fy": 0.05 / 100.0,
    "cx": 0.30, "cy": 0.30,          # px
    "k1": 0.01, "k2": 0.01, "k3": 0.05,
    "p1": 0.30, "p2": 0.30,          # px
    "rx": 0.02, "ry": 0.02, "rz": 0.02,   # deg
    "tx": 0.10, "ty": 0.10, "tz": 0.10,   # mm
    "rgb_mm": 0.50,
    "baseline_mm": 0.10,
}

#: verdict thresholds: within budget = constrained, within this factor = weak
WEAK_FACTOR = 4.0


# ---------------------------------------------------------------------------
# names / masks / information
# ---------------------------------------------------------------------------

def rig_param_names(result: CalibrationResult | None = None) -> list[str]:
    """Rig parameter names (first 39 entries of ``info["param_names"]``)."""
    names = None
    if result is not None:
        names = result.info.get("param_names")
    if names and len(names) >= NRIG:
        return [str(n) for n in names[:NRIG]]
    return list(RIG_NAMES)


def _mask(result: CalibrationResult, free_mask: Any = None,
          names: Sequence[str] | None = None,
          *, drop_unobservable: bool = True) -> np.ndarray:
    """Boolean (39,) mask of the free rig parameters.

    ``drop_unobservable`` also removes parameters whose information diagonal is
    exactly zero (they are not estimated by this data); with it False they stay
    in the mask and :func:`marginal_sigmas` reports ``inf`` for them, which is
    the more informative answer for a *free* parameter that carries no
    information.
    """
    base: np.ndarray
    if free_mask is not None:
        m = np.asarray(free_mask, bool).ravel()
        base = np.zeros(NRIG, bool)
        base[:min(m.size, NRIG)] = m[:NRIG]
    else:
        base = np.asarray(free_rig_mask(result), bool)[:NRIG].copy()
    if drop_unobservable:
        S = rig_information(result)
        base &= np.diag(np.asarray(S, float))[:NRIG] > 0
    return base


def _free_covariance(result: CalibrationResult, free: np.ndarray) -> np.ndarray:
    """Marginal covariance of the free rig parameters (Schur-marginalised)."""
    S = np.asarray(rig_information(result), float)[np.ix_(free, free)]
    return _inv_spd(S)


# ---------------------------------------------------------------------------
# marginal sigmas
# ---------------------------------------------------------------------------

class MarginalSigmas(dict):
    """``{parameter_name: 1-sigma}`` with the free mask and provenance attached.

    The architecture §7 signature is ``dict[str, float]`` and the GUI's
    ``WizardController`` iterates ``.items()``, so the mapping itself is flat.
    Everything a caller needs to audit the numbers travels as attributes:

    ``free_mask``   boolean ``(39,)`` mask actually used
    ``free_names``  parameter names in canonical order
    ``source``      which matrix the sigmas came from
    ``worst``       ``{"name", "sigma"}`` of the largest σ (or None)
    ``runtime_ms``  measured cost of the call
    ``cross_check_param_std_max_rel_diff``
                    max relative difference against the estimator's own
                    ``param_std()`` (same model, independent path)

    ``as_report()`` returns the same information as a plain dict.
    """

    def __init__(self, sigmas: Mapping[str, float], free_mask: np.ndarray,
                 free_names: Sequence[str], source: str, runtime_ms: float):
        super().__init__({str(k): float(v) for k, v in sigmas.items()})
        self.free_mask = np.asarray(free_mask, bool).copy()
        self.free_names = list(free_names)
        self.source = str(source)
        self.runtime_ms = float(runtime_ms)
        self.cross_check_param_std_max_rel_diff: float | None = None
        self.note: str = ""
        self.worst = (max(self.items(), key=lambda kv: kv[1]) if len(self) else None)
        if self.worst is not None:
            self.worst = {"name": self.worst[0], "sigma": float(self.worst[1])}

    # -- convenience -----------------------------------------------------
    @property
    def n_free(self) -> int:
        return len(self)

    def mask_list(self) -> list[bool]:
        return [bool(x) for x in self.free_mask]

    def as_report(self) -> dict:
        return {"sigmas": dict(self), "free_mask": self.mask_list(),
                "free_names": self.free_names, "n_free": self.n_free,
                "source": self.source, "worst": self.worst,
                "cross_check_param_std_max_rel_diff":
                    self.cross_check_param_std_max_rel_diff,
                "runtime_ms": self.runtime_ms, "note": self.note}


def marginal_sigmas(result: CalibrationResult, free_mask: Any = None) -> MarginalSigmas:
    """Per-parameter 1-σ from the Schur-marginalised rig information.

    Parameters
    ----------
    result:
        A :class:`calib.calibrate.CalibrationResult`; ``marginal_information``
        is used when present, otherwise it is rebuilt from ``information`` and
        the free mask (both already eliminate the board poses).
    free_mask:
        Optional override; length 39 or the full parameter vector.

    Returns
    -------
    :class:`MarginalSigmas`
        A ``dict[str, float]`` of name → 1-σ (the architecture §7 signature)
        carrying ``free_mask``, ``free_names``, ``source``, ``worst`` and
        ``runtime_ms`` as attributes; ``.as_report()`` returns them as a dict.
        A free parameter with numerically zero information gets ``inf`` —
        that is an unobservable direction, not a small σ.
    """
    import time
    t0 = time.perf_counter()
    names = rig_param_names(result)
    free = _mask(result, free_mask, names, drop_unobservable=False)
    idx = np.where(free)[0]
    source = ("marginal_information" if getattr(result, "marginal_information", None)
              is not None else "information_schur")
    out = MarginalSigmas({}, free, [names[i] for i in idx], source, 0.0)
    if len(idx) == 0:
        out.note = "no free rig parameters (factory-only model)"
        out.runtime_ms = 1000.0 * (time.perf_counter() - t0)
        return out
    S = np.asarray(rig_information(result), float)
    Sf = S[np.ix_(idx, idx)]
    diag = np.diag(Sf)
    good = diag > 0
    cov = np.full((len(idx), len(idx)), np.inf)
    if np.any(good):
        cov[np.ix_(good, good)] = _inv_spd(Sf[np.ix_(good, good)])
    sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
    sig = {names[i]: float(s) for i, s in zip(idx, sd)}
    out.update(sig)
    out.worst = max(out.items(), key=lambda kv: kv[1]) if len(out) else None
    if out.worst is not None:
        out.worst = {"name": out.worst[0], "sigma": float(out.worst[1])}
    # the estimator's own marginal σ, as an independent (same-model) check
    try:
        ref = result.param_std()
        diffs = [abs(sig[k] - ref[k]) / max(abs(ref[k]), 1e-30)
                 for k in sig if k in ref and np.isfinite(ref[k]) and abs(ref[k]) > 0]
        out.cross_check_param_std_max_rel_diff = float(max(diffs)) if diffs else None
    except Exception:                              # pragma: no cover - defensive
        out.cross_check_param_std_max_rel_diff = None
    out.runtime_ms = 1000.0 * (time.perf_counter() - t0)
    return out


# ---------------------------------------------------------------------------
# identifiability
# ---------------------------------------------------------------------------

def _scaled_spectrum(S: np.ndarray, names: Sequence[str], free: np.ndarray,
                     top_directions: int = 3) -> dict:
    idx = np.where(free)[0]
    if len(idx) < 2:
        return {"n_free_rig": int(len(idx)), "note": "too few free rig parameters"}
    Sf = S[np.ix_(idx, idx)]
    d = np.sqrt(np.maximum(np.diag(Sf), 0.0))
    zero = ~(d > 0)
    dropped = [names[i] for i in idx[zero]]
    if np.any(zero):
        idx = idx[~zero]
        d = d[~zero]
    if len(idx) < 2:
        return {"n_free_rig": int(len(idx)),
                "dropped_zero_information": dropped,
                "note": "fewer than two parameters with positive information"}
    Sf = S[np.ix_(idx, idx)]
    D = 1.0 / d
    Ss = Sf * np.outer(D, D)
    Ss = 0.5 * (Ss + Ss.T)
    w, V = np.linalg.eigh(Ss)
    order = np.argsort(w)[::-1]
    w = w[order]
    V = V[:, order]
    nm = [names[i] for i in idx]
    wmax = float(w[0]) if len(w) else 0.0
    wmin = float(w[-1]) if len(w) else 0.0
    directions = []
    for k in range(min(top_directions, len(w))):
        v = V[:, k]
        comp = np.argsort(np.abs(v))[::-1][:6]
        directions.append({
            "rank": k,
            "eigenvalue": float(w[k]),
            "explained_fraction": float(w[k] / w.sum()) if w.sum() else float("nan"),
            "dominant": [{"param": nm[i], "component": float(v[i]),
                          "sigma_units": float(v[i] / d[i])} for i in comp],
        })
    # the *weakest* direction is the smallest eigenvalue, not the last of the
    # top-k list
    vw = V[:, -1]
    compw = np.argsort(np.abs(vw))[::-1][:8]
    weakest = {"rank": int(len(w) - 1), "eigenvalue": float(w[-1]),
               "explained_fraction": float(w[-1] / w.sum()) if w.sum() else float("nan"),
               "dominant": [{"param": nm[i], "component": float(vw[i]),
                             "sigma_units": float(vw[i] / d[i])} for i in compw]}
    return {
        "n_free_rig": int(len(idx)),
        "eigenvalues": [float(x) for x in w],
        "eigen_tail": [float(x) for x in w[-8:]],
        "condition_scaled": float(wmax / wmin) if wmin > 0 else float("inf"),
        "condition_number_scaled": float(wmax / wmin) if wmin > 0 else float("inf"),
        "decades": float(np.log10(wmax / wmin)) if wmin > 0 else float("inf"),
        "trace_scaled": float(w.sum()),
        "weakest": weakest,
        "directions": directions,
        "dropped_zero_information": dropped,
    }


def identifiability(result: CalibrationResult, free_mask: Any = None,
                    *, rank_rtol: float = 1e-10,
                    direction_floor: float = 1e-3,
                    top_correlations: int = 5) -> dict:
    """Scaled condition number, numerical rank, eigen tail, correlations.

    Returns ``{"condition_scaled", "condition_scaled_top", "rank", "n_free",
    "rank_rtol", "rank_scaled", "eigenvalues" (scaled), "eigenvalues_raw",
    "eigen_tail", "decades", "decades_raw", "top_correlations", "weakest",
    "weakest_correlated_direction", "directions", "dropped_zero_information",
    "runtime_ms"}``.

    ``eigenvalues`` is the scaled spectrum (collinearity, unit-free);
    ``eigenvalues_raw`` is the plain information spectrum whose decades the
    study quotes.

    ``rank`` is the numerical rank of the *equivalent Jacobian*: the
    information eigenvalues are compared against ``lambda_max * rank_rtol**2``,
    which is the same criterion as ``matrix_rank(J, rcond=rank_rtol)`` and is
    therefore comparable to the study's "Jacobian rank 242 of 242".  It is a
    numerical-stability check, **not** an observability test: the vendor
    25-view corpus has full rank and still leaves 17 parameters unobservable.

    ``weak_directions`` counts the scaled eigenvalues below
    ``direction_floor`` — directions in which a parameter retains less than
    ``direction_floor`` of its own information.  That *is* an observability
    count, and it is the number to read next to the condition number.
    """
    import time
    t0 = time.perf_counter()
    names = rig_param_names(result)
    free = _mask(result, free_mask, names, drop_unobservable=False)
    S = np.asarray(rig_information(result), float)
    idx_all = np.where(free)[0]
    diag = np.diag(S)[idx_all] if len(idx_all) else np.zeros(0)
    keep = diag > 0
    idx = idx_all[keep]
    dropped = [names[i] for i in idx_all[~keep]]
    surviving = np.zeros(NRIG, bool)
    surviving[idx] = True
    out: dict[str, Any] = {"rank_rtol": float(rank_rtol),
                           "dropped_zero_information": dropped,
                           "n_free": int(len(idx)),
                           "n_params": int(len(names)),
                           "free": [names[i] for i in idx]}
    spec = _scaled_spectrum(S, names, surviving) if len(idx) >= 2 else {
        "n_free_rig": int(len(idx)),
        "note": "fewer than two parameters with positive information"}
    out.update(spec)
    out["dropped_zero_information"] = dropped
    out["n_free"] = int(len(idx))
    if len(idx):
        Sf = S[np.ix_(idx, idx)]
        wr = np.linalg.eigvalsh(0.5 * (Sf + Sf.T))[::-1]
        out["eigenvalues_raw"] = [float(x) for x in wr]
        out["rank"] = int(np.sum(wr > max(float(wr[0]) * rank_rtol ** 2, 0.0)))
        pos = wr[wr > 0]
        out["decades_raw"] = (float(np.log10(float(pos[0]) / float(pos[-1])))
                              if len(pos) else float("inf"))
    else:
        out["eigenvalues_raw"] = []
        out["rank"] = 0
        out["decades_raw"] = float("inf")
    w = np.asarray(spec.get("eigenvalues", []), float)
    out["direction_floor"] = float(direction_floor)
    out["weak_directions"] = int(np.sum(w < float(direction_floor))) if len(w) else 0
    if len(w) >= 2:
        mid = float(w[len(w) // 2])
        out["condition_scaled_top"] = float(w[0] / mid) if mid > 0 else float("inf")
        try:
            cov = _free_covariance(result, free)
            sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
            good = sd > 0
            R = np.eye(len(sd))
            R[np.ix_(good, good)] = (cov[np.ix_(good, good)]
                                     / np.outer(sd[good], sd[good]))
            pairs = []
            nm = [names[i] for i in idx]
            for i in range(len(nm)):
                if not good[i]:
                    continue
                for j in range(i + 1, len(nm)):
                    if not good[j]:
                        continue
                    pairs.append({"a": nm[i], "b": nm[j], "corr": float(R[i, j])})
            pairs.sort(key=lambda p: -abs(p["corr"]))
            out["top_correlations"] = pairs[:int(top_correlations)]
            wc, Vc = np.linalg.eigh(0.5 * (R + R.T))
            k = int(np.argmax(wc))
            v = Vc[:, k]
            comp = np.argsort(np.abs(v))[::-1][:5]
            out["weakest_correlated_direction"] = {
                "eigenvalue": float(wc[k]),
                "sigma_amplification": float(np.sqrt(max(wc[k], 0.0))),
                "dominant": [{"param": nm[i], "component": float(v[i])}
                             for i in comp if good[i]],
            }
        except np.linalg.LinAlgError:              # pragma: no cover - defensive
            out["top_correlations"] = []
    else:
        out["condition_scaled_top"] = float("inf")
        out["top_correlations"] = []
    out["free"] = [names[i] for i in idx]
    out["condition"] = out.get("condition_scaled")      # legacy alias
    out["runtime_ms"] = 1000.0 * (time.perf_counter() - t0)
    return out


# ---------------------------------------------------------------------------
# parameter budgets and verdicts
# ---------------------------------------------------------------------------

def parameter_budgets(target: str = "standard",
                      focal_px: float = 1159.0,
                      colour_target: bool = True) -> dict:
    """Per-parameter accuracy budgets derived from ``ACCURACY_TARGETS``.

    The *shape* of the table is the plan §4 budget list; its *scale* comes from
    the preset, so ``fast``/``standard``/``high`` stay consistent with
    :func:`calib.quality.stopping_report`:

    * intrinsics/px and distortion budgets scale with
      ``max_sigma_fx_px / 0.75``;
    * rotation budgets scale with ``max_sigma_rotation_deg / 0.20``;
    * translation/baseline budgets scale with
      ``max_sigma_baseline_mm / 0.12``;
    * the colour budget is ``max_sigma_rgb_mm``.

    Returns ``{name: {"budget", "unit", "kind", "plan_budget"}}`` plus
    ``__meta__``.
    """
    p = accuracy_preset(target)
    t = float(p["max_sigma_fx_px"]) / 0.75
    r = float(p["max_sigma_rotation_deg"]) / 0.20
    m = float(p["max_sigma_baseline_mm"]) / 0.12
    out: dict[str, Any] = {"__meta__": {"target": target, "preset": p,
                                        "intrinsic_scale": t,
                                        "rotation_scale": r,
                                        "translation_scale": m,
                                        "focal_px": focal_px,
                                        "note": "plan §4 shape x ACCURACY_TARGETS scale"}}
    for c in CAMERAS:
        out[f"{c}.fx"] = {"budget": p["max_sigma_fx_px"], "unit": "px", "kind": "intrinsic",
                          "plan_budget": PLAN_BUDGETS["fx"] * focal_px}
        out[f"{c}.fy"] = {"budget": p["max_sigma_fx_px"], "unit": "px", "kind": "intrinsic",
                          "plan_budget": PLAN_BUDGETS["fy"] * focal_px}
        out[f"{c}.cx"] = {"budget": 0.30 * t, "unit": "px", "kind": "intrinsic",
                          "plan_budget": PLAN_BUDGETS["cx"]}
        out[f"{c}.cy"] = {"budget": 0.30 * t, "unit": "px", "kind": "intrinsic",
                          "plan_budget": PLAN_BUDGETS["cy"]}
        for k in ("k1", "k2"):
            out[f"{c}.{k}"] = {"budget": 0.01 * t, "unit": "coeff", "kind": "distortion",
                               "plan_budget": PLAN_BUDGETS[k]}
        out[f"{c}.k3"] = {"budget": 0.05 * t, "unit": "coeff", "kind": "distortion",
                          "plan_budget": PLAN_BUDGETS["k3"]}
        for k in ("p1", "p2"):
            out[f"{c}.{k}"] = {"budget": 0.30 * t, "unit": "coeff", "kind": "distortion",
                               "plan_budget": PLAN_BUDGETS[k]}
    for c in ("right", "tex"):
        for k in ("rx", "ry", "rz"):
            out[f"{c}.{k}"] = {"budget": p["max_sigma_rotation_deg"],
                               "unit": "deg", "kind": "rotation",
                               "plan_budget": PLAN_BUDGETS[k]}
        for k in ("tx", "ty", "tz"):
            out[f"{c}.{k}"] = {"budget": p["max_sigma_baseline_mm"],
                               "unit": "mm", "kind": "translation",
                               "plan_budget": PLAN_BUDGETS[k]}
    if colour_target:
        out["__colour__"] = {"budget": p["max_sigma_rgb_mm"], "unit": "mm",
                             "kind": "colour", "plan_budget": PLAN_BUDGETS["rgb_mm"]}
    return out


def _verdict(ratio: float) -> str:
    if not np.isfinite(ratio):
        return "unobservable"
    if ratio <= 1.0:
        return "constrained"
    if ratio <= WEAK_FACTOR:
        return "weak"
    return "unobservable"


def _pose_absorption(result: CalibrationResult, names: Sequence[str]) -> dict[str, float]:
    """Max |corr| between each rig parameter and the board-pose block.

    A small σ that is produced by trading against a board pose is not a
    measurement of the parameter: the pose absorbs it (the classic
    focal-length/depth and k1/pose degeneracies).  The joint covariance is
    used when the estimator stored one; otherwise an empty dict is returned.

    The gauge camera (``left``) is excluded from the *downgrade* the caller
    applies, because the board pose **is** the left-camera pose in this gauge
    (``extrinsics['left'] = identity``): every left-camera intrinsic is
    structurally correlated with it.  Measured on the 297-view session,
    ``|corr(left.fx, view.tz)| = 0.962`` and ``|corr(left.cx, view.tx)| = 0.991``
    while the right/tex cameras sit at 0.22-0.79 for the same parameters, and
    the correlations did not change when the principal point was freed.  The
    numbers are still reported so the correlation remains auditable.
    """
    cov = result.covariance
    info = result.info.get("param_names") or []
    if cov is None or cov.ndim != 2 or cov.shape[0] <= NRIG:
        return {}
    n = cov.shape[0]
    pnames = [str(x) for x in info] if info else []
    pose_idx = [i for i in range(NRIG, n)
                if i >= len(pnames) or pnames[i].startswith("view")]
    if not pose_idx:
        pose_idx = list(range(NRIG, n))
    sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
    out = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for i, nm in enumerate(names[:NRIG]):
            if sd[i] <= 0:
                continue
            c = np.abs(cov[i, pose_idx]) / (sd[i] * sd[pose_idx] + 1e-300)
            if len(c):
                out[nm] = float(np.nanmax(c))
    return out


def observability_gate(result: CalibrationResult, coverage: Mapping[str, Any] | None,
                       *, target: str = "standard",
                       free_mask: Any = None,
                       observations: Sequence[Observation] | None = None,
                       pitch: float = plate.DEFAULT_PITCH_MM,
                       model_sensitivity: Mapping[str, Any] | None = None,
                       compute_model_sensitivity: bool = False,
                       colour_present: bool | None = None,
                       strict: bool = True,
                       image_size: tuple[int, int] = (1280, 1024)) -> dict:
    """Per-parameter verdicts plus the named gates of architecture §7.

    Parameters
    ----------
    result:
        calibration result of the candidate model.
    coverage:
        the :func:`tools.wizard.coverage.measure` output (or just its ``D6``
        entry).  Used to decide whether the colour gate applies.
    target:
        ``fast`` | ``standard`` | ``high`` (``calib.quality.ACCURACY_TARGETS``).
    observations, pitch, compute_model_sensitivity:
        Held-out model-sensitivity analysis is a real re-fit (seconds).  Leave
        ``compute_model_sensitivity`` False on the live path and pass a
        precomputed :func:`model_sensitivity` result instead.
    strict:
        When True an *unknown* required gate fails the gate (conservative).
        When False unknowns are reported but do not block.

    Returns
    -------
    dict
        ``{"target", "passed", "reasons", "counts", "parameters", "gates",
        "identifiability", "sigmas", "runtime_ms"}``.
    """
    import time
    t0 = time.perf_counter()
    preset = accuracy_preset(target)
    names = rig_param_names(result)
    free = _mask(result, free_mask, names)
    sig = marginal_sigmas(result, free_mask)
    idf = identifiability(result, free_mask)
    budgets = parameter_budgets(target, focal_px=_focal_px(result))
    absorption = _pose_absorption(result, names)
    cam = result.cameras
    params: dict[str, dict] = {}
    counts = {"constrained": 0, "weak": 0, "unobservable": 0}
    #: distortion is judged in its *pixel effect at the field corner*: a
    #: coefficient sigma has no meaning on its own (the plan's own table mixes
    #: coefficient units and pixels), so the single comparable budget is the
    #: focal-length budget converted to a corner displacement.
    distortion_budget_px = float(preset["max_sigma_fx_px"])
    for nm in sig.free_names:
        b = budgets.get(nm)
        if b is None:
            continue
        s = float(sig.get(nm, float("inf")))
        unit = b["unit"]
        sigma_units = s
        effect_px = None
        if unit == "deg":
            sigma_units = float(np.degrees(s))
            ratio = sigma_units / max(b["budget"], 1e-12)
        elif unit == "coeff":
            effect_px = _distortion_effect_px(nm, s, cam, image_size)
            if effect_px is None:
                ratio = s / max(b["budget"], 1e-12)
            else:
                sigma_units = float(effect_px)
                ratio = effect_px / max(distortion_budget_px, 1e-12)
        else:
            ratio = s / max(b["budget"], 1e-12)
        v = _verdict(ratio)
        pose_corr = absorption.get(nm)
        gauge = nm.split(".")[0] == "left"
        note = ""
        if gauge and pose_corr is not None and pose_corr >= 0.95:
            note = ("structural gauge correlation: the board pose is the left-camera "
                    "pose, so this |corr| is a property of the parameterisation and "
                    "is not used to downgrade the verdict")
        if not gauge and pose_corr is not None and pose_corr >= 0.95 and v == "constrained":
            v = "weak"
            note = (f"downgraded: |corr|={pose_corr:.3f} with a board pose — the "
                    "pose absorbs this parameter")
        elif not gauge and pose_corr is not None and pose_corr >= 0.99 and v != "unobservable":
            v = "unobservable"
            note = (f"downgraded: |corr|={pose_corr:.3f} with a board pose")
        counts[v] += 1
        rec = {"sigma": s, "sigma_units": sigma_units,
               "unit": "px_at_corner" if effect_px is not None else unit,
               "sigma_unit": unit,
               "budget": (distortion_budget_px if effect_px is not None
                          else b["budget"]),
               "plan_budget": b["plan_budget"],
               "ratio": float(ratio) if np.isfinite(ratio) else float("inf"),
               "verdict": v, "kind": b["kind"]}
        if pose_corr is not None:
            rec["max_abs_corr_with_pose"] = float(pose_corr)
        if note:
            rec["note"] = note
        if effect_px is not None:
            rec["effect_px_at_corner"] = float(effect_px)
            rec["coefficient_sigma"] = s
        params[nm] = rec

    reasons: list[str] = []
    gates: dict[str, dict] = {}

    # ---- every *free* parameter must at least be observable -------------
    unobs = [n for n, r in params.items() if r["verdict"] == "unobservable"]
    weak = [n for n, r in params.items() if r["verdict"] == "weak"]
    gates["freed_parameters"] = {
        "required": True,
        "status": "fail" if unobs else "pass",
        "pass": not unobs,
        "unobservable": unobs, "weak": weak,
        "note": "a parameter that is free but unobservable is absorbed by the "
                "board poses; do not free it (plan §5)",
    }
    if unobs:
        reasons.append(f"{len(unobs)} free parameter(s) are unobservable: "
                       + ", ".join(unobs[:8])
                       + ("…" if len(unobs) > 8 else ""))

    # ---- stereo rotation constrained -----------------------------------
    rot_sig = {k: float(np.degrees(params[k]["sigma"]))
               for k in ("right.rx", "right.ry", "right.rz") if k in params}
    if rot_sig:
        worst = max(rot_sig.items(), key=lambda kv: kv[1])
        ratio = worst[1] / max(preset["max_sigma_rotation_deg"], 1e-12)
        v = _verdict(ratio)
        gates["stereo_rotation"] = {
            "required": True, "status": v,
            "pass": v == "constrained",
            "value_deg": worst[1], "worst_parameter": worst[0],
            "limit_deg": preset["max_sigma_rotation_deg"],
            "per_axis_deg": rot_sig,
            "note": "plan §7c: only the stereo rotation is genuinely constrained "
                    "on a one-axis sweep",
        }
        if v != "constrained":
            reasons.append(f"stereo rotation {v}: worst axis {worst[0]} "
                           f"σ={worst[1]:.3f}° vs {preset['max_sigma_rotation_deg']:.3f}°")
    else:
        gates["stereo_rotation"] = {"required": True, "status": "unobservable",
                                    "pass": False,
                                    "note": "stereo extrinsic is fixed, not estimated"}
        reasons.append("stereo rotation not estimated (stereo extrinsic fixed)")

    # ---- baseline weak-or-better ---------------------------------------
    b_sig = baseline_sigma(result)
    ratio = b_sig / max(preset["max_sigma_baseline_mm"], 1e-12)
    v = _verdict(ratio) if np.isfinite(b_sig) else "unobservable"
    ok = v in ("constrained", "weak")
    gates["baseline"] = {"required": True, "status": v, "pass": bool(ok),
                         "value_mm": float(b_sig) if np.isfinite(b_sig) else None,
                         "limit_mm": preset["max_sigma_baseline_mm"],
                         "ratio": float(ratio) if np.isfinite(ratio) else None}
    if not ok:
        reasons.append(f"baseline {v}: σ={b_sig:.3f} mm vs "
                       f"{preset['max_sigma_baseline_mm']:.2f} mm")

    # ---- colour (only when colour views exist) -------------------------
    colour = _colour_views(coverage) if colour_present is None else bool(colour_present)
    if colour:
        ra = rgb_alignment(result)
        r_ratio = ra["translation_mm"] / max(preset["max_sigma_rgb_mm"], 1e-12)
        v = _verdict(r_ratio) if np.isfinite(r_ratio) else "unobservable"
        gates["colour"] = {"required": True, "status": v,
                           "pass": v in ("constrained", "weak"),
                           "translation_mm": ra["translation_mm"],
                           "rotation_deg": ra["rotation_deg"],
                           "limit_mm": preset["max_sigma_rgb_mm"],
                           "pixel_at_400mm": ra["pixel_at_distance"],
                           "note": "colour is only useful if this gate passes; "
                                   "otherwise color_status stays 'stale'"}
        if v == "unobservable":
            reasons.append(f"colour {v}: σ={ra['translation_mm']:.2f} mm vs "
                           f"{preset['max_sigma_rgb_mm']:.2f} mm")
    else:
        gates["colour"] = {"required": False, "status": "not_applicable",
                           "pass": True,
                           "note": "no colour views in the accepted set; colour "
                                   "stays 'unknown' and must not be marked ok"}

    # ---- model sensitivity (held-out, never training RMS) --------------
    ms = model_sensitivity
    if ms is None and compute_model_sensitivity and observations:
        ms = model_sensitivity_analysis(observations, result, pitch=pitch)
    if ms is None:
        gates["model_sensitivity"] = {
            "required": True, "status": "unknown", "pass": False,
            "note": "not computed: needs held-out observations "
                    "(observability_gate(..., compute_model_sensitivity=True))"}
        reasons.append("model sensitivity unknown (no held-out analysis supplied)")
    else:
        worth = bool(ms.get("worth_freeing"))
        # The gate asks whether the *candidate's* distortion policy is the one
        # the held-out evidence supports, in both directions:
        #   holds kc + freeing not worth it        -> pass
        #   holds kc + freeing worth it            -> fail (under-modelled)
        #   frees kc + freeing worth it            -> pass
        #   frees kc + freeing not worth it        -> fail (over-fitted model)
        # The shipped gate only implemented the last two cases, so a candidate
        # that correctly held the factory distortion could never pass after the
        # analysis delivered its (negative) result.
        kc_fields = ("k1", "k2", "p1", "p2", "k3")
        kc_free = [names[i] for i in np.where(free)[0]
                   if names[i].split(".")[-1] in kc_fields]
        frees_kc = bool(kc_free)
        rel = float(ms.get("relative_improvement", 0.0) or 0.0)
        ok = (worth if frees_kc else not worth) and bool(ms.get("ok", True))
        gates["model_sensitivity"] = {
            "required": True, "status": "pass" if ok else "fail",
            "pass": bool(ok), "value": ms, "distortion_free": frees_kc,
            "freed_distortion_parameters": kc_free,
            "note": ("held-out prediction change when the distortion model is "
                     "freed; training RMS is deliberately not used.  A candidate "
                     "that holds the factory distortion passes when freeing it is "
                     "not worth it, and a candidate that frees it passes only when "
                     "it is."),
        }
        if not ms.get("ok", True):
            reasons.append("model sensitivity: the held-out refit did not "
                           "converge, so the comparison is not interpretable "
                           f"({ms.get('reason') or 'no reason recorded'})")
        elif frees_kc and not worth:
            reasons.append(f"model sensitivity: the model frees distortion but "
                           f"freeing it changes held-out error by only {rel:.1%} "
                           f"(< 10 %)")
        elif not frees_kc and worth:
            reasons.append(f"model sensitivity: the model holds the factory "
                           f"distortion although freeing it would improve "
                           f"held-out error by {rel:.1%} (>= 10 %)")

    # ---- overall -------------------------------------------------------
    blocking = [k for k, g in gates.items()
                if g.get("required") and not g.get("pass")
                and not (g.get("status") == "unknown" and not strict)]
    passed = not blocking
    elapsed = 1000.0 * (time.perf_counter() - t0)
    return {
        "target": target,
        "preset": preset,
        "passed": bool(passed),
        "blocking_gates": blocking,
        "reasons": reasons,
        "counts": counts,
        "parameters": params,
        "free_names": sig.free_names,
        "gates": gates,
        "identifiability": idf,
        "sigmas": dict(sig),
        "runtime_ms": elapsed,
    }


def _focal_px(result: CalibrationResult) -> float:
    try:
        vals = [result.cameras[c].fx for c in CAMERAS]
        return float(np.median(vals))
    except Exception:                             # pragma: no cover - defensive
        return 1159.0


def _distortion_effect_px(name: str, sigma: float, cam: Mapping[str, CameraIntrinsics],
                          image_size: tuple[int, int]) -> float | None:
    """Pixel displacement at the image corner for a distortion σ.

    ``radial``: ``Δu = f σ_k r^(2k+1)``; tangential: ``Δu ≈ 2 f σ_p r²``;
    ``skew``: ``Δu = f σ_alpha``.  ``r`` is the corner radius in normalised
    coordinates of the camera that owns the parameter.
    """
    camera = name.split(".")[0]
    field = name.split(".")[-1]
    c = cam.get(camera)
    if c is None:
        return None
    W, H = image_size
    r = float(np.hypot((W / 2.0) / c.fx, (H / 2.0) / c.fy))
    if field == "k1":
        return float(c.fx * sigma * r ** 3)
    if field == "k2":
        return float(c.fx * sigma * r ** 5)
    if field == "k3":
        return float(c.fx * sigma * r ** 7)
    if field in ("p1", "p2"):
        return float(2.0 * c.fx * sigma * r * r)
    return None


def _colour_views(coverage: Mapping[str, Any] | None) -> bool:
    """Did the accepted set contain usable colour views?"""
    if not coverage:
        return False
    node = coverage.get("D6") if "D6" in coverage else coverage.get("colour")
    if isinstance(node, Mapping):
        m = node.get("measured") or {}
        try:
            return int(m.get("n_views_with_colour", 0) or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


# ---------------------------------------------------------------------------
# model sensitivity (held-out, expensive)
# ---------------------------------------------------------------------------

def _pnp_residuals(result: CalibrationResult, obs: Sequence[Observation],
                   pitch: float) -> np.ndarray:
    """Per-observation pixel error after a free per-view PnP pose."""
    from calib.geometry import project, solve_pnp
    obj = plate.board_points_mm(pitch)
    uv_idx = {(float(u), float(v)): i for i, (u, v) in enumerate(plate.lattice_uv())}
    errs = []
    for o in obs:
        cam = result.cameras.get(o.camera)
        if cam is None:
            continue
        keys = [k for k in o.uv if (float(k[0]), float(k[1])) in uv_idx]
        if len(keys) < 6:
            continue
        idx = [uv_idx[(float(k[0]), float(k[1]))] for k in keys]
        pts3 = obj[idx]
        pts2 = np.array([o.uv[k] for k in keys], float)
        p = solve_pnp(pts3, pts2, cam.K, cam.kc)
        if p is None:
            continue
        Xc = pts3 @ p.R.T + p.t
        pred = project(cam.K, cam.kc, Xc)
        errs.append(np.linalg.norm(pts2 - pred, axis=1))
    return np.concatenate(errs) if errs else np.zeros(0)


def _rms(err: np.ndarray) -> float:
    return float(np.sqrt(np.mean(err ** 2))) if len(err) else float("nan")


_MODEL_SENSITIVITY_CACHE: dict[tuple, dict] = {}
_MODEL_SENSITIVITY_ORDER: list[tuple] = []
_MODEL_SENSITIVITY_MAX = 4


def _model_sensitivity_key(observations: Sequence[Observation],
                           result: CalibrationResult, pitch: float,
                           holdout_fraction: float, seed: int,
                           max_nfev: int) -> tuple:
    """Content key for the expensive held-out analysis.

    The finish path asks for the same analysis up to three times (release
    decision, observability gate, lifecycle validation).  Measured on the
    preserved 35-view session that cost 2.6 s of a 7.8 s finish even after the
    solver was made fast.  The key covers every input that changes the result;
    it is cheap (~1 ms) compared with the two refits it guards.
    """
    h = hashlib.blake2b(digest_size=16)
    for o in sorted(observations, key=lambda z: (str(z.camera), int(z.view))):
        h.update(str(o.camera).encode())
        h.update(int(o.view).to_bytes(8, "little", signed=True))
        for k in sorted(o.uv):
            h.update(np.asarray(k, np.float64).tobytes())
            h.update(np.asarray(o.uv[k], np.float64).tobytes())
    h.update(np.asarray(result.vector(), np.float64).tobytes())
    h.update(np.asarray([pitch, holdout_fraction, seed, max_nfev], np.float64).tobytes())
    return (h.hexdigest(),)


def _sensitivity_refit(observations: Sequence[Observation], pitch: float,
                       init: Mapping[str, CameraIntrinsics], free_kc: bool,
                       max_nfev: int = 400) -> tuple[CalibrationResult, dict]:
    """Fit one side of the held-out model comparison with the analytic LM.

    The shipped code called :func:`calib.calibrate.calibrate` (SciPy TRF with a
    finite-difference Jacobian).  At 238 training views that problem has ~1460
    free parameters, so a single numeric Jacobian costs >1460 residual
    evaluations; with ``max_nfev=400`` SciPy stopped *before completing one
    Jacobian* and both "fitted" models were still at the initial guess.  The
    analysis then reported a 0.018 % held-out difference -- two unfitted models
    compared to each other.  The analytic LM (``calib.solvers``) fits the same
    model to convergence in tens of seconds, so the comparison is between real
    fits and is also cheaper, because it never evaluates n+1 residuals per
    Jacobian column.  The SciPy engine remains the fallback when the solver
    layer is unavailable.
    """
    try:
        from calib import solvers
        problem = solvers.make_problem(list(observations), pitch_mm=float(pitch),
                                      init_cameras=dict(init))
        out = solvers.solve_joint(problem, backend="lm", robust="huber",
                                  f_scale=0.8, free_kc=free_kc, free_intrinsics=True,
                                  covariance=False,
                                  max_iter=max(12, min(40, int(max_nfev) // 20)))
        result = out.get("result")
        if result is not None:
            return result, dict(out.get("diagnostics") or {})
    except Exception:                                # pragma: no cover - fallback
        pass
    res = calibrate(list(observations), pitch, dict(init), free_kc=free_kc,
                    free_intrinsics=True, max_nfev=max_nfev)
    return res, {"solver_status": "scipy-engine", "success": bool(
        res.info.get("success", True)), "nit": res.info.get("nfev")}


def model_sensitivity_analysis(observations: Sequence[Observation],
                               result: CalibrationResult,
                               *, pitch: float = plate.DEFAULT_PITCH_MM,
                               holdout_fraction: float = 0.2,
                               seed: int = 0,
                               max_nfev: int = 400) -> dict:
    """Held-out prediction with a free vs a fixed distortion model.

    Fits the *same* rig twice on a training split — once with ``free_kc=True``
    and once with the candidate's distortion fixed — and compares both on views
    that were never in either fit, each evaluated with a free per-view pose
    (PnP).  The reported number is the held-out RMS change, never the training
    residual.  Cost: two calibrations (seconds), so call it once at the end of
    a session.
    """
    key = _model_sensitivity_key(observations, result, pitch,
                                 holdout_fraction, seed, max_nfev)
    cached = _MODEL_SENSITIVITY_CACHE.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    views = sorted({int(o.view) for o in observations})
    if len(views) < 6:
        return {"ok": False, "reason": f"only {len(views)} views"}
    rng = np.random.default_rng(seed)
    perm = rng.permutation(views)
    n_test = max(1, int(round(holdout_fraction * len(views))))
    test = set(int(v) for v in perm[:n_test])
    train = [o for o in observations if int(o.view) not in test]
    held = [o for o in observations if int(o.view) in test]
    init = {c: CameraIntrinsics(result.cameras[c].fx, result.cameras[c].fy,
                                result.cameras[c].cx, result.cameras[c].cy,
                                result.cameras[c].kc.copy()) for c in CAMERAS}
    try:
        r_free, d_free = _sensitivity_refit(train, pitch, init, True, max_nfev)
        r_fixed, d_fixed = _sensitivity_refit(train, pitch, init, False, max_nfev)
    except Exception as exc:                       # pragma: no cover - defensive
        return {"ok": False, "reason": f"refit failed: {exc}"}
    converged = (bool(d_free.get("success")) and bool(d_fixed.get("success")))
    e_free = _rms(_pnp_residuals(r_free, held, pitch))
    e_fixed = _rms(_pnp_residuals(r_fixed, held, pitch))
    delta = e_fixed - e_free
    rel = delta / e_fixed if e_fixed and np.isfinite(e_fixed) else float("nan")
    out = {
        "ok": True, "n_views": len(views), "n_train_views": len(views) - n_test,
        "n_heldout_views": n_test,
        "heldout_rms_free_kc_px": e_free,
        "heldout_rms_fixed_kc_px": e_fixed,
        "delta_px": float(delta),
        "relative_improvement": float(rel),
        "worth_freeing": bool(rel >= 0.10),
        "train_rms_free_kc_px": float(r_free.residual_rms),
        "train_rms_fixed_kc_px": float(r_fixed.residual_rms),
        "backend": d_free.get("solver_status"), "refit_converged": converged,
        "refit_iterations": [d_free.get("nit"), d_fixed.get("nit")],
        "note": "selection uses the held-out change; the training RMS is reported "
                "only to show it moves the other way",
    }
    if not converged:
        # A comparison between two *unfitted* models is not evidence either way.
        out["ok"] = False
        out["reason"] = (f"refit did not converge ({d_free.get('solver_status')}, "
                         f"{d_fixed.get('solver_status')}); the held-out change is "
                         "not interpretable")
    _MODEL_SENSITIVITY_CACHE[key] = copy.deepcopy(out)
    _MODEL_SENSITIVITY_ORDER.append(key)
    while len(_MODEL_SENSITIVITY_ORDER) > _MODEL_SENSITIVITY_MAX:
        old = _MODEL_SENSITIVITY_ORDER.pop(0)
        _MODEL_SENSITIVITY_CACHE.pop(old, None)
    return out


# ---------------------------------------------------------------------------
# staged release (plan §5)
# ---------------------------------------------------------------------------

def _stage_state(result: CalibrationResult, names: Sequence[str],
                 free: np.ndarray) -> dict[str, bool]:
    """Which release stages are already free in the model?"""
    def is_free(nm: str) -> bool:
        i = names.index(nm) if nm in names else -1
        return bool(i >= 0 and free[i])

    ir_intr = all(is_free(f"{c}.{f}") for c in ("left", "right")
                  for f in ("fx", "fy", "cx", "cy"))
    ir_rad = any(is_free(f"{c}.{f}") for c in ("left", "right") for f in ("k1", "k2"))
    ir_kc5 = any(is_free(f"{c}.{f}") for c in ("left", "right")
                 for f in ("k3", "p1", "p2"))
    stereo = all(is_free(f"right.{f}") for f in _EXTR_NAMES)
    colour = any(is_free(f"tex.{f}") for f in _INTR_NAMES + _EXTR_NAMES)
    return {"ir_intrinsics": ir_intr, "ir_radial": ir_rad, "ir_kc5": ir_kc5,
            "stereo_extrinsic": stereo, "colour": colour}


def _dims_satisfied(coverage: Mapping[str, Any] | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    if not coverage:
        return out
    for dim in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"):
        node = coverage.get(dim)
        if isinstance(node, Mapping):
            out[dim] = bool(node.get("satisfied"))
    for name, node in (coverage.get("checks") or {}).items():
        if isinstance(node, Mapping):
            out.setdefault(f"check:{name}", bool(node.get("satisfied")))
    return out


def _dim_ok(dims: Mapping[str, bool], dim: str) -> bool | None:
    """True/False/None(unknown) for a coverage dimension."""
    if dim in dims:
        return bool(dims[dim])
    return None


def release_plan(result: CalibrationResult, gate: Mapping[str, Any] | None,
                 *, coverage: Mapping[str, Any] | None = None,
                 free_mask: Any = None) -> dict:
    """Full staged-release reasoning (see :func:`next_parameter_release`)."""
    names = rig_param_names(result)
    free = _mask(result, free_mask, names)
    state = _stage_state(result, names, free)
    cov = coverage
    if cov is None and gate is not None:
        cov = gate.get("coverage")
    dims = _dims_satisfied(cov)
    verdicts = {k: v.get("verdict") for k, v in ((gate or {}).get("parameters") or {}).items()}
    gates = (gate or {}).get("gates") or {}
    ms = (gates.get("model_sensitivity") or {}).get("value") or {}

    def verdict(nm: str) -> str | None:
        return verdicts.get(nm)

    steps = []
    # +1 IR intrinsics: needs both distance axes of the design (D1)
    d1 = _dim_ok(dims, "D1")
    ok = bool(d1)
    steps.append({"stage": "ir_intrinsics", "already_free": state["ir_intrinsics"],
                  "allowed": ok, "gate": "D1 distance bands",
                  "detail": ("focal length is degenerate with distance "
                             "(measured corr(fx, depth) = 0.9991) until the "
                             "distance bands are covered"
                             if not ok else
                             "distance bands covered: focal length is separable "
                             "from depth; require >= 10 % held-out improvement "
                             "before keeping it free")})
    # +2 radial: D1 + D4 and intrinsics constrained
    d4 = _dim_ok(dims, "D4")
    intr_ok = all(verdict(f"{c}.{f}") == "constrained"
                  for c in ("left", "right") for f in ("fx", "fy", "cx", "cy")
                  if f"{c}.{f}" in verdicts) and any(
        f"{c}.fx" in verdicts for c in ("left", "right"))
    ok = bool(d1) and bool(d4) and bool(intr_ok)
    steps.append({"stage": "ir_radial", "already_free": state["ir_radial"],
                  "allowed": ok,
                  "gate": "D1 + D4 + intrinsics constrained",
                  "detail": ("radial terms need corner coverage and a constrained "
                             "perspective block; distortion is unobservable on a "
                             "central-band sweep (measured: σ_k1 unchanged by 25 "
                             "diverse views)" if not ok else
                             "corner coverage present and the perspective block is "
                             "constrained: k1/k2 may be freed, held-out improvement "
                             "required")})
    # +3 full kc5: all four corners + a demonstrated held-out gain
    corners = dims.get("check:image_corners")
    if corners is None:
        corners = gates.get("image_corners")
    gain_ok = bool(ms.get("worth_freeing"))
    ok = bool(d1) and corners is True and gain_ok
    steps.append({"stage": "ir_kc5", "already_free": state["ir_kc5"],
                  "allowed": ok,
                  "gate": "D4 corners + model sensitivity ≥ 10 % held-out gain",
                  "detail": ("full kc5 + skew needs all four image corners and a "
                             "held-out gain of at least 10 %; otherwise the extra "
                             "terms absorb pose error"
                             if not ok else
                             "corner coverage and a held-out gain justify the "
                             "5-coefficient model")})
    # +4 stereo extrinsic: rotation constrained and stereo overlap covered
    st = (gates.get("stereo_rotation") or {}).get("status")
    d5 = _dim_ok(dims, "D5")
    ok = st == "constrained" and bool(d5)
    steps.append({"stage": "stereo_extrinsic", "already_free": state["stereo_extrinsic"],
                  "allowed": ok,
                  "gate": "stereo rotation constrained + D5 stereo overlap",
                  "detail": ("the stereo extrinsic is measurable but the sweep only "
                             "exposes part of it; the rotation must be constrained "
                             "before the 6-DoF block is released" if not ok else
                             "stereo rotation is constrained: the extrinsic may be "
                             "freed, cross-camera held-out must improve")})
    # +5 colour: D6 and the colour gate
    col = (gates.get("colour") or {}).get("status")
    d6 = _dim_ok(dims, "D6")
    ok = bool(d6) and col in ("constrained", "weak")
    steps.append({"stage": "colour", "already_free": state["colour"],
                  "allowed": ok,
                  "gate": "D6 colour participation + colour gate",
                  "detail": ("colour stays 'unknown' without colour views and an "
                             "independent colour check (the shipped colour extrinsic "
                             "is ~18 mm / ~49 px off)" if not ok else
                             "colour views and a passing colour gate: colour may be "
                             "released")})

    next_stage = None
    blocked_by = None
    for s in steps:
        if s["already_free"]:
            continue
        if s["allowed"]:
            next_stage = s["stage"]
        else:
            blocked_by = s
        break
    return {"next": next_stage,
            "current_stage": _current_stage(state),
            "free_stages": [k for k, v in state.items() if v],
            "blocked": blocked_by,
            "steps": steps,
            "justify": (blocked_by["detail"] if blocked_by else
                        (steps[-1]["detail"] if next_stage == steps[-1]["stage"]
                         else "all stages already free")),
            "note": "plan §5 policy: intrinsics -> radial -> full kc5 -> rig -> colour; "
                    "a blocked stage blocks later stages (the policy is sequential)"}


def _current_stage(state: Mapping[str, bool]) -> str:
    cur = "base"
    for s in RELEASE_STAGES:
        if state.get(s):
            cur = s
    return cur


def next_parameter_release(result: CalibrationResult, gate: Mapping[str, Any] | None,
                           *, coverage: Mapping[str, Any] | None = None,
                           free_mask: Any = None,
                           explain: dict | None = None) -> str | None:
    """Next parameter block the plan §5 policy allows to be freed.

    Returns ``"ir_intrinsics" | "ir_radial" | "ir_kc5" | "stereo_extrinsic" |
    "colour"`` or ``None`` when nothing more may be released.  The reasoning
    (which gate allows or blocks it) is returned by :func:`release_plan`; pass
    a dict as ``explain`` to have it filled in place.
    """
    plan = release_plan(result, gate, coverage=coverage, free_mask=free_mask)
    if explain is not None:
        explain.clear()
        explain.update(plan)
    return plan["next"]
