"""The wizard launcher's ``--complete-session`` wiring (Capture Missing Views).

The operator entry point for the extension workflow is the Qt launcher:

    tools/wizard/gui/app.py --complete-session ~/einstar-calibration-03 \\
        --session-dir ~/einstar-calibration-03-add --source live

These tests pin the argument surface and the control flow **without** creating a
Qt window, spawning a capture process or touching a real session.  The
controller behaviour itself (plan, hold tracker, extension store) is covered by
``tests/test_missing_views.py``.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
for _p in (REPO, TOOLS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from tools.wizard.gui import app as launcher  # noqa: E402


def test_complete_session_defaults_to_off():
    args = launcher.parse(["--source", "replay", "--frames", "/tmp/x"])
    assert args.complete_session == ""
    assert args.extension_json == ""


def test_complete_session_flag_is_accepted():
    args = launcher.parse([
        "--source", "live",
        "--session-dir", "/tmp/ext",
        "--complete-session", "/tmp/base",
    ])
    assert args.complete_session == "/tmp/base"
    assert args.session_dir == "/tmp/ext"


def _run(monkeypatch, controller, window_cls):
    ui = importlib.import_module("wizard.gui.controller_adapter")
    mw = importlib.import_module("wizard.gui.modern_window")
    monkeypatch.setattr(ui, "open_source", lambda *a, **kw: object(), raising=False)
    monkeypatch.setattr(mw, "ModernMainWindow", window_cls, raising=False)
    real = importlib.import_module("tools.wizard.gui.app")
    # ``main()`` does ``from wizard.gui import controller_adapter`` inside the
    # function; patching the module object itself is what reaches it.
    monkeypatch.setattr(ui, "real_controller_available",
                        lambda: True, raising=False)
    monkeypatch.setattr(ui, "make_controller",
                        lambda **kw: controller, raising=False)
    return real.main([
        "--source", "replay", "--frames", "/tmp/x",
        "--session-dir", "/tmp/ext", "--complete-session", "/tmp/base",
        "--headless", "--duration", "0.1",
    ])


def test_complete_session_refuses_a_controller_without_support(monkeypatch):
    """A controller that cannot complete an existing session must fail loudly
    instead of silently starting a fresh full capture."""
    class _NoExtension:
        pass

    class _Window:                              # pragma: no cover - not reached
        def __init__(self, *a, **kw):
            pytest.fail("the window must not open when the mode is refused")

    with pytest.raises(SystemExit) as exc:      # argparse-free early return path
        rc = _run(monkeypatch, _NoExtension(), _Window)
        raise SystemExit(rc)
    assert exc.value.code == 2


def test_complete_session_calls_begin_extension(monkeypatch):
    calls = {}

    class _Controller:
        def begin_extension(self, base):
            calls["base"] = base
            return {"ok": True, "accepted": 297, "plan": {"poses": [1, 2, 3, 4]}}

        def resume(self):                       # pragma: no cover - not reached
            raise AssertionError("resume must not run in extension mode")

        def snapshot(self):
            return {"state": "running", "counts": {"accepted": 297},
                    "coverage": {"satisfied": 7, "total": 8},
                    "readiness": {"ready": False, "missing": ["stationarity"]}}

    opened = {}

    class _Window:
        def __init__(self, controller, source, **kwargs):
            opened["controller"] = controller

        def show(self):
            opened["shown"] = True

        def maybe_autostart(self):
            opened["autostart"] = True

        def _on_start(self):
            opened["started"] = True

        def snapshot_png(self, path):
            opened["snapshot"] = str(path)

        def _on_cancel(self, *a, **kw):
            opened["cancelled"] = True

        def close(self):
            opened["closed"] = True

        def shutdown(self, *a, **kw):
            opened["shutdown"] = True

        def deleteLater(self):
            opened["deleted"] = True

    rc = _run(monkeypatch, _Controller(), _Window)
    assert calls.get("base") == "/tmp/base"
    assert opened.get("shown") is True
    assert rc == 0
