#!/usr/bin/env python3
"""Guided one-session calibration recorder for the Einstar 41-marker plate.

This is the **offline-tested acquisition front end** for a single guided
calibration session.  It does not talk to the scanner and it never writes to
the device: it reads frame sets (from the probe's PGM triplets, the private
vendor 25-pose corpus, or a replay manifest), asks :class:`calib.session.
CalibrationSession` whether each set is informative, stationary and diverse,
retains *every* candidate frame set with SHA-256 provenance, reserves a
held-out split, and finally solves a host-side, versioned profile.

Why this tool exists
--------------------
The previous capture lost the colour camera (``hasTexture:false``) and then
mixed a stale ``TexCCF`` into the geometry fit.  This recorder therefore:

* verifies that **all three cameras** produced a decodable image for every
  accepted set (and records colour participation explicitly, including a
  ``hasTexture``-like state);
* records synchronisation evidence (measured skew when timestamps exist,
  otherwise the capture-index grouping, clearly labelled as an assumption);
* keeps the colour frames even when the colour calibration is rejected;
* gates each candidate against the *stereo-consistent* board pose, which
  catches mirrored/wrong grid assignments that a single-camera PnP cannot see
  (the vendor corpus contains one such view, set 9);
* solves geometry from left/right even if the colour profile is stale or
  missing, and reports that state instead of silently fitting to it;
* never activates the new profile and never touches the factory snapshot.

Inputs
------
``--frames DIR``
    ``frame_%06d_cam{0,1,2}.pgm`` (or png/bmp/ppm) triplets, as written by the
    capture probe.  The probe mapping is **not** the intuitive order:
    ``cam0`` = right IR (arrives rotated 180 deg), ``cam1`` = left IR (as
    calibrated), ``cam2`` = colour (rotated 180 deg).  The loader maps the
    streams and rotates ``right``/``tex`` back in memory; the retained copy is
    the original byte stream.  See ``recon/src/frame_set.cpp`` and
    ``docs/reconstruction/audit.md`` for the evidence.
``--vendor-corpus DIR``
    ``imageLeft<N>.bmp`` / ``imageRight<N>.bmp`` / ``imageTex<N>.bmp``.
``--replay MANIFEST``
    JSON list (or ``{"sets": [...]}``) of explicit frame sets; see README.

Outputs
-------
``<session>/manifest.json``
    Complete candidate log (accepted and rejected) with per-file SHA-256,
    decisions, reasons, metrics, poses, train/heldout labels, coverage,
    stopping report, completeness report and profile provenance.
``<session>/sets/<seq>_<label>/...``
    Byte-exact copies of every candidate frame plus a per-set ``meta.json``.
``<session>/accepted.json`` / ``heldout.json`` / ``candidates.jsonl`` /
``coverage.json`` / ``stopping.json`` / ``completeness.json``.
``<session>/profiles/``
    Versioned :class:`calib.profiles.ProfileStore` revision (factory snapshot
    immutable, new revision deliberately **not** activated), the raw solve
    result and the reconstruction-profile export.

The module is importable (tests use the functions directly) and is the CLI::

    python tools/calibsession/calibrate_session.py \
        --vendor-corpus <private-inputs>/vendor-calib-rapid \
        --out /tmp/session_vendor \
        --factory-dir sources/calibration/res_Einscan-E10_200x150

    python tools/calibsession/calibrate_session.py --verify /tmp/session_vendor

Orientation and stream order
----------------------------
The live hardware and ``recon/src/frame_set.cpp`` establish::

    stream 0 (cam0) = IR right,  rotated 180 deg relative to calibration
    stream 1 (cam1) = IR left,   already in calibration orientation
    stream 2 (cam2) = colour,    rotated 180 deg

``--vendor-corpus`` files (``imageLeftN``/``imageRightN``/``imageTexN``) are
already in calibration orientation and are not rotated.  ``--replay`` sets may
declare ``"rotate_180": ["right", "tex"]``; omitting it means the referenced
files are already oriented.

This file is host-side only.  Nothing here opens a USB device, writes to the
scanner, or modifies its input images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:              # allow `python tools/.../x.py`
    sys.path.insert(0, str(REPO_ROOT))

import cv2
import numpy as np

from calib import guidance, plate
from calib.calibrate import (CAMERAS, CalibrationResult, CameraIntrinsics,
                            Observation, calibrate)
from calib.ccf import CCFRecord, consistency as ccf_consistency, load as load_ccf
from calib.detect import detect_grid_auto
from calib.evaluate import heldout_reprojection, per_view_errors
from calib.geometry import project, solve_pnp
from calib.integration import result_summary
from calib.localize import refine_grid
from calib.pipeline import profile_from_result, solve_observations
from calib.profiles import ProfileStore, sha256_file
from calib.quality import stopping_report
from calib.session import CalibrationSession, FrameSet

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

SCHEMA = "einstar.calibsession/1"
TOOL_NAME = "tools/calibsession/calibrate_session.py"

#: Probe stream -> calibration camera.  This is the hardware mapping proven by
#: the live capture and encoded in ``recon/src/frame_set.cpp`` /
#: ``libeinstar calibration.hpp``: stream 0 = **right** IR, stream 1 = **left**
#: IR, stream 2 = colour.  It is NOT the intuitive left/right/colour order.
DEFAULT_CAM_MAP = {0: "right", 1: "left", 2: "tex"}

#: Probe streams 0 and 2 arrive rotated 180 deg relative to the calibration
#: orientation; stream 1 (left IR) arrives as calibrated.
PROBE_ROTATED_CAMERAS = ("right", "tex")

CAMERA_LABELS = {"left": "IR left", "right": "IR right", "tex": "colour/texture"}

#: file endings accepted for frame sets
IMAGE_SUFFIXES = (".pgm", ".ppm", ".png", ".bmp", ".tif", ".tiff")

#: coverage thresholds used by the completeness gate.  ``--strict`` tightens
#: them; the defaults are deliberately achievable in one operator session while
#: still rejecting a clustered/fronto-parallel capture.
DEFAULT_RULES: dict[str, Any] = {
    "min_accepted": 15,
    "min_train": 10,
    "min_heldout": 5,
    "distance_edges_mm": [200.0, 280.0, 360.0, 440.0, 520.0, 620.0],
    "distance_bins_min": 3,
    "tilt_both_signs": True,
    "tilt_sign_deg": 5.0,
    "tilt_max_deg_min": 12.0,
    "quadrant_fraction_min": 0.75,
    "roll_bins_min": 0,             # advisory by default; --strict sets 2
    "roll_bin_deg": 30.0,
    "tex_participation_min": 0.8,
    "tex_candidate_fraction_min": 0.9,
    "require_all_cameras": True,
    "require_sync_evidence": True,
    "require_dwell": True,
    "require_profile": True,
    "require_export": False,        # reported, but export failures are warnings
    "min_stereo_markers": 16,
    "min_big_markers": 3,
    "max_stereo_pose_delta_deg": 2.0,
    "max_stereo_pose_delta_mm": 6.0,
    "max_view_pnp_rms_px": 3.0,
    "require_factory_orientation": True,
    "max_factory_rotation_delta_deg": 10.0,
}

STRICT_RULES: dict[str, Any] = {
    "distance_bins_min": 4,
    "roll_bins_min": 2,
    "quadrant_fraction_min": 1.0,
    "tilt_max_deg_min": 20.0,
}


class CalibSessionError(RuntimeError):
    """A user-facing input/configuration error (exit code 1)."""


class ImageError(RuntimeError):
    """One image could not be read/validated; recorded per candidate."""


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file_chunked(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _jsonable(obj: Any) -> Any:
    """Recursively convert numpy / Path / non-finite numbers for strict JSON."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, (np.integer, int)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, Path):
        return str(obj)
    if obj is None or isinstance(obj, str):
        return obj
    return str(obj)


