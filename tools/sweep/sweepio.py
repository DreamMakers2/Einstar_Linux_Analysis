"""I/O for a captured Einstar sweep: frames, manifest, factory profile.

The capture is treated as **read-only**; nothing in this package writes into the
capture directory.

Stream -> calibration-camera mapping is the project convention
(``recon/include/einstar/recon/frame_set.hpp``)::

    stream 0 = right IR, rotated 180 deg relative to the calibration frame
    stream 1 = left  IR, already in calibration orientation
    stream 2 = colour, raw Bayer mosaic, rotated 180 deg

``calibration_image`` applies that mapping exactly once, so every consumer of
this module works in calibration orientation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Iterable, Optional

import cv2
import numpy as np

#: USB stream -> factory-profile camera key
STREAM_ROLE = {0: "ir_right", 1: "ir_left", 2: "color"}

#: USB stream -> needs a 180 deg rotation to reach calibration orientation
STREAM_ROT180 = {0: True, 1: False, 2: True}

#: USB stream -> short human label
STREAM_LABEL = {0: "cam0/right_IR", 1: "cam1/left_IR", 2: "cam2/colour"}

#: cameras that carry the calibration plate (both IR)
IR_STREAMS = (0, 1)


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------

def read_pgm(path: str | os.PathLike) -> np.ndarray:
    """Read a binary P5 8-bit PGM as ``uint8`` (shape HxW), or raise."""
    path = str(path)
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None:
        raise IOError(f"cannot read {path}")
    if im.dtype != np.uint8 or im.ndim != 2:
        raise IOError(f"unexpected image format in {path}: {im.dtype} {im.shape}")
    return im


def calibration_image(stream: int, image: np.ndarray) -> np.ndarray:
    """Rotate a raw stream image into the calibration orientation."""
    if STREAM_ROT180[int(stream)]:
        return np.ascontiguousarray(np.rot90(image, 2))
    return image


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

@dataclass
class Group:
    """One synchronised three-camera group."""
    group: int
    device_group: Optional[int]
    frame_ids: dict[int, int]
    host_time_ns: Optional[int]
    paths: dict[int, str]

    def path(self, stream: int) -> str:
        return self.paths[int(stream)]


@dataclass
class SweepSource:
    frames_dir: str
    groups: list[Group]
    manifest: dict
    n_groups_manifest: int = 0
    n_groups_incomplete: int = 0
    dropped_groups: list[int] = field(default_factory=list)
    frame_size: tuple[int, int] = (1280, 1024)

    @property
    def n(self) -> int:
        return len(self.groups)


def load_frames(frames_dir: str | os.PathLike,
                cams: Iterable[int] = IR_STREAMS) -> SweepSource:
    """Load the frame inventory of a capture directory.

    Uses ``frames_manifest.json`` (schema ``einstar-frames-v1``) when present.
    A group is kept only when **all** requested camera streams exist on disk;
    kept groups are sorted by group id.  Groups present in the manifest but
    incomplete are reported in ``dropped_groups``.
    """
    frames_dir = str(frames_dir)
    cams = tuple(int(c) for c in cams)
    all_streams = (0, 1, 2)
    manifest_path = Path(frames_dir) / "frames_manifest.json"
    manifest: dict = {}
    groups: list[Group] = []
    n_manifest = 0
    n_incomplete = 0
    size = (1280, 1024)

    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        fr = manifest.get("frame") or {}
        size = (int(fr.get("width", size[0])), int(fr.get("height", size[1])))
        n_manifest = len(manifest.get("groups") or [])
        n_incomplete = int(manifest.get("groups_incomplete") or 0)
        for rec in manifest.get("groups") or []:
            paths = {}
            for s in all_streams:
                p = rec.get(f"stream{s}")
                if p and os.path.exists(p):
                    paths[s] = p
            if len(paths) < len(all_streams):
                continue
            fids = {int(k): int(v) for k, v in (rec.get("frame_ids") or {}).items()}
            groups.append(Group(int(rec["group"]), rec.get("device_group"), fids,
                                rec.get("host_time_ns"), paths))
    if not groups:
        # no manifest: enumerate the directory
        found: dict[int, dict[int, str]] = {}
        for name in os.listdir(frames_dir):
            if not (name.startswith("frame_") and name.endswith(".pgm") and "_cam" in name):
                continue
            stem, _ = name.rsplit("_cam", 1)
            try:
                gid = int(stem.split("_")[1])
                cam = int(_.split(".")[0])
            except (IndexError, ValueError):
                continue
            found.setdefault(gid, {})[cam] = os.path.join(frames_dir, name)
        for gid in sorted(found):
            if len(found[gid]) < len(all_streams):
                continue
            groups.append(Group(gid, None, {}, None, found[gid]))

    groups.sort(key=lambda g: g.group)
    dropped = [g.group for g in groups
               if any(s not in g.paths for s in cams)]
    if dropped:
        groups = [g for g in groups if g.group not in set(dropped)]
    return SweepSource(frames_dir=frames_dir, groups=groups, manifest=manifest,
                       n_groups_manifest=n_manifest,
                       n_groups_incomplete=n_incomplete,
                       dropped_groups=dropped, frame_size=size)


def manifest_summary(src: SweepSource, cams: Iterable[int]) -> dict:
    """Cadence / trigger-counter / frame-id statistics of the capture."""
    cams = tuple(int(c) for c in cams)
    m = src.manifest or {}
    out: dict = {
        "schema": m.get("schema"),
        "simulated": m.get("simulated"),
        "requested_groups": m.get("requested_groups"),
        "groups_written": m.get("groups_written"),
        "groups_complete_used": src.n,
        "groups_incomplete": src.n_groups_incomplete,
        "frame_size": list(src.frame_size),
    }
    cad = m.get("cadence") or {}
    if cad:
        out["cadence"] = {
            "intervals": cad.get("intervals"),
            "mean_ms": cad.get("mean_ms"),
            "min_ms": cad.get("min_ms"),
            "max_ms": cad.get("max_ms"),
            "fps": cad.get("fps"),
        }
    dev = m.get("device") or {}
    if dev:
        out["trigger"] = {
            "period_us": (dev.get("volatile_settings") or {}).get("trigger_period_us"),
            "exposure_us": (dev.get("volatile_settings") or {}).get("exposure_us"),
            "gain": (dev.get("volatile_settings") or {}).get("gain"),
        }
    dg = [g.device_group for g in src.groups if g.device_group is not None]
    if dg:
        steps = np.diff(np.asarray(dg, dtype=np.int64))
        out["device_group"] = {
            "first": int(dg[0]), "last": int(dg[-1]), "n": len(dg),
            "unique": int(len(set(dg))),
            "gaps": int(np.sum(steps != 1)) if len(steps) else 0,
            "step_min": int(steps.min()) if len(steps) else None,
            "step_max": int(steps.max()) if len(steps) else None,
        }
    # frame-id (8-bit slot counter) consistency across cameras and wraps
    mism = 0
    wraps = 0
    prev = None
    for g in src.groups:
        vals = [g.frame_ids.get(s) for s in cams if s in g.frame_ids]
        if len(set(vals)) > 1:
            mism += 1
        vals_all = list(g.frame_ids.values())
        if len(set(vals_all)) > 1:
            mism += 0  # already counted for the requested cameras
        if prev is not None and vals and prev and vals[0] < prev[0]:
            wraps += 1
        if vals:
            prev = vals
    out["frame_ids"] = {
        "mismatched_groups": mism,
        "wraps": wraps,
        "modulus": 256,
        "available": bool(src.groups and src.groups[0].frame_ids),
    }
    ht = [g.host_time_ns for g in src.groups if g.host_time_ns is not None]
    if len(ht) > 1:
        dt = np.diff(np.asarray(ht, dtype=np.int64)) / 1e6
        out["host_time"] = {"dt_mean_ms": float(dt.mean()),
                            "dt_min_ms": float(dt.min()),
                            "dt_max_ms": float(dt.max()),
                            "duration_s": float((ht[-1] - ht[0]) / 1e9)}
    return out


# ---------------------------------------------------------------------------
# factory profile
# ---------------------------------------------------------------------------

@dataclass
class CameraProfile:
    name: str
    fx: float
    fy: float
    cx: float
    cy: float
    skew: float
    dist: np.ndarray
    R: np.ndarray
    T: np.ndarray

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.fx, self.skew, self.cx],
                         [0.0, self.fy, self.cy],
                         [0.0, 0.0, 1.0]], float)

    @property
    def kc(self) -> np.ndarray:
        return np.asarray(self.dist, float).ravel()


@dataclass
class Profile:
    path: str
    schema: Optional[str]
    image_size: tuple[int, int]
    pitch_mm: float
    cameras: dict[str, CameraProfile]
    provenance: list[str] = field(default_factory=list)
    color_status: Optional[str] = None

    def camera(self, stream: int) -> CameraProfile:
        return self.cameras[STREAM_ROLE[int(stream)]]


def load_profile(path: str | os.PathLike) -> Profile:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    cams: dict[str, CameraProfile] = {}
    for name, c in (doc.get("cameras") or {}).items():
        cams[name] = CameraProfile(
            name=name, fx=float(c["fx"]), fy=float(c["fy"]),
            cx=float(c["cx"]), cy=float(c["cy"]), skew=float(c.get("skew", 0.0)),
            dist=np.asarray(c.get("dist", [0.0] * 5), float),
            R=np.asarray(c.get("R", np.eye(3).ravel()), float).reshape(3, 3),
            T=np.asarray(c.get("T", [0.0, 0.0, 0.0]), float).ravel())
    for need in ("ir_left", "ir_right"):
        if need not in cams:
            raise ValueError(f"profile {path} has no camera {need!r}")
    return Profile(path=str(path), schema=doc.get("schema"),
                   image_size=(int(doc.get("image_width", 1280)),
                               int(doc.get("image_height", 1024))),
                   pitch_mm=float(doc.get("plate_pitch_mm", 28.0)),
                   cameras=cams,
                   provenance=list(doc.get("provenance") or []),
                   color_status=doc.get("color_status"))


def factory_stereo(profile: Profile) -> tuple[np.ndarray, np.ndarray]:
    """Factory pose of the right IR camera in the left IR frame: X_r = R X_l + T."""
    right = profile.cameras["ir_right"]
    left = profile.cameras["ir_left"]
    # the profile is expressed in the reference frame (ir_left, R=I, T=0)
    R_rel = right.R @ left.R.T
    t_rel = right.T - R_rel @ left.T
    return R_rel, t_rel
