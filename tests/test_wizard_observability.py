"""Observability tests: marginal sigmas, identifiability, gate, staged release."""
from __future__ import annotations

import time

import numpy as np
import pytest

from calib.calibrate import CAMERAS, CalibrationResult, CameraIntrinsics
from calib.geometry import rodrigues
from tools.wizard import observability
from tools.wizard.observability import (RIG_NAMES, identifiability, marginal_sigmas,
                                        next_parameter_release, observability_gate,
                                        parameter_budgets, release_plan)

NRIG = 39


# ---------------------------------------------------------------------------
# synthetic calibration results (constructed directly: fast and deterministic)
# ---------------------------------------------------------------------------

def make_result(S: np.ndarray | None = None, *, n_views: int = 6,
                free: np.ndarray | None = None,
                covariance: np.ndarray | None = None) -> CalibrationResult:
    """A CalibrationResult carrying a chosen marginal information matrix."""
    cams = {c: CameraIntrinsics(1159.0, 1159.0, 640.0, 512.0,
                                np.array([-0.16, 0.17, 0.0002, 0.0001, 0.02]))
            for c in CAMERAS}
    extr = {"left": (np.eye(3), np.zeros(3)),
            "right": (rodrigues(np.array([0.0, np.radians(22.0), 0.0])),
                      np.array([-158.5, 0.0, 30.0])),
            "tex": (rodrigues(np.array([0.0, np.radians(8.0), 0.0])),
                    np.array([-120.0, -20.0, 10.0]))}
    views = [(np.eye(3), np.array([0.0, 0.0, 400.0 + 2.0 * i])) for i in range(n_views)]
    free = np.ones(NRIG, bool) if free is None else np.asarray(free, bool)
    if S is None:
        S = np.diag(np.full(NRIG, 4.0))
    S = np.asarray(S, float).reshape(NRIG, NRIG)
    S[~free, :] = 0.0
    S[:, ~free] = 0.0
    if covariance is None:
        cov = np.diag(np.full(NRIG + 6 * n_views, 1e-6))
        idx = np.where(free)[0]
        sub = S[np.ix_(idx, idx)]
        if len(idx):
            cov[np.ix_(idx, idx)] = np.linalg.pinv(sub, rcond=1e-14)
        covariance = cov
    names = list(RIG_NAMES) + [f"view{v}.{f}" for v in range(n_views)
                               for f in ("rx", "ry", "rz", "tx", "ty", "tz")]
    info = {"param_names": names, "free_mask": free.tolist(),
            "views_used": list(range(n_views)), "covariance": {"n_free": int(free.sum())}}
    return CalibrationResult(cams, extr, views, covariance,
                             residual_rms=0.4, residual_max=1.2,
                             n_observations=100, n_parameters=len(names),
                             info=info, information=None, marginal_information=S)


def diag_result(sigmas: dict[str, float], **kw) -> CalibrationResult:
    """Result whose marginal information is diagonal with the given 1-σ."""
    S = np.zeros((NRIG, NRIG))
    free = np.zeros(NRIG, bool)
    for name, sigma in sigmas.items():
        i = RIG_NAMES.index(name)
        S[i, i] = 1.0 / float(sigma) ** 2
        free[i] = True
    return make_result(S, free=free, **kw)


# ---------------------------------------------------------------------------
# marginal sigmas
# ---------------------------------------------------------------------------

def test_marginal_sigmas_recover_the_information_diagonal():
    want = {"left.fx": 0.25, "left.cx": 0.10, "right.tx": 0.30, "right.rz": 0.02}
    res = diag_result(want)
    out = marginal_sigmas(res)
    # a flat dict[str, float] (architecture §7) with the mask as an attribute
    assert isinstance(out, dict)
    assert set(out) == set(want)
    for name, sigma in want.items():
        assert out[name] == pytest.approx(sigma, rel=1e-6)
    assert out.n_free == 4
    assert set(out.free_names) == set(want)
    assert out.worst["name"] == "right.tx"
    assert out.cross_check_param_std_max_rel_diff < 1e-6
    assert out.runtime_ms >= 0.0
    assert out.as_report()["sigmas"] == dict(out)
    assert out.mask_list() == [bool(x) for x in out.free_mask]
    assert len(out.free_mask) == NRIG


