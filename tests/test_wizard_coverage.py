"""Coverage gate tests: every dimension, the 1-D sweep rejection, guidance."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from tools.wizard import coverage
from tools.wizard.coverage import (CoverageSpec, DIMENSIONS, family_pose, measure,
                                   plate_normal, readiness, roll_deg, tilt_deg)

PRIVATE_SWEEP = Path.home() / "einstar-private/analysis/sweep-b/sweep_report.json"
FACTORY = Path.home() / "einstar-private/einstar-profiles/factory-e10.json"


# ---------------------------------------------------------------------------
# a deterministic, fully-covering synthetic session
# ---------------------------------------------------------------------------

def nested_quality(*, focus_l: float = 800.0, focus_r: float = 800.0,
                   sat_l: float = 0.001, sat_r: float = 0.001,
                   contrast_l: float = 60.0, contrast_r: float = 60.0,
                   active_l: float = 1.0, active_r: float = 1.0,
                   tex_focus: float = 150.0, tex_sat: float = 0.001,
                   tex_contrast: float = 17.0, active_tex: float = 1.0,
                   speed: float | None = 0.2, motion_deg: float | None = 0.05,
                   dt: float | None = 0.35, blur: float | None = 0.01,
                   passed: bool = True) -> dict:
    """The live wizard's nested ``FrameQuality`` metrics layout."""
    metrics = {
        "left.active": active_l, "left.focus": focus_l,
        "left.saturation": sat_l, "left.contrast": contrast_l,
        "right.active": active_r, "right.focus": focus_r,
        "right.saturation": sat_r, "right.contrast": contrast_r,
        "tex.active": active_tex, "tex.focus": tex_focus,
        "tex.saturation": tex_sat, "tex.contrast": tex_contrast,
        "stereo_markers": 40.0, "big_markers": 3.0, "pose_ok": 1.0,
    }
    if speed is not None:
        metrics["speed_mm_s"] = speed
        metrics["stationarity_speed_mm_s"] = speed
    if motion_deg is not None:
        metrics["motion_deg"] = motion_deg
    if dt is not None:
        metrics["dt_s"] = dt
        metrics["stationarity_window_s"] = dt
    if speed is not None and motion_deg is not None and dt:
        metrics["stationarity_rate_deg_s"] = motion_deg / dt
        metrics["stationary"] = bool(speed <= 3.0 and motion_deg / dt <= 1.5)
    if blur is not None:
        metrics["motion_blur_px"] = blur
    return {"passed": passed, "reasons": [], "metrics": metrics}


def _view(view_id: int, distance: float, tx: float, ty: float, roll: float,
          centre: tuple[float, float], t: float, *, n_markers: int = 40,
          shared: int = 40, quality: dict | None = None,
          artefact: float | None = None, dwell: bool = False) -> dict:
    R, tr = family_pose(distance, tx, ty, roll)
    pts = np.array([[centre[0] + 8 * (u - 3.5), centre[1] + 8 * (v - 2)]
                    for v in range(5) for u in range(8)], float)
    v = {
        "view_id": int(view_id),
        "pose": {"R": R, "t": tr},
        "n_markers": {"left": n_markers, "right": n_markers, "tex": n_markers},
        "quality": nested_quality() if quality is None else quality,
        "image_points": {"left": pts, "right": pts - [150.0, 0.0], "tex": pts},
        "shared_markers_ir": int(shared),
        "assignment_ok": True,
        "timestamp": float(t),
    }
    if artefact is not None:
        v["scale_artefact"] = {"length_mm": 100.0, "measured_mm": artefact}
    if dwell:
        v["dwell"] = True
    return v


#: image centre of each 3x3 region of a 1280x1024 frame
_REGION_CENTRE = {
    (r, c): (213.0 + 427.0 * c, 170.0 + 341.0 * r) for r in range(3) for c in range(3)
}


def good_views(dwell: int = 2) -> list[dict]:
    """A session that satisfies every dimension of the default spec.

    Each pose is captured ``dwell`` times 0.35 s apart (a real "hold still"),
    with a 2.5 s reposition between poses.  The nested quality record carries
    a real short-term stationarity observation.
    """
    views: list[dict] = []
    k = 0
    t = 0.0
    plan: list[tuple[int, int, tuple[int, int], float, float | None]] = []

    regions = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 0), (2, 1), (2, 2)]
    rolls = [45.0, 135.0, 225.0, 315.0]
    for i in range(12):
        plan.append((i % 3, i % 4, regions[i % 9], rolls[i % 4], None))
    for n, (band, quad, region, roll) in enumerate([
            (0, 0, (0, 0), 45.0), (1, 1, (0, 2), 135.0),
            (2, 2, (2, 0), 225.0), (0, 3, (2, 2), 315.0)]):
        plan.append((band, quad, region, roll, 100.0 + 0.05 * n))
    plan.append((1, 0, (1, 1), 45.0, 99.98))

    for band, quad, region, roll, artefact in plan:
        lo, hi = CoverageSpec().distance_bands_mm[band]
        d = 0.5 * (lo + hi)
        sx = -1.0 if quad in (2, 3) else 1.0
        sy = -1.0 if quad in (1, 3) else 1.0
        for _ in range(max(int(dwell), 1)):
            views.append(_view(k, d, sx * 18.0, sy * 18.0, roll,
                               _REGION_CENTRE[region], t, artefact=artefact))
            k += 1
            t += 0.35
        t += 2.5
    return views


