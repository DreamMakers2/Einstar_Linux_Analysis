"""Periodic disparity preview through the existing C++ recon CLI.

The engine already consumes this PGM layout, so the cheapest correct preview is
``recon_cli reconstruct`` on a temporary three-frame directory at low ``--scale``
with ``--no-color``: no Python stereo implementation, no engine changes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

import cv2
import numpy as np


@dataclass
class DepthPreview:
    gid: int | None = None
    ts: float = 0.0
    ok: bool = False
    error: str = ""
    image: np.ndarray | None = None
    valid: float = 0.0
    p50_mm: float = 0.0
    p95_mm: float = 0.0
    points: int = 0
    seconds: float = 0.0
    run_ms: float = 0.0
    scale: float = 0.5

    def age(self) -> float | None:
        return time.time() - self.ts if self.ts else None


class DepthWorker:
    """Latest-group-only worker; runs at most once every ``interval`` seconds."""

    def __init__(self, frames, recon_cli: str, profile: str, scale: float = 0.5,
                 interval: float = 3.0, timeout: float = 30.0):
        self.frames, self.recon_cli, self.profile = frames, recon_cli, profile
        self.scale, self.interval, self.timeout = scale, max(0.5, interval), timeout
        self._pending, self._result = None, DepthPreview(scale=scale)
        self._lock, self._wake, self._stop = threading.Lock(), threading.Event(), threading.Event()
        self._last_start = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="depth")
        self._thread.start()

    def request(self, gid: int | None) -> None:
        if gid is not None:
            with self._lock:
                self._pending = gid
            self._wake.set()

    def latest(self) -> DepthPreview:
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
            if gid is None:
                continue
            delay = self.interval - (time.monotonic() - self._last_start)
            if delay > 0 and self._stop.wait(delay):
                return
            with self._lock:
                if self._pending is not None:
                    gid, self._pending = self._pending, None
            self._last_start = time.monotonic()
            try:
                result = self._reconstruct(gid)
            except Exception as exc:                    # keep the worker alive
                result = DepthPreview(gid, time.time(), False,
                                      f"{type(exc).__name__}: {exc}"[:220],
                                      scale=self.scale)
            with self._lock:
                self._result = result

    def _reconstruct(self, gid: int) -> DepthPreview:
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="einstar-liveview-") as work:
                frames_dir, out_dir = os.path.join(work, "frames"), os.path.join(work, "out")
                os.makedirs(frames_dir)
                os.makedirs(out_dir)
                for cam in (0, 1, 2):
                    target = os.path.join(frames_dir, f"frame_{gid:06d}_cam{cam}.pgm")
                    try:
                        os.link(self.frames.path(gid, cam), target)
                    except OSError:
                        shutil.copy2(self.frames.path(gid, cam), target)
                process = subprocess.run(
                    [self.recon_cli, "reconstruct", "--frames", frames_dir, "--id", str(gid),
                     "--profile", self.profile, "--out", out_dir,
                     "--scale", f"{self.scale:g}", "--no-color"],
                    capture_output=True, text=True, timeout=self.timeout)
                image_path = os.path.join(out_dir, "recon_disparity.png")
                image = cv2.imread(image_path, cv2.IMREAD_COLOR) \
                    if process.returncode == 0 and os.path.isfile(image_path) else None
                if image is None:
                    detail = (process.stderr or process.stdout or "").strip()
                    detail = detail.splitlines()[-1] if detail else "no output"
                    return DepthPreview(gid, time.time(), False,
                                        f"recon_cli: {detail}"[:220], scale=self.scale)
                try:
                    with open(os.path.join(out_dir, "recon_report.json"), "r",
                              encoding="utf-8") as handle:
                        report = json.load(handle)
                except (OSError, ValueError):
                    report = {}
                stereo, depth, timings = (report.get(k, {}) for k in
                                          ("stereo", "depth", "timings"))
                return DepthPreview(
                    gid, time.time(), True, "", image,
                    100.0 * float(stereo.get("valid_fraction", 0)),
                    float(depth.get("p50_mm", 0)), float(depth.get("p95_mm", 0)),
                    int(report.get("cloud", {}).get("points", 0)),
                    float(timings.get("total", 0)), 1000.0 * (time.monotonic() - started),
                    self.scale)
        except FileNotFoundError as exc:
            return DepthPreview(gid, time.time(), False, f"missing file: {exc}"[:220],
                                scale=self.scale)
        except subprocess.TimeoutExpired:
            return DepthPreview(gid, time.time(), False,
                                f"recon_cli timeout after {self.timeout:g}s", scale=self.scale)
        except OSError as exc:
            return DepthPreview(gid, time.time(), False,
                                f"{type(exc).__name__}: {exc}"[:220], scale=self.scale)


def find_recon_cli(explicit: str | None = None) -> str | None:
    def usable(path):
        return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)

    if explicit:
        return os.path.abspath(explicit) if usable(explicit) else None
    candidates = [os.environ.get("EINSTAR_RECON_CLI"),
                  os.path.join(os.path.dirname(__file__), "..", "..",
                               "build", "recon", "recon_cli")]
    return next((os.path.abspath(c) for c in candidates if usable(c)), None)
