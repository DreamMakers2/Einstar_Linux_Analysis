"""Einstar — one entry point for calibration, scanning and profiles.

The project grew two windows (the calibration wizard and the scanner).  A user
should not have to know which script to run, so this launcher is the single
command:

    .venv/bin/python tools/app.py --frames DIR            # scan a recording
    .venv/bin/python tools/app.py --source live           # calibrate live
    .venv/bin/python tools/app.py --profiles              # profile manager

Each button opens the corresponding window, which stays a separate top-level
window: the calibration wizard owns a capture process and a session directory,
and the scanner owns a different session.  Keeping them as windows rather than
reparenting their widgets into tabs avoids one window's lifecycle stomping on
the other's timers and worker threads.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

_HERE = Path(__file__).resolve()
REPO = _HERE.parents[1] if _HERE.parent.name == "tools" else _HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

try:  # Qt is optional for --help and for import-only use
    from PySide6 import QtCore, QtGui, QtWidgets
except Exception:  # pragma: no cover
    QtCore = QtGui = QtWidgets = None

FACTORY = Path(os.path.expanduser("~/einstar-private/einstar-profiles/factory-e10.json"))
STORE = Path(os.path.expanduser("~/einstar-private/calib-profiles"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Einstar launcher")
    ap.add_argument("--profile", default=str(FACTORY), help="factory profile JSON")
    ap.add_argument("--source", default="replay", choices=("live", "replay", "vendor"))
    ap.add_argument("--frames", default=str(Path(os.path.expanduser(
        "~/einstar-private/sweep-b/frames"))))
    ap.add_argument("--session-dir", default=str(Path.home() / "einstar-session"))
    ap.add_argument("--store", default=str(STORE), help="profile store root")
    ap.add_argument("--mode", default="standard", choices=("quick", "standard", "high"))
    ap.add_argument("--headless", action="store_true")
    return ap


class Launcher(QtWidgets.QMainWindow):
    """Single-window entry point; each button opens its own top-level window."""

    def __init__(self, args) -> None:
        super().__init__()
        self.args = args
        self.setWindowTitle("Einstar")
        self.resize(560, 420)
        try:
            self.setStyleSheet(theme.app_stylesheet())
        except Exception:
            pass
        central = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(central)
        title = QtWidgets.QLabel("EINSTAR · LINUX")
        title.setStyleSheet("font-size: 22px; font-weight: 600; padding: 6px;")
        lay.addWidget(title)
        lay.addWidget(QtWidgets.QLabel(
            "Calibrate the scanner, scan an object, or manage calibration profiles."))
        lay.addSpacing(10)
        self.btn_cal = QtWidgets.QPushButton("Calibration wizard…")
        self.btn_scan = QtWidgets.QPushButton("Scan…")
        self.btn_profiles = QtWidgets.QPushButton("Profiles…")
        for b in (self.btn_cal, self.btn_scan, self.btn_profiles):
            b.setMinimumHeight(42)
            lay.addWidget(b)
        self.info = QtWidgets.QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setMaximumHeight(120)
        lay.addWidget(self.info, 1)
        self.setCentralWidget(central)
        self._wizard = None
        self._scan = None
        self.btn_cal.clicked.connect(self.open_calibration)
        self.btn_scan.clicked.connect(self.open_scan)
        self.btn_profiles.clicked.connect(self.show_profiles)
        self.show_profiles()

    # -- calibration -------------------------------------------------
    def open_calibration(self) -> None:
        from wizard.gui import controller_adapter
        from wizard.gui.main_window import MainWindow
        try:
            source = controller_adapter.open_source(
                self.args.source, frames=self.args.frames, session_dir=self.args.session_dir)
            if controller_adapter.real_controller_available():
                controller = controller_adapter.make_controller(
                    factory_profile=self.args.profile, session_dir=self.args.session_dir,
                    mode=self.args.mode, source_kind=self.args.source)
            else:
                controller = controller_adapter.DemoController(
                    factory_profile=self.args.profile, session_dir=self.args.session_dir,
                    mode=self.args.mode, source_kind=self.args.source)
        except Exception as exc:
            self.info.appendPlainText(f"calibration unavailable: {exc}")
            return
        self._wizard = MainWindow(controller, source, headless=self.args.headless)
        self._wizard.show()
        self.info.appendPlainText(f"calibration window: source={self.args.source} "
                                  f"mode={self.args.mode}")

    # -- scanning ----------------------------------------------------
    def open_scan(self) -> None:
        from tools.scan.gui import ScanWindow
        from tools.scan.session import ScanConfig
        try:
            source = None
            frames = Path(self.args.frames)
            if self.args.source == "live":
                from tools.scan.live import CaptureSource
                capture_dir = Path(self.args.session_dir) / "scan-capture"
                source = CaptureSource(
                    session_dir=capture_dir,
                    capture_binary=REPO / "build" / "driver" / "einstar-capture-frames")
                frames = capture_dir / "frames"
            config = ScanConfig(
                frames_dir=frames, profile=Path(self.args.profile),
                out_dir=Path(self.args.session_dir) / "scan",
                recon_binary=REPO / "build" / "recon" / "recon_cli")
        except Exception as exc:
            self.info.appendPlainText(f"scan unavailable: {exc}")
            return
        self._scan = ScanWindow(config, count=24, source=source)
        self._scan.show()
        self.info.appendPlainText(f"scan window: {self.args.source} {frames}")

    # -- profiles ----------------------------------------------------
    def show_profiles(self) -> None:
        try:
            from wizard import lifecycle
            active = lifecycle.active_profile(self.args.store, serial=None) \
                if False else None
        except Exception:
            active = None
        lines = [f"factory profile: {self.args.profile}",
                 f"  exists: {Path(self.args.profile).is_file()}",
                 f"profile store: {self.args.store}",
                 f"  exists: {Path(self.args.store).is_dir()}"]
        if active:
            lines.append(f"active: {active.get('state')} "
                         f"revision {active.get('revision')}")
        self.info.setPlainText("\n".join(lines))

    def closeEvent(self, event) -> None:  # noqa: N802
        for win in (self._wizard, self._scan):
            try:
                if win is not None:
                    win.shutdown()
                    win.close()
            except Exception:
                pass
        super().closeEvent(event)



def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from wizard.gui import theme

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

    launcher = Launcher(args)
    launcher.show()
    if args.headless:
        app.processEvents()
        return 0
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