# ---------------------------------------------------------------------------
# spec / geometry
# ---------------------------------------------------------------------------

def test_spec_defaults_match_architecture_section_6():
    s = CoverageSpec()
    assert s.distance_bands_mm == ((300.0, 380.0), (380.0, 460.0), (460.0, 560.0))
    assert s.min_views_per_distance_band == 3
    assert s.tilt_axes == "two"
    assert s.min_tilt_deg == 15.0
    assert s.min_views_per_tilt_quadrant == 2
    assert s.roll_bins == 4
    assert s.min_views_per_roll_bin == 1
    assert s.image_regions == 9
    assert s.min_shared_markers == 12
    assert s.min_colour_markers == 12
    assert s.require_colour is True
    assert s.max_saturation_frac == 0.05
    assert s.min_focus == 150.0
    # D7 thresholds must not be lowered to manufacture READY.
    assert s.stationary_speed_mm_s == 3.0
    assert s.stationary_rate_deg_s == 1.5
    assert s.min_stationary_views == 6


def test_family_pose_roundtrip():
    for tx, ty, roll in [(0, 0, 0), (20, -15, 90), (-30, 25, 180), (12, 18, 270)]:
        R, t = family_pose(420.0, tx, ty, roll)
        gx, gy = tilt_deg(R)
        assert gx == pytest.approx(tx, abs=1e-6)
        assert gy == pytest.approx(ty, abs=1e-6)
        assert roll_deg(R) == pytest.approx(roll % 360.0, abs=1e-6)
        assert np.allclose(np.linalg.norm(t), np.linalg.norm([t[0], t[1], 420.0]))
        assert plate_normal(R)[2] < 0


# ---------------------------------------------------------------------------
# measurement basics
# ---------------------------------------------------------------------------

def test_measure_reports_all_dimensions_with_hints():
    views = good_views()
    m = measure(views, stationarity_observations=views)
    assert set(DIMENSIONS) <= set(m)
    assert m["n_views"] == len(views) and m["n_dwell_views"] == 0
    for d in DIMENSIONS:
        node = m[d]
        for key in ("required", "measured", "satisfied", "deficit", "hint", "detail"):
            assert key in node, (d, key)
        assert isinstance(node["hint"], str) and len(node["hint"]) > 8
        assert node["hint"].endswith((".", "!", ")"))


def test_all_dimensions_satisfied_by_a_good_session():
    spec = CoverageSpec(require_scale_artefact=True)
    views = good_views()
    m = measure(views, spec, stationarity_observations=views)
    r = readiness(m)
    assert r["ready"] is True, r["reasons"]
    assert r["unsatisfied"] == [] and r["unsatisfied_dimensions"] == []
    for d in DIMENSIONS:
        assert m[d]["satisfied"] is True, (d, m[d]["hint"])
    assert sum(m["D1"]["measured"]["band_counts"]) == m["n_usable"]
    assert min(m["D1"]["measured"]["band_counts"]) >= 3
    assert m["D3"]["measured"]["n_bins_covered"] == 4
    assert min(m["D4"]["measured"]["corners"].values()) >= 1
    assert m["D4"]["measured"]["regions_visited"] >= 6
    assert m["D5"]["measured"]["min_shared"] == 40
    # accepted views are a subset of the stream; the stream has the dwells
    assert m["D7"]["measured"]["n_stationary_intervals"] >= 6
    assert m["checks"]["stationarity"]["satisfied"] is True


