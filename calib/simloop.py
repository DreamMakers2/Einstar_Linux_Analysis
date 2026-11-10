"""Closed-loop simulation of the guided calibration session.

No hardware is available, so the operator and the three cameras are simulated:

* :func:`render_frame` draws the board (plate quad + circular markers) as the
  three cameras would see it, with blur, noise, saturation and missing markers;
* :class:`VirtualOperator` moves the scanner to a requested board pose with a
  movement error and captures a synchronous frame set;
* :func:`run_closed_loop` drives :class:`calib.session.CalibrationSession`
  exactly as the live system would: capture -> quality/gain decision -> solve
  -> recommend -> move.

This is the harness used for the architecture comparison (online strategies).
It deliberately includes a *detector error model* (2-D Gaussian, optional
outliers and tilt-dependent eccentricity bias) so the comparison is not
detector-free; the real detector is evaluated separately on vendor images.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from . import guidance, plate
from .calibrate import CAMERAS, CameraIntrinsics, Observation
from .geometry import project
from .synth import SyntheticScene, make_scene


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_frame(scene: SyntheticScene, R: np.ndarray, t: np.ndarray,
                 rng: Optional[np.random.Generator] = None,
                 blur_sigma: float = 0.0, noise: float = 2.0,
                 saturation_frac: float = 0.0, missing_frac: float = 0.0,
                 size: tuple[int, int] = (1280, 1024)) -> dict[str, np.ndarray]:
    """Render one synchronous three-camera frame set."""
    rng = rng or np.random.default_rng(0)
    w, h = size
    obj = plate.board_points_mm(scene.pitch_mm)
    big_uv = {(2.0, 1.0), (4.0, 1.0), (5.0, 1.0), (3.5, 3.0)}
    out = {}
    for c in CAMERAS:
        Rc, tc = scene.extrinsics[c]
        Rw = Rc @ R
        tw = Rc @ t + tc
        Xc = obj @ Rw.T + tw
        uv = project(scene.cameras[c].K, scene.cameras[c].kc, Xc)
        img = np.full((h, w), 16, np.uint8)
        # plate quad: outer 8x6 pitch rectangle projected
        corners = np.array([[-112.0, 84.0, 0], [112.0, 84.0, 0],
                            [112.0, -84.0, 0], [-112.0, -84.0, 0]])
        Xcc = corners @ Rw.T + tw
        uvc = project(scene.cameras[c].K, scene.cameras[c].kc, Xcc)
        if (np.all(np.isfinite(uvc)) and np.abs(uvc).max() < 5000.0
                and 0 <= uvc[:, 0].min() and uvc[:, 0].max() < w
                and 0 <= uvc[:, 1].min() and uvc[:, 1].max() < h):
            poly = np.clip(uvc, [0, 0], [w - 1, h - 1]).astype(np.int32)
            if cv2.contourArea(poly) > 200:
                cv2.fillConvexPoly(img, poly, 78)
        for i, (u, v) in enumerate(uv):
            if not (0 <= u < w and 0 <= v < h):
                continue
            if rng.random() < missing_frac:
                continue
            # approximate projected radius from local scale (28 mm marker pitch
            # is ~ marker diameter 1.0 pitch; radius ~ pitch/2)
            j = min(i + 1, len(uv) - 1)
            pitch_px = max(4.0, float(np.linalg.norm(uv[j] - uv[i]))) if j != i else 10.0
            r = max(2, int(round(0.18 * pitch_px)))
            uv_key = (float(plate.lattice_uv()[i][0]), float(plate.lattice_uv()[i][1]))
            val = 255 if uv_key in big_uv else 235
            cv2.circle(img, (int(round(u)), int(round(v))), r, int(val), -1, cv2.LINE_AA)
        if saturation_frac > 0:
            m = rng.random((h, w)) < saturation_frac
            img[m] = 255
        if blur_sigma > 0:
            img = cv2.GaussianBlur(img, (0, 0), blur_sigma)
        if noise > 0:
            img = np.clip(img.astype(np.float32) + rng.normal(0, noise, (h, w)), 0, 255).astype(np.uint8)
        out[c] = img
    return out


def observations_from_pose(scene: SyntheticScene, R: np.ndarray, t: np.ndarray,
                           view: int = 0,
                           noise_px: float = 0.2, outlier_frac: float = 0.0,
                           missing_frac: float = 0.0,
                           rng: Optional[np.random.Generator] = None,
                           eccentricity: bool = False) -> list[Observation]:
    """Noise/outlier model for a captured frame set (no image rendering)."""
    rng = rng or np.random.default_rng(0)
    obj = plate.board_points_mm(scene.pitch_mm)
    out = []
    for c in CAMERAS:
        Rc, tc = scene.extrinsics[c]
        Rw = Rc @ R
        tw = Rc @ t + tc
        Xc = obj @ Rw.T + tw
        uv = project(scene.cameras[c].K, scene.cameras[c].kc, Xc)
        uv = uv + rng.normal(0, noise_px, uv.shape)
        if eccentricity:
            # first-order perspective bias of a projected circle: the ellipse
            # centre is displaced radially outward by ~f (R/d)^2 sin t / cos^3 t
            # (research review eq. 1); approximate here with the board tilt.
            n = np.asarray(R)[:, 2]
            tilt = np.arctan2(np.linalg.norm(n[:2]), abs(n[2]))
            f = scene.cameras[c].fx
            rad = 0.5 * scene.pitch_mm  # marker radius ~14 mm
            d = np.maximum(Xc[:, 2], 1e-6)
            mag = f * (rad / d) ** 2 * np.sin(tilt) / max(np.cos(tilt) ** 3, 0.05)
            cxy = np.array([scene.cameras[c].K[0, 2], scene.cameras[c].K[1, 2]])
            v = uv - cxy
            v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
            uv = uv + mag * v
        if outlier_frac:
            m = rng.random(len(uv)) < outlier_frac
            uv[m] += rng.normal(0, 8.0, (int(m.sum()), 2))
        if missing_frac:
            keep = rng.random(len(uv)) >= missing_frac
        else:
            keep = np.ones(len(uv), bool)
        d = {}
        for i, (u, v) in enumerate(uv):
            if not keep[i] or not (0 <= u < 1280 and 0 <= v < 1024):
                continue
            uu, vv = plate.lattice_uv()[i]
            d[(float(uu), float(vv))] = np.array([u, v], float)
        out.append(Observation(c, view, d))
    return out


# ---------------------------------------------------------------------------
# virtual operator
# ---------------------------------------------------------------------------

@dataclass
class VirtualOperator:
    scene: SyntheticScene
    noise_px: float = 0.2
    outlier_frac: float = 0.0
    missing_frac: float = 0.0
    move_noise_mm: float = 4.0
    move_noise_deg: float = 0.6
    eccentricity: bool = False
    render: bool = True
    blur_sigma: float = 0.0
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))
    pose: Optional[tuple] = None
    n_captures: int = 0

    def move_to(self, R: np.ndarray, t: np.ndarray) -> tuple:
        """Move the scanner, adding a realistic positioning error."""
        dR = cv2.Rodrigues(self.rng.normal(0, np.radians(self.move_noise_deg), 3))[0]
        dt = self.rng.normal(0, self.move_noise_mm, 3)
        # the pose is board -> camera; a translation error moves the board
        # opposite to the scanner, so perturb t in the board frame
        Rb = np.asarray(R)
        R2 = dR @ Rb
        t2 = Rb.T @ dt + np.asarray(t)
        self.pose = (R2, t2)
        return self.pose

    def capture(self, R=None, t=None, timestamp: float = 0.0):
        from .session import FrameSet
        if R is not None:
            self.move_to(R, t)
        R, t = self.pose
        grids = {o.camera: o for o in observations_from_pose(
            self.scene, R, t, view=0, noise_px=self.noise_px,
            outlier_frac=self.outlier_frac, missing_frac=self.missing_frac,
            rng=self.rng, eccentricity=self.eccentricity)}
        self.n_captures += 1
        images = {}
        if self.render:
            images = render_frame(self.scene, R, t, rng=self.rng,
                                  blur_sigma=self.blur_sigma)
        return FrameSet(images=images, timestamp=timestamp, label=f"cap{self.n_captures}"), grids


# ---------------------------------------------------------------------------
# closed loop
# ---------------------------------------------------------------------------

def run_closed_loop(scene: SyntheticScene, mode: str = "standard",
                    max_views: int = 30, seed: int = 0,
                    render: bool = True, noise_px: float = 0.2,
                    outlier_frac: float = 0.0, missing_frac: float = 0.0,
                    eccentricity: bool = False,
                    camera_start: int = 0,
                    solve_fn=None, verbose: bool = False) -> dict:
    """Drive the session as a live operator would, following its guidance."""
    from .session import CalibrationSession, FrameSet
    rng = np.random.default_rng(seed)
    op = VirtualOperator(scene, noise_px=noise_px, outlier_frac=outlier_frac,
                         missing_frac=missing_frac, eccentricity=eccentricity,
                         render=render, rng=rng)
    # start from a scene view (or a generic 400 mm fronto-parallel pose)
    if scene.views:
        R0, t0 = scene.views[camera_start % len(scene.views)]
    else:
        R0, t0 = np.diag([1.0, -1.0, -1.0]), np.array([0, 0, 400.0])
    op.pose = (np.asarray(R0), np.asarray(t0))
    sess = CalibrationSession(mode=mode, solve_fn=solve_fn, seed=seed)
    sess.policy["require_images"] = bool(render)
    # initial intrinsics: perturbed factory values (as a real device would have)
    from .synth import perturbed_init
    sess.cameras = perturbed_init(scene, rng)
    timeline = []
    ts = 0.0
    for step in range(max_views):
        # the operator holds still for a few frames; the session sees the
        # first post-move frame as "moving" and the later ones as a dwell
        dec = None
        t_pose = (op.pose[0].copy(), op.pose[1].copy())
        for k in range(3):
            frames, grids = op.capture(timestamp=ts + 0.1 * k)
            dec = sess.ingest(frames, grids=grids)
            if dec.accepted:
                break
        timeline.append({"step": step, "accepted": bool(dec and dec.accepted),
                         "reasons": dec.reasons if dec else [],
                         "gain": dec.predicted_gain if dec else None,
                         "n_accepted": len(sess.accepted),
                         "solve_s": sess.solve_seconds})
        if verbose:
            print(step, dec.accepted, dec.reasons[:2], None if dec.predicted_gain is None else round(dec.predicted_gain,3))
        # if the operator has been rejected several times in a row, try a
        # different part of the workspace (this also exercises failure recovery)
        recent = timeline[-4:]
        if len(recent) == 4 and not any(r["accepted"] for r in recent):
            R, t = scene.views[int(rng.integers(len(scene.views)))]
            op.pose = (np.asarray(R), np.asarray(t))
        # stop only after the quality gate passes and gain is small
        if len(sess.accepted) >= 6:
            st = sess.stop_decision()
            if st["stop"]:
                break
        rec = sess.recommend()
        if not rec.get("ready"):
            R, t = scene.views[int(rng.integers(len(scene.views)))]
            op.pose = (np.asarray(R), np.asarray(t))
        else:
            R, t = rec.get("target_pose", op.pose)
            op.pose = (np.asarray(R, float), np.asarray(t, float))
        ts += 0.7
    st = sess.stop_decision()
    return {"session": sess, "timeline": timeline, "stop": st,
            "n_captures": op.n_captures, "n_accepted": len(sess.accepted),
            "rejected": sess.rejected, "status": sess.status()}
