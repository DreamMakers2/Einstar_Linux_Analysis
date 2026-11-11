"""Tests for the colour-extrinsic fit (tools/calib/fit_colour_extrinsic.py).

Two independent checks:

* a **synthetic recovery** test where the true colour extrinsic is known: the
  fitting maths must recover a perturbed transform, so a good held-out number on
  real data cannot be an artefact of the optimiser;
* a **real-data** check on a slice of the recorded sweep: the shipped record
  must reproject badly and the fit must be good on held-out views.

The profile-status rule is tested too: the engine only samples colour when
`color_status == "ok"`, so that status must be earned by a held-out measurement.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

pytest.importorskip("cv2")

from calib import geometry, plate, recon_profile  # noqa: E402
from tools.calib import fit_colour_extrinsic as fitmod  # noqa: E402
from wizard import sources  # noqa: E402

FACTORY = Path(os.path.expanduser("~/einstar-private/einstar-profiles/factory-e10.json"))
SWEEP = Path(os.path.expanduser("~/einstar-private/sweep-b/frames"))


def _factory():
    return recon_profile.load(FACTORY)


def _rel(rx, ry, rz, t):
    R = cv2.Rodrigues(np.array([rx, ry, rz], float))[0]
    return R, np.asarray(t, float)


class _Pose:
    def __init__(self, R, t):
        self.R = R
        self.t = t


def test_synthetic_recovery_of_a_known_colour_extrinsic():
    """Render a board from a known colour pose, then recover the transform."""
    if not FACTORY.is_file():
        pytest.skip("factory profile absent")
    profile = _factory()
    Kc = np.asarray(profile.camera("color").intrinsics().K, float)
    kc = np.asarray(profile.camera("color").intrinsics().kc, float)

    true_R, true_T = _rel(0.02, -0.015, 0.004, [-132.0, 18.0, 12.0])
    left = profile.camera("ir_left").intrinsics()
    rng = np.random.default_rng(4)
    views = []
    for i in range(24):
        # a board pose in the left camera, looking at the plate
        d = 380.0 + 40.0 * (i % 4)
        tx, ty = rng.uniform(-0.15, 0.15), rng.uniform(-0.12, 0.12)
        roll = rng.uniform(-0.3, 0.3)
        R = cv2.Rodrigues(np.array([tx, ty, roll]))[0]
        pose = _Pose(R, np.array([0.0, 0.0, d]))
        obj = plate.board_points_mm()
        Xc = (true_R @ ((pose.R @ obj.T).T + pose.t).T).T + true_T
        img = geometry.project(Kc, kc, Xc)
        views.append({"frame": i, "pose": pose, "object": obj, "image": img,
                      "n_markers": len(obj)})

    # feed the fit a deliberately wrong starting extrinsic
    class _Cam:
        def __init__(self):
            self.R = (cv2.Rodrigues(np.array([0.05, 0.03, -0.02]))[0]).tolist()
            self.T = [-110.0, 22.0, 20.0]

        def intrinsics(self):
            return profile.camera("color").intrinsics()

    class _Profile:
        def camera(self, name):
            return _Cam()

    fit = fitmod.fit_colour_extrinsic(views, _Profile(), holdout=0.25)
    R_fit = np.asarray(fit["rotation"], float)
    T_fit = np.asarray(fit["translation"], float)
    err_t = float(np.linalg.norm(T_fit - true_T))
    err_r = float(np.degrees(np.arccos(np.clip(
        (np.trace(R_fit @ true_R.T) - 1.0) / 2.0, -1.0, 1.0))))
    assert fit["heldout_rms_px"] < 0.01, fit["heldout_rms_px"]
    assert err_t < 0.5, err_t
    assert err_r < 0.05, err_r


def test_real_sweep_fit_beats_the_shipped_record():
    if not (FACTORY.is_file() and SWEEP.is_dir()):
        pytest.skip("sweep or factory profile absent")
    profile = _factory()
    views = fitmod.collect_views(SWEEP, profile, max_views=24, stride=7)
    if len(views) < 10:
        pytest.skip(f"only {len(views)} usable views in the slice")
    fit = fitmod.fit_colour_extrinsic(views, profile, holdout=0.35)
    assert fit["before_rms_px"] > 50.0, fit["before_rms_px"]
    assert fit["heldout_rms_px"] < 2.0, fit["heldout_rms_px"]
    assert fit["train_rms_px"] < 2.0, fit["train_rms_px"]
    assert fit["improvement"] > 20.0, fit["improvement"]


def test_profile_status_is_earned_by_the_heldout_measurement(tmp_path):
    if not FACTORY.is_file():
        pytest.skip("factory profile absent")
    profile = _factory()
    # enough views that the held-out split clears the promotion threshold
    views = fitmod.collect_views(SWEEP, profile, max_views=40, stride=3) \
        if SWEEP.is_dir() else []
    if len(views) < 10:
        pytest.skip("not enough views")
    fit = fitmod.fit_colour_extrinsic(views, profile, holdout=0.4)

    # without the flag the status stays "fitted" - the engine will not use it
    plain = fitmod.write_profile(FACTORY, fit, tmp_path / "plain.json")
    doc = json.loads(plain.read_text())
    assert doc["color_status"] == "fitted"

    # with the flag and a good held-out number it becomes "ok"
    promoted = fitmod.write_profile(FACTORY, fit, tmp_path / "ok.json",
                                    validated_status=True)
    doc_ok = json.loads(promoted.read_text())
    assert doc_ok["color_status"] == "ok"
    assert any("colour_validation" in p for p in doc_ok["provenance"])

    # a bad held-out number must NOT be promoted, even with the flag set
    bad = dict(fit)
    bad["heldout_rms_px"] = 12.0
    bad["n_heldout"] = 30
    refused = fitmod.write_profile(FACTORY, bad, tmp_path / "bad.json",
                                   validated_status=True)
    assert json.loads(refused.read_text())["color_status"] == "fitted"
    bad_views = dict(fit)
    bad_views["n_heldout"] = 2
    refused2 = fitmod.write_profile(FACTORY, bad_views, tmp_path / "bad2.json",
                                    validated_status=True)
    assert json.loads(refused2.read_text())["color_status"] == "fitted"


def test_fit_generalises_to_an_independent_recording():
    """The colour pose must hold on a *different* session, not just held-out views."""
    sweep_c = Path(os.path.expanduser("~/einstar-private/sweep-c/frames"))
    if not (FACTORY.is_file() and SWEEP.is_dir() and sweep_c.is_dir()):
        pytest.skip("second recording not available")
    profile = _factory()
    train = fitmod.collect_views(SWEEP, profile, max_views=40, stride=3)
    evalv = fitmod.collect_views(sweep_c, profile, max_views=40, stride=7)
    if len(train) < 20 or len(evalv) < 10:
        pytest.skip(f"not enough views ({len(train)} train, {len(evalv)} eval)")
    fit = fitmod.fit_colour_extrinsic(train, profile, holdout=0.4)
    result = fitmod.evaluate_on(evalv, fit, profile)
    assert result["n_views"] >= 10
    assert result["stored_rms_px"] > 50.0, result
    assert result["fitted_rms_px"] < 2.0, result