def _broken(kind: str) -> list[dict]:
    views = good_views()
    if kind == "distance":
        for v in views:
            v["pose"]["t"] = [0.0, 0.0, 420.0]
    elif kind == "tilt":
        for v in views:
            R0 = np.asarray(v["pose"]["R"], float)
            v["pose"]["R"], _ = family_pose(float(np.linalg.norm(v["pose"]["t"])),
                                            0.0, 0.0, roll_deg(R0))
    elif kind == "roll":
        for v in views:
            R0 = np.asarray(v["pose"]["R"], float)
            tx, ty = tilt_deg(R0)
            v["pose"]["R"], _ = family_pose(float(np.linalg.norm(v["pose"]["t"])),
                                            tx, ty, 30.0)
    elif kind == "regions":
        for v in views:
            v["image_points"]["left"] = _REGION_CENTRE[(1, 1)] + np.zeros((40, 2))
    elif kind == "stereo":
        for v in views:
            v["shared_markers_ir"] = 5
    elif kind == "colour":
        for v in views:
            v["n_markers"]["tex"] = 0
    elif kind == "quality":
        for v in views:
            v["quality"]["metrics"]["left.saturation"] = 0.2
            v["quality"]["metrics"]["right.saturation"] = 0.2
    elif kind == "scale":
        for v in views:
            v.pop("scale_artefact", None)
    else:                                            # pragma: no cover
        raise AssertionError(kind)
    return views


@pytest.mark.parametrize("kind,dimension,needle", [
    ("distance", "D1", "distance bands"),
    ("tilt", "D2", "second axis"),
    ("roll", "D3", "roll"),
    ("regions", "D4", "corner"),
    ("stereo", "D5", "stereo overlap"),
    ("colour", "D6", "colour"),
    ("quality", "D7", "quality"),
    ("scale", "D8", "artefact"),
])
def test_each_dimension_fails_when_its_requirement_is_removed(kind, dimension, needle):
    spec = CoverageSpec(require_scale_artefact=True)
    views = _broken(kind)
    m = measure(views, spec, stationarity_observations=views)
    r = readiness(m)
    assert m[dimension]["satisfied"] is False, m[dimension]
    assert dimension in r["unsatisfied_dimensions"]
    assert any(needle in label.lower() for label in r["unsatisfied"]), r["unsatisfied"]
    assert r["ready"] is False
    coupled = {"distance": {"D8"}}.get(kind, set())
    others = [d for d in DIMENSIONS if d != dimension and d not in coupled]
    assert all(m[d]["satisfied"] for d in others), \
        [(d, m[d]["hint"]) for d in others if not m[d]["satisfied"]]
    for reason in r["reasons"]:
        assert reason["deficit"] is not None
        assert reason["hint"]


# ---------------------------------------------------------------------------
# the 1-D sweep must fail
# ---------------------------------------------------------------------------

def _synthetic_sweep_views(n: int = 350) -> list[dict]:
    """350 poses generated from the *measured* sweep-b numbers."""
    x = np.concatenate([np.linspace(-99.8, -99.8, 94),
                        np.linspace(-99.8, 30.2, 209),
                        np.linspace(30.2, 30.2, n - 303)])
    z = np.concatenate([np.linspace(370.8, 388.2, 94),
                        np.linspace(370.8, 397.9, 209),
                        np.linspace(397.9, 397.9, n - 303)])
    tilt = np.concatenate([np.linspace(11.6, 19.5, 94),
                           np.linspace(19.5, 5.4, 209),
                           np.linspace(5.4, 5.4, n - 303)])
    u = np.linspace(326.0, 710.0, n)
    v = np.linspace(441.0, 458.0, n)
    out = []
    for i in range(n):
        tx = -tilt[i] * (10.6 / math.hypot(10.6, 6.0))
        ty = -tilt[i] * (6.0 / math.hypot(10.6, 6.0))
        roll = -178.0 + 1.2 * math.sin(i * 0.37)
        R, _ = family_pose(1.0, tx, ty, roll)
        t = np.array([x[i], -28.7 + 1.3 * math.sin(i * 0.11), z[i]], float)
        pts = np.array([[u[i] + 74.0 * (uu - 3.5), v[i] + 46.0 * (vv - 2)]
                        for vv in range(5) for uu in range(8)], float)
        out.append({
            "view_id": i, "pose": {"R": R, "t": t},
            "n_markers": {"left": 40, "right": 40, "tex": 0},
            "quality": {"mean": 35.0, "saturation_frac": 0.0011, "focus": 809.0},
            "image_points": {"left": pts, "right": pts - [150.0, 0.0],
                             "tex": np.zeros((0, 2))},
            "shared_markers_ir": 40, "assignment_ok": True,
            "timestamp": 0.0997 * i,
        })
    return out


def _sweep_views() -> tuple[list[dict], str]:
    if PRIVATE_SWEEP.is_file():
        try:
            from tools.wizard.observability_cli import sweep_accepted_views
            views = sweep_accepted_views(PRIVATE_SWEEP)
            if len(views) >= 300:
                return views, "sweep-b report"
        except Exception:                          # pragma: no cover - defensive
            pass
    return _synthetic_sweep_views(), "synthesised from bring-up numbers"


