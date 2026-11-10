"""Offline tests for the guided one-session calibration recorder.

The tests use three kinds of evidence:

* the private vendor 25-pose corpus (skipped cleanly when absent) for the real
  detector, the real 1280x1024 frames and the real 41-marker plate;
* synthetic PGM frame sets with *declared* detections (exact replay of known
  observations) so the session/retention/completeness logic is tested without
  depending on a synthetic-image detector;
* ``calib.simloop`` for a closed-loop operator simulation.

No test opens a scanner, writes to a device, or modifies the source images.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "calibsession"))

import calibrate_session as cs  # noqa: E402
from calib.simloop import observations_from_pose, render_frame, run_closed_loop  # noqa: E402
from calib.synth import make_scene  # noqa: E402

VENDOR = Path("<private-inputs>/vendor-calib-rapid")
FACTORY = REPO / "sources" / "calibration" / "res_Einscan-E10_200x150"

HAS_VENDOR = VENDOR.is_dir() and all(
    (VENDOR / f"image{side}1.bmp").is_file()
    for side in ("Left", "Right", "Tex"))
needs_vendor = pytest.mark.skipif(
    not HAS_VENDOR, reason="private vendor calibration corpus is absent")
needs_factory = pytest.mark.skipif(
    not FACTORY.is_dir(), reason="shipped factory CCFs are absent")

#: quality policy for the tiny declared-detection frames: the pixels carry no
#: plate (the detections are declared), so only the geometric gates matter.
TINY_QUALITY = {
    "min_markers": 16, "min_stereo_markers": 16, "min_big_markers": 3,
    "min_plate_area_frac": 0.0, "max_plate_area_frac": 1e9,
    "max_saturation_frac": 1.0,
    "min_contrast": {"left": 0.0, "right": 0.0, "tex": 0.0},
    "min_focus": {"left": 0.0, "right": 0.0, "tex": 0.0},
}

TINY_RULES = {
    "min_accepted": 12, "min_train": 8, "min_heldout": 5,
    "distance_bins_min": 2, "tilt_both_signs": False,
    "quadrant_fraction_min": 0.0,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_pgm(path: Path, image: np.ndarray) -> None:
    h, w = image.shape
    path.write_bytes(b"P5\n%d %d\n255\n" % (w, h) + image.astype(np.uint8).tobytes())


def _declared_replay(root: Path, n_sets: int = 18, seed: int = 41,
                     omit: tuple[str, ...] = (), tiny: bool = True) -> tuple:
    """Create synthetic PGM frames + a replay manifest with exact detections."""
    root.mkdir(parents=True, exist_ok=True)
    scene = make_scene(n_views=n_sets, seed=seed,
                       distance_range=(300.0, 540.0), tilt_max_deg=22.0)
    rng = np.random.default_rng(seed + 1)
    sets = []
    for i, (R, t) in enumerate(scene.views):
        d = root / f"set_{i:02d}"
        d.mkdir(exist_ok=True)
        paths = {}
        for cam_idx, cam in enumerate(("left", "right", "tex")):
            if cam in omit:
                continue
            img = (rng.integers(0, 256, (48, 64), dtype=np.uint8)
                   if tiny else np.full((1024, 1280), 32, np.uint8))
            p = d / f"frame_{i:06d}_cam{cam_idx}.pgm"
            _write_pgm(p, img)
            paths[cam] = str(p)
        obs = observations_from_pose(scene, R, t, view=i, noise_px=0.1, rng=rng)
        det = {o.camera: {f"{float(u)},{float(v)}": [float(x), float(y)]
                          for (u, v), (x, y) in o.uv.items()}
               for o in obs if o.camera not in omit}
        sets.append({
            "label": f"syn_{i:02d}", "timestamp": float(i),
            "camera_timestamps": {"left": float(i),
                                  "right": float(i) + 0.0002,
                                  "tex": float(i) + 0.0001},
            "cameras": paths, "detections": det,
        })
    manifest = root / "replay.json"
    manifest.write_text(json.dumps({"sets": sets}, indent=1))
    return scene, manifest


def _probe_triplet(frames_dir: Path, idx: int, pose: int) -> None:
    """Write one probe-style triplet for vendor pose ``pose``.

    Hardware mapping (recon/src/frame_set.cpp): cam0 = right IR rotated 180,
    cam1 = left IR as calibrated, cam2 = colour rotated 180.  The vendor files
    are already in calibration orientation, so the test rotates two of them.
    """
    import cv2
    frames_dir.mkdir(parents=True, exist_ok=True)
    for cam_idx, side, rotate in ((0, "Right", True), (1, "Left", False),
                                  (2, "Tex", True)):
        img = cv2.imread(str(VENDOR / f"image{side}{pose}.bmp"),
                         cv2.IMREAD_GRAYSCALE)
        assert img is not None
        if rotate:
            img = cv2.rotate(img, cv2.ROTATE_180)
        _write_pgm(frames_dir / f"frame_{idx:06d}_cam{cam_idx}.pgm", img)


def _run_replay(root: Path, manifest: Path, scene, *, rules=None,
                quality=None, out: str = "session", **kwargs) -> dict:
    sets, info = cs.load_replay(manifest)
    rec = cs.SessionRecorder(
        root / out, source_kind="replay", source_info=info,
        rules={**cs.DEFAULT_RULES, **TINY_RULES, **(rules or {})},
        quality={**TINY_QUALITY, **(quality or {})},
        target=int(kwargs.pop("target", 24)),
        localize_method=None, dwell_mode="assume",
        init_cameras=scene.cameras, **kwargs)
    for item in sets:
        rec.process(item)
    return rec.finish()


def _mutate(manifest: dict) -> dict:
    return copy.deepcopy(manifest)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tiny_session(tmp_path_factory):
    root = tmp_path_factory.mktemp("tiny_session")
    scene, manifest = _declared_replay(root / "inputs", n_sets=18)
    report = _run_replay(root, manifest, scene)
    return {"root": root, "report": report,
            "manifest": report["manifest"], "scene": scene,
            "manifest_path": manifest}


@pytest.fixture(scope="module")
def vendor_session(tmp_path_factory):
    if not VENDOR.exists() or not FACTORY.exists():
        pytest.skip("vendor corpus / factory calibration not available")
    root = tmp_path_factory.mktemp("vendor_session")
    sets, info = cs.discover_vendor_corpus(VENDOR)
    rec = cs.SessionRecorder(
        root / "session", source_kind="vendor-corpus", source_info=info,
        rules=dict(cs.DEFAULT_RULES), target=24,
        localize_method=None, dwell_mode="assume",
        factory_dir=FACTORY, cache=root / "detections.json", verbose=False)
    for item in sets:
        rec.process(item)
    report = rec.finish()
    return {"root": root, "report": report, "manifest": report["manifest"],
            "cache": root / "detections.json", "sets": sets}


# ---------------------------------------------------------------------------
# tests: input grouping
# ---------------------------------------------------------------------------

@needs_vendor
def test_vendor_grouping_associates_25_three_camera_sets(vendor_session):
    sets, info = cs.discover_vendor_corpus(VENDOR)
    assert len(sets) == 25
    assert info.n_sets == 25
    assert info.cameras == {"left": 25, "right": 25, "tex": 25}
    assert info.missing_by_camera == {"left": 0, "right": 0, "tex": 0}
    for i, item in enumerate(sets, start=1):
        assert item.label == f"vendor_{i:02d}"
        assert item.paths["left"].name == f"imageLeft{i}.bmp"
        assert item.paths["right"].name == f"imageRight{i}.bmp"
        assert item.paths["tex"].name == f"imageTex{i}.bmp"
        assert item.sync_source == "grouped-files"
        assert item.rotate_180 == {}       # vendor files are already oriented


def test_replay_rejects_duplicate_labels_and_missing_files(tmp_path):
    img = np.zeros((8, 8), np.uint8)
    p = tmp_path / "a.pgm"
    _write_pgm(p, img)
    good = {"label": "x", "cameras": {"left": str(p), "right": str(p),
                                      "tex": str(p)}}
    duplicate = tmp_path / "dup.json"
    duplicate.write_text(json.dumps({"sets": [good, dict(good)]}))
    with pytest.raises(cs.CalibSessionError, match="duplicate set label"):
        cs.load_replay(duplicate)
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"sets": [
        {"label": "y", "cameras": {"left": str(tmp_path / "nope.pgm")}}]}))
    with pytest.raises(cs.CalibSessionError, match="file not found"):
        cs.load_replay(missing)


# ---------------------------------------------------------------------------
# tests: manifest round-trip and verification
# ---------------------------------------------------------------------------

def test_manifest_roundtrip_and_completeness(tiny_session):
    man = tiny_session["manifest"]
    assert man["schema"] == cs.SCHEMA
    assert len(man["sets"]) == 18
    counts = man["counts"]
    assert counts["accepted"] >= 12
    assert counts["heldout"] >= 5
    assert counts["accepted"] == counts["train"] + counts["heldout"]
    assert len(man["accepted_ids"]) == counts["accepted"]
    assert len(man["heldout_ids"]) == counts["heldout"]
    assert set(man["train_ids"]).isdisjoint(man["heldout_ids"])
    assert man["completeness"]["ok"], man["completeness"]["missing"]
    assert len(man["content_digest"]) == 64
    # the manifest on disk round-trips exactly
    disk = json.loads((tiny_session["root"] / "session" / "manifest.json")
                      .read_text())
    assert disk["content_digest"] == man["content_digest"]
    assert disk["sets"] == man["sets"]


def test_verify_detects_corrupted_copy(tiny_session, tmp_path):
    source = tiny_session["root"] / "session"
    copy_dir = tmp_path / "corrupted"
    shutil.copytree(source, copy_dir)
    assert cs.verify_session(copy_dir)["ok"] is True
    stored = sorted(copy_dir.glob("sets/*/*.pgm"))[0]
    raw = bytearray(stored.read_bytes())
    raw[-1] ^= 0x01
    stored.write_bytes(raw)
    result = cs.verify_session(copy_dir)
    assert result["ok"] is False
    assert any("sha256 mismatch" in p for p in result["problems"])


def test_verify_detects_truncated_copy(tiny_session, tmp_path):
    source = tiny_session["root"] / "session"
    copy_dir = tmp_path / "truncated"
    shutil.copytree(source, copy_dir)
    stored = sorted(copy_dir.glob("sets/*/*.pgm"))[0]
    raw = stored.read_bytes()
    stored.write_bytes(raw[:len(raw) // 2])
    result = cs.verify_session(copy_dir)
    assert result["ok"] is False
    assert any("size" in p or "sha256" in p or "decode" in p
               for p in result["problems"])


# ---------------------------------------------------------------------------
# tests: completeness failure cases
# ---------------------------------------------------------------------------

def test_completeness_missing_tex_and_right(tiny_session):
    man = _mutate(tiny_session["manifest"])
    accepted = [s for s in man["sets"] if s["accepted"]]
    accepted[0]["files"]["tex"] = {
        "stored": None, "decoded": False, "error": "missing camera image"}
    res = cs.check_completeness(man, rules=man["rules"])
    assert not res["ok"]
    assert any("tex" in m for m in res["missing"])

    man2 = _mutate(tiny_session["manifest"])
    [s for s in man2["sets"] if s["accepted"]][1]["files"]["right"] = {
        "stored": None, "decoded": False, "error": "missing camera image"}
    res2 = cs.check_completeness(man2, rules=man2["rules"])
    assert not res2["ok"]
    assert any("right" in m for m in res2["missing"])


def test_completeness_wrong_image_size(tiny_session):
    man = _mutate(tiny_session["manifest"])
    accepted = [s for s in man["sets"] if s["accepted"]]
    accepted[0]["files"]["left"]["width"] = 999
    res = cs.check_completeness(man, rules=man["rules"])
    assert not res["ok"]
    assert any("inconsistent sizes" in m for m in res["missing"])


def test_completeness_too_few_heldout(tiny_session):
    man = _mutate(tiny_session["manifest"])
    n = 0
    for s in man["sets"]:
        if s["accepted"] and s["split"] == "heldout" and n < 2:
            s["split"] = "train"
            n += 1
    res = cs.check_completeness(man, rules=man["rules"])
    assert not res["ok"]
    assert any("held-out" in m for m in res["missing"])


def test_session_without_colour_fails_clearly(tmp_path):
    scene, manifest = _declared_replay(tmp_path / "inputs", n_sets=8,
                                       omit=("tex",))
    report = _run_replay(tmp_path, manifest, scene,
                         rules={"min_heldout": 2})
    assert report["colour"]["frames_present"] == 0
    assert report["colour"]["hasTexture"] is False
    assert report["completeness"]["ok"] is False
    joined = " | ".join(report["completeness"]["missing"])
    assert "colour camera decodable in 0/8" in joined


# ---------------------------------------------------------------------------
# tests: vendor corpus as a one-session capture
# ---------------------------------------------------------------------------

@needs_vendor
@needs_factory
def test_vendor_session_accepts_a_diverse_subset(vendor_session):
    man = vendor_session["manifest"]
    counts = man["counts"]
    assert counts["candidates"] == 25
    assert counts["accepted"] >= 15, counts          # sensible subset, not all
    assert counts["accepted"] < 25                   # not a rubber stamp
    assert counts["heldout"] >= 5
    assert counts["train"] == counts["accepted"] - counts["heldout"]
    assert man["completeness"]["ok"], man["completeness"]["missing"]
    # every accepted set has all three cameras and a decodable frame
    for s in man["sets"]:
        if not s["accepted"]:
            continue
        assert s["split"] in ("train", "heldout")
        for cam in ("left", "right", "tex"):
            assert s["files"][cam]["decoded"] is True
            assert s["files"][cam]["copy_verified"] is True
            assert s["files"][cam]["sha256"] == s["files"][cam]["copy_sha256"]
    # the known mis-assigned vendor view (set 9) is rejected by the stereo gate
    by_label = {s["label"]: s for s in man["sets"]}
    assert by_label["vendor_09"]["accepted"] is False
    assert any("stereo pose inconsistent" in r
               for r in by_label["vendor_09"]["reasons"])
    # held-out views are not in the final training fit
    final = man["final_solve"]["info"]
    assert final["n_train_views"] == counts["train"]
    assert all(s["split"] == "heldout" for s in man["sets"]
               if s["view_id"] in man["heldout_ids"])
    assert final["factory_rig_check"]["orientation_ok"] is True
    # verify the stored session
    assert cs.verify_session(vendor_session["root"] / "session")["ok"] is True


@needs_vendor
@needs_factory
def test_vendor_colour_participation_and_hastexture(vendor_session):
    man = vendor_session["manifest"]
    colour = man["colour"]
    assert colour["frames_present"] == 25
    assert colour["frames_decoded"] == 25
    assert colour["accepted_with_tex_markers"] == man["counts"]["accepted"]
    assert colour["participation_frac"] == 1.0
    assert colour["hasTexture"] is True
    assert colour["profile_state"] == "present"
    # the colour profile is a factory initialiser, not proof of accuracy
    cons = colour.get("factory_consistency")
    assert cons is None or "delta_translation_mm" in cons


@needs_vendor
@needs_factory
def test_vendor_session_is_deterministic(vendor_session, tmp_path):
    root = vendor_session["root"]
    sets, info = cs.discover_vendor_corpus(VENDOR)
    rec = cs.SessionRecorder(
        tmp_path / "rerun", source_kind="vendor-corpus", source_info=info,
        rules=dict(cs.DEFAULT_RULES), target=24,
        localize_method=None, dwell_mode="assume", factory_dir=FACTORY,
        cache=root / "detections.json", verbose=False)
    for item in sets:
        rec.process(item)
    report = rec.finish()
    assert report["manifest"]["content_digest"] == \
        vendor_session["manifest"]["content_digest"]
    assert report["manifest"]["accepted_ids"] == \
        vendor_session["manifest"]["accepted_ids"]
    assert report["manifest"]["heldout_ids"] == \
        vendor_session["manifest"]["heldout_ids"]
    assert report["completeness"]["ok"]


@needs_vendor
@needs_factory
def test_stale_colour_profile_does_not_block_geometry(vendor_session, tmp_path):
    stale = tmp_path / "factory_stale"
    stale.mkdir()
    (stale / "LeftCCF.txt").write_bytes((FACTORY / "LeftCCF.txt").read_bytes())
    (stale / "RightCCF.txt").write_bytes((FACTORY / "RightCCF.txt").read_bytes())
    # a left record masquerading as the colour camera: parseable but stale
    (stale / "TexCCF.txt").write_bytes((FACTORY / "LeftCCF.txt").read_bytes())
    sets, info = cs.discover_vendor_corpus(VENDOR)
    rec = cs.SessionRecorder(
        tmp_path / "session", source_kind="vendor-corpus", source_info=info,
        rules=dict(cs.DEFAULT_RULES), target=24,
        localize_method=None, dwell_mode="assume", factory_dir=stale,
        cache=vendor_session["root"] / "detections.json", verbose=False)
    for item in sets:
        rec.process(item)
    report = rec.finish()
    colour = report["colour"]
    assert colour["profile_state"] == "stale"
    assert colour["hasTexture"] is True
    assert report["final"]["ok"] is True
    info = report["final"]["info"]
    assert info["baseline_mm"] > 150.0
    assert report["manifest"]["completeness"]["ok"], \
        report["manifest"]["completeness"]["missing"]
    # the new revision still must not be activated
    assert report["profile"]["activated"] is False


# ---------------------------------------------------------------------------
# tests: probe-style frames (mapping + orientation) and sequential dwell
# ---------------------------------------------------------------------------

def test_probe_mapping_constants_match_the_hardware_reference():
    # recon/src/frame_set.cpp: kStreamRightIr = 0, kStreamLeftIr = 1,
    # kStreamColor = 2, and calibration_image() rotates streams 0 and 2.
    assert cs.DEFAULT_CAM_MAP == {0: "right", 1: "left", 2: "tex"}
    assert cs.PROBE_ROTATED_CAMERAS == ("right", "tex")


@needs_vendor
@needs_factory
def test_probe_triplet_is_interpreted_in_calibration_orientation(tmp_path):
    import cv2
    from calib.ccf import load as load_ccf, relative_pose
    from calib.calibrate import CameraIntrinsics

    frames_dir = tmp_path / "probe"
    _probe_triplet(frames_dir, 0, 1)
    sets, info = cs.discover_frames(frames_dir)
    assert len(sets) == 1
    assert info.extra["cam_map"] == {"0": "right", "1": "left", "2": "tex"}
    assert info.extra["rotation_180"] == ["right", "tex"]
    item = sets[0]
    assert item.paths["right"].name == "frame_000000_cam0.pgm"
    assert item.paths["left"].name == "frame_000000_cam1.pgm"
    assert item.paths["tex"].name == "frame_000000_cam2.pgm"

    loaded = {c: cs.load_image(c, item.paths[c],
                               rotate_180=item.rotate_180.get(c, False))
              for c in ("left", "right", "tex")}
    for cam, side in (("left", "Left"), ("right", "Right"), ("tex", "Tex")):
        original = cv2.imread(str(VENDOR / f"image{side}1.bmp"),
                              cv2.IMREAD_GRAYSCALE)
        assert np.array_equal(loaded[cam].gray, original), cam
        # the retained bytes are the probe bytes, not a re-encoded image
        assert loaded[cam].sha256 == cs.sha256_bytes(
            item.paths[cam].read_bytes())
        assert loaded[cam].facts["rotated_180"] is (cam != "left")
        grid = cs.detect_image(loaded[cam])
        assert grid is not None and grid.n_lattice >= 30

    # geometry: with the correct mapping the left/right PnP reproduces the
    # asset's stereo layout; a swap/no-rotation changes the relative rotation
    factory = {n: load_ccf(FACTORY / f"{n}.txt")
               for n in ("LeftCCF", "RightCCF", "TexCCF")}
    cams = {c: CameraIntrinsics.from_ccf(factory[n]) for c, n in
            (("left", "LeftCCF"), ("right", "RightCCF"), ("tex", "TexCCF"))}
    grids = {c: cs.to_observation(c, cs.detect_image(loaded[c]))
             for c in ("left", "right", "tex")}
    m = cs.pnp_view_metrics(grids, cams, 28.0)
    assert "stereo" in m
    Rf, tf = relative_pose(factory["LeftCCF"], factory["RightCCF"])
    R = np.asarray(m["stereo"]["R"], float)
    t = np.asarray(m["stereo"]["t"], float)
    baseline = float(np.linalg.norm(t))
    delta = float(np.degrees(np.arccos(np.clip(
        (np.trace(R @ Rf.T) - 1) / 2, -1, 1))))
    assert 140.0 < baseline < 185.0
    assert delta < 3.0, delta


@needs_vendor
@needs_factory
def test_frames_triplets_and_sequential_dwell(tmp_path):
    frames_dir = tmp_path / "frames"
    # six diverse vendor poses, each captured twice: the first frame after the
    # move fails the motion gate, the second is the stationary dwell keyframe.
    poses = (1, 3, 4, 5, 7, 8)
    idx = 0
    for pose in poses:
        for rep in range(2):
            _probe_triplet(frames_dir, idx, pose)
            idx += 1
    (frames_dir / "timestamps.json").write_text(json.dumps({
        str(i): {"left": float(i), "right": float(i) + 0.0003,
                 "tex": float(i) + 0.0001} for i in range(idx)}))
    sets, info = cs.discover_frames(frames_dir)
    assert len(sets) == idx
    assert info.sync_sidecar is not None
    assert all(s.camera_timestamps is not None for s in sets)
    assert sets[0].sync_source == "measured-sidecar"
    assert sets[0].rotate_180 == {"right": True, "tex": True}
    rules = dict(cs.DEFAULT_RULES)
    rules.update({"min_accepted": 4, "min_train": 2, "min_heldout": 2,
                  "distance_bins_min": 2, "tilt_both_signs": False,
                  "quadrant_fraction_min": 0.0})
    rec = cs.SessionRecorder(
        tmp_path / "session", source_kind="frames", source_info=info,
        rules=rules, target=12,
        holdout=cs.HoldoutPolicy(n_holdout=2, target=12, bootstrap_views=3),
        localize_method=None, dwell_mode="sequential", factory_dir=FACTORY,
        verbose=False)
    for item in sets:
        rec.process(item)
    report = rec.finish()
    man = report["manifest"]
    records = man["sets"]
    accepted = [s for s in records if s["accepted"]]
    assert len(accepted) == len(poses)
    # measured sync is recorded for every accepted set
    assert all(s["sync"]["state"] == "measured" for s in accepted)
    # the very first frame has no motion reference and is kept; its repeat is a
    # duplicate.  For every later pose: frame 1 = moving/rejected,
    # frame 2 = stationary dwell/accepted.
    assert records[0]["accepted"] and records[0]["dwell_state"] == "first-frame"
    assert not records[1]["accepted"]
    assert any("similar" in r for r in records[1]["reasons"])
    for k in range(1, len(poses)):
        first, second = records[2 * k], records[2 * k + 1]
        assert not first["accepted"], (k, first["reasons"])
        assert any("moving" in r for r in first["reasons"]), first["reasons"]
        assert first["dwell_state"] == "moving"
        assert second["accepted"], (k, second["reasons"])
        assert second["dwell_state"] == "stationary"
    # a repeated pose is not double-counted: each pose contributes once
    assert len({(round(s["pose"]["distance_mm"]), round(s["pose"]["tilt_deg"], 1))
                for s in accepted}) == len(poses)
    # every stored right/colour copy is the original probe byte stream
    for s in man["sets"]:
        for cam in ("right", "tex"):
            assert s["files"][cam]["rotated_180"] is True
            assert s["files"][cam]["sha256"] == s["files"][cam]["copy_sha256"]


@needs_vendor
@needs_factory
def test_frames_manifest_gives_trigger_group_sync(tmp_path):
    frames_dir = tmp_path / "frames"
    _probe_triplet(frames_dir, 0, 1)
    _probe_triplet(frames_dir, 1, 3)
    manifest = {
        "schema": "einstar-frames-v1",
        "camera_map": {"0": "right_ir", "1": "left_ir", "2": "color"},
        "complete": True, "groups_written": 2, "groups_incomplete": 0,
        "groups": [
            {"group": i, "device_group": 1000 + i,
             "host_time_ns": 5_000_000_000 + i * 90_000_000,
             "stream0": f"frame_{i:06d}_cam0.pgm",
             "stream1": f"frame_{i:06d}_cam1.pgm",
             "stream2": f"frame_{i:06d}_cam2.pgm",
             "frame_ids": {"0": 10 + i, "1": 20 + i, "2": 30 + i}}
            for i in range(2)],
    }
    (frames_dir / "frames_manifest.json").write_text(json.dumps(manifest))
    sets, info = cs.discover_frames(frames_dir)
    assert info.extra["frames_manifest"]["groups_written"] == 2
    assert all(s.sync_source == "device-trigger-group" for s in sets)
    assert [s.meta["device_group"] for s in sets] == [1000, 1001]
    assert sets[1].timestamp == (5_000_000_000 + 90_000_000) * 1e-9
    rules = dict(cs.DEFAULT_RULES)
    rules.update({"min_accepted": 1, "min_train": 1, "min_heldout": 1,
                  "distance_bins_min": 1, "tilt_both_signs": False,
                  "quadrant_fraction_min": 0.0})
    rec = cs.SessionRecorder(
        tmp_path / "session", source_kind="frames", source_info=info,
        rules=rules, target=12,
        holdout=cs.HoldoutPolicy(n_holdout=1, target=12, bootstrap_views=3),
        localize_method=None, dwell_mode="assume", factory_dir=FACTORY)
    for item in sets:
        rec.process(item)
    rec.finish()
    for r in rec.records:
        assert r.sync["state"] == "declared"
        assert "trigger group" in r.sync["source"]
        assert "device_group" in r.sync
    # an inconsistent camera map is a hard error, not a silent remap
    bad = dict(manifest)
    bad["camera_map"] = {"0": "left_ir", "1": "right_ir", "2": "color"}
    (frames_dir / "frames_manifest.json").write_text(json.dumps(bad))
    with pytest.raises(cs.CalibSessionError, match="camera_map"):
        cs.discover_frames(frames_dir)


@needs_vendor
@needs_factory
def test_missing_probe_rotation_is_detected(tmp_path):
    frames_dir = tmp_path / "probe"
    for i, pose in enumerate((1, 3, 4, 5, 7, 8)):
        _probe_triplet(frames_dir, i, pose)
    sets, info = cs.discover_frames(frames_dir)
    # simulate using files that are still in the probe orientation
    for item in sets:
        item.rotate_180 = {}
    rules = dict(cs.DEFAULT_RULES)
    rules.update({"min_accepted": 5, "min_train": 3, "min_heldout": 2,
                  "distance_bins_min": 2, "tilt_both_signs": False,
                  "quadrant_fraction_min": 0.0})
    rec = cs.SessionRecorder(
        tmp_path / "session", source_kind="frames", source_info=info,
        rules=rules, target=12,
        holdout=cs.HoldoutPolicy(n_holdout=2, target=12, bootstrap_views=3),
        localize_method=None, dwell_mode="assume", factory_dir=FACTORY,
        verbose=False)
    for item in sets:
        rec.process(item)
    report = rec.finish()
    frc = report["final"]["info"].get("factory_rig_check") or {}
    assert frc.get("orientation_ok") is False
    assert frc["right"]["delta_rotation_deg"] > 90.0
    assert report["completeness"]["ok"] is False
    assert any("orientation" in m for m in report["completeness"]["missing"])


# ---------------------------------------------------------------------------
# tests: closed-loop synthetic session
# ---------------------------------------------------------------------------

def test_synthetic_closed_loop_stops_sensibly():
    scene = make_scene(n_views=6, noise_px=0.1, seed=5)
    out = run_closed_loop(scene, mode="quick", max_views=6, render=False,
                          seed=0)
    assert out["n_accepted"] >= 1
    assert out["n_accepted"] <= out["n_captures"]
    assert len(out["timeline"]) <= 6
    assert "stop" in out and "checks" in out["stop"]
    # a bounded run, not an infinite acquisition loop
    assert out["stop"]["n_views"] == out["n_accepted"]
