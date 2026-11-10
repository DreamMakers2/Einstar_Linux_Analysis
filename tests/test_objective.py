"""Tests for the task-space objective and view scoring."""
import numpy as np
import pytest

from calib import objective, plate
from calib.adaptive import ViewCandidate, board_pose
from calib.calibrate import CAMERAS, CameraIntrinsics, calibrate
from calib.simloop import observations_from_pose
from calib.synth import make_scene, perturbed_init


def _rig_from_scene(scene):
    from calib.geometry import rvec_from_R
    x = np.zeros(39)
    for i, c in enumerate(CAMERAS):
        x[9 * i:9 * i + 9] = scene.cameras[c].as_vec()
    for i, c in enumerate(("right", "tex")):
        R, t = scene.extrinsics[c]
        x[27 + 6 * i:27 + 6 * i + 6] = np.concatenate([rvec_from_R(R), t])
    return x


def test_triangulation_and_sensitivity_are_consistent():
    scene = make_scene(n_views=3, noise_px=0.0, seed=0)
    x = _rig_from_scene(scene)
    pts = objective.measurement_volume(n_z=2, n_xy=2)
    cams, extr = objective._split_rig(x)
    uv_l = objective._pixels(cams, extr, "left", pts)
    uv_r = objective._pixels(cams, extr, "right", pts)
    X = objective.triangulate(x, uv_l, uv_r)
    assert np.abs(X - pts).max() < 1e-4
    G = objective.task_sensitivity(x, pts)
    assert G.shape == (len(pts), 3, 39)
    assert np.isfinite(G).all()
    # focal length changes must move the reconstruction
    j = 0
    assert np.abs(G[:, :, j]).max() > 0


def test_task_covariance_and_metric_agree():
    scene = make_scene(n_views=4, noise_px=0.0, seed=1)
    x = _rig_from_scene(scene)
    pts = objective.measurement_volume(n_z=2, n_xy=2)
    G = objective.task_sensitivity(x, pts)
    C = np.diag(np.linspace(1e-4, 1e-2, 39))
    cov = objective.task_covariance_from_G(G, C)
    assert cov.shape == (len(pts), 3, 3)
    assert np.all(np.linalg.eigvalsh(cov) > -1e-12)
    M = objective.task_metric(G)
    assert np.isclose(np.trace(C @ M), np.einsum("pii->", cov), rtol=1e-8)


def test_predicted_gain_matches_re_solve():
    scene = make_scene(n_views=8, noise_px=0.15, seed=4)
    rng = np.random.default_rng(2)
    init = perturbed_init(scene, rng)
    obs = []
    for i, (R, t) in enumerate(scene.views[:7]):
        obs += observations_from_pose(scene, R, t, view=i, noise_px=0.15, rng=rng)
    res7 = calibrate(obs, 28.0, init, free_kc=False, free_intrinsics=True, max_nfev=300)
    R8, t8 = scene.views[7]
    obs8 = observations_from_pose(scene, R8, t8, view=7, noise_px=0.15, rng=rng)
    res8 = calibrate(obs + obs8, 28.0, init, free_kc=False, free_intrinsics=True, max_nfev=300)
    pts = objective.measurement_volume(n_z=3, n_xy=2)
    a7 = objective.evaluate_task(res7, "A", pts).value
    a8 = objective.evaluate_task(res8, "A", pts).value
    actual = (a7 - a8) / a7
    cand = ViewCandidate(float(t8[2]), 0, 0, 0, R8, t8, "held")
    pred = objective.score_candidate(res7, cand, pts, "A", effort=0.0)
    assert pred is not None
    predicted = pred.gain / pred.objective_before
    assert actual > 0
    assert abs(predicted - actual) < 0.5 * actual + 0.01


def test_candidate_outside_field_is_rejected():
    scene = make_scene(n_views=4, noise_px=0.0, seed=2)
    x = _rig_from_scene(scene)
    # a pose far above the field: plate cannot be visible
    R = np.diag([1.0, -1.0, -1.0])
    t = np.array([0.0, 4000.0, 400.0])
    J = objective._candidate_joint_jacobian(x, R, t, 28.0, np.ones(39, bool))
    assert J is None


def test_volume_contains_working_range():
    pts = objective.measurement_volume()
    assert pts[:, 2].min() == 200 and pts[:, 2].max() == 600
    assert np.isfinite(pts).all()
