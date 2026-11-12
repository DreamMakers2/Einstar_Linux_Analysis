"""Qt scanning window: live preview, accumulating model, tracking status.

It drives the same `ScanSession` as the command-line scanner, so there is one
implementation of the pipeline and the GUI cannot drift from it.

The point-cloud panel is a painter-based projection, not OpenGL: it has to run
offscreen for the tests, and at a few thousand preview points a QPainter view is
fast enough and has no driver dependency.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))
sys.path.insert(0, str(_HERE.parents[1]))

from tools.scan.session import ScanConfig, ScanSession  # noqa: E402
from tools.scan.view3d import MeshView  # noqa: E402
from wizard.gui import theme  # noqa: E402


class CloudView(QtWidgets.QWidget):
    """Depth-coloured projection of the accumulating preview cloud."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self._points: np.ndarray | None = None
        self._caption = "no data"
        self._distance = QtCore.QPointF(0.0, 1.0)

    def set_cloud(self, points) -> None:
        self._points = None if points is None else np.asarray(points, float)
        self.update()

    def set_caption(self, text: str) -> None:
        self._caption = str(text)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(getattr(theme, "BG_TILE", theme.PANEL)))
        rect = self.rect().adjusted(10, 10, -10, -10)
        painter.setPen(QtGui.QColor(theme.MUTED))
        painter.drawRect(rect)
        if self._points is None or len(self._points) == 0:
            painter.drawText(rect, QtCore.Qt.AlignCenter, self._caption)
            painter.end()
            return
        pts = self._points
        # isometric-ish projection: x right, y up, z into the screen
        x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
        depth = float(np.median(z)) if len(z) else 1.0
        depth = max(depth, 1.0)
        sx = rect.center().x() + (x) * (rect.width() * 0.35 / max(200.0, depth))
        sy = rect.center().y() - (y) * (rect.width() * 0.35 / max(200.0, depth))
        lo, hi = float(np.percentile(z, 5)), float(np.percentile(z, 95))
        span = max(hi - lo, 1e-6)
        for px, py, pz in zip(sx, sy, z):
            if not (rect.left() <= px <= rect.right() and rect.top() <= py <= rect.bottom()):
                continue
            t = min(max((pz - lo) / span, 0.0), 1.0)
            colour = QtGui.QColor.fromHsvF(0.58 - 0.5 * t, 0.85, 0.95)
            painter.setPen(QtGui.QPen(colour, 1.6))
            painter.drawPoint(QtCore.QPointF(float(px), float(py)))
        painter.setPen(QtGui.QColor(theme.TEXT))
        painter.drawText(rect.adjusted(6, 4, -6, -4), QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop,
                         self._caption)
        painter.end()