def test_one_axis_sweep_is_not_ready_and_names_what_is_missing():
    views, origin = _sweep_views()
    assert len(views) == 350, origin
    m = measure(views, CoverageSpec(require_colour=False))
    r = readiness(m)
    assert r["ready"] is False, origin
    labels = " | ".join(r["unsatisfied"]).lower()
    for needle in ("distance band", "second axis", "roll", "corner"):
        assert needle in labels, (origin, r["unsatisfied"])
    assert m["D1"]["measured"]["band_counts"].count(0) >= 2
    assert m["D1"]["measured"]["distance_mm"]["span"] < 30.0
    assert m["D3"]["measured"]["n_bins_covered"] == 1
    assert m["D4"]["measured"]["regions_visited"] <= 3
    assert all(c == 0 for c in m["D4"]["measured"]["corners"].values())
    assert m["D2"]["measured"]["tilt_y"]["positive"] == 0
    assert m["D2"]["measured"]["tilt_y"]["negative"] == 0
    assert m["D5"]["satisfied"] is True
    assert "not ready" in r["summary"] and "D1" in r["summary"]


# ---------------------------------------------------------------------------
# D7 quality path
# ---------------------------------------------------------------------------

def test_nested_quality_uses_actual_per_camera_metrics():
    v = good_views()[0]
    v["quality"] = nested_quality(focus_l=100.0, sat_r=0.2)
    m = measure([v], stationarity_observations=[v])
    q = m["checks"]["quality"]
    assert q["satisfied"] is False
    assert q["measured"]["focus_min"] == 100.0
    assert q["measured"]["saturation_max"] == 0.2
    reasons = " | ".join(m["D7"]["detail"]["quality"][0]["reasons"])
    assert "left" in reasons and "right" in reasons
    assert q["measured"]["quality_source_counts"] == {"nested_metrics": 1}


def test_tex_is_reported_but_not_gated():
    v = good_views()[0]
    v["quality"] = nested_quality(tex_focus=5.0, tex_sat=0.5)
    m = measure([v], stationarity_observations=[v])
    q = m["checks"]["quality"]
    assert q["satisfied"] is True
    row = m["D7"]["detail"]["quality"][0]
    assert row["tex_focus"] == 5.0 and row["tex_saturation"] == 0.5
    assert q["measured"]["tex_note"]


def test_inactive_camera_metrics_are_not_required():
    v = good_views()[0]
    q = nested_quality(active_r=0.0)
    del q["metrics"]["right.focus"]
    del q["metrics"]["right.saturation"]
    v["quality"] = q
    m = measure([v], stationarity_observations=[v])
    check = m["checks"]["quality"]
    assert check["satisfied"] is True
    assert check["measured"]["missing_metrics"] == []
    assert m["D7"]["detail"]["quality"][0]["active_cameras"] == ["left"]


def test_missing_quality_metrics_never_pass():
    v = good_views()[0]
    v["quality"] = {"passed": True,
                    "metrics": {"left.active": 1.0, "right.active": 1.0}}
    m = measure([v], stationarity_observations=[v])
    q = m["checks"]["quality"]
    assert q["satisfied"] is False
    assert set(q["measured"]["missing_metrics"]) >= {
        "left.focus", "left.saturation", "right.focus", "right.saturation"}
    assert m["D7"]["satisfied"] is False
    # no quality record at all is also a failure, not a pass
    v2 = good_views()[1]
    v2.pop("quality")
    m2 = measure([v2], stationarity_observations=[v2])
    q2 = m2["checks"]["quality"]
    assert q2["satisfied"] is False
    assert q2["measured"]["quality_source_counts"] == {"missing": 1}


def test_legacy_flat_quality_is_read_when_recorded():
    v = good_views()[0]
    v["quality"] = {"focus": 500.0, "saturation_frac": 0.01}
    m = measure([v], stationarity_observations=[v])
    q = m["checks"]["quality"]
    assert q["satisfied"] is True
    assert q["measured"]["quality_source_counts"] == {"legacy_flat": 1}
    # a flat record missing saturation is not silently accepted
    v2 = good_views()[1]
    v2["quality"] = {"focus": 500.0}
    m2 = measure([v2], stationarity_observations=[v2])
    assert m2["checks"]["quality"]["satisfied"] is False
    assert "saturation_frac" in m2["checks"]["quality"]["measured"]["missing_metrics"]


# ---------------------------------------------------------------------------
# D7 stationarity path
# ---------------------------------------------------------------------------

