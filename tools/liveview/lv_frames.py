"""Frame-directory scanning, cached PGM loading and display previews."""
from __future__ import annotations

from collections import OrderedDict
import os
import re
import threading
import time

import cv2
import numpy as np

FRAME_RE = re.compile(r"^frame_(\d{6})_cam([0-2])\.pgm$")
FRAME_SHAPE = (1024, 1280)
FRAME_BYTES = FRAME_SHAPE[0] * FRAME_SHAPE[1]
CAM_NAMES = ("right IR", "left IR", "colour")


class Scan:
    def __init__(self, directory: str):
        self.directory, self.gids, self.seen = directory, {}, {0: None, 1: None, 2: None}
        self.newest = self.newest_left = None
        self.complete = self.incomplete = self.frames = 0
        self.scanned_at, self.error = 0.0, None

    def cams(self, gid) -> frozenset[int]:
        return self.gids.get(gid, frozenset()) if gid is not None else frozenset()

    def age(self, now: float) -> float:
        return now - self.scanned_at


def scan(directory: str) -> Scan:
    """One os.scandir pass; frame pixels are never read here."""
    result, result.scanned_at = Scan(directory), time.monotonic()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                match = FRAME_RE.match(entry.name)
                if match is None or not entry.is_file():
                    continue
                gid, cam = int(match.group(1)), int(match.group(2))
                result.gids.setdefault(gid, set()).add(cam)
                result.frames += 1
                if result.seen[cam] is None or gid > result.seen[cam]:
                    result.seen[cam] = gid
    except OSError as exc:
        result.error = str(exc)
        return result
    if result.gids:
        result.newest = max(result.gids)
        left = [gid for gid, cams in result.gids.items() if 1 in cams]
        result.newest_left = max(left) if left else None
        result.complete = sum(len(cams) == 3 for cams in result.gids.values())
        result.incomplete = sum(len(cams) != 3 for cams in result.gids.values())
    result.gids = {gid: frozenset(cams) for gid, cams in result.gids.items()}
    return result


def fit(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Letterbox an image into ``size`` (frames are 5:4 like the tiles)."""
    width, height = size
    scale = min(width / image.shape[1], height / image.shape[0])
    new_w, new_h = max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    if (new_w, new_h) == (width, height):
        return resized
    canvas = np.full((height, width, 3), 8, np.uint8)
    x, y = (width - new_w) // 2, (height - new_h) // 2
    canvas[y:y + new_h, x:x + new_w] = resized
    return canvas


def map_points(points: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    scale = min(width / FRAME_SHAPE[1], height / FRAME_SHAPE[0])
    return np.asarray(points, float) * scale + np.array(
        [(width - FRAME_SHAPE[1] * scale) / 2, (height - FRAME_SHAPE[0] * scale) / 2])


class FrameDir:
    """Directory listing refreshed at ~2 Hz plus a small decoded-frame cache."""

    def __init__(self, directory: str, refresh_s: float = 0.5, cache: int = 10):
        self.directory, self.refresh_s, self.cache_size = os.path.abspath(directory), refresh_s, cache
        self.scan, self._last_scan = Scan(self.directory), 0.0
        self._lock, self._gray, self._preview, self._errors = (
            threading.Lock(), OrderedDict(), OrderedDict(), {})

    def refresh(self, force: bool = False) -> Scan:
        now = time.monotonic()
        if force or now - self._last_scan >= self.refresh_s:
            self.scan, self._last_scan = scan(self.directory), now
        return self.scan

    def path(self, gid: int, cam: int) -> str:
        return os.path.join(self.directory, f"frame_{gid:06d}_cam{cam}.pgm")

    def has(self, gid, cam: int) -> bool:
        return cam in self.scan.cams(gid)

    def error(self, gid: int, cam: int) -> str | None:
        with self._lock:
            return self._errors.get((gid, cam))

    def _remember(self, cache: OrderedDict, key, value):
        with self._lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > self.cache_size:
                cache.popitem(last=False)
        return value

    def gray(self, gid: int, cam: int) -> np.ndarray | None:
        """Load and validate a PGM; ``None`` on a partially written file."""
        key = (gid, cam)
        with self._lock:
            if key in self._gray:
                self._gray.move_to_end(key)
                return self._gray[key]
        image, error = None, None
        try:
            size = os.stat(self.path(gid, cam)).st_size
            if size < FRAME_BYTES:
                raise OSError(f"incomplete PGM ({size} of {FRAME_BYTES + 17} bytes)")
            image = cv2.imread(self.path(gid, cam), cv2.IMREAD_UNCHANGED)
            if image is None:
                raise OSError("cv2.imread returned None")
            if image.dtype != np.uint8 or image.shape != FRAME_SHAPE:
                raise OSError(f"unexpected frame {image.shape} {image.dtype}")
        except OSError as exc:
            error = str(exc)
        with self._lock:
            if error is not None:
                self._errors[key] = error
                return None
            self._errors.pop(key, None)
        return self._remember(self._gray, key, image)

    def preview(self, gid: int, cam: int, size: tuple[int, int] = (400, 320)):
        """Display-oriented BGR tile (cam0/cam2 are rotated 180 degrees)."""
        key = (gid, cam, size)
        with self._lock:
            if key in self._preview:
                self._preview.move_to_end(key)
                return self._preview[key]
        raw = self.gray(gid, cam)
        if raw is None:
            return None
        if cam == 2:
            image = cv2.rotate(cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR), cv2.ROTATE_180)
        elif cam == 0:
            image = cv2.rotate(raw, cv2.ROTATE_180)
        else:
            image = raw
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        tile = fit(image, size)
        return self._remember(self._preview, key,
                              _enhance_colour(tile) if cam == 2 else tile)


def _enhance_colour(tile: np.ndarray) -> np.ndarray:
    """Grey-world + gamma, display only (the sensor frame is very dark)."""
    means = np.maximum(tile.astype(np.float32).reshape(-1, 3).mean(axis=0), 1.0)
    scaled = np.clip(tile.astype(np.float32) * (90.0 / means), 0, 255) / 255.0
    return (np.power(scaled, 0.65) * 255.0).astype(np.uint8)
