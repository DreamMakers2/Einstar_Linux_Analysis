"""Marker detection, plate pose, image quality and per-camera workers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time
from typing import NamedTuple

import cv2
import numpy as np

from calib import plate
from calib.detect import detect_grid_auto
from calib.geometry import project_world, solve_pnp

MIN_MARKERS = 30
_UV_INDEX = {(round(float(u), 3), round(float(v), 3)): i
             for i, (u, v) in enumerate(plate.lattice_uv())}


class CameraModel(NamedTuple):
    name: str
    K: np.ndarray
    dist: np.ndarray


class Profile(NamedTuple):
    path: str
    models: dict[str, CameraModel]
    obj: np.ndarray


class Quality(NamedTuple):
    mean: float
    sat_pct: float
    blur: float


class Markers(NamedTuple):
    n: int
    uv: np.ndarray
    points: np.ndarray
    rms: float
    max_residual: float
    ms: float


class PoseEstimate(NamedTuple):
    x: float
    y: float
    z: float
    tilt_deg: float
    inplane_deg: float
    rms_px: float
    n: int


@dataclass
class CamResult:
    gid: int
    cam: int
    ts: float
    markers: Markers | None = None
    quality: Quality | None = None
    pose: PoseEstimate | None = None
    error: str | None = None


def load_profile(path: str) -> Profile:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    models = {}
    for name in ("ir_left", "ir_right", "color"):
        cam = data.get("cameras", {}).get(name)
        if not isinstance(cam, dict):
            continue
        fx, fy = float(cam.get("fx", 0)), float(cam.get("fy", 0))
        if fx <= 0 or fy <= 0:
            continue
        # NOTE: the profile's skew is already an absolute K[0,1] pixel value.
        K = np.array([[fx, float(cam.get("skew", 0)), float(cam.get("cx", 0))],
                      [0, fy, float(cam.get("cy", 0))], [0, 0, 1]], float)
        models[name] = CameraModel(name, K, np.array(cam.get("dist", [0.0] * 5), float).ravel())
    if "ir_left" not in models:
        raise ValueError(f"{path}: no usable cameras.ir_left intrinsics")
    pitch = data.get("plate_pitch_mm")
    return Profile(path, models,
                   plate.board_points_mm(pitch=float(pitch)) if pitch else plate.board_points_mm())


def measure_quality(gray: np.ndarray) -> Quality:
    return Quality(float(gray.mean()),
                   100.0 * float(np.count_nonzero(gray >= 250)) / float(gray.size),
                   float(cv2.Laplacian(gray, cv2.CV_64F).var()))


def detect_markers(gray: np.ndarray) -> tuple[Markers | None, str | None]:
    started = time.perf_counter()
    try:
        observation = detect_grid_auto(gray)
    except Exception as exc:
        return None, f"detector: {type(exc).__name__}: {exc}"
    ms = 1000.0 * (time.perf_counter() - started)
    if observation is None:
        return Markers(0, np.empty((0, 2)), np.empty((0, 2)), 0.0, 0.0, ms), None
    return Markers(int(observation.n_lattice), np.asarray(observation.uv, float),
                   np.asarray(observation.pixels, float), float(observation.rms),
                   float(observation.max_residual), ms), None


def estimate_pose(markers: Markers, profile: Profile) -> PoseEstimate | None:
    model = profile.models.get("ir_left")
    if model is None or markers.n < 6:
        return None
    obj, image = [], []
    for j, (u, v) in enumerate(markers.uv):
        row = _UV_INDEX.get((round(float(u), 3), round(float(v), 3)))
        if row is not None:
            obj.append(profile.obj[row])
            image.append(markers.points[j])
    if len(obj) < 6:
        return None
    obj, image = np.asarray(obj, float), np.asarray(image, float)
    pose = solve_pnp(obj, image, model.K, model.dist)
    if pose is None:
        return None
    predicted = project_world(model.K, model.dist, pose.R, pose.t, obj)
    rms = float(np.sqrt(np.mean(np.sum((predicted - image) ** 2, axis=1))))
    normal = pose.R @ np.array([0.0, 0.0, 1.0])
    tilt = float(np.degrees(np.arccos(np.clip(abs(normal[2]), 0.0, 1.0))))
    inplane = (float(np.degrees(np.arctan2(pose.R[1, 0], pose.R[0, 0]))) + 90.0) % 180.0 - 90.0
    return PoseEstimate(float(pose.t[0]), float(pose.t[1]), float(pose.t[2]),
                        tilt, inplane, rms, len(obj))


class AnalysisWorker:
    """Latest-group-wins detection thread for one camera."""

    def __init__(self, frames, cam: int, profile: Profile | None):
        self.frames, self.cam, self.profile = frames, cam, profile
        self._pending = self._result = None
        self._lock, self._wake, self._stop = threading.Lock(), threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"analysis-cam{cam}")
        self._thread.start()

    def request(self, gid: int | None) -> None:
        if gid is None:
            return
        with self._lock:
            if self._result is not None and self._result.gid == gid \
                    and self._result.error is None:
                return
            if self._pending == gid:
                return
            self._pending = gid
        self._wake.set()

    def latest(self) -> CamResult | None:
        with self._lock:
            return self._result

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            with self._lock:
                gid, self._pending = self._pending, None
            if gid is not None:
                try:
                    result = self._analyse(gid)
                except Exception as exc:               # keep the worker alive
                    result = CamResult(gid, self.cam, time.time(),
                                       error=f"{type(exc).__name__}: {exc}")
                with self._lock:
                    self._result = result

    def _analyse(self, gid: int) -> CamResult:
        gray = self.frames.gray(gid, self.cam)
        if gray is None:
            return CamResult(gid, self.cam, time.time(),
                             error=self.frames.error(gid, self.cam) or "frame unreadable")
        if self.cam == 0:
            gray = cv2.rotate(gray, cv2.ROTATE_180)
        quality = measure_quality(gray) if self.cam == 1 else None
        markers, error = detect_markers(gray)
        pose = estimate_pose(markers, self.profile) \
            if error is None and markers and self.cam == 1 and self.profile else None
        return CamResult(gid, self.cam, time.time(), markers, quality, pose, error)