def test_marginal_sigmas_free_mask_override_and_zero_information():
    res = diag_result({"left.fx": 1.0, "left.fy": 2.0, "left.cx": 3.0})
    mask = np.zeros(NRIG, bool)
    mask[RIG_NAMES.index("left.fx")] = True
    out = marginal_sigmas(res, free_mask=mask)
    assert out.free_names == ["left.fx"]
    assert out["left.fx"] == pytest.approx(1.0, rel=1e-9)

    # a "free" parameter with zero information must be reported as infinite
    S = np.zeros((NRIG, NRIG))
    S[0, 0] = 1.0
    free = np.zeros(NRIG, bool)
    free[[0, 1]] = True
    res2 = make_result(S, free=free)
    out2 = marginal_sigmas(res2)
    assert out2["left.fx"] == pytest.approx(1.0, rel=1e-9)
    assert out2["left.fy"] == float("inf")


def test_factory_only_model_has_no_free_rig_parameters():
    free = np.zeros(NRIG, bool)
    res = make_result(np.zeros((NRIG, NRIG)), free=free)
    out = marginal_sigmas(res)
    assert out.n_free == 0 and dict(out) == {} and out.worst is None
    idf = identifiability(res)
    assert idf["n_free"] == 0 and idf["rank"] == 0


# ---------------------------------------------------------------------------
# identifiability
# ---------------------------------------------------------------------------

def test_identifiability_condition_rank_and_correlations():
    S = np.zeros((NRIG, NRIG))
    i, j = RIG_NAMES.index("left.fx"), RIG_NAMES.index("left.fy")
    S[np.ix_([i, j], [i, j])] = [[1.0, 0.9], [0.9, 1.0]]
    free = np.zeros(NRIG, bool)
    free[[i, j]] = True
    res = make_result(S, free=free)
    idf = identifiability(res)
    assert idf["n_free"] == 2
    assert idf["rank"] == 2
    # eigenvalues of the column-normalised matrix are 1.9 and 0.1
    assert idf["condition_scaled"] == pytest.approx(19.0, rel=1e-9)
    assert idf["eigenvalues"][0] == pytest.approx(1.9, rel=1e-9)
    assert idf["eigenvalues"][-1] == pytest.approx(0.1, rel=1e-9)
    # a strongly collinear information block gives a strongly *anti*correlated
    # parameter covariance: fx and fy trade off against each other
    assert idf["top_correlations"][0]["corr"] == pytest.approx(-0.9, abs=1e-9)
    assert {idf["top_correlations"][0]["a"], idf["top_correlations"][0]["b"]} == \
        {"left.fx", "left.fy"}
    # the weakest direction is the anti-correlated focal-length combination
    dom = {d["param"]: d["component"] for d in idf["weakest"]["dominant"]}
    assert set(dom) == {"left.fx", "left.fy"}
    assert dom["left.fx"] * dom["left.fy"] < 0
    assert idf["weakest"]["eigenvalue"] == pytest.approx(0.1, rel=1e-9)
    assert idf["weakest"]["rank"] == 1
    assert idf["weakest_correlated_direction"]["sigma_amplification"] == pytest.approx(
        np.sqrt(1.9), rel=1e-9)


def test_identifiability_rank_depends_on_the_tolerance():
    S = np.diag([1.0, 1.0, 1e-6] + [0.0] * (NRIG - 3))
    free = np.zeros(NRIG, bool)
    free[:3] = True
    res = make_result(S, free=free)
    loose = identifiability(res, rank_rtol=1e-3)
    tight = identifiability(res, rank_rtol=1e-12)
    # the rank is the Jacobian-equivalent one: eigenvalues are compared against
    # lambda_max * rcond**2, so a 1e-6 information gap is rank-deficient at
    # rcond=1e-3 (6e-7 < 1e-6) and full rank at rcond=1e-12
    assert tight["rank"] == 3 and loose["rank"] == 2
    # the *scaled* spectrum is unit-diagonal by construction, so a purely
    # diagonal information block is perfectly conditioned; the seven decades of
    # the raw spectrum are reported separately and are what the study quotes
    assert tight["condition_scaled"] == pytest.approx(1.0, rel=1e-9)
    assert tight["decades_raw"] == pytest.approx(6.0, abs=1e-6)
    assert tight["eigenvalues_raw"][0] == pytest.approx(1.0, rel=1e-9)


