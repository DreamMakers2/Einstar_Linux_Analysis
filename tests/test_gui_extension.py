"""Qt entry point for "capture only the missing views" (extension sessions).

The workflow itself lives in the controller (``begin_extension``) and is wired
into the launcher by ``--complete-session``.  The window cannot rebind a live
capture, so its operator button restarts the *application* with the right
flags.  These tests pin both halves offline:

* the pure argv rewrite / directory derivation (no Qt, no controller), and
* the widget behaviour with an offscreen ``QApplication``: when the ghost
  button appears, what it asks, and that the relaunch is released, ordered and
  refused safely.

No scanner, private data or network is required.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Must be set before PySide6 creates a QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO, REPO / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from tools.wizard.gui import extension  # noqa: E402

BASE = "/sessions/cal-01"
NEW = "/sessions/cal-01-ext"
APP = "/repo/tools/wizard/gui/app.py"
PYTHON = "/usr/bin/python3"


# ---------------------------------------------------------------------------
# pure argv rewrite
# ---------------------------------------------------------------------------
def test_launch_drops_session_source_and_capture_options():
    original = [
        "--session-dir", "/old/session",
        "--complete-session", "/old/base",
        "--source", "replay",
        "--frames", "/old/frames",
        "--recording", "/old/raw.e1r",
        "--scene", "distance",
        "--views", "3",
        "--noise", "0.4",
        "--seed", "11",
        "--duration", "8",
        "--snapshot-out", "/tmp/snap.png",
        "--extension-json", "/tmp/ext.json",
        "--max-groups", "4",
        "--resume", "--headless", "--autostart", "--demo",
        "--async-decisions", "--loop",
    ]
    out = extension.build_extension_launch(original, BASE, NEW, APP, PYTHON)
    assert out == [PYTHON, APP, "--source", "live",
                   "--complete-session", BASE, "--session-dir", NEW]


def test_launch_drops_the_equals_form_too():
    original = [
        "--session-dir=/old/session",
        "--complete-session=/old/base",
        "--source=replay",
        "--frames=/old/frames",
        "--recording=/old/raw.e1r",
        "--scene=distance",
        "--views=3",
        "--noise=0.4",
        "--seed=11",
        "--duration=8",
        "--snapshot-out=/tmp/snap.png",
        "--extension-json=/tmp/ext.json",
        "--max-groups=4",
        "--resume", "--headless", "--autostart", "--demo",
        "--async-decisions", "--loop",
    ]
    out = extension.build_extension_launch(original, BASE, NEW, APP, PYTHON)
    assert out == [PYTHON, APP, "--source", "live",
                   "--complete-session", BASE, "--session-dir", NEW]


def test_launch_preserves_unrelated_options_and_forces_live():
    original = [
        "--profile", "/profiles/factory.json",
        "--mode", "high",
        "--pace", "4.0",
        "--no-auto-exposure",
        "--session-dir=/old/session",
        "--source", "synthetic",
        "--scene", "coverage",
    ]
    out = extension.build_extension_launch(original, BASE, NEW, APP, PYTHON)
    assert out[:2] == [PYTHON, APP]
    assert out[2:] == [
        "--profile", "/profiles/factory.json",
        "--mode", "high",
        "--pace", "4.0",
        "--no-auto-exposure",
        "--source", "live",
        "--complete-session", BASE,
        "--session-dir", NEW,
    ]
    # exactly one source, and it is the forced live one
    assert out.count("--source") == 1
    assert "/old/session" not in out
    assert "synthetic" not in out


def test_launch_does_not_swallow_the_next_option_when_a_value_is_missing():
    out = extension.build_extension_launch(
        ["--session-dir", "--profile", "/p.json"], BASE, NEW, APP, PYTHON)
    assert "--profile" in out and "/p.json" in out
    # a trailing value option with no value at all is still dropped cleanly
    out = extension.build_extension_launch(["--session-dir"], BASE, NEW, APP, PYTHON)
    assert out == [PYTHON, APP, "--source", "live",
                   "--complete-session", BASE, "--session-dir", NEW]


def test_generated_arguments_are_accepted_by_the_real_launcher_parser():
    from tools.wizard.gui import app as launcher

    original = ["--session-dir", "/old/session", "--source", "replay",
                "--headless", "--profile", "/profiles/factory.json",
                "--mode", "quick"]
    argv = extension.build_extension_launch(original, BASE, NEW, APP, PYTHON)
    args = launcher.parse(argv[2:])
    assert args.source == "live"
    assert args.complete_session == BASE
    assert args.session_dir == NEW
    assert args.profile == "/profiles/factory.json"
    assert args.mode == "quick"
    # the old launch mode must not leak into the extension run
    assert args.headless is False


def test_launcher_path_prefers_the_adjacent_app_script(tmp_path):
    assert Path(extension.launcher_path(None)).name == "app.py"
    # a non-launcher argv[0] (pytest, a wrapper) must not be restarted
    assert Path(extension.launcher_path("pytest")).name == "app.py"
    assert Path(extension.launcher_path(__file__)).name == "app.py"
    # ...but a real app.py given as argv[0] is honoured
    real = tmp_path / "app.py"
    real.write_text("# launcher\n", encoding="utf-8")
    assert extension.launcher_path(str(real)) == str(real.resolve())


# ---------------------------------------------------------------------------
# directory derivation + refusal
# ---------------------------------------------------------------------------
def test_derive_extension_dir_is_a_sibling():
    assert extension.derive_extension_dir(BASE) == NEW
    assert extension.derive_extension_dir(BASE + "/") == NEW
    assert Path(NEW).parent == Path(BASE).parent


def test_extension_target_refusal(tmp_path):
    base = tmp_path / "cal-01"
    base.mkdir()
    target = Path(extension.derive_extension_dir(str(base)))
    assert target == tmp_path / "cal-01-ext"
    # missing: fine (the new process creates it)
    assert extension.extension_target_problem(str(target)) == ""
    # existing but empty: still fine, begin_extension writes its own manifest
    target.mkdir()
    assert extension.extension_target_problem(str(target)) == ""
    # non-empty: refused with a reason, not an exception
    (target / "session.json").write_text("{}", encoding="utf-8")
    problem = extension.extension_target_problem(str(target))
    assert problem
    assert "not empty" in problem

    # a file in the way is refused as well
    (tmp_path / "blocked-ext").write_text("x", encoding="utf-8")
    assert "file" in extension.extension_target_problem(
        str(tmp_path / "blocked-ext"))


# ---------------------------------------------------------------------------
# window integration (offscreen)
# ---------------------------------------------------------------------------
@pytest.fixture()
def qt():
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return app, QtCore, QtWidgets


class _Source:
    """Minimal frame-source stand-in: the window only reads info/continuous."""

    continuous = True

    def __init__(self):
        from tools.wizard import types
        self.info = types.SourceInfo(kind="synthetic", path="fixture")
        self.closed = False

    def stop(self, reason=""):
        return None

    def close(self):
        self.closed = True


def _make_controller(tmp_path, *, can_extend=True, extension_active=False):
    from tools.wizard.gui import controller_adapter

    class _ExtendableController(controller_adapter.DemoController):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.extension = ({"base_session_dir": "/older"}
                              if extension_active else None)
            self.begin_extension_calls = []

        def begin_extension(self, base):
            self.begin_extension_calls.append(base)
            return {"ok": True, "session_dir": str(self.session_dir)}

    cls = _ExtendableController if can_extend else controller_adapter.DemoController
    return cls(factory_profile="demo", session_dir=str(tmp_path / "current"),
               mode="standard", source_kind="synthetic")


def _make_window(qt, controller, *, launch_argv=None, headless=False):
    app, _QtCore, _QtWidgets = qt
    from tools.wizard.gui.modern_window import ModernMainWindow
    win = ModernMainWindow(controller, _Source(), launch_argv=launch_argv,
                           headless=headless)
    win.show()
    app.processEvents()
    return win


def _destroy(qt, win):
    app, QtCore, _QtWidgets = qt
    try:
        win._timer.stop()
        win._clock.stop()
    except Exception:
        pass
    try:
        win.close()
        win.deleteLater()
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
    except Exception:
        pass


def test_button_visible_only_in_an_idle_extendable_session(qt, tmp_path):
    ctl = _make_controller(tmp_path)
    win = _make_window(qt, ctl, launch_argv=["--source", "live"])
    try:
        assert win.btn_extension.objectName() == "ghost"
        assert "Complete calibration" in win.btn_extension.text()
        # the window itself is shown, so this is a real visibility check (and
        # a sibling control is visible, proving the check is not vacuous)
        assert win.isVisible()
        assert win.btn_start.isVisible()
        assert win.btn_extension.isVisible()

        # recording: the capture is bound, the entry point must disappear
        ctl.state = "recording"
        win.refresh()
        assert not win.btn_extension.isVisible()
        win._apply_state("recording")
        assert not win.btn_extension.isVisible()

        # back to idle: offered again
        ctl.state = "idle"
        win.refresh()
        assert win.btn_extension.isVisible()
    finally:
        _destroy(qt, win)


def test_button_hidden_without_launcher_arguments(qt, tmp_path, monkeypatch):
    _, _QtCore, QtWidgets = qt
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: pytest.fail(
            "the picker must not open without launcher arguments")))
    ctl = _make_controller(tmp_path)
    win = _make_window(qt, ctl, launch_argv=None)
    try:
        assert not win.btn_extension.isVisible()
        # invoking the slot directly must be a no-op as well
        win._on_complete_calibration()
    finally:
        _destroy(qt, win)


def test_button_hidden_when_the_controller_cannot_extend_or_is_extending(qt, tmp_path):
    plain = _make_controller(tmp_path, can_extend=False)
    win = _make_window(qt, plain, launch_argv=["--source", "live"])
    try:
        assert not win.btn_extension.isVisible()
    finally:
        _destroy(qt, win)

    extending = _make_controller(tmp_path, extension_active=True)
    win = _make_window(qt, extending, launch_argv=["--source", "live"])
    try:
        assert not win.btn_extension.isVisible()
    finally:
        _destroy(qt, win)


def test_button_hidden_and_silent_in_headless_mode(qt, tmp_path, monkeypatch):
    _, _QtCore, QtWidgets = qt
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: pytest.fail(
            "headless mode must not open a directory picker")))
    ctl = _make_controller(tmp_path)
    win = _make_window(qt, ctl, launch_argv=["--source", "live"], headless=True)
    try:
        assert not win.btn_extension.isVisible()
        win._on_complete_calibration()
    finally:
        _destroy(qt, win)


def test_click_releases_capture_then_relaunches_with_extension_flags(
        qt, tmp_path, monkeypatch):
    app, QtCore, QtWidgets = qt
    ctl = _make_controller(tmp_path)
    base = tmp_path / "cal-01"
    base.mkdir()
    factory = tmp_path / "factory.json"
    launch = ["--session-dir", str(tmp_path / "old-session"),
              "--source", "synthetic", "--scene", "distance",
              "--profile", str(factory), "--mode", "high"]
    win = _make_window(qt, ctl, launch_argv=launch)
    events = []
    calls = []
    warnings = []

    def fake_get_dir(parent, title, directory=""):
        events.append("pick")
        return str(base)

    def fake_question(parent, title, text, *args, **kwargs):
        events.append("question")
        assert "restart" in text and "read-only" in text
        return QtWidgets.QMessageBox.StandardButton.Yes

    def fake_warning(parent, title, text, *args, **kwargs):
        warnings.append(str(text))

    def fake_shutdown(*args, **kwargs):
        events.append(("shutdown", kwargs.get("timeout_s")))
        return True

    def fake_start_detached(program, arguments, *args, **kwargs):
        events.append("start")
        calls.append((program, list(arguments)))
        return True, 4242

    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(fake_get_dir))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(fake_question))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(fake_warning))
    monkeypatch.setattr(QtCore.QProcess, "startDetached",
                        staticmethod(fake_start_detached))
    monkeypatch.setattr(win, "shutdown", fake_shutdown)
    try:
        win.btn_extension.click()
        app.processEvents()

        assert not warnings
        assert events[0] == "pick" and events[1] == "question"
        assert ("shutdown", 10.0) in events, events
        assert "start" in events
        assert events.index(("shutdown", 10.0)) < events.index("start")
        assert calls, "the wizard was not restarted"

        program, args = calls[0]
        assert program == sys.executable
        assert args[0].endswith("app.py")
        expected = extension.derive_extension_dir(str(base))
        assert args[1:] == [
            "--profile", str(factory), "--mode", "high",
            "--source", "live",
            "--complete-session", str(base),
            "--session-dir", expected,
        ]
        # the old binding must be gone
        assert str(tmp_path / "old-session") not in args
        assert "synthetic" not in args
        # the old window is closed only after a successful restart
        assert not win.isVisible()
    finally:
        _destroy(qt, win)


def test_existing_non_empty_target_aborts_before_any_relaunch(
        qt, tmp_path, monkeypatch):
    app, QtCore, QtWidgets = qt
    ctl = _make_controller(tmp_path)
    base = tmp_path / "cal-02"
    base.mkdir()
    target = Path(extension.derive_extension_dir(str(base)))
    target.mkdir()
    (target / "session.json").write_text("{}", encoding="utf-8")
    win = _make_window(qt, ctl, launch_argv=["--source", "live"])
    warnings = []
    calls = []
    events = []

    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(base)))
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning",
        staticmethod(lambda parent, title, text, *a, **k: warnings.append(str(text))))
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: pytest.fail(
            "the operator must not be asked to confirm a refused target")))
    monkeypatch.setattr(
        QtCore.QProcess, "startDetached",
        staticmethod(lambda *a, **k: calls.append(a) or True))
    monkeypatch.setattr(win, "shutdown",
                        lambda *a, **k: events.append("shutdown") or True)
    try:
        win.btn_extension.click()
        app.processEvents()

        assert len(warnings) == 1
        assert "not empty" in warnings[0]
        assert not calls, "a non-empty target must never be overwritten"
        assert "shutdown" not in events
        assert win.isVisible()
    finally:
        _destroy(qt, win)


def test_live_unverified_release_aborts_before_restart(qt, tmp_path, monkeypatch):
    """A live scanner that did not verify its stop must not be double-opened."""
    app, QtCore, QtWidgets = qt
    ctl = _make_controller(tmp_path)
    base = tmp_path / "cal-live"
    base.mkdir()
    win = _make_window(qt, ctl, launch_argv=["--source", "live"])
    critical = []
    starts = []
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: str(base)))
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "critical",
        staticmethod(lambda parent, title, text, *a, **k: critical.append(str(text))))
    monkeypatch.setattr(win, "_source_kind", lambda: "live")
    monkeypatch.setattr(win, "shutdown", lambda *a, **k: False)
    monkeypatch.setattr(
        win, "_start_detached",
        staticmethod(lambda argv: starts.append(list(argv)) or (True, "")))
    try:
        win.btn_extension.click()
        app.processEvents()

        assert critical, "the operator must be told the stop was not verified"
        assert "could not be verified" in critical[0]
        assert not starts, "a second wizard must not start while the scanner may be live"
        assert win.isVisible(), "the window must stay open on a refusal"
        assert win.btn_extension.isEnabled(), "the operator can retry after checking"
    finally:
        _destroy(qt, win)
