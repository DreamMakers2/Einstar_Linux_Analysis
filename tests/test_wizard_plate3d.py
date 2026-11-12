"""The 3-D plate preview and the panels that must never mislead."""
from __future__ import annotations

import math
import os
from pathlib import Path
import sys

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _ink(image) -> float:
    """Fraction of non-background pixels: a blank widget reads ~0."""
    from PySide6 import QtGui
    img = image.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    lit = 0
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            c = img.pixelColor(x, y)
            if abs(c.red() - 11) + abs(c.green() - 14) + abs(c.blue() - 18) > 24:
                lit += 1
    return lit / ((w // 3 + 1) * (h // 3 + 1))


def test_renders_a_pose_and_reacts_to_orbit(app):
    from tools.wizard.gui.plate3d import PlatePreview
    w = PlatePreview()
    blank = _ink(w.render_image(360, 280))
    assert blank < 0.02, "an empty preview should be background"
    w.set_pose(np.eye(3), np.array([0.0, 0.0, 400.0]))
    w.set_status("400 mm · tilt 0°")
    front = _ink(w.render_image(360, 280))
    assert front > 0.10, front
    c, s = math.cos(math.radians(35)), math.sin(math.radians(35))
    w.set_pose(np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), np.array([12, 8, 430.0]))
    tilted = _ink(w.render_image(360, 280))
    assert abs(tilted - front) > 0.01, "a tilted board must draw differently"
    w.mode = "orbit"
    w.azimuth_deg, w.elevation_deg = 50.0, 25.0
    orbit = _ink(w.render_image(360, 280))
    assert orbit > 0.10, orbit
    img_front = w.render_image(120, 100)
    w.mode = "scanner"
    w.set_pose(np.eye(3), np.array([0.0, 0.0, 430.0]))
    img_scan = w.render_image(120, 100)
    assert img_front.size() == img_scan.size()


def test_plate_geometry_matches_the_spec():
    from tools.wizard.gui.plate3d import plate_geometry
    grid, corners = plate_geometry(28.0, 8, 5)
    assert grid.shape == (40, 3)
    assert abs(float(np.abs(corners[:, 0]).max()) - 4 * 28.0) < 1e-9
    assert abs(float(np.abs(corners[:, 1]).max()) - 2.5 * 28.0) < 1e-9


def test_capture_panel_never_reports_zero_fps_for_replay(app):
    from tools.wizard.gui import panels
    panel = panels.CapturePanel()
    panel.update_from({
        "source": {"kind": "replay", "path": "/tmp/frames"},
        "capture": {"state": "idle", "fps": 0.0, "groups_written": 0},
        "counts": {"accepted": 3, "rejected": 9},
        "processing_fps": 4.8,
        "session": {"raw_recording": "/tmp/s/raw.e1r"},
    })
    assert panel.rows["state"].text() == "replay source"
    assert "decisions/s" in panel.rows["rate"].text()
    assert "0.00 fps" not in panel.rows["rate"].text()
    assert panel.rows["groups"].text() == "3 accepted · 9 rejected"


def test_capture_panel_reports_a_live_capture(app):
    from tools.wizard.gui import panels
    panel = panels.CapturePanel()
    panel.update_from({
        "source": {"kind": "live", "serial": "ABC"},
        "capture": {"state": "running", "fps": 9.8, "throughput_mbps": 38.0,
                    "groups_written": 120, "groups_on_disk": 40, "ring_size": 120,
                    "drops": 2, "disk_free_gb": 21.0},
        "counts": {"accepted": 5, "rejected": 1},
        "session": {"raw_recording": "/tmp/s/raw.e1r"},
    })
    assert panel.rows["state"].text() == "running"
    assert "9.80 fps" in panel.rows["rate"].text()
    assert "120 written" in panel.rows["groups"].text()


def test_guidance_feedback_says_what_to_do_next():
    """The operator must see why a pose was not captured, in plain words."""
    from tools.wizard.gui.panels import feedback_text
    text, colour = feedback_text({"accepted": True, "view_id": 7})
    assert "captured view 7" in text and colour == "OK"

    text, colour = feedback_text(
        {"accepted": False, "reasons": ["too similar to an accepted view (0.1 deg, 2 mm)"]})
    assert "pose already covered" in text and colour == "MUTED"

    text, colour = feedback_text({"accepted": False, "reasons": ["moving (6.2 mm, 0.4 deg)"]})
    assert "slower" in text and colour == "WARN"

    text, colour = feedback_text(
        {"accepted": False, "reasons": ["redundant view (predicted gain 0.10% < 3.00%)"]})
    assert "adds nothing new" in text

    text, colour = feedback_text({"accepted": False, "reasons": ["no pose"]})
    assert "plate not read" in text and colour == "BAD"

    text, colour = feedback_text(
        {"accepted": False, "reasons": ["orientation ambiguous"]})
    assert colour == "BAD"

    assert feedback_text(None) == ("", "MUTED")


def test_progress_panel_reports_readiness_not_a_percentage(app):
    """A missing dimension must read as NOT READY with the reasons named."""
    from tools.wizard.gui import panels
    panel = panels.ProgressPanel()
    panel.update_from({
        "counts": {"accepted": 3, "rejected": 9, "redundant": 9},
        "coverage": {"satisfied": 3, "total": 8, "dimensions": {
            "distance_bands": {"satisfied": False, "hint": "Move closer"},
            "image_regions": {"satisfied": True}}},
        "readiness": {"ready": False,
                      "missing": ["distance_bands", "tilt_two_axes",
                                  "tilt_quadrants", "roll_sectors"]},
        "source": {"kind": "replay"}, "capture": {},
        "session": {"elapsed_s": 12.0}, "processing_fps": 4.6,
    })
    text = panel.readiness.text()
    assert text.startswith("NOT READY")
    assert "distance bands" in text and "+2 more" in text
    assert "4.6 decisions/s" in panel.footer.text()
    assert panel.bar.value() == 3 and panel.bar.maximum() == 8

    panel.update_from({
        "counts": {}, "coverage": {"satisfied": 8, "total": 8, "dimensions": {}},
        "readiness": {"ready": True, "missing": []},
        "source": {"kind": "live"}, "capture": {"fps": 9.9},
        "session": {"elapsed_s": 60.0},
    })
    assert "READY" in panel.readiness.text()
    assert "9.9 fps" in panel.footer.text()


def test_controller_reports_the_real_source_kind(app, tmp_path):
    """A live run must not be labelled replay anywhere in the UI."""
    from tools.wizard.gui import controller_adapter
    factory = Path(os.path.expanduser(
        "~/einstar-private/einstar-profiles/factory-e10.json"))
    if not factory.is_file():
        pytest.skip("factory profile not available")
    if not controller_adapter.real_controller_available():
        pytest.skip("real controller unavailable")
    live = controller_adapter.make_controller(
        factory_profile=str(factory), session_dir=str(tmp_path / "s"),
        mode="standard", source_kind="live")
    snap = live.snapshot()
    assert snap["source"]["kind"] == "live", snap.get("source")
    replay = controller_adapter.make_controller(
        factory_profile=str(factory), session_dir=str(tmp_path / "r"),
        mode="standard", source_kind="replay")
    assert replay.snapshot()["source"]["kind"] == "replay"


def test_scanner_projection_tracks_translation_and_distance(app):
    """Auto-fitting the bbox made t=(0,0,400) and (120,60,400) identical."""
    from PySide6 import QtCore
    from tools.wizard.gui.plate3d import PlatePreview
    w = PlatePreview()
    rect = QtCore.QRectF(0, 0, 400, 300)
    w.set_pose(np.eye(3), np.array([0.0, 0.0, 400.0]))
    a, _ = w._project(rect)
    w.set_pose(np.eye(3), np.array([120.0, 60.0, 400.0]))
    b, _ = w._project(rect)
    assert a[0] != b[0], "translation must move the projected plate"
    w.set_pose(np.eye(3), np.array([0.0, 0.0, 800.0]))
    c, _ = w._project(rect)
    # indices 40 and 41 are the two corners on the bottom edge
    span_a = abs(a[40][0] - a[41][0])
    span_c = abs(c[40][0] - c[41][0])
    assert span_c < 0.75 * span_a, (span_a, span_c)
