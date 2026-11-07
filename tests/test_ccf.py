#!/usr/bin/env python3
"""Tests for the Einstar CCF calibration format.

pytest-style (plain ``test_*`` functions with asserts).  pytest is optional::

    pytest tests/test_ccf.py -q
    python3 tests/test_ccf.py          # tiny built-in runner, no pytest needed

The tests are written against the artefacts that ship with this repository plus
the one device capture in ``<private-inputs>`` (skipped when absent).
"""

from __future__ import annotations

import itertools
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/ccf"))

import decode_ccf as C  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from support import FIXTURES  # noqa: E402

CALIB = FIXTURES / "res_Einscan-E10_200x150"
OLD = FIXTURES / "ccf_200x150"
WEB = FIXTURES / "web_extracted"
BIN = FIXTURES / "calibration.bin"
DECODER = ROOT / "tools/ccf/decode_ccf.py"

if not CALIB.exists():  # optional proprietary fixtures: skip the whole module
    import pytest as _pytest

    _pytest.skip(
        f"vendor calibration fixtures not available under {FIXTURES}; "
        "set EINSTAR_FIXTURES to run the evidence-backed suite",
        allow_module_level=True,
    )

COPIES = [CALIB, OLD] + [d / "ccf/200x150" for d in sorted(WEB.glob("EXStar*"))
                         if (d / "ccf/200x150/LeftCCF.txt").exists()]


def load(directory: Path, name: str) -> C.CcfRecord:
    return C.decode_record((directory / f"{name}CCF.txt").read_bytes(),
                           f"{directory.name}/{name}")


# ---------------------------------------------------------------------------
# container / layout
# ---------------------------------------------------------------------------

def test_record_sizes():
    assert (CALIB / "LeftCCF.txt").stat().st_size == 520
    assert (CALIB / "RightCCF.txt").stat().st_size == 520
    assert (CALIB / "TexCCF.txt").stat().st_size == 264


def test_layout_arithmetic():
    # 33 float64 + 64 int32 == 520 ; colour record is exactly 33 float64
    assert C.NUM_FIELDS * 8 + C.TRAILER_INTS * 4 == 520
    assert C.NUM_FIELDS * 8 == 264


def test_trailer_shape():
    for name in ("Left", "Right"):
        rec = load(CALIB, name)
        assert len(rec.trailer_digits) == 64
        assert min(rec.trailer_digits) >= 0
        assert max(rec.trailer_digits) <= 9
    assert load(CALIB, "Tex").trailer_digits == []


def test_trailer_is_a_role_constant_not_calibration():
    # same 256 bytes in two different CCF records of the same role ...
    for name in ("Left", "Right"):
        a, b = load(CALIB, name), load(OLD, name)
        assert a.trailer_digits == b.trailer_digits
        # ... while the calibration doubles differ
        assert a.stored != b.stored
    # ... and Left != Right
    assert load(CALIB, "Left").trailer_digits != load(CALIB, "Right").trailer_digits


def test_unknown_size_rejected():
    try:
        C.decode_record(b"\0" * 100, "x.txt")
    except ValueError:
        return
    raise AssertionError("100-byte record should have been rejected")


# ---------------------------------------------------------------------------
# the key
# ---------------------------------------------------------------------------

def test_color_rotation_needs_no_key():
    tex = load(CALIB, "Tex")
    m = tex.rotation_metrics(tex.stored)
    assert m["orthonormal"], m
    assert abs(m["det"] - 1.0) < 1e-12


def test_stored_ir_rotation_is_not_a_rotation():
    for name in ("Left", "Right"):
        rec = load(CALIB, name)
        m = rec.rotation_metrics(rec.stored)
        assert not m["orthonormal"]
        assert abs(m["det"] - 1.0) > 100          # det ~ -921 / -998
        assert m["max_abs_RRt_minus_I"] > 100


def test_ir_key_restores_rotation_everywhere():
    """The same key must work for every copy of the file that exists on disk."""
    for directory in COPIES:
        for name in ("Left", "Right"):
            rec = load(directory, name)
            m = rec.rotation_metrics()
            assert m["orthonormal"], (directory, name, m)
            assert abs(m["det"] - 1.0) < 1e-12
            assert m["max_abs_RRt_minus_I"] < 1e-12


def test_ir_key_is_the_unique_integer_solution():
    """Only one integer offset vector can make both IR rotations orthonormal."""
    left, right = load(CALIB, "Left"), load(CALIB, "Right")
    ls, rs = np.array(left.stored[23:32]), np.array(right.stored[23:32])
    candidates = [[k for k in range(int(np.floor(v)) - 2, int(np.ceil(v)) + 3)
                   if abs(v - k) <= 1.0] for v in ls]
    solutions = []
    for combo in itertools.product(*[range(len(c)) for c in candidates]):
        key = np.array([candidates[i][combo[i]] for i in range(9)], float)
        good = True
        for stored in (ls, rs):
            m = (stored - key).reshape(3, 3)
            if (np.abs(m @ m.T - np.eye(3)).max() > 1e-9
                    or abs(np.linalg.det(m) - 1.0) > 1e-9):
                good = False
                break
        if good:
            solutions.append(tuple(int(k) for k in key))
    assert solutions == [(-2, -4, -11, -9, 0, 5, -11, -12, 0)], solutions