class ScanWorker(QtCore.QThread):
    """Runs the scan in a thread: one Qt signal per frame, one at the end."""

    progress = QtCore.Signal(dict)
    finished_scan = QtCore.Signal(dict)
    failed = QtCore.Signal(str)

    def __init__(self, config: ScanConfig, start: int, count: int, parent=None,
                 source=None) -> None:
        super().__init__(parent)
        self.config = config
        # NB: do not shadow QThread.start()/stop() with attributes
        self.first_index = int(start)
        self.frame_count = int(count)
        self._stop = False
        self._pause = False
        self.session: ScanSession | None = None
        #: refresh the live mesh every N processed frames
        self.mesh_every = 5
        #: optional live CaptureSource; when set the worker owns the capture
        self.source = source

    def _maybe_preview_mesh(self, snap: dict) -> None:
        if self.session is None:
            return
        n = int(snap.get("frames_integrated") or 0)
        if n > 0 and n % max(1, int(self.mesh_every)) == 0:
            preview = self.session.preview_mesh()
            if preview is not None:
                snap["mesh"] = preview

    def stop(self) -> None:
        self._stop = True

    def pause(self, value: bool = True) -> None:
        self._pause = bool(value)

    def run(self) -> None:  # noqa: D401 - QThread API
        try:
            self.session = ScanSession(self.config)
            if self.source is not None:
                started = self.source.start()
                if not started.get("started"):
                    self.failed.emit(f"capture failed to start: "
                                     f"{started.get('error', 'unknown')}")
                    return
                try:
                    for frame_id in self.source.ids(max_frames=self.frame_count):
                        if self._stop:
                            break
                        while self._pause and not self._stop:
                            QtCore.QThread.msleep(50)
                        self.session.process(int(frame_id))
                        snap = self.session.snapshot()
                        snap["capture"] = self.source.snapshot()
                        self._maybe_preview_mesh(snap)
                        self.progress.emit(snap)
                finally:
                    self.source.stop()
            else:
                for i in range(self.first_index, self.first_index + self.frame_count):
                    if self._stop:
                        break
                    while self._pause and not self._stop:
                        QtCore.QThread.msleep(50)
                    self.session.process(i)
                    snap = self.session.snapshot()
                    self._maybe_preview_mesh(snap)
                    self.progress.emit(snap)
            report = self.session.finish()
            self.finished_scan.emit(report)
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if self.session is not None:
                self.session.close()


