"""Coverage gate tests: every dimension, the 1-D sweep rejection, guidance."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from tools.wizard import coverage
from tools.wizard.coverage import (CoverageSpec, DIMENSIONS, family_pose, measure,
                                   plate_normal, readiness, roll_deg, tilt_deg)

PRIVATE_SWEEP = Path.home() / "einstar-private/analysis/sweep-b/sweep_report.json"


# ---------------------------------------------------------------------------
# a deterministic, fully-covering synthetic session
# ---------------------------------------------------------------------------

def _view(view_id: int, distance: float, tx: float, ty: float, roll: float,
          centre: tuple[float, float], t: float, *, n_markers: int = 40,
          shared: int = 40, sat: float = 0.001, focus: float = 800.0,
          artefact: float | None = None) -> dict:
    R, tr = family_pose(distance, tx, ty, roll)
    pts = np.array([[centre[0] + 8 * (u - 3.5), centre[1] + 8 * (v - 2)]
                    for v in range(5) for u in range(8)], float)
    v = {
        "view_id": int(view_id),
        "pose": {"R": R, "t": tr},
        "n_markers": {"left": n_markers, "right": n_markers, "tex": n_markers},
        "quality": {"mean": 60.0, "saturation_frac": sat, "focus": focus,
                    "blur": focus},
        "image_points": {"left": pts, "right": pts - [150.0, 0.0], "tex": pts},
        "shared_markers_ir": int(shared),
        "assignment_ok": True,
        "timestamp": float(t),
    }
    if artefact is not None:
        v["scale_artefact"] = {"length_mm": 100.0, "measured_mm": artefact}
    return v


#: image centre of each 3x3 region of a 1280x1024 frame
_REGION_CENTRE = {
    (r, c): (213.0 + 427.0 * c, 170.0 + 341.0 * r) for r in range(3) for c in range(3)
}


def good_views(dwell: int = 2) -> list[dict]:
    """A session that satisfies every dimension of the default spec.

    Each pose is captured ``dwell`` times 0.35 s apart (a real "hold still"),
    with a 2.5 s reposition between poses so the *transition* is not mistaken
    for motion blur.  This is what D7 (stationarity) measures.
    """
    views: list[dict] = []
    k = 0
    t = 0.0
    plan: list[tuple[int, int, tuple[int, int], float, float | None]] = []

    regions = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 0), (2, 1), (2, 2)]
    rolls = [45.0, 135.0, 225.0, 315.0]
    for i in range(12):
        plan.append((i % 3, i % 4, regions[i % 9], rolls[i % 4], None))
    # explicit corner visits (regions 0,2,6,8 are the four image corners)
    for n, (band, quad, region, roll) in enumerate([
            (0, 0, (0, 0), 45.0), (1, 1, (0, 2), 135.0),
            (2, 2, (2, 0), 225.0), (0, 3, (2, 2), 315.0)]):
        plan.append((band, quad, region, roll, 100.0 + 0.05 * n))
    plan.append((1, 0, (1, 1), 45.0, 99.98))     # 5th scale-artefact pose

    for band, quad, region, roll, artefact in plan:
        lo, hi = CoverageSpec().distance_bands_mm[band]
        d = 0.5 * (lo + hi)
        sx = -1.0 if quad in (2, 3) else 1.0
        sy = -1.0 if quad in (1, 3) else 1.0
        for _ in range(max(int(dwell), 1)):
            # 18 deg beyond the 15 deg requirement, symmetric about zero
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


def test_family_pose_roundtrip():
    for tx, ty, roll in [(0, 0, 0), (20, -15, 90), (-30, 25, 180), (12, 18, 270)]:
        R, t = family_pose(420.0, tx, ty, roll)
        gx, gy = tilt_deg(R)
        assert gx == pytest.approx(tx, abs=1e-6)
        assert gy == pytest.approx(ty, abs=1e-6)
        assert roll_deg(R) == pytest.approx(roll % 360.0, abs=1e-6)
        assert np.allclose(np.linalg.norm(t), np.linalg.norm([t[0], t[1], 420.0]))
        assert plate_normal(R)[2] < 0        # plate faces the camera


def test_measure_reports_all_dimensions_with_hints():
    m = measure(good_views())
    assert set(DIMENSIONS) <= set(m)
    for d in DIMENSIONS:
        node = m[d]
        for key in ("required", "measured", "satisfied", "deficit", "hint", "detail"):
            assert key in node, (d, key)
        assert isinstance(node["hint"], str) and len(node["hint"]) > 8
        assert node["hint"].endswith((".", "!"))


# ---------------------------------------------------------------------------
# every dimension: satisfied and unsatisfied
# ---------------------------------------------------------------------------

def test_all_dimensions_satisfied_by_a_good_session():
    spec = CoverageSpec(require_scale_artefact=True)
    m = measure(good_views(), spec)
    r = readiness(m)
    assert r["ready"] is True, r["reasons"]
    assert r["unsatisfied"] == [] and r["unsatisfied_dimensions"] == []
    for d in DIMENSIONS:
        assert m[d]["satisfied"] is True, (d, m[d]["hint"])
    # measured ranges are real numbers, not placeholders
    assert sum(m["D1"]["measured"]["band_counts"]) == m["n_usable"]
    assert min(m["D1"]["measured"]["band_counts"]) >= 3
    assert m["D3"]["measured"]["n_bins_covered"] == 4
    assert min(m["D4"]["measured"]["corners"].values()) >= 1
    assert m["D4"]["measured"]["regions_visited"] >= 6
    assert m["D5"]["measured"]["min_shared"] == 40
    assert m["D7"]["measured"]["n_stationary"] >= 6


def _broken(kind: str) -> list[dict]:
    views = good_views()
    if kind == "distance":
        for v in views:
            R = np.asarray(v["pose"]["R"], float)
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
        for i, v in enumerate(views):
            v["quality"]["saturation_frac"] = 0.2
            v["timestamp"] = 0.001 * i
            v["pose"]["t"] = np.asarray(v["pose"]["t"], float) + [0.0, 0.0, 1.0 * i]
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
    ("quality", "D7", "stationarity"),
    ("scale", "D8", "artefact"),
])
def test_each_dimension_fails_when_its_requirement_is_removed(kind, dimension, needle):
    spec = CoverageSpec(require_scale_artefact=True)
    m = measure(_broken(kind), spec)
    r = readiness(m)
    assert m[dimension]["satisfied"] is False, m[dimension]
    assert dimension in r["unsatisfied_dimensions"]
    assert any(needle in label.lower() for label in r["unsatisfied"]), r["unsatisfied"]
    assert r["ready"] is False
    # the other dimensions must stay satisfied: the gate is not a global veto.
    # D8 is genuinely coupled to D1 (a scale artefact must be imaged at several
    # distances), so removing the distance diversity also removes that check.
    coupled = {"distance": {"D8"}}.get(kind, set())
    others = [d for d in DIMENSIONS if d != dimension and d not in coupled]
    assert all(m[d]["satisfied"] for d in others), \
        [(d, m[d]["hint"]) for d in others if not m[d]["satisfied"]]
    # every failure carries a number and an operator sentence
    for reason in r["reasons"]:
        assert reason["deficit"] is not None
        assert reason["hint"]


# ---------------------------------------------------------------------------
# the 1-D sweep must fail
# ---------------------------------------------------------------------------

def _synthetic_sweep_views(n: int = 350) -> list[dict]:
    """350 poses generated from the *measured* sweep-b numbers.

    ``docs/reconstruction/live_bringup_2026-11-11.md`` §6/§7a: x -99.8..+30.2 mm,
    z 370.8..397.9 mm, tilt 5.4..19.5 deg about one fixed axis (tilt_x:tilt_y =
    -10.6:-6.0), roll -178.0 +- 1.2 deg, plate centre u 326..710 px, v 441..458 px,
    3 legs, 6.5 mm/s.  A deterministic piecewise-linear reproduction of that.
    """
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
    """Prefer the real sweep-b report, fall back to the synthesised fixture."""
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
    # the sweep never ran the colour detector: colour is unknown, not absent
    m = measure(views, CoverageSpec(require_colour=False))
    r = readiness(m)
    assert r["ready"] is False, origin
    labels = " | ".join(r["unsatisfied"]).lower()
    for needle in ("distance band", "second axis", "roll", "corner"):
        assert needle in labels, (origin, r["unsatisfied"])
    # the failures are metric, not an empty-set artefact
    assert m["D1"]["measured"]["band_counts"].count(0) >= 2          # one band only
    assert m["D1"]["measured"]["distance_mm"]["span"] < 30.0
    assert m["D3"]["measured"]["n_bins_covered"] == 1
    assert m["D4"]["measured"]["regions_visited"] <= 3
    assert all(c == 0 for c in m["D4"]["measured"]["corners"].values())
    assert m["D2"]["measured"]["tilt_y"]["positive"] == 0
    assert m["D2"]["measured"]["tilt_y"]["negative"] == 0
    # stereo overlap *is* satisfied: the sweep's only strength
    assert m["D5"]["satisfied"] is True
    assert m["D5"]["measured"]["min_shared"] >= 35
    # and the summary names the dimensions
    assert "not ready" in r["summary"] and "D1" in r["summary"]


# ---------------------------------------------------------------------------
# guidance
# ---------------------------------------------------------------------------

def test_guidance_returns_a_coarse_family_pose_never_a_trajectory():
    views = good_views()
    views = views[:6]                     # far from complete
    m = measure(views)
    g = coverage.guidance_target(m, views[-1]["pose"])
    assert g["mode"] == "move"
    assert g["precise"] is False
    assert g["target_pose"] is not None and g["target_R"] is not None
    R = np.asarray(g["target_R"], float)
    t = np.asarray(g["target_t"], float)
    # the target really belongs to an unsatisfied family
    assert g["unlocks"], g
    fam = g["family"]
    lo, hi = m["spec"]["distance_bands_mm"][fam["distance_band"]]
    assert lo <= float(np.linalg.norm(t)) <= hi
    assert tilt_deg(R)[0] == pytest.approx(fam["tilt_x_deg"], abs=1e-6)
    assert tilt_deg(R)[1] == pytest.approx(fam["tilt_y_deg"], abs=1e-6)
    # ranked alternatives exist and are sorted by the effort score
    scores = [f["effort"]["score"] for f in g["families"]]
    assert scores == sorted(scores)
    assert "mm" in g["instruction"] and g["instruction"].endswith(".")


def test_guidance_prefers_the_nearest_unsatisfied_family():
    views = good_views()
    # keep only the closest band: the 300-380 band is nearest to a 350 mm pose
    m = measure([v for v in views if np.linalg.norm(v["pose"]["t"]) < 380.0])
    cur_R, cur_t = family_pose(350.0, 0.0, 0.0, 45.0)
    g = coverage.guidance_target(m, {"R": cur_R, "t": cur_t})
    assert g["mode"] == "move"
    assert g["family"]["distance_band"] == 1        # 380-460 is the nearest gap
    assert g["effort"]["score"] >= 0.0


def test_guidance_keeps_guiding_through_ordinary_motion():
    """Inter-frame speed is not blur and must not stop the session.

    A 1.2 ms exposure at ordinary hand speed produces a sharp frame; the old
    gate rejected 55 of 178 views and then asked the operator to hold still on
    nearly every decision.  The coverage guidance must keep offering the next
    family; D7 still records that this was not a dwell view.
    """
    views = good_views()[:6]               # far from complete
    views[-1]["timestamp"] = views[-2]["timestamp"] + 1e-3
    views[-1]["pose"]["t"] = np.asarray(views[-2]["pose"]["t"], float) + [0.0, 0.0, 50.0]
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
    m = measure(good_views(), spec)
    g = coverage.guidance_target(m, None)
    assert g["mode"] == "done" and g["target_pose"] is None
    assert "coverage complete" in g["instruction"].lower()


def test_empty_input_is_not_ready():
    m = measure([])
    r = readiness(m)
    assert r["ready"] is False and r["n_views"] == 0
    assert len(r["unsatisfied_dimensions"]) >= len(DIMENSIONS) - 1   # D8 not required
    assert {"D1", "D2", "D3", "D4", "D5", "D6"}.issubset(set(r["unsatisfied_dimensions"]))
    g = coverage.guidance_target(m, None)
    assert g["mode"] in ("move", "hold")
    assert g["precise"] is False
