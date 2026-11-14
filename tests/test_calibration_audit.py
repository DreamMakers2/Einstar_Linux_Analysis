"""Regression tests for the covariance / stopping audit.

These tests expose the two published defects directly:

* ``rig_covariance`` must be the marginal (poses integrated out), not the
  conditional covariance given the poses;
* ``rescaled`` must apply the parameter-transformation Jacobian, so intrinsic
  and rotation sigmas do not scale with the translation factor.
"""
import numpy as np
import pytest

from calib import plate
from calib.adaptive import free_rig_mask, rig_covariance
from calib.calibrate import (CalibrationResult, CameraIntrinsics, Observation,
                             calibrate, rvec_from_R)
from calib.ccf import CCFRecord, consistency, decode_record, encode_record, relative_pose
from calib.quality import baseline_sigma, relative_pose_sigma, stopping_report
from calib.synth import make_scene, perturbed_init


def _fit(n=8, seed=31, **kw):
    rng = np.random.default_rng(seed)
    sc = make_scene(n, noise_px=0.15, outlier_frac=0.0, seed=seed)
    res = calibrate(sc.observations, sc.pitch_mm, perturbed_init(sc, rng),
                    free_kc=False, free_intrinsics=True, max_nfev=500, **kw)
    return sc, res


def test_rig_covariance_is_marginal_not_conditional():
    _, res = _fit(robust_covariance=False)
    C = res.covariance
    rr = C[:39, :39]
    rv = C[:39, 39:]
    vv = C[39:, 39:]
    conditional = rr - rv @ np.linalg.pinv(vv, rcond=1e-10) @ rv.T
    marginal = rig_covariance(res)
    free = free_rig_mask(res)
    idx = np.where(free)[0]
    # the old conditional formula is far too small for the focal lengths
    focal = [0, 1, 9, 10, 18, 19]
    assert np.all(np.diag(marginal)[focal] > 2.0 * np.diag(conditional)[focal])
    # and the marginal is exactly the top-left block, not a Schur complement
    assert np.allclose(marginal, rr)
    # Information consistency: the marginal information is the inverse of the
    # rig block of the published covariance.  The mu^2 factor is deliberately
    # absent: multiplying by sigma_px**2 was the published bug (the Schur
    # complement of the Gauss-Newton information carries no residual scale, so
    # inverting it returned the covariance for unit-variance residuals and
    # overstated every sigma by 1/sigma_px).
    inv = np.linalg.solve(rr[np.ix_(idx, idx)], np.eye(len(idx)))
    M = res.marginal_information[np.ix_(idx, idx)]
    assert np.allclose(M, inv, rtol=1e-6, atol=1e-6 * np.max(np.abs(M)))
    sd = np.sqrt(np.diag(rr))[idx]
    sd_info = np.sqrt(np.diag(np.linalg.inv(M)))
    assert np.allclose(sd_info, sd, rtol=1e-6)


def test_rescaled_uses_full_jacobian():
    _, res = _fit()
    sd = np.sqrt(np.diag(res.covariance))
    idx = np.asarray(res.info["free_mask"])
    intr = np.where(idx[:39])[0]
    r2 = res.rescaled(2.0)
    sd2 = np.sqrt(np.diag(r2.covariance))
    # intrinsics and rotations are not scaled; translations are
    assert np.allclose(sd2[intr][:12], sd[intr][:12], rtol=1e-6, atol=1e-9)
    assert np.allclose(sd2[30:33], 2.0 * sd[30:33], rtol=1e-6, atol=1e-9)
    assert np.isclose(baseline_sigma(r2), 2.0 * baseline_sigma(res), rtol=1e-4)
    # anchoring to a target baseline propagates the anchor uncertainty
    r3 = res.rescaled(target_baseline=200.0, anchor_sigma_mm=0.05)
    assert np.isclose(np.linalg.norm(r3.baseline()), 200.0, rtol=1e-8)
    assert baseline_sigma(r3) >= 0.05 - 1e-6
    # rescaling rewrites the pitch provenance instead of silently keeping it
    assert str(r3.info["pitch_source"]).startswith("ccf_anchored")
    assert np.isclose(r3.info["pitch_mm"], res.info["pitch_mm"] * 200.0
                      / np.linalg.norm(res.baseline()), rtol=1e-9)
    # and the intrinsics do not silently acquire the translation factor
    sd3 = np.sqrt(np.diag(r3.covariance))
    assert np.allclose(sd3[intr][:12], sd[intr][:12], rtol=1e-4, atol=1e-8)