def _stationary_stream(n: int = 8, *, speed: float = 0.2,
                       motion_deg: float = 0.05, dt: float = 0.5,
                       start: float = 0.0, step: float = 2.0) -> list[dict]:
    return [{"view_id": f"stream-{i}", "timestamp": start + i * step,
             "quality": nested_quality(speed=speed, motion_deg=motion_deg,
                                       dt=dt)}
            for i in range(n)]


def _dwell_stream(episodes: int, *, frames: int = 3, fps: float = 10.0,
                  speed: float = 0.2, motion_deg: float = 0.05,
                  gap: float = 2.0) -> list[dict]:
    """Real dwell episodes: ``frames`` consecutive 10 Hz samples per pause.

    A single observation is *not* a dwell: one decision sample with a long dt
    only bounds net displacement over that dt, which the raw trace of the real
    session showed to be a false positive.  The qualification rule requires at
    least two consecutive samples and ~0.3 s of duration.
    """
    out = []
    for e in range(episodes):
        t0 = e * (frames / fps + gap)
        for i in range(frames):
            out.append({"view_id": f"dwell-{e}-{i}", "group": e * 100 + i,
                        "timestamp": t0 + i / fps,
                        "quality": nested_quality(speed=speed,
                                                  motion_deg=motion_deg,
                                                  dt=1.0 / fps)})
    return out


def test_stationarity_requires_a_stream_sequence():
    views = good_views()[:12]
    m = measure(views)
    st = m["checks"]["stationarity"]
    assert st["satisfied"] is False
    assert st["measured"]["stationarity_status"] == "unestablished"
    assert st["measured"]["accepted_stationary_views"] == 12
    assert "unestablished" in st["hint"].lower()
    # the same observations supplied as a stream establish it
    m2 = measure(views, stationarity_observations=views)
    assert m2["checks"]["stationarity"]["satisfied"] is True
    assert m2["checks"]["stationarity"]["measured"]["stationarity_status"] == "stream"


def test_stationarity_from_motion_fields_and_missing_is_not_pass():
    views = good_views()[:6]
    for i, v in enumerate(views):
        v["timestamp"] = 2.0 * i
    stream = _dwell_stream(6)
    m = measure(views, stationarity_observations=stream)
    st = m["checks"]["stationarity"]
    assert st["satisfied"] is True
    assert st["measured"]["n_stationary_intervals"] == 6
    assert st["measured"]["stationarity_source"] == "stream"
    # missing motion fields cannot become a dwell
    bad = good_views()[0]
    bad["quality"] = nested_quality(speed=None, motion_deg=None, dt=None)
    m2 = measure([bad], stationarity_observations=[bad])
    st2 = m2["checks"]["stationarity"]
    assert st2["satisfied"] is False
    assert st2["measured"]["stationarity_status"] == "unestablished"
    assert st2["measured"]["stationarity_source_counts"] == {"missing": 1}
    assert "no per-view stationarity" in " ".join(
        m2["D7"]["detail"]["stationarity"][0]["reasons"])


def test_stream_dwells_satisfy_when_accepted_views_are_moving():
    views = good_views()
    for v in views:
        v["quality"] = nested_quality(speed=30.0, motion_deg=10.0, dt=0.5)
    stream = _dwell_stream(6)
    m = measure(views, stationarity_observations=stream)
    st = m["checks"]["stationarity"]
    assert st["satisfied"] is True
    assert st["measured"]["accepted_stationary_views"] == 0
    assert st["measured"]["n_stationary"] == 18
    assert st["measured"]["stationarity_source"] == "stream"
    assert m["checks"]["quality"]["satisfied"] is True
    assert m["D7"]["satisfied"] is True


def test_distinct_intervals_not_raw_frame_count():
    views = good_views()[:6]
    # ten 10 Hz frames of one pause are one dwell interval, not ten poses
    one_pause = _dwell_stream(1, frames=10)
    m = measure(views, stationarity_observations=one_pause)
    st = m["checks"]["stationarity"]
    assert st["measured"]["n_stationary_intervals"] == 1
    assert st["satisfied"] is False
    # a lone sample with a long dt is not a dwell episode
    lone = measure(views, stationarity_observations=_stationary_stream(6))
    assert lone["checks"]["stationarity"]["satisfied"] is False
    assert lone["checks"]["stationarity"]["measured"][
        "n_stationary_intervals"] == 0
    # six separated pauses are six intervals
    six = _dwell_stream(6)
    m2 = measure(views, stationarity_observations=six)
    assert m2["checks"]["stationarity"]["measured"]["n_stationary_intervals"] == 6
    assert m2["checks"]["stationarity"]["satisfied"] is True


