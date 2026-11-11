"""Live status JSON, capture manifest and disk helpers."""
from __future__ import annotations

import json
import os
import shutil
import time

SCHEMA = "einstar-live-status-v1"
STALE_S = 2.0


class StatusFile:
    """Last good live status plus its age; never fails the viewer."""

    def __init__(self, path: str | None):
        self.path = os.path.abspath(path) if path else None
        self.data, self.error, self.mtime = None, None if path else "no --status-file", 0.0

    def read(self) -> None:
        if not self.path:
            return
        try:
            self.mtime = os.stat(self.path).st_mtime
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("status JSON is not an object")
            self.data, self.error = data, None
        except (OSError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def age(self) -> float | None:
        return time.time() - self.mtime if self.mtime else None

    @property
    def fresh(self) -> bool:
        age = self.age()
        return self.data is not None and age is not None and age <= STALE_S

    def state(self) -> str:
        if not self.path:
            return "NO STATUS"
        if self.data is None:
            return "STALE" if self.mtime else "NO FILE"
        return "LIVE" if self.fresh else "STALE"

    def get(self, key, default=None):
        return self.data.get(key, default) if self.data else default


def read_manifest(directory: str, cache: dict | None = None) -> dict | None:
    path = os.path.join(directory, "frames_manifest.json")
    try:
        stat = os.stat(path)
        if cache is not None and cache.get("mtime") == stat.st_mtime \
                and cache.get("size") == stat.st_size:
            return cache.get("data")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return None
        if cache is not None:
            cache.update(mtime=stat.st_mtime, size=stat.st_size, data=data)
        return data
    except (OSError, ValueError):
        return None


def disk_usage(path: str) -> tuple[int, int] | None:
    try:
        usage = shutil.disk_usage(path)
        return usage.free, usage.total
    except OSError:
        return None


def fmt_bytes(value) -> str:
    if value is None:
        return "--"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000.0 or unit == "TB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000.0
    return "--"


def fmt_age(age: float | None) -> str:
    if age is None:
        return "--"
    return f"{age * 1000:.0f} ms" if age < 1.0 else f"{age:.1f} s"
