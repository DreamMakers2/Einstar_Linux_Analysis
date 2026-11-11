"""Interactive 3-D view for the scanning application.

The view is a QPainter rasteriser with a painter's algorithm, not OpenGL: it has
to work in the offscreen tests (no GPU context), it must not depend on a driver,
and a scan mesh of a few tens of thousands of triangles is redrawn only when the
camera moves or the mesh changes, not per video frame.

It supports the interactions a user actually needs on a scan: orbit (drag),
zoom (wheel), reset (double click), and a depth-shaded / normal-shaded render so
the surface is readable.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets


class MeshView(QtWidgets.QWidget):
    """Orbitable, depth-sorted mesh view."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 300)
        self.setMouseTracking(True)
        self._vertices: np.ndarray | None = None
        self._faces: np.ndarray | None = None
        self._points: np.ndarray | None = None
        self._caption = "no mesh"
        self.yaw = 35.0
        self.pitch = -25.0
        self.zoom = 1.0
        self._last_mouse: QtCore.QPoint | None = None
        self._cache: QtGui.QImage | None = None
        self._cache_key: tuple | None = None

    # -- content ----------------------------------------------------------
    def set_mesh(self, vertices, faces) -> None:
        self._vertices = None if vertices is None else np.asarray(vertices, float)
        self._faces = None if faces is None else np.asarray(faces, np.int64)
        self._cache = None
        self.update()

    def set_points(self, points) -> None:
        self._points = None if points is None else np.asarray(points, float)
        self._cache = None
        self.update()

    def set_caption(self, text: str) -> None:
        self._caption = str(text)
        self.update()

    def reset_view(self) -> None:
        self.yaw, self.pitch, self.zoom = 35.0, -25.0, 1.0
        self._cache = None
        self.update()

    # -- camera -----------------------------------------------------------
    def orbit(self, dx: float, dy: float) -> None:
        self.yaw += float(dx) * 0.4
        self.pitch = float(np.clip(self.pitch + float(dy) * 0.4, -89.0, 89.0))
        self._cache = None
        self.update()

    def apply_zoom(self, factor: float) -> None:
        self.zoom = float(np.clip(self.zoom * float(factor), 0.05, 20.0))
        self._cache = None
        self.update()

    def rotation(self) -> np.ndarray:
        ry = math.radians(self.yaw)
        rx = math.radians(self.pitch)
        Ry = np.array([[math.cos(ry), 0, math.sin(ry)],
                       [0, 1, 0],
                       [-math.sin(ry), 0, math.cos(ry)]])
        Rx = np.array([[1, 0, 0],
                       [0, math.cos(rx), -math.sin(rx)],
                       [0, math.sin(rx), math.cos(rx)]])
        return Rx @ Ry

    # -- rendering --------------------------------------------------------
    def _project(self, points: np.ndarray, width: int, height: int):
        R = self.rotation()
        p = (R @ points.T).T
        span = float(np.max(np.ptp(p, axis=0))) if len(p) else 1.0
        span = max(span, 1e-6)
        scale = 0.85 * min(width, height) / span * self.zoom
        centre = p.mean(axis=0) if len(p) else np.zeros(3)
        x = width * 0.5 + (p[:, 0] - centre[0]) * scale
        y = height * 0.5 - (p[:, 1] - centre[1]) * scale
        return np.stack([x, y], axis=1), p[:, 2] - centre[2], scale

    def render_image(self, width: int, height: int) -> QtGui.QImage:
        image = QtGui.QImage(max(width, 8), max(height, 8),
                             QtGui.QImage.Format_RGB32)
        image.fill(QtGui.QColor("#12151a"))
        painter = QtGui.QPainter(image)
        try:
            if self._faces is not None and len(self._faces):
                self._paint_mesh(painter, width, height)
            elif self._points is not None and len(self._points):
                self._paint_points(painter, width, height)
            else:
                painter.setPen(QtGui.QColor("#94a3b8"))
                painter.drawText(image.rect(), QtCore.Qt.AlignCenter, self._caption)
        finally:
            painter.end()
        return image

    def _paint_points(self, painter, width, height) -> None:
        xy, z, _ = self._project(self._points, width, height)
        order = np.argsort(z)
        lo, hi = float(np.percentile(z, 5)), float(np.percentile(z, 95))
        span = max(hi - lo, 1e-6)
        for i in order:
            t = min(max((z[i] - lo) / span, 0.0), 1.0)
            painter.setPen(QtGui.QPen(QtGui.QColor.fromHsvF(0.58 - 0.5 * t, 0.85, 0.95), 1.5))
            painter.drawPoint(QtCore.QPointF(float(xy[i, 0]), float(xy[i, 1])))

    def _paint_mesh(self, painter, width, height) -> None:
        verts = self._vertices
        faces = self._faces
        xy, z, _ = self._project(verts, width, height)
        tri = xy[faces]
        zc = z[faces].mean(axis=1)
        # world normals, rotated with the view, for shading
        p = (self.rotation() @ verts.T).T
        n = np.cross(p[faces[:, 1]] - p[faces[:, 0]], p[faces[:, 2]] - p[faces[:, 0]])
        length = np.linalg.norm(n, axis=1, keepdims=True)
        length[length == 0] = 1.0
        n = n / length
        light = np.array([0.35, 0.45, 0.82])
        light = light / np.linalg.norm(light)
        shade = np.clip(np.abs(n @ light), 0.05, 1.0)
        order = np.argsort(zc)
        lo, hi = float(np.percentile(zc, 5)), float(np.percentile(zc, 95))
        span = max(hi - lo, 1e-6)
        painter.setPen(QtCore.Qt.NoPen)
        for i in order:
            t = min(max((zc[i] - lo) / span, 0.0), 1.0)
            base = QtGui.QColor.fromHsvF(0.58 - 0.45 * t, 0.55, 0.35 + 0.6 * float(shade[i]))
            poly = QtGui.QPolygonF([QtCore.QPointF(float(tri[i, k, 0]),
                                                   float(tri[i, k, 1])) for k in range(3)])
            painter.setBrush(base)
            painter.drawPolygon(poly)

    # -- Qt events --------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self.render_image(self.width(), self.height())
        painter = QtGui.QPainter(self)
        painter.drawImage(0, 0, self._cache)
        painter.setPen(QtGui.QColor("#94a3b8"))
        painter.drawText(self.rect().adjusted(8, 6, -8, -8),
                         QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop, self._caption)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._last_mouse = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._last_mouse is None or not (event.buttons() & QtCore.Qt.LeftButton):
            return
        pos = event.position().toPoint()
        delta = pos - self._last_mouse
        self._last_mouse = pos
        self.orbit(delta.x(), delta.y())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._last_mouse = None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.reset_view()

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        self.apply_zoom(1.12 ** steps)