def test_dwell_views_count_for_d7_but_not_pose_dimensions():
    base = good_views()[0]
    base["pose"]["t"] = [0.0, 0.0, 340.0]
    dwell = []
    for e in range(6):
        for i in range(3):
            v = good_views()[0]
            v["view_id"] = 100 + 3 * e + i
            v["pose"]["t"] = [0.0, 0.0, 520.0]   # band 2 if it counted
            v["timestamp"] = e * 2.0 + 0.1 * i
            v["dwell"] = True
            dwell.append(v)
    m = measure([base] + dwell, stationarity_observations=dwell)
    assert m["n_views"] == 19 and m["n_dwell_views"] == 18
    assert m["n_pose_views"] == 1
    assert m["D1"]["measured"]["band_counts"] == [1, 0, 0]
    assert m["D7"]["measured"]["n_stationary_intervals"] == 6
    assert m["checks"]["stationarity"]["satisfied"] is True


# ---------------------------------------------------------------------------
# guidance
# ---------------------------------------------------------------------------

def test_guidance_returns_a_coarse_family_pose_never_a_trajectory():
    views = good_views()[:6]
    m = measure(views)
    g = coverage.guidance_target(m, views[-1]["pose"])
    assert g["mode"] == "move"
    assert g["precise"] is False
    assert g["target_pose"] is not None and g["target_R"] is not None
    R = np.asarray(g["target_R"], float)
    t = np.asarray(g["target_t"], float)
    assert g["unlocks"], g
    fam = g["family"]
    lo, hi = m["spec"]["distance_bands_mm"][fam["distance_band"]]
    assert lo <= float(np.linalg.norm(t)) <= hi
    assert tilt_deg(R)[0] == pytest.approx(fam["tilt_x_deg"], abs=1e-6)
    assert tilt_deg(R)[1] == pytest.approx(fam["tilt_y_deg"], abs=1e-6)
    keys = [(-f["unlock_weight"], f["effort"]["score"]) for f in g["families"]]
    assert keys == sorted(keys)
    assert "mm" in g["instruction"] and g["instruction"].endswith(".")


def test_guidance_prefers_the_nearest_unsatisfied_distance_band():
    views = good_views()
    for v in views:
        v["pose"]["t"] = [0.0, 0.0, 350.0]       # only band 0 populated
    m = measure(views, stationarity_observations=views)
    cur_R, cur_t = family_pose(350.0, 0.0, 0.0, 45.0)
    g = coverage.guidance_target(m, {"R": cur_R, "t": cur_t})
    assert g["mode"] == "move"
    assert g["family"]["distance_band"] == 1        # 380-460 is the nearest gap
    assert g["family"]["distance_required"] is True
    assert "distance band 1" in g["unlocks"]


def test_guidance_keeps_guiding_through_ordinary_motion():
    views = good_views()[:6]
    last = views[-1]
    last["timestamp"] = views[-2]["timestamp"] + 1e-3
    last["pose"]["t"] = np.asarray(views[-2]["pose"]["t"], float) + [0.0, 0.0, 50.0]
    last["quality"] = nested_quality(speed=500.0, motion_deg=20.0, dt=0.001)
    m = measure(views)
    assert m["stable"] is False
    g = coverage.guidance_target(m, views[-1]["pose"])
    assert g["mode"] == "move"
    assert g["target_pose"] is not None
    assert "hold still" not in g["instruction"].lower()


def test_guidance_holds_when_the_pose_itself_is_unreliable():
    views = good_views()[:6]
    views[-1]["assignment_ok"] = False
    m = measure(views)
    assert m["stable"] is False
    g = coverage.guidance_target(m, views[-1]["pose"])
    assert g["mode"] == "hold"
    assert g["target_pose"] is None and g["target_t"] is None
    assert g["precise"] is False
    assert "hold still" in g["instruction"].lower()


def test_guidance_done_when_coverage_complete():
    spec = CoverageSpec(require_scale_artefact=True)
    views = good_views()
    m = measure(views, spec, stationarity_observations=views)
    g = coverage.guidance_target(m, None)
    assert g["mode"] == "done" and g["target_pose"] is None
    assert "coverage complete" in g["instruction"].lower()


def test_empty_input_is_not_ready():
    m = measure([])
    r = readiness(m)
    assert r["ready"] is False and r["n_views"] == 0
    assert len(r["unsatisfied_dimensions"]) >= len(DIMENSIONS) - 1
    assert {"D1", "D2", "D3", "D4", "D5", "D6"}.issubset(
        set(r["unsatisfied_dimensions"]))
    g = coverage.guidance_target(m, None)
    assert g["mode"] in ("move", "hold")
    assert g["precise"] is False


# ---------------------------------------------------------------------------
# D4 guidance: only deficient image cells, no distance-first wording
# ---------------------------------------------------------------------------