def test_identifiability_drops_zero_information_parameter():
    S = np.zeros((NRIG, NRIG))
    S[0, 0] = 1.0
    free = np.zeros(NRIG, bool)
    free[[0, 1]] = True
    idf = identifiability(make_result(S, free=free))
    assert idf["n_free"] == 1
    assert idf["dropped_zero_information"] == ["left.fy"]


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def _model_sensitivity(worth: bool = True) -> dict:
    return {"ok": True, "n_views": 12, "n_train_views": 10, "n_heldout_views": 2,
            "heldout_rms_free_kc_px": 0.30, "heldout_rms_fixed_kc_px":
            0.30 / (1.0 + (0.12 if worth else 0.01)),
            "delta_px": 0.03, "relative_improvement": 0.12 if worth else 0.01,
            "worth_freeing": bool(worth), "note": "held-out only"}


def _good_sigmas() -> dict[str, float]:
    out = {}
    for cam in CAMERAS:
        out[f"{cam}.fx"] = 0.20
        out[f"{cam}.fy"] = 0.20
        out[f"{cam}.cx"] = 0.10
        out[f"{cam}.cy"] = 0.10
        out[f"{cam}.k1"] = 1e-4        # -> 0.04 px at the corner
        out[f"{cam}.k2"] = 1e-4
        out[f"{cam}.p1"] = 1e-5
        out[f"{cam}.p2"] = 1e-5
        out[f"{cam}.k3"] = 1e-4
    for cam in ("right", "tex"):
        for f, s in (("rx", 0.002), ("ry", 0.002), ("rz", 0.002),
                     ("tx", 0.02), ("ty", 0.02), ("tz", 0.02)):
            out[f"{cam}.{f}"] = s
    return out


def test_gate_passes_on_a_constrained_model():
    res = diag_result(_good_sigmas())
    gate = observability_gate(res, None, target="standard",
                              model_sensitivity=_model_sensitivity(True))
    assert gate["passed"] is True, gate["reasons"]
    assert gate["counts"]["unobservable"] == 0
    assert gate["gates"]["stereo_rotation"]["status"] == "constrained"
    assert gate["gates"]["baseline"]["status"] in ("constrained", "weak")
    assert gate["gates"]["colour"]["status"] == "not_applicable"
    assert gate["gates"]["model_sensitivity"]["pass"] is True
    assert gate["gates"]["freed_parameters"]["pass"] is True
    budget = parameter_budgets("standard")
    assert budget["left.fx"]["budget"] == 0.75
    assert budget["left.fx"]["plan_budget"] == pytest.approx(0.05 / 100 * 1159.0)
    assert budget["right.rx"]["budget"] == 0.20


def test_gate_verdicts_and_reasons_name_the_weak_parameters():
    sig = _good_sigmas()
    sig["left.fx"] = 0.75 * 3.0          # weak (ratio 3)
    sig["left.cx"] = 3.0                 # unobservable (ratio 10)
    sig["right.k1"] = 0.02               # ~8 px at the corner -> unobservable
    res = diag_result(sig)
    gate = observability_gate(res, None, model_sensitivity=_model_sensitivity(True),
                              target="standard")
    p = gate["parameters"]
    assert p["left.fx"]["verdict"] == "weak"
    assert p["left.cx"]["verdict"] == "unobservable"
    assert p["right.k1"]["verdict"] == "unobservable"
    assert p["left.fy"]["verdict"] == "constrained"
    assert p["right.k1"]["unit"] == "px_at_corner"
    assert p["right.k1"]["effect_px_at_corner"] > 5.0
    assert gate["passed"] is False
    assert gate["gates"]["freed_parameters"]["pass"] is False
    assert set(gate["gates"]["freed_parameters"]["unobservable"]) == \
        {"left.cx", "right.k1"}
    assert any("unobservable" in r for r in gate["reasons"])


