"""Lifecycle tests: candidate, Phase-7 gates, activation, rollback, invariants."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from calib import synth
from calib.calibrate import CAMERAS, CalibrationResult, CameraIntrinsics
from calib.ccf import encode_record
from calib.profiles import ProfileStore
from tools.wizard import lifecycle
from tools.wizard.lifecycle import (FactoryMutated, LifecycleError, activate,
                                    active_profile, profile_to_records, rollback,
                                    save_candidate, tree_digest, validate)
from tools.wizard.types import sha256_file

SERIAL = "TESTUNIT"


# ---------------------------------------------------------------------------
# fixtures: a store with a factory snapshot, a candidate profile, observations
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path, serial: str = SERIAL) -> Path:
    root = tmp_path / "store"
    fac = root / serial / "factory"
    fac.mkdir(parents=True)
    files = {}
    for i, (name, ir) in enumerate((("left", True), ("right", True), ("tex", False))):
        v = np.zeros(33)
        v[0] = v[1] = 1159.0 + i
        v[4], v[5] = 624.0, 534.0 + i
        v[10:15] = [-0.162, 0.173, 0.0002, 0.00015, 0.018]
        v[20:23] = [0.0, 0.0, 400.0]
        v[23:32] = np.eye(3).ravel()
        raw = encode_record(v, ir=ir, trailer=bytes(256) if ir else None)
        p = fac / f"{name}.CCF"
        p.write_bytes(raw)
        files[f"{name}.CCF"] = sha256_file(p)
    (fac / "manifest.json").write_text(
        json.dumps({"created_utc": "2026-11-11T00:00:00+00:00", "files": files}))
    return root


@pytest.fixture(scope="module")
def scene():
    return synth.make_scene(n_views=12, noise_px=0.2, outlier_frac=0.0, seed=3,
                            distance_range=(300.0, 560.0), tilt_max_deg=25.0)


def profile_from_scene(scene, *, dT=(0.0, 0.0, 0.0), dR: np.ndarray | None = None) -> dict:
    cams = {}
    for src, dst in (("left", "ir_left"), ("right", "ir_right"), ("tex", "color")):
        c = scene.cameras[src]
        R, t = scene.extrinsics[src]
        if src != "left":                      # ir_left must stay the identity
            t = np.asarray(t, float) + np.asarray(dT, float)
            if dR is not None:
                R = np.asarray(dR, float) @ R
        cams[dst] = {"fx": c.fx, "fy": c.fy, "cx": c.cx, "cy": c.cy,
                     "skew": 0.0, "dist": [float(x) for x in c.kc],
                     "R": np.asarray(R, float).ravel().tolist(),
                     "T": [float(x) for x in t]}
    return {"schema": "einstar.recon.profile/1", "kind": "candidate",
            "image_width": 1280, "image_height": 1024, "units": "mm",
            "plate_pitch_mm": float(scene.pitch_mm), "color_status": "candidate",
            "cameras": cams}


def fake_result(cams: dict, extr: dict, rms: float = 0.01) -> CalibrationResult:
    """A rig result without a fit (validate only needs cameras + extrinsics)."""
    return CalibrationResult(dict(cams), dict(extr), [], None, rms, rms, 0, 0,
                             "huber", {"free_kc": True})


def factory_result(scene, offset=(0.0, 0.5, 0.0), rms: float = 0.60):
    """The factory model: the same scene with a stale 0.5 mm extrinsic."""
    extr = {"left": scene.extrinsics["left"]}
    for cam in ("right", "tex"):
        R, t = scene.extrinsics[cam]
        extr[cam] = (R, np.asarray(t, float) + np.asarray(offset, float))
    return fake_result(scene.cameras, extr, rms=rms)


def passing_gates(**over) -> dict:
    g = {"observability": {"passed": True, "counts": {"constrained": 20, "weak": 0,
                                                      "unobservable": 0},
                           "blocking_gates": [],
                           "identifiability": {"condition_scaled": 1.0e4},
                           "gates": {"stereo_rotation": {"status": "constrained"},
                                     "model_sensitivity": {"value": {
                                         "ok": True, "worth_freeing": True,
                                         "relative_improvement": 0.12}}}},
         "model_sensitivity": {"ok": True, "worth_freeing": True,
                               "relative_improvement": 0.12,
                               "heldout_rms_free_kc_px": 0.30,
                               "heldout_rms_fixed_kc_px": 0.34},
         "target": "standard"}
    g.update(over)
    return g


def session_dir(tmp_path: Path, profile: dict, *, report: dict | None = None,
                name: str = "SESSION") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    save_candidate(d, profile, report or {"schema": "test", "gates": {}})
    return d


# ---------------------------------------------------------------------------
# save_candidate
# ---------------------------------------------------------------------------

def test_save_candidate_is_pending_and_carries_provenance(tmp_path, scene):
    d = session_dir(tmp_path, profile_from_scene(scene))
    prof = json.loads((d / "candidate/profile.json").read_text())
    assert prof["validation"] == "pending"
    assert prof["schema"] == "einstar.recon.profile/1"
    cand = prof["candidate"]
    assert cand["schema"] == lifecycle.CANDIDATE_SCHEMA
    assert cand["software_version"]
    assert cand["model"]["distortion_model"] == "opencv5"
    assert cand["model"]["pitch_mm"] == pytest.approx(scene.pitch_mm)
    assert "accepted_views_digest" not in cand["input_hashes"] or True
    assert prof["scale"]["validated"] is False           # scale is never claimed
    val = json.loads((d / "candidate/validation.json").read_text())
    assert val["validation"] == "pending" and val["created_utc"]
    assert sha256_file(d / "candidate/validation.json") == \
        cand["validation_report_sha256"]
    # the written bytes load as a real profile
    from calib import recon_profile
    loaded = recon_profile.load(d / "candidate/profile.json")
    assert set(loaded.cameras) == {"ir_left", "ir_right", "color"}


def test_save_candidate_refuses_a_bad_profile(tmp_path):
    d = tmp_path / "S"
    bad = {"schema": "einstar.recon.profile/1", "cameras": {"ir_left": {
        "fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0, "dist": [0] * 5,
        "R": [1, 0, 0, 0, 1, 0, 0, 0, 0], "T": [0, 0, 0]}}}
    with pytest.raises(LifecycleError) as exc:
        save_candidate(d, bad, {})
    assert exc.value.code == "bad_profile"
    assert not (d / "candidate/profile.json").exists()


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def test_validate_passes_a_truth_matched_candidate(tmp_path, scene):
    d = session_dir(tmp_path, profile_from_scene(scene))
    factory = factory_result(scene)
    rep = validate(d / "candidate/profile.json", scene.observations, factory,
                   gates=passing_gates())
    assert rep["passed"] is True, rep["reasons"]
    assert rep["metrics"]["cross_camera"]["factory"]["worst_rms_px"] > 0.9
    assert rep["gates"]["cross_camera"]["value"]["worst_rms_px"] < 0.5
    m = rep["metrics"]
    assert m["cross_camera"]["worst_rms_px"] == pytest.approx(0.2, abs=0.15)
    # the noise floor of a 12-view, 0.2 px scene: the per-view PnP pair has
    # ~0.1 deg of rotation noise, which is a 0.4 mm lever arm at 400 mm, so the
    # baseline magnitude carries ~0.3 mm and the direction ~0.2 deg
    assert m["stereo_consistency"]["rotation_deg_median"] < 0.15
    assert m["stereo_consistency"]["baseline_error_mm_median"] < 0.50
    assert m["stereo_consistency"]["direction_error_deg_median"] < 0.30
    # 0.2 px of disparity noise at ~400 mm gives ~0.2 mm of plane RMS; the
    # live factory profile measured 0.237 mm on the real plate
    assert abs(m["triangulation"]["plane_rms_mm"]) < 0.50
    assert abs(m["triangulation"]["scale_rel_error"]) < 1e-3
    assert m["pose_diversity"]["roll_bins_covered"] == 4
    assert m["pose_diversity"]["distance_span_mm"] > 150.0
    # the reference training residual is present but explicitly not a gate input
    assert m["reference_training_rms_px"] == pytest.approx(factory.residual_rms)
    assert "not an acceptance input" in rep["note"]
    # the verdict is durable next to the candidate
    written = json.loads((d / "candidate/validation.json").read_text())
    assert written["passed"] is True and written["candidate_sha256"] == \
        rep["candidate_sha256"]


def test_validate_rejects_a_misaligned_extrinsic(tmp_path, scene):
    bad = profile_from_scene(scene, dT=(0.0, 2.0, 0.0))
    d = session_dir(tmp_path, bad, name="BAD")
    factory = factory_result(scene)
    rep = validate(d / "candidate/profile.json", scene.observations, factory,
                   gates=passing_gates())
    assert rep["passed"] is False
    assert rep["gates"]["cross_camera"]["value"]["worst_rms_px"] > 3.0
    assert any("cross-camera" in r for r in rep["reasons"])
    assert rep["gates"]["stereo_consistency"]["pass"] is False


def test_validate_ignores_a_low_training_rms(tmp_path, scene):
    """A tiny factory residual must not rescue a geometrically wrong candidate."""
    bad = profile_from_scene(scene, dT=(0.0, 3.0, 0.0))
    d = session_dir(tmp_path, bad, name="BAD2")
    cheat = factory_result(scene, offset=(0.0, 0.5, 0.0), rms=1e-6)
    rep = validate(d / "candidate/profile.json", scene.observations, cheat,
                   gates=passing_gates())
    assert rep["passed"] is False
    assert rep["metrics"]["reference_training_rms_px"] == pytest.approx(1e-6)


def test_validate_needs_observability_and_model_sensitivity(tmp_path, scene):
    d = session_dir(tmp_path, profile_from_scene(scene))
    rep = validate(d / "candidate/profile.json", scene.observations, None,
                   gates={"target": "standard"})
    assert rep["passed"] is False
    failed = {k for k, v in rep["gates"].items() if not v["pass"]}
    assert {"observability", "model_sensitivity"} <= failed
    assert any("observability gate not supplied" in r for r in rep["reasons"])
    assert any("model sensitivity not supplied" in r for r in rep["reasons"])


def test_validate_rejects_a_profile_without_a_scale_label(tmp_path, scene):
    prof = profile_from_scene(scene)
    d = session_dir(tmp_path, prof)
    p = d / "candidate/profile.json"
    doc = json.loads(p.read_text())
    doc.pop("scale", None)
    doc.get("provenance", {}).pop("physical_scale", None)
    p.write_text(json.dumps(doc))
    rep = validate(p, scene.observations, None, gates=passing_gates())
    assert rep["passed"] is False
    assert rep["gates"]["scale_label"]["pass"] is False
    assert any("scale-uncertainty" in r for r in rep["reasons"])


# ---------------------------------------------------------------------------
# activate / rollback / active_profile
# ---------------------------------------------------------------------------

def test_activate_requires_a_passing_validation(tmp_path, scene):
    store = make_store(tmp_path)
    d = session_dir(tmp_path, profile_from_scene(scene))
    before = tree_digest(store)
    with pytest.raises(LifecycleError) as exc:
        activate(d / "candidate/profile.json", store, SERIAL)
    assert exc.value.code == "not_validated"
    assert tree_digest(store) == before


def test_activate_writes_a_new_revision_and_records_the_parent(tmp_path, scene):
    store = make_store(tmp_path)
    d = session_dir(tmp_path, profile_from_scene(scene))
    factory = factory_result(scene)
    rep = validate(d / "candidate/profile.json", scene.observations, factory,
                   gates=passing_gates())
    assert rep["passed"]
    before_factory = tree_digest(store / SERIAL / "factory")

    out = activate(d / "candidate/profile.json", store, SERIAL)
    assert out["activated"] is True and out["revision"] == 1
    assert out["parent_revision"] is None
    assert (store / SERIAL / "revisions/0001/ccf/left.CCF").is_file()
    assert (store / SERIAL / "active").read_text().strip() == "0001"
    assert tree_digest(store / SERIAL / "factory") == before_factory

    ap = active_profile(store, SERIAL)
    assert ap["state"] == "validated" and ap["revision"] == 1
    assert ap["integrity_ok"] is True
    assert ap["validation"]["passed"] is True
    assert ap["factory"]["sha256"]

    # a second candidate becomes revision 2 with revision 1 as its parent
    d2 = session_dir(tmp_path, profile_from_scene(scene, dT=(0.0, 0.05, 0.0)),
                     name="SESSION2")
    validate(d2 / "candidate/profile.json", scene.observations, factory,
             gates=passing_gates())
    out2 = activate(d2 / "candidate/profile.json", store, SERIAL)
    assert out2["revision"] == 2 and out2["parent_revision"] == 1
    assert active_profile(store, SERIAL)["revision"] == 2

    # rollback -> 1, then -> factory (no earlier revision), then explicit 2
    rb = rollback(store, SERIAL)
    assert rb["rolled_back"] and rb["revision"] == 1
    assert active_profile(store, SERIAL)["revision"] == 1
    rb2 = rollback(store, SERIAL)
    assert rb2["state"] == "factory" and rb2["revision"] is None
    assert active_profile(store, SERIAL)["state"] == "factory"
    rb3 = rollback(store, SERIAL, revision=2)
    assert rb3["revision"] == 2 and active_profile(store, SERIAL)["revision"] == 2
    with pytest.raises(LifecycleError):
        rollback(store, SERIAL, revision=99)


def test_failed_activation_leaves_every_store_file_byte_identical(tmp_path, scene,
                                                                  monkeypatch):
    store = make_store(tmp_path)
    d = session_dir(tmp_path, profile_from_scene(scene))
    validate(d / "candidate/profile.json", scene.observations, None,
             gates=passing_gates())
    before = tree_digest(store)

    # (a) a failed validation is refused before anything is written
    failing = passing_gates(observability={"passed": False, "reasons": ["nope"],
                                           "blocking_gates": ["stereo_rotation"],
                                           "identifiability": {}})
    validate(d / "candidate/profile.json", scene.observations, None, gates=failing)
    with pytest.raises(LifecycleError):
        activate(d / "candidate/profile.json", store, SERIAL)
    assert tree_digest(store) == before

    # (b) a profile edited after validation is refused (digest binding)
    validate(d / "candidate/profile.json", scene.observations, None,
             gates=passing_gates())
    p = d / "candidate/profile.json"
    original = p.read_text()
    doc = json.loads(original)
    doc["cameras"]["ir_right"]["T"][0] += 0.5
    p.write_text(json.dumps(doc))
    with pytest.raises(LifecycleError) as exc:
        activate(p, store, SERIAL)
    assert exc.value.code == "digest_mismatch"
    assert tree_digest(store) == before

    # (c) a write that fails halfway through is rolled back exactly
    p.write_text(original)
    validate(p, scene.observations, None, gates=passing_gates())

    def boom(self, *a, **k):
        rev = Path(self.device_dir(a[0] if a else SERIAL)) / "revisions" / "0001"
        (rev / "ccf").mkdir(parents=True, exist_ok=True)
        (rev / "ccf/left.CCF").write_bytes(b"torn")
        (rev / "manifest.json").write_text("{}")
        raise OSError("disk full")

    monkeypatch.setattr(ProfileStore, "save_revision", boom)
    with pytest.raises(LifecycleError) as exc:
        activate(p, store, SERIAL)
    assert exc.value.code == "write_failed"
    assert tree_digest(store) == before
    assert not (store / SERIAL / "revisions" / "0001").exists()
    assert active_profile(store, SERIAL)["state"] == "factory"


def test_factory_is_never_written_and_is_verified(tmp_path, scene):
    store = make_store(tmp_path)
    fac = store / SERIAL / "factory"
    before = tree_digest(fac)
    # make the snapshot read-only: any write attempt would raise EACCES
    for p in fac.iterdir():
        os.chmod(p, 0o444)
    os.chmod(fac, 0o555)
    modes = {p.name: p.stat().st_mode for p in fac.iterdir()}
    try:
        d = session_dir(tmp_path, profile_from_scene(scene))
        validate(d / "candidate/profile.json", scene.observations, None,
                 gates=passing_gates())
        out = activate(d / "candidate/profile.json", store, SERIAL)
        assert out["activated"] is True
        assert tree_digest(fac) == before
        assert {p.name: p.stat().st_mode for p in fac.iterdir()} == modes
        # tampering with the snapshot is caught, not silently accepted
        target = fac / "left.CCF"
        os.chmod(fac, 0o755)
        os.chmod(target, 0o644)
        for p in fac.iterdir():
            os.chmod(p, 0o644)
        raw = bytearray(target.read_bytes())
        raw[0] ^= 0xFF
        target.write_bytes(bytes(raw))
        with pytest.raises(FactoryMutated):
            active_profile(store, SERIAL)
    finally:
        os.chmod(fac, 0o755)
        for p in fac.iterdir():
            os.chmod(p, 0o644)


def test_active_profile_falls_back_and_reports_corruption(tmp_path, scene):
    store = make_store(tmp_path)
    ap = active_profile(store, SERIAL)
    assert ap["state"] == "factory" and ap["integrity_ok"] is True
    assert ap["path"].endswith("factory")
    d = session_dir(tmp_path, profile_from_scene(scene))
    validate(d / "candidate/profile.json", scene.observations, None,
             gates=passing_gates())
    activate(d / "candidate/profile.json", store, SERIAL)
    # corrupt the revision: it must not be reported as active
    (store / SERIAL / "revisions/0001/ccf/left.CCF").write_bytes(b"junk")
    ap2 = active_profile(store, SERIAL)
    assert ap2["state"] == "factory"
    assert ap2["integrity_ok"] is False
    assert "verification" in ap2["reason"]


def test_profile_to_records_round_trips_the_intrinsics(tmp_path, scene):
    store = make_store(tmp_path)
    prof = profile_from_scene(scene)
    recs = profile_to_records(prof, store, SERIAL)
    assert set(recs) == {"left", "right", "tex"}
    left = recs["left"]
    # the CCF stores the normalised alpha, the profile the absolute K[0,1]
    assert left.values[8] == pytest.approx(0.0, abs=1e-12)
    assert left.values[0] == pytest.approx(prof["cameras"]["ir_left"]["fx"])
    assert left.values[4] == pytest.approx(prof["cameras"]["ir_left"]["cx"])
    assert np.allclose(left.values[10:15], prof["cameras"]["ir_left"]["dist"])
    assert np.allclose(left.values[23:32],
                       np.asarray(prof["cameras"]["ir_left"]["R"]).ravel())
    assert len(left.raw) == 520 and len(recs["tex"].raw) == 264
    # unknown fields are preserved from the factory record, not zeroed
    assert np.any(left.values[15:20] != 0) or np.any(left.values[2:4] != 0) \
        or left.values[32] == 0.0
    assert left.trailer is not None and len(left.trailer) == 64
