"""Shared data types and contracts for the calibration wizard.

Everything in this module is deliberately dependency-light (numpy + stdlib) so
that the live source, the replay source and the synthetic source can all produce
the *same* objects and therefore drive the *same* decision code.

See ``docs/calibration/wizard_architecture.md`` for the contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import numpy as np

#: Calibration-engine camera order (``calib.calibrate.CAMERAS``).
CAMERA_KEYS: tuple[str, str, str] = ("left", "right", "tex")

#: File/stream index written by ``einstar-capture-frames`` per camera key.
FILE_CAM: Mapping[str, int] = {"left": 1, "right": 0, "tex": 2}

#: Human labels used by the GUI.
CAMERA_LABELS: Mapping[str, str] = {
    "left": "Left IR (cam1)",
    "right": "Right IR (cam0, rotated 180°)",
    "tex": "Colour (cam2, BayerRG, rotated 180°)",
}

IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 1024

#: Source identifiers.
SOURCE_LIVE = "live"
SOURCE_REPLAY = "replay"
SOURCE_SYNTHETIC = "synthetic"

#: Capture-process states written by the C++ tool (``status.json``).
CAPTURE_STATES = ("starting", "running", "paused", "stopping", "stopped")


@dataclass(frozen=True)
class FrameGroup:
    """One synchronised three-camera trigger group, already oriented.

    ``images`` maps every available camera key to a calibration-oriented 8-bit
    grey array of shape ``(IMAGE_HEIGHT, IMAGE_WIDTH)``.  A camera that is
    missing from a (possibly incomplete) group is simply absent from the dict.
    """

    index: int
    device_group: int | None
    timestamp_s: float
    images: Mapping[str, np.ndarray]
    slot_ids: Mapping[str, int] = field(default_factory=dict)
    complete: bool = True
    source: str = SOURCE_REPLAY
    label: str = ""
    #: False for still collections (vendor corpus, synthetic scenes): a large
    #: pose jump between groups is expected, so no motion gate applies.
    continuous: bool = True
    #: Colour BGR preview for the GUI, present only when the source provides it.
    colour_bgr: np.ndarray | None = None
    #: unconverted Bayer frame; when set, ``colour_bgr``/``images["tex"]`` are
    #: produced on demand by the consumer instead of in the source.  Debayering
    #: costs 11 ms per group and is pure waste for a group the scheduler skips.
    raw_colour: np.ndarray | None = None

    def camera(self, key: str) -> np.ndarray | None:
        return self.images.get(key)

    @property
    def cameras_present(self) -> tuple[str, ...]:
        return tuple(k for k in CAMERA_KEYS if k in self.images)

    def digest(self) -> str:
        """Content digest of the oriented images (determinism checks)."""
        h = hashlib.sha256()
        for key in CAMERA_KEYS:
            img = self.images.get(key)
            if img is None:
                h.update(b"<missing>")
                continue
            h.update(key.encode())
            h.update(np.ascontiguousarray(img).tobytes())
        return h.hexdigest()


@dataclass(frozen=True)
class SourceInfo:
    """Static description of where frames come from (for the GUI header)."""

    kind: str                       # live | replay | synthetic
    path: str = ""
    serial: str = ""                # redacted or empty for replay
    firmware: str = ""
    width: int = IMAGE_WIDTH
    height: int = IMAGE_HEIGHT
    note: str = ""
    #: True when consecutive groups are a continuous video stream (live capture
    #: or a replay of one).  False for still collections (vendor corpus,
    #: synthetic scenes): a large pose jump between groups is then expected and
    #: must not be treated as motion blur / operator movement.
    continuous: bool = True


# ---------------------------------------------------------------------------
# orientation helpers (used by every source; must stay identical)
# ---------------------------------------------------------------------------

def orient_right(gray: np.ndarray) -> np.ndarray:
    """cam0 = right IR arrives rotated 180° relative to calibration."""
    return np.ascontiguousarray(np.rot90(gray, 2))


def orient_left(gray: np.ndarray) -> np.ndarray:
    """cam1 = left IR arrives in calibration orientation."""
    return np.ascontiguousarray(gray)


def debayer_bgr(raw: np.ndarray) -> np.ndarray:
    """cam2 raw BayerRG → BGR, rotated 180° (`BayerRG` is the vendor CFA)."""
    import cv2

    if raw.ndim != 2:
        return np.ascontiguousarray(raw)
    bgr = cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR)
    return np.ascontiguousarray(np.rot90(bgr, 2))


def grey_from_bgr(bgr: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray((0.299 * bgr[..., 2] + 0.587 * bgr[..., 1]
                                 + 0.114 * bgr[..., 0]).astype(np.uint8))


# ---------------------------------------------------------------------------
# small IO helpers shared by the session and the capture process
# ---------------------------------------------------------------------------

def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Write JSON atomically: ``.tmp`` + ``rename`` (same directory)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=_json_default) + "\n")
    os.replace(tmp, path)


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def append_jsonl(path: str | Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=_json_default) + "\n")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_s() -> float:
    return time.time()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


# ---------------------------------------------------------------------------
# session directory layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionPaths:
    root: Path

    @property
    def session_json(self) -> Path: return self.root / "session.json"

    @property
    def manifest_json(self) -> Path: return self.root / "manifest.json"

    @property
    def decisions_jsonl(self) -> Path: return self.root / "decisions.jsonl"

    @property
    def groups_dir(self) -> Path: return self.root / "groups"

    @property
    def raw_recording(self) -> Path: return self.root / "raw.e1r"

    @property
    def status_json(self) -> Path: return self.root / "status.json"

    @property
    def candidate_dir(self) -> Path: return self.root / "candidate"

    @property
    def report_md(self) -> Path: return self.root / "report.md"

    def group_dir(self, index: int) -> Path:
        return self.groups_dir / f"{index:06d}"

    def group_file(self, index: int, camera_key: str) -> Path:
        return self.group_dir(index) / f"cam{FILE_CAM[camera_key]}.pgm"


def write_pgm(path: str | Path, image: np.ndarray) -> None:
    """Binary P5 8-bit PGM, atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = np.ascontiguousarray(image, dtype=np.uint8)
    header = f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode("ascii")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(header)
        fh.write(img.tobytes())
    os.replace(tmp, path)


def read_pgm(path: str | Path) -> np.ndarray:
    data = Path(path).read_bytes()
    if not data.startswith(b"P5"):
        raise ValueError(f"{path}: not a binary PGM")
    fields: list[bytes] = []
    idx = 2
    while len(fields) < 3:
        while idx < len(data) and data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b"#":
            while idx < len(data) and data[idx:idx + 1] != b"\n":
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx:idx + 1].isspace():
            idx += 1
        fields.append(data[start:idx])
    idx += 1  # exactly one whitespace byte after maxval
    width, height = int(fields[0]), int(fields[1])
    return np.frombuffer(data[idx:idx + width * height], dtype=np.uint8).reshape(
        height, width)