def write_json(path: str | Path, obj: Any) -> None:
    """Atomic, strict, deterministic JSON write."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(_jsonable(obj), indent=2, sort_keys=True,
                              allow_nan=False) + "\n")
    os.replace(tmp, p)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _angle_deg(R: np.ndarray) -> float:
    c = (float(np.trace(np.asarray(R, float))) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def _pose_dict(pose: Optional[tuple]) -> Optional[dict]:
    if pose is None:
        return None
    R, t = np.asarray(pose[0], float), np.asarray(pose[1], float)
    return {"R": R.tolist(), "t": t.tolist(),
            "distance_mm": float(np.linalg.norm(t)),
            "tilt_deg": tilt_magnitude_deg(R),
            "tilt_x_deg": tilt_x_deg(R),
            "tilt_y_deg": tilt_y_deg(R),
            "roll_deg": roll_deg(R)}


def tilt_magnitude_deg(R: np.ndarray) -> float:
    n = np.asarray(R, float)[:, 2]
    return float(np.degrees(np.arccos(np.clip(abs(n[2]), -1.0, 1.0))))


def tilt_x_deg(R: np.ndarray) -> float:
    n = np.asarray(R, float)[:, 2]
    return float(np.degrees(np.arctan2(n[1], max(abs(n[2]), 1e-9))))


def tilt_y_deg(R: np.ndarray) -> float:
    n = np.asarray(R, float)[:, 2]
    return float(np.degrees(np.arctan2(n[0], max(abs(n[2]), 1e-9))))


def roll_deg(R: np.ndarray) -> float:
    """In-plane board rotation (ZYX roll) in degrees, wrapped to [-90, 90)."""
    R = np.asarray(R, float)
    a = float(np.degrees(np.arctan2(R[1, 0], R[0, 0])))
    while a >= 90.0:
        a -= 180.0
    while a < -90.0:
        a += 180.0
    return a


# ---------------------------------------------------------------------------
# plate profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlateProfile:
    cols: int = int(plate.GRID_COLS)
    rows: int = int(plate.GRID_ROWS)
    pitch_mm: float = float(plate.DEFAULT_PITCH_MM)
    pitch_source: str = str(plate.DEFAULT_PITCH_SOURCE)

    @classmethod
    def parse(cls, text: Optional[str]) -> "PlateProfile":
        if not text:
            return cls()
        out = {"cols": cls.cols, "rows": cls.rows,
               "pitch_mm": cls.pitch_mm, "pitch_source": cls.pitch_source}
        for tok in re.split(r"[,\s]+", text.strip()):
            if not tok:
                continue
            if "=" not in tok:
                raise CalibSessionError(
                    f"--plate-profile: expected key=value, got {tok!r}")
            key, val = tok.split("=", 1)
            key = key.strip().lower()
            if key in ("cols", "columns", "n_cols"):
                out["cols"] = int(val)
            elif key in ("rows", "n_rows"):
                out["rows"] = int(val)
            elif key in ("pitch", "pitch_mm"):
                out["pitch_mm"] = float(val)
            elif key in ("source", "pitch_source"):
                out["pitch_source"] = val
            else:
                raise CalibSessionError(f"--plate-profile: unknown key {key!r}")
        if out["cols"] * out["rows"] != 40:
            # the recovered big-marker code is defined for the 8x5 board; other
            # topologies would need a different PlateSpec, so say so explicitly
            raise CalibSessionError(
                "this recorder implements the recovered 41-marker Einstar "
                f"plate (8x5); got {out['cols']}x{out['rows']}")
        if not (5.0 <= out["pitch_mm"] <= 200.0):
            raise CalibSessionError(
                f"implausible marker pitch {out['pitch_mm']} mm")
        return cls(**out)

    def apply(self):
        spec = plate.PlateSpec(cols=self.cols, rows=self.rows,
                               pitch_mm=self.pitch_mm,
                               pitch_source=self.pitch_source)
        plate.use_spec(spec)
        return spec

    def as_dict(self) -> dict:
        return {"cols": self.cols, "rows": self.rows,
                "pitch_mm": self.pitch_mm, "pitch_source": self.pitch_source,
                "n_markers": 41,
                "big_markers_uv": plate.big_points_uv().tolist(),
                "expected_markers": int(self.cols * self.rows) + 1}


# ---------------------------------------------------------------------------
# image reading / validation
# ---------------------------------------------------------------------------

_PGM_WS = b" \t\r\n\v\f"


def _pgm_header(raw: bytes):
    """Parse a binary PGM (P5) / PPM (P6) header; return (kind,w,h,maxval,off)."""
    if raw[:2] not in (b"P5", b"P6"):
        raise ImageError("not a binary PGM/PPM (expected P5/P6)")
    kind = raw[:2].decode()
    pos = 2
    fields: list[bytes] = []
    while len(fields) < 3:
        while pos < len(raw) and raw[pos:pos + 1] in (b" ", b"\t", b"\r",
                                                       b"\n", b"\v", b"\f"):
            pos += 1
        if pos >= len(raw):
            raise ImageError("truncated PGM header")
        if raw[pos:pos + 1] == b"#":
            nl = raw.find(b"\n", pos)
            if nl < 0:
                raise ImageError("truncated PGM comment")
            pos = nl + 1
            continue
        start = pos
        while pos < len(raw) and raw[pos:pos + 1] not in (b" ", b"\t", b"\r",
                                                           b"\n", b"\v", b"\f"):
            pos += 1
        fields.append(raw[start:pos])
    if pos >= len(raw) or raw[pos:pos + 1] not in (b" ", b"\t", b"\r",
                                                    b"\n", b"\v", b"\f"):
        raise ImageError("PGM header missing whitespace after maxval")
    pos += 1
    try:
        w, h, maxval = (int(fields[0]), int(fields[1]), int(fields[2]))
    except ValueError as exc:
        raise ImageError(f"invalid PGM header: {exc}") from exc
    if w <= 0 or h <= 0 or maxval <= 0:
        raise ImageError("invalid PGM dimensions/maxval")
    return kind, w, h, maxval, pos


def decode_image_bytes(raw: bytes, suffix: str = "") -> tuple[np.ndarray, dict]:
    """Decode and validate one image; returns (gray uint8, facts).

    Raises :class:`ImageError` for truncated/corrupt/unsupported input.  The
    byte-level checks are deliberate: the recorder must prove that a stored
    file is complete, not merely that OpenCV returned an array.
    """
    if len(raw) < 16:
        raise ImageError("file too small to be an image")
    suffix = suffix.lower()
    if raw[:2] in (b"P5", b"P6"):
        kind, w, h, maxval, off = _pgm_header(raw)
        channels = 1 if kind == "P5" else 3
        if maxval > 255:
            raise ImageError(f"only 8-bit PGM is supported (maxval={maxval})")
        need = w * h * channels
        have = len(raw) - off
        if have < need:
            raise ImageError(f"truncated {kind}: {have} payload bytes, need {need}")
        payload = raw[off:off + need]
        if channels == 1:
            gray = np.frombuffer(payload, np.uint8).reshape(h, w).copy()
        else:
            rgb = np.frombuffer(payload, np.uint8).reshape(h, w, 3)
            gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1]
                    + 0.114 * rgb[..., 2]).astype(np.uint8)
        facts = {"format": kind, "width": int(w), "height": int(h),
                 "channels": int(channels), "maxval": int(maxval),
                 "trailing_bytes": int(max(0, have - need))}
        return gray, facts

    if raw[:2] == b"BM":
        if len(raw) < 54:
            raise ImageError("truncated BMP header")
        declared = int.from_bytes(raw[2:6], "little")
        if declared and declared != len(raw):
            raise ImageError(
                f"BMP size field {declared} != file size {len(raw)} "
                "(truncated or padded)")
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ImageError("BMP decode failed")
        facts = {"format": "BMP", "width": int(img.shape[1]),
                 "height": int(img.shape[0]),
                 "channels": int(1 if img.ndim == 2 else img.shape[2]),
                 "bit_depth": int(img.dtype.itemsize * 8)}
        return _to_gray_u8(img), facts

    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ImageError(f"unsupported or corrupt image ({suffix or 'unknown'})")
    facts = {"format": (suffix.lstrip(".").upper() or "unknown"),
             "width": int(img.shape[1]), "height": int(img.shape[0]),
             "channels": int(1 if img.ndim == 2 else img.shape[2]),
             "bit_depth": int(img.dtype.itemsize * 8)}
    return _to_gray_u8(img), facts


def _to_gray_u8(img: np.ndarray) -> np.ndarray:
    if img.dtype == np.uint16:
        img = (img >> 8).astype(np.uint8)
    elif img.dtype != np.uint8:
        if img.dtype.kind == "f":
            img = np.clip(img, 0, 255).astype(np.uint8)
        else:
            img = np.clip(img, 0, 255).astype(np.uint8)
    if img.ndim == 2:
        return img
    if img.ndim == 3:
        if img.shape[2] == 1:
            return img[..., 0]
        if img.shape[2] == 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if img.shape[2] == 4:
            return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    raise ImageError(f"unsupported image shape {img.shape}")


@dataclass
class LoadedImage:
    camera: str
    path: Optional[Path]
    raw: Optional[bytes] = None
    sha256: Optional[str] = None
    gray: Optional[np.ndarray] = None
    facts: dict = field(default_factory=dict)
    rotated_180: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.gray is not None and self.error is None

    def as_dict(self) -> dict:
        return {"camera": self.camera,
                "source": (str(self.path) if self.path is not None else None),
                "sha256": self.sha256,
                "bytes": (len(self.raw) if self.raw is not None else None),
                "decoded": bool(self.ok),
                "rotated_180": bool(self.rotated_180),
                "error": self.error,
                **{k: v for k, v in self.facts.items()
                   if k in ("format", "width", "height", "channels",
                            "trailing_bytes")}}


def load_image(camera: str, path: Optional[Path],
               rotate_180: bool = False) -> LoadedImage:
    """Load one frame; ``rotate_180`` is an in-memory orientation fix only.

    The returned ``raw``/``sha256`` always describe the original file bytes, so
    the retained copy stays byte-exact even when the decoded array is rotated
    back into the calibration orientation.
    """
    if path is None:
        return LoadedImage(camera, None, rotated_180=bool(rotate_180),
                           error="missing camera image")
    p = Path(path)
    if not p.is_file():
        return LoadedImage(camera, p, error=f"file not found: {p}")
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return LoadedImage(camera, p, error=f"read failed: {exc}")
    out = LoadedImage(camera, p, raw=raw, sha256=sha256_bytes(raw))
    out.rotated_180 = bool(rotate_180)
    try:
        gray, facts = decode_image_bytes(raw, p.suffix)
        if rotate_180:
            gray = cv2.rotate(gray, cv2.ROTATE_180)
        facts["rotated_180"] = bool(rotate_180)
        out.gray, out.facts = gray, facts
    except ImageError as exc:
        out.error = str(exc)
    return out


# ---------------------------------------------------------------------------
# input discovery
# ---------------------------------------------------------------------------

@dataclass
class InputSet:
    index: int
    label: str
    paths: dict[str, Path]
    timestamp: float = 0.0
    camera_timestamps: Optional[dict[str, float]] = None
    sync_source: str = "grouped-files"
    dwell: str = "assume"
    detections: Optional[dict] = None       # declared per-camera detections
    rotate_180: dict = field(default_factory=dict)  # camera -> in-memory rotation
    meta: dict = field(default_factory=dict)


@dataclass
class SourceInfo:
    kind: str
    path: str
    n_sets: int
    cameras: dict[str, int] = field(default_factory=dict)
    missing_by_camera: dict[str, int] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def sync_sidecar(self):
        return self.extra.get("sync_sidecar")

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "n_sets": self.n_sets,
                "cameras": dict(self.cameras),
                "missing_by_camera": dict(self.missing_by_camera),
                **self.extra}


def _parse_cam_map(text: Optional[str]) -> dict[int, str]:
    if not text:
        return dict(DEFAULT_CAM_MAP)
    out = {}
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            raise CalibSessionError(f"--cam-map: expected index=camera, got {tok!r}")
        k, v = tok.split("=", 1)
        try:
            idx = int(k)
        except ValueError as exc:
            raise CalibSessionError(f"--cam-map: bad camera index {k!r}") from exc
        v = v.strip().lower()
        if v not in CAMERAS:
            raise CalibSessionError(f"--cam-map: unknown camera {v!r}")
        out[idx] = v
    if sorted(out) != sorted(DEFAULT_CAM_MAP):
        raise CalibSessionError(
            f"--cam-map must map 0,1,2; got {sorted(out)}")
    return out


def _sync_sidecar(directory: Path) -> dict:
    for name in ("timestamps.json", "sync.json", "frames.json"):
        p = directory / name
        if p.is_file():
            try:
                return {"path": str(p), "data": read_json(p)}
            except Exception as exc:
                raise CalibSessionError(f"cannot parse {p}: {exc}") from exc
    return {}


def _timestamps_for_sidecar(data: Any, index: int, cam_map: Mapping[int, str]):
    """Extract per-camera timestamps for one frame index from a sidecar."""
    entry = None
    if isinstance(data, dict):
        for key in (str(index), f"{index:06d}", index):
            if key in data:
                entry = data[key]
                break
        if entry is None and "frames" in data:
            return _timestamps_for_sidecar(data["frames"], index, cam_map)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and int(item.get("frame", item.get("index", -1))) == index:
                entry = item
                break
        if entry is None and 0 <= index < len(data):
            entry = data[index]
    if entry is None:
        return None
    if isinstance(entry, dict):
        out = {}
        for cam_idx, name in cam_map.items():
            for key in (name, f"cam{cam_idx}", str(cam_idx)):
                if key in entry and entry[key] is not None:
                    out[name] = float(entry[key])
                    break
        if "timestamp" in entry and not out:
            out = {name: float(entry["timestamp"]) for name in cam_map.values()}
        return out or None
    if isinstance(entry, (list, tuple)) and len(entry) == len(cam_map):
        return {name: float(entry[i]) for i, name in cam_map.items()}
    if isinstance(entry, (int, float)):
        return {name: float(entry) for name in cam_map.values()}
    return None


def discover_frames(directory: str | Path,
                    cam_map: Optional[str] = None) -> tuple[list[InputSet], SourceInfo]:
    d = Path(directory)
    if not d.is_dir():
        raise CalibSessionError(f"--frames: not a directory: {d}")
    cmap = _parse_cam_map(cam_map)
    pattern = re.compile(
        r"^frame[_-]?(\d+)[_-]cam[_-]?([0-9]+)\.(pgm|ppm|png|bmp|tif|tiff)$",
        re.IGNORECASE)
    found: dict[int, dict[str, Path]] = {}
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        m = pattern.match(p.name)
        if not m:
            continue
        idx = int(m.group(1))
        cam_idx = int(m.group(2))
        if cam_idx not in cmap:
            raise CalibSessionError(
                f"unsupported camera index cam{cam_idx} in {p.name}; "
                f"expected {sorted(cmap)}")
        found.setdefault(idx, {})[cmap[cam_idx]] = p
    if not found:
        raise CalibSessionError(
            f"no frame_%06d_cam{{0,1,2}} triplets found in {d}")
    sidecar = _sync_sidecar(d)
    sets = []
    for seq, idx in enumerate(sorted(found)):
        ts = None
        if sidecar:
            ts = _timestamps_for_sidecar(sidecar["data"], idx, cmap)
        base = float(idx) if ts is None else float(min(ts.values()))
        sets.append(InputSet(index=idx, label=f"frame_{idx:06d}",
                             paths=dict(found[idx]), timestamp=base,
                             camera_timestamps=ts,
                             sync_source=("measured-sidecar" if ts else
                                          "grouped-files"),
                             dwell="sequential",
                             rotate_180={cam: True for cam in
                                         PROBE_ROTATED_CAMERAS
                                         if cam in found[idx]}))
    missing = {c: sum(1 for s in sets if c not in s.paths) for c in CAMERAS}
    present = {c: sum(1 for s in sets if c in s.paths) for c in CAMERAS}
    info = SourceInfo("frames", str(d), len(sets), present, missing,
                      {"cam_map": {str(k): v for k, v in cmap.items()},
                       "pattern": "frame_%06d_cam{0,1,2}",
                       "rotation_180": list(PROBE_ROTATED_CAMERAS),
                       "orientation_source": "probe hardware mapping "
                       "(stream 0 = right rotated 180, stream 1 = left as "
                       "calibrated, stream 2 = colour rotated 180)",
                       "sync_sidecar": sidecar.get("path")})
    return sets, info


def discover_vendor_corpus(directory: str | Path) -> tuple[list[InputSet], SourceInfo]:
    d = Path(directory)
    if not d.is_dir():
        raise CalibSessionError(f"--vendor-corpus: not a directory: {d}")
    pattern = re.compile(
        r"^image[_-]?(left|right|tex)[_-]?(\d+)\.(bmp|png|tif|tiff|pgm)$",
        re.IGNORECASE)
    found: dict[int, dict[str, Path]] = {}
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        m = pattern.match(p.name)
        if not m:
            continue
        side = m.group(1).lower()
        idx = int(m.group(2))
        found.setdefault(idx, {})[side] = p
    if not found:
        raise CalibSessionError(f"no imageLeft/imageRight/imageTex files in {d}")
    sets = [InputSet(index=idx, label=f"vendor_{idx:02d}",
                     paths=dict(found[idx]), timestamp=float(idx),
                     camera_timestamps=None, sync_source="grouped-files",
                     dwell="assume",
                     meta={"corpus_index": idx})
            for idx in sorted(found)]
    missing = {c: sum(1 for s in sets if c not in s.paths) for c in CAMERAS}
    present = {c: sum(1 for s in sets if c in s.paths) for c in CAMERAS}
    info = SourceInfo("vendor-corpus", str(d), len(sets), present, missing,
                      {"grouping": "image<Camera><N>.<ext>; N is the pose index",
                       "note": "timestamps absent; camera grouping is by file "
                               "index, not by a measured hardware timestamp"})
    return sets, info


def load_replay(manifest: str | Path) -> tuple[list[InputSet], SourceInfo]:
    p = Path(manifest)
    if not p.is_file():
        raise CalibSessionError(f"--replay: file not found: {p}")
    try:
        data = read_json(p)
    except Exception as exc:
        raise CalibSessionError(f"--replay: cannot parse {p}: {exc}") from exc
    if isinstance(data, dict):
        raw_sets = data.get("sets", data.get("frames"))
    elif isinstance(data, list):
        raw_sets = data
    else:
        raw_sets = None
    if not isinstance(raw_sets, list) or not raw_sets:
        raise CalibSessionError(
            "--replay: expected a JSON list of frame sets or {\"sets\": [...]}")
    base = p.parent
    sets: list[InputSet] = []
    seen_labels: set[str] = set()
    for i, item in enumerate(raw_sets):
        if not isinstance(item, dict):
            raise CalibSessionError(f"--replay: set {i} is not an object")
        label = str(item.get("label", f"set_{i:03d}"))
        if label in seen_labels:
            raise CalibSessionError(
                f"--replay: duplicate set label {label!r} at index {i}")
        seen_labels.add(label)
        cams = item.get("cameras", item.get("images", item.get("paths")))
        if not isinstance(cams, dict) or not cams:
            raise CalibSessionError(f"--replay: set {i} has no cameras/images map")
        paths: dict[str, Path] = {}
        for cam, rel in cams.items():
            cam = str(cam).lower()
            if cam not in CAMERAS:
                raise CalibSessionError(
                    f"--replay: set {i}: unknown camera {cam!r}")
            if rel is None:
                continue
            q = Path(str(rel))
            q = q if q.is_absolute() else (base / q)
            if not q.is_file():
                raise CalibSessionError(
                    f"--replay: set {i}: {cam} file not found: {q}")
            paths[cam] = q
        ts = item.get("timestamp", float(i))
        cts_raw = item.get("camera_timestamps")
        cts = ({str(k).lower(): float(v) for k, v in cts_raw.items()}
               if isinstance(cts_raw, dict) else None)
        if cts:
            for cam in cts:
                if cam not in CAMERAS:
                    raise CalibSessionError(
                        f"--replay: set {i}: unknown camera timestamp {cam!r}")
        sync_source = str(item.get("sync_source",
                                   "measured" if cts else "grouped-files"))
        det_raw = item.get("detections")
        detections = None
        if det_raw is not None:
            if not isinstance(det_raw, dict):
                raise CalibSessionError(
                    f"--replay: set {i}: detections must be a camera map")
            detections = {}
            for cam, pts in det_raw.items():
                cam = str(cam).lower()
                if cam not in CAMERAS:
                    raise CalibSessionError(
                        f"--replay: set {i}: unknown detection camera {cam!r}")
                if not isinstance(pts, dict):
                    raise CalibSessionError(
                        f"--replay: set {i}: {cam} detections must be an "
                        "object keyed by \"u,v\"")
                parsed = {}
                for key, xy in pts.items():
                    try:
                        uu, vv = (float(x) for x in str(key).split(","))
                        x, y = float(xy[0]), float(xy[1])
                    except Exception as exc:
                        raise CalibSessionError(
                            f"--replay: set {i}: bad {cam} detection "
                            f"{key!r} -> {xy!r}") from exc
                    parsed[(uu, vv)] = np.asarray([x, y], float)
                detections[cam] = parsed
        rot_raw = item.get("rotate_180", [])
        if isinstance(rot_raw, dict):
            rotate = {str(k).lower(): bool(v) for k, v in rot_raw.items()}
        elif isinstance(rot_raw, (list, tuple)):
            rotate = {str(k).lower(): True for k in rot_raw}
        else:
            raise CalibSessionError(
                f"--replay: set {i}: rotate_180 must be a list or camera map")
        for cam in rotate:
            if cam not in CAMERAS:
                raise CalibSessionError(
                    f"--replay: set {i}: unknown rotate_180 camera {cam!r}")
        dwell = str(item.get("dwell", "assume"))
        if dwell not in ("assume", "sequential"):
            raise CalibSessionError(
                f"--replay: set {i}: dwell must be assume|sequential")
        sets.append(InputSet(index=i, label=label, paths=paths,
                             timestamp=float(ts), camera_timestamps=cts,
                             sync_source=sync_source, dwell=dwell,
                             detections=detections, rotate_180=rotate,
                             meta={k: v for k, v in item.items()
                                   if k not in ("cameras", "images", "paths",
                                                "camera_timestamps",
                                                "detections", "rotate_180")}))
    missing = {c: sum(1 for s in sets if c not in s.paths) for c in CAMERAS}
    present = {c: sum(1 for s in sets if c in s.paths) for c in CAMERAS}
    info = SourceInfo("replay", str(p), len(sets), present, missing,
                      {"manifest_sha256": sha256_file(p)})
    return sets, info


# ---------------------------------------------------------------------------
# detection helper (optional on-disk cache)
# ---------------------------------------------------------------------------

class DetectionCache:
    def __init__(self, path: Optional[str | Path]):
        self.path = Path(path) if path else None
        self.data: dict = {}
        if self.path and self.path.is_file():
            try:
                self.data = read_json(self.path)
            except Exception:
                self.data = {}

    def key(self, sha: Optional[str]) -> Optional[str]:
        return f"{sha}:pattern-v1" if sha else None

    def get(self, sha: Optional[str]):
        k = self.key(sha)
        return None if k is None else self.data.get(k)

    def put(self, sha: Optional[str], grid) -> None:
        k = self.key(sha)
        if k is None:
            return
        self.data[k] = {"points": {f"{u},{v}": [float(x), float(y)]
                                   for (u, v), (x, y) in
                                   ((key, tuple(np.asarray(val, float)))
                                    for key, val in grid.points.items())},
                        "n_lattice": int(grid.n_lattice),
                        "rms": float(grid.rms)}
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, self.data)

    def as_grid(self, entry):
        from calib.detect import GridObservation
        pts = {tuple(float(x) for x in k.split(",")): np.asarray(v, float)
               for k, v in entry["points"].items()}
        return GridObservation(pts, np.eye(3), int(entry["n_lattice"]),
                               float(entry["rms"]), float("nan"), np.zeros(0))


def detect_image(loaded: LoadedImage, cache: Optional[DetectionCache] = None):
    if not loaded.ok:
        return None
    entry = cache.get(loaded.sha256) if cache is not None else None
    if entry is not None:
        return cache.as_grid(entry)
    grid = detect_grid_auto(loaded.gray)
    if grid is not None and cache is not None:
        cache.put(loaded.sha256, grid)
    return grid


def to_observation(camera: str, grid, view: int = -1) -> Observation:
    return Observation(camera, view,
                       {tuple(float(x) for x in k): np.asarray(v, float)
                        for k, v in grid.points.items()})


# ---------------------------------------------------------------------------
# held-out policy and the guided session subclass
# ---------------------------------------------------------------------------

@dataclass
class HoldoutPolicy:
    """Deterministic online held-out reservation.

    Every ``every``-th accepted view after ``start`` becomes validation-only.
    The cadence is chosen so that ``n_holdout`` views exist by the planning
    target: e.g. target 24 -> start 6, every 3 -> held-out at accepted views
    6, 9, 12, 15, 18 (five by 18 accepted).  A candidate that is not diverse
    enough with respect to the existing held-out poses is skipped and the next
    accepted view is tried, so the reservation is stratified without looking
    at any fitted quantity.
    """

    n_holdout: int = 5
    target: int = 24
    bootstrap_views: int = 4
    min_angle_deg: float = 8.0
    min_distance_mm: float = 40.0

    def __post_init__(self):
        self.n_holdout = int(self.n_holdout)
        self.target = int(self.target)
        self.bootstrap_views = int(self.bootstrap_views)
        if self.n_holdout < 1:
            raise CalibSessionError("--min-heldout must be >= 1")
        self.start = self.bootstrap_views + 2
        self.every = max(3, int(round(self.target / 8.0)))

    def as_dict(self) -> dict:
        return {"n_holdout": self.n_holdout, "target": self.target,
                "start_accepted_index": self.start,
                "every_accepted": self.every,
                "min_angle_deg": self.min_angle_deg,
                "min_distance_mm": self.min_distance_mm,
                "rule": ("hold every `every`-th accepted view after `start`, "
                         "until `n_holdout`; skip a view that is too similar "
                         "to the existing held-out poses")}


class GuidedSession(CalibrationSession):
    """``CalibrationSession`` with an explicit train/held-out split.

    Only two seams are overridden:

    * :meth:`_maybe_solve` designates the just-accepted view (if its turn has
      come) and then hands **training observations only** to the parent's
      solver.  The parent's maths, acceptance rule, quality gate, guidance and
      stopping logic are untouched.
    * :meth:`ingest` only brackets the call with a flag so the designation
      happens on a genuine acceptance, not on an external ``solve_now``.
    """

    def __init__(self, *, holdout: Optional[HoldoutPolicy] = None, **kwargs):
        self.holdout_policy = holdout or HoldoutPolicy()
        super().__init__(**kwargs)
        self.heldout_view_ids: set[int] = set()
        self.split_by_view: dict[int, str] = {}
        self.holdout_events: list[dict] = []
        self._next_holdout = int(self.holdout_policy.start)
        self._designate_on_ingest = False
        self.solve_errors: list[str] = []

    # -- train / held-out --------------------------------------------------
    def ingest(self, frames: FrameSet, grids: Optional[dict] = None):
        self._designate_on_ingest = True
        try:
            return super().ingest(frames, grids=grids)
        finally:
            self._designate_on_ingest = False

    def _maybe_solve(self) -> bool:
        if self._designate_on_ingest and self.accepted:
            self._designate_last_accepted()
        saved = self.obs
        if self.heldout_view_ids:
            self.obs = [o for o in saved
                        if int(o.view) not in self.heldout_view_ids]
        try:
            ok = super()._maybe_solve()
        finally:
            self.obs = saved
        if self.last_solve_error:
            self.solve_errors.append(str(self.last_solve_error))
            self.last_solve_error = None
        return ok

    def _designate_last_accepted(self) -> None:
        pol = self.holdout_policy
        if len(self.heldout_view_ids) >= pol.n_holdout:
            return
        n = len(self.accepted)
        if n < self._next_holdout:
            return
        if n > self._next_holdout:              # a previous candidate was skipped
            self._next_holdout = n
        view = self.accepted[-1]
        if self._pose_is_diverse(view.pose):
            self.heldout_view_ids.add(view.view_id)
            self.split_by_view[view.view_id] = "heldout"
            self.holdout_events.append({
                "accepted_index": n, "view_id": view.view_id,
                "rule": "scheduled", "pose": _pose_dict(view.pose)})
            self._next_holdout = n + pol.every
        else:
            self.holdout_events.append({
                "accepted_index": n, "view_id": view.view_id,
                "rule": "skipped_too_similar_to_heldout"})
            self._next_holdout = n + 1

    def _pose_is_diverse(self, pose) -> bool:
        pol = self.holdout_policy
        if pose is None:
            return False
        for vid in self.heldout_view_ids:
            other = next((a.pose for a in self.accepted if a.view_id == vid), None)
            if other is None:
                continue
            ang = _angle_deg(np.asarray(pose[0]) @ np.asarray(other[0]).T)
            dz = abs(float(np.linalg.norm(np.asarray(pose[1]))
                           - np.linalg.norm(np.asarray(other[1]))))
            if ang < pol.min_angle_deg and dz < pol.min_distance_mm:
                return False
        return True

    # -- accessors ---------------------------------------------------------
    @property
    def train_obs(self) -> list[Observation]:
        return [o for o in self.obs if int(o.view) not in self.heldout_view_ids]

    @property
    def heldout_obs(self) -> list[Observation]:
        return [o for o in self.obs if int(o.view) in self.heldout_view_ids]

    def split_of(self, view_id: Optional[int]) -> str:
        if view_id is None:
            return "rejected"
        return self.split_by_view.get(int(view_id), "train")

    def holdout_status(self) -> dict:
        pol = self.holdout_policy
        return {"reserved": len(self.heldout_view_ids),
                "required": pol.n_holdout,
                "next_at_accepted_index": int(self._next_holdout),
                "start": int(pol.start), "every": int(pol.every),
                "events": list(self.holdout_events)}


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

def _bin_index(value: float, edges: Sequence[float]) -> int:
    return int(np.clip(np.searchsorted(edges, value) - 1, 0, len(edges) - 2))


def compute_coverage(accepted: Sequence[dict], rules: Optional[dict] = None) -> dict:
    """Distance / tilt / roll / image-quadrant coverage of accepted sets."""
    r = {**DEFAULT_RULES, **(rules or {})}
    edges = [float(x) for x in r["distance_edges_mm"]]
    poses = [a.get("pose") for a in accepted if a.get("pose")]
    distances = np.array([float(p["distance_mm"]) for p in poses], float) \
        if poses else np.zeros(0)
    tilts = np.array([float(p["tilt_deg"]) for p in poses], float) \
        if poses else np.zeros(0)
    tx = np.array([float(p["tilt_x_deg"]) for p in poses], float) \
        if poses else np.zeros(0)
    ty = np.array([float(p["tilt_y_deg"]) for p in poses], float) \
        if poses else np.zeros(0)
    rolls = np.array([float(p["roll_deg"]) for p in poses], float) \
        if poses else np.zeros(0)

    distance_counts = [0] * (len(edges) - 1)
    below = above = 0
    for d in distances:
        if d < edges[0]:
            below += 1
        elif d > edges[-1]:
            above += 1
        else:
            distance_counts[_bin_index(float(d), edges)] += 1

    med_tx = float(np.median(tx)) if len(tx) else 0.0
    med_ty = float(np.median(ty)) if len(ty) else 0.0
    sign = float(r["tilt_sign_deg"])
    rel_tx = tx - med_tx
    rel_ty = ty - med_ty
    tilt_x_signs = [int(np.sum(rel_tx > sign)), int(np.sum(rel_tx < -sign))]
    tilt_y_signs = [int(np.sum(rel_ty > sign)), int(np.sum(rel_ty < -sign))]

    roll_bin_deg = float(r["roll_bin_deg"])
    roll_edges = list(np.arange(-90.0, 90.0 + 1e-9, roll_bin_deg))
    roll_counts = [0] * (len(roll_edges) - 1)
    for a in rolls:
        roll_counts[_bin_index(float(a), roll_edges)] += 1

    quadrants: dict[str, list[int]] = {c: [0, 0, 0, 0] for c in CAMERAS}
    quad_views: dict[str, int] = {c: 0 for c in CAMERAS}
    for a in accepted:
        q = (a.get("quadrants") or {})
        for c in CAMERAS:
            idx = q.get(c)
            if idx is not None and 0 <= int(idx) < 4:
                quadrants[c][int(idx)] += 1
                quad_views[c] += 1
    quad_fraction = {c: (float(sum(1 for x in quadrants[c] if x > 0) / 4.0)
                         if accepted else 0.0) for c in CAMERAS}

    stereo = [int((a.get("quality", {}).get("metrics", {}) or {})
                  .get("stereo_markers", 0) or 0) for a in accepted]
    big = [int(a.get("quality", {}).get("metrics", {}).get("big_markers", 0)
               or 0) for a in accepted]
    tex_markers = [a.get("colour", {}).get("tex_markers")
                   for a in accepted]
    tex_detected = [m for m in tex_markers if m is not None and m > 0]

    cov = {
        "n_accepted": len(accepted),
        "distance_edges_mm": edges,
        "distance_counts": distance_counts,
        "distance_bins_occupied": int(sum(1 for x in distance_counts if x > 0)),
        "distance_out_of_range": {"below_mm": below, "above_mm": above},
        "distance_min_mm": float(distances.min()) if len(distances) else None,
        "distance_max_mm": float(distances.max()) if len(distances) else None,
        "tilt_deg_min": float(tilts.min()) if len(tilts) else None,
        "tilt_deg_max": float(tilts.max()) if len(tilts) else None,
        "tilt_median_signed_x_deg": med_tx,
        "tilt_median_signed_y_deg": med_ty,
        "tilt_x_signs": tilt_x_signs,
        "tilt_y_signs": tilt_y_signs,
        "tilt_x_range_deg": (float(tx.max() - tx.min()) if len(tx) else 0.0),
        "tilt_y_range_deg": (float(ty.max() - ty.min()) if len(ty) else 0.0),
        "roll_edges_deg": [float(x) for x in roll_edges],
        "roll_counts": roll_counts,
        "roll_bins_occupied": int(sum(1 for x in roll_counts if x > 0)),
        "roll_values_deg": [float(x) for x in rolls],
        "image_quadrant_counts": quadrants,
        "image_quadrant_views": quad_views,
        "image_quadrant_fraction": quad_fraction,
        "stereo_markers_min": int(min(stereo)) if stereo else None,
        "stereo_markers_median": float(np.median(stereo)) if stereo else None,
        "big_markers_min": int(min(big)) if big else None,
        "tex_markers_min": int(min(tex_detected)) if tex_detected else None,
        "tex_detection_fraction": (float(len(tex_detected) / len(accepted))
                                   if accepted else 0.0),
    }
    cov["missing"] = coverage_missing(cov, r)
    cov["ok"] = not cov["missing"]
    return cov


def coverage_missing(cov: dict, rules: Optional[dict] = None) -> list[str]:
    r = {**DEFAULT_RULES, **(rules or {})}
    missing: list[str] = []
    if cov["distance_bins_occupied"] < int(r["distance_bins_min"]):
        empty = [f"{cov['distance_edges_mm'][i]:.0f}-"
                 f"{cov['distance_edges_mm'][i+1]:.0f} mm"
                 for i, n in enumerate(cov["distance_counts"]) if n == 0]
        missing.append(
            f"distance coverage: {cov['distance_bins_occupied']} of "
            f"{len(cov['distance_counts'])} bins occupied "
            f"(need {r['distance_bins_min']}); empty: {', '.join(empty)}")
    if r["tilt_both_signs"]:
        for axis, signs in (("x", cov["tilt_x_signs"]), ("y", cov["tilt_y_signs"])):
            if signs[0] < 1 or signs[1] < 1:
                missing.append(
                    f"tilt {axis} signs: positive {signs[0]}, negative {signs[1]} "
                    f"(need >=1 each, relative to the median pose)")
    if cov["tilt_deg_max"] is not None and \
            cov["tilt_deg_max"] < float(r["tilt_max_deg_min"]):
        missing.append(
            f"tilt magnitude: max {cov['tilt_deg_max']:.1f} deg "
            f"(need >= {r['tilt_max_deg_min']:.0f} deg)")
    if cov["n_accepted"]:
        for cam in ("left", "right"):
            if cov["image_quadrant_fraction"][cam] + 1e-9 < float(
                    r["quadrant_fraction_min"]):
                missing.append(
                    f"{cam} image quadrants: "
                    f"{cov['image_quadrant_fraction'][cam]:.2f} covered "
                    f"(need {r['quadrant_fraction_min']:.2f})")
        if int(r["roll_bins_min"]) > 0 and \
                cov["roll_bins_occupied"] < int(r["roll_bins_min"]):
            missing.append(
                f"roll coverage: {cov['roll_bins_occupied']} bins occupied "
                f"(need {r['roll_bins_min']})")
    return missing


# ---------------------------------------------------------------------------
# candidate records
# ---------------------------------------------------------------------------

@dataclass
class CandidateRecord:
    seq: int
    label: str
    timestamp: float
    source: dict
    files: dict
    camera_present: dict
    detection: dict
    n_markers: dict
    accepted: bool
    reasons: list
    split: str
    view_id: Optional[int]
    quality: dict
    pose: Optional[dict]
    motion: Optional[dict]
    sync: dict
    dwell_state: str
    colour: dict
    predicted_gain: Optional[float]
    holdout_rms_px: Optional[float]
    gate: dict
    session: dict = field(default_factory=dict)
    quadrants: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return _jsonable({
            "seq": self.seq, "label": self.label, "timestamp": self.timestamp,
            "source": self.source, "files": self.files,
            "camera_present": self.camera_present,
            "detection": self.detection, "n_markers": self.n_markers,
            "accepted": self.accepted, "decision": ("accepted" if self.accepted
                                                    else "rejected"),
            "reasons": self.reasons, "split": self.split,
            "view_id": self.view_id, "quality": self.quality,
            "pose": self.pose, "motion_vs_previous": self.motion,
            "sync": self.sync, "dwell_state": self.dwell_state,
            "colour": self.colour, "quadrants": self.quadrants,
            "predicted_gain": self.predicted_gain,
            "holdout_rms_px": self.holdout_rms_px, "recorder_gate": self.gate,
            "session": self.session,
        })


# ---------------------------------------------------------------------------
# stereo-consistency pre-gate
# ---------------------------------------------------------------------------

def pnp_view_metrics(grids: Mapping[str, Observation],
                     cameras: Mapping[str, CameraIntrinsics],
                     pitch: float) -> dict:
    """Per-camera PnP reprojection RMS and the left/right relative pose.

    A single-camera PnP can fit a *wrong* lattice assignment almost perfectly;
    the relative board pose implied by the two IR cameras cannot (the Einstar
    corpus contains one such frame, set 9: left PnP 0.57 px, right 0.35 px,
    but the implied rig rotation is 179.5 deg).  This is the check that keeps
    such a frame out of the fit.
    """
    from calib import plate as _plate
    obj = _plate.board_points_mm(pitch)
    uv_idx = [(float(u), float(v)) for (u, v) in _plate.lattice_uv()]
    out: dict[str, Any] = {"cameras": {}}
    poses = {}
    for cam in ("left", "right", "tex"):
        o = grids.get(cam)
        if o is None or len(o.uv) < 6:
            continue
        keys = [k for k in uv_idx if k in o.uv]
        if len(keys) < 6:
            continue
        cam_model = cameras.get(cam)
        if cam_model is None:
            continue
        pts3 = np.asarray([obj[uv_idx.index(k)] for k in keys], float)
        pts2 = np.asarray([o.uv[k] for k in keys], float)
        p = solve_pnp(pts3, pts2, cam_model.K, cam_model.kc)
        if p is None:
            continue
        Xc = pts3 @ p.R.T + p.t
        pred = project(cam_model.K, cam_model.kc, Xc)
        rms = float(np.sqrt(np.mean(np.sum((pred - pts2) ** 2, axis=1))))
        poses[cam] = p
        out["cameras"][cam] = {"rms_px": rms, "n_markers": int(len(keys))}
    if "left" in poses and "right" in poses:
        R = poses["right"].R @ poses["left"].R.T
        t = poses["right"].t - R @ poses["left"].t
        out["stereo"] = {"R": R.tolist(), "t": t.tolist(),
                         "baseline_mm": float(np.linalg.norm(t))}
    return out


def stereo_gate(metrics: dict, result: Optional[CalibrationResult],
                rules: dict) -> tuple[bool, list[str], dict]:
    """Compare the per-view stereo pose with the current calibration."""
    reasons: list[str] = []
    detail: dict = {}
    for cam, m in (metrics.get("cameras") or {}).items():
        if m["rms_px"] > float(rules["max_view_pnp_rms_px"]):
            reasons.append(
                f"{cam} reprojection inconsistent ({m['rms_px']:.2f} px > "
                f"{rules['max_view_pnp_rms_px']:.1f} px)")
    if result is not None and "stereo" in metrics:
        Rr, tr = result.extrinsics["right"]
        Rl, tl = result.extrinsics["left"]
        Rt = Rr @ Rl.T
        tt = tr - Rt @ tl
        Rv = np.asarray(metrics["stereo"]["R"], float)
        tv = np.asarray(metrics["stereo"]["t"], float)
        dR = _angle_deg(Rv @ Rt.T)
        dt = float(np.linalg.norm(tv - tt))
        detail = {"delta_rotation_deg": dR, "delta_translation_mm": dt,
                  "reference_baseline_mm": float(np.linalg.norm(tt))}
        if dR > float(rules["max_stereo_pose_delta_deg"]) or \
                dt > float(rules["max_stereo_pose_delta_mm"]):
            reasons.append(
                f"stereo pose inconsistent with the current calibration "
                f"(dR={dR:.1f} deg, dt={dt:.1f} mm; limits "
                f"{rules['max_stereo_pose_delta_deg']:.1f} deg / "
                f"{rules['max_stereo_pose_delta_mm']:.1f} mm)")
    return (not reasons), reasons, detail


# ---------------------------------------------------------------------------
# session recorder
# ---------------------------------------------------------------------------

def _lm_solve(obs, pitch, cameras, free_kc, robust_loss, f_scale):
    """Fast session solve: LM on a built Problem, with the engine fallback.

    ``CalibrationSession`` expects ``solve_fn(obs, pitch, cameras, free_kc,
    robust_loss, f_scale)``.  The session's own default first tries
    ``solvers.solve_joint(..., pitch_mm=...)``, which raises ``TypeError`` on
    the current solver API and falls back to the much slower SciPy engine; this
    adapter passes the Problem object instead and keeps the LM backend.
    """
    try:
        from calib import solvers
        prob = solvers.make_problem(obs, pitch_mm=pitch, init_cameras=cameras)
        out = solvers.solve_joint(prob, backend="lm", robust=robust_loss,
                                  f_scale=f_scale, free_kc=free_kc,
                                  covariance=True)
        res = out.get("result")
        if res is not None:
            return res
    except Exception:
        pass
    return calibrate(obs, pitch, cameras, free_kc=free_kc,
                     robust_loss=robust_loss, f_scale=f_scale,
                     free_intrinsics=True)


def _default_camera(size_hint: tuple[int, int] = (1280, 1024)) -> CameraIntrinsics:
    w, h = size_hint
    return CameraIntrinsics(1150.0, 1150.0, w / 2.0, h / 2.0, np.zeros(5))


def _plausible_camera(cam: CameraIntrinsics,
                      size_hint: tuple[int, int] = (1280, 1024)) -> bool:
    w, h = size_hint
    if not (100.0 <= cam.fx <= 10000.0 and 100.0 <= cam.fy <= 10000.0):
        return False
    if not (-w <= cam.cx <= 2 * w and -h <= cam.cy <= 2 * h):
        return False
    if not np.all(np.isfinite(np.asarray(cam.kc, float))):
        return False
    return True


def _load_factory(factory_dir: Optional[str | Path],
                  tex_profile: Optional[str | Path]) -> tuple[dict, dict, dict]:
    """Load factory CCFs; return (records, errors, provenance)."""
    records: dict[str, CCFRecord] = {}
    errors: dict[str, str] = {}
    sources: dict[str, str] = {}
    if factory_dir is not None:
        d = Path(factory_dir)
        if not d.is_dir():
            raise CalibSessionError(f"--factory-dir: not a directory: {d}")
        for name, cam in (("LeftCCF.txt", "left"), ("RightCCF.txt", "right"),
                          ("TexCCF.txt", "tex")):
            p = d / name
            if not p.is_file():
                errors[cam] = f"missing {p}"
                continue
            try:
                records[cam] = load_ccf(p)
                sources[cam] = str(p)
            except Exception as exc:
                errors[cam] = f"{type(exc).__name__}: {exc}"
    if tex_profile is not None:
        p = Path(tex_profile)
        if not p.is_file():
            raise CalibSessionError(f"--tex-profile: file not found: {p}")
        try:
            records["tex"] = load_ccf(p)
            sources["tex"] = str(p)
            errors.pop("tex", None)
        except Exception as exc:
            errors["tex"] = f"{type(exc).__name__}: {exc}"
            records.pop("tex", None)
    return records, errors, sources


class SessionRecorder:
    """One guided calibration session: frame sets in, complete session out."""

    def __init__(self, session_dir: str | Path, *,
                 source_kind: str = "frames",
                 source_info: Optional[SourceInfo] = None,
                 plate_profile: Optional[PlateProfile] = None,
                 mode: str = "standard",
                 target: int = 24,
                 rules: Optional[dict] = None,
                 holdout: Optional[HoldoutPolicy] = None,
                 localize_method: str = "auto",
                 dwell_mode: str = "assume",
                 factory_dir: Optional[str | Path] = None,
                 factory_source: str = "explicit",
                 tex_profile: Optional[str | Path] = None,
                 solve_every: int = 1,
                 seed: int = 0,
                 link: bool = False,
                 force: bool = False,
                 cache: Optional[str | Path] = None,
                 quality: Optional[dict] = None,
                 image_size: Optional[tuple[int, int]] = None,
                 init_cameras: Optional[Mapping[str, CameraIntrinsics]] = None,
                 verbose: bool = False):
        self.dir = Path(session_dir)
        if self.dir.exists() and any(self.dir.iterdir()) and not force:
            raise CalibSessionError(
                f"session directory is not empty: {self.dir} (use --force)")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sets_dir = self.dir / "sets"
        self.sets_dir.mkdir(exist_ok=True)
        self.link = bool(link)
        self.verbose = bool(verbose)
        self.source_kind = source_kind
        self.source_info = source_info or SourceInfo(source_kind, "", 0)
        self.plate = plate_profile or PlateProfile()
        self.spec = self.plate.apply()
        self.rules = {**DEFAULT_RULES, **(rules or {})}
        self.target = int(target)
        self.dwell_mode = dwell_mode
        self.localize_method = localize_method
        self.cache = DetectionCache(cache)
        self.image_size = tuple(image_size) if image_size else (1280, 1024)
        self.started_utc = _now_iso()
        self.t_start = time.time()

        # factory / colour profile ----------------------------------------
        self.factory_records, self.factory_errors, self.factory_sources = \
            _load_factory(factory_dir, tex_profile)
        self.factory_dir = str(factory_dir) if factory_dir else None
        self.factory_source = factory_source if factory_dir else "none"
        self.tex_profile = str(tex_profile) if tex_profile else None
        self._factory_consistency: Optional[dict] = None
        self.colour_profile_state = self._assess_colour_profile()
        self._init_override = ({c: CameraIntrinsics(cam.fx, cam.fy, cam.cx,
                                                    cam.cy, np.asarray(cam.kc,
                                                                       float).copy(),
                                                    cam.skew)
                                for c, cam in (init_cameras or {}).items()}
                               or None)

        init = self._initial_cameras()
        self.init_cameras = init
        self.session = GuidedSession(
            holdout=holdout or HoldoutPolicy(target=self.target),
            mode=mode, factory=self.factory_records or None,
            pitch_mm=self.plate.pitch_mm, cameras=init,
            solve_fn=_lm_solve, live=True, seed=seed,
            quality=quality, localize_method=localize_method)
        self.session.policy["require_images"] = True
        if solve_every and int(solve_every) > 0:
            self.session.policy["solve_every"] = int(solve_every)
        self.mode = mode
        self.records: list[CandidateRecord] = []
        self._previous_candidate_pose: Optional[tuple] = None
        self._previous_candidate_timestamp: Optional[float] = None
        self._profile_info: dict = {}
        self._write_initial_config()

    # -- setup -------------------------------------------------------------
    def _assess_colour_profile(self) -> dict:
        rec = self.factory_records.get("tex")
        err = self.factory_errors.get("tex")
        if rec is None:
            state = "missing" if (err is None or str(err).startswith("missing")) \
                else "unreadable"
            return {"state": state,
                    "error": err,
                    "source": self.factory_sources.get("tex")
                    or self.tex_profile,
                    "loaded": False, "plausible": False}
        cam = CameraIntrinsics.from_ccf(rec)
        plausible = _plausible_camera(cam)   # default probe/corpus size
        return {"state": ("present" if plausible else "implausible"),
                "error": None,
                "source": self.factory_sources.get("tex"),
                "loaded": True, "plausible": bool(plausible),
                "fx": float(cam.fx), "fy": float(cam.fy),
                "cx": float(cam.cx), "cy": float(cam.cy)}

    def _initial_cameras(self) -> dict[str, CameraIntrinsics]:
        if self._init_override:
            out = dict(self._init_override)
            for cam in CAMERAS:
                out.setdefault(cam, _default_camera(self.image_size))
            return out
        out: dict[str, CameraIntrinsics] = {}
        base = _default_camera(self.image_size)
        for cam in CAMERAS:
            rec = self.factory_records.get(cam)
            if rec is not None:
                try:
                    cand = CameraIntrinsics.from_ccf(rec)
                    if _plausible_camera(cand, self.image_size):
                        out[cam] = cand
                        continue
                except Exception:
                    pass
            out[cam] = base
        # a stale colour record (e.g. a left CCF copied into TexCCF) must not
        # start the joint solve at a co-located colour camera or absurd focal
        # length; fall back to the left IR intrinsics and report the state.
        left = out.get("left", base)
        tex = out.get("tex", base)
        if self.colour_profile_state.get("state") != "present":
            out["tex"] = CameraIntrinsics(left.fx, left.fy, left.cx, left.cy,
                                          np.asarray(left.kc, float).copy())
        else:
            try:
                Rrel, trel = _ccf_relative_left_tex(self.factory_records)
                if float(np.linalg.norm(trel)) < 20.0 or \
                        float(np.linalg.norm(trel)) > 400.0:
                    self.colour_profile_state["state"] = "stale"
                    self.colour_profile_state["reason"] = (
                        f"factory left-tex baseline {np.linalg.norm(trel):.1f} mm "
                        "is implausible; colour record not trusted for init")
                    out["tex"] = CameraIntrinsics(
                        left.fx, left.fy, left.cx, left.cy,
                        np.asarray(left.kc, float).copy())
            except Exception:
                pass
        if not _plausible_camera(out.get("tex", tex)):
            out["tex"] = CameraIntrinsics(left.fx, left.fy, left.cx, left.cy,
                                          np.asarray(left.kc, float).copy())
        return out

    def _write_initial_config(self) -> None:
        write_json(self.dir / "config.json", {
            "schema": SCHEMA, "created_utc": self.started_utc,
            "tool": TOOL_NAME,
            "source": self.source_info.as_dict(),
            "plate": self.plate.as_dict(),
            "mode": self.mode,
            "target": self.target,
            "rules": self.rules,
            "holdout": self.session.holdout_policy.as_dict(),
            "dwell_mode": self.dwell_mode,
            "localize_method": self.localize_method,
            "factory_dir": self.factory_dir,
            "factory_source": self.factory_source,
            "factory_errors": self.factory_errors,
            "tex_profile": self.tex_profile,
            "colour_profile": self.colour_profile_state,
            "init_cameras": {c: cam.as_dict() for c, cam in
                             self.init_cameras.items()},
            "note": "host-side only; nothing is written to the scanner",
        })

    # -- per set -----------------------------------------------------------
    def process(self, item: InputSet) -> CandidateRecord:
        rotations = dict(item.rotate_180 or {})
        loaded = {c: load_image(c, item.paths.get(c),
                                rotate_180=bool(rotations.get(c))) 
                  for c in CAMERAS}
        images = {c: im.gray for c, im in loaded.items() if im.ok}
        grids: dict[str, Observation] = {}
        detection: dict[str, dict] = {}
        declared = item.detections is not None
        for c in CAMERAS:
            im = loaded[c]
            if not im.ok:
                detection[c] = {"n_lattice": 0, "rms": None,
                                "error": im.error, "source": "image"}
                continue
            if declared:
                pts = (item.detections or {}).get(c)
                if pts:
                    grids[c] = Observation(c, -1,
                                           {tuple(float(x) for x in k):
                                            np.asarray(v, float)
                                            for k, v in pts.items()})
                    detection[c] = {"n_lattice": len(pts), "rms": None,
                                    "error": None, "source": "manifest"}
                else:
                    detection[c] = {"n_lattice": 0, "rms": None,
                                    "error": "not provided by replay manifest",
                                    "source": "manifest"}
                continue
            g = detect_image(im, self.cache)
            detection[c] = ({"n_lattice": int(g.n_lattice),
                             "rms": float(g.rms),
                             "error": None, "source": "image"}
                            if g is not None else
                            {"n_lattice": 0, "rms": None,
                             "error": "grid not assigned", "source": "image"})
            if g is not None:
                grids[c] = to_observation(c, g)

        # what the session itself would see as previous pose -----------------
        # the CLI/recorder dwell mode is authoritative: "assume" means each
        # input set is a committed keyframe, "sequential" applies the live
        # motion gate between successive sets (regardless of per-item metadata)
        assume = (self.dwell_mode == "assume")
        prev_pose = None if assume else self.session.last_pose
        prev_ts = None if assume else self.session.last_timestamp
        ts = item.timestamp
        cts = item.camera_timestamps
        # the quality evaluation uses current cameras/rig, exactly like ingest
        q = guidance.evaluate_frame(
            images=images, grids=grids, timestamp=ts,
            previous_pose=prev_pose, previous_timestamp=prev_ts,
            camera_timestamps=cts, quality=self.session.policy.get("quality"),
            cameras=self.session.cameras, rig=self.session._rig(),
            require_images=True)

        # recorder pre-gate ---------------------------------------------------
        pnp = pnp_view_metrics(grids, self.session.cameras, self.plate.pitch_mm)
        gate_ok, gate_reasons, gate_detail = stereo_gate(
            pnp, self.session.result, self.rules)
        gate = {"passed": bool(gate_ok), "reasons": gate_reasons,
                "detail": gate_detail,
                "pnp": {k: v for k, v in (pnp.get("cameras") or {}).items()}}

        sync_state = self._sync_state(cts, item.sync_source, q)
        dwell_state = self._dwell_state(item, assume, q, prev_pose)
        colour = self._colour_state(loaded, grids)

        frameset = FrameSet(images=images, timestamp=ts,
                            camera_timestamps=cts, label=item.label)
        if gate_ok:
            if assume:
                # a committed keyframe is already stationary; the parent session
                # must not compare it with the *previous pose* (that would reject
                # every new pose as "moving").  Duplicate/gain checks still use
                # every accepted pose.
                self.session.last_pose = None
                self.session.last_timestamp = None
            decision = self.session.ingest(frameset, grids=grids)
        else:
            # keep the parent session's dwell bookkeeping consistent for the
            # next sequential frame, then record a recorder-level rejection.
            if q.pose is not None:
                self.session.last_pose = q.pose
                self.session.last_timestamp = ts
            decision = None

        if decision is None:
            accepted = False
            reasons = list(gate_reasons)
            view_id = None
            split = "rejected"
            gain = None
            holdout_rms = None
            status = {}
        else:
            accepted = bool(decision.accepted)
            reasons = list(decision.reasons)
            view_id = decision.view_id if accepted else None
            split = self.session.split_of(view_id) if accepted else "rejected"
            gain = decision.predicted_gain
            status = decision.status or {}
            if accepted:
                av = next((a for a in self.session.accepted
                           if a.view_id == view_id), None)
                holdout_rms = None if av is None else av.holdout_rms_px
            else:
                holdout_rms = None

        # pose (prefer the session's normalised pose on acceptance)
        pose = None
        if accepted:
            av = next((a for a in self.session.accepted
                       if a.view_id == view_id), None)
            pose = _pose_dict(av.pose) if av is not None else None
        if pose is None:
            pose = _pose_dict(q.pose)
        motion = self._motion_vs_previous(q.pose, ts, assume)
        quadrants = self._quadrants(grids, images)
        session_brief = self._session_brief(status)

        record = CandidateRecord(
            seq=len(self.records), label=item.label, timestamp=float(ts),
            source={c: (str(item.paths[c]) if c in item.paths else None)
                    for c in CAMERAS},
            files={}, camera_present={c: bool(loaded[c].ok) for c in CAMERAS},
            detection=detection,
            n_markers={c: int(sum(1 for _ in grids[c].uv)) if c in grids else 0
                       for c in CAMERAS},
            accepted=accepted, reasons=reasons, split=split, view_id=view_id,
            quality=q.as_dict(), pose=pose, motion=motion,
            sync={"state": sync_state[0], "source": sync_state[1],
                  "skew_ms": sync_state[2]},
            dwell_state=dwell_state, colour=colour,
            predicted_gain=(float(gain) if gain is not None else None),
            holdout_rms_px=(float(holdout_rms) if holdout_rms is not None
                            else None),
            gate=gate, session=session_brief, quadrants=quadrants)
        record.files = self._retain(item, loaded, record)
        self.records.append(record)
        self._append_candidate(record)
        self._previous_candidate_pose = q.pose
        self._previous_candidate_timestamp = ts
        if self.verbose:
            self._print_progress(record)
        return record

    # -- per-set helpers ---------------------------------------------------
    def _sync_state(self, cts, source: str, q) -> tuple[str, str, Optional[float]]:
        skew = q.metrics.get("sync_skew_ms") if q is not None else None
        if cts and len(cts) >= 2:
            return ("measured", source, float(skew) if skew is not None else None)
        if source in ("grouped-files", "capture-index"):
            return ("declared", "grouped by capture index; no hardware "
                    "timestamps available", None)
        if source == "replay-declared":
            return ("declared", source, None)
        return ("unknown", source, None)

    def _dwell_state(self, item: InputSet, assume: bool, q, prev_pose) -> str:
        if assume:
            return "assumed-keyframe"
        if q is None:
            return "unknown"
        if prev_pose is None:
            return "first-frame"
        if any("moving" in r for r in q.reasons):
            return "moving"
        return "stationary"

    def _motion_vs_previous(self, pose, ts, assume) -> Optional[dict]:
        if pose is None or self._previous_candidate_pose is None:
            return None
        R0, t0 = self._previous_candidate_pose
        ang = _angle_deg(np.asarray(pose[0]) @ np.asarray(R0).T)
        d = float(np.linalg.norm(np.asarray(pose[1]) - np.asarray(t0)))
        dt = (float(ts - self._previous_candidate_timestamp)
              if self._previous_candidate_timestamp is not None else None)
        return {"translation_mm": d, "rotation_deg": ang, "dt_s": dt,
                "dwell_compared": (not assume)}

    def _colour_state(self, loaded, grids) -> dict:
        tex = loaded["tex"]
        tex_markers = (int(len(grids["tex"].uv)) if "tex" in grids else 0)
        return {"tex_present": bool(tex.path is not None),
                "tex_decoded": bool(tex.ok),
                "tex_markers": (tex_markers if tex.ok else None),
                "tex_error": tex.error,
                "frames_kept": True}

    def _quadrants(self, grids, images) -> dict:
        out: dict[str, Optional[int]] = {}
        for c in CAMERAS:
            g = grids.get(c)
            img = images.get(c)
            if g is None or not g.uv or img is None:
                out[c] = None
                continue
            uv = np.asarray(list(g.uv.values()), float)
            u, v = float(uv[:, 0].mean()), float(uv[:, 1].mean())
            h, w = img.shape[:2]
            out[c] = int((1 if v >= h / 2 else 0) * 2 +
                         (1 if u >= w / 2 else 0))
        return out

    @staticmethod
    def _session_brief(status: dict) -> dict:
        """Compact copy of the parent status (the full one is redundant)."""
        if not status:
            return {}
        task = status.get("task") or {}
        return {
            "n_accepted": status.get("n_accepted"),
            "n_rejected": status.get("n_rejected"),
            "n_solves": status.get("n_solves"),
            "rms_px": status.get("rms_px"),
            "baseline_mm": status.get("baseline_mm"),
            "task_value": task.get("value"),
            "task_sigma_mm": task.get("sigma_mm_median"),
            "bins_occupied": (status.get("bins") or {}).get("occupied"),
        }

    # -- retention ---------------------------------------------------------
    def _retain(self, item: InputSet, loaded, record: CandidateRecord) -> dict:
        set_dir = self.sets_dir / f"{record.seq:04d}_{_safe(item.label)}"
        set_dir.mkdir(parents=True, exist_ok=False)
        files: dict[str, dict] = {}
        for c in CAMERAS:
            im = loaded[c]
            entry = im.as_dict()
            if im.ok:
                dst = set_dir / Path(im.path).name
                if self.link:
                    try:
                        os.link(im.path, dst)
                    except OSError:
                        shutil.copyfile(im.path, dst)
                else:
                    shutil.copyfile(im.path, dst)
                digest = sha256_file(dst)
                entry.update({
                    "stored": str(dst.relative_to(self.dir)),
                    "copy_sha256": digest,
                    "copy_verified": bool(digest == im.sha256),
                    "bytes": int(dst.stat().st_size),
                })
                if digest != im.sha256:
                    raise CalibSessionError(
                        f"copy verification failed for {im.path} -> {dst}")
            else:
                entry.update({"stored": None, "copy_sha256": None,
                              "copy_verified": False})
            files[c] = entry
        meta = {"seq": record.seq, "label": record.label,
                "files": files, "accepted": record.accepted,
                "split": record.split, "reasons": record.reasons}
        write_json(set_dir / "meta.json", meta)
        return files

    def _append_candidate(self, record: CandidateRecord) -> None:
        path = self.dir / "candidates.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_jsonable(record.as_dict()), sort_keys=True,
                               allow_nan=False) + "\n")

    def _print_progress(self, rec: CandidateRecord) -> None:
        tag = "ACCEPT" if rec.accepted else "reject"
        counts = "/".join(str(rec.n_markers.get(c, 0)) for c in CAMERAS)
        d = rec.pose["distance_mm"] if rec.pose else float("nan")
        tilt = rec.pose["tilt_deg"] if rec.pose else float("nan")
        print(f"[{rec.seq + 1:3d}] {rec.label:18s} {tag:6s} "
              f"markers {counts:>12s}  d={d:6.0f} mm  tilt={tilt:5.1f} deg"
              + ("" if rec.accepted else
                 "  " + "; ".join(rec.reasons[:2])[:120]))

    # -- live guidance -----------------------------------------------------
    def should_stop(self) -> dict:
        """Stop only when the session's report and the data minima agree."""
        if self.session.result is None:
            return {"stop": False, "reasons": ["no calibration yet"]}
        try:
            stop = self.session.stop_decision()
        except Exception as exc:
            return {"stop": False,
                    "reasons": [f"stopping report failed: {exc}"]}
        accepted = [r for r in self.records if r.accepted]
        heldout = [r for r in accepted if r.split == "heldout"]
        cov = compute_coverage([r.as_dict() for r in accepted], self.rules)
        reasons = []
        if not stop.get("stop"):
            reasons.extend(stop.get("reasons_failed") or [])
            if not reasons:
                reasons.append("session reports useful views remain")
        if len(accepted) < int(self.rules["min_accepted"]):
            reasons.append(f"accepted {len(accepted)}/"
                           f"{self.rules['min_accepted']}")
        if len(heldout) < int(self.rules["min_heldout"]):
            reasons.append(f"held-out {len(heldout)}/"
                           f"{self.rules['min_heldout']}"
                           f" (next at accepted #{self.session._next_holdout})")
        reasons.extend(cov["missing"])
        return {"stop": not reasons, "session_stop": bool(stop.get("stop")),
                "reasons": reasons, "coverage": cov}

    # -- completion --------------------------------------------------------
    def finish(self, *, profile_root: Optional[str | Path] = None,
               serial: str = "unknown", save_profile: bool = True,
               export: bool = True, anchor_baseline: bool = False,
               free_kc: Optional[bool] = None,
               robust_loss: Optional[str] = None,
               f_scale: Optional[float] = None) -> dict:
        accepted = [r for r in self.records if r.accepted]
        heldout = [r for r in accepted if r.split == "heldout"]
        train = [r for r in accepted if r.split == "train"]

        final = self._final_solve(train, anchor_baseline=anchor_baseline,
                                  free_kc=free_kc,
                                  robust_loss=robust_loss, f_scale=f_scale)
        heldout_report = self._heldout_report(final["result"]) if final["ok"] else {}
        stopping = self._stopping_report(final, heldout_report)

        accepted_dicts = [r.as_dict() for r in accepted]
        coverage = compute_coverage(accepted_dicts, self.rules)
        colour = self._colour_summary()
        effort = self._effort_estimate(accepted_dicts)

        profile = {}
        if save_profile and final["ok"]:
            profile = self._save_profile(final, profile_root=profile_root,
                                         serial=serial, export=export)

        manifest = self._manifest_shell(accepted, heldout, train, coverage,
                                        stopping, profile, colour, effort,
                                        final)
        manifest["counts"] = self._counts(accepted, heldout, train)
        completeness = check_completeness(manifest, self.rules,
                                          session_dir=self.dir)
        manifest["completeness"] = completeness
        manifest["content_digest"] = content_digest(manifest)
        write_json(self.dir / "manifest.json", manifest)
        write_json(self.dir / "accepted.json",
                   {"accepted": [r.as_dict() for r in accepted],
                    "n": len(accepted)})
        write_json(self.dir / "heldout.json",
                   {"heldout": [r.as_dict() for r in heldout],
                    "n": len(heldout)})
        write_json(self.dir / "coverage.json", coverage)
        write_json(self.dir / "stopping.json", stopping)
        write_json(self.dir / "completeness.json", completeness)
        write_json(self.dir / "candidates_index.json",
                   {"n": len(self.records),
                    "jsonl": "candidates.jsonl",
                    "jsonl_sha256": (sha256_file(self.dir / "candidates.jsonl")
                                     if (self.dir / "candidates.jsonl").exists()
                                     else None)})
        return {"manifest": manifest, "completeness": completeness,
                "coverage": coverage, "stopping": stopping,
                "profile": profile,
                "final": {k: v for k, v in final.items()
                          if k not in ("result", "obs")},
                "heldout": heldout_report, "colour": colour,
                "effort": effort,
                "elapsed_s": round(time.time() - self.t_start, 2)}

    def _counts(self, accepted, heldout, train) -> dict:
        return {"candidates": len(self.records),
                "accepted": len(accepted),
                "rejected": len(self.records) - len(accepted),
                "train": len(train), "heldout": len(heldout),
                "accepted_with_tex": sum(1 for r in accepted
                                         if r.colour.get("tex_decoded")),
                "accepted_with_tex_markers": sum(
                    1 for r in accepted
                    if (r.colour.get("tex_markers") or 0) > 0),
                "gated_by_recorder": sum(1 for r in self.records
                                         if not r.gate.get("passed", True)),
                "session_rejected": sum(1 for r in self.records
                                        if r.gate.get("passed", True)
                                        and not r.accepted)}

    # -- final solve -------------------------------------------------------
    def _final_solve(self, train: Sequence[CandidateRecord], *,
                     anchor_baseline: bool, free_kc: Optional[bool],
                     robust_loss: Optional[str],
                     f_scale: Optional[float]) -> dict:
        if self.session.result is None or not self.session.train_obs:
            return {"ok": False, "reason": "no training observations",
                    "result": None, "info": {}}
        train_ids = {int(r.view_id) for r in train if r.view_id is not None}
        obs = [o for o in self.session.obs if int(o.view) in train_ids]
        if not obs:
            return {"ok": False, "reason": "no training observations",
                    "result": None, "info": {}}
        policy = self.session.policy
        if free_kc is None:
            free_kc = bool(len({o.view for o in obs}) >= int(policy["kc_views"]))
        robust_loss = robust_loss or policy.get("robust_loss", "huber")
        f_scale = float(policy.get("f_scale", 1.0) if f_scale is None else f_scale)
        init = {c: CameraIntrinsics(cam.fx, cam.fy, cam.cx, cam.cy, cam.kc.copy(),
                                    cam.skew)
                for c, cam in self.session.cameras.items()}
        backend = "auto"
        t0 = time.time()
        result = solve_observations(obs, self.plate.pitch_mm, init,
                                    free_kc=bool(free_kc),
                                    backend=backend, robust_loss=robust_loss,
                                    f_scale=f_scale)
        dropped: list[int] = []
        try:
            pv = per_view_errors(result, obs, self.plate.pitch_mm)
            if pv:
                med = float(np.median([d["rms_px"] for d in pv.values()]))
                bad = {v for v, d in pv.items()
                       if d["rms_px"] > max(2.0, 3.0 * med)}
                keep = [o for o in obs if int(o.view) not in bad]
                if bad and len({o.view for o in keep}) >= 8:
                    result = solve_observations(
                        keep, self.plate.pitch_mm, init, free_kc=bool(free_kc),
                        backend=backend, robust_loss=robust_loss,
                        f_scale=f_scale)
                    obs = keep
                    dropped = sorted(int(v) for v in bad)
        except Exception:
            pass
        info = {"ok": True, "solver": (result.info.get("solver")
                                       or result.info.get("backend")),
                "free_kc": bool(free_kc), "robust_loss": robust_loss,
                "f_scale": f_scale, "n_train_views": int(len({o.view for o in obs})),
                "n_train_observations": int(len(obs)),
                "dropped_train_views": dropped,
                "solve_seconds": round(time.time() - t0, 3),
                "summary": result_summary(result),
                "anchored": False}
        if anchor_baseline and self.factory_records.get("left") is not None \
                and self.factory_records.get("right") is not None:
            try:
                from calib.ccf import relative_pose
                R0, t0f = relative_pose(self.factory_records["left"],
                                        self.factory_records["right"])
                target = float(np.linalg.norm(t0f))
                before = float(np.linalg.norm(result.baseline()))
                if before > 1e-9:
                    result = result.rescaled(target_baseline=target,
                                             anchor_sigma_mm=0.05)
                    info.update({"anchored": True,
                                 "target_baseline_mm": target,
                                 "measured_baseline_before_anchor_mm": before,
                                 "scale_factor": result.info.get("scale_factor")})
                    info["summary"] = result_summary(result)
            except Exception as exc:
                info["anchor_error"] = f"{type(exc).__name__}: {exc}"
        # baseline provenance: keep the vendor 1% scale difference visible
        baseline = float(np.linalg.norm(result.baseline()))
        factory_baseline = None
        if self.factory_records.get("left") is not None and \
                self.factory_records.get("right") is not None:
            try:
                from calib.ccf import relative_pose
                _, tf = relative_pose(self.factory_records["left"],
                                      self.factory_records["right"])
                factory_baseline = float(np.linalg.norm(tf))
            except Exception:
                factory_baseline = None
        info["baseline_mm"] = baseline
        info["factory_baseline_mm"] = factory_baseline
        # orientation sanity guard.  A globally-consistent 180 deg stream
        # rotation can be absorbed by the fit (only the hardware mapping can
        # disambiguate left/right and a *common* rotation), but an upside-down
        # single stream changes the right-vs-left relative rotation by ~180 deg
        # and is caught here against the factory rig.
        if self.factory_records.get("left") is not None and \
                self.factory_records.get("right") is not None:
            try:
                from calib.ccf import relative_pose
                Rf, tf = relative_pose(self.factory_records["left"],
                                       self.factory_records["right"])
                Rl, tl = result.extrinsics["left"]
                Rr, tr = result.extrinsics["right"]
                Rt = Rr @ Rl.T
                tt = tr - Rt @ tl
                dR = _angle_deg(Rt @ Rf.T)
                dt = float(np.linalg.norm(tt - tf))
                check = {
                    "right": {"delta_rotation_deg": dR,
                              "delta_translation_mm": dt,
                              "fitted_baseline_mm": float(np.linalg.norm(tt)),
                              "factory_baseline_mm": float(np.linalg.norm(tf))},
                    "orientation_ok": bool(
                        dR <= float(self.rules["max_factory_rotation_delta_deg"])),
                    "limit_deg": float(
                        self.rules["max_factory_rotation_delta_deg"]),
                    "note": ("left/right stream order and a common 180 deg "
                             "rotation are not observable from calibration "
                             "residuals; they are fixed by the hardware "
                             "mapping in recon/src/frame_set.cpp.  This check "
                             "catches an upside-down single stream."),
                }
                if self.factory_records.get("tex") is not None:
                    Rtf, ttf = _ccf_relative_left_tex(self.factory_records)
                    Rtex = result.extrinsics["tex"][0] @ Rl.T
                    ttex = result.extrinsics["tex"][1] - Rtex @ tl
                    check["tex"] = {
                        "delta_rotation_deg": _angle_deg(Rtex @ Rtf.T),
                        "delta_translation_mm": float(np.linalg.norm(ttex - ttf)),
                        "note": "colour record may be stale; reported, not gated",
                    }
                info["factory_rig_check"] = check
            except Exception as exc:
                info["factory_rig_check"] = {"error": f"{type(exc).__name__}: {exc}"}
        info["baseline_delta_pct"] = (100.0 * (baseline / factory_baseline - 1.0)
                                      if factory_baseline else None)
        info["equivalent_pitch_mm"] = (
            self.plate.pitch_mm * factory_baseline / baseline
            if factory_baseline and baseline > 1e-9 else None)
        info["plate_pitch_mm"] = self.plate.pitch_mm
        info["pitch_source"] = self.plate.pitch_source
        info["note"] = ("baseline is at the nominal plate pitch unless "
                        "anchored; the CCF-vs-nominal scale difference is "
                        "reported, not hidden")
        return {"ok": True, "result": result, "info": info, "obs": obs}

    def _heldout_report(self, result: CalibrationResult) -> dict:
        rows = []
        for vid in sorted(self.session.heldout_view_ids):
            try:
                h = heldout_reprojection(self.session.heldout_obs, result,
                                         self.plate.pitch_mm, vid)
            except Exception as exc:
                h = {"error": f"{type(exc).__name__}: {exc}"}
            h["view_id"] = int(vid)
            rows.append(h)
        rms = [float(r["rms_px"]) for r in rows
               if isinstance(r.get("rms_px"), (int, float))
               and math.isfinite(float(r["rms_px"]))]
        per_cam: dict[str, list[float]] = {}
        for r in rows:
            for cam, v in (r.get("cameras") or {}).items():
                if isinstance(v.get("rms_px"), (int, float)):
                    per_cam.setdefault(cam, []).append(float(v["rms_px"]))
        return {"n": len(rows), "views": rows,
                "median_rms_px": float(np.median(rms)) if rms else None,
                "max_rms_px": float(np.max(rms)) if rms else None,
                "by_camera_median_rms_px": {c: float(np.median(v))
                                            for c, v in per_cam.items()},
                "note": "board pose solved from the left held-out image; all "
                        "other cameras predicted through the fitted rig"}

    def _stopping_report(self, final: dict, heldout: dict) -> dict:
        out: dict = {"session": None, "final": None,
                     "stop_recommended": False,
                     "recommended_additional_views": [],
                     "note": ("the session report is the live stopping "
                              "decision; the final report re-evaluates the "
                              "training-only fit")}
        try:
            out["session"] = self.session.stop_decision()
        except Exception as exc:
            out["session"] = {"error": f"{type(exc).__name__}: {exc}",
                              "stop": False}
        if final.get("ok"):
            try:
                out["final"] = stopping_report(
                    final["result"], final.get("obs"),
                    heldout_rms_px=heldout.get("median_rms_px"),
                    pitch=self.plate.pitch_mm,
                    min_views=int(self.session.policy["stop_min_views"]),
                    require_heldout=True, compute_stability=False,
                    require_stability=False)
            except Exception as exc:
                out["final"] = {"error": f"{type(exc).__name__}: {exc}",
                                "ok": False}
        accepted = [r.as_dict() for r in self.records if r.accepted]
        cov = compute_coverage(accepted, self.rules)
        out["recommended_additional_views"] = self._recommendations(
            cov, accepted, self.session.heldout_view_ids)
        out["stop_recommended"] = bool(
            (out.get("session") or {}).get("stop")
            and not out["recommended_additional_views"]
            and len(self.session.heldout_view_ids) >= int(
                self.rules["min_heldout"]))
        return out

    def _recommendations(self, cov: dict, accepted: Sequence[dict],
                         heldout_ids: Sequence[int]) -> list[str]:
        rec: list[str] = list(cov.get("missing") or [])
        n_acc = len(accepted)
        n_train = sum(1 for a in accepted if a.get("split") == "train")
        if n_acc < int(self.rules["min_accepted"]):
            rec.append(f"accept {int(self.rules['min_accepted']) - n_acc} more "
                       f"diverse set(s) (currently {n_acc}/"
                       f"{self.rules['min_accepted']} accepted)")
        if n_train < int(self.rules["min_train"]):
            rec.append(f"fit needs {int(self.rules['min_train']) - n_train} "
                       f"more training set(s) (currently {n_train}/"
                       f"{self.rules['min_train']}); held-out views are not fit")
        if len(heldout_ids) < int(self.rules["min_heldout"]):
            rec.append(
                f"reserve {int(self.rules['min_heldout']) - len(heldout_ids)} "
                f"more held-out set(s) (currently {len(heldout_ids)}/"
                f"{self.rules['min_heldout']}; the next reservation is at "
                f"accepted #{self.session._next_holdout})")
        # advisory coverage that is not mandatory unless --strict
        if int(self.rules.get("roll_bins_min", 0)) == 0 and \
                cov.get("roll_bins_occupied", 0) < 2:
            rec.append("roll variety: all accepted views are near the same "
                       "in-plane rotation; rotate the plate ~30-45 deg for "
                       "one or two views (advisory unless --strict)")
        if not rec:
            return rec
        # add the session's own next target when it has one
        try:
            g = self.session.recommend(top=1)
            if g.get("ready") and g.get("text"):
                rec.append("session next target: " + str(g["text"]))
        except Exception:
            pass
        return rec

    # -- colour / effort ---------------------------------------------------
    def _colour_summary(self) -> dict:
        accepted = [r for r in self.records if r.accepted]
        frames_present = sum(1 for r in self.records
                             if r.colour.get("tex_present"))
        decoded = sum(1 for r in self.records if r.colour.get("tex_decoded"))
        with_markers = [r for r in accepted
                        if (r.colour.get("tex_markers") or 0) > 0]
        participation = (len(with_markers) / len(accepted)) if accepted else 0.0
        state = self.colour_profile_state
        factory = None
        if state.get("state") == "present" and self.factory_records.get("tex") \
                is not None and self.session.result is not None:
            try:
                Rf, tf = _ccf_relative_left_tex(self.factory_records)
                res = self.session.result
                Re = res.extrinsics["tex"][0] @ res.extrinsics["left"][0].T
                te = res.extrinsics["tex"][1] - Re @ res.extrinsics["left"][1]
                factory = ccf_consistency(
                    self.factory_records["left"], self.factory_records["tex"],
                    expected=(Re, te), tau_translation_mm=3.0,
                    tau_rotation_deg=0.3)
            except Exception as exc:
                factory = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "profile": dict(state),
            "profile_state": state.get("state"),
            "frames_present": int(frames_present),
            "frames_decoded": int(decoded),
            "accepted": len(accepted),
            "accepted_with_tex_markers": len(with_markers),
            "participation_frac": float(participation),
            "hasTexture": bool(accepted and decoded == len(self.records)
                               and participation >= float(
                                   self.rules["tex_participation_min"])),
            "hasTexture_note": ("hasTexture means: colour frames were present "
                                "and decodable for every candidate, the colour "
                                "camera saw the plate in the accepted views, "
                                "and the frames were retained.  It is not a "
                                "claim that the colour profile is accurate."),
            "factory_consistency": factory,
            "fit_includes_tex": bool(any(
                o.camera == "tex" for o in self.session.train_obs)),
        }

    def _effort_estimate(self, accepted: Sequence[dict]) -> dict:
        prev = None
        total = 0.0
        n_moves = 0
        moves = []
        for rec in accepted:
            pose = rec.get("pose")
            if not pose:
                continue
            R = np.asarray(pose["R"], float)
            t = np.asarray(pose["t"], float)
            if prev is not None:
                try:
                    ins = guidance.instruction(prev, (R, t))
                    total += float(ins["effort"])
                    n_moves += 1
                    moves.append({"seq": rec["seq"], "label": rec["label"],
                                  "text": ins["text"],
                                  "effort": float(ins["effort"])})
                except Exception:
                    pass
            prev = (R, t)
        sec = 6.0 * total + 1.5 * max(len(accepted), 1)
        return {"n_views": len(accepted), "n_moves": n_moves,
                "total_effort_units": round(total, 3),
                "mean_effort_per_view": round(total / n_moves, 3)
                if n_moves else None,
                "estimated_seconds": round(sec, 1),
                "model": ("INFERRED: effort = 0.15 + lateral/150 mm + "
                          "depth/150 mm + angle/25 deg (calib.guidance), "
                          "wall time = 6 s/unit + 1.5 s/view; not measured "
                          "human data"),
                "moves": moves}

    # -- profiles ----------------------------------------------------------
    def _save_profile(self, final: dict, *, profile_root, serial: str,
                      export: bool) -> dict:
        result = final["result"]
        info = final["info"]
        root = Path(profile_root) if profile_root else (self.dir / "profiles")
        root.mkdir(parents=True, exist_ok=True)
        out: dict = {"profile_root": str(root), "serial": serial,
                     "activated": False, "saved": False,
                     "factory_snapshot": None, "revision": None,
                     "export": None, "result_json": None}
        # raw solve result (export_profile.py --result consumes this shape)
        result_json = root / "result.json"
        write_json(result_json, {"result": result.as_dict(),
                                 "info": info,
                                 "heldout": None})
        out["result_json"] = str(result_json)
        store = ProfileStore(root)
        prior = None
        try:
            prior = store.active(serial)
        except Exception:
            prior = None
        try:
            if self.factory_records:
                files = {}
                for cam in CAMERAS:
                    src = (self.factory_sources.get(cam))
                    if src:
                        files[cam] = Path(src)
                if files:
                    try:
                        snap = store.snapshot_factory(serial, files)
                        out["factory_snapshot"] = str(snap)
                    except FileExistsError:
                        out["factory_snapshot"] = str(
                            store.device_dir(serial) / "factory")
                        out["factory_snapshot_note"] = "existing snapshot kept"
            prof = profile_from_result(result,
                                       factory=self.factory_records or None)
            rev = store.save_revision(
                serial, prof,
                meta={"created_by": TOOL_NAME, "mode": self.mode,
                      "n_views": info["n_train_views"],
                      "n_heldout": len(self.session.heldout_view_ids),
                      "pitch_mm": self.plate.pitch_mm,
                      "pitch_source": self.plate.pitch_source,
                      "source_kind": self.source_kind,
                      "content_digest": None,
                      "note": "host-side only; factory snapshot immutable; "
                              "revision deliberately not activated"},
                covariance=result.covariance,
                param_names=result.info.get("param_names"))
            out.update({"saved": True, "revision": int(rev.index),
                        "revision_path": str(rev.root),
                        "verified": bool(rev.verify())})
            # never auto-activate the new revision
            try:
                if prior is not None:
                    store.activate(serial, prior.index)
                else:
                    active = store.device_dir(serial) / "active"
                    if active.exists():
                        active.unlink()
            except Exception as exc:
                out["activation_error"] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        if export and out.get("saved"):
            exp = self._export_profile(result_json, root / "exported",
                                       result, info)
            out["export"] = exp
        out["result_json_sha256"] = (sha256_file(result_json)
                                     if result_json.exists() else None)
        (self.dir / "profiles").mkdir(parents=True, exist_ok=True)
        write_json(self.dir / "profiles" / "profile_manifest.json", out)
        return out

    def _export_profile(self, result_json: Path, out_dir: Path,
                        result: CalibrationResult, info: dict) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "reconstruction_profile.json"
        script = REPO_ROOT / "tools" / "calib" / "export_profile.py"
        cmd = [sys.executable, str(script), "--result", str(result_json),
               "--out", str(out_path)]
        res: dict = {"ok": False, "command": " ".join(cmd),
                     "out": str(out_path)}
        if not script.is_file():
            res["error"] = f"export script not found: {script}"
            return res
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=180, cwd=str(REPO_ROOT))
            res["returncode"] = int(proc.returncode)
            res["stdout"] = proc.stdout.strip()[-2000:]
            res["stderr"] = proc.stderr.strip()[-2000:]
            if proc.returncode != 0 or not out_path.is_file():
                res["error"] = "export_profile.py failed"
                return res
            prof = read_json(out_path)
            w, h = _image_size_from_records(self.records)
            prof["image_width"] = int(w)
            prof["image_height"] = int(h)
            prof["plate_pitch_mm"] = float(self.plate.pitch_mm)
            prov = list(prof.get("provenance") or [])
            prov.append(f"calibsession_pitch_source={self.plate.pitch_source}")
            prov.append(f"calibsession_baseline_mm={info.get('baseline_mm')}")
            prov.append(f"calibsession_factory_baseline_mm="
                        f"{info.get('factory_baseline_mm')}")
            prof["provenance"] = prov
            write_json(out_path, prof)
            res.update({"ok": True,
                        "sha256": sha256_file(out_path),
                        "bytes": out_path.stat().st_size})
        except Exception as exc:
            res["error"] = f"{type(exc).__name__}: {exc}"
        return res

    # -- manifest ----------------------------------------------------------
    def _manifest_shell(self, accepted, heldout, train, coverage, stopping,
                        profile, colour, effort, final) -> dict:
        return {
            "schema": SCHEMA,
            "created_utc": self.started_utc,
            "tool": TOOL_NAME,
            "config": {
                "source_kind": self.source_kind,
                "mode": self.mode,
                "target": self.target,
                "dwell_mode": self.dwell_mode,
                "localize_method": self.localize_method,
                "plate": self.plate.as_dict(),
                "holdout": self.session.holdout_policy.as_dict(),
                "factory_dir": self.factory_dir,
                "factory_source": self.factory_source,
                "tex_profile": self.tex_profile,
            },
            "source": self.source_info.as_dict(),
            "detection_source": ("replay-manifest" if any(
                (r.detection.get("left") or {}).get("source") == "manifest"
                for r in self.records) else "image"),
            "counts": self._counts(accepted, heldout, train),
            "accepted_ids": [int(r.view_id) for r in accepted
                             if r.view_id is not None],
            "heldout_ids": sorted(int(v) for v in self.session.heldout_view_ids),
            "train_ids": [int(r.view_id) for r in train
                          if r.view_id is not None],
            "sets": [r.as_dict() for r in self.records],
            "coverage": coverage,
            "stopping": stopping,
            "colour": colour,
            "effort": effort,
            "profiles": profile,
            "final_solve": {k: v for k, v in final.items()
                            if k not in ("result", "obs")},
            "rules": dict(self.rules),
            "timing": {"detect_s": round(float(self.session.detect_seconds), 3),
                       "refine_s": round(float(self.session.refine_seconds), 3),
                       "solve_s": round(float(self.session.solve_seconds), 3),
                       "n_solves": int(self.session._solves),
                       "wall_s": round(time.time() - self.t_start, 2)},
            "notes": [
                "frames are byte-exact copies; source images are never modified",
                "held-out views are excluded from every training solve",
                "the host profile revision is not activated and the factory "
                "snapshot is immutable",
                "no hardware was contacted and nothing was written to the scanner",
            ],
        }


