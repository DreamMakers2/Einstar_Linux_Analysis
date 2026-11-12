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
            # imported here: the launcher class lives at module scope, where the
            # wizard package may not be importable (e.g. a Qt-free --help run)
            from wizard.gui import theme
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
    def _refresh_profile_text(self) -> None:
        """Short summary in the launcher's info panel (no dialog)."""
        try:
            from tools.wizard import profile_view
            serials = profile_view.list_serials(self.args.store)
            lines = [f"factory profile: {self.args.profile}",
                     f"  exists: {Path(self.args.profile).is_file()}",
                     f"profile store: {self.args.store}",
                     f"  devices: {', '.join(serials) if serials else 'none'}"]
            for serial in serials:
                summary = profile_view.store_summary(self.args.store, serial)
                active = summary["active"]
                lines.append(f"  {serial}: active {active['state']}"
                             f" rev {active['revision']},"
                             f" {len(summary['revisions'])} revision(s)")
            self.info.setPlainText("\n".join(lines))
        except Exception as exc:            # noqa: BLE001 - shown to the user
            self.info.setPlainText(f"profile store unavailable: {exc}")

    def show_profiles(self) -> None:
        """Open the profile manager (list, active state, rollback)."""
        self._refresh_profile_text()
        self._profiles = ProfilesDialog(self.args.store, self)
        self._profiles.show()

    def closeEvent(self, event) -> None:  # noqa: N802
        for win in (self._wizard, self._scan):
            try:
                if win is not None:
                    win.shutdown()
                    win.close()
            except Exception:
                pass
        super().closeEvent(event)



class ProfilesDialog(QtWidgets.QDialog):
    """List a profile store, show what is active, and roll back.

    Activation of a new candidate stays with the calibration flow (it needs a
    validated candidate and its verdict); this dialog is the management view:
    what exists, what is active, is it intact, and how to go back.
    """

    def __init__(self, store_root: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calibration profiles")
        self.resize(760, 420)
        self.store_root = store_root
        lay = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Device:"))
        self.serial_box = QtWidgets.QComboBox()
        self.serial_box.setMinimumWidth(220)
        top.addWidget(self.serial_box)
        top.addStretch(1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        top.addWidget(self.refresh_btn)
        lay.addLayout(top)

        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["revision", "state", "verified", "created"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.rollback_sel = QtWidgets.QPushButton("Roll back to selected")
        self.rollback_factory = QtWidgets.QPushButton("Roll back to factory")
        close = QtWidgets.QPushButton("Close")
        buttons.addWidget(self.rollback_sel)
        buttons.addWidget(self.rollback_factory)
        buttons.addStretch(1)
        buttons.addWidget(close)
        lay.addLayout(buttons)

        self.refresh_btn.clicked.connect(self.reload)
        self.serial_box.currentIndexChanged.connect(lambda _i: self.reload())
        self.rollback_sel.clicked.connect(self.rollback_selected)
        self.rollback_factory.clicked.connect(lambda: self.rollback(None))
        close.clicked.connect(self.accept)

        self._populate_serials()
        self.reload()

    # -- data ------------------------------------------------------------
    def _populate_serials(self) -> None:
        from tools.wizard import profile_view
        serials = profile_view.list_serials(self.store_root)
        current = self.serial_box.currentText()
        self.serial_box.blockSignals(True)
        self.serial_box.clear()
        self.serial_box.addItems(serials or ["<none>"])
        if current in serials:
            self.serial_box.setCurrentText(current)
        self.serial_box.blockSignals(False)

    def reload(self) -> None:
        from tools.wizard import profile_view
        serial = self.serial_box.currentText()
        if not serial or serial == "<none>":
            self.status.setText(f"no device store in {self.store_root}")
            self.table.setRowCount(0)
            self.rollback_sel.setEnabled(False)
            self.rollback_factory.setEnabled(False)
            return
        summary = profile_view.store_summary(self.store_root, serial)
        active = summary["active"]
        state = active["state"]
        rev = active["revision"]
        where = "factory" if state == "factory" else f"revision {rev}"
        healthy = "intact" if active["integrity_ok"] else f"INTEGRITY: {active['reason']}"
        self.status.setText(
            f"active: {where} ({state}, {healthy})    revisions: "
            f"{len(summary['revisions'])}    factory snapshot: "
            f"{'present' if summary['factory_present'] else 'MISSING'}")
        rows = summary["revisions"]
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("index", "state", "verified", "created")):
                item = QtWidgets.QTableWidgetItem(str(row[key]))
                if key == "index" and rev is not None and int(row["index"]) == int(rev):
                    item.setText(f"{row['index']}  (active)")
                self.table.setItem(i, j, item)
        self.rollback_sel.setEnabled(bool(rows) and state != "factory")
        self.rollback_factory.setEnabled(state != "factory")

    # -- actions ---------------------------------------------------------
    def rollback(self, revision) -> None:
        from tools.wizard import lifecycle
        serial = self.serial_box.currentText()
        if not serial or serial == "<none>":
            return
        try:
            result = lifecycle.rollback(self.store_root, serial, revision)
        except Exception as exc:            # noqa: BLE001 - shown to the user
            QtWidgets.QMessageBox.warning(self, "Rollback failed", str(exc))
            return
        self.status.setText(
            f"rolled back: state={result.get('state')} "
            f"revision={result.get('revision')} "
            f"({result.get('reason') or 'ok'})")
        self.reload()

    def rollback_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        index = int(str(item.text()).split()[0])
        self.rollback(index)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

    launcher = Launcher(args)
    launcher.show()
    if args.headless:
        app.processEvents()
        return 0
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