def test_distortion_key_matches_in_flash_record():
    if not BIN.exists():
        return
    buf = BIN.read_bytes()
    recs22 = [struct.unpack_from("<22d", buf, C.CAMERA_RECORD_OFF + i * C.CAMERA_RECORD_STRIDE)
              for i in range(3)]
    key = np.array([0, 6, 2, 7, 13], float)
    for name, v in zip(("Left", "Right", "Tex"), recs22):
        stored = np.array(load(CALIB, name).stored[10:15])
        expected = key if name != "Tex" else np.zeros(5)
        assert np.allclose(stored - v[4:9], expected, atol=1e-12), (name, stored - v[4:9])


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

def test_intrinsics():
    for directory in COPIES:
        for name in ("Left", "Right", "Tex"):
            r = load(directory, name)
            assert 500 < r.fx < 3000 and 500 < r.fy < 3000
            assert 0 < r.cx < C.IMAGE_W and 0 < r.cy < C.IMAGE_H
            assert abs(r.fx / r.fy - 1) < 5e-3
            assert abs(r.alpha) < 1e-2


def test_square_pixels_for_the_colour_camera():
    tex = load(CALIB, "Tex")
    assert abs(tex.fx - tex.fy) < 0.1


def test_distortion_ranges():
    for name in ("Left", "Right", "Tex"):
        k1, k2, p1, p2, k3 = load(CALIB, name).kc
        assert -1 < k1 < 1 and -1 < k2 < 1 and -1 < k3 < 1
        assert abs(p1) < 1e-2 and abs(p2) < 1e-2


def test_stereo_baseline():
    left, right, tex = (load(CALIB, n) for n in ("Left", "Right", "Tex"))
    b = np.linalg.norm(left.centre() - right.centre())
    assert 50 < b < 250, b
    # the rigid rig triangle is reproduced by both encodings of the same unit
    if BIN.exists():
        buf = BIN.read_bytes()
        cents = []
        for i in range(3):
            v = struct.unpack_from("<22d", buf, C.CAMERA_RECORD_OFF + i * C.CAMERA_RECORD_STRIDE)
            R = np.array(v[10:19]).reshape(3, 3)
            cents.append(-R.T @ np.array(v[19:22]))
        for a, b in ((0, 1), (0, 2), (1, 2)):
            assert abs(np.linalg.norm(cents[a] - cents[b])
                       - np.linalg.norm([left, right, tex][a].centre()
                                        - [left, right, tex][b].centre())) < 3.0


def test_relative_pose_is_consistent():
    left, right = load(CALIB, "Left"), load(CALIB, "Right")
    Rrel = left.R @ right.R.T
    trel = left.T - Rrel @ right.T
    assert abs(np.linalg.norm(trel) - np.linalg.norm(left.centre() - right.centre())) < 1e-9


# ---------------------------------------------------------------------------
# the device dump
# ---------------------------------------------------------------------------

def test_bin_container():
    if not BIN.exists():
        return
    buf = BIN.read_bytes()
    assert len(buf) == 6568
    assert struct.unpack_from("<3I", buf, 0) == (1, 1280, 1024)
    block = C.locate_ccf_block(buf)
    assert struct.unpack_from("<I", buf, block)[0] == 4
    assert buf[block + 0xF1C:block + 0xF20] == b"FQFQ"
    for name, rel, size in (("left", 0x108, 520), ("right", 0x610, 520), ("tex", 0xB18, 264)):
        assert struct.unpack_from("<I", buf, block + rel)[0] == size
        payload = buf[block + rel + 4: block + rel + 4 + size]
        assert payload == (CALIB / f"{name.capitalize()}CCF.txt").read_bytes()


def test_bin_cli_json():
    if not BIN.exists():
        return
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        out = Path(fh.name)
    try:
        subprocess.check_call([sys.executable, str(DECODER), "--bin", str(BIN),
                               "--json-out", str(out)], stdout=subprocess.DEVNULL)
        data = json.loads(out.read_text())
        assert data["ccf_block_offset"] == 0x39B
        assert data["ccf_block_type"] == 4
        assert data["header"] == {"u32_0": 1, "width": 1280, "height": 1024}
        for name in ("left", "right", "tex"):
            rec = data["records"][name]
            assert rec["rotation_check_decoded"]["orthonormal"]
            assert rec["rotation_check_decoded"]["det"] > 0.999999999
    finally:
        out.unlink(missing_ok=True)


def test_cli_txt_json():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        out = Path(fh.name)
    try:
        subprocess.check_call([sys.executable, str(DECODER), "--txt",
                               str(CALIB / "LeftCCF.txt"), "--json-out", str(out)],
                              stdout=subprocess.DEVNULL)
        rec = json.loads(out.read_text())["record"]
        assert rec["kind"] == "ir"
        assert abs(rec["intrinsics"]["fx"] - 1159.115416) < 1e-5
        assert rec["rotation_check_decoded"]["orthonormal"]
    finally:
        out.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# minimal runner (pytest is not a hard dependency)
# ---------------------------------------------------------------------------

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
