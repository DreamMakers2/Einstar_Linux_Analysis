"""Image tiles: three camera previews with marker/pose overlays, plus the
coverage map.  All drawing is plain QPainter so the same widgets render in the
headless snapshot path.
"""
from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from . import theme


def ndarray_to_qimage(gray: np.ndarray) -> QtGui.QImage:
    """8-bit grey numpy array -> QImage (copies, so the caller may reuse)."""
    img = np.ascontiguousarray(gray, dtype=np.uint8)
    h, w = img.shape[:2]
    qimg = QtGui.QImage(img.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8)
    return qimg.copy()


def apply_display_gain(bgr: np.ndarray) -> np.ndarray:
    """Grey-world white balance for preview only (never used for calibration)."""
    img = np.asarray(bgr, float)
    means = img.reshape(-1, 3).mean(axis=0) + 1e-6
    gain = means.mean() / means
    gain = np.clip(gain, 0.4, 2.5)
    out = np.clip(img * gain.reshape(1, 1, 3), 0, 255).astype(np.uint8)
    return out


def bgr_to_qimage(bgr: np.ndarray) -> QtGui.QImage:
    img = np.ascontiguousarray(bgr, dtype=np.uint8)
    h, w = img.shape[:2]
    qimg = QtGui.QImage(img.data, w, h, 3 * w, QtGui.QImage.Format.Format_BGR888)
    return qimg.copy()


