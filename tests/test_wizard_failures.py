"""Failure-case tests for the calibration wizard.

Each test drives the *real* decision path with a dataset that is insufficient or
misleading in one designed way and asserts that the wizard refuses it.  The
tests are deliberately small (12-20 rendered views, or a direct gate call) so
the file stays fast; the full evidence matrix lives in
``tools/wizard/failure_matrix.py`` and
``docs/calibration/wizard_failure_matrix.md``.

Offline only: no scanner, no capture process and no ``libusb`` backend.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from calib import synth                          # noqa: E402
from calib.calibrate import calibrate            # noqa: E402
from tools.wizard import (coverage, failure_matrix, lifecycle,  # noqa: E402
                          observability, sources, types)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run_case(case_id: str, tmp_path: Path) -> dict:
    case = failure_matrix.CASE_BY_ID[case_id]
    records = failure_matrix.run_case(case, tmp_path, force=True)
    assert records, f"{case_id} produced no record"
    return records[0]


def _profile_from_scene(scene, dT=(0.0, 0.0, 0.0)) -> dict:
    """A valid candidate profile whose right camera is shifted by ``dT`` mm."""
    cams = {}
    for src, dst in (("left", "ir_left"), ("right", "ir_right"), ("tex", "color")):
        c = scene.cameras[src]
        R, t = scene.extrinsics[src]
        t = np.asarray(t, float) + (np.asarray(dT, float) if src != "left"
                                    else np.zeros(3))
        cams[dst] = {"fx": c.fx, "fy": c.fy, "cx": c.cx, "cy": c.cy,
                     "skew": 0.0, "dist": [float(x) for x in c.kc],
                     "R": np.asarray(R, float).ravel().tolist(),
                     "T": [float(x) for x in t]}
    return {"schema": "einstar.recon.profile/1", "kind": "candidate",
            "image_width": 1280, "image_height": 1024, "units": "mm",
            "plate_pitch_mm": float(scene.pitch_mm),
            "color_status": "candidate", "cameras": cams}


def _validate_profile(tmp_path: Path, doc: dict, observations) -> dict:
    cand = tmp_path / "candidate"
    cand.mkdir(parents=True, exist_ok=True)
    types.atomic_write_json(cand / "profile.json", doc)
    return lifecycle.validate(cand / "profile.json", observations, None)


def _view(i: int, tex_markers: int) -> dict:
    return {"view_id": i, "pose": (np.eye(3), np.array([0.0, 0.0, 400.0])),
            "n_markers": {"left": 40, "right": 40, "tex": int(tex_markers)},
            "quality": {"saturation_frac": 0.0, "focus": 500.0},
            "image_points": {"left": np.zeros((4, 2))}}


# ---------------------------------------------------------------------------
# 1. repeated viewpoints / redundant-only data
# ---------------------------------------------------------------------------

def test_repeated_viewpoints_are_refused(tmp_path: Path) -> None:
    rec = _run_case("repeated-viewpoints", tmp_path)
    dec = rec["decisions"]
    assert dec["groups"] == 12
    # the wizard seeds a few bootstrap views, then rejects every duplicate
    assert dec["accepted"] <= 3
    assert dec["rejected"] >= 9
    assert any("too similar to an accepted view" in key
               for key in dec["raw_reasons"]), dec["raw_reasons"]
    assert rec["coverage"]["ready"] is False
    assert rec["candidate"]["would_activate"] is False
    assert rec["verdict"] == "correctly refused"
    # Coverage not ready means the UI refuses to start a final solve; if a
    # caller forces one, validation must still not report a pass.
    assert (rec.get("validation") or {}).get("passed") is not True


# ---------------------------------------------------------------------------
# 7. incorrect marker orientation (mirrored plate layout)
# ---------------------------------------------------------------------------

def test_mirrored_marker_layout_is_rejected_at_ingest(tmp_path: Path) -> None:
    rec = _run_case("mirrored-plate", tmp_path)
    dec = rec["decisions"]
    assert dec["accepted"] == 0
    assert dec["rejected"] == 12
    assert any("mirrored marker geometry" in key for key in dec["raw_reasons"]), \
        dec["raw_reasons"]
    assert rec["candidate"]["produced"] is False
    assert rec["verdict"] == "correctly refused"


# ---------------------------------------------------------------------------
# 8. ambiguous partial detections (missing markers)
# ---------------------------------------------------------------------------

def test_missing_markers_scene_cannot_calibrate(tmp_path: Path) -> None:
    rec = _run_case("missing-markers", tmp_path)
    # The partial detector assigns a canonical lattice from the three large
    # markers with a verified identity, so a half-visible plate can be accepted
    # (measured: 3 of 12 before the identity work, 6 of 12 after it).  The
    # requirement is that such a dataset still cannot produce a usable
    # calibration, which is what the rest of this test asserts.
    assert rec["decisions"]["accepted"] <= 6, rec["decisions"]
    assert rec["decisions"]["rejected"] >= 5, rec["decisions"]
    # the refusal must name the missing/ambiguous marker evidence, whatever
    # the current detector calls it ("no markers", "only N markers",
    # "ambiguous marker assignment", ...)
    assert any(("marker" in key) or ("only" in key)
               for key in rec["decisions"]["raw_reasons"]), \
        rec["decisions"]["raw_reasons"]
    assert rec["readiness"]["ready"] is False
    assert rec["candidate"].get("would_activate") is False
    assert rec["verdict"] == "correctly refused"


# ---------------------------------------------------------------------------
# 11. colour participation rule: >= 12 views with >= 12 colour markers, >= 60 %
# ---------------------------------------------------------------------------

def test_colour_participation_rule() -> None:
    six = coverage.measure([_view(i, 40) for i in range(6)])
    assert six["D6"]["satisfied"] is False
    assert six["checks"]["colour"]["measured"]["n_views_with_colour"] == 6

    eleven = coverage.measure([_view(i, 40) for i in range(11)])
    assert eleven["D6"]["satisfied"] is False, "11 colour views must not pass"

    twelve = coverage.measure([_view(i, 40) for i in range(12)])
    assert twelve["D6"]["satisfied"] is True
    assert twelve["checks"]["colour"]["measured"]["participation_fraction"] == 1.0

    # twelve views but only six carry colour markers: 50 % < 60 %
    half = coverage.measure([_view(i, 40 if i < 6 else 0) for i in range(12)])
    assert half["D6"]["satisfied"] is False
    assert half["checks"]["colour"]["measured"]["participation_fraction"] == 0.5

    # nine colour views out of twelve is 75 %, but still below the 12-view floor
    mixed = coverage.measure([_view(i, 40 if i < 9 else 0) for i in range(12)])
    assert mixed["D6"]["satisfied"] is False


# ---------------------------------------------------------------------------
# 10. poorly constrained distortion: a real over-parameterised fit is refused
# ---------------------------------------------------------------------------

def test_observability_refuses_overparameterised_distortion() -> None:
    scene = synth.make_scene(n_views=8, noise_px=0.2, outlier_frac=0.0, seed=3)
    held = calibrate(scene.observations, scene.pitch_mm, scene.cameras,
                     free_kc=False, free_intrinsics=True)
    free = calibrate(scene.observations, scene.pitch_mm, scene.cameras,
                     free_kc=True, free_intrinsics=True)
    assert free.info["free_kc"] is True
    # the extra parameters still fit the training data at least as well
    assert free.residual_rms <= held.residual_rms + 1e-9

    gate = observability.observability_gate(free, None, target="standard")
    assert gate["passed"] is False
    assert "freed_parameters" in gate["blocking_gates"], gate["blocking_gates"]
    # The corrected marginal covariance reclassifies parameters that the old,
    # unscaled Schur complement called "unobservable" as "weak".  What matters
    # is that the excess distortion terms are not constrained; the gate refuses
    # the over-parameterised model on either count.
    counts = gate["counts"]
    assert counts["unobservable"] + counts["weak"] >= 10, counts
    assert counts["unobservable"] >= 1, counts
    dist = {name: rec for name, rec in gate["parameters"].items()
            if name.split(".")[-1] in ("k1", "k2", "k3", "p1", "p2")}
    assert dist, "the free fit must report distortion parameters"
    assert any(rec["verdict"] != "constrained" for rec in dist.values()), dist

    idf_free = observability.identifiability(free)
    idf_held = observability.identifiability(held)
    assert idf_free["n_free"] == 39
    assert idf_held["n_free"] == 24
    assert idf_free["condition_scaled"] > 3.0 * idf_held["condition_scaled"]
    sigmas = observability.marginal_sigmas(free)
    worst = max((abs(float(s)) for k, s in sigmas.items()
                 if k.split(".")[-1] in ("k1", "k2", "k3", "p1", "p2")),
                default=0.0)
    assert worst > 0.1, f"distortion coefficients look constrained: {worst}"
    # the gate's own unit-aware ratio (sigma / budget) is the invariant that
    # does not depend on the covariance scaling; the k2/k3 terms exceed it.
    ratios = [float(rec.get("ratio") or 0.0)
              for name, rec in gate["parameters"].items()
              if name.split(".")[-1] in ("k1", "k2", "k3", "p1", "p2")]
    assert ratios and max(ratios) > 2.0, ratios


# ---------------------------------------------------------------------------
# 12. inadequate held-out performance: the cross-camera gate detects it
# ---------------------------------------------------------------------------

def test_cross_camera_gate_detects_baseline_error(tmp_path: Path) -> None:
    scene = synth.make_scene(n_views=10, noise_px=0.2, outlier_frac=0.0, seed=3,
                             distance_range=(300.0, 560.0), tilt_max_deg=25.0)
    views = sorted({int(o.view) for o in scene.observations})
    held = [o for o in scene.observations if int(o.view) in set(views[::4])]
    assert len({int(o.view) for o in held}) >= 2

    good = _validate_profile(tmp_path / "good",
                             _profile_from_scene(scene), held)
    bad = _validate_profile(tmp_path / "bad",
                            _profile_from_scene(scene, dT=(0.5, 0.0, 0.0)), held)

    good_cc = good["gates"]["cross_camera"]
    bad_cc = bad["gates"]["cross_camera"]
    assert good_cc["pass"] is True, good["reasons"]
    assert good_cc["value"]["worst_rms_px"] < 0.50, good_cc["value"]
    assert bad_cc["pass"] is False, bad_cc["value"]
    assert bad_cc["value"]["worst_rms_px"] > 1.0, bad_cc["value"]
    assert any("cross-camera" in reason for reason in bad["reasons"]), bad["reasons"]


# ---------------------------------------------------------------------------
# 9. corrupted replay images are silently skipped (and the dataset shrinks)
# ---------------------------------------------------------------------------

def test_corrupted_replay_groups_are_skipped(tmp_path: Path) -> None:
    spec = failure_matrix.SyntheticSpec(pattern="diverse", n_views=4, seed=7)
    src = failure_matrix.SyntheticSource(spec)
    frames = sources.write_replay_frames(list(src), tmp_path / "frames")
    (frames / "frame_000001_cam1.pgm").unlink()
    p = frames / "frame_000002_cam2.pgm"
    p.write_bytes(p.read_bytes()[:128])

    reader = sources.ReplayDirSource(frames)
    groups = list(reader)
    yielded = [int(g.index) for g in groups]
    reader.close()
    # Groups are renumbered densely by the source (that is what the session
    # manifests and resume rely on), but a corrupt or incomplete group must be
    # skipped and recorded rather than ending the replay.
    assert len(yielded) == 2, yielded
    assert all(all(im in g.images for im in ("left", "right", "tex"))
               for g in groups)
    assert len(reader.skipped) == 2, reader.skipped
    assert all("incomplete or unreadable" in str(s["reason"])
               for s in reader.skipped)
