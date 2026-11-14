"""Regression tests for partial-plate lattice assignment.

Each case renders the real factory plate with the synthetic renderer, detects
with the real blob detector and checks the canonical labelling that
``calib.detect_partial`` recovers.  The point of the module is that a plate
whose chiral marker has left the image is still labelled canonically instead of
being lost or labelled as a shifted sub-lattice.
"""
from __future__ import annotations

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

from calib import detect, detect_partial, plate, recon_profile  # noqa: E402
from tools.sweep import orient  # noqa: E402
from wizard import coverage, sources  # noqa: E402

FACTORY = Path(os.path.expanduser("~/einstar-private/einstar-profiles/factory-e10.json"))
pytestmark = pytest.mark.skipif(not FACTORY.is_file(), reason="factory profile absent")

CHIRAL_PX = (624, 615)          # measured predicted position at 400 mm


@pytest.fixture(scope="module")
def rig():
    profile = recon_profile.load(FACTORY)
    cams = {k: profile.camera(n).intrinsics()
            for k, n in (("left", "ir_left"), ("right", "ir_right"), ("tex", "color"))}
    spec = sources.SyntheticSpec(n_views=1, noise_sigma=1.0, seed=3, cameras=cams)
    return profile, cams, spec


def _frame(rig, *, mask_chiral: bool):
    profile, cams, spec = rig
    R, t = coverage.family_pose(400.0, 14.0, 10.0, 0.0)
    img = sources.render_plate_view(cams["left"], (R, t), spec)
    if mask_chiral:
        img = img.copy()
        cv2.circle(img, CHIRAL_PX, 24, int(spec.background), -1)
    return np.ascontiguousarray(img)


def _spans(obs):
    us = [float(k[0]) for k in obs.points]
    vs = [float(k[1]) for k in obs.points]
    return (min(us), max(us)), (min(vs), max(vs))


def test_full_plate_standard_and_partial_agree(rig):
    img = _frame(rig, mask_chiral=False)
    strict = detect.detect_grid_auto(img)
    assert strict is not None and strict.n_lattice == 40
    assert strict.rms < 1.0
    partial = detect_partial.detect_grid_partial(img)
    assert partial.observation is not None, partial.reason
    (u0, u1), (v0, v1) = _spans(partial.observation)
    assert (u0, u1) == (0.0, 7.0) and (v0, v1) == (0.0, 4.0)
    assert partial.n_inliers >= 35


def test_chiral_masked_recovers_canonical_span(rig):
    """The strict path mislabels a sub-lattice; the partial path must not."""
    img = _frame(rig, mask_chiral=True)
    strict = detect.detect_grid_auto(img)
    if strict is not None:
        # the strict labelling is a shifted/narrower subset - that is the bug
        assert min(float(k[0]) for k in strict.points) > 0 or strict.n_lattice < 30
    partial = detect_partial.detect_grid_partial(img)
    assert partial.observation is not None, partial.reason
    obs = partial.observation
    (u0, u1), (v0, v1) = _spans(obs)
    assert (u0, u1) == (0.0, 7.0), (u0, u1)
    assert (v0, v1) == (0.0, 4.0), (v0, v1)
    assert obs.n_lattice >= 30 and obs.rms < 1.5


def test_partial_witness_accepts_when_chiral_is_out_of_area(rig):
    """Genuine out-of-frame loss is accepted; occlusion is not."""
    img = _frame(rig, mask_chiral=True)
    partial = detect_partial.detect_grid_partial(img)
    obs = partial.observation
    assert obs is not None
    outside = orient.check_orientation(img, obs, image_shape=(600, 1280)).as_dict()
    assert outside["ok"] is True
    assert outside["reason"] == "partial_verified"
    assert outside["n_visible"] == 3
    assert outside["scores"]["identity"] > outside["scores"]["mirror_u"]
    inside = orient.check_orientation(img, obs, image_shape=(1024, 1280)).as_dict()
    assert inside["ok"] is False
    assert inside["reason"] in {"partial_witness", "ambiguous"}


def test_random_blobs_are_rejected(rig):
    rng = np.random.default_rng(0)
    blobs = [detect.Blob(float(x), float(y), 40.0, 5, 5)
             for x, y in rng.uniform(100, 1100, size=(30, 2))]
    result = detect_partial.assign_grid_partial(blobs)
    assert result.observation is None
    assert result.reason


def test_coarse_lattice_without_the_2to1_signature_is_rejected(rig):
    """A regular grid without the (2,1),(4,1),(5,1) signature is not the plate."""
    blobs = []
    for row in range(5):
        for col in range(8):
            blobs.append(detect.Blob(100.0 + 60.0 * col, 100.0 + 60.0 * row,
                                     40.0, 6, 6))
    result = detect_partial.assign_grid_partial(blobs)
    assert result.observation is None


def test_determinism(rig):
    img = _frame(rig, mask_chiral=True)
    a = detect_partial.detect_grid_partial(img).as_dict()
    b = detect_partial.detect_grid_partial(img).as_dict()
    assert a == b


def test_controller_prefers_the_canonical_labelling(rig):
    from wizard import controller
    img = _frame(rig, mask_chiral=True)
    strict = detect.detect_grid_auto(img)
    chosen = controller.WizardController._prefer_partial(img, strict)
    assert chosen is not None
    us = {round(float(k[0])) for k in chosen.points}
    assert min(us) == 0 and max(us) == 7