class CameraTile(QtWidgets.QFrame):
    """One camera preview with an optional marker overlay and a status strip."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumSize(320, 260)
        self._title = title
        self._qimage: QtGui.QImage | None = None
        self._points: np.ndarray | None = None
        self._axes: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._status = "no signal"
        self._status_colour = theme.MUTED
        self._badge = ""
        self._badge_colour = theme.MUTED
        self._stale = True

    # -- data -----------------------------------------------------------
    def set_frame(self, image: np.ndarray | None, points=None,
                  axes=None, colour: bool = False) -> None:
        if image is None:
            self._qimage = None
        elif colour:
            self._qimage = bgr_to_qimage(apply_display_gain(image))
        else:
            self._qimage = ndarray_to_qimage(image)
        self._points = None if points is None else np.asarray(points, float).reshape(-1, 2)
        self._axes = axes
        self._stale = image is None
        self.update()

    def set_status(self, text: str, colour: str | None = None) -> None:
        self._status = text
        self._status_colour = colour or theme.MUTED
        self.update()

    def set_badge(self, text: str, colour: str | None = None) -> None:
        self._badge = text
        self._badge_colour = colour or theme.MUTED
        self.update()

    # -- painting -------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        header_h = 22
        strip_h = 20
        image_rect = QtCore.QRect(rect.left() + 6, rect.top() + header_h,
                                  rect.width() - 12,
                                  rect.height() - header_h - strip_h - 6)

        p.setPen(QtGui.QColor(theme.MUTED))
        f = p.font(); f.setPointSize(9); p.setFont(f)
        p.drawText(QtCore.QRect(rect.left() + 8, rect.top() + 3, rect.width() - 16, 18),
                   QtCore.Qt.AlignmentFlag.AlignLeft, self._title)

        if self._badge:
            p.setPen(QtGui.QColor(self._badge_colour))
            p.drawText(QtCore.QRect(rect.left() + 8, rect.top() + 3, rect.width() - 16, 18),
                       QtCore.Qt.AlignmentFlag.AlignRight, self._badge)

        p.fillRect(image_rect, QtGui.QColor("#0b0e12"))
        if self._qimage is not None and not self._qimage.isNull():
            scaled = self._qimage.scaled(
                image_rect.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
            x = image_rect.left() + (image_rect.width() - scaled.width()) // 2
            y = image_rect.top() + (image_rect.height() - scaled.height()) // 2
            p.drawImage(x, y, scaled)
            if self._points is not None and len(self._points):
                sx = scaled.width() / self._qimage.width()
                sy = scaled.height() / self._qimage.height()
                p.setPen(QtGui.QPen(QtGui.QColor(theme.OK), 1.2))
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                for u, v in self._points:
                    p.drawEllipse(QtCore.QPointF(x + u * sx, y + v * sy), 5.0, 5.0)
            if self._axes is not None:
                self._draw_axes(p, x, y, scaled.width() / self._qimage.width(),
                                scaled.height() / self._qimage.height())
        else:
            p.setPen(QtGui.QColor(theme.MUTED))
            f = p.font(); f.setPointSize(11); p.setFont(f)
            p.drawText(image_rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                       "waiting for frames…" if self._stale else "no image")

        p.setPen(QtGui.QColor(self._status_colour))
        f = p.font(); f.setPointSize(9); p.setFont(f)
        p.drawText(QtCore.QRect(rect.left() + 8, rect.bottom() - strip_h,
                                rect.width() - 16, strip_h - 2),
                   QtCore.Qt.AlignmentFlag.AlignLeft, self._status)

    def _draw_axes(self, p: QtGui.QPainter, ox: float, oy: float,
                   sx: float, sy: float) -> None:
        """Draw the plate coordinate frame: origin + two axes, in pixels."""
        origin, ax, ay = self._axes
        o = QtCore.QPointF(ox + float(origin[0]) * sx, oy + float(origin[1]) * sy)
        for vec, colour, label in ((ax, "#ff6b6b", "u"), (ay, "#4ade80", "v")):
            tip = QtCore.QPointF(ox + float(vec[0]) * sx, oy + float(vec[1]) * sy)
            p.setPen(QtGui.QPen(QtGui.QColor(colour), 2.0))
            p.drawLine(o, tip)
            p.setPen(QtGui.QColor(colour))
            p.drawText(tip + QtCore.QPointF(3, -3), label)
        p.setPen(QtGui.QPen(QtGui.QColor(theme.INFO), 2.0))
        p.setBrush(QtGui.QColor(theme.INFO))
        p.drawEllipse(o, 3.5, 3.5)


class CoverageTile(QtWidgets.QFrame):
    """3x3 image-region occupancy + distance bands + tilt quadrants.

    This is the operator's "where has the plate been" map: it makes missing
    coverage obvious without reading numbers.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumSize(320, 260)
        self._regions = np.zeros((3, 3), int)
        self._distances: list[float] = []
        self._tilts: list[tuple[float, float]] = []
        self._rolls: list[float] = []
        self._ready = False

    def set_data(self, regions, distances, tilts, rolls, ready: bool) -> None:
        self._regions = np.asarray(regions, int).reshape(3, 3)
        self._distances = list(distances)
        self._tilts = list(tilts)
        self._rolls = list(rolls)
        self._ready = ready
        self.update()

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(8, 8, -8, -8)
        f = p.font(); f.setPointSize(9); p.setFont(f)
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRect(rect.left(), rect.top(), rect.width(), 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft, "IMAGE COVERAGE  ·  plate centre")
        grid = QtCore.QRect(rect.left(), rect.top() + 20, min(rect.width() - 120, 210),
                            min(rect.height() - 90, 160))
        cell_w = grid.width() / 3
        cell_h = grid.height() / 3
        vmax = max(1, int(self._regions.max()))
        for r in range(3):
            for c in range(3):
                n = int(self._regions[r, c])
                t = 0.0 if n == 0 else min(1.0, 0.35 + 0.65 * n / max(vmax, 1))
                colour = QtGui.QColor(theme.OK)
                colour.setAlphaF(0.15 + 0.75 * t if n else 0.08)
                p.fillRect(QtCore.QRectF(grid.left() + c * cell_w + 2,
                                         grid.top() + r * cell_h + 2,
                                         cell_w - 4, cell_h - 4), colour)
                if n:
                    p.setPen(QtGui.QColor("#06251a"))
                    p.drawText(QtCore.QRectF(grid.left() + c * cell_w + 2,
                                             grid.top() + r * cell_h + 2,
                                             cell_w - 4, cell_h - 4),
                               QtCore.Qt.AlignmentFlag.AlignCenter, str(n))
        p.setPen(QtGui.QPen(QtGui.QColor(theme.BORDER), 1))
        p.drawRect(grid)

        # distance bands
        bx = grid.right() + 14
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRect(bx, rect.top(), rect.width() - (bx - rect.left()), 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft, "DEPTH BANDS (mm)")
        bands = [(300, 380), (380, 460), (460, 560)]
        for i, (lo, hi) in enumerate(bands):
            n = sum(1 for d in self._distances if lo <= d < hi)
            y = rect.top() + 22 + i * 20
            p.setPen(QtGui.QColor(theme.OK if n else theme.MUTED))
            p.drawText(QtCore.QRect(bx, y, rect.width() - (bx - rect.left()), 18),
                       QtCore.Qt.AlignmentFlag.AlignLeft, f"{lo}–{hi}: {n}")

        # tilt quadrants + roll sectors
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRect(rect.left(), grid.bottom() + 8, rect.width(), 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft,
                   f"TILT QUADRANTS  ·  ROLL SECTORS  ·  ready={self._ready}")
        quads = [0, 0, 0, 0]
        for tx, ty in self._tilts:
            if abs(tx) < 8 and abs(ty) < 8:
                continue
            idx = (1 if ty > 0 else 0) * 2 + (1 if tx > 0 else 0)
            quads[idx] += 1
        sectors = [0, 0, 0, 0]
        for rr in self._rolls:
            sectors[int(((rr + 180.0) % 360.0) // 90.0) % 4] += 1
        y = grid.bottom() + 26
        for i, n in enumerate(quads):
            colour = theme.OK if n else theme.BORDER
            p.fillRect(QtCore.QRect(rect.left() + i * 26, y, 22, 16), QtGui.QColor(colour))
            p.setPen(QtGui.QColor("#06251a"))
            p.drawText(QtCore.QRect(rect.left() + i * 26, y, 22, 16),
                       QtCore.Qt.AlignmentFlag.AlignCenter, str(n))
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRect(rect.left() + 112, y, 80, 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft, "roll:")
        for i, n in enumerate(sectors):
            colour = theme.INFO if n else theme.BORDER
            p.fillRect(QtCore.QRect(rect.left() + 152 + i * 24, y, 20, 16),
                       QtGui.QColor(colour))
            p.setPen(QtGui.QColor("#04121f"))
            p.drawText(QtCore.QRect(rect.left() + 152 + i * 24, y, 20, 16),
                       QtCore.Qt.AlignmentFlag.AlignCenter, str(n))
