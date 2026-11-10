"""Tests for :mod:`calib.solvers`.

The suite is deliberately small and fast: one 6-view synthetic scene is shared
by most tests (m = 1440 residuals, 75 free parameters), so a full run costs a
few seconds.  The native tests skip when ``calib/ceres/calib_ref`` is absent.
"""

from __future__ import annotations

import numpy as np
import pytest

from calib import solvers, synth
from calib.calibrate import CAMERAS, CameraIntrinsics

N_VIEWS = 6
SEED = 0


@pytest.fixture(scope="module")
def scene():
    return synth.make_scene(n_views=N_VIEWS, pitch_mm=28.0, noise_px=0.2,
                            outlier_frac=0.0, seed=SEED)


@pytest.fixture(scope="module")
def problem(scene):
    init = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
            for c in CAMERAS}
    return solvers.make_problem(scene.observations, scene.pitch_mm, init)


@pytest.fixture(scope="module")
def truth(scene, problem):
    x = np.zeros(problem.n)
    for i, c in enumerate(CAMERAS):
        x[9 * i:9 * i + 9] = scene.cameras[c].as_vec()
    for i, c in enumerate(("right", "tex")):
        R, t = scene.extrinsics[c]
        x[27 + 6 * i:30 + 6 * i] = solvers.rvec_from_R(R)
        x[30 + 6 * i:33 + 6 * i] = t
    for v, (R, t) in enumerate(scene.views):
        x[39 + 6 * v:42 + 6 * v] = solvers.rvec_from_R(R)
        x[42 + 6 * v:45 + 6 * v] = t
    return x


def test_problem_layout(problem):
    assert problem.n_views == N_VIEWS
    assert problem.n == 39 + 6 * N_VIEWS
    assert problem.m == 2 * 40 * 3 * N_VIEWS
    assert len(problem.names) == problem.n
    assert problem.names[0] == "left.fx"
    assert problem.names[39] == "view0.rx"
    mask = problem.free_mask(True)
    assert mask.sum() == problem.n
    assert problem.free_mask(False)[[4, 5, 6, 7, 8]].sum() == 0
    assert problem.free_mask("k1k2")[[4, 5]].all()
    assert not problem.free_mask("k1k2")[[6, 7, 8]].any()
    assert problem.free_mask(True, free_intrinsics=False)[:4].sum() == 0
    assert problem.free_mask(True, fix_left_intrinsics=True)[:9].sum() == 0
    assert problem.free_mask(True, fix_left_intrinsics=True)[9:27].sum() == 18
    with pytest.raises(ValueError):
        solvers.make_problem([])
    with pytest.raises(ValueError):
        solvers.solve_joint(problem, backend="nope")


def test_jacobian_matches_finite_differences(problem):
    x0 = problem.start(True)
    free = np.where(problem.free_mask(True))[0]
    r, J = problem.evaluate(x0, free)
    assert J.shape == (problem.m, len(free))
    assert np.all(np.isfinite(J))

    def step(k):
        if k < 27:
            return 1e-4 if (k % 9) < 4 else 1e-7
        return 1e-7 if ((k - 27) % 6) < 3 else 1e-4

    worst = 0.0
    for jj, k in enumerate(free):
        h = step(int(k))
        xp = x0.copy(); xp[k] += h
        xm = x0.copy(); xm[k] -= h
        num = (problem.residuals(xp) - problem.residuals(xm)) / (2.0 * h)
        den = max(np.linalg.norm(num), 1e-9)
        worst = max(worst, np.linalg.norm(num - J[:, jj]) / den)
    assert worst < 1e-5, f"analytic Jacobian disagrees with FD: {worst:.2e}"


def test_solve_scipy_fields(problem, truth):
    out = solvers.solve_joint(problem, backend="scipy", jac="analytic")
    for key in ("x", "cameras", "extrinsics", "views", "residuals",
                "residual_rms", "covariance", "param_std", "derived",
                "diagnostics", "timings", "result"):
        assert key in out, key
    assert out["backend"] == "scipy"
    assert not out["fallback"]
    assert out["x"].shape == (problem.n,)
    assert out["covariance"].shape == (problem.n, problem.n)
    assert np.allclose(np.diag(out["covariance"]) >= 0, True)
    assert out["residual_rms"] < 1.5
    assert abs(out["x"][0] - truth[0]) < 30.0     # within the start's error bar
    d = out["diagnostics"]
    assert d["rank"] == d["n_free"]
    assert d["condition"] > 0
    # the CalibrationResult view is consistent with the dict
    assert out["result"].covariance is not None
    assert np.isclose(out["result"].residual_rms, out["residual_rms"])
    assert out["derived"]["baseline_mm"] > 100.0
    assert out["derived"]["points"][450.0]["sigma_z_mm"] > 0


@pytest.mark.skipif(not solvers._native_available(),
                    reason="calib/ceres/calib_ref not built")
def test_scipy_vs_native_agreement(problem):
    a = solvers.solve_joint(problem, backend="scipy", jac="analytic")
    b = solvers.solve_joint(problem, backend="native")
    assert b["backend"] == "native" and not b["fallback"]
    # same optimum: intrinsics in px, translations in mm
    assert np.max(np.abs(a["x"][:39] - b["x"][:39])) < 0.10
    assert abs(solvers.baseline_mm(a["x"]) - solvers.baseline_mm(b["x"])) < 0.02
    # covariance computed at the two solutions agrees to a few percent
    for name in ("left.fx", "left.cx"):
        sa, sb = a["param_std"][name], b["param_std"][name]
        assert abs(sa - sb) / sa < 0.05, (name, sa, sb)
    ra = a["derived"]["baseline_sigma_mm"]
    rb = b["derived"]["baseline_sigma_mm"]
    assert abs(ra - rb) / ra < 0.05, (ra, rb)
    # the native binary's own variance diagonal is close to the recomputation
    ratio = b["diagnostics"].get("native_cov_diag_ratio")
    assert ratio is not None
    assert abs(ratio["median"] - 1.0) < 0.05


