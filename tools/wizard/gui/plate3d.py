"""Lightweight 3-D preview of the calibration plate.

Renders the *known* plate geometry transformed by the estimated pose - no stereo
reconstruction, no OpenGL, no extra threads.  That is what the wizard needs: the
operator should see where the board is and how it is tilted, not a reconstructed
surface that costs more than the guidance itself.

Two views are supported:

* ``scanner`` - project the board through the left camera's real intrinsics, so
  the drawing matches what the camera sees;
* ``orbit``   - a synthetic camera the operator can drag, which makes the tilt
  and the third dimension readable when the plate is nearly fronto-parallel.

The widget never computes anything heavy: the board is 40 lattice points and four
corners, so a repaint is microseconds.  The window still throttles updates.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

__all__ = ["PlatePreview", "plate_geometry"]


def plate_geometry(pitch_mm: float = 28.0, cols: int = 8, rows: int = 5):
    """Lattice points (mm, board frame) plus the outline corners."""
    us = np.arange(cols, dtype=float) - (cols - 1) / 2.0
    vs = np.arange(rows, dtype=float) - (rows - 1) / 2.0
    grid = np.array([[u * pitch_mm, v * pitch_mm, 0.0] for v in vs for u in us])
    half_u = (cols / 2.0) * pitch_mm
    half_v = (rows / 2.0) * pitch_mm
    corners = np.array([[-half_u, -half_v, 0.0], [half_u, -half_v, 0.0],
                        [half_u, half_v, 0.0], [-half_u, half_v, 0.0]])
    return grid, corners


class PlatePreview(QtWidgets.QFrame):
    """Paint the board at its estimated pose; drag to orbit."""

    def __init__(self, parent=None, *, pitch_mm: float = 28.0):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setMinimumSize(240, 200)
        self.setMouseTracking(True)
        self.grid, self.corners = plate_geometry(pitch_mm)
        self._R = np.eye(3)
        self._t = np.array([0.0, 0.0, 400.0])
        self._K = np.array([[1150.0, 0.0, 640.0], [0.0, 1150.0, 512.0],
                            [0.0, 0.0, 1.0]])
        self._have_pose = False
        self._target_R = np.eye(3)
        self._target_t = np.array([0.0, 0.0, 400.0])
        #: a pose that has just arrived is tracked; the window marks it False
        #: when no recent decision exists
        self._tracking = True
        self.mode = "scanner"          # scanner | orbit
        self.azimuth_deg = 25.0
        self.elevation_deg = 18.0
        self._drag = None
        self._status = "waiting for a pose"
        # A decision arrives at ~5-10 Hz; the plate is a rigid body, so a
        # short critically-damped interpolation makes the preview smooth
        # without inventing a pose: it always converges to the last measured
        # one and never extrapolates past it.
        self._anim = QtCore.QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._animate)

    # -- data ------------------------------------------------------------
    def set_intrinsics(self, fx: float, fy: float, cx: float, cy: float) -> None:
        self._K = np.array([[float(fx), 0.0, float(cx)],
                            [0.0, float(fy), float(cy)],
                            [0.0, 0.0, 1.0]])

    def set_pose(self, R, t) -> None:
        try:
            target_R = np.asarray(R, float).reshape(3, 3)
            target_t = np.asarray(t, float).reshape(3)
        except Exception:
            return
        self._target_R = target_R
        self._target_t = target_t
        self._tracking = True
        if not self._have_pose:
            # first acquisition: show it immediately, do not sweep in from the
            # placeholder pose
            self._R = target_R.copy()
            self._t = target_t.copy()
            self._have_pose = True
            self.update()
            return
        if not self.isVisible():
            # Offscreen use (snapshot tests, image export) is deterministic:
            # no interpolation timer will ever fire.
            self._R = target_R.copy()
            self._t = target_t.copy()
            self.update()
            return
        if not self._anim.isActive():
            self._anim.start()

    def set_tracking(self, tracked: bool) -> None:
        """Truthful visual state: no pose means no confident plate."""
        tracked = bool(tracked)
        if tracked == self._tracking:
            return
        self._tracking = tracked
        self.update()

    def _animate(self) -> None:
        if not self._have_pose:
            self._anim.stop()
            return
        alpha = 0.45
        dR = self._target_R - self._R
        if float(np.abs(dR).max()) < 1e-4:
            self._R = self._target_R.copy()
        else:
            # orthonormalise the interpolated rotation (cheap 3x3 SVD)
            guess = self._R + alpha * dR
            u, _, vt = np.linalg.svd(guess)
            self._R = u @ vt
        dt = self._target_t - self._t
        if float(np.abs(dt).max()) < 0.05:
            self._t = self._target_t.copy()
        else:
            self._t = self._t + alpha * dt
        converged = (float(np.abs(self._target_R - self._R).max()) < 1e-4
                     and float(np.abs(self._target_t - self._t).max()) < 0.05)
        if converged:
            self._R = self._target_R.copy()
            self._t = self._target_t.copy()
            self._anim.stop()
        self.update()

    def set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.update()

    def clear(self) -> None:
        self._anim.stop()
        self._have_pose = False
        self.update()

    def toggle_mode(self) -> None:
        self.mode = "orbit" if self.mode == "scanner" else "scanner"
        self.update()

    # -- interaction -----------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        self._drag = QtCore.QPointF(event.position())

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag is None:
            return
        delta = event.position() - self._drag
        self._drag = QtCore.QPointF(event.position())
        self.azimuth_deg = (self.azimuth_deg + delta.x() * 0.6) % 360.0
        self.elevation_deg = float(np.clip(self.elevation_deg + delta.y() * 0.5,
                                           -85.0, 85.0))
        self.mode = "orbit"
        self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.mode = "scanner"
        self.azimuth_deg, self.elevation_deg = 25.0, 18.0
        self.update()

    # -- projection ------------------------------------------------------
    def _camera_points(self):
        """Board points in camera coordinates, plus the camera intrinsics."""
        world = np.vstack([self.grid, self.corners])
        R = self._R
        if self.mode == "scanner":
            return (R @ world.T).T + self._t, self._K
        radius = max(160.0, 2.6 * float(np.abs(self.grid).max()))
        az = math.radians(self.azimuth_deg)
        el = math.radians(self.elevation_deg)
        direction = np.array([math.sin(az) * math.cos(el),
                              -math.sin(el),
                              math.cos(az) * math.cos(el)])
        cam = -direction * radius            # camera position in board frame
        forward = -cam / max(np.linalg.norm(cam), 1e-9)
        up = np.array([0.0, 1.0, 0.0])
        right = np.cross(up, forward)
        if np.linalg.norm(right) < 1e-6:
            right = np.array([1.0, 0.0, 0.0])
        right = right / np.linalg.norm(right)
        up2 = np.cross(forward, right)
        Rview = np.stack([right, up2, forward])
        pts = (Rview @ (R @ world.T)).T - (Rview @ cam)
        f = 1.7 * radius
        K = np.array([[f, 0.0, 0.0], [0.0, f, 0.0], [0.0, 0.0, 1.0]])
        return pts, K

    def _project(self, rect: QtCore.QRectF):
        """Project to widget pixels, fitted to the rect; None where behind."""
        pts, K = self._camera_points()
        raw = []
        for X in pts:
            z = float(X[2])
            if z <= 1.0:
                raw.append(None)
                continue
            u = float(K[0, 0]) * float(X[0]) / z + float(K[0, 2])
            v = float(K[1, 1]) * float(X[1]) / z + float(K[1, 2])
            raw.append((u, v))
        usable = [p for p in raw if p is not None]
        if len(usable) < 4:
            return raw, None
        if self.mode == "scanner":
            # Fixed image-to-widget mapping: the camera frame is 1280x1024.
            # Auto-fitting the board's bbox made translation and distance
            # invisible (the plate always filled the card).
            margin = 24.0
            sx = (rect.width() - 2 * margin) / 1280.0
            sy = (rect.height() - 2 * margin - 22.0) / 1024.0
            scale = max(1e-6, min(sx, sy))
            ox = rect.center().x()
            oy = rect.center().y() + 4.0
            mapped = [(None if p is None else
                       (ox + (p[0] - 640.0) * scale,
                        oy + (p[1] - 512.0) * scale)) for p in raw]
            return mapped, scale
        us = np.array([p[0] for p in usable])
        vs = np.array([p[1] for p in usable])
        span_u = max(float(us.max() - us.min()), 1e-6)
        span_v = max(float(vs.max() - vs.min()), 1e-6)
        margin = 26.0
        sx = (rect.width() - 2 * margin) / span_u
        sy = (rect.height() - 2 * margin - 22) / span_v
        scale = min(sx, sy)
        cu = (us.max() + us.min()) / 2.0
        cv = (vs.max() + vs.min()) / 2.0
        ox = rect.center().x()
        oy = rect.center().y() + 4
        return [(None if p is None else (ox + (p[0] - cu) * scale,
                                         oy + (p[1] - cv) * scale)) for p in raw], scale

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        super().paintEvent(event)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._paint(p, QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2))
        p.end()

    def render_image(self, width: int, height: int) -> QtGui.QImage:
        # An offscreen render is a deterministic still: show the newest pose
        # rather than a mid-interpolation frame.
        if self._have_pose:
            self._R = self._target_R.copy()
            self._t = self._target_t.copy()
            self._anim.stop()
        img = QtGui.QImage(int(width), int(height),
                           QtGui.QImage.Format.Format_RGB32)
        img.fill(QtGui.QColor("#0b0e12"))
        p = QtGui.QPainter(img)
        self._paint(p, QtCore.QRectF(0, 0, width, height))
        p.end()
        return img

    def _paint(self, p: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        self._paint_board(p, rect)

    def _paint_board(self, p: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        p.setPen(QtGui.QColor(theme.MUTED))
        f = p.font(); f.setPointSize(9); p.setFont(f)
        p.drawText(QtCore.QRectF(rect.left() + 6, rect.top() + 2,
                                 rect.width() - 12, 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft,
                   "3-D PLATE  ·  " + ("drag to orbit"
                                       if self.mode == "orbit"
                                       else "scanner view"))
        if not self._have_pose:
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                       "waiting for the plate…")
            return
        if not self._tracking:
            # No recent measurement: keep the geometry on screen for context
            # but say so, rather than presenting a stale pose as current.
            p.setOpacity(0.35)
        raw, scale = self._project(rect)
        if scale is None:
            p.setPen(QtGui.QColor(theme.WARN))
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                       "plate behind the camera")
            return
        n_grid = len(self.grid)
        grid_px, corner_px = raw[:n_grid], raw[n_grid:]
        if any(v is None for v in corner_px):
            p.setPen(QtGui.QColor(theme.WARN))
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                       "part of the plate is behind the camera")
            return
        quad = QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in corner_px])
        p.setPen(QtGui.QPen(QtGui.QColor(theme.ACCENT), 1.6))
        p.setBrush(QtGui.QColor(30, 46, 60, 170))
        p.drawPolygon(quad)
        # draw markers far-to-near so a tilted board reads correctly
        order = sorted(range(n_grid),
                       key=lambda i: -float(self.grid[i][0] ** 2))
        for i in order:
            pt = grid_px[i]
            if pt is None:
                continue
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(theme.OK))
            p.drawEllipse(QtCore.QPointF(*pt), 3.0, 3.0)
        # board axes from the centre, scaled into the drawing
        centre = np.mean([pt for pt in corner_px], axis=0)
        for axis, colour, label in ((np.array([1.0, 0.0, 0.0]), "#ff6b6b", "u"),
                                    (np.array([0.0, 1.0, 0.0]), "#4ade80", "v")):
            tip_world = self._R @ (axis * 40.0)
            if self.mode == "scanner":
                tip_cam = tip_world + self._t
                K = self._K
                if tip_cam[2] <= 1.0:
                    continue
                u = float(K[0, 0]) * tip_cam[0] / tip_cam[2] + float(K[0, 2])
                v = float(K[1, 1]) * tip_cam[1] / tip_cam[2] + float(K[1, 2])
                # reuse the fitted scale through the same projection of the
                # centre so the arrow matches the drawing
                c_cam = self._t
                if c_cam[2] <= 1.0:
                    continue
                uc = float(K[0, 0]) * c_cam[0] / c_cam[2] + float(K[0, 2])
                vc = float(K[1, 1]) * c_cam[1] / c_cam[2] + float(K[1, 2])
                tip = (centre[0] + (u - uc) * scale, centre[1] + (v - vc) * scale)
            else:
                tip = (centre[0] + 26.0 * (label == "u") + 0.0,
                       centre[1] - 26.0 * (label == "v"))
            p.setPen(QtGui.QPen(QtGui.QColor(colour), 2.0))
            p.drawLine(QtCore.QPointF(*centre), QtCore.QPointF(*tip))
            p.setPen(QtGui.QColor(colour))
            p.drawText(QtCore.QPointF(tip[0] + 3, tip[1] - 3), label)
        if not self._tracking:
            p.setOpacity(1.0)
            p.setPen(QtGui.QColor(theme.WARN))
            p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                       "plate not tracked — bring it into view")
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRectF(rect.left() + 6, rect.bottom() - 18,
                                 rect.width() - 12, 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft, self._status)