# ---------------------------------------------------------------------------
# helper functions used by the recorder
# ---------------------------------------------------------------------------

def _ccf_relative_left_tex(records: Mapping[str, CCFRecord]):
    from calib.ccf import relative_pose
    return relative_pose(records["left"], records["tex"])


def _image_size_from_records(records: Sequence[CandidateRecord]) -> tuple[int, int]:
    for r in records:
        for cam in CAMERAS:
            f = r.files.get(cam) or {}
            if f.get("width") and f.get("height"):
                return int(f["width"]), int(f["height"])
    return 1280, 1024


def _safe(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("._")
    return (s or "set")[:80]


# ---------------------------------------------------------------------------
# completeness / verification
# ---------------------------------------------------------------------------

def check_completeness(manifest: Mapping[str, Any],
                       rules: Optional[dict] = None,
                       session_dir: Optional[str | Path] = None) -> dict:
    """Decide whether a session may be ended.  Returns every failure by name."""
    r = {**DEFAULT_RULES, **(rules or {})}
    sets = list(manifest.get("sets") or [])
    accepted = [s for s in sets if s.get("accepted")]
    heldout = [s for s in accepted if s.get("split") == "heldout"]
    train = [s for s in accepted if s.get("split") == "train"]
    counts = manifest.get("counts") or {
        "accepted": len(accepted), "train": len(train),
        "heldout": len(heldout)}
    checks: list[dict] = []
    missing: list[str] = []

    def add(name: str, ok: bool, value: Any, detail: str = ""):
        checks.append({"name": name, "pass": bool(ok), "value": value,
                       "detail": detail})
        if not ok:
            missing.append(detail or name)

    add("accepted_sets", len(accepted) >= int(r["min_accepted"]),
        len(accepted),
        f"accepted {len(accepted)} sets < required {r['min_accepted']}")
    add("training_sets", len(train) >= int(r["min_train"]),
        len(train),
        f"training {len(train)} sets < required {r['min_train']}")
    add("heldout_sets", len(heldout) >= int(r["min_heldout"]),
        len(heldout),
        f"held-out {len(heldout)} sets < required {r['min_heldout']}")

    # cameras / files on every accepted set
    cam_problems = []
    for s in accepted:
        for cam in CAMERAS:
            f = (s.get("files") or {}).get(cam) or {}
            if not f.get("decoded") or not f.get("stored"):
                cam_problems.append(
                    f"{s.get('label')}: camera {cam} missing/undecodable "
                    f"({f.get('error') or 'no file'})")
    add("all_three_cameras", (not cam_problems) or (not r["require_all_cameras"]),
        len(cam_problems),
        "; ".join(cam_problems[:4]) if cam_problems else "")
    # a systematic colour loss must fail even if every accepted set happened to
    # be rejected: count the candidate coverage, not just the accepted one
    all_sets = list(sets)
    tex_candidates = sum(
        1 for s in all_sets
        if ((s.get("files") or {}).get("tex") or {}).get("decoded"))
    frac_cand = (tex_candidates / len(all_sets)) if all_sets else 0.0
    add("colour_candidate_coverage",
        frac_cand >= float(r["tex_candidate_fraction_min"]),
        round(frac_cand, 3),
        f"colour camera decodable in {tex_candidates}/{len(all_sets)} candidate "
        f"sets (need {r['tex_candidate_fraction_min']:.0%}); hasTexture="
        f"{bool(manifest.get('colour', {}).get('hasTexture'))}")

    # per-camera image size consistency
    size_problems = []
    for cam in CAMERAS:
        sizes = {(int(f["width"]), int(f["height"]))
                 for s in accepted
                 for f in [(s.get("files") or {}).get(cam) or {}]
                 if f.get("width") and f.get("height")}
        if len(sizes) > 1:
            size_problems.append(f"{cam}: inconsistent sizes {sorted(sizes)}")
    cross = {}
    for cam in CAMERAS:
        for s in accepted:
            f = (s.get("files") or {}).get(cam) or {}
            if f.get("width"):
                cross.setdefault(cam, (int(f["width"]), int(f["height"])))
    add("consistent_image_size", not size_problems,
        {c: list(v) for c, v in cross.items()},
        "; ".join(size_problems))

    # byte integrity
    if session_dir is not None:
        bad = []
        base = Path(session_dir)
        for s in sets:
            for cam, f in (s.get("files") or {}).items():
                stored = f.get("stored")
                if not stored:
                    continue
                p = base / stored
                if not p.is_file():
                    bad.append(f"{stored}: missing")
                    continue
                try:
                    if sha256_file(p) != f.get("sha256"):
                        bad.append(f"{stored}: sha256 mismatch")
                        continue
                    raw = p.read_bytes()
                    decode_image_bytes(raw, p.suffix)
                except Exception as exc:
                    bad.append(f"{stored}: {exc}")
        add("files_intact", not bad, len(bad),
            "; ".join(bad[:4]) if bad else "")

    # colour participation
    tex_ok = sum(1 for s in accepted
                 if ((s.get("files") or {}).get("tex") or {}).get("decoded"))
    frac = (tex_ok / len(accepted)) if accepted else 0.0
    add("colour_frames", tex_ok == len(accepted) and len(accepted) > 0, tex_ok,
        f"colour camera decodable in {tex_ok}/{len(accepted)} accepted sets")
    tex_markers = sum(1 for s in accepted
                      if ((s.get("colour") or {}).get("tex_markers") or 0) > 0)
    mfrac = (tex_markers / len(accepted)) if accepted else 0.0
    add("colour_participation", mfrac >= float(r["tex_participation_min"]),
        round(mfrac, 3),
        f"colour camera detected the plate in {tex_markers}/{len(accepted)} "
        f"accepted sets (need {r['tex_participation_min']:.0%}); hasTexture="
        f"{bool(manifest.get('colour', {}).get('hasTexture'))}")

    # sync / dwell
    sync_bad = [s.get("label") for s in accepted
                if ((s.get("sync") or {}).get("state") not in
                    ("measured", "declared"))]
    add("sync_evidence", (not sync_bad) or (not r["require_sync_evidence"]),
        {"unknown": len(sync_bad)},
        f"no synchronisation evidence for {len(sync_bad)} accepted sets: "
        f"{sync_bad[:3]}" if sync_bad else "")
    dwell_bad = [s.get("label") for s in accepted
                 if (s.get("dwell_state") not in
                     ("assumed-keyframe", "stationary", "first-frame"))]
    add("dwell", (not dwell_bad) or (not r["require_dwell"]),
        {"moving": len(dwell_bad)},
        f"accepted while moving: {dwell_bad[:3]}" if dwell_bad else "")

    # stereo gate on accepted sets
    gate_bad = [s.get("label") for s in accepted
                if not ((s.get("recorder_gate") or {}).get("passed", True))]
    add("stereo_gate", not gate_bad, len(gate_bad),
        f"accepted with a failed stereo gate: {gate_bad[:3]}" if gate_bad else "")
    # marker-quality minima (the session quality gate normally enforces these;
    # re-checked here so a relaxed custom policy cannot slip through)
    weak = []
    for s in accepted:
        m = (s.get("quality") or {}).get("metrics") or {}
        if int(m.get("stereo_markers", 0) or 0) < int(r["min_stereo_markers"]):
            weak.append(f"{s.get('label')}: stereo markers "
                        f"{m.get('stereo_markers')} < {r['min_stereo_markers']}")
        if int(m.get("big_markers", 0) or 0) < int(r["min_big_markers"]):
            weak.append(f"{s.get('label')}: large markers "
                        f"{m.get('big_markers')} < {r['min_big_markers']}")
    add("marker_quality", not weak, len(weak),
        "; ".join(weak[:4]) if weak else "")

    # coverage (already computed in the manifest; recompute defensively)
    cov = manifest.get("coverage") or compute_coverage(accepted, r)
    for m in (cov.get("missing") or coverage_missing(cov, r)):
        add("coverage", False, m, m)
    if not (cov.get("missing") or coverage_missing(cov, r)):
        add("coverage", True, {"distance_bins": cov.get("distance_bins_occupied"),
                               "roll_bins": cov.get("roll_bins_occupied")}, "")

    # profile
    prof = manifest.get("profiles") or {}
    if r["require_profile"]:
        add("profile_saved", bool(prof.get("saved")),
            {"saved": prof.get("saved"), "revision": prof.get("revision")},
            "no host-side profile revision was saved")
        add("profile_not_activated", prof.get("activated") is not True,
            {"activated": prof.get("activated")},
            "the new revision must not be activated automatically")
    frc = ((manifest.get("final_solve") or {}).get("info") or {}).get(
        "factory_rig_check") or {}
    if frc and "orientation_ok" in frc and r["require_factory_orientation"]:
        add("factory_orientation", bool(frc["orientation_ok"]),
            {"delta_rotation_deg": (frc.get("right") or {}).get(
                "delta_rotation_deg")},
            "camera orientation inconsistent with the factory rig "
            f"(left-right rotation delta "
            f"{(frc.get('right') or {}).get('delta_rotation_deg')} deg > "
            f"{frc.get('limit_deg')} deg); a stream may be upside down")
    if prof.get("export"):
        exp = prof["export"]
        add("profile_export", bool(exp.get("ok")) or not r["require_export"],
            {"ok": exp.get("ok"), "out": exp.get("out")},
            f"reconstruction profile export failed: {exp.get('error')}")

    ok = all(c["pass"] for c in checks)
    return {"ok": bool(ok), "checks": checks, "missing": missing,
            "n_checks": len(checks), "rules": r}


def _round_floats(obj: Any, nd: int = 2) -> Any:
    """Canonicalise floats so solver round-off (1e-9) cannot change a digest."""
    if isinstance(obj, dict):
        return {str(k): _round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, nd) for v in obj]
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return (round(v, nd) + 0.0) if math.isfinite(v) else None
    if isinstance(obj, np.ndarray):
        return _round_floats(obj.tolist(), nd)
    if isinstance(obj, (np.integer, int)) and not isinstance(obj, bool):
        return int(obj)
    return _jsonable(obj)