def test_rebase_does_not_publish_wrong_absolute_covariance():
    _, res = _fit()
    rb = res.rebased(0)
    assert rb.covariance is None
    assert np.isclose(baseline_sigma(rb), baseline_sigma(res), rtol=1e-6)
    assert np.allclose(relative_pose_sigma(rb, "left", "right")["translation_mm"],
                       relative_pose_sigma(res, "left", "right")["translation_mm"])


def test_stopping_never_accepts_covariance_only():
    _, res = _fit(12)
    st = stopping_report(res)
    assert st["ok"] is False
    # the missing independent checks are named explicitly
    failed = " ".join(st["reasons_failed"])
    assert "heldout" in failed and ("observations" in failed or "image_quadrants" in failed)


def test_view_remapping_is_compact_and_consistent():
    rng = np.random.default_rng(5)
    sc = make_scene(6, noise_px=0.1, outlier_frac=0.0, seed=5)
    obs = [Observation(o.camera, o.view + 10, o.uv) for o in sc.observations]
    res = calibrate(obs, sc.pitch_mm, perturbed_init(sc, rng),
                    free_kc=False, free_intrinsics=True, max_nfev=400)
    assert res.info["views_used"] == list(range(10, 16))
    assert len(res.views) == 6
    assert np.allclose(res.view_pose(10)[1], res.views[0][1])
    assert res.covariance.shape[0] == 39 + 6 * 6


def _rec(R, T, ir=True):
    v = np.zeros(33)
    v[0] = v[1] = 1000.0
    v[4] = 640.0; v[5] = 512.0
    v[20:23] = T
    v[23:32] = np.asarray(R, float).ravel()
    raw = encode_record(v, ir=ir, trailer=bytes(256) if ir else None)
    return decode_record(raw, ir=ir)


def test_ccf_relative_pose_is_frame_invariant():
    R1 = np.eye(3)
    R2 = np.array([[0.96, 0.0, -0.28], [0.0, 1.0, 0.0], [0.28, 0.0, 0.96]])
    T1 = np.array([0.0, 0.0, 400.0])
    T2 = np.array([120.0, 0.0, 410.0])
    a = _rec(R1, T1); b = _rec(R2, T2)
    R, t = relative_pose(a, b)
    # rotate the world frame: R' = R Rw^T, T' = T - R Rw^T tw
    th = 0.4
    Rw = np.array([[np.cos(th), -np.sin(th), 0], [np.sin(th), np.cos(th), 0], [0, 0, 1]])
    tw = np.array([10.0, -20.0, 5.0])
    a2 = _rec(R1 @ Rw.T, T1 - R1 @ Rw.T @ tw)
    b2 = _rec(R2 @ Rw.T, T2 - R2 @ Rw.T @ tw)
    R2r, t2r = relative_pose(a2, b2)
    assert np.allclose(R, R2r, atol=1e-12)
    assert np.allclose(t, t2r, atol=1e-9)


def test_ccf_consistency_guard_rejects_bad_translation():
    R1 = np.eye(3); R2 = np.eye(3)
    T1 = np.array([0.0, 0.0, 400.0]); T2 = np.array([120.0, 0.0, 410.0])
    a = _rec(R1, T1); b = _rec(R2, T2)
    good = consistency(a, b, expected=(R2 @ R1.T, T2 - R2 @ R1.T @ T1),
                       tau_translation_mm=2.0)
    assert good["ok"] is True
    bad = consistency(a, b, expected=(R2 @ R1.T, T2 - R2 @ R1.T @ T1 + np.array([0, 17.0, 0])),
                      tau_translation_mm=2.0)
    assert bad["ok"] is False
    assert bad["delta_translation_mm"] > 15.0


# ---------------------------------------------------------------------------
# observability gate: sigma provenance and the held-out refit (2026-11-13)
# ---------------------------------------------------------------------------

