#!/usr/bin/env python3
"""Assert the structural and geometric invariants of the Einstar CCF format.

Every check below is backed by bytes on disk; run with ``--verbose`` to see the
measured numbers behind each verdict.  Exit status is non-zero if any invariant
fails.

    python3 tools/ccf/validate_ccf.py
    python3 tools/ccf/validate_ccf.py --bin <device-dump.bin>
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decode_ccf as C  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CALIB = REPO / "sources/calibration/res_Einscan-E10_200x150"
DEFAULT_OLD = REPO / "sources/calibration/ccf_200x150"
DEFAULT_BIN = Path("<device-dump.bin>")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _rel_rot(R1: np.ndarray, R2: np.ndarray) -> float:
    R = R1 @ R2.T
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1.0) / 2.0))))


def run_checks(calib_dir: Path, bin_path: Path | None,
               old_dir: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    add = lambda n, ok, d: checks.append(Check(n, bool(ok), d))  # noqa: E731

    left_p, right_p, tex_p = (calib_dir / f for f in
                              ("LeftCCF.txt", "RightCCF.txt", "TexCCF.txt"))
    recs = {n: C.decode_record(p.read_bytes(), str(p))
            for n, p in (("left", left_p), ("right", right_p), ("tex", tex_p))}

    # ---- 1. sizes -------------------------------------------------------
    add("record sizes are 520 / 520 / 264",
        (recs["left"].size, recs["right"].size, recs["tex"].size) == (520, 520, 264),
        f"left={recs['left'].size} right={recs['right'].size} tex={recs['tex'].size}")
    add("520 = 33*float64 + 64*int32",
        recs["left"].size == C.NUM_FIELDS * 8 + C.TRAILER_INTS * 4,
        f"{C.NUM_FIELDS * 8} + {C.TRAILER_INTS * 4} = {C.NUM_FIELDS * 8 + C.TRAILER_INTS * 4}")

    # ---- 2. trailer -----------------------------------------------------
    for n in ("left", "right"):
        t = recs[n].trailer_digits
        add(f"{n}: trailer = 64 int32 in 0..9",
            len(t) == 64 and min(t) >= 0 and max(t) <= 9,
            f"n={len(t)} range={min(t)}..{max(t)}")
    add("tex: no trailer", not recs["tex"].trailer_digits,
        "264 B record holds only the 33 doubles")
    add("left and right trailers differ", recs["left"].trailer_digits != recs["right"].trailer_digits,
        "role-specific constant, not per-unit calibration data")

    # ---- 3. rotation orthonormality (the central invariant) --------------
    for n in ("left", "right", "tex"):
        m = recs[n].rotation_metrics()
        add(f"{n}: det(R)=+1 and ||RR^T-I||_max < 1e-9",
            m["orthonormal"],
            f"det={m['det']:.12f} max={m['max_abs_RRt_minus_I']:.3e} "
            f"F={m['frobenius_RRt_minus_I']:.3e}")
    for n in ("left", "right"):
        ms = recs[n].rotation_metrics(recs[n].stored)
        add(f"{n}: STORED 3x3 is not a rotation (key is required)",
            abs(ms["det"] - 1.0) > 1e-3 and ms["max_abs_RRt_minus_I"] > 1e-3,
            f"det={ms['det']:.6f} max|RR^T-I|={ms['max_abs_RRt_minus_I']:.3f}")

    # ---- 4. intrinsics ---------------------------------------------------
    for n in ("left", "right", "tex"):
        r = recs[n]
        ok = (500 < r.fx < 3000 and 500 < r.fy < 3000
              and 0 < r.cx < C.IMAGE_W and 0 < r.cy < C.IMAGE_H
              and abs(r.fx / r.fy - 1) < 5e-3 and abs(r.alpha) < 1e-2)
        add(f"{n}: intrinsics plausible for {C.IMAGE_W}x{C.IMAGE_H}",
            ok, f"fx={r.fx:.3f} fy={r.fy:.3f} fx/fy={r.fx / r.fy:.6f} "
                f"cx={r.cx:.3f} cy={r.cy:.3f} alpha={r.alpha:.3e}")
    add("colour lens has square pixels (fx == fy)", 
        abs(recs["tex"].fx / recs["tex"].fy - 1) < 1e-4,
        f"fx-fy={recs['tex'].fx - recs['tex'].fy:+.6f} px")
    add("principal points lie within 60 px of the image centre",
        all(abs(recs[n].cx - C.IMAGE_W / 2) < 60 and abs(recs[n].cy - C.IMAGE_H / 2) < 60
            for n in recs),
        "; ".join(f"{n}: dx={recs[n].cx - C.IMAGE_W / 2:+.1f} "
                  f"dy={recs[n].cy - C.IMAGE_H / 2:+.1f}" for n in recs))

    # ---- 5. distortion ---------------------------------------------------
    for n in ("left", "right", "tex"):
        k1, k2, p1, p2, k3 = recs[n].kc
        ok = all(abs(v) < 1.0 for v in (k1, k2, k3)) and abs(p1) < 0.01 and abs(p2) < 0.01
        add(f"{n}: distortion coefficients in physical range", ok,
            f"k1={k1:+.6f} k2={k2:+.6f} p1={p1:+.2e} p2={p2:+.2e} k3={k3:+.6f}")

    # ---- 6. pose / stereo ------------------------------------------------
    add("translations are ~0.25-0.30 m from the origin",
        all(200 < np.linalg.norm(recs[n].T) < 400 for n in recs),
        "; ".join(f"{n}: |T|={np.linalg.norm(recs[n].T):.3f}" for n in recs))
    b = float(np.linalg.norm(recs["left"].centre() - recs["right"].centre()))
    add("IR stereo baseline in 50..250 mm", 50 < b < 250, f"|C_left - C_right| = {b:.3f} mm")
    add("IR pair relative rotation < 45 deg",
        _rel_rot(recs["left"].R, recs["right"].R) < 45,
        f"{_rel_rot(recs['left'].R, recs['right'].R):.3f} deg")
    Rrel = recs["left"].R @ recs["right"].R.T
    trel = recs["left"].T - Rrel @ recs["right"].T
    add("relative translation reproduces the baseline",
        abs(np.linalg.norm(trel) - b) < 1e-6,
        f"|t_rel|={np.linalg.norm(trel):.6f} vs {b:.6f}")

    # ---- 7. the device dump ---------------------------------------------
    if bin_path and bin_path.exists():
        buf = bin_path.read_bytes()
        add("dump size is 6568 bytes", len(buf) == 6568, f"{len(buf)}")
        hdr = struct.unpack_from("<3I", buf, 0)
        add("dump header = (1, 1280, 1024)", hdr == (1, 1280, 1024), f"{hdr}")
        block = C.locate_ccf_block(buf)
        add("CCF block type == 4 at 'FQFQ'-0xF1C",
            struct.unpack_from("<I", buf, block)[0] == 4
            and buf[block + C.CCF_BLOCK_MAGIC_OFF:block + C.CCF_BLOCK_MAGIC_OFF + 4] == b"FQFQ",
            f"block @ {block:#x}, magic @ {block + C.CCF_BLOCK_MAGIC_OFF:#x}")
        for name, rel in C.CCF_SLOT_OFFSETS.items():
            abs_off = block + rel
            declared = struct.unpack_from("<I", buf, abs_off)[0]
            want = C.CCF_SLOT_SIZES[name]
            payload = buf[abs_off + 4: abs_off + 4 + want]
            same = payload == (calib_dir / f"{name.capitalize()}CCF.txt").read_bytes() \
                if name != "tex" else payload == (calib_dir / "TexCCF.txt").read_bytes()
            add(f"{name} slot: u32 length {want} and bytes identical to the .txt file",
                declared == want and same,
                f"@{abs_off:#x} len={declared} identical={same}")
        # 22-double records
        recs22 = []
        for i in range(3):
            off = C.CAMERA_RECORD_OFF + i * C.CAMERA_RECORD_STRIDE
            v = struct.unpack_from("<22d", buf, off)
            R = np.array(v[10:19]).reshape(3, 3)
            recs22.append((off, np.array(v), R))
            add(f"22-double record {i} @ {off:#x} is a rotation",
                abs(np.linalg.det(R) - 1) < 1e-9 and np.abs(R @ R.T - np.eye(3)).max() < 1e-9,
                f"det={np.linalg.det(R):.12f} max={np.abs(R @ R.T - np.eye(3)).max():.2e}")
        # exact-integer relation for the distortion block
        KD = np.array([0, 6, 2, 7, 13], float)
        for n, (off, v, R) in zip(("left", "right", "tex"), recs22):
            # stored CCF doubles minus the 22-double record must be exactly the key
            d = np.array(recs[n].stored[10:15]) - v[4:9]
            add(f"{n}: stored kc - 22-double kc == integer key "
                f"(0,6,2,7,13)" if n != "tex" else f"{n}: stored kc == 22-double kc",
                np.allclose(d, KD if n != "tex" else 0.0, atol=1e-12),
                "difference=" + np.array2string(d, precision=3))
        # rig geometry: the three camera centres form a rigid triangle
        cents22 = [(-R.T @ v[19:22]) for _, v, R in recs22]
        cents_ccf = [recs['left'].centre(), recs['right'].centre(), recs['tex'].centre()]
        pairs = ((0, 1), (0, 2), (1, 2))
        d22 = [float(np.linalg.norm(cents22[a] - cents22[b])) for a, b in pairs]
        dcc = [float(np.linalg.norm(cents_ccf[a] - cents_ccf[b])) for a, b in pairs]
        worst = max(abs(a - b) for a, b in zip(d22, dcc))
        add("22-double rig triangle agrees with the decoded CCF rig (<3 mm)",
            worst < 3.0,
            "dcm=" + ", ".join(f"{x:.3f}" for x in d22)
            + "  ccf=" + ", ".join(f"{x:.3f}" for x in dcc)
            + f"  worst={worst:.3f} mm")

    # ---- 8. copy-to-copy stability of the trailer ------------------------
    if old_dir and old_dir.exists():
        for n in ("left", "right"):
            p = old_dir / f"{n.capitalize()}CCF.txt"
            if not p.exists():
                continue
            o = C.decode_record(p.read_bytes(), str(p))
            add(f"{n}: trailer identical in the older copy while the doubles differ",
                o.trailer_digits == recs[n].trailer_digits and o.stored != recs[n].stored,
                "trailer is a role constant, the first 256 B are per-calibration")
            m = o.rotation_metrics()
            add(f"{n}: older copy also orthonormal after the same key",
                m["orthonormal"],
                f"det={m['det']:.12f} max={m['max_abs_RRt_minus_I']:.3e}")

    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calib-dir", type=Path, default=DEFAULT_CALIB)
    ap.add_argument("--old-dir", type=Path, default=DEFAULT_OLD)
    ap.add_argument("--bin", type=Path, default=DEFAULT_BIN)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    checks = run_checks(args.calib_dir, args.bin, args.old_dir)
    width = max(len(c.name) for c in checks)
    failed = 0
    print(f"CCF validation  ({args.calib_dir})")
    print("-" * (width + 12))
    for c in checks:
        if not c.ok:
            failed += 1
        print(f"[{'PASS' if c.ok else 'FAIL'}] {c.name:<{width}}  {c.detail}")
    print("-" * (width + 12))
    print(f"{len(checks) - failed}/{len(checks)} checks passed"
          + ("" if failed == 0 else f"  ({failed} FAILED)"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