def content_digest(manifest: Mapping[str, Any]) -> str:
    """Deterministic digest of the decision content (excludes wall clock).

    Floats are rounded to 1e-2 so that solver round-off (the LM may take one
    extra iteration) cannot make two identical captures look different; the
    decisions, reasons, labels, counts and file SHA-256 values are exact.
    """
    keep = {k: manifest.get(k) for k in
            ("schema", "config", "source", "counts", "accepted_ids",
             "heldout_ids", "train_ids", "sets", "coverage")}
    # A cached detection is the same decision as a freshly computed one; the
    # provenance of the detector result must not change the session digest.
    sets = []
    for item in keep.get("sets") or []:
        item = dict(item)
        item.pop("detection_source", None)
        det = item.get("detection")
        if isinstance(det, dict):
            item["detection"] = {
                c: ({k: v for k, v in cam.items() if k != "source"}
                    if isinstance(cam, dict) else cam)
                for c, cam in det.items()}
        sets.append(item)
    keep["sets"] = sets
    blob = json.dumps(_round_floats(keep), sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return sha256_bytes(blob.encode())


def verify_session(session_dir: str | Path) -> dict:
    """Re-hash and re-decode every retained file; verify the session is complete."""
    d = Path(session_dir)
    man_path = d / "manifest.json"
    problems: list[str] = []
    if not man_path.is_file():
        return {"ok": False, "session": str(d),
                "problems": ["manifest.json missing (session not finished)"],
                "checks": []}
    try:
        man = read_json(man_path)
    except Exception as exc:
        return {"ok": False, "session": str(d),
                "problems": [f"manifest.json unreadable: {exc}"], "checks": []}
    if man.get("schema") != SCHEMA:
        problems.append(f"unexpected schema {man.get('schema')!r}")
    # digest recomputation
    try:
        if content_digest(man) != man.get("content_digest"):
            problems.append("content_digest mismatch (manifest content changed)")
    except Exception as exc:
        problems.append(f"content_digest could not be recomputed: {exc}")
    # per-file hashes + decode
    n_files = 0
    for s in man.get("sets") or []:
        for cam, f in (s.get("files") or {}).items():
            stored = f.get("stored")
            if not stored:
                if s.get("accepted"):
                    problems.append(
                        f"{s.get('label')}: accepted set lacks {cam} file")
                continue
            p = d / stored
            n_files += 1
            if not p.is_file():
                problems.append(f"{stored}: missing")
                continue
            size = p.stat().st_size
            if f.get("bytes") is not None and size != int(f["bytes"]):
                problems.append(f"{stored}: size {size} != {f['bytes']}")
            digest = sha256_file(p)
            if digest != f.get("sha256"):
                problems.append(f"{stored}: sha256 mismatch")
                continue
            try:
                _, facts = decode_image_bytes(p.read_bytes(), p.suffix)
                if f.get("width") and (facts["width"], facts["height"]) != (
                        int(f["width"]), int(f["height"])):
                    problems.append(
                        f"{stored}: decoded size {facts['width']}x"
                        f"{facts['height']} != manifest "
                        f"{f['width']}x{f['height']}")
            except Exception as exc:
                problems.append(f"{stored}: decode failed: {exc}")
    # candidate log consistency
    jsonl = d / "candidates.jsonl"
    if jsonl.is_file():
        lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
        if len(lines) != len(man.get("sets") or []):
            problems.append(
                f"candidates.jsonl has {len(lines)} entries for "
                f"{len(man.get('sets') or [])} manifest sets")
        else:
            for i, ln in enumerate(lines):
                try:
                    rec = json.loads(ln)
                except Exception as exc:
                    problems.append(f"candidates.jsonl line {i+1}: {exc}")
                    break
                if rec.get("seq") != i:
                    problems.append(f"candidates.jsonl line {i+1}: seq "
                                    f"{rec.get('seq')} != {i}")
                    break
    else:
        problems.append("candidates.jsonl missing")
    # profile revision integrity
    prof = man.get("profiles") or {}
    if prof.get("saved"):
        root = prof.get("profile_root")
        serial = prof.get("serial", "unknown")
        idx = prof.get("revision")
        if root and idx is not None:
            try:
                revs = {r.index: r for r in ProfileStore(root).revisions(serial)}
                rev = revs.get(int(idx))
                if rev is None:
                    problems.append(f"profile revision {idx} not found")
                elif not rev.verify():
                    problems.append(f"profile revision {idx} hash mismatch")
            except Exception as exc:
                problems.append(f"profile revision check failed: {exc}")
        if prof.get("activated") is True:
            problems.append("new profile revision is activated (must not be)")
    # completeness (with a fresh file check)
    comp = check_completeness(man, man.get("rules"), session_dir=d)
    if not comp["ok"]:
        problems.extend(f"incomplete: {m}" for m in comp["missing"])
    return {"ok": not problems, "session": str(d),
            "n_files": n_files, "problems": problems,
            "checks": comp["checks"],
            "n_sets": len(man.get("sets") or []),
            "counts": man.get("counts"),
            "content_digest": man.get("content_digest"),
            "completeness": comp}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_size(text: Optional[str]) -> Optional[tuple[int, int]]:
    if not text:
        return None
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(text))
    if not m:
        raise CalibSessionError(f"--image-size: expected WxH, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def _summary_text(report: dict) -> str:
    man = report["manifest"]
    counts = man.get("counts") or {}
    cov = man.get("coverage") or {}
    comp = man.get("completeness") or {}
    prof = man.get("profiles") or {}
    colour = man.get("colour") or {}
    stop = man.get("stopping") or {}
    fin = man.get("final_solve") or {}
    lines = []
    lines.append(f"session: {report.get('session_dir', '')}")
    lines.append(
        f"candidates {counts.get('candidates', 0)} | accepted "
        f"{counts.get('accepted', 0)} (train {counts.get('train', 0)}, "
        f"held-out {counts.get('heldout', 0)}) | rejected "
        f"{counts.get('rejected', 0)}")
    lines.append(
        f"colour: frames present {colour.get('frames_present', 0)}/decoded "
        f"{colour.get('frames_decoded', 0)}; plate seen in "
        f"{colour.get('accepted_with_tex_markers', 0)}/"
        f"{colour.get('accepted', 0)} accepted; hasTexture="
        f"{colour.get('hasTexture')}; profile={colour.get('profile_state')}")
    if fin.get("ok"):
        info = fin.get("info") or {}
        lines.append(
            f"geometry: {info.get('n_train_views')} views, "
            f"{_fmt((fin.get('info') or {}).get('baseline_mm'))} mm baseline, "
            f"pitch {info.get('plate_pitch_mm')} mm "
            f"(CCF delta {_fmt(info.get('baseline_delta_pct'), 3)} %, "
            f"equivalent pitch {_fmt(info.get('equivalent_pitch_mm'), 3)} mm)")
    frc = (fin.get("info") or {}).get("factory_rig_check") or {}
    if frc.get("orientation_ok") is not None:
        lines.append(
            f"factory orientation: {'ok' if frc.get('orientation_ok') else 'FAIL'} "
            f"(left-right dR={_fmt((frc.get('right') or {}).get('delta_rotation_deg'), 2)} deg)")
    ho = report.get("heldout") or {}
    if ho.get("n"):
        lines.append(
            f"held-out: {ho['n']} views, median "
            f"{_fmt(ho.get('median_rms_px'), 3)} px, max "
            f"{_fmt(ho.get('max_rms_px'), 3)} px")
    lines.append(
        f"coverage: distance bins {cov.get('distance_bins_occupied')}/"
        f"{len(cov.get('distance_counts') or [])}, tilt max "
        f"{_fmt(cov.get('tilt_deg_max'), 1)} deg, tilt signs x"
        f"{cov.get('tilt_x_signs')} y{cov.get('tilt_y_signs')}, roll bins "
        f"{cov.get('roll_bins_occupied')}, quadrants "
        f"{ {k: round(v, 2) for k, v in (cov.get('image_quadrant_fraction') or {}).items()} }")
    ss = stop.get("session") or {}
    lines.append(
        f"stopping: session_stop={ss.get('stop')} "
        f"(n={ss.get('n_views')}); final_ok="
        f"{(stop.get('final') or {}).get('ok')}")
    if stop.get("recommended_additional_views"):
        lines.append("recommended additional views:")
        for rec in stop["recommended_additional_views"]:
            lines.append(f"  - {rec}")
    else:
        lines.append("recommended additional views: none")
    if prof.get("saved"):
        lines.append(f"profile: revision {prof.get('revision')} (not activated) "
                     f"at {prof.get('profile_root')}")
    else:
        lines.append(f"profile: not saved ({prof.get('error') or 'disabled'})")
    exp = prof.get("export") or {}
    lines.append(f"export: {'ok' if exp.get('ok') else 'not available'}"
                 + (f" -> {exp.get('out')}" if exp.get("out") else ""))
    lines.append(f"completeness: {'OK' if comp.get('ok') else 'INCOMPLETE'}")
    for m in comp.get("missing") or []:
        lines.append(f"  MISSING: {m}")
    lines.append(f"elapsed: {report.get('elapsed_s')} s")
    return "\n".join(lines)


def _fmt(v, nd: int = 2) -> str:
    try:
        return "n/a" if v is None else f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _run_verify(args) -> int:
    res = verify_session(args.verify)
    if args.json:
        print(json.dumps(_jsonable(res), indent=2, sort_keys=True))
    else:
        print(f"verify {res['session']}: {'OK' if res['ok'] else 'FAILED'}")
        print(f"  files checked: {res.get('n_files', 0)}; sets: "
              f"{res.get('n_sets', 0)}; digest: {res.get('content_digest')}")
        for p in res.get("problems") or []:
            print(f"  PROBLEM: {p}")
    return 0 if res["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=("Guided one-session calibration recorder for the Einstar "
                     "41-marker plate (host-side; no hardware)."),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--frames", type=Path,
                     help="directory of frame_%%06d_cam{0,1,2} triplets")
    src.add_argument("--vendor-corpus", type=Path,
                     help="directory of imageLeft/imageRight/imageTex N files")
    src.add_argument("--replay", type=Path,
                     help="JSON manifest of explicit frame sets")
    ap.add_argument("--verify", type=Path, metavar="SESSION_DIR",
                    help="re-hash and verify a finished session, then exit")
    ap.add_argument("--out", type=Path, help="session output directory")
    ap.add_argument("--plate-profile", default=None,
                    metavar="cols=8,rows=5,pitch=28.0",
                    help="plate geometry (default: calib.plate)")
    ap.add_argument("--mode", choices=("quick", "standard", "high"),
                    default="standard")
    ap.add_argument("--target", type=int, default=24,
                    help="planning target for accepted views (20-40 typical)")
    ap.add_argument("--min-accepted", type=int, default=None,
                    help="completeness minimum accepted sets (default 15)")
    ap.add_argument("--min-train", type=int, default=None)
    ap.add_argument("--min-heldout", type=int, default=None)
    ap.add_argument("--strict", action="store_true",
                    help="stricter coverage for a production-quality session")
    ap.add_argument("--cam-map",
                    default=None,
                    metavar="0=right,1=left,2=tex",
                    help="probe stream -> camera mapping (default: the proven "
                         "hardware order; vendor/replay inputs are unaffected)")
    ap.add_argument("--frames-rotation", choices=("probe", "as-calibrated"),
                    default="probe",
                    help="probe = rotate cam0(right)/cam2(tex) 180 deg back to "
                         "calibration orientation; as-calibrated = the files "
                         "are already oriented")
    ap.add_argument("--localize", default="auto",
                    choices=("auto", "conic_perspective", "conic", "centroid",
                             "centroid_bg", "edge", "none"),
                    help="sub-pixel refinement of committed sets")
    ap.add_argument("--dwell", choices=("auto", "assume", "sequential"),
                    default="auto",
                    help="assume = each input set is a committed keyframe; "
                         "sequential = live motion gate between sets")
    ap.add_argument("--factory-dir", type=Path,
                    help="directory with LeftCCF.txt/RightCCF.txt/TexCCF.txt "
                         "(default: the repo's shipped factory CCFs, if present)")
    ap.add_argument("--no-factory", action="store_true",
                    help="do not use any factory CCF, even the shipped one")
    ap.add_argument("--tex-profile", type=Path,
                    help="explicit colour CCF (overrides the factory TexCCF)")
    ap.add_argument("--serial", default="unknown",
                    help="profile-store serial directory")
    ap.add_argument("--profile-root", type=Path, default=None,
                    help="ProfileStore root (default <out>/profiles)")
    ap.add_argument("--no-profile", action="store_true",
                    help="skip the ProfileStore revision")
    ap.add_argument("--no-export", action="store_true",
                    help="skip tools/calib/export_profile.py")
    ap.add_argument("--anchor-baseline", action="store_true",
                    help="rescale to the factory baseline (OFF by default: "
                         "keeps the 28.0 vs 27.72 mm scale difference visible)")
    ap.add_argument("--force", action="store_true",
                    help="allow writing into a non-empty session directory")
    ap.add_argument("--max-sets", type=int, default=None,
                    help="process at most N input sets")
    ap.add_argument("--stop-when-ready", action="store_true",
                    help="stop early when the session and coverage minima agree")
    ap.add_argument("--solve-every", type=int, default=1)
    ap.add_argument("--cache", type=Path, default=None,
                    help="optional detection cache JSON")
    ap.add_argument("--link", action="store_true",
                    help="hard-link copies instead of copying (same filesystem; "
                         "the session then shares inodes with the source)")
    ap.add_argument("--image-size", default=None, metavar="WxH",
                    help="initial image size for default intrinsics")
    ap.add_argument("--json", action="store_true",
                    help="print the machine-readable summary")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.verify is not None:
        return _run_verify(args)
    if args.out is None:
        ap.error("--out is required (or use --verify SESSION_DIR)")
    if not (args.frames or args.vendor_corpus or args.replay):
        ap.error("one of --frames/--vendor-corpus/--replay is required")
    try:
        profile = PlateProfile.parse(args.plate_profile)
        rules = dict(DEFAULT_RULES)
        if args.strict:
            rules.update(STRICT_RULES)
        if args.min_accepted is not None:
            rules["min_accepted"] = int(args.min_accepted)
        if args.min_train is not None:
            rules["min_train"] = int(args.min_train)
        if args.min_heldout is not None:
            rules["min_heldout"] = int(args.min_heldout)
        if args.no_profile:
            rules["require_profile"] = False
        if not (12 <= int(args.target) <= 60):
            raise CalibSessionError("--target must be between 12 and 60")
        holdout = HoldoutPolicy(n_holdout=int(rules["min_heldout"]),
                                target=int(args.target))
        if args.frames:
            sets, info = discover_frames(args.frames, cam_map=args.cam_map)
            if args.frames_rotation == "as-calibrated":
                for item in sets:
                    item.rotate_180 = {}
                info.extra["rotation_180"] = []
                info.extra["orientation_source"] = (
                    "explicitly disabled (--frames-rotation as-calibrated)")
        elif args.vendor_corpus:
            sets, info = discover_vendor_corpus(args.vendor_corpus)
        else:
            sets, info = load_replay(args.replay)
        if args.max_sets is not None:
            sets = sets[:int(args.max_sets)]
            info.n_sets = len(sets)
        factory_dir = args.factory_dir
        factory_source = "explicit" if factory_dir is not None else "none"
        if factory_dir is None and not args.no_factory:
            candidate = (REPO_ROOT / "sources" / "calibration"
                         / "res_Einscan-E10_200x150")
            if all((candidate / n).is_file() for n in
                   ("LeftCCF.txt", "RightCCF.txt", "TexCCF.txt")):
                factory_dir = candidate
                factory_source = "auto"
        for cam, n_missing in (info.missing_by_camera or {}).items():
            if n_missing:
                print(f"warning: {cam} image missing in {n_missing}/"
                      f"{info.n_sets} input set(s); those candidates will be "
                      f"recorded and rejected", file=sys.stderr)
        dwell = args.dwell
        if dwell == "auto":
            dwell = "sequential" if info.kind == "frames" else "assume"
        recorder = SessionRecorder(
            args.out, source_kind=info.kind, source_info=info,
            plate_profile=profile, mode=args.mode, target=args.target,
            rules=rules, holdout=holdout,
            localize_method=(None if args.localize == "none" else args.localize),
            dwell_mode=dwell, factory_dir=factory_dir,
            factory_source=factory_source, tex_profile=args.tex_profile,
            solve_every=args.solve_every,
            seed=args.seed, link=args.link, force=args.force,
            cache=args.cache, image_size=_parse_size(args.image_size),
            verbose=not args.quiet)
        t0 = time.time()
        seen = 0
        for item in sets:
            rec = recorder.process(item)
            seen += 1
            if args.stop_when_ready and rec.accepted:
                st = recorder.should_stop()
                if st["stop"]:
                    if not args.quiet:
                        print(f"stopping after {seen} input sets: session and "
                              f"coverage minima agree")
                    break
        total_sets = len(sets)
        report = recorder.finish(
            profile_root=args.profile_root, serial=args.serial,
            save_profile=not args.no_profile, export=not args.no_export,
            anchor_baseline=args.anchor_baseline)
        report["session_dir"] = str(args.out)
        report["elapsed_s"] = round(time.time() - t0, 2)
        if args.json:
            print(json.dumps(_jsonable(report), indent=2, sort_keys=True))
        else:
            print(_summary_text(report))
        return 0 if report["completeness"]["ok"] else 2
    except CalibSessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"unexpected error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        if os.environ.get("CALIBSESSION_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
