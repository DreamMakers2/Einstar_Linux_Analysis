"""The fast analytic calibration backend must reproduce the SciPy reference.

The production path switched from SciPy TRF finite differences to the
analytic-Jacobian LM in ``calib.solvers``.  This test pins the numerical
equivalence on a synthetic scene and on the reduced-distortion configuration
used by the final profile (the configuration with the largest opportunity for
a silent divergence).
"""

from __future__ import annotations

import numpy as np
import pytest

from calib import synth
from calib.calibrate import CAMERAS, CameraIntrinsics, calibrate


@pytest.fixture(scope="module")
def scene():
    return synth.make_scene(n_views=7, pitch_mm=28.0, noise_px=0.2,
                            outlier_frac=0.0, seed=3)


@pytest.fixture(scope="module")
def init():
    return {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
            for c in CAMERAS}


@pytest.mark.parametrize("free_kc", [False, True])
def test_fast_backend_matches_scipy(scene, init, free_kc):
    ref = calibrate(scene.observations, scene.pitch_mm, init,
                    free_kc=free_kc, backend="scipy", max_nfev=400)
    fast = calibrate(scene.observations, scene.pitch_mm, init,
                     free_kc=free_kc, backend="fast", max_nfev=400)

    assert fast.info.get("solver", {}).get("backend") == "lm"
    # Same optimum (frame-invariant parameterisation).
    assert abs(fast.residual_rms - ref.residual_rms) < 2e-4
    d = np.abs(fast.vector() - ref.vector())
    assert d.max() < 2e-3
    assert abs(np.linalg.norm(fast.baseline()) -
               np.linalg.norm(ref.baseline())) < 2e-3
    # Covariance is computed from an analytic Jacobian instead of finite
    # differences; the diagonal must agree to a few percent.
    sd_ref = np.sqrt(np.maximum(np.diag(ref.covariance), 0.0))
    sd_fast = np.sqrt(np.maximum(np.diag(fast.covariance), 0.0))
    keep = sd_ref > 0
    ratio = sd_fast[keep] / sd_ref[keep]
    assert np.median(np.abs(ratio - 1.0)) < 0.05
    assert np.percentile(np.abs(ratio - 1.0), 95) < 0.15


def test_fast_backend_is_faster(scene, init):
    import time

    t0 = time.perf_counter()
    calibrate(scene.observations, scene.pitch_mm, init, free_kc=False,
              backend="scipy", max_nfev=400)
    slow = time.perf_counter() - t0
    t0 = time.perf_counter()
    calibrate(scene.observations, scene.pitch_mm, init, free_kc=False,
              backend="fast", max_nfev=400)
    fast = time.perf_counter() - t0
    # The measured ratio is ~7x on real sessions and ~4-8x here.  A 2x floor
    # keeps the test meaningful without being flaky on a loaded CI host.
    assert fast < slow / 2.0, (fast, slow)


# ---------------------------------------------------------------------------
# covariance / marginal-information consistency (2026-11-13)
# ---------------------------------------------------------------------------

def _solver_fit(observations, pitch, init, *, free_kc=False, priors=None,
                free=None, covariance=True):
    from calib import solvers
    prob = solvers.make_problem(observations, pitch_mm=pitch, init_cameras=init)
    out = solvers.solve_joint(prob, backend="lm", robust="huber", f_scale=0.8,
                              priors=priors, free_kc=free_kc, free=free,
                              covariance=covariance, max_iter=80)
    return out, prob


