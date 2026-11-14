"""Operator-facing calibration window.

This is a replacement for the engineering dashboard, not a restyle of it.
The operator sees three large live feeds, one instruction, one progress strip
and one primary action.  Everything else lives in a separate Advanced dialog.

The window never performs acquisition or calibration work on the GUI thread.
``SessionWorker`` owns the source loop, ``DecisionRunner`` owns detection, and
``FinishWorker`` owns the final solve.  Preview is fed by the newest complete
group before any decision touches it, so the feeds hold the device cadence
instead of the detector's cadence.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from . import extension as extension_mod
from . import panels, plate3d, status, theme, tiles, worker
from wizard import exposure as exposure_mod

#: coverage dimension key -> (short label, explanation)
DIMENSIONS = {
    "distance_bands": ("distance", "near, middle and far distance"),
    "tilt_two_axes": ("tilt", "tilt the plate in both directions"),
    "roll_sectors": ("rotation", "rotate the plate in its own plane"),
    "image_regions": ("position", "place the plate in different image areas"),
    "stereo_overlap": ("overlap", "both IR cameras must see the plate"),
    "colour_participation": ("colour", "the colour camera must see the plate"),
    "quality": ("stillness", "a few stationary, in-focus dwell views"),
    "scale_artefact": ("scale", "the plate appears at two or more distances"),
}


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _qimage_gray(arr: np.ndarray) -> QtGui.QImage:
    a = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = a.shape[:2]
    return QtGui.QImage(a.data, w, h, int(a.strides[0]),
                        QtGui.QImage.Format.Format_Grayscale8)


def _qimage_bgr(arr: np.ndarray) -> QtGui.QImage:
    a = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = a.shape[:2]
    return QtGui.QImage(a.data, w, h, int(a.strides[0]),
                        QtGui.QImage.Format.Format_BGR888)


class FeedView(QtWidgets.QWidget):
    """Large, calm camera feed.  No engineering overlay by default."""

    def __init__(self, title: str, accent: str = theme.INFO, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 220)
        self._title = title
        self._accent = accent
        self._img: QtGui.QImage | None = None
        self._buf = None
        self._status = "waiting"
        self._markers: np.ndarray | None = None
        self._show_markers = False
        self._ready = False
        self._last_shape = None
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_frame(self, image, *, colour: bool = False) -> None:
        if image is None:
            return
        if colour:
            # Grey-world is enough to remove the Bayer/illuminator cast.  Do
            # it on a subsample of the frame, then apply three gains: a full
            # float conversion of 3.9M pixels is measurable at 10 Hz and this
            # is preview-only (calibration always uses the raw frames).
            try:
                small = np.asarray(image[::8, ::8], np.float32)
                means = small.reshape(-1, 3).mean(axis=0) + 1e-3
                gains = np.clip(means.mean() / means, 0.6, 2.2).astype(np.float32)
                image = np.clip(np.asarray(image, np.float32) * gains, 0, 255).astype(np.uint8)
            except Exception:
                pass
        self._buf = image
        self._img = _qimage_bgr(image) if colour else _qimage_gray(image)
        self._last_shape = tuple(np.shape(image)[:2])
        self.update()

    def set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.update()

    def set_markers(self, points, enabled: bool) -> None:
        self._markers = None if points is None else np.asarray(points, float)
        self._show_markers = bool(enabled)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 16, 16)
        p.fillPath(path, QtGui.QColor("#0b0d11"))
        if self._img is not None and not self._img.isNull():
            p.save()
            p.setClipPath(path)
            scaled = self._img.scaled(
                rect.size().toSize(),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
            x = rect.left() + (rect.width() - scaled.width()) / 2.0
            y = rect.top() + (rect.height() - scaled.height()) / 2.0
            p.drawImage(QtCore.QPointF(x, y), scaled)
            if self._show_markers and self._markers is not None and len(self._markers):
                sx = scaled.width() / max(self._img.width(), 1)
                sy = scaled.height() / max(self._img.height(), 1)
                p.setPen(QtGui.QPen(QtGui.QColor(75, 220, 160, 200), 1.1))
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                for u, v in self._markers:
                    p.drawEllipse(QtCore.QPointF(x + u * sx, y + v * sy), 4.5, 4.5)
            p.restore()
        else:
            p.setPen(QtGui.QColor(theme.MUTED))
            f = self.font(); f.setPointSize(10); p.setFont(f)
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                       "waiting for the scanner…")

        # title and state, deliberately small and quiet
        p.setPen(QtGui.QColor(theme.TEXT))
        f = self.font(); f.setPointSize(9); f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 0.8)
        p.setFont(f)
        p.drawText(QtCore.QRectF(rect.left() + 14, rect.top() + 10,
                                 rect.width() - 28, 18),
                   QtCore.Qt.AlignmentFlag.AlignLeft, self._title.upper())
        p.setPen(QtGui.QColor(self._accent if self._img is not None else theme.MUTED))
        p.drawText(QtCore.QRectF(rect.left() + 14, rect.top() + 10,
                                 rect.width() - 28, 18),
                   QtCore.Qt.AlignmentFlag.AlignRight, self._status)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 16), 1))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 16, 16)


class InstructionCard(QtWidgets.QFrame):
    """One large instruction and one movement icon.  Nothing else."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(8)
        top = QtWidgets.QHBoxLayout()
        self.kicker = QtWidgets.QLabel("MOVE THE PLATE")
        self.kicker.setObjectName("kicker")
        top.addWidget(self.kicker)
        top.addStretch(1)
        self.ready_pill = QtWidgets.QLabel("NOT READY")
        self.ready_pill.setObjectName("pill")
        top.addWidget(self.ready_pill)
        lay.addLayout(top)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(16)
        self.icon = MovementIcon()
        self.icon.setFixedSize(118, 118)
        body.addWidget(self.icon, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(6)
        self.headline = QtWidgets.QLabel("Place the plate in front of the scanner")
        self.headline.setObjectName("headline")
        self.headline.setWordWrap(True)
        self.headline.setMinimumHeight(76)
        text.addWidget(self.headline)
        self.detail = QtWidgets.QLabel("The camera will start automatically.")
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        text.addWidget(self.detail)
        body.addLayout(text, 1)
        lay.addLayout(body)

        self.feedback = QtWidgets.QLabel("")
        self.feedback.setObjectName("feedback")
        self.feedback.setMinimumHeight(30)
        self.feedback.setStyleSheet("background: transparent;")
        lay.addWidget(self.feedback)
        self._feedback_serial = 0

    @staticmethod
    def short_instruction(text: str) -> str:
        """Extract one action from the guidance engine's full sentence.

        The coverage planner deliberately explains *all* the differences between
        the current and target pose; that is useful evidence, not a good
        headline.  The operator gets one verb and one direction here, and the
        exact target stays in the secondary line.
        """
        t = str(text or "").lower()
        if not t.strip():
            return "Place the plate in front of the scanner"
        if "press start" in t:
            return "Press Start to begin"
        if t.startswith("paused") or "paused —" in t:
            return "Paused — resume to continue"
        if "source ended" in t:
            return "Not enough coverage"
        if "recording complete" in t:
            return "Recording complete — finish"
        if "validated profile" in t:
            return "Validated profile written"
        if "candidate written" in t:
            return "Candidate not validated"
        if "building the profile" in t:
            return "Building profile"
        if "needs attention" in t:
            return "Needs attention — see message"
        if "hold still" in t or "too fast" in t:
            return "Hold still for a moment"
        if "closer" in t:
            return "Move the plate closer"
        if "farther" in t or "further" in t:
            return "Move the plate farther away"
        if "tilt" in t:
            if "left" in t and "right" in t:
                return "Tilt the plate left, then right"
            if "left" in t:
                return "Tilt the plate left"
            if "right" in t:
                return "Tilt the plate right"
            if "up" in t:
                return "Tilt the plate up"
            if "down" in t:
                return "Tilt the plate down"
            return "Tilt the plate"
        if "rotate the plate" in t or "roll" in t:
            return "Rotate the plate in your hand"
        if "put the plate centre" in t or "plate centre in" in t:
            return "Move the plate to the highlighted area"
        if "move right" in t:
            return "Move the plate right"
        if "move left" in t:
            return "Move the plate left"
        if "move up" in t:
            return "Move the plate up"
        if "move down" in t:
            return "Move the plate down"
        if "ready" in t or "coverage complete" in t:
            return "Coverage complete"
        if "no admissible" in t or "check the plate" in t:
            return "Show the plate to both cameras"
        return "Move the plate to a new pose"

    def set_instruction(self, text: str, detail: str = "") -> None:
        full = text or "Move the plate to a new position"
        short = self.short_instruction(full)
        self.headline.setText(short)
        # The one-line target (distance/tilt/roll/region) is useful; the whole
        # planner sentence is not.  It stays in the tooltip for diagnostics.
        self.detail.setText(detail.strip())
        self.setToolTip(full)
        self.icon.set_instruction(short)
        size = 34 if len(short) <= 24 else (28 if len(short) <= 40 else 23)
        self.headline.setStyleSheet(f"font-size: {size}px; font-weight: 700;")

    def set_ready(self, ready: bool, missing: list[str]) -> None:
        if ready:
            self.ready_pill.setText("READY TO FINISH")
            self.ready_pill.setStyleSheet(
                f"color: {theme.OK}; border-color: {theme.OK};")
            self.kicker.setText("COVERAGE COMPLETE")
        else:
            self.ready_pill.setText("CAPTURING")
            self.ready_pill.setStyleSheet(
                f"color: {theme.MUTED}; border-color: {theme.BORDER};")
            self.kicker.setText("MOVE THE PLATE")

    def show_feedback(self, text: str, colour: str, ms: int = 2200) -> None:
        self._feedback_serial += 1
        serial = self._feedback_serial
        self.feedback.setText(text)
        self.feedback.setStyleSheet(f"color: {colour}; font-size: 14px; font-weight: 600;")
        # A parented timer is destroyed with the card; a raw singleShot lambda
        # can fire after the C++ label is gone during window teardown.
        timer = getattr(self, "_feedback_timer", None)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            self._feedback_timer = timer
            timer.timeout.connect(self._clear_feedback_serial)
        self._feedback_pending_serial = serial
        timer.start(max(0, int(ms)))

    def _clear_feedback_serial(self) -> None:
        if getattr(self, "_feedback_pending_serial", None) == self._feedback_serial:
            self.feedback.setText("")

    def clear_feedback(self) -> None:
        self._feedback_serial += 1
        timer = getattr(self, "_feedback_timer", None)
        if timer is not None:
            timer.stop()
        self.feedback.setText("")


class MovementIcon(QtWidgets.QWidget):
    """A large, quiet arrow / pause / rotate glyph derived from the sentence."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = "wait"
        self._angle = 0.0

    def set_instruction(self, text: str) -> None:
        t = str(text).lower()
        self._mode, self._angle = "arrow", 0.0
        if any(k in t for k in ("hold still", "pause", "stop moving", "too fast")):
            self._mode = "pause"
        elif any(k in t for k in ("rotate", "turn the plate")):
            self._mode = "rotate"
        elif "closer" in t or "toward" in t:
            # Depth is not a screen direction; keep it distinct from up/down.
            self._mode = "toward"
        elif "farther" in t or "further" in t or "away" in t:
            self._mode = "away"
        elif "left" in t:
            self._angle = 180.0
        elif "right" in t:
            self._angle = 0.0
        elif "up" in t:
            self._angle = -90.0
        elif "down" in t:
            self._angle = 90.0
        elif "corner" in t or "edge" in t:
            self._mode = "target"
        elif "ready" in t:
            self._mode = "check"
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(4, 4, -4, -4)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(theme.ACCENT).darker(220))
        p.drawEllipse(rect)
        pen = QtGui.QPen(QtGui.QColor(theme.TEXT), 7.0,
                         QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap,
                         QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        c = rect.center()
        r = rect.width() * 0.28
        if self._mode == "pause":
            p.drawLine(QtCore.QPointF(c.x() - 12, c.y() - 18),
                       QtCore.QPointF(c.x() - 12, c.y() + 18))
            p.drawLine(QtCore.QPointF(c.x() + 12, c.y() - 18),
                       QtCore.QPointF(c.x() + 12, c.y() + 18))
        elif self._mode == "rotate":
            arc = QtCore.QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
            p.drawArc(arc, 40 * 16, 280 * 16)
            tip = QtCore.QPointF(c.x() + r * math.cos(math.radians(40)),
                                 c.y() - r * math.sin(math.radians(40)))
            p.setBrush(QtGui.QColor(theme.TEXT))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawPolygon(QtGui.QPolygonF([
                tip + QtCore.QPointF(0, -12), tip + QtCore.QPointF(14, 6),
                tip + QtCore.QPointF(-8, 8)]))
        elif self._mode == "target":
            p.drawEllipse(c, r * 0.55, r * 0.55)
            p.drawEllipse(c, r * 0.18, r * 0.18)
            p.drawLine(QtCore.QPointF(c.x(), c.y() - r),
                       QtCore.QPointF(c.x(), c.y() - r * 0.55))
            p.drawLine(QtCore.QPointF(c.x(), c.y() + r * 0.55),
                       QtCore.QPointF(c.x(), c.y() + r))
            p.drawLine(QtCore.QPointF(c.x() - r, c.y()),
                       QtCore.QPointF(c.x() - r * 0.55, c.y()))
            p.drawLine(QtCore.QPointF(c.x() + r * 0.55, c.y()),
                       QtCore.QPointF(c.x() + r, c.y()))
        elif self._mode in ("toward", "away"):
            # Depth is not a screen direction: draw a plate between two
            # chevrons that point inward (closer) or outward (farther).
            sign = 1.0 if self._mode == "toward" else -1.0
            p.drawRect(QtCore.QRectF(c.x() - r * 0.28, c.y() - r * 0.55,
                                     r * 0.56, r * 1.1))
            for side in (-1.0, 1.0):
                tip = c.x() + side * sign * r * 0.10
                base = c.x() + side * sign * r * 0.72
                p.drawLine(QtCore.QPointF(base, c.y() - r * 0.45),
                           QtCore.QPointF(tip, c.y()))
                p.drawLine(QtCore.QPointF(base, c.y() + r * 0.45),
                           QtCore.QPointF(tip, c.y()))
        elif self._mode == "check":
            p.drawLine(QtCore.QPointF(c.x() - r, c.y()),
                       QtCore.QPointF(c.x() - r * 0.2, c.y() + r * 0.7))
            p.drawLine(QtCore.QPointF(c.x() - r * 0.2, c.y() + r * 0.7),
                       QtCore.QPointF(c.x() + r, c.y() - r * 0.65))
        else:
            p.save()
            p.translate(c)
            p.rotate(self._angle)
            p.drawLine(QtCore.QPointF(-r, 0), QtCore.QPointF(r, 0))
            p.setBrush(QtGui.QColor(theme.TEXT))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawPolygon(QtGui.QPolygonF([
                QtCore.QPointF(r + 6, 0), QtCore.QPointF(r - 12, -14),
                QtCore.QPointF(r - 12, 14)]))
            p.restore()


class ProgressStrip(QtWidgets.QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedHeight(116)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(8)
        row = QtWidgets.QHBoxLayout()
        self.views = QtWidgets.QLabel("0")
        self.views.setObjectName("progressNumber")
        row.addWidget(self.views)
        cap = QtWidgets.QLabel("views captured")
        cap.setObjectName("muted")
        row.addWidget(cap)
        row.addSpacing(12)
        self.families = QtWidgets.QLabel("0 / 8 pose families")
        self.families.setObjectName("muted")
        row.addWidget(self.families)
        row.addStretch(1)
        self.hint = QtWidgets.QLabel("")
        self.hint.setObjectName("muted")
        row.addWidget(self.hint)
        lay.addLayout(row)
        self.chips: dict[str, QtWidgets.QLabel] = {}
        chip_row = QtWidgets.QHBoxLayout()
        chip_row.setSpacing(6)
        for key, (label, tip) in DIMENSIONS.items():
            lab = QtWidgets.QLabel(label)
            lab.setObjectName("chip")
            lab.setFixedHeight(26)
            lab.setToolTip(tip)
            self.chips[key] = lab
            chip_row.addWidget(lab)
        chip_row.addStretch(1)
        lay.addLayout(chip_row)

    def update_from(self, snap: dict) -> None:
        counts = snap.get("counts") or {}
        self.views.setText(str(int(counts.get("accepted", 0) or 0)))
        cov = snap.get("coverage") or {}
        dims = cov.get("dimensions") or {}
        satisfied = int(cov.get("satisfied", 0) or 0)
        total = int(cov.get("total", 8) or 8)
        self.families.setText(f"{satisfied} / {total} pose families")
        for key, lab in self.chips.items():
            node = dims.get(key) or {}
            ok = bool(node.get("satisfied"))
            lab.setProperty("ok", ok)
            lab.style().unpolish(lab); lab.style().polish(lab)
            lab.setToolTip(str(node.get("hint") or DIMENSIONS[key][1]))
        read = snap.get("readiness") or {}
        missing = read.get("missing") or []
        if read.get("ready"):
            self.hint.setText("All pose families covered — you can finish")
        elif missing:
            self.hint.setText("Missing: " + ", ".join(
                str(m).replace("_", " ") for m in missing[:2]))
        else:
            self.hint.setText("Coverage is starting — keep capturing")


class AdvancedDialog(QtWidgets.QDialog):
    """All technical evidence, deliberately out of the operator's way."""

    def __init__(self, window: "ModernMainWindow", parent=None):
        super().__init__(parent or window)
        self.window = window
        self.setWindowTitle("Advanced diagnostics")
        self.resize(1000, 720)
        lay = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.observability = panels.ObservabilityPanel()
        self.tabs.addTab(self.observability, "Calibration")
        self.acq = QtWidgets.QPlainTextEdit()
        self.acq.setReadOnly(True)
        self.tabs.addTab(self.acq, "Acquisition")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Events")
        self.raw = QtWidgets.QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.tabs.addTab(self.raw, "Snapshot")
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        row = QtWidgets.QHBoxLayout(); row.addStretch(1); row.addWidget(close)
        lay.addLayout(row)

    def refresh(self, snap: Mapping[str, Any]) -> None:
        self.observability.update_from(dict(snap))
        import json
        cap = snap.get("capture") or {}
        source = snap.get("source") or {}
        acq = cap.get("acquisition") or {}
        child = acq.get("child") or {}
        device = acq.get("device") or {}
        recording = acq.get("recording") or {}
        trailer = recording.get("trailer") or {}
        stop = acq.get("stop") or {}
        guide = snap.get("guidance") or {}
        progress = guide.get("progress") or {}
        perf = snap.get("performance") or {}
        worker_metrics = {}
        try:
            worker_metrics = self.window.worker.metrics() if self.window.worker else {}
        except Exception:
            worker_metrics = {}
        lines = [
            f"session state {snap.get('state')}   reader feed: "
            f"{'ended' if self.window._source_ended else 'active'}",
            f"capture state {acq.get('state', 'unknown')}  "
            f"(raw status {cap.get('state')})",
            f"verified      {acq.get('verified')}   safe {acq.get('safe')}   "
            f"terminal {acq.get('terminal')}",
            f"child         pid {child.get('pid')} running {child.get('running')} "
            f"exit {child.get('exit_code')} {child.get('exit_signal') or ''}",
            f"stop          requested {stop.get('requested')} reason "
            f"{stop.get('reason')} attempts {stop.get('attempts')}",
            f"device        cleanup attempted {device.get('cleanup_attempted')} "
            f"verified {device.get('cleanup_verified')} "
            f"trigger {device.get('trigger_counts')} "
            f"laser {device.get('laser_percent')} %",
            f"recording     {(snap.get('session') or {}).get('raw_recording')}",
            f"recording end trailer {trailer.get('present')} "
            f"({trailer.get('trailer_bytes')} B) "
            f"manifest {recording.get('manifest', {}).get('present')} "
            f"finalized {recording.get('finalized')}",
            f"fps           {cap.get('fps')} (status age "
            f"{(acq.get('status') or {}).get('age_s')} s)",
            f"groups        {cap.get('groups_written')} recorded   "
            f"frames {cap.get('frames_completed')} drops "
            f"{cap.get('frames_dropped')}",
            f"throughput    {cap.get('throughput_mbps')} MB/s   "
            f"exposure {cap.get('exposure_us')} gain {cap.get('gain')}",
            f"decisions     evaluated {worker_metrics.get('decision_total')} / "
            f"preview {worker_metrics.get('preview_total')} / newest-frame "
            f"skips {worker_metrics.get('superseded')} "
            f"(raw recording retains every group)",
            f"decision lag  {worker_metrics.get('decision_lag')}",
            f"guidance      age {guide.get('age_s')} s  target "
            f"{(guide.get('target') or {}).get('region')}  "
            f"priority {guide.get('priority')}",
            f"progress      accepted {progress.get('accepted')} "
            f"since progress {progress.get('seconds_since_progress')} s  "
            f"stalled {progress.get('stalled')}  "
            f"stop advised {progress.get('stop_recommended')}  "
            f"unreachable {progress.get('unreachable')}",
            f"missing       {progress.get('remaining_information') or '-'}",
            f"persistence   writes {perf.get('persist', {}).get('writes')} "
            f"seconds {perf.get('persist', {}).get('seconds'):.2f}"
            if isinstance(perf.get("persist", {}).get("seconds"), (int, float))
            else "persistence   -",
            "",
            "evidence:",
        ]
        for reason in (acq.get("reasons") or []):
            lines.append(f"  reason   {reason}")
        for conflict in (acq.get("conflicts") or []):
            lines.append(f"  conflict {conflict}")
        for note in (acq.get("evidence_notes") or []):
            lines.append(f"  note     {note}")
        self.acq.setPlainText("\n".join(lines))
        self.log.setPlainText("\n".join(self.window._events[-300:]))
        self.raw.setPlainText(json.dumps(snap, indent=1, default=str))


class ModernMainWindow(QtWidgets.QMainWindow):
    def __init__(self, controller, source, *, pace_hz: float = 0.0,
                 headless: bool = False, max_groups: int = 0,
                 autostart: bool = False, auto_exposure: bool = True,
                 parent=None, async_decisions: bool | None = None,
                 launch_argv: list[str] | None = None):
        super().__init__(parent)
        self.controller = controller
        self.source = source
        # Still collections (synthetic/vendor) must not be judged by a video
        # motion rate.  The source's own continuous flag is authoritative.
        try:
            value = getattr(source, "continuous", None)
            if value is None:
                value = getattr(getattr(source, "info", None), "continuous", True)
            continuous = bool(value)
            controller._continuous_override = continuous
            controller.controller.continuous = continuous
        except Exception:
            pass
        self.headless = headless
        self._pace_hz = float(pace_hz)
        self._max_groups = int(max_groups)
        self._autostart = bool(autostart)
        self.worker: worker.SessionWorker | None = None
        self._finish_worker: worker.FinishWorker | None = None
        self._events: list[str] = []
        self._started_at = 0.0
        self._last_preview = -1
        self._closing = False
        self._cancelling = False
        self._worker_end: dict | None = None
        self._stop_worker = None
        self._finished_result: dict | None = None
        self._finish_started_at = 0.0
        self._advanced: AdvancedDialog | None = None
        self._show_markers = False
        self._ready = False
        self._auto_exposure_enabled = bool(auto_exposure)
        self._async_decisions = async_decisions
        self._auto_exposure = exposure_mod.AutoExposure(min_interval_s=0.75)
        self._last_ir_image = None
        self._last_rgb_image = None
        self._ir_roi = exposure_mod.RoiHold()
        self._rgb_roi = exposure_mod.RoiHold()
        self._exposure_run_id = f"{int(time.time())}-{os.getpid()}"
        self._exposure_baseline_done = False
        self._exposure_mismatch_seen = 0
        self._exposure_log_path = None
        self._ui_error = None
        self._display_state: str | None = None
        self._result_validated = False
        self._source_ended = False
        self._feedback_hold_until = 0.0
        self._shutdown_done = False
        # Launcher arguments without the program name.  ``None`` means "this
        # window was not started by the CLI", and the extension entry point
        # stays hidden because there is nothing to restart with.
        self._launch_argv = None if launch_argv is None else [str(a) for a in launch_argv]
        self._extension_busy = False

        self.setWindowTitle("Einstar Calibration")
        self.resize(1640, 1000)
        self.setMinimumSize(1180, 760)
        self.setStyleSheet(theme.modern_stylesheet())

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(12)
        root.addWidget(self._topbar())

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(14)
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(12)
        feeds = QtWidgets.QHBoxLayout()
        feeds.setSpacing(12)
        self.feed_left = FeedView("Left infrared", theme.INFO)
        self.feed_right = FeedView("Right infrared", theme.INFO)
        self.feed_colour = FeedView("Colour", theme.OK)
        for w in (self.feed_left, self.feed_right, self.feed_colour):
            feeds.addWidget(w, 1)
        feed_host = QtWidgets.QWidget()
        feed_host.setLayout(feeds)
        feed_host.setMinimumHeight(300)
        feed_host.setMaximumHeight(430)
        left.addWidget(feed_host, 0)
        self.progress = ProgressStrip()
        left.addWidget(self.progress, 0)
        self.coverage_map = tiles.CoverageTile()
        self.coverage_map.setMinimumHeight(190)
        self.coverage_map.setMaximumHeight(260)
        left.addWidget(self.coverage_map, 1)
        body.addLayout(left, 1)
        side = QtWidgets.QVBoxLayout()
        side.setSpacing(12)
        self.instruction = InstructionCard()
        side.addWidget(self.instruction, 0)
        self.plate = plate3d.PlatePreview()
        self.plate.setMinimumHeight(280)
        side.addWidget(self.plate, 1)
        held = QtWidgets.QFrame()
        held.setObjectName("card")
        hl = QtWidgets.QVBoxLayout(held)
        hl.setContentsMargins(16, 12, 16, 12)
        hl.setSpacing(4)
        self.scanner_line = QtWidgets.QLabel("Scanner: starting…")
        self.scanner_line.setObjectName("muted")
        self.scanner_line.setWordWrap(True)
        self.session_line = QtWidgets.QLabel("No session yet")
        self.session_line.setObjectName("muted")
        hl.addWidget(self.scanner_line)
        hl.addWidget(self.session_line)
        side.addWidget(held, 0)
        side_host = QtWidgets.QWidget()
        side_host.setLayout(side)
        side_host.setFixedWidth(400)
        self._side_host = side_host
        body.addWidget(side_host, 0)
        root.addLayout(body, 1)

        root.addWidget(self._footer())
        self.error_banner = QtWidgets.QLabel("")
        self.error_banner.setObjectName("errorBanner")
        self.error_banner.setWordWrap(True)
        self.error_banner.setMinimumHeight(30)
        self.error_banner.setVisible(False)
        root.addWidget(self.error_banner)

        # A stop recommendation must be impossible to miss; it is separate
        # from the generic error banner and names the safe action.
        self.stop_banner = QtWidgets.QLabel("")
        self.stop_banner.setObjectName("stopBanner")
        self.stop_banner.setWordWrap(True)
        self.stop_banner.setMinimumHeight(34)
        self.stop_banner.setStyleSheet(
            "background:#4a3500; color:#ffd88a; border:1px solid #a8791f;"
            " border-radius:6px; padding:8px;")
        self.stop_banner.setVisible(False)
        root.addWidget(self.stop_banner)

        self.finish_overlay = self._finish_overlay(central)
        self.finish_overlay.setVisible(False)

        self.controls.start.clicked.connect(self._on_start)
        self.controls.pause.clicked.connect(self._on_pause)
        self.controls.finish.clicked.connect(self._on_finish)
        self.controls.advanced.clicked.connect(self._open_advanced)
        self.controls.cancel.clicked.connect(self._on_cancel)
        self.controls.extension.clicked.connect(self._on_complete_calibration)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(100)
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(250)
        for seq, fn in (("Space", self._on_pause), ("F", self._on_finish),
                        ("Ctrl+Shift+A", self._open_advanced)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.activated.connect(fn)
        self._apply_state("idle")

    # -- construction ----------------------------------------------------
    def _topbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QFrame()
        bar.setObjectName("topbar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(4, 0, 4, 0)
        mark = QtWidgets.QLabel("EINSTAR")
        mark.setObjectName("wordmark")
        lay.addWidget(mark)
        sub = QtWidgets.QLabel("CALIBRATION")
        sub.setObjectName("kicker")
        lay.addWidget(sub)
        lay.addStretch(1)
        self.status_pill = QtWidgets.QLabel("●  SCANNER READY")
        self.status_pill.setObjectName("statusPill")
        lay.addWidget(self.status_pill)
        lay.addSpacing(18)
        self.fps_label = QtWidgets.QLabel("— fps")
        self.fps_label.setObjectName("mono")
        lay.addWidget(self.fps_label)
        self.views_label = QtWidgets.QLabel("0 views")
        self.views_label.setObjectName("mono")
        lay.addWidget(self.views_label)
        return bar

    def _footer(self) -> QtWidgets.QWidget:
        self.controls = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(self.controls)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.footer_note = QtWidgets.QLabel(
            "Keep the plate in view; the scanner captures automatically.")
        self.footer_note.setObjectName("muted")
        lay.addWidget(self.footer_note, 1)
        # Rare, deliberate operator action: it restarts the application, so it
        # must not compete with Start/Finish.  Hidden unless the window was
        # started by the CLI and the controller can extend an existing session.
        self.btn_extension = QtWidgets.QPushButton("Complete calibration…")
        self.btn_extension.setObjectName("ghost")
        self.btn_extension.setToolTip(
            "Restart the wizard to capture only the poses an existing session "
            "still needs. The base session is opened read-only.")
        self.btn_extension.setVisible(False)
        lay.addWidget(self.btn_extension)
        self.btn_cancel = QtWidgets.QPushButton("Stop && keep data")
        self.btn_cancel.setObjectName("ghost")
        self.btn_cancel.setToolTip("End the session and keep every recorded frame")
        lay.addWidget(self.btn_cancel)
        self.btn_advanced = QtWidgets.QPushButton("Advanced")
        self.btn_advanced.setObjectName("ghost")
        lay.addWidget(self.btn_advanced)
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setObjectName("secondary")
        lay.addWidget(self.btn_pause)
        self.btn_start = QtWidgets.QPushButton("Start calibration")
        self.btn_start.setObjectName("primary")
        self.btn_start.setMinimumWidth(230)
        lay.addWidget(self.btn_start)
        self.btn_finish = QtWidgets.QPushButton("Finish && build profile")
        self.btn_finish.setObjectName("primary")
        self.btn_finish.setMinimumWidth(230)
        self.btn_finish.setVisible(False)
        lay.addWidget(self.btn_finish)
        # aliases used by the headless app and by tests
        self.controls.start = self.btn_start
        self.controls.pause = self.btn_pause
        self.controls.finish = self.btn_finish
        self.controls.advanced = self.btn_advanced
        self.controls.cancel = self.btn_cancel
        self.controls.extension = self.btn_extension
        return self.controls

    def _finish_overlay(self, parent) -> QtWidgets.QWidget:
        overlay = QtWidgets.QFrame(parent)
        overlay.setObjectName("overlay")
        overlay.setGeometry(parent.rect())
        lay = QtWidgets.QVBoxLayout(overlay)
        lay.addStretch(1)
        card = QtWidgets.QFrame()
        card.setObjectName("card")
        card.setMinimumWidth(500)
        card.setMaximumWidth(560)
        cl = QtWidgets.QVBoxLayout(card)
        cl.setContentsMargins(28, 24, 28, 24)
        cl.setSpacing(10)
        self.finish_title = QtWidgets.QLabel("Building your calibration")
        self.finish_title.setObjectName("headline")
        self.finish_text = QtWidgets.QLabel(
            "Optimising the three-camera rig and running independent checks. "
            "The window stays responsive.")
        self.finish_text.setObjectName("muted")
        self.finish_text.setWordWrap(True)
        self.finish_text.setMinimumHeight(44)
        self.finish_progress = QtWidgets.QProgressBar()
        self.finish_progress.setRange(0, 0)
        cl.addWidget(self.finish_title)
        cl.addWidget(self.finish_text)
        cl.addWidget(self.finish_progress)
        lay.addWidget(card, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)
        return overlay

    # -- state -----------------------------------------------------------
    def _event(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._events.append(f"{stamp}  {text}")
        if self._advanced is not None and self._advanced.isVisible():
            self._advanced.log.setPlainText("\n".join(self._events[-300:]))

    def _set_pill(self, text: str, colour: str) -> None:
        self.status_pill.setText(text)
        self.status_pill.setStyleSheet(
            f"color: {colour}; border-color: {colour};")

    def _source_kind(self) -> str:
        try:
            return str((getattr(self.source, "info", None).kind
                        if getattr(self.source, "info", None) is not None
                        else getattr(self.source, "kind", "")) or "")
        except Exception:
            return ""

    def _set_feed_status(self, state: str) -> None:
        kind = self._source_kind().lower()
        if state == "recording":
            label = "LIVE" if kind == "live" else (
                "REPLAY" if kind == "replay" else "SYNTHETIC")
        elif state == "paused":
            label = "PAUSED"
        elif state == "finishing":
            label = "FINISHING"
        elif state in ("done", "ended"):
            label = "ENDED"
        elif state == "error":
            label = "ERROR"
        else:
            label = "IDLE"
        for feed in (self.feed_left, self.feed_right, self.feed_colour):
            if hasattr(feed, "set_status"):
                feed.set_status(label)

    def _apply_state(self, state: str) -> None:
        terminal = state in ("done", "error", "ended", "finishing",
                             "cancelled_unverified")
        if terminal:
            self._display_state = state
        elif state in ("recording", "paused", "idle", "cancelled"):
            self._display_state = None
        self._session_state = state
        recording = state == "recording"
        paused = state == "paused"
        finishing = state == "finishing"
        self.btn_start.setVisible(state in ("idle", "cancelled"))
        self.btn_pause.setVisible(state in ("recording", "paused"))
        self.btn_finish.setVisible(bool(self._ready)
                                   and state in ("recording", "paused", "ended"))
        self.btn_finish.setEnabled(bool(self._ready)
                                   and state in ("recording", "paused", "ended"))
        self.btn_pause.setText("Resume" if paused else "Pause")
        self.btn_cancel.setEnabled(state in ("recording", "paused"))
        self.btn_cancel.setVisible(state in ("recording", "paused"))
        self.btn_advanced.setEnabled(True)
        if state == "done":
            if self._result_validated:
                self._set_pill("●  CALIBRATION VALIDATED", theme.OK)
            else:
                self._set_pill("●  CANDIDATE WRITTEN", theme.WARN
                               if hasattr(theme, "WARN") else theme.MUTED)
        elif state == "error":
            self._set_pill("●  NEEDS ATTENTION", theme.ERROR
                           if hasattr(theme, "ERROR") else theme.MUTED)
        elif state == "ended":
            self._set_pill("●  RECORDING ENDED", theme.WARN
                           if hasattr(theme, "WARN") else theme.MUTED)
        elif state == "cancelled_unverified":
            self._set_pill("●  STOPPED · UNVERIFIED",
                           getattr(theme, "ERROR", theme.WARN))
        elif finishing:
            self._set_pill("●  BUILDING PROFILE", theme.INFO)
        elif recording:
            self._set_pill("●  CAPTURING", theme.INFO)
        elif paused:
            self._set_pill("●  PAUSED", theme.WARN
                           if hasattr(theme, "WARN") else theme.MUTED)
        elif state == "cancelled":
            self._set_pill("●  STOPPED", theme.MUTED)
        else:
            self._set_pill("●  NOT STARTED", theme.MUTED)
        self._set_feed_status(state)
        self.finish_overlay.setVisible(finishing)
        if not recording:
            self.instruction.clear_feedback()
        if state == "idle":
            self.instruction.set_instruction("Press Start to begin", "")
            self.instruction.set_ready(False, [])
        elif paused:
            self.instruction.set_instruction("Paused — resume to continue", "")
        elif finishing:
            self.instruction.set_instruction("Building the profile", "")
        elif state == "ended":
            if self._ready:
                self.instruction.set_instruction(
                    "Recording complete — finish to build the profile", "")
            else:
                self.instruction.set_instruction(
                    "Source ended — not enough coverage for a profile", "")
        elif state == "done":
            self.instruction.set_instruction(
                "Validated profile written" if self._result_validated
                else "Candidate written — validation did not pass", "")
        elif state == "error":
            self.instruction.set_instruction("Needs attention — see the message", "")
        elif state == "cancelled_unverified":
            self.instruction.set_instruction(
                "Stopped without verified scanner/recording evidence", "")
        if state != "recording":
            if state in ("idle", "cancelled"):
                self.instruction.ready_pill.setText("NOT READY")
                self.instruction.kicker.setText("PRESS START")
            elif state == "paused":
                self.instruction.ready_pill.setText("PAUSED")
                self.instruction.kicker.setText("PAUSED")
            elif state == "finishing":
                self.instruction.ready_pill.setText("BUILDING")
                self.instruction.kicker.setText("FINAL VALIDATION")
            elif state == "ended":
                self.instruction.ready_pill.setText("ENDED")
                self.instruction.kicker.setText("RECORDING FINISHED")
            elif state == "cancelled_unverified":
                self.instruction.ready_pill.setText("UNVERIFIED")
                self.instruction.kicker.setText("CHECK DIAGNOSTICS")
            elif state == "done":
                self.instruction.ready_pill.setText(
                    "VALIDATED" if self._result_validated else "NOT VALIDATED")
                self.instruction.kicker.setText("SESSION COMPLETE")
            elif state == "error":
                self.instruction.ready_pill.setText("NEEDS ATTENTION")
                self.instruction.kicker.setText("CHECK THE MESSAGE")
        self._sync_extension_button(state)

    def _tick(self) -> None:
        if (self._finish_worker is not None and self._finish_worker.isRunning()
                and self._finish_started_at):
            elapsed = time.time() - self._finish_started_at
            self.finish_text.setText(
                f"Optimising and validating… {elapsed:.1f} s. "
                "The window stays responsive.")
        state = getattr(self, "_session_state", "idle")
        if state != "recording":
            self.fps_label.setText("— fps")
            return
        hz = None
        try:
            metrics = self.worker.metrics() if self.worker is not None else {}
            hz = _num((metrics.get("preview") or {}).get("hz"))
        except Exception:
            hz = None
        if hz is None:
            try:
                hz = _num(((self.controller.snapshot().get("capture") or {})
                           .get("fps")))
            except Exception:
                hz = None
        self.fps_label.setText(f"{hz:.1f} fps" if hz is not None else "— fps")

    # -- feed ------------------------------------------------------------
    def _on_preview(self, payload: dict) -> None:
        if self._closing:
            return
        index = payload.get("index")
        if index == self._last_preview:
            return
        self._last_preview = index
        images = payload.get("images") or {}
        if "left" in images:
            self._last_ir_image = images["left"]
            self.feed_left.set_frame(images["left"])
        if "right" in images:
            self.feed_right.set_frame(images["right"])
        colour = payload.get("colour_bgr")
        if colour is not None:
            self._last_rgb_image = colour
            self.feed_colour.set_frame(colour, colour=True)
        elif "tex" in images:
            self._last_rgb_image = images["tex"]
            self.feed_colour.set_frame(images["tex"])

    # -- controls --------------------------------------------------------
    def _make_worker(self) -> worker.SessionWorker:
        kwargs = {}
        if self._async_decisions is not None:
            kwargs["async_decisions"] = bool(self._async_decisions)
        w = worker.SessionWorker(
            self.controller, self.source, pace_hz=self._pace_hz,
            max_groups=self._max_groups, parent=self, **kwargs)
        w.preview.connect(self._on_preview)
        w.decision.connect(self._on_decision)
        w.error.connect(self._fail)
        return w

    def _on_start(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            if str(getattr(self.controller, "state", "")) in ("done", "cancelled"):
                self._fail("this session is finished; start a new application "
                           "session to calibrate again")
                return
        except Exception:
            pass
        # PR_SET_PDEATHSIG is tied to the *thread* that created the child.
        # Start the capture on the long-lived GUI thread; starting it inside
        # the reader thread would make that thread's normal exit deliver
        # SIGTERM to the scanner (observed as stop_reason signal:SIGTERM).
        try:
            starter = getattr(self.source, "start", None)
            if callable(starter):
                starter()
        except Exception as exc:
            self._fail(f"could not start acquisition: {type(exc).__name__}: {exc}")
            return
        try:
            begun = self.controller.begin() or {}
            previous = begun.get("previous_session")
            if previous:
                # A reused session directory is isolated, not silently mixed;
                # tell the operator where the old attempt went.
                self._event("previous session moved to "
                            + str(previous.get("directory")))
        except Exception as exc:
            # The source may already be running (live capture is started on
            # this thread before begin()).  A failed begin must not leave the
            # scanner streaming with no controller.
            try:
                close = getattr(self.source, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            self._fail(f"could not start: {type(exc).__name__}: {exc}")
            return
        self._started_at = time.time()
        self._auto_exposure.reset()
        self._ir_roi.reset()
        self._rgb_roi.reset()
        self._exposure_baseline_done = False
        self._exposure_mismatch_seen = 0
        self._last_exposure_request = None
        self._last_exposure_request_at = 0.0
        self._exposure_run_id = f"{int(self._started_at)}-{os.getpid()}"
        self._ui_error = None
        self._display_state = None
        self._source_ended = False
        self.error_banner.setVisible(False)
        self.error_banner.setText("")
        self.worker = self._make_worker()
        self.worker.finished_ok.connect(self._on_worker_done)
        self.worker.start()
        self._apply_state("recording")
        self._event("recording started")

    def _on_pause(self) -> None:
        if self.worker is None:
            return
        if self.worker.paused:
            self.worker.request_resume()
            try:
                self.controller.unpause()
            except Exception as exc:
                self._fail(str(exc))
            self._apply_state("recording")
            self._event("resumed")
        else:
            self.worker.request_pause()
            try:
                self.controller.pause()
            except Exception as exc:
                self._fail(str(exc))
            self._apply_state("paused")
            self._event("paused")

    def _on_decision(self, payload: dict) -> None:
        now = time.time()
        if payload.get("accepted"):
            self._auto_exposure.freeze()
        text, colour_name = panels.feedback_text(payload)
        colour = getattr(theme, colour_name, theme.MUTED)
        if payload.get("accepted"):
            self._feedback_hold_until = now + 1.8
            self.instruction.show_feedback(text, colour, 3000)
        elif text:
            # A rejection arrives at 4-10 Hz and would otherwise erase the
            # accepted-view confirmation before the operator can see it.
            if now >= getattr(self, "_feedback_hold_until", 0.0):
                self.instruction.show_feedback(text, colour, 1800)
        self._event(("captured " if payload.get("accepted") else "rejected ")
                    + str(payload.get("view_id") if payload.get("accepted") else
                          "; ".join(str(r) for r in (payload.get("reasons") or []))))

    def _on_worker_done(self, info: dict) -> None:
        info = dict(info or {})
        groups = int(info.get("groups_ingested", 0) or 0)
        self._worker_end = info
        self._event(f"acquisition worker ended ({groups} groups)")
        state = str(getattr(self, "_session_state", ""))
        if state in ("finishing", "done", "error"):
            return
        if self._cancelling:
            # _on_cancel applies the final cancelled/cancelled_unverified state
            return
        self._source_ended = True
        acq = dict(info.get("acquisition") or {})
        if not acq:
            try:
                acq = dict((self.controller.snapshot().get("capture") or {})
                           .get("acquisition") or {})
            except Exception:
                acq = {}
        verified = acq.get("verified")
        running = bool((acq.get("child") or {}).get("running"))
        capture_live = (str(getattr(getattr(self.source, "kind", ""), "lower", "")
                            or self._source_kind()).endswith("live"))
        acq_state = str(acq.get("state") or "")
        if (not capture_live and not running and verified is None
                and acq_state in ("", "not_applicable")):
            # Replay/synthetic sessions own no scanner; reader end is normal.
            self._display_state = None
            self._apply_state("ended")
            if self._ready:
                self.footer_note.setText(
                    "Recording complete. Press Finish to build and validate "
                    "the profile.")
            else:
                self.footer_note.setText(
                    "Recording ended with incomplete coverage; no profile "
                    "can be built.")
            return
        self._apply_acquisition_end(acq)
        if running:
            # The child must never be left behind because only the reader
            # stopped.  Stop + verify asynchronously; keep the UI truthful.
            self.error_banner.setText(
                "The reader stopped but the capture process is still running. "
                "Stopping the scanner and finalising the recording…")
            self.error_banner.setVisible(True)
            self._stop_scanner_now("reader ended while capture still running")
            return
        if verified is True:
            self._display_state = None
            self._apply_state("ended")
            self._apply_acquisition_end(acq)
            if self._ready:
                self.footer_note.setText(
                    "Recording finalized (scanner stop verified). Press Finish "
                    "to build and validate the profile.")
            else:
                self.footer_note.setText(
                    "Recording finalized, but coverage is incomplete; no "
                    "profile can be built. The raw recording is preserved.")
        elif verified is False:
            reasons = [str(r) for r in (acq.get("reasons") or [])]
            self._display_state = "error"
            self._apply_state("error")
            self._fail("Acquisition ended without a verified safe state: "
                       + ("; ".join(reasons[:3]) or "no evidence"))
            self._apply_acquisition_end(acq)
        else:
            self._display_state = "error"
            self._apply_state("error")
            self._fail("Acquisition ended but its scanner state is unverified.")
            self._apply_acquisition_end(acq)

    def _apply_acquisition_end(self, acq: Mapping[str, Any]) -> None:
        """Set the pill/footer from the authoritative acquisition verdict."""
        state = str(acq.get("state") or "")
        if state == "stopped_verified":
            self._set_pill("●  CAPTURE STOPPED · VERIFIED", theme.OK)
        elif state in ("stopping_unverified", "failed"):
            self._set_pill("●  SCANNER STATE UNVERIFIED",
                           getattr(theme, "ERROR", theme.WARN))
        elif state == "stopping":
            self._set_pill("●  STOPPING SCANNER", theme.WARN)
        elif state == "running":
            self._set_pill("●  CAPTURE STILL RUNNING", theme.WARN)

    def _stop_scanner_now(self, reason: str) -> None:
        if self._stop_worker is not None and self._stop_worker.isRunning():
            return
        if not hasattr(self.controller, "stop_capture"):
            return
        self._stop_worker = worker.StopWorker(self.controller, reason, parent=self)
        self._stop_worker.done.connect(self._on_scanner_stopped)
        self._stop_worker.start()

    def _on_scanner_stopped(self, result: dict) -> None:
        self._stop_worker = None
        acq = dict((result or {}).get("acquisition") or {})
        self._apply_acquisition_end(acq)
        if result.get("verified"):
            self._display_state = None
            self._apply_state("ended")
            self.footer_note.setText(
                "Scanner stopped and recording finalised (verified).")
            self._event("acquisition stop verified after the reader ended")
        else:
            reasons = [str(r) for r in (result.get("reasons") or [])]
            self._display_state = "error"
            self._apply_state("error")
            self._fail("Scanner stop is UNVERIFIED: "
                       + ("; ".join(reasons[:3]) or "no evidence"))
        self.refresh()

    def _on_finish(self) -> None:
        if self._finish_worker is not None and self._finish_worker.isRunning():
            return
        state = str(getattr(self, "_session_state", ""))
        if not self._ready:
            self.instruction.show_feedback(
                "Finish is locked until coverage is complete", theme.MUTED, 2500)
            self._event("finish refused: coverage not ready")
            return
        if state not in ("recording", "paused", "ended"):
            self.instruction.show_feedback(
                "Finish is only available for an active or completed capture",
                theme.MUTED, 2500)
            return
        self._finished_result = None
        self._result_validated = False
        self._finish_started_at = time.time()
        self._apply_state("finishing")
        self.finish_title.setText("Building your calibration")
        self.finish_text.setText(
            "Optimising the three-camera rig and running independent checks. "
            "The window stays responsive.")
        self._finish_worker = worker.FinishWorker(self.controller, self.worker,
                                                  parent=self)
        self._finish_worker.progress.connect(self._on_finish_progress)
        self._finish_worker.done.connect(self._on_finish_done)
        self._finish_worker.failed.connect(self._on_finish_failed)
        self._finish_worker.start()
        self._event("final calibration started")

    def _on_finish_progress(self, elapsed: float) -> None:
        self.finish_text.setText(
            f"Optimising and validating… {elapsed:.0f} s. "
            "The window stays responsive.")

    def _on_finish_done(self, result: dict) -> None:
        self._finish_worker = None
        self._finished_result = dict(result or {})
        ok = bool(result.get("ok"))
        validation = result.get("validation") or {}
        verdict = (validation.get("verdict")
                   if isinstance(validation, dict) else None) or {}
        passed = (validation.get("passed") is True
                  or verdict.get("would_activate") is True)
        self._result_validated = bool(ok and passed)
        self.finish_overlay.setVisible(False)
        if not ok:
            self._display_state = "error"
            self._apply_state("error")
            self._fail(str(result.get("reason") or result.get("error")
                           or "the final solve failed"))
            self.refresh()
            return
        self._apply_state("done")
        reasons = [str(r) for r in (verdict.get("reasons") or [])]
        if self._result_validated:
            gates = verdict.get("gates") or {}
            quality = str(gates.get("quality_status") or "").lower()
            if quality and quality != "pass":
                self.instruction.show_feedback(
                    "✓ Profile validated — quality advisory", theme.OK, 10000)
                self._event("candidate validated; quality report has a warning")
                self.footer_note.setText(
                    "Independent gates passed; the quality report has an advisory "
                    "warning. Factory calibration was not modified.")
            else:
                self.instruction.show_feedback(
                    "✓ Profile validated by all gates", theme.OK, 10000)
                self._event("candidate profile written; validation passed")
                self.footer_note.setText(
                    "Validated candidate written. Factory calibration was not modified.")
        else:
            reason = reasons[0] if reasons else "validation did not activate"
            self.instruction.show_feedback(
                "Candidate written — validation did not pass", theme.MUTED, 10000)
            self._event("candidate profile written; validation failed: " + reason)
            self.footer_note.setText(
                "Candidate saved for inspection; validation failed — " + reason)
        self.refresh()

    def _on_finish_failed(self, message: str) -> None:
        self._finish_worker = None
        self.finish_overlay.setVisible(False)
        self._display_state = "error"
        self._apply_state("error")
        self._fail(f"finish failed: {message}")
        self._event(f"finish failed: {message}")

    def _on_cancel(self) -> None:
        if str(getattr(self, "_session_state", "")) not in (
                "recording", "paused", "ended", "error"):
            return
        self._cancelling = True
        self._set_pill("●  STOPPING SCANNER", theme.WARN)
        self.footer_note.setText(
            "Stopping the scanner: waiting for process exit, trigger/laser "
            "readback and recording finalization…")
        w = self.worker
        if w is not None and w.isRunning():
            w.request_stop()
            deadline = time.time() + 20.0
            while w.isRunning() and time.time() < deadline:
                QtWidgets.QApplication.processEvents()
                time.sleep(0.01)
        try:
            result = self.controller.cancel(reason="operator stop")
        except Exception as exc:
            self._fail(f"stop failed: {type(exc).__name__}: {exc}")
            self._cancelling = False
            return
        self._cancelling = False
        self._display_state = None
        self.refresh()
        state = str(result.get("state") or "cancelled")
        acq = dict(result.get("acquisition") or {})
        if state == "cancelled_unverified" or not result.get("safe", False):
            self._display_state = "error"
            self._apply_state("error")
            reasons = [str(r) for r in (result.get("reasons") or [])]
            self._fail("Stopped, but the scanner/recording state is UNVERIFIED: "
                       + ("; ".join(reasons[:3]) or "no evidence"))
            self._apply_acquisition_end(acq)
            self._event("stopped with UNVERIFIED acquisition state; "
                        "all recorded data kept")
        else:
            self._apply_state("cancelled")
            self._apply_acquisition_end(acq)
            self.footer_note.setText(
                "Stopped and verified: scanner off, recording finalised. "
                "All recorded data was kept.")
            self._event("stopped and verified; all recorded data kept")

    def _open_advanced(self) -> None:
        if self._advanced is None:
            self._advanced = AdvancedDialog(self, self)
        try:
            self._advanced.refresh(self.controller.snapshot())
        except Exception:
            pass
        self._advanced.show()
        self._advanced.raise_()
        self._advanced.activateWindow()

    # -- complete missing views (relaunch) --------------------------------
    def _extension_entry_available(self) -> bool:
        """True when this window may offer the missing-view workflow.

        The capture child and the session directory are bound at launch time,
        so the only safe way to add views to *another* session is a fresh
        process.  Without the original launcher arguments there is nothing to
        restart with, and a headless run must never pop dialogs.
        """
        if self.headless or self._launch_argv is None:
            return False
        if not callable(getattr(self.controller, "begin_extension", None)):
            return False
        try:
            if getattr(self.controller, "extension", None):
                return False
        except Exception:
            return False
        return True

    def _sync_extension_button(self, state: str | None = None) -> None:
        state = str(state if state is not None
                    else getattr(self, "_session_state", "idle"))
        allowed = (self._extension_entry_available()
                   and not self._extension_busy
                   and state in ("idle", "cancelled", "error"))
        self.btn_extension.setVisible(bool(allowed))
        self.btn_extension.setEnabled(bool(allowed))

    def _on_complete_calibration(self) -> None:
        """Pick a base session, derive its extension directory and relaunch."""
        if (self.headless or self._extension_busy
                or not self._extension_entry_available()):
            return
        start_dir = ""
        try:
            root = getattr(getattr(self.controller, "paths", None), "root", None)
            if root:
                start_dir = str(root)
        except Exception:
            start_dir = ""
        base = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select the session to complete", start_dir)
        if not base:
            return
        new_dir = extension_mod.derive_extension_dir(base)
        problem = extension_mod.extension_target_problem(new_dir)
        if problem:
            QtWidgets.QMessageBox.warning(
                self, "Complete calibration",
                f"{problem}.\n\nChoose a different base session.")
            return
        answer = QtWidgets.QMessageBox.question(
            self, "Complete calibration",
            "The wizard will restart to capture only the missing poses. "
            "The base session is opened read-only.\n\n"
            f"Base session: {base}\nNew session: {new_dir}",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._relaunch_for_extension(base, new_dir)

    def _relaunch_for_extension(self, base_dir: str, new_dir: str) -> None:
        python = sys.executable or "python3"
        app_path = extension_mod.launcher_path(sys.argv[0] if sys.argv else None)
        try:
            argv = extension_mod.build_extension_launch(
                list(self._launch_argv or []), base_dir, new_dir, app_path, python)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, "Complete calibration",
                f"Could not build the restart command: "
                f"{type(exc).__name__}: {exc}")
            return
        self._extension_busy = True
        self._sync_extension_button()
        self._set_pill("●  RESTARTING", theme.INFO)
        self.footer_note.setText(
            "Releasing the scanner, then restarting the wizard to capture the "
            "missing poses…")
        self._event(f"extension: restarting from {base_dir} into {new_dir}")
        # Let the operator see the busy state before the window stops painting.
        QtWidgets.QApplication.processEvents()
        try:
            released = self.shutdown(timeout_s=10.0)
        except Exception as exc:                 # pragma: no cover - defensive
            released = False
            self._event(f"extension: shutdown raised "
                        f"{type(exc).__name__}: {exc}")
        # A live extension must not race the old process for the scanner.  When
        # the shutdown could not verify the stop (trigger off, child exited,
        # illuminator off), starting a second wizard would be a hardware risk:
        # refuse and tell the operator to relaunch manually.
        try:
            live_source = self._source_kind() == "live"
        except Exception:
            live_source = False
        if live_source and not released:
            self._extension_busy = False
            self._sync_extension_button()
            self._set_pill("●  SCANNER NOT RELEASED", theme.WARN)
            self.footer_note.setText(
                "The scanner stop could not be verified; the wizard was not "
                "restarted. Close this window and relaunch it manually.")
            QtWidgets.QMessageBox.critical(
                self, "Complete calibration",
                "The scanner stop could not be verified, so the wizard was "
                "not restarted. Close this window, check that the scanner is "
                "idle, and relaunch the complete-session command manually.\n\n"
                f"Command:\n{' '.join(argv)}")
            return
        ok, detail = self._start_detached(argv)
        if not ok:
            self._extension_busy = False
            self._sync_extension_button()
            self._set_pill("●  NOT STARTED", theme.MUTED)
            self.footer_note.setText(
                "The wizard could not be restarted. Close this window and "
                "relaunch it manually.")
            QtWidgets.QMessageBox.critical(
                self, "Complete calibration",
                f"The wizard could not be restarted.\n\n{detail}\n\n"
                f"Command:\n{' '.join(argv)}")
            return
        self._event(f"extension: restart released={bool(released)}")
        self.close()

    @staticmethod
    def _start_detached(argv: list[str]) -> tuple[bool, str]:
        """Start the relaunched wizard; return ``(started, failure_detail)``."""
        if not argv:
            return False, "empty command line"
        try:
            result = QtCore.QProcess.startDetached(
                str(argv[0]), [str(a) for a in argv[1:]])
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        # PySide6 returns ``(started, pid)`` for the static overload; older
        # bindings returned a bare bool.  Accept both.
        if isinstance(result, tuple):
            started = bool(result[0]) if result else False
        else:
            started = bool(result)
        if not started:
            return False, "QProcess.startDetached reported failure"
        return True, ""

    def _fail(self, message: str) -> None:
        self._ui_error = str(message)
        self.error_banner.setText(str(message))
        self.error_banner.setWordWrap(True)
        self.error_banner.setVisible(True)
        if not self._closing:
            self._set_pill("●  NEEDS ATTENTION",
                           theme.ERROR if hasattr(theme, "ERROR") else theme.MUTED)
        self.error_banner.setVisible(True)
        self._event(message)

    def _update_coverage_map(self, snap: dict) -> None:
        cov = snap.get("coverage") or {}
        views = cov.get("views") or []
        regions = np.zeros((3, 3), int)
        distances, tilts, rolls = [], [], []
        for view in views:
            uv = view.get("image_uv")
            if uv:
                try:
                    r = int(np.clip(float(uv[1]) * 3, 0, 2))
                    c = int(np.clip(float(uv[0]) * 3, 0, 2))
                    regions[r, c] += 1
                except (TypeError, ValueError):
                    pass
            d = view.get("distance_mm")
            if d is not None:
                distances.append(float(d))
            tx, ty = view.get("tilt_x_deg"), view.get("tilt_y_deg")
            if tx is not None and ty is not None:
                tilts.append((float(tx), float(ty)))
            rr = view.get("roll_deg")
            if rr is not None:
                rolls.append(float(rr))
        self.coverage_map.set_data(regions, distances, tilts, rolls,
                                   bool((snap.get("readiness") or {}).get("ready")))

    def _auto_expose(self, snap: dict) -> None:
        """Adjust volatile exposure/gain/illumination on measured frame stats.

        Only live sessions with a capture supervisor participate.  The advisor
        freezes after each accepted view and during a stationary hold, so a
        calibration observation is never taken while exposure is moving; hard
        clipping overrides that freeze.  Brightness is metered on the detected
        plate bbox (with a bounded stale fallback); full-frame metering is only
        used for gross clipping protection, never to raise a dark background.
        """
        if not self._auto_exposure_enabled:
            return
        source = self.source
        capture = getattr(source, "capture", None)
        dbg = os.environ.get("EINSTAR_EXPOSURE_DEBUG") == "1"
        if dbg:
            print("exposure: kind", (snap.get("source") or {}).get("kind"),
                  "source", type(source).__name__, "capture", type(capture).__name__,
                  flush=True)
        if str((snap.get("source") or {}).get("kind", "")).lower() != "live":
            return
        if capture is None or not hasattr(capture, "set_volatile"):
            return
        if snap.get("state") != "recording":
            if dbg:
                print("exposure: state", snap.get("state"), flush=True)
            return
        now = time.monotonic()
        cap = snap.get("capture") or {}
        try:
            exp = cap.get("exposure_us") or []
            gain = cap.get("gain") or []
            laser = cap.get("laser_percent")
            if len(exp) < 3 or len(gain) < 3 or laser is None:
                return
            current = exposure_mod.ExposureSettings(
                {i: int(exp[i]) for i in range(3)},
                {i: int(gain[i]) for i in range(3)}, int(laser))
            # A previous run may have left the control file at a pinned
            # setting (the capture child applies an existing file at start).
            # Re-establish the configured baseline once per recording run, so
            # "far away" always has headroom instead of inheriting max gain.
            if not self._exposure_baseline_done:
                self._exposure_baseline_done = True
                baseline = self._exposure_baseline(capture)
                if (baseline is not None
                        and baseline != current.as_capture_args()):
                    try:
                        record = capture.set_volatile(
                            exposure_us=baseline["exposure_us"],
                            gain=baseline["gain"],
                            laser_percent=baseline["laser_percent"],
                            source="auto_exposure_baseline")
                        self._write_exposure_entry({
                            "run": self._exposure_run_id,
                            "session_started_s": float(self._started_at or 0.0),
                            "at_s": time.time(),
                            "reason": "reset to configured baseline",
                            "changes": {"baseline": baseline},
                            "requested": baseline,
                            "readback_before": current.as_capture_args(),
                            "write": record,
                            "stats": None,
                            "at_limits": [],
                            "mismatch": None,
                            "source": "auto_exposure_baseline",
                        })
                        self._event("auto exposure: reset to the configured "
                                    "baseline")
                    except Exception as exc:
                        self._event("auto exposure baseline skipped: "
                                    f"{type(exc).__name__}: {exc}")
                    return
            detection_age = snap.get("detections_age_s")
            observed_at = (now if detection_age is None
                           else now - max(0.0, float(detection_age)))
            ir_points = list((snap.get("detections") or {}).get("left") or [])
            ir_box, ir_meter, ir_pts = self._ir_roi.box(
                ir_points, now, observed_at=observed_at)
            ir_stats = None
            if self._last_ir_image is not None:
                if ir_box is None:
                    ir_stats = exposure_mod.FrameStats.from_image(
                        self._last_ir_image)
                    ir_stats.metering = "frame"
                else:
                    ir_stats = exposure_mod.FrameStats.from_image(
                        self._last_ir_image, bbox=ir_box, exclude=ir_pts)
                    ir_stats.metering = ir_meter
            rgb_points = list((snap.get("detections") or {}).get("tex") or [])
            rgb_box, rgb_meter, rgb_pts = self._rgb_roi.box(
                rgb_points, now, observed_at=observed_at)
            rgb_stats = None
            if self._last_rgb_image is not None:
                if rgb_box is None:
                    rgb_stats = exposure_mod.FrameStats.from_image(
                        self._last_rgb_image)
                    rgb_stats.metering = "frame"
                else:
                    rgb_stats = exposure_mod.FrameStats.from_image(
                        self._last_rgb_image, bbox=rgb_box, exclude=rgb_pts)
                    rgb_stats.metering = rgb_meter
            decision = snap.get("last_decision") or {}
            metrics = ((decision.get("quality") or {}).get("metrics") or {})
            stationary = bool(metrics.get("stationary"))
            advice = self._auto_exposure.observe(
                ir=ir_stats, rgb=rgb_stats, current=current, now=now,
                stationary=stationary)
            if dbg:
                print(f"exposure: ir={ir_stats} rgb={rgb_stats} "
                      f"current={current} advice={advice.reason} "
                      f"changed={advice.changed} limits={advice.at_limits}",
                      flush=True)
            self._exposure_mismatch_seen = self._report_exposure_mismatch()
            if not advice.changed or advice.settings is None:
                return
            request = (
                tuple(sorted(dict(advice.settings.exposure_us).items())),
                tuple(sorted(dict(advice.settings.gain).items())),
                int(advice.settings.laser_percent))
            if (request == self._last_exposure_request
                    and (now - self._last_exposure_request_at) < 2.0):
                # The device polls the control file and reports the readback
                # with some lag.  Rewriting an identical request inside that
                # grace produced two duplicate writes for a frozen/clamped
                # value (a second hard-clipping step before the first was
                # observed is also unsafe).  The advisor re-emits after the
                # grace if the readback still differs.
                return
            self._last_exposure_request = request
            self._last_exposure_request_at = now
            record = capture.set_volatile(
                exposure_us=advice.settings.exposure_us,
                gain=advice.settings.gain,
                laser_percent=advice.settings.laser_percent,
                source="auto_exposure")
            self._log_exposure(advice, record, current)
            self._event("auto exposure: " + advice.reason)
        except Exception as exc:
            self._event(f"auto exposure skipped: {type(exc).__name__}: {exc}")

    @staticmethod
    def _exposure_baseline(capture) -> dict | None:
        """Configured volatile baseline (never hard-coded here)."""
        config = getattr(capture, "config", None)
        if config is None:
            return None
        try:
            exp = {int(k): int(v) for k, v in dict(config.exposure_us).items()}
            gain = {int(k): int(v) for k, v in dict(config.gain).items()}
            laser = int(config.laser_percent)
        except (AttributeError, TypeError, ValueError):
            return None
        if len(exp) < 3 or len(gain) < 3:
            return None
        return {"exposure_us": exp, "gain": gain, "laser_percent": laser}

    def _report_exposure_mismatch(self) -> int:
        """Surface (once per occurrence) a request the device did not apply."""
        status = self._auto_exposure.status()
        count = int(status.get("mismatch_count", 0) or 0)
        if count > self._exposure_mismatch_seen:
            mismatch = status.get("mismatch") or {}
            self._exposure_mismatch_seen = count
            self._event("auto exposure readback mismatch: requested "
                        f"{mismatch.get('requested')} read back "
                        f"{mismatch.get('readback')}")
        return count

    def _log_exposure(self, advice, record: dict, current) -> None:
        self._write_exposure_entry({
            "run": self._exposure_run_id,
            "session_started_s": float(self._started_at or 0.0),
            "at_s": time.time(),
            "reason": advice.reason,
            "changes": advice.changes,
            "requested": (advice.settings.as_capture_args()
                          if advice.settings is not None else None),
            "readback_before": current.as_capture_args(),
            "write": record,
            "stats": advice.stats,
            "at_limits": list(advice.at_limits),
            "mismatch": advice.mismatch,
            "source": "auto_exposure",
        })
        self._write_exposure_status()

    def _write_exposure_entry(self, entry: dict) -> None:
        try:
            import json
            root = Path(self.controller.paths.root)
            self._exposure_log_path = root / "exposure.jsonl"
            with self._exposure_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        except Exception:
            pass

    def _write_exposure_status(self) -> None:
        """Small machine-readable status for diagnostics (not the log)."""
        try:
            import json
            root = Path(self.controller.paths.root)
            payload = {
                "run": self._exposure_run_id,
                "at_s": time.time(),
                "auto_exposure": self._auto_exposure.status(),
            }
            (root / "exposure-status.json").write_text(
                json.dumps(payload, indent=1, sort_keys=True, default=str) + "\n",
                encoding="utf-8")
        except Exception:
            pass

    # -- refresh ---------------------------------------------------------
    def refresh(self) -> None:
        if self._closing:
            return
        try:
            snap = self.controller.snapshot()
        except Exception as exc:
            self._fail(f"state read failed: {type(exc).__name__}: {exc}")
            return
        readiness = snap.get("readiness") or {}
        ready = bool(readiness.get("ready"))
        self._ready = ready
        state = str(snap.get("state") or "idle")
        if getattr(self, "_display_state", None):
            state = self._display_state
        self._apply_state(state)
        # The guidance engine's target is authoritative.  This window used to
        # shorten any target whose distance differed by >25 mm to "Move the
        # plate farther away", even when every distance band was satisfied --
        # the exact instruction that hid the missing right-hand corners in the
        # 52 GB session.  ``operator_guidance`` is a pure, tested function.
        view = status.operator_guidance(snap)
        if state in ("recording", "paused"):
            self.instruction.set_instruction(view["instruction"],
                                             view["detail"])
            self.instruction.set_ready(ready,
                                       list(readiness.get("missing") or []))
            self.footer_note.setText(view["footer"])
            if view.get("banner"):
                self.stop_banner.setText(str(view["banner"]))
                self.stop_banner.setVisible(True)
            else:
                self.stop_banner.setVisible(False)
            if view.get("stop_recommended"):
                self.btn_cancel.setText("Stop now && keep data")
                self.btn_cancel.setObjectName("primary")
            else:
                self.btn_cancel.setText("Stop && keep data")
                self.btn_cancel.setObjectName("ghost")
        else:
            self.stop_banner.setVisible(False)
            if view.get("banner") and view.get("tone") == "error":
                self.stop_banner.setText(str(view["banner"]))
                self.stop_banner.setVisible(True)
        self.progress.update_from(snap)
        self._update_coverage_map(snap)
        self.views_label.setText(f"{int((snap.get('counts') or {}).get('accepted', 0))} views")
        # 3-D pose at 5 Hz.  The latest pose is deliberately retained while
        # paused; a stale-pose marker is shown when the source has ended.
        now = time.time()
        if now - getattr(self, "_last_3d", 0.0) >= 0.2:
            self._last_3d = now
            camera = snap.get("camera_left") or {}
            if camera:
                self.plate.set_intrinsics(camera.get("fx", 1150.0),
                                          camera.get("fy", 1150.0),
                                          camera.get("cx", 640.0),
                                          camera.get("cy", 512.0))
            pose = snap.get("pose_rt")
            age = snap.get("pose_age_s")
            fresh = (pose is not None and
                     (age is None or float(age) <= 0.75))
            self.plate.set_tracking(bool(fresh))
            if pose and fresh:
                self.plate.set_pose(pose.get("R"), pose.get("t"))
            last = snap.get("last_pose") or {}
            bits = []
            if last.get("distance_mm") is not None:
                bits.append(f"{float(last['distance_mm']):.0f} mm")
            if last.get("tilt_deg") is not None:
                bits.append(f"tilt {float(last['tilt_deg']):.0f}°")
            if last.get("roll_deg") is not None:
                bits.append(f"roll {float(last['roll_deg']):.0f}°")
            self.plate.set_status("  ·  ".join(bits) or "point the plate at the scanner")
        cap = snap.get("capture") or {}
        source = snap.get("source") or {}
        kind = str(source.get("kind") or self._source_kind() or "")
        bits = []
        if kind:
            bits.append(kind)
        bits.append(state)
        if kind == "live":
            if source.get("serial"):
                bits.append(str(source["serial"]))
            acq_label = status.acquisition_label(cap)
            if acq_label:
                bits.append(acq_label)
            fps = _num(cap.get("fps"))
            if fps is not None:
                bits.append(f"{fps:.1f} fps (last measured)"
                            if acq_label.endswith(("UNVERIFIED", "FAILED",
                                                   "stopped — verified"))
                            else f"{fps:.1f} fps")
            if cap.get("groups_written") is not None:
                bits.append(f"{int(cap['groups_written'])} groups recorded")
        else:
            metrics = self.worker.metrics() if self.worker is not None else {}
            seen = int(metrics.get("groups_seen", 0) or 0)
            bits.append(f"{seen} groups processed")
            bits.append(f"{int((snap.get('counts') or {}).get('accepted', 0))} accepted")
        self.scanner_line.setText("  ·  ".join(bits) or "Scanner: starting…")
        elapsed = _num((snap.get("session") or {}).get("elapsed_s"))
        if elapsed is not None and elapsed > 0:
            self.session_line.setText(
                f"Session {int(elapsed)//60:02d}:{int(elapsed)%60:02d}  ·  "
                f"{int((snap.get('counts') or {}).get('accepted', 0))} views")
        if state == "recording":
            self._auto_expose(snap)
        if snap.get("error"):
            self._fail(str(snap["error"]))
        if self._advanced is not None and self._advanced.isVisible():
            self._advanced.refresh(snap)

    # -- shutdown --------------------------------------------------------
    def wait_for_finish(self, timeout: float = 600.0) -> bool:
        fw = self._finish_worker
        if fw is None:
            return True
        deadline = time.time() + timeout
        while fw.isRunning() and time.time() < deadline:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)
        # The last ``done`` signal is queued from the worker thread.  Process
        # it before returning; otherwise shutdown can write ui_metrics while
        # the window is still in the "finishing" state even though the solve
        # has completed.
        for _ in range(3):
            QtWidgets.QApplication.processEvents()
            time.sleep(0.01)
        return not fw.isRunning()

    def shutdown(self, timeout_s: float = 30.0) -> bool:
        if self._shutdown_done:
            return True
        self._timer.stop()
        self._clock.stop()
        ok = True
        fw = self._finish_worker
        if fw is not None and fw.isRunning():
            ok = self.wait_for_finish(timeout_s) and ok
        w = self.worker
        if w is not None and w.isRunning():
            w.request_stop()
            deadline = time.time() + timeout_s
            while w.isRunning() and time.time() < deadline:
                QtWidgets.QApplication.processEvents()
                time.sleep(0.01)
            ok = (not w.isRunning()) and ok
        # Stop and verify the scanner BEFORE writing evidence or hiding the
        # window.  The 52 GB session wrote ui_metrics while the process was
        # still streaming and only stopped it later in source.close().
        state = str(self.controller.snapshot().get("state") or "")
        try:
            if state in ("recording", "paused"):
                result = self.controller.cancel(reason="window closed")
            else:
                result = self.controller.stop_capture(reason="window closed")
        except Exception as exc:
            result = {"verified": False, "safe": False,
                      "reasons": [f"{type(exc).__name__}: {exc}"]}
            ok = False
        try:
            # populate source.final_verdict for ui_metrics even when the
            # controller already stopped the same supervisor
            self.source.stop(reason="window closed")
        except Exception:
            pass
        try:
            self._closing = False   # allow the one authoritative final refresh
            self._cancelling = True
            self.refresh()
            self._cancelling = False
        except Exception:
            pass
        self._closing = True
        self._write_ui_metrics()
        if not result.get("verified"):
            ok = False
        try:
            self.source.close()
        except Exception:
            ok = False
        try:
            self.controller.close()
        except Exception:
            pass
        # QThread wrappers are Python-owned children of this window.  Delete
        # them explicitly after they have stopped instead of relying on Qt's
        # parent-child destruction order at interpreter shutdown (the latter
        # can visit a parent before a child and touch freed QObject memory).
        if w is not None:
            try:
                w.deleteLater()
            except Exception:
                pass
            self.worker = None
        if fw is not None:
            try:
                fw.deleteLater()
            except Exception:
                pass
            self._finish_worker = None
        self._shutdown_done = True
        return ok

    def _write_ui_metrics(self) -> None:
        try:
            import json
            root = Path(self.controller.paths.root)
            root.mkdir(parents=True, exist_ok=True)
            snap = self.controller.snapshot()
            capture = snap.get("capture") or {}
            data = {
                "schema": "einstar-ui-metrics/2",
                "written_at_s": time.time(),
                "worker": (self.worker.metrics()
                           if self.worker is not None else None),
                "source": {k: v for k, v in
                           ((snap.get("source") or {}).items())},
                "session_state": snap.get("state"),
                "acquisition": capture.get("acquisition"),
                "capture_state": capture.get("state"),
                "guidance": snap.get("guidance"),
                "progress": (snap.get("readiness") or {}).get("progress"),
                "performance": snap.get("performance"),
                "error": snap.get("error") or self._ui_error,
                "capture_safe_off": getattr(
                    getattr(self.source, "capture", None),
                    "_safe_off_result", None),
                "source_final_verdict": getattr(self.source, "final_verdict", None),
                "finish": {
                    "validated": bool(self._result_validated),
                    "state": self._session_state,
                    "result_present": bool(self._finished_result),
                    "reasons": list(
                        (((self._finished_result or {}).get("validation")
                          if isinstance((self._finished_result or {}).get("validation"), dict)
                          else {}) or {}).get("verdict", {}).get("reasons") or []),
                },
                "auto_exposure": self._auto_exposure.history,
                "auto_exposure_status": self._auto_exposure.status(),
            }
            (root / "ui_metrics.json").write_text(
                json.dumps(data, indent=1, sort_keys=True) + "\n",
                encoding="utf-8")
        except Exception:
            pass

    def closeEvent(self, event):  # noqa: N802
        if self._finish_worker is not None and self._finish_worker.isRunning():
            event.ignore()
            self.finish_text.setText("Waiting for the final solve to finish safely…")
            QtCore.QTimer.singleShot(200, self.close)
            return
        w = self.worker
        if w is not None and w.isRunning():
            self._closing = True
            w.request_stop()
            self.status_pill.setText("●  STOPPING")
            event.ignore()
            QtCore.QTimer.singleShot(200, self.close)
            return
        self.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "finish_overlay"):
            self.finish_overlay.setGeometry(self.centralWidget().rect())
        # The documented 1180-wide window must not clip the third feed.  Give
        # the side column less room and let each feed shrink to 210 px there.
        narrow = self.width() < 1320
        if hasattr(self, "_side_host"):
            self._side_host.setFixedWidth(320 if narrow else 400)
        target = 210 if narrow else 260
        for feed in (getattr(self, "feed_left", None),
                     getattr(self, "feed_right", None),
                     getattr(self, "feed_colour", None)):
            if feed is not None:
                feed.setMinimumWidth(target)

    def snapshot_png(self, path: str) -> None:
        QtWidgets.QApplication.processEvents()
        self.grab().save(path)

    def maybe_autostart(self) -> None:
        if self.headless or not self._autostart:
            return
        QtCore.QTimer.singleShot(500, self._on_start)