def test_gate_sigma_is_the_published_covariance_sigma():
    """The gate must report the same 1-sigma the estimator publishes.

    ``marginal_sigmas`` inverts ``result.marginal_information``; that matrix is
    now the inverse of the rig block of the *published* covariance, so the gate
    and ``param_std`` cannot drift apart.  The old Schur complement of the
    Gauss-Newton information had no residual scale at all and overstated every
    sigma by 1/sigma_px (measured 3.7x on the 297-view session).
    """
    from tools.wizard import observability

    sc, res = _fit()
    gate = observability.observability_gate(res, None, target="standard")
    ref = res.param_std()
    free = [n for n in gate["free_names"] if not n.startswith("view")]
    assert free
    for name in free:
        assert abs(gate["sigmas"][name] - ref[name]) <= 1e-4 * max(abs(ref[name]), 1e-12)
    # distractor: a held parameter must not appear as a free one
    assert not any(name.endswith(".k3") for name in free)


def test_model_sensitivity_refit_converges():
    """The held-out comparison must be between two *fitted* models.

    The shipped implementation used SciPy TRF with a finite-difference
    Jacobian and ``max_nfev=400``; at ~1460 free parameters it stopped before
    completing one Jacobian, so both models were the initial guess and the
    reported improvement was an artefact.
    """
    from tools.wizard import observability

    sc = make_scene(12, noise_px=0.2, outlier_frac=0.0, seed=19)
    res = calibrate(sc.observations, sc.pitch_mm, None, free_kc=False,
                    max_nfev=400)
    rep = observability.model_sensitivity_analysis(sc.observations, res,
                                                   pitch=sc.pitch_mm)
    assert rep["ok"] is True, rep
    assert rep["refit_converged"] is True, rep
    assert np.isfinite(rep["relative_improvement"])
    assert rep["heldout_rms_fixed_kc_px"] > 0
    gate = observability.observability_gate(res, None, target="standard",
                                            model_sensitivity=rep)
    sub = gate["gates"]["model_sensitivity"]
    # this result holds the factory distortion, so it passes only when freeing
    # it is *not* worth it (the gate judges the candidate's policy, both ways)
    assert sub["distortion_free"] is False
    assert sub["pass"] is (not bool(rep["worth_freeing"]))


@pytest.mark.parametrize("free_kc,worth,expect_pass", [
    (False, False, True),    # holds the factory distortion, freeing is not worth it
    (False, True, False),    # holds it although the evidence says free it
    (True, False, False),    # frees it although the evidence says hold it
    (True, True, True),      # frees it and the evidence supports that
])
def test_model_sensitivity_gate_follows_the_candidate_policy(free_kc, worth,
                                                             expect_pass):
    """The gate asks whether *this* candidate's distortion policy is supported.

    The shipped gate equated "worth freeing" with "pass", so a candidate that
    correctly held the factory distortion could never pass once the held-out
    analysis returned its (negative) answer.  Both directions must be checked.
    """
    from tools.wizard import observability

    sc = make_scene(8, noise_px=0.15, outlier_frac=0.0, seed=23)
    res = calibrate(sc.observations, sc.pitch_mm, None, free_kc=free_kc,
                    max_nfev=300)
    ms = {"ok": True, "refit_converged": True, "worth_freeing": worth,
          "relative_improvement": 0.20 if worth else 0.01,
          "heldout_rms_free_kc_px": 0.30, "heldout_rms_fixed_kc_px": 0.31}
    gate = observability.observability_gate(res, None, target="standard",
                                            model_sensitivity=ms)
    sub = gate["gates"]["model_sensitivity"]
    assert sub["distortion_free"] is bool(free_kc)
    assert sub["pass"] is expect_pass
    assert ("model_sensitivity" in gate["blocking_gates"]) is (not expect_pass)


def test_model_sensitivity_gate_fails_when_the_refit_did_not_converge():
    from tools.wizard import observability

    sc = make_scene(8, noise_px=0.15, outlier_frac=0.0, seed=24)
    res = calibrate(sc.observations, sc.pitch_mm, None, free_kc=False,
                    max_nfev=300)
    ms = {"ok": False, "reason": "refit did not converge", "worth_freeing": False}
    gate = observability.observability_gate(res, None, target="standard",
                                            model_sensitivity=ms)
    assert gate["gates"]["model_sensitivity"]["pass"] is False
    assert "model_sensitivity" in gate["blocking_gates"]
