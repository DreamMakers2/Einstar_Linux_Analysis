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
    # information/Schur consistency: inv(C_rr) = marginal information
    sig = res.info["covariance"]["sigma_px"]
    inv = np.linalg.solve(rr[np.ix_(idx, idx)], np.eye(len(idx))) * sig ** 2
    M = res.marginal_information[np.ix_(idx, idx)]
    assert np.allclose(M, inv, rtol=1e-6, atol=1e-6 * np.max(np.abs(M)))


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