def test_gate_fails_on_a_weak_stereo_rotation():
    sig = _good_sigmas()
    sig["right.ry"] = 0.002 * 2.5       # 0.005 rad = 0.286 deg vs the 0.20 deg limit
    res = diag_result(sig)
    gate = observability_gate(res, None, model_sensitivity=_model_sensitivity(True))
    assert gate["gates"]["stereo_rotation"]["pass"] is False
    assert gate["gates"]["stereo_rotation"]["status"] == "weak"


def test_colour_gate_only_when_colour_views_exist():
    sig = _good_sigmas()
    sig["tex.tx"] = 7.0                 # 7 mm colour extrinsic uncertainty
    res = diag_result(sig)
    no_colour = {"D6": {"satisfied": True, "measured": {"n_views_with_colour": 0}}}
    yes_colour = {"D6": {"satisfied": False, "measured": {"n_views_with_colour": 12}}}
    g0 = observability_gate(res, no_colour, model_sensitivity=_model_sensitivity())
    g1 = observability_gate(res, yes_colour, model_sensitivity=_model_sensitivity())
    assert g0["gates"]["colour"]["status"] == "not_applicable"
    assert g0["gates"]["colour"]["pass"] is True
    assert g1["gates"]["colour"]["status"] == "unobservable"
    assert g1["gates"]["colour"]["pass"] is False
    assert any("colour" in r for r in g1["reasons"])


def test_gate_reports_unknown_model_sensitivity_and_strictness():
    res = diag_result(_good_sigmas())
    strict = observability_gate(res, None, model_sensitivity=None, strict=True)
    loose = observability_gate(res, None, model_sensitivity=None, strict=False)
    assert strict["gates"]["model_sensitivity"]["status"] == "unknown"
    assert strict["passed"] is False
    assert "model sensitivity" in " ".join(strict["reasons"])
    assert loose["passed"] is True
    # a supplied but not-worth-it analysis fails the gate
    bad = observability_gate(res, None, model_sensitivity=_model_sensitivity(False))
    assert bad["gates"]["model_sensitivity"]["pass"] is False
    assert any("model sensitivity" in r for r in bad["reasons"])


def test_gate_reports_its_own_runtime():
    res = diag_result(_good_sigmas())
    gate = observability_gate(res, None, model_sensitivity=_model_sensitivity())
    assert gate["runtime_ms"] >= 0.0
    assert gate["identifiability"]["runtime_ms"] >= 0.0
    assert isinstance(gate["sigmas"], dict) and gate["sigmas"]
    assert gate["identifiability"]["condition"] == gate["identifiability"]["condition_scaled"]


# ---------------------------------------------------------------------------
# staged release (plan §5)
# ---------------------------------------------------------------------------

def _cov(d1=True, d4=True, d5=True, d6=True, corners=True) -> dict:
    return {"D1": {"satisfied": d1}, "D2": {"satisfied": True},
            "D3": {"satisfied": True}, "D4": {"satisfied": d4},
            "D5": {"satisfied": d5}, "D6": {"satisfied": d6},
            "checks": {"image_corners": {"satisfied": corners}}}


def _free(*names: str) -> np.ndarray:
    m = np.zeros(NRIG, bool)
    for n in names:
        m[RIG_NAMES.index(n)] = True
    return m


def _gate(intr: bool = True, stereo: str = "constrained",
          colour: str = "constrained", ms: bool = True) -> dict:
    params = {}
    for cam in ("left", "right"):
        for f, v in (("fx", "constrained"), ("fy", "constrained"),
                     ("cx", "constrained"), ("cy", "constrained"),
                     ("k1", "constrained" if intr else "weak")):
            params[f"{cam}.{f}"] = {"verdict": v}
    return {"parameters": params,
            "gates": {"stereo_rotation": {"status": stereo},
                      "colour": {"status": colour},
                      "model_sensitivity": {"value": _model_sensitivity(ms)}}}


