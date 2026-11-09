"""Read/write the vendor CCF calibration records without losing bytes.

The on-disk ``*CCF.txt`` files are raw little-endian binary, not text:

* 264 bytes = 33 float64 (colour camera)
* 520 bytes = 33 float64 + 256-byte trailer (64 int32, IR cameras)

Field order (MATLAB Calib_Results packing, proved in
``docs/calibration/ccf_format.md``)::

    fx fy pad pad cx cy pad pad alpha pad k1 k2 p1 p2 k3 pad*5
    Tx Ty Tz R[9] error

IR records are obfuscated by a per-field integer offset (``stored = true + key``).
This module keeps both the raw bytes and the decoded values; the writer can
reproduce the raw bytes exactly (round-trip proved against the shipped files).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

import numpy as np

#: proved IR key for the fields we use (see docs/calibration/ccf_format.md)
IR_KEY = np.array([
    8, -4, 0, 0, -6, -1, 0, 0, -2, 0,
    0, 6, 2, 7, 13, 0, 0, 0, 0, 0,
    0, 0, 0, -2, -4, -11, -9, 0, 5, -11, -12, 0, 0,
], float)

COLOR_KEY = np.zeros(33, float)

IDX = dict(fx=0, fy=1, cx=4, cy=5, alpha=8,
           k1=10, k2=11, p1=12, p2=13, k3=14,
           tx=20, ty=21, tz=22)


@dataclass
class CCFRecord:
    raw: bytes
    values: np.ndarray          # decoded 33 doubles
    key: np.ndarray
    name: str = ""
    ir: bool = True

    @property
    def trailer(self) -> np.ndarray | None:
        if len(self.raw) <= 264:
            return None
        return np.frombuffer(self.raw[264:520], dtype="<i4").copy()

    def K(self) -> np.ndarray:
        v = self.values
        return np.array([[v[0], v[8] * v[0], v[4]],
                         [0.0, v[1], v[5]],
                         [0.0, 0.0, 1.0]])

    def dist(self) -> np.ndarray:
        v = self.values
        return np.array([v[10], v[11], v[12], v[13], v[14]])

    def R(self) -> np.ndarray:
        return self.values[23:32].reshape(3, 3)

    def T(self) -> np.ndarray:
        return self.values[20:23]


def decode_record(data: bytes, name: str = "", ir: bool | None = None) -> CCFRecord:
    if len(data) not in (264, 520):
        raise ValueError(f"CCF record must be 264 or 520 bytes, got {len(data)}")
    stored = np.array(struct.unpack("<33d", data[:264]), float)
    ir = (len(data) == 520) if ir is None else ir
    key = IR_KEY.copy() if ir else COLOR_KEY.copy()
    return CCFRecord(raw=bytes(data), values=stored - key, key=key, name=name, ir=ir)


def encode_record(values: np.ndarray, ir: bool = True, trailer: bytes | None = None,
                  key: np.ndarray | None = None) -> bytes:
    values = np.asarray(values, float).reshape(33)
    k = (IR_KEY if ir else COLOR_KEY) if key is None else np.asarray(key, float)
    stored = values + k
    out = struct.pack("<33d", *stored)
    if ir:
        if trailer is None:
            trailer = bytes(256)
        if len(trailer) != 256:
            raise ValueError("IR trailer must be 256 bytes")
        out += trailer
    return out


def load(path: str | Path, ir: bool | None = None) -> CCFRecord:
    p = Path(path)
    return decode_record(p.read_bytes(), name=p.name, ir=ir)


def save(path: str | Path, values: np.ndarray, ir: bool = True,
         trailer: bytes | None = None) -> None:
    Path(path).write_bytes(encode_record(values, ir=ir, trailer=trailer))


def compare(a: CCFRecord, b: CCFRecord) -> dict:
    return {"max_abs_diff": float(np.max(np.abs(a.values - b.values))),
            "raw_equal": a.raw == b.raw}


def pose(rec: CCFRecord) -> tuple[np.ndarray, np.ndarray]:
    """World->camera (R, T) as stored."""
    return rec.R(), rec.T()


def relative_pose(a: CCFRecord, b: CCFRecord) -> tuple[np.ndarray, np.ndarray]:
    """Pose of camera b in camera a's frame: (R_ba, t_ba).

    CCF stores each camera's world->camera pose in the common board frame, so
    ``t_ba = T_b - R_b R_a^T T_a``.  This is frame-invariant: it is the quantity
    a consistency guard must compare, not the raw T values.
    """
    Ra, Ta = pose(a)
    Rb, Tb = pose(b)
    R = Rb @ Ra.T
    return R, Tb - R @ Ta


def rotation_angle_deg(R: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def consistency(a: CCFRecord, b: CCFRecord,
                expected: tuple[np.ndarray, np.ndarray] | None = None,
                tau_translation_mm: float = 3.0,
                tau_rotation_deg: float = 0.3) -> dict:
    """Compare a pair's relative pose with an independent expectation.

    The guard is intentionally frame-invariant.  A record can be internally
    well-formed and still be a stale/mixed-generation record; this catches that
    case when an independent estimate is available.
    """
    R, t = relative_pose(a, b)
    out = {"translation_mm": float(np.linalg.norm(t)),
           "rotation_deg": rotation_angle_deg(R)}
    if expected is not None:
        Re, te = expected
        dR = R @ Re.T
        out["delta_translation_mm"] = float(np.linalg.norm(t - te))
        out["delta_rotation_deg"] = rotation_angle_deg(dR)
        out["ok"] = bool(out["delta_translation_mm"] <= tau_translation_mm and
                         out["delta_rotation_deg"] <= tau_rotation_deg)
    else:
        out["ok"] = None
        out["note"] = "no independent expectation supplied"
    return out