@pytest.mark.skipif(not solvers._native_available(),
                    reason="calib/ceres/calib_ref not built")
def test_native_fallback_missing_binary(problem, monkeypatch, tmp_path):
    monkeypatch.setattr(solvers, "NATIVE_BINARY", tmp_path / "nope")
    out = solvers.solve_joint(problem, backend="native")
    assert out["fallback"] is True
    assert out["backend"] == "scipy"
    assert "missing" in (out["fallback_reason"] or "")
    assert np.isfinite(out["residual_rms"])
    with pytest.raises(FileNotFoundError):
        solvers.solve_joint(problem, backend="native", allow_fallback=False)


def test_native_fallback_unsupported_config(problem):
    """Priors / reduced distortion are not expressible in the native backend."""
    if not solvers._native_available():
        pytest.skip("calib/ceres/calib_ref not built")
    out = solvers.solve_joint(problem, backend="native",
                              priors={"intrinsics": 20.0})
    assert out["fallback"] is True
    assert "native" in (out["fallback_reason"] or "")
    assert out["diagnostics"]["n_prior_rows"] > 0
    with pytest.raises(ValueError):
        solvers.solve_joint(problem, backend="native", free_kc="k1k2",
                            allow_fallback=False)
    # any held distortion coefficient makes the binary's dense covariance
    # singular, so the wrapper must refuse the native path
    out = solvers.solve_joint(problem, backend="native", free_kc=False)
    assert out["fallback"] is True
    assert "singular" in (out["fallback_reason"] or "")
    out = solvers.solve_joint(problem, backend="native",
                              fix_left_intrinsics=True)
    assert out["fallback"] is True


def test_covariance_matches_engine(problem):
    """The analytic sandwich reproduces the engine's FD-Jacobian covariance."""
    out = solvers.solve_joint(problem, backend="scipy", jac="analytic")
    x = out["x"]
    eng = solvers.engine._Problem(problem.observations, problem.obj,
                                 problem.n_views, True, "huber", 1.0, True)
    cov_engine, info = solvers.engine.covariance(
        eng, x, problem.residuals(x), free_intrinsics=True, free_kc=True,
        robust=True, f_scale=1.0, return_info=True)
    assert cov_engine is not None
    free = np.where(problem.free_mask(True))[0]
    mine = np.diag(out["covariance"])[free]
    theirs = np.diag(cov_engine)[free]
    ok = (mine > 0) & (theirs > 0)
    ratio = theirs[ok] / mine[ok]
    assert np.median(ratio) == pytest.approx(1.0, abs=0.02)
    assert np.max(np.abs(ratio - 1.0)) < 0.10
    assert info["rank"] == len(free)


def test_robust_losses(problem):
    rms = {}
    for loss in ("none", "huber", "cauchy", "soft_l1"):
        out = solvers.solve_joint(problem, backend="scipy", jac="analytic",
                                  robust=loss, f_scale=1.0)
        assert np.all(np.isfinite(out["x"]))
        assert out["diagnostics"]["robust"] == loss
        rms[loss] = out["residual_rms"]
    # a robust loss never lowers the plain least-squares RMS much below the
    # other losses' (it optimises a different cost); all must stay finite and
    # within a factor of two of each other on clean data
    assert max(rms.values()) < 2.0 * min(rms.values())
    with pytest.raises(ValueError):
        solvers.solve_joint(problem, robust="tukey")


def test_priors_shrink_and_bias(problem, truth):
    plain = solvers.solve_joint(problem, backend="scipy", jac="analytic")
    mean = float(truth[0] + 25.0)          # deliberately offset prior mean
    strong = solvers.solve_joint(
        problem, backend="scipy", jac="analytic",
        priors={"parameters": {"left.fx": (mean, 0.5)}})
    assert strong["diagnostics"]["n_prior_rows"] == 1
    assert abs(strong["x"][0] - mean) < abs(plain["x"][0] - mean)
    assert strong["param_std"]["left.fx"] < plain["param_std"]["left.fx"]
    weak = solvers.solve_joint(problem, backend="scipy", jac="analytic",
                               priors={"intrinsics": 1000.0})
    assert np.allclose(weak["x"], plain["x"], atol=1e-4)


def test_lm_matches_scipy(problem):
    a = solvers.solve_joint(problem, backend="scipy", jac="analytic")
    b = solvers.solve_joint(problem, backend="lm")
    assert np.max(np.abs(a["x"][:39] - b["x"][:39])) < 1e-3
    assert abs(a["residual_rms"] - b["residual_rms"]) < 1e-4


def test_opencv_smoke(problem):
    out = solvers.solve_joint(problem, backend="opencv")
    assert out["backend"] == "opencv"
    assert np.all(np.isfinite(out["x"]))
    # OpenCV's independent per-camera fits are a different estimator; the rig
    # must still be roughly right
    assert solvers.baseline_mm(out["x"]) == pytest.approx(158.5, abs=10.0)


def test_reconstruction_roundtrip(truth):
    for depth in (250.0, 450.0, 600.0):
        P = solvers.reconstruct_point(truth, depth)
        assert np.allclose(P, [0.0, 0.0, depth], atol=1e-6)


def test_backend_report():
    rep = solvers.backend_report()
    assert set(rep["backends"]) >= {"scipy", "native", "opencv", "lm"}
    assert rep["backends"]["scipy"]["priors"] is True
    assert "sha256" in rep["native"]
    assert isinstance(rep["runs"], list)
