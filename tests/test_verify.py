"""Tests for independent verification and bias diagnostics."""
import numpy as np

from calib import verify
from calib.adaptive import board_pose
from calib.calibrate import CAMERAS, calibrate
from calib.geometry import project
from calib import plate
from calib.simloop import observations_from_pose
from calib.synth import make_scene, perturbed_init


def _tilted_views(scene, tilts, d=380.0, noise=0.1, seed=0):
    rng = np.random.default_rng(seed)
    obs = []
    for i, tx in enumerate(tilts):
        R, t = board_pose(d, tx, 0.0, 0.0)
        obs += observations_from_pose(scene, R, t, view=i, noise_px=noise, rng=rng)
    return obs


def _fit(scene, obs):
    init = perturbed_init(scene, np.random.default_rng(0))
    return calibrate(obs, 28.0, init, free_kc=True, free_intrinsics=True, max_nfev=400)


def test_residual_trends_detect_tilt_bias():
    scene = make_scene(n_views=4, noise_px=0.1, seed=11)
    tilts = [-30, -20, -10, 0, 10, 20, 30, 0, 15, -15]
    obs = _tilted_views(scene, tilts, noise=0.05, seed=3)
    # inject a deterministic outward radial shift proportional to tilt
    by_view = {o.view: o for o in obs if o.camera == "left"}
    for v, o in by_view.items():
        R, t = board_pose(380.0, tilts[v], 0.0, 0.0)
        tilt = abs(tilts[v])
        cam = scene.cameras["left"]
        for k, uv in list(o.uv.items()):
            d = np.asarray(uv) - np.array([cam.cx, cam.cy])
            d = d / max(np.linalg.norm(d), 1e-9)
            o.uv[k] = np.asarray(uv) + 0.15 * tilt * d
    res = _fit(scene, obs)
    rep = verify.residual_trends(res, obs)
    assert rep["ok"]
    assert rep["circular_tilt_slope"]["slope"] > 0.01
    assert rep["tilt_bias_detected"]


def test_residual_trends_clean_is_not_flagged():
    scene = make_scene(n_views=4, noise_px=0.1, seed=12)
    tilts = [-25, -15, 0, 15, 25, 0, 10, -10]
    obs = _tilted_views(scene, tilts, noise=0.05, seed=4)
    res = _fit(scene, obs)
    rep = verify.residual_trends(res, obs)
    assert rep["ok"]
    assert not rep["tilt_bias_detected"]


def test_epipolar_consistency_noise_free():
    scene = make_scene(n_views=4, noise_px=0.0, seed=13)
    obs = _tilted_views(scene, [0, 10, -10, 20], noise=0.0, seed=5)
    res = _fit(scene, obs)
    e = verify.epipolar_consistency(res, obs)
    assert e["ok"] and e["median_px"] < 0.05


def test_heldout_and_verify_report():
    scene = make_scene(n_views=12, noise_px=0.1, seed=14)
    rng = np.random.default_rng(5)
    obs = []
    for i, (R, t) in enumerate(scene.views):
        obs += observations_from_pose(scene, R, t, view=i, noise_px=0.1, rng=rng)
    init = perturbed_init(scene, np.random.default_rng(0))
    res = calibrate(obs, 28.0, init, free_kc=False, free_intrinsics=True, max_nfev=400)
    r = verify.verify(res, obs, init, do_heldout=True, do_split_half=False)
    assert r["heldout"]["ok"]
    assert r["heldout"]["heldout_rms_px"] < 0.5
    assert "gates" in r
