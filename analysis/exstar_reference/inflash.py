#!/usr/bin/env python
"""Decode the two vendor calibration datasets in the device info dump.

Input: ``~/einstar-private/calibration.bin`` (6568 B, read twice on 2026-11-07,
both copies SHA-256 557a79926fbd8459...).  The dump holds

  * a header ``(u32 magic=1, u32 width=1280, u32 height=1024)`` at 0,
  * three 176-byte **unkeyed 22-double camera records** at 12 / 260 / 508
    (``fx fy cx cy dist6 R9 T3``, left/right/colour, 248-byte stride),
  * a **CCF block** whose three 520/520/264-byte records start at 1191 / 2479 /
    3767 -- byte-identical to ``sources/calibration/res_Einscan-E10_200x150``
    (= the pre-recalibration copy on the Windows host), i.e. the host CCF of
    2026-02-09 (the dump carries the ASCII timestamp ``2026-02-09 21:34``),
  * ASCII device strings, including ``<redacted-production-date>`` after the records.

The records are found by structure (orthonormal R, |T| in 300..800 mm), not by
offset, so a different dump layout still decodes.

Writes ``inflash_records.json`` (private data -> numbers only, no raw dump).
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import vendor_lib as V  # noqa: E402

DUMP = os.path.expanduser("~/einstar-private/calibration.bin")
NAMES = ("left", "right", "tex")


def find_records(blob: bytes) -> list:
    """Scan for 22-double records: 4 intrinsics, 6 dist, orthonormal R9, T3."""
    hits = []
    for off in range(0, len(blob) - 176 + 1, 4):
        v = np.frombuffer(blob[off:off + 176], dtype="<f8")
        if not np.all(np.isfinite(v)):
            continue
        R = v[10:19].reshape(3, 3)
        if not (300.0 < np.linalg.norm(v[19:22]) < 800.0):
            continue
        if np.abs(R @ R.T - np.eye(3)).max() > 1e-9:
            continue
        if abs(abs(np.linalg.det(R)) - 1.0) > 1e-9:
            continue
        hits.append((off, v.copy()))
    return hits


def rig(a: dict, b: dict) -> dict:
    R = b["R"] @ a["R"].T
    t = b["T"] - R @ a["T"]
    return {"baseline_mm": float(np.linalg.norm(t)), "t_rel": t.tolist(),
            "rel_rot_deg": float(np.degrees(np.arccos(
                np.clip((np.trace(R) - 1) / 2, -1, 1))))}


def main() -> int:
    blob = open(DUMP, "rb").read()
    hits = find_records(blob)
    if len(hits) != 3:
        raise SystemExit(f"expected 3 records, found {len(hits)}")
    cams = {}
    for (off, v), name in zip(hits, NAMES):
        cams[name] = {"offset": off, "fx": float(v[0]), "fy": float(v[1]),
                      "cx": float(v[2]), "cy": float(v[3]),
                      "dist6": [float(x) for x in v[4:10]],
                      "kc": [float(x) for x in v[4:9]],
                      "alpha": float(v[9]),
                      "R": v[10:19].reshape(3, 3).tolist(),
                      "T": [float(x) for x in v[19:22]]}
        cams[name]["centre_mm"] = (-v[10:19].reshape(3, 3).T
                                   @ v[19:22]).tolist()
        cams[name]["fx_over_fy"] = cams[name]["fx"] / cams[name]["fy"]

    # CCF block byte-identity check
    setA = os.path.join(V.FACTORY_REPO)
    ccf_offsets = {}
    identical = {}
    for side in ("Left", "Right", "Tex"):
        data = open(os.path.join(setA, f"{side}CCF.txt"), "rb").read()
        ccf_offsets[side] = blob.find(data)
        identical[side] = ccf_offsets[side] >= 0
    newdir = V.NEW_DIR
    new_in_dump = {s: blob.find(open(os.path.join(newdir, f"{s}CCF.txt"), "rb").read()) >= 0
                   for s in ("Left", "Right", "Tex")}

    # dataset comparison
    fac = V.decode_ccf_set(V.FACTORY_REPO, "best")
    new = V.decode_ccf_set(V.NEW_DIR, "best")
    prof = V.model_from_profile("analysis/calibration/solver/final/profile.json")
    inflash_model = {c: {"R": np.array(cams[c]["R"], float),
                         "T": np.array(cams[c]["T"], float),
                         "fx": cams[c]["fx"], "fy": cams[c]["fy"],
                         "cx": cams[c]["cx"], "cy": cams[c]["cy"],
                         "kc": np.array(cams[c]["kc"], float), "skew": 0.0}
                     for c in NAMES}

    datasets = {"ccf_A_20260209": fac, "ccf_NEW_20261114": new,
                "inflash_22double": inflash_model, "linux_candidate": prof}
    pairs = (("left", "right"), ("left", "tex"), ("right", "tex"))
    comparison = {}
    for tag, m in datasets.items():
        comparison[tag] = {f"{a}_{b}": rig(m[a], m[b]) for a, b in pairs}
        comparison[tag]["intrinsics"] = {
            c: {"fx": m[c]["fx"], "fy": m[c]["fy"], "cx": m[c]["cx"],
                "cy": m[c]["cy"]} for c in NAMES}

    # distortion bit-identity across the three vendor datasets
    dist_same = {}
    for c in NAMES:
        a = np.array(fac[c]["kc"], float)
        b = np.array(new[c]["kc"], float)
        d = np.array(cams[c]["kc"], float)
        dist_same[c] = {"ccfA_vs_ccfNEW_max_abs": float(np.abs(a - b).max()),
                        "ccfA_vs_inflash_max_abs": float(np.abs(a - d).max())}

    out = {
        "schema": "einstar.exstar-inflash-dump/1",
        "dump": {"path": DUMP, "size": len(blob),
                 "sha256": hashlib.sha256(blob).hexdigest()},
        "header": {"magic": struct.unpack("<I", blob[0:4])[0],
                   "width": struct.unpack("<I", blob[4:8])[0],
                   "height": struct.unpack("<I", blob[8:12])[0]},
        "records": cams,
        "ccf_block": {"offsets": ccf_offsets, "byte_identical_to_setA": identical,
                      "new_ccf_present": new_in_dump},
        "rig": {f"{a}_{b}": rig(inflash_model[a], inflash_model[b])
                for a, b in pairs},
        "comparison": comparison,
        "distortion_bit_identity": dist_same,
    }
    V.save_json(out, os.path.join(HERE, "inflash_records.json"))

    print(f"dump {out['dump']['sha256'][:16]}  header "
          f"{out['header']}  records at "
          f"{[c['offset'] for c in cams.values()]}")
    print("CCF set A byte-identical inside dump:", identical)
    for a, b in pairs:
        r = out["rig"][f"{a}_{b}"]
        print(f"  in-flash {a}-{b}: {r['baseline_mm']:.4f} mm  "
              f"{r['rel_rot_deg']:.4f} deg")
    for tag in ("ccf_A_20260209", "ccf_NEW_20261114", "linux_candidate"):
        r = comparison[tag]["left_right"]
        print(f"  {tag:22s} LR {r['baseline_mm']:.4f} mm  "
              f"{r['rel_rot_deg']:.4f} deg  "
              f"L-tex {comparison[tag]['left_tex']['baseline_mm']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
