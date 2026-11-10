"""Tests for the adaptive session state machine."""
import numpy as np

from calib import guidance
from calib.calibrate import CAMERAS
from calib.session import CalibrationSession, FrameSet
from calib.simloop import observations_from_pose
from calib.synth import make_scene, perturbed_init


def _grids(scene, R, t, rng, view=0):
    obs = observations_from_pose(scene, R, t, view=view, noise_px=0.1, rng=rng)
    return {o.camera: o for o in obs}


def test_session_bootstrap_solve_and_stop():
    scene = make_scene(n_views=6, noise_px=0.1, seed=5)
    sess = CalibrationSession(mode="quick", seed=0)
    sess.policy["require_images"] = False
    sess.cameras = perturbed_init(scene, np.random.default_rng(0))
    rng = np.random.default_rng(1)
    for i in range(3):
        R, t = scene.views[i]
        # a live stream sees the first post-move frame as motion and the next
        # one (same pose) as a valid dwell
        d = sess.ingest(FrameSet({}, timestamp=2.0 * i), grids=_grids(scene, R, t, rng, i))
        if not d.accepted:
            d = sess.ingest(FrameSet({}, timestamp=2.0 * i + 0.1),
                            grids=_grids(scene, R, t, rng, i))
        assert d.accepted, d.reasons
    assert sess.result is not None
    rec = sess.recommend()
    assert rec["ready"] and rec["text"]
    st = sess.stop_decision()
    assert "checks" in st and "n_views" in st


def test_session_rejects_motion_then_accepts_dwell():
    scene = make_scene(n_views=6, noise_px=0.1, seed=6)
    sess = CalibrationSession(mode="quick", seed=0)
    sess.policy["require_images"] = False
    sess.cameras = perturbed_init(scene, np.random.default_rng(0))
    rng = np.random.default_rng(1)
    R0, t0 = scene.views[0]
    d0 = sess.ingest(FrameSet({}, timestamp=0.0), grids=_grids(scene, R0, t0, rng, 0))
    assert d0.accepted
    R1, t1 = scene.views[1]
    d1 = sess.ingest(FrameSet({}, timestamp=0.1), grids=_grids(scene, R1, t1, rng, 1))
    assert not d1.accepted
    assert any("moving" in r for r in d1.reasons)
    # same pose again: now stationary, but identical to the first -> redundant
    d2 = sess.ingest(FrameSet({}, timestamp=0.2), grids=_grids(scene, R0, t0, rng, 0))
    assert not d2.accepted
    assert any("similar" in r for r in d2.reasons)


def test_session_quality_rejection_is_explained():
    scene = make_scene(n_views=4, noise_px=0.1, seed=7)
    sess = CalibrationSession(mode="quick", seed=0)
    sess.policy["require_images"] = False
    sess.cameras = perturbed_init(scene, np.random.default_rng(0))
    R, t = scene.views[0]
    obs = observations_from_pose(scene, R, t, view=0, noise_px=0.1,
                                 missing_frac=0.6, rng=np.random.default_rng(2))
    d = sess.ingest(FrameSet({}, timestamp=0.0), grids={o.camera: o for o in obs})
    assert not d.accepted
    assert d.reasons
    assert sess.rejected


def test_status_has_coverage_and_bins():
    scene = make_scene(n_views=4, noise_px=0.1, seed=8)
    sess = CalibrationSession(mode="quick", seed=0)
    sess.policy["require_images"] = False
    sess.cameras = perturbed_init(scene, np.random.default_rng(0))
    R, t = scene.views[0]
    sess.ingest(FrameSet({}, timestamp=0.0),
                grids=_grids(scene, R, t, np.random.default_rng(3), 0))
    st = sess.status()
    assert "coverage" in st and "bins" in st
    assert st["bins"]["occupied"] >= 1
