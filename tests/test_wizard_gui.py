"""End-to-end GUI tests, run in a subprocess.

Qt (PySide6) does not survive being initialised inside the same interpreter as
the rest of this suite, so every test here drives the real entry point
``tools/wizard/gui/app.py`` as a child process and checks its output and the
snapshot it writes.  That is also a more honest test: it exercises the exact
command a user runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "tools" / "wizard" / "gui" / "app.py"
PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.is_file():  # pragma: no cover - fall back to the running one
    PYTHON = Path(sys.executable)


def _run(args, timeout=300):
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return subprocess.run([str(PYTHON), str(APP), *args],
                          cwd=str(REPO), env=env, timeout=timeout,
                          capture_output=True, text=True)


def _summary(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("wizard: "):
            out = {}
            for part in line[len("wizard: "):].split():
                if "=" in part:
                    key, _, value = part.partition("=")
                    out[key] = value
            return out
    return {}


def test_app_help_is_available():
    proc = subprocess.run([str(PYTHON), str(APP), "--help"], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    for token in ("--source", "--frames", "--session-dir", "--headless", "--demo"):
        assert token in proc.stdout


@pytest.mark.skipif(
    not (Path.home() / "einstar-private" / "sweep-b" / "frames").is_dir(),
    reason="replay capture not available")
def test_replay_session_snapshot(tmp_path):
    """Real controller + real frames: renders, decides, shuts down cleanly."""
    frames = Path.home() / "einstar-private" / "sweep-b" / "frames"
    out = tmp_path / "session"
    shot = tmp_path / "wizard.png"
    proc = _run(["--source", "replay", "--frames", str(frames), "--pace", "40",
                 "--mode", "quick", "--session-dir", str(out),
                 "--headless", "--duration", "12", "--snapshot-out", str(shot)])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert shot.is_file() and shot.stat().st_size > 20_000
    summary = _summary(proc.stdout)
    assert summary.get("state") in {"recording", "cancelled", "done"}
    assert int(summary.get("accepted", "0")) >= 1
    assert int(summary.get("rejected", "0")) >= 1
    assert summary.get("ready") == "False", summary
    for name in ("session.json", "decisions.jsonl", "groups"):
        assert (out / name).exists(), name


def test_demo_controller_renders_without_data(tmp_path):
    """Snapshot smoke test on an empty frame directory (no groups, no crash)."""
    shot = tmp_path / "demo.png"
    proc = _run(["--source", "replay", "--frames", str(tmp_path), "--demo",
                 "--session-dir", str(tmp_path / "s"),
                 "--headless", "--duration", "2", "--snapshot-out", str(shot)])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert shot.is_file() and shot.stat().st_size > 10_000


def test_vendor_corpus_session_if_available(tmp_path):
    corpus = Path.home() / "einstar-private" / "vendor-calib-rapid"
    if not corpus.is_dir():
        pytest.skip("vendor corpus not available")
    out = tmp_path / "vendor"
    shot = tmp_path / "vendor.png"
    proc = _run(["--source", "vendor", "--frames", str(corpus), "--pace", "5",
                 "--mode", "standard", "--session-dir", str(out), "--finish",
                 "--headless", "--duration", "20", "--snapshot-out", str(shot)],
                timeout=900)
    assert proc.returncode == 0, proc.stderr[-2000:]
    summary = _summary(proc.stdout)
    assert int(summary.get("accepted", "0")) >= 5, summary
    data = json.loads((out / "manifest.json").read_text())
    accepted = data.get("accepted") or data.get("views") or []
    assert len(accepted) >= 5
    # the finish path must produce a candidate and a full gate report
    candidate = out / "candidate" / "profile.json"
    validation = out / "candidate" / "validation.json"
    assert candidate.is_file() and validation.is_file()
    profile = json.loads(candidate.read_text())
    assert profile.get("validation") == "pending"
    report = json.loads(validation.read_text())
    lc = report.get("lifecycle_validate") or {}
    assert lc.get("gates"), lc
    assert (out / "report.md").is_file()


def test_closing_a_window_during_a_long_solve_never_aborts(tmp_path):
    """A close during a solver pause must defer, not destroy a running thread.

    Reproduced the reported crash: ``worker.wait(5000)`` timed out while the
    solver was busy, the window was destroyed, and Qt aborted the process with
    the thread still alive.  The window now refuses the close and retries.
    """
    script = tmp_path / "close_during_solve.py"
    script.write_text(textwrap.dedent('''
        import sys, threading, time
        sys.path.insert(0, "tools"); sys.path.insert(0, ".")
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication([])
        from tools.wizard.gui import controller_adapter
        from tools.wizard.gui.main_window import MainWindow
        import os
        frames = os.path.expanduser("~/einstar-private/sweep-b/frames")
        source = controller_adapter.open_source("replay", frames=frames)
        ctl = controller_adapter.make_controller(
            factory_profile=os.path.expanduser(
                "~/einstar-private/einstar-profiles/factory-e10.json"),
            session_dir=sys.argv[1], mode="standard", source_kind="replay")
        win = MainWindow(ctl, source, pace_hz=10.0, headless=True)
        win.resize(1000, 700); win.show(); app.processEvents()
        running = threading.Event()
        real = ctl.session.solve_fn
        def slow(*a, **k):
            running.set(); time.sleep(3.0); return real(*a, **k)
        ctl.session.solve_fn = slow
        win._on_start()
        t0 = time.time()
        while time.time() - t0 < 40 and not running.is_set():
            app.processEvents(); time.sleep(0.01)
        assert running.is_set(), "never reached a solve"
        t = time.time(); win.close(); app.processEvents()
        took = time.time() - t
        print("close-took", round(took, 2))
        print("visible-after-close", bool(win.isVisible()))
        deadline = time.time() + 30
        while time.time() < deadline:
            app.processEvents(); time.sleep(0.02)
            if win.worker is None or not win.worker.isRunning():
                break
        win.shutdown(timeout_s=30.0)
        print("worker-finished", win.worker is None or not win.worker.isRunning())
    '''))
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.pop("DISPLAY", None)
    proc = subprocess.run([str(PYTHON), str(script), str(tmp_path / "session")],
                          cwd=str(REPO), env=env, capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = proc.stdout
    assert "close-took 0.0" in out or "close-took 0.1" in out, out
    assert "visible-after-close True" in out, out
    assert "worker-finished True" in out, out


def test_end_to_end_rehearsal_refuses_an_insufficient_session(tmp_path):
    """Start, capture, finish: a candidate is written and correctly refused.

    The factory profile must stay byte-identical, and the refusal must name the
    missing dimensions rather than a percentage.
    """
    import hashlib
    script = tmp_path / "e2e.py"
    script.write_text(textwrap.dedent('''
        import hashlib, json, sys, time
        sys.path.insert(0, "tools"); sys.path.insert(0, ".")
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication([])
        from tools.wizard.gui import controller_adapter
        from tools.wizard.gui.main_window import MainWindow
        import os, shutil
        frames = os.path.expanduser("~/einstar-private/sweep-b/frames")
        factory = os.path.expanduser(
            "~/einstar-private/einstar-profiles/factory-e10.json")
        session = sys.argv[1]
        shutil.rmtree(session, ignore_errors=True)
        before = hashlib.sha256(open(factory, "rb").read()).hexdigest()
        src = controller_adapter.open_source("replay", frames=frames)
        ctl = controller_adapter.make_controller(
            factory_profile=factory, session_dir=session,
            mode="standard", source_kind="replay")
        win = MainWindow(ctl, src, headless=True, max_groups=30)
        win.show(); app.processEvents(); win._on_start()
        t0 = time.time()
        while time.time() - t0 < 180 and win.worker and win.worker.isRunning():
            app.processEvents(); time.sleep(0.01)
        app.processEvents()
        win._on_finish(); app.processEvents()
        cand = ctl.paths.candidate_dir
        files = sorted(p.name for p in cand.glob("*")) if cand.is_dir() else []
        after = hashlib.sha256(open(factory, "rb").read()).hexdigest()
        v = {}
        if (cand / "validation.json").is_file():
            v = json.loads((cand / "validation.json").read_text())
        print("files", files)
        print("factory-identical", before == after)
        print("state", ctl.snapshot().get("state"))
        print("passed", v.get("passed"))
        print("reasons", len(v.get("reasons") or []))
        win.shutdown(); win.close()
    '''))
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.pop("DISPLAY", None)
    proc = subprocess.run([str(PYTHON), str(script), str(tmp_path / "session")],
                          cwd=str(REPO), env=env, capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = proc.stdout
    assert "profile.json" in out and "validation.json" in out, out
    assert "factory-identical True" in out, out
    assert "state done" in out, out
    assert "passed False" in out, out
    assert "reasons " in out and "reasons 0" not in out, out


def test_finish_does_not_freeze_the_window(tmp_path):
    """The final solve runs on its own thread; the UI keeps painting.

    Measured 38.3 s at 35 views.  Run inline it froze the window for the whole
    time - no repaint, no feedback - which is indistinguishable from a crash.
    """
    script = tmp_path / "finish.py"
    script.write_text(textwrap.dedent('''
        import sys, time
        sys.path.insert(0, "tools"); sys.path.insert(0, ".")
        from PySide6 import QtCore, QtWidgets
        app = QtWidgets.QApplication([])
        from tools.wizard.gui import controller_adapter
        from tools.wizard.gui import main_window as mw

        class SlowController(controller_adapter.DemoController):
            def finish(self):
                time.sleep(3.0)                  # a stand-in for the real solve
                return {"ok": True, "profile": {"path": "x"}}

        ctl = SlowController(factory_profile="demo", session_dir="demo",
                             source_kind="replay")
        src = controller_adapter.open_source("synthetic", scene="diverse")
        win = mw.MainWindow(ctl, src, headless=True)
        win.show(); app.processEvents()

        ticks = {"n": 0}
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
        timer.start(50)

        win._on_finish()
        t0 = time.time()
        while time.time() - t0 < 2.0:
            app.processEvents(); time.sleep(0.01)
        during = ticks["n"]
        running = win._finish_worker is not None and win._finish_worker.isRunning()
        print("ticks-during", during)
        print("worker-running", running)
        deadline = time.time() + 20
        while time.time() < deadline and win._finish_worker is not None:
            app.processEvents(); time.sleep(0.01)
        print("ticks-after", ticks["n"])
        print("controls-enabled", win.controls.isEnabled())
        win.shutdown(); win.close()
    '''))
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.pop("DISPLAY", None)
    proc = subprocess.run([str(PYTHON), str(script)], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = proc.stdout
    during = int(out.split("ticks-during")[1].split()[0])
    after = int(out.split("ticks-after")[1].split()[0])
    assert during >= 20, out          # 2 s at 50 ms -> ~40 ticks if responsive
    assert "worker-running True" in out, out
    assert after > during, out
    assert "controls-enabled True" in out, out