def test_marginal_information_is_inverse_of_covariance_block():
    """The gate's sigma and param_std must describe the same estimator.

    The shipped Schur complement inverted the Gauss-Newton information, which
    carries no residual scale, and it used a different rank cutoff than the
    covariance; the two uncertainty paths disagreed by 4x on a 60-view real
    session (and ~3.7x on the 297-view one).
    """
    from calib import synth
    from calib.adaptive import free_rig_mask
    from tools.wizard import observability

    sc = synth.make_scene(n_views=8, pitch_mm=28.0, noise_px=0.2,
                          outlier_frac=0.0, seed=11)
    init = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
            for c in CAMERAS}
    out, _ = _solver_fit(sc.observations, sc.pitch_mm, init)
    res = out["result"]
    free = free_rig_mask(res)
    idx = np.where(free)[0]
    H = np.asarray(res.marginal_information)[np.ix_(idx, idx)]
    C = np.asarray(res.covariance)[np.ix_(idx, idx)]
    assert np.allclose(H @ C, np.eye(len(idx)), atol=1e-6, rtol=1e-6)

    sig = observability.marginal_sigmas(res)
    ref = res.param_std()
    names = observability.rig_param_names(res)
    for i in idx:
        name = names[i]
        assert abs(sig[name] - ref[name]) <= 1e-4 * max(abs(ref[name]), 1e-12)


def test_condition_scaled_is_scale_invariant():
    """The unit-free condition number must not move when a column's units do."""
    from calib import solvers

    rng = np.random.default_rng(4)
    J = rng.normal(size=(800, 40))
    J[:, 3] *= 1e4                      # e.g. a translation column in 0.1 um
    r = rng.normal(size=800)
    idx = np.arange(J.shape[1])
    _, info = solvers.covariance_from_jacobian(J, r, idx, J.shape[1],
                                               robust="none")
    _, info2 = solvers.covariance_from_jacobian(J / np.linalg.norm(J, axis=0),
                                                r, idx, J.shape[1], robust="none")
    assert abs(info["condition_scaled"] - info2["condition_scaled"]) < 1e-6
    # and the raw condition number is exactly the unit-dependent one
    assert info["condition"] > 1e3 * info["condition_scaled"]


def test_full_free_mask_holds_named_parameters():
    from calib import synth, solvers

    sc = synth.make_scene(n_views=6, pitch_mm=28.0, noise_px=0.2,
                          outlier_frac=0.0, seed=2)
    init = {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
            for c in CAMERAS}
    prob = solvers.make_problem(sc.observations, pitch_mm=sc.pitch_mm,
                                init_cameras=init)
    mask = solvers.full_free_mask(prob, free_kc=False,
                                  held=("left.cx", "right.cy"))
    names = prob.names
    assert not mask[names.index("left.cx")]
    assert not mask[names.index("right.cy")]
    assert mask[names.index("left.fx")]
    assert not mask[names.index("left.k1")]      # free_kc=False holds distortion
    # every parameter the keyword knobs allow stays free
    assert mask[names.index("left.fx")]
    assert mask[names.index("tex.tz")]
    # an unknown name is an error, not a silently ignored hold
    with pytest.raises(ValueError):
        solvers.full_free_mask(prob, held=("left.fq",))


def test_held_parameter_starts_at_the_caller_value():
    """A held intrinsic must not keep the OpenCV initialisation.

    ``_initial_intrinsics`` estimates the whole intrinsic block whenever
    ``free_intrinsics`` is True, so an explicitly held principal point used to
    keep the OpenCV estimate instead of the caller's value -- the "held" model
    then published a principal point nobody had optimised or asked for
    (measured: -2.0 px on the 297-view session).
    """
    from calib import synth, solvers

    sc = synth.make_scene(n_views=6, pitch_mm=28.0, noise_px=0.2,
                          outlier_frac=0.0, seed=5)
    ref = CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
    init = {c: ref for c in CAMERAS}
    prob = solvers.make_problem(sc.observations, pitch_mm=sc.pitch_mm,
                                init_cameras=init)
    held = tuple(f"{c}.{f}" for c in CAMERAS for f in ("cx", "cy"))
    free = solvers.full_free_mask(prob, free_kc=False, held=held)
    x0 = prob.start(False, True, False, free=free)
    names = prob.names
    for c in CAMERAS:
        for field in ("cx", "cy"):
            assert x0[names.index(f"{c}.{field}")] == getattr(ref, field)
    out = solvers.solve_joint(prob, backend="lm", robust="huber", f_scale=0.8,
                              free=free, free_kc=False, covariance=False,
                              max_iter=20)
    for c in CAMERAS:
        assert out["result"].cameras[c].cx == ref.cx
        assert out["result"].cameras[c].cy == ref.cy