def test_staged_release_follows_the_plan_order():
    base = make_result(free=np.zeros(NRIG, bool))
    # 1. nothing may be released while the distance bands are missing
    plan = release_plan(base, _gate(), coverage=_cov(d1=False))
    assert plan["next"] is None
    assert "distance" in plan["justify"]
    # 2. distance covered -> IR intrinsics
    assert next_parameter_release(base, _gate(), coverage=_cov()) == "ir_intrinsics"
    # 3. intrinsics free and constrained -> radial
    stage1 = make_result(free=_free(*[f"{c}.{f}" for c in ("left", "right")
                                       for f in ("fx", "fy", "cx", "cy")]))
    assert next_parameter_release(stage1, _gate(),
                                  coverage=_cov()) == "ir_radial"
    # 4. radial free, corners covered and a held-out gain -> full kc5
    stage2 = make_result(free=_free(*[f"{c}.{f}" for c in ("left", "right")
                                       for f in ("fx", "fy", "cx", "cy", "k1", "k2")]))
    assert next_parameter_release(stage2, _gate(), coverage=_cov()) == "ir_kc5"
    # 5. kc5 without a demonstrated held-out gain is blocked
    assert next_parameter_release(stage2, _gate(ms=False), coverage=_cov()) is None
    stage3 = make_result(free=_free(*[f"{c}.{f}" for c in ("left", "right")
                                       for f in ("fx", "fy", "cx", "cy",
                                                 "k1", "k2", "k3", "p1", "p2")]))
    # 6. stereo extrinsic needs a constrained stereo rotation
    assert next_parameter_release(stage3, _gate(), coverage=_cov()) == "stereo_extrinsic"
    assert next_parameter_release(stage3, _gate(stereo="weak"),
                                  coverage=_cov()) is None
    stage4 = make_result(free=_free(*[f"{c}.{f}" for c in ("left", "right")
                                       for f in ("fx", "fy", "cx", "cy",
                                                 "k1", "k2", "k3", "p1", "p2")]
                                    + [f"right.{f}" for f in
                                       ("rx", "ry", "rz", "tx", "ty", "tz")]))
    assert next_parameter_release(stage4, _gate(), coverage=_cov()) == "colour"
    assert next_parameter_release(stage4, _gate(colour="unobservable"),
                                  coverage=_cov()) is None
    # 7. everything free -> nothing more to release
    everything = make_result(free=np.ones(NRIG, bool))
    explain: dict = {}
    assert next_parameter_release(everything, _gate(), coverage=_cov(),
                                  explain=explain) is None
    assert explain["free_stages"] == list(observability.RELEASE_STAGES)
    assert "all stages already free" in explain["justify"]


def test_release_plan_explains_every_stage():
    res = make_result(free=np.zeros(NRIG, bool))
    plan = release_plan(res, _gate(), coverage=_cov())
    assert [s["stage"] for s in plan["steps"]] == list(observability.RELEASE_STAGES)
    assert plan["current_stage"] == "base"
    for step in plan["steps"]:
        assert step["gate"] and step["detail"]
    assert plan["next"] == "ir_intrinsics"


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

def test_runtime_budget_on_a_40_view_result():
    rng = np.random.default_rng(3)
    n_views = 40
    dim = NRIG + 6 * n_views
    B = rng.normal(size=(dim, NRIG))
    S = B.T @ B
    S = S / np.max(np.diag(S)) * 100.0 + np.eye(NRIG) * 1e-6
    J = rng.normal(size=(3 * dim, dim))
    cov = np.linalg.pinv(J.T @ J, rcond=1e-12)
    res = make_result(S, n_views=n_views, covariance=cov)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        identifiability(res)
        observability_gate(res, None, strict=False)
        times.append(time.perf_counter() - t0)
    median_ms = 1e3 * float(np.median(times))
    # measured on this host: ~1.1 ms identifiability + ~3.0 ms gate; leave a
    # generous ceiling so the test is a regression guard, not a benchmark
    assert median_ms < 100.0, median_ms
    assert identifiability(res)["n_free"] == NRIG