class ScanWindow(QtWidgets.QMainWindow):
    def __init__(self, config: ScanConfig, *, count: int = 8,
                 source=None) -> None:
        super().__init__()
        self.config = config
        self.count = int(count)
        self.source = source
        self.worker: ScanWorker | None = None
        self.last_snapshot: dict = {}
        self.setWindowTitle("Einstar — scan")
        self.resize(1280, 760)
        try:
            self.setStyleSheet(theme.app_stylesheet())
        except Exception:
            if hasattr(theme, "STYLESHEET"):
                self.setStyleSheet(theme.STYLESHEET)

        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        header = QtWidgets.QLabel("EINSTAR · SCAN")
        header.setObjectName("Header")
        outer.addWidget(header)

        body = QtWidgets.QHBoxLayout()
        left = QtWidgets.QVBoxLayout()
        self.view = MeshView()
        left.addWidget(self.view, 1)
        self.status = QtWidgets.QPlainTextEdit()
        self.status.setReadOnly(True)
        self.status.setMaximumHeight(150)
        left.addWidget(self.status)
        body.addLayout(left, 3)

        right = QtWidgets.QVBoxLayout()
        self.controls = QtWidgets.QGroupBox("Scan")
        cl = QtWidgets.QVBoxLayout(self.controls)
        self.btn_start = QtWidgets.QPushButton(f"Scan {self.count} frames")
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            cl.addWidget(b)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        right.addWidget(self.controls)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        right.addWidget(self.log, 1)
        body.addLayout(right, 2)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        self.btn_start.clicked.connect(self.start_scan)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop.clicked.connect(self.stop_scan)

    # -- slots ------------------------------------------------------------
    @QtCore.Slot()
    def start_scan(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        mode = "live capture" if self.source is not None else "recorded frames"
        self.log.appendPlainText(f"scan: {mode} {self.config.frames_dir} "
                                 f"count={self.count} voxel={self.config.voxel_mm} mm")
        self.worker = ScanWorker(self.config, 0, self.count, source=self.source)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_scan.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)

    @QtCore.Slot()
    def toggle_pause(self) -> None:
        if self.worker is None:
            return
        paused = self.btn_pause.text().startswith("Pause")
        self.worker.pause(paused)
        self.btn_pause.setText("Resume" if paused else "Pause")

    @QtCore.Slot()
    def stop_scan(self) -> None:
        if self.worker is not None:
            self.worker.stop()

    @QtCore.Slot(dict)
    def on_progress(self, snap: dict) -> None:
        self.last_snapshot = snap
        mesh = snap.get("mesh")
        if mesh is not None:
            verts, faces = mesh
            self.view.set_mesh(verts, faces)
        elif snap.get("cloud"):
            self.view.set_points(snap.get("cloud"))
        vol = snap.get("volume") or {}
        novelty = snap.get("novelty")
        new = "" if novelty is None else f"  new surface {100.0 * novelty:.0f}%"
        coverage = snap.get("coverage") or {}
        if coverage.get("state") == "converged":
            new += "  · converged (nothing new for a while)"
        caption = (f"frames {snap['frames_integrated']}  rejected {snap['frames_rejected']}"
                   f"  voxels {vol.get('observed', 0):,}{new}"
                   f"   drag to orbit, wheel to zoom")
        self.view.set_caption(caption)
        last = snap.get("last") or {}
        self.status.setPlainText(
            f"frames      {snap['frames_processed']} (integrated {snap['frames_integrated']}, "
            f"rejected {snap['frames_rejected']})\n"
            f"depth p50   {last.get('depth_p50_mm')} mm   sigma {last.get('depth_sigma_p50_mm')} mm\n"
            f"points      {last.get('points')}\n"
            f"tracking    {'ok' if last.get('tracking_ok') else last.get('tracking_reason') or 'first frame'}"
            f"   step {last.get('translation_mm')} mm / {last.get('rotation_deg')} deg\n"
            f"quality     rms {last.get('rms_mm')} mm   inliers "
            f"{(last.get('inlier_ratio') or 0) * 100:.0f}%   condition {last.get('condition')}\n"
            f"elapsed     {snap['elapsed_s']} s")

    @QtCore.Slot(dict)
    def on_finished(self, report: dict) -> None:
        self.log.appendPlainText(
            f"done: frames {report.get('frames')} rejected {report.get('rejected')} "
            f"vertices {report.get('vertices')} faces {report.get('faces')}")
        statuses = report.get("frames_status") or []
        last_new = None
        for status in reversed(statuses):
            if status.get("novelty") is not None:
                last_new = float(status["novelty"])
                break
        if last_new is not None:
            self.log.appendPlainText(
                f"new surface on the last frame: {100.0 * last_new:.1f}%")
        self.log.appendPlainText(f"mesh: {report.get('obj')}")
        self.load_mesh_file(report.get("ply") or report.get("obj"))
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)

    def load_mesh_file(self, path) -> bool:
        """Show a mesh (our ASCII PLY) in the 3-D view."""
        if not path:
            return False
        try:
            from tools.scan.export import read_ply_mesh
            verts, faces = read_ply_mesh(path)
        except Exception as exc:
            self.log.appendPlainText(f"mesh load failed: {exc}")
            return False
        self.view.set_mesh(verts, faces)
        self.view.set_caption(f"{len(verts):,} vertices  {len(faces):,} faces")
        return True

    def load_project(self, path) -> bool:
        """Open a saved scan project and display its mesh."""
        try:
            from tools.scan.export import load_project
            doc = load_project(path)
        except Exception as exc:
            self.log.appendPlainText(f"project load failed: {exc}")
            return False
        mesh = (doc.get("mesh") or {})
        self.log.appendPlainText(f"project: {path}")
        report = doc.get("report") or {}
        self.log.appendPlainText(
            f"  frames {report.get('frames')} rejected {report.get('rejected')} "
            f"vertices {report.get('vertices')} faces {report.get('faces')}")
        return self.load_mesh_file(mesh.get("ply") or mesh.get("obj"))

    @QtCore.Slot(str)
    def on_failed(self, message: str) -> None:
        self.log.appendPlainText(f"ERROR {message}")
        if os.environ.get("EINSTAR_SCAN_DEBUG"):
            print(f"scan worker failed: {message}", file=sys.stderr, flush=True)
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(5000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.shutdown()
        super().closeEvent(event)

    def snapshot_png(self, path: str | Path) -> bool:
        image = self.grab()
        return bool(image.save(str(path)))
