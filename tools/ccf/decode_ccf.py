#!/usr/bin/env python3
"""Decoder for the SHINING 3D / EXStar CCF calibration records.

Two containers are supported:

  * ``--txt FILE``  a single ``LeftCCF.txt`` / ``RightCCF.txt`` / ``TexCCF.txt``
    record.  Despite the extension these files are raw little-endian binary.
      - 264 bytes  -> colour ("Tex") record: 33 float64, no key.
      - 520 bytes  -> IR ("Left"/"Right") record: 33 float64 followed by a
                      256-byte trailer (64 little-endian int32, each 0..9).

  * ``--bin FILE``  a 6568-byte device-info dump (the blob returned by the
    firmware "read custom info" command, e.g. calibration.bin).  It embeds,
    among other things, a CCF block holding the three records verbatim.

The 33 doubles use the layout of the MATLAB Camera-Calibration-Toolbox
``Calib_Results`` file that SHINING also ships as ``LeftLongCCF.txt``::

    fc[2], _pad[2], cc[2], _pad[2], alpha, _pad, kc[5], _pad[5],
    T[3], R[9] (row major), error

so field indices are::

    0 fx   1 fy   2 -   3 -   4 cx   5 cy   6 -   7 -
    8 alpha   9 -   10 k1  11 k2  12 p1  13 p2  14 k3
    15..19 -   20 Tx 21 Ty 22 Tz   23..31 R(row major)   32 reprojection error

IR records are obfuscated with a per-field *integer* offset ("the IR key"):
``stored = true + key``.  The key entries for the rotation block were recovered
from the data alone (they are the unique integers that turn the stored 3x3
block into an orthonormal, det=+1 matrix for six independent CCF files); see
docs/calibration/ccf_format.md.  ``--key-mode`` lets the caller reproduce the
different confidence levels.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

RECORD_COLOR_SIZE = 264          # 33 float64
RECORD_IR_SIZE = 520             # 33 float64 + 256 byte trailer
NUM_FIELDS = 33
NUM_KEYED_FIELDS = 32            # the trailer/error field (32) is never keyed
TRAILER_INTS = 64                # 64 x int32, values 0..9

IMAGE_W, IMAGE_H = 1280, 1024    # all three cameras

FIELD_NAMES = [
    "fx", "fy", "_pad0", "_pad1", "cx", "cy", "_pad2", "_pad3",
    "alpha", "_pad4",
    "k1", "k2", "p1", "p2", "k3",
    "_pad5", "_pad6", "_pad7", "_pad8", "_pad9",
    "Tx", "Ty", "Tz",
    "R00", "R01", "R02", "R10", "R11", "R12", "R20", "R21", "R22",
    "error",
]

#: Recovered IR key ("stored = true + key").  ``None`` marks an entry whose
#: absolute value is not determined by the available evidence (only the
#: difference key[0]-key[1] = 12 is forced by fx == fy).
IR_KEY_BEST = [
    8.0, -4.0, 0.0, 0.0, -6.0, -1.0, 0.0, 0.0,
    -2.0, 0.0,
    0.0, 6.0, 2.0, 7.0, 13.0,
    0.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 0.0,
    -2.0, -4.0, -11.0, -9.0, 0.0, 5.0, -11.0, -12.0, 0.0,
    0.0,
]

#: Entries for which only the *difference* key[0]-key[1] == 12 is proven;
#: 8/-4 is the choice that additionally agrees with the in-flash 22-double
#: records to <1 px.
IR_KEY_PROVEN = [
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    -2.0, 0.0,
    0.0, 6.0, 2.0, 7.0, 13.0,
    0.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 0.0,
    -2.0, -4.0, -11.0, -9.0, 0.0, 5.0, -11.0, -12.0, 0.0,
    0.0,
]

#: Per-entry confidence, for the report/JSON.
KEY_CONFIDENCE = {
    0: "hypothesis (only key[0]-key[1]=12 is forced)",
    1: "hypothesis (only key[0]-key[1]=12 is forced)",
    4: "hypothesis (both cameras agree with the in-flash record to 0.28 px)",
    5: "hypothesis",
    8: "strong (uniquely plausible integer)",
    10: "proven (exact vs in-flash 22-double record)",
    11: "proven (exact vs in-flash 22-double record)",
    12: "proven (exact vs in-flash 22-double record)",
    13: "proven (exact vs in-flash 22-double record)",
    14: "proven (exact vs in-flash 22-double record)",
    20: "assumed 0 (rig triangle agrees to 2.4 mm)",
    21: "assumed 0 (rig triangle agrees to 2.4 mm)",
    22: "assumed 0 (rig triangle agrees to 2.4 mm)",
    32: "assumed 0 (stored value is 0.0)",
}
for _i in range(23, 32):
    KEY_CONFIDENCE[_i] = "PROVEN by orthonormality (2 calibrations x 2 cameras, 1e-15)"


def ir_key(mode: str) -> list[float]:
    if mode == "zero":
        return [0.0] * NUM_FIELDS
    if mode == "proven":
        return list(IR_KEY_PROVEN)
    return list(IR_KEY_BEST)


# --------------------------------------------------------------------------
# record decoding
# --------------------------------------------------------------------------

@dataclass
class CcfRecord:
    path: str
    size: int
    kind: str                      # "ir" | "color" | "unknown"
    stored: list[float]
    decoded: list[float]
    key: list[float]
    trailer_digits: list[int] = field(default_factory=list)

    # -- convenience accessors (decoded values) ---------------------------
    @property
    def fx(self) -> float: return self.decoded[0]

    @property
    def fy(self) -> float: return self.decoded[1]

    @property
    def cx(self) -> float: return self.decoded[4]

    @property
    def cy(self) -> float: return self.decoded[5]

    @property
    def alpha(self) -> float: return self.decoded[8]

    @property
    def kc(self) -> list[float]: return self.decoded[10:15]

    @property
    def T(self) -> np.ndarray: return np.array(self.decoded[20:23], float)

    @property
    def R(self) -> np.ndarray: return np.array(self.decoded[23:32], float).reshape(3, 3)

    @property
    def error(self) -> float: return self.decoded[32]

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.fx, 0.0, self.cx],
                         [0.0, self.fy, self.cy],
                         [0.0, 0.0, 1.0]])

    def centre(self) -> np.ndarray:
        """Camera centre in world coordinates: C = -R^T T."""
        return -self.R.T @ self.T

    def rotation_metrics(self, values: list[float] | None = None) -> dict:
        R = (self.R if values is None
             else np.array(values[23:32], float).reshape(3, 3))
        RtR = R @ R.T
        return {
            "det": float(np.linalg.det(R)),
            "max_abs_RRt_minus_I": float(np.abs(RtR - np.eye(3)).max()),
            "frobenius_RRt_minus_I": float(np.linalg.norm(RtR - np.eye(3))),
            "orthonormal": bool(np.abs(RtR - np.eye(3)).max() < 1e-9
                                and abs(np.linalg.det(R) - 1.0) < 1e-9),
        }

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "kind": self.kind,
            "fields": {FIELD_NAMES[i]: self.decoded[i] for i in range(NUM_FIELDS)},
            "stored": {FIELD_NAMES[i]: self.stored[i] for i in range(NUM_FIELDS)},
            "key": {FIELD_NAMES[i]: self.key[i] for i in range(NUM_FIELDS)},
            "intrinsics": {
                "fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy,
                "K": self.K.tolist(),
                "fx_over_fy": self.fx / self.fy if self.fy else None,
                "alpha": self.alpha,
                "cx_offset_from_centre_px": self.cx - IMAGE_W / 2.0,
                "cy_offset_from_centre_px": self.cy - IMAGE_H / 2.0,
            },
            "distortion": dict(zip(("k1", "k2", "p1", "p2", "k3"), self.kc)),
            "extrinsics": {
                "T": self.T.tolist(),
                "R": self.R.tolist(),
                "camera_centre": self.centre().tolist(),
                "baseline_from_origin": float(np.linalg.norm(self.T)),
            },
            "rotation_check_decoded": self.rotation_metrics(),
            "rotation_check_stored": self.rotation_metrics(self.stored),
            "error": self.error,
            "trailer": {
                "present": bool(self.trailer_digits),
                "count": len(self.trailer_digits),
                "values": self.trailer_digits[:8],
                "min": min(self.trailer_digits) if self.trailer_digits else None,
                "max": max(self.trailer_digits) if self.trailer_digits else None,
                "digits": "".join(str(d) for d in self.trailer_digits) if self.trailer_digits else "",
            },
        }


def _classify(name: str) -> str:
    n = name.lower()
    if "tex" in n or "color" in n:
        return "color"
    if "left" in n or "right" in n:
        return "ir"
    return "unknown"


def decode_record(data: bytes, path: str = "<bytes>", key_mode: str = "best") -> CcfRecord:
    if len(data) not in (RECORD_COLOR_SIZE, RECORD_IR_SIZE):
        raise ValueError(
            f"{path}: unsupported CCF record size {len(data)} "
            f"(expected {RECORD_COLOR_SIZE} or {RECORD_IR_SIZE})")
    kind = _classify(path)
    if len(data) == RECORD_COLOR_SIZE:
        kind = "color" if kind == "unknown" else kind
    else:
        kind = "ir" if kind == "unknown" else kind

    stored = list(struct.unpack_from("<33d", data, 0))
    key = ir_key(key_mode) if kind == "ir" else [0.0] * NUM_FIELDS
    decoded = [stored[i] - key[i] for i in range(NUM_FIELDS)]

    trailer: list[int] = []
    if len(data) == RECORD_IR_SIZE:
        trailer = list(struct.unpack_from(f"<{TRAILER_INTS}i", data, NUM_FIELDS * 8))

    return CcfRecord(path=path, size=len(data), kind=kind,
                     stored=stored, decoded=decoded, key=key,
                     trailer_digits=trailer)


# --------------------------------------------------------------------------
# device-info dump (--bin)
# --------------------------------------------------------------------------

CCF_BLOCK_MAGIC = b"FQFQ"
CCF_BLOCK_MAGIC_OFF = 0xF1C       # magic offset inside the CCF block
CCF_SLOT_OFFSETS = {"left": 0x108, "right": 0x610, "tex": 0xB18}
CCF_SLOT_SIZES = {"left": RECORD_IR_SIZE, "right": RECORD_IR_SIZE,
                  "tex": RECORD_COLOR_SIZE}
CAMERA_RECORD_OFF = 0x0C          # first 22-double camera record
CAMERA_RECORD_STRIDE = 0xF8       # 248 bytes
CAMERA_RECORD_DOUBLES = 22        # fx fy cx cy dist6 R9 T3


def locate_ccf_block(buf: bytes) -> int:
    """Return the offset of the CCF block (whose first u32 is the type == 4)."""
    magic = buf.find(CCF_BLOCK_MAGIC)
    while magic >= 0:
        start = magic - CCF_BLOCK_MAGIC_OFF
        if start >= 0 and struct.unpack_from("<I", buf, start)[0] == 4:
            return start
        magic = buf.find(CCF_BLOCK_MAGIC, magic + 1)
    raise ValueError("CCF block not found (no 'FQFQ' with a type-4 header)")


def _printable_runs(buf: bytes, min_len: int = 8) -> list[dict]:
    """NUL-free printable ASCII runs, reported by offset only.

    Used instead of matching vendor tag prefixes so that this public tool
    carries no device-identifying literal strings.
    """
    runs: list[dict] = []
    i = 0
    n = len(buf)
    while i < n:
        if 0x20 <= buf[i] < 0x7F:
            j = i
            while j < n and 0x20 <= buf[j] < 0x7F:
                j += 1
            if j - i >= min_len:
                runs.append({"offset": i,
                             "value": buf[i:j].decode("ascii", "replace")})
            i = j
        else:
            i += 1
    return runs


def _parse_para_sections(buf: bytes) -> list[dict]:
    """Locate the tagged FAFA/FBFB white-balance / colour-correction blocks."""
    out = []
    for tag in (b"FAFA", b"FBFB", b"FWFW", b"AABB", b"DADA"):
        pos = buf.find(tag)
        while pos >= 0:
            entry = {"tag": tag.decode("ascii"), "offset": pos}
            if tag in (b"FAFA", b"FBFB") and pos + 9 <= len(buf):
                entry["u32"] = struct.unpack_from("<I", buf, pos + 4)[0]
                entry["ccm"] = list(struct.unpack_from("<15f", buf, pos + 9))
                entry["dark"] = list(struct.unpack_from("<3f", buf, pos + 69))
            if tag == b"FWFW" and pos + 9 <= len(buf):
                entry["u32"] = struct.unpack_from("<I", buf, pos + 4)[0]
            out.append(entry)
            pos = buf.find(tag, pos + 1)
    return out


def decode_bin(path: str, key_mode: str = "best") -> dict:
    buf = Path(path).read_bytes()
    result: dict = {"path": path, "size": len(buf), "records": {}, "camera_records": []}

    if len(buf) >= 12:
        result["header"] = {
            "u32_0": struct.unpack_from("<I", buf, 0)[0],
            "width": struct.unpack_from("<I", buf, 4)[0],
            "height": struct.unpack_from("<I", buf, 8)[0],
        }

    # --- 22-double camera records (fx fy cx cy dist6 R9 T3) ---------------
    for i in range(3):
        off = CAMERA_RECORD_OFF + i * CAMERA_RECORD_STRIDE
        if off + 8 * CAMERA_RECORD_DOUBLES > len(buf):
            break
        v = list(struct.unpack_from(f"<{CAMERA_RECORD_DOUBLES}d", buf, off))
        R = np.array(v[10:19], float).reshape(3, 3)
        T = np.array(v[19:22], float)
        result["camera_records"].append({
            "offset": off,
            "fx": v[0], "fy": v[1], "cx": v[2], "cy": v[3],
            "dist6": v[4:10],
            "R": R.tolist(), "T": T.tolist(),
            "camera_centre": (-R.T @ T).tolist(),
            "det": float(np.linalg.det(R)),
            "max_abs_RRt_minus_I": float(np.abs(R @ R.T - np.eye(3)).max()),
        })

    # --- CCF block --------------------------------------------------------
    block = locate_ccf_block(buf)
    result["ccf_block_offset"] = block
    result["ccf_block_type"] = struct.unpack_from("<I", buf, block)[0]
    slots = {}
    for name, rel in CCF_SLOT_OFFSETS.items():
        abs_off = block + rel
        declared = struct.unpack_from("<I", buf, abs_off)[0]
        want = CCF_SLOT_SIZES[name]
        if declared != want:
            raise ValueError(f"{name} slot at {abs_off:#x}: length {declared} != {want}")
        payload = buf[abs_off + 4: abs_off + 4 + want]
        slots[name] = payload
        result["records"][name] = decode_record(
            payload, f"{path}:{name}", key_mode).as_dict()
    result["ccf_slots"] = {k: {"offset": block + CCF_SLOT_OFFSETS[k],
                               "length": CCF_SLOT_SIZES[k]} for k in slots}

    # --- misc tagged blocks ----------------------------------------------
    result["tagged_sections"] = _parse_para_sections(buf)
    # Report embedded printable runs structurally instead of matching specific
    # vendor prefixes, so the tool itself carries no device-identifying strings.
    result.setdefault("strings", []).extend(_printable_runs(buf, min_len=8))
    result["trailer_sha256_hint"] = None
    return result


# --------------------------------------------------------------------------
# reporting helpers
# --------------------------------------------------------------------------

def _fmt_record(rec: CcfRecord) -> str:
    m = rec.rotation_metrics()
    ms = rec.rotation_metrics(rec.stored)
    lines = [
        f"--- {rec.path}  ({rec.size} bytes, kind={rec.kind})",
        f"    fx={rec.fx:.6f}  fy={rec.fy:.6f}  (fx/fy={rec.fx / rec.fy:.8f})",
        f"    cx={rec.cx:.6f}  cy={rec.cy:.6f}   alpha={rec.alpha:.9f}",
        "    kc = [" + ", ".join(f"{c:.9f}" for c in rec.kc) + "]",
        f"    T  = [{rec.T[0]:.6f}, {rec.T[1]:.6f}, {rec.T[2]:.6f}]  |T|={np.linalg.norm(rec.T):.4f}",
        f"    R  =\n        {rec.R[0]}\n        {rec.R[1]}\n        {rec.R[2]}",
        f"    det(R)={m['det']:.12f}  max|RR^T-I|={m['max_abs_RRt_minus_I']:.3e}  "
        f"||RR^T-I||_F={m['frobenius_RRt_minus_I']:.3e}",
        f"    (stored, before key: det={ms['det']:.6f} "
        f"max|RR^T-I|={ms['max_abs_RRt_minus_I']:.6f})",
        f"    error={rec.error:.9f}",
    ]
    if rec.trailer_digits:
        lines.append(f"    trailer: {len(rec.trailer_digits)} int32, "
                     f"range {min(rec.trailer_digits)}..{max(rec.trailer_digits)}, "
                     f"digits={''.join(str(d) for d in rec.trailer_digits)[:32]}...")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Decode SHINING 3D / EXStar CCF calibration records.")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--txt", metavar="FILE",
                     help="LeftCCF.txt / RightCCF.txt / TexCCF.txt record")
    src.add_argument("--bin", metavar="FILE",
                     help="6568-byte device-info dump containing the CCF block")
    ap.add_argument("--json-out", metavar="FILE", help="write the decode as JSON")
    ap.add_argument("--key-mode", choices=("best", "proven", "zero"), default="best",
                    help="IR key table to subtract (default: best)")
    ap.add_argument("--print-key", action="store_true", help="print the IR key table")
    args = ap.parse_args(argv)
    if not (args.txt or args.bin or args.print_key):
        ap.error("one of --txt, --bin or --print-key is required")

    if args.print_key:
        for i in range(NUM_FIELDS):
            print(f"  key[{i:2d}] {FIELD_NAMES[i]:6} = {ir_key(args.key_mode)[i]:+6.1f}"
                  f"   {KEY_CONFIDENCE.get(i, 'zero / structurally empty')}")

    if not (args.txt or args.bin):
        return 0

    if args.txt:
        rec = decode_record(Path(args.txt).read_bytes(), args.txt, args.key_mode)
        print(_fmt_record(rec))
        payload = {"mode": "txt", "key_mode": args.key_mode, "record": rec.as_dict()}
    else:
        payload = decode_bin(args.bin, args.key_mode)
        payload["key_mode"] = args.key_mode
        for name in ("left", "right", "tex"):
            d = payload["records"].get(name)
            if d:
                print(f"=== {name} @ {payload['ccf_slots'][name]['offset']:#x}")
                v = d["fields"]
                print(f"    fx={v['fx']:.6f} fy={v['fy']:.6f} cx={v['cx']:.6f} cy={v['cy']:.6f}")
                print(f"    kc=[{v['k1']:.9f}, {v['k2']:.9f}, {v['p1']:.9f}, "
                      f"{v['p2']:.9f}, {v['k3']:.9f}]")
                print(f"    det={d['rotation_check_decoded']['det']:.12f} "
                      f"max|RR^T-I|={d['rotation_check_decoded']['max_abs_RRt_minus_I']:.3e}")
        print("\n--- 22-double camera records (unkeyed, different origin) ---")
        for i, c in enumerate(payload["camera_records"]):
            print(f"  [{i}] off={c['offset']:#06x} fx={c['fx']:.4f} fy={c['fy']:.4f} "
                  f"cx={c['cx']:.4f} cy={c['cy']:.4f} |T|={np.linalg.norm(c['T']):.3f} "
                  f"det={c['det']:.12f}")
        print(f"\n  CCF block @ {payload['ccf_block_offset']:#x} "
              f"(type={payload['ccf_block_type']})")
        for s in payload.get("strings", []):
            print(f"  string @ {s['offset']:#06x}: {s['value']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