def d4_pattern_views() -> list[dict]:
    """297 views whose 3x3 image counts are the real session03 pattern.

    ``[9, 30, 0, 38, 198, 5, 2, 15, 0]``: every corner except top-right and
    bottom-right is already visited.  Poses and quality cycle through
    :func:`good_views`, so D1/D2/D3/D5/D6 stay satisfied and D4 is the only
    pose failure.
    """
    import copy
    base = good_views()
    counts = [9, 30, 0, 38, 198, 5, 2, 15, 0]
    cells = [(i // 3, i % 3) for i, c in enumerate(counts) for _ in range(c)]
    out = []
    for i, cell in enumerate(cells):
        v = copy.deepcopy(base[i % len(base)])
        v["view_id"] = i
        v["timestamp"] = float(i)
        centre = np.asarray(_REGION_CENTRE[cell], float)
        pts = np.array([[centre[0] + 8.0 * (u - 3.5),
                         centre[1] + 8.0 * (v - 2)]
                        for v in range(5) for u in range(8)], float)
        v["image_points"] = {"left": pts, "right": pts - [150.0, 0.0],
                             "tex": pts}
        out.append(v)
    return out


def test_guidance_targets_only_deficient_right_corners_without_distance():
    views = d4_pattern_views()
    m = measure(views)
    assert m["D4"]["measured"]["corners"] == {
        "top_left": 9, "top_right": 0, "bottom_left": 2, "bottom_right": 0}
    assert m["D1"]["satisfied"] is True
    g = coverage.guidance_target(m, views[-1]["pose"])
    assert g["mode"] == "move"
    low = g["instruction"].lower()
    assert ("top-right" in low) or ("bottom-right" in low), low
    assert "top-left" not in low and "bottom-left" not in low, low
    assert "mm" not in low and "distance" not in low, low
    assert g["family"]["image_target"]["kind"] == "corner"
    assert g["family"]["image_region"] in ([0, 2], [2, 2])
    assert g["family"]["distance_required"] is False
    assert "distance band" not in " ".join(g["unlocks"])
    assert all(f["image_region"] in ([0, 2], [2, 2]) for f in g["families"])


def _factory_left_and_right():
    if not FACTORY.is_file():
        pytest.skip("factory profile not available on this host")
    from calib.recon_profile import load
    prof = load(FACTORY)
    return prof.cameras["ir_left"], prof.cameras["ir_right"]


def test_right_corner_lateral_projects_to_image_right_and_is_attainable():
    """The col=2 lateral offset must project right with the factory intrinsics.

    It must also keep enough plate visible in both IR cameras: the right corner
    is physically attainable, but only with partial plate visibility in the
    right IR (36/40 in the real-session target, >= 28 live acceptance and >= 12 D5).
    """
    from calib import geometry, plate
    left, right = _factory_left_and_right()
    views = d4_pattern_views()
    m = measure(views)
    g = coverage.guidance_target(m, views[-1]["pose"])
    R = np.asarray(g["target_R"], float)
    t = np.asarray(g["target_t"], float)
    assert g["family"]["lateral_mm"][0] > 0.0     # camera x => image right

    uv = geometry.project(left.K, left.dist, t.reshape(1, 3))[0]
    assert uv[0] > 2.0 * 1280.0 / 3.0, uv
    row = g["family"]["image_region"][0]
    if row == 0:
        assert uv[1] < 1024.0 / 3.0, uv
    else:
        assert uv[1] > 2.0 * 1024.0 / 3.0, uv

    obj = plate.board_points_mm()
    uv_l = geometry.project_world(left.K, left.dist, R, t, obj)
    x_right = obj @ (right.R @ R).T + (right.R @ t + right.T)
    uv_r = geometry.project(right.K, right.dist, x_right)
    ok_l = ((uv_l[:, 0] >= 0) & (uv_l[:, 0] < 1280)
            & (uv_l[:, 1] >= 0) & (uv_l[:, 1] < 1024))
    ok_r = ((uv_r[:, 0] >= 0) & (uv_r[:, 0] < 1280)
            & (uv_r[:, 1] >= 0) & (uv_r[:, 1] < 1024))
    assert int(ok_l.sum()) >= 28
    assert int(ok_r.sum()) >= 28        # live controller's per-camera floor
    assert int((ok_l & ok_r).sum()) >= 12
    # the visible-left centroid is in the requested corner, not merely the
    # projected board origin
    centre = uv_l[ok_l].mean(axis=0)
    assert centre[0] > 2.0 * 1280.0 / 3.0, centre
    if row == 0:
        assert centre[1] < 1024.0 / 3.0, centre
    else:
        assert centre[1] > 2.0 * 1024.0 / 3.0, centre


def test_satisfied_distance_is_never_the_reported_priority():
    views = d4_pattern_views()            # D1 satisfied, D4 right corners missing
    m = measure(views)
    assert m["D1"]["satisfied"] is True and m["D4"]["satisfied"] is False
    g = coverage.guidance_target(m, views[-1]["pose"])
    low = g["instruction"].lower()
    assert "mm" not in low and "further" not in low and "closer" not in low, low
    assert g["family"]["distance_required"] is False
    assert "distance band" not in " ".join(g["unlocks"])


# ---------------------------------------------------------------------------
# D7 sub-indicators and readiness shape
# ---------------------------------------------------------------------------

def test_d7_subindicators_are_separate_and_readiness_reports_d7_once():
    views = good_views()
    m = measure(views)                    # no stream: stationarity unestablished
    dims = m["dimensions"]
    assert dims["quality"]["satisfied"] is True
    assert dims["stationarity"]["satisfied"] is False
    assert dims["quality"]["hint"] != dims["stationarity"]["hint"]
    assert "quality" in dims["quality"]["hint"].lower()
    assert "stationarity" in dims["stationarity"]["hint"].lower()
    r = readiness(m)
    assert r["unsatisfied_dimensions"] == ["D7"]
    assert r["unsatisfied_dimensions"].count("D7") == 1
    assert not any("stationarity / quality" in label.lower()
                   for label in r["unsatisfied"])

    bad = _broken("quality")
    m2 = measure(bad, stationarity_observations=bad)
    d2 = m2["dimensions"]
    assert d2["quality"]["satisfied"] is False
    assert d2["stationarity"]["satisfied"] is True
    assert d2["quality"]["hint"] != d2["stationarity"]["hint"]
    assert readiness(m2)["unsatisfied_dimensions"] == ["D7"]


def test_d7_only_failure_holds_for_dwell_evidence_not_another_pose():
    views = good_views()
    m = measure(views)
    assert [d for d in DIMENSIONS if not m[d]["satisfied"]] == ["D7"]
    g = coverage.guidance_target(m, views[-1]["pose"])
    assert g["mode"] == "hold"
    assert g["target_pose"] is None and g["family"] is None
    assert "dwell" in g["instruction"].lower()
    assert "stationarity" in g["instruction"].lower()
    assert g["dwell_hint"]


def test_explicit_stationary_flag_without_motion_fields_is_used():
    stream = [{"group": e * 100 + i, "timestamp": e * 2.0 + 0.1 * i,
               "quality": {"metrics": {"stationary": True}}}
              for e in range(6) for i in range(3)]
    m = measure(good_views(), stationarity_observations=stream)
    st = m["checks"]["stationarity"]
    assert st["satisfied"] is True
    assert st["measured"]["stationarity_source_counts"] == {"explicit": 18}
    assert st["measured"]["n_stationary_intervals"] == 6


def test_still_collection_poses_are_each_a_dwell_episode():
    """Synthetic/vendor stills are deliberate stationary poses, not a stream.

    A still collection has no consecutive 10 Hz cadence: each image is one
    stationary pose.  Requiring two consecutive samples (right for a live
    stream) must not discard them; six still poses are six dwell episodes.
    """
    views = good_views()[:6]
    stream = [{"group": i, "timestamp": 2.0 * i,
               "stationarity_still": True, "stationary": True,
               "quality": {"metrics": {"stationary": True}}}
              for i in range(6)]
    m = measure(views, stationarity_observations=stream)
    st = m["checks"]["stationarity"]
    assert st["measured"]["n_stationary_intervals"] == 6
    assert st["satisfied"] is True


def test_fixed_tilt_centre_keeps_the_goalposts_stable():
    """A moving tilt centre can make a satisfied dimension disappear.

    The gate centres the tilt excursion on its midrange.  With eight views at
    0 deg and two at +20 deg, the dynamic centre is +10 deg and both positive
    views count as zero; a live session that freezes the centre after its first
    views does not have this failure mode.
    """
    views = [_view(i, 400, 0, 0, 0, (640, 512), i * 0.5) for i in range(8)]
    views += [_view(8 + i, 400, 20, 0, 0, (640, 512), 4 + i * 0.5)
              for i in range(2)]
    dynamic = measure(views)
    fixed = measure(views, tilt_center_deg=(0.0, 0.0))
    dyn_x = dynamic["checks"]["tilt_axis_x"]["measured"]
    fix_x = fixed["checks"]["tilt_axis_x"]["measured"]
    assert dyn_x["tilt_centre_deg"] == pytest.approx(10.0)
    assert dyn_x["positive"] == 0
    assert fix_x["tilt_centre_deg"] == 0.0
    assert fix_x["positive"] == 2