# ---------------------------------------------------------------------------
# identity safety: chirality decides, counts never do
# ---------------------------------------------------------------------------

def _reference(rig):
    """Verified 40-marker labelling of the unmasked render (ground truth)."""
    img = _frame(rig, mask_chiral=False)
    obs = detect.detect_grid_auto(img)
    assert obs is not None and obs.n_lattice == 40 and obs.identity_ok
    return img, dict(obs.points)


def _pitch(points) -> float:
    P = np.array(list(points.values()), float)
    d = np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2)
    np.fill_diagonal(d, 1e9)
    return float(np.median(d.min(1)))


def _label_errors(result, truth) -> tuple[int, int]:
    """(correct, wrong) labels of a partial result against the truth pixels."""
    obs = result.observation
    assert obs is not None, result.reason
    ref = np.array(list(truth.values()), float)
    ref_uv = np.array(list(truth.keys()), float)
    right = wrong = 0
    for (u, v), pix in obs.points.items():
        j = int(np.argmin(np.linalg.norm(ref - np.asarray(pix, float), axis=1)))
        if np.linalg.norm(ref[j] - np.asarray(pix, float)) > 2.5:
            continue
        if abs(ref_uv[j][0] - u) < 0.01 and abs(ref_uv[j][1] - v) < 0.01:
            right += 1
        else:
            wrong += 1
    return right, wrong


def test_partial_never_returns_a_mirrored_labelling(rig):
    """A mirror-image frame has no front-facing labelling: det>0 or nothing."""
    img, _ = _reference(rig)
    mirrored = np.ascontiguousarray(img[:, ::-1])
    result = detect_partial.detect_grid_partial(mirrored)
    if result.observation is not None:
        det = float(np.linalg.det(np.asarray(result.observation.homography, float)))
        assert det > 0, "a mirrored labelling must never be selected"
        assert not result.observation.identity_ok, (
            "the witness cannot verify a mirrored plate layout: "
            f"score={result.witness_score}/{result.witness_visible}")


def test_truncated_rows_are_disambiguated_by_chirality(rig):
    """Rows v=3,4 out of frame: both v-signs explain the same blobs.

    This is the coin flip that produced the "mirrored marker geometry"
    rejections on real frames.  ``det(H) > 0`` must decide it, so the labels
    have to be the canonical ones, not merely self-consistent.
    """
    img, truth = _reference(rig)
    pitch = _pitch(truth)
    row2 = [truth[(float(u), 2.0)] for u in range(8)]
    ycut = int(min(p[1] for p in row2) + 0.45 * pitch)
    cropped = np.ascontiguousarray(img[:ycut, :])
    result = detect_partial.detect_grid_partial(cropped)
    assert result.observation is not None, result.reason
    assert float(np.linalg.det(np.asarray(result.observation.homography, float))) > 0
    right, wrong = _label_errors(result, truth)
    assert wrong == 0, f"{wrong} wrong labels out of {right + wrong}"
    assert right >= 15
    assert {round(float(k[1])) for k in result.observation.points} <= {0.0, 1.0, 2.0}


def test_one_big_marker_out_of_frame_uses_pair_and_chiral_witness(rig):
    """A column clipped away removes (2,1): the pair + chiral marker must label it."""
    img, truth = _reference(rig)
    pitch = _pitch(truth)
    x22 = truth[(2.0, 1.0)][0]
    x41 = truth[(4.0, 1.0)][0]
    xcut = int(round(0.5 * (x22 + x41) + 0.15 * pitch))
    cropped = np.ascontiguousarray(img[:, xcut:])
    result = detect_partial.detect_grid_partial(cropped)
    assert result.observation is not None, result.reason
    obs = result.observation
    assert float(np.linalg.det(np.asarray(obs.homography, float))) > 0
    assert obs.identity_ok, (f"witness {result.witness_score}/{result.witness_visible} "
                             f"({result.reason})")
    # coordinates shift by the crop
    shifted = {k: np.asarray(v, float) - np.array([xcut, 0.0]) for k, v in truth.items()}
    right, wrong = _label_errors(result, shifted)
    assert wrong == 0 and right >= 12


def test_verified_partial_observation_stops_the_sweep(rig):
    """A verified partial view is accepted on the first pass (performance rule).

    The last two columns are out of frame, so the plate is partial but all four
    orientation markers are still visible: the first polarity/threshold pass is
    already verified with more than ``VERIFIED_EARLY_MIN`` markers, which is the
    evidence-based reason to stop instead of paying six passes.
    """
    img, truth = _reference(rig)
    pitch = _pitch(truth)
    col6 = [truth[(6.0, float(v))] for v in range(5)]
    xcut = int(min(p[0] for p in col6) - 0.45 * pitch)
    cropped = np.ascontiguousarray(img[:, :xcut])
    out = detect.detect_grid_auto(cropped, early_lattice=40, early_rms=0.5)
    assert out is not None and out.identity_ok
    assert out.n_lattice >= detect.VERIFIED_EARLY_MIN
    exhaustive = detect.detect_grid_auto(cropped)
    assert exhaustive is not None and exhaustive.identity_ok
    assert exhaustive.n_lattice >= out.n_lattice
