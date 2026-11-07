#!/usr/bin/env python3
"""Fixture-free tests for every published parser/encoder.

The public repository does not ship proprietary artefacts, so these tests build
synthetic inputs that obey the *documented* formats and check that the tools
accept, reject and round-trip them correctly.  The evidence-backed counterpart
tests (real firmware, real CCF records, the real FPGA payload) live in the other
test modules and skip automatically when fixtures are absent.

Run standalone: python3 tests/test_synthetic.py
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "firmware"))
sys.path.insert(0, str(ROOT / "tools" / "protocol"))
sys.path.insert(0, str(ROOT / "tools" / "ccf"))
sys.path.insert(0, str(ROOT / "tools" / "fpga"))
sys.path.insert(0, str(ROOT / "tests"))

import decode_ccf as C  # noqa: E402
import einstar_proto as P  # noqa: E402
import fpga_bits as F  # noqa: E402
import fx3img  # noqa: E402
import numpy as np  # noqa: E402


# ---------------------------------------------------------------------------
# synthetic artefacts
# ---------------------------------------------------------------------------


def make_fx3_image(data_words=(0x11111111, 0x22222222), addr=0x100, entry=0x40003000) -> bytes:
    n = len(data_words)
    img = bytearray(b"CY" + bytes([0x1C, 0xB0]))
    img += struct.pack("<II", n, addr)
    for w in data_words:
        img += struct.pack("<I", w)
    img += struct.pack("<II", 0, entry)                       # terminator
    img += struct.pack("<I", sum(data_words) & 0xFFFFFFFF)    # dCheckSum
    return bytes(img)


def make_ccf(kind: str, key_mode: str = "proven") -> tuple[bytes, list[float]]:
    """Build a CCF record from a known calibration, keyed the way the vendor does."""
    true = [0.0] * 33
    true[0], true[1] = 1000.0, 1002.0      # fx, fy (square pixels)
    true[4], true[5] = 641.0, 511.0        # cx, cy
    true[8] = 1e-5                         # alpha
    true[10:15] = [-0.16, 0.17, 1e-4, -2e-4, 0.02]
    true[20:23] = [10.0, -20.0, 300.0]     # T
    a = math.radians(5.0)
    R = np.array([[math.cos(a), -math.sin(a), 0.0],
                  [math.sin(a), math.cos(a), 0.0],
                  [0.0, 0.0, 1.0]])
    true[23:32] = list(R.reshape(-1))
    true[32] = 0.05
    key = C.ir_key(key_mode) if kind == "ir" else [0.0] * 33
    stored = [true[i] + key[i] for i in range(33)]
    data = bytearray(struct.pack("<33d", *stored))
    if kind == "ir":
        data += struct.pack("<64i", *([0] * 64))
    return bytes(data), true


def make_pango_payload() -> bytes:
    words = [F.BUS_WIDTH_DETECT, 0x08100020]
    words += [0xFFFFFFFF] * 10
    words.append(F.SYNC_WORD)
    # TYPE1 write ADRR (11) <- 0x02000000
    words += [(5 << 29) | (1 << 27) | (11 << 22) | 1, 0x02000000]
    # TYPE2, 4 data words
    words += [(2 << 29) | 4, 0xAAAAAAAA, 0xBBBBBBBB, 0xCCCCCCCC, 0xDDDDDDDD]
    # TYPE1 write IDR (1) <- IDCODE
    words += [(5 << 29) | (1 << 27) | (1 << 22) | 1, 0x00511899]
    words.append(0xA0000000)  # TYPE1 NOP, count 0
    return struct.pack(">%dI" % len(words), *words)


# ---------------------------------------------------------------------------
# firmware container
# ---------------------------------------------------------------------------


def test_container_roundtrip_and_checksum() -> None:
    base = make_fx3_image()
    image = base + bytes(0x2000 - len(base))
    assert len(image) == 0x2000
    packed = P.pack_image(image)
    assert len(packed) == 0x2002
    assert P.unpack_image(packed) == image
    bad = bytearray(packed)
    bad[5] ^= 0xFF
    try:
        P.unpack_image(bytes(bad))
    except P.ProtocolError:
        return
    raise AssertionError("corrupt record must be rejected")


def test_fx3_parse_checksum_and_elf() -> None:
    img = make_fx3_image()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "synthetic.img"
        p.write_bytes(img)
        fw = fx3img.load(p)
    assert fw.container == "raw"
    assert fw.entry == 0x40003000
    assert fw.stored_checksum == fw.computed_checksum
    assert [s.address for s in fw.sections] == [0x100]
    elf = fx3img.build_elf(fw)
    assert elf[:4] == b"\x7fELF" and elf[4] == 1 and elf[5] == 1
    assert struct.unpack_from("<I", elf, 24)[0] == fw.entry
    phoff = struct.unpack_from("<I", elf, 28)[0]
    assert struct.unpack_from("<I", elf, phoff + 8)[0] == 0x100  # p_vaddr


def test_fx3_checksum_tamper_detected() -> None:
    img = bytearray(make_fx3_image())
    img[-1] ^= 0xFF  # corrupt the stored checksum
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.img"
        p.write_bytes(bytes(img))
        fw = fx3img.load(p)
    assert fw.stored_checksum != fw.computed_checksum


# ---------------------------------------------------------------------------
# protocol model
# ---------------------------------------------------------------------------


def test_update_state_machine_on_synthetic_image() -> None:
    image = bytearray(0x2000)
    image[0:2] = b"CY"
    packed = P.pack_image(bytes(image))
    model = P.UpdateModel()
    announce = model.begin(packed)
    assert struct.unpack_from(">I", announce, 9)[0] == 0x2000
    chunks = list(model.chunks(packed))
    assert len(chunks) == 2
    assert model.state is P.UpdateState.DONE and model.progress == 1000


def test_stream_framing_and_loss() -> None:
    parser = P.ImageStreamParser()
    data = bytes([7]) * P.FRAME_BYTES
    frames = []
    for pkt in P.make_stream_packets(2, 42, 123456789, data):
        frames.extend(parser.feed(pkt))
    assert len(frames) == 1
    assert (frames[0].camera, frames[0].frame_id, frames[0].timestamp) == (2, 42, 123456789)
    assert len(frames[0].data) == P.FRAME_BYTES


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


def test_ccf_ir_key_roundtrip() -> None:
    data, true = make_ccf("ir")
    assert len(data) == C.RECORD_IR_SIZE
    rec = C.decode_record(data, "LeftCCF.txt", key_mode="proven")
    assert rec.kind == "ir"
    assert abs(rec.fx - true[0]) < 1e-9
    assert abs(rec.cx - true[4]) < 1e-9
    R = rec.R
    assert abs(np.linalg.det(R) - 1.0) < 1e-9
    assert np.max(np.abs(R @ R.T - np.eye(3))) < 1e-9
    assert rec.trailer_digits == [0] * 64


def test_ccf_color_is_unkeyed() -> None:
    data, true = make_ccf("color")
    assert len(data) == C.RECORD_COLOR_SIZE
    rec = C.decode_record(data, "TexCCF.txt")
    assert rec.kind == "color"
    assert rec.fx == true[0] and rec.R[0, 0] == true[23]


def test_ccf_rejects_unknown_size() -> None:
    try:
        C.decode_record(b"\0" * 100, "x")
    except ValueError:
        return
    raise AssertionError("unknown record size must be rejected")


def test_camera_centre_geometry() -> None:
    data, true = make_ccf("ir")
    rec = C.decode_record(data, "RightCCF.txt", key_mode="proven")
    C_centre = rec.centre()
    T = np.array(true[20:23])
    R = np.array(true[23:32]).reshape(3, 3)
    assert np.allclose(T, -R @ C_centre, atol=1e-9)


# ---------------------------------------------------------------------------
# FPGA
# ---------------------------------------------------------------------------


def test_pango_stream_parse() -> None:
    payload = make_pango_payload()
    words = list(struct.unpack(">%dI" % (len(payload) // 4), payload))
    header, packets = F.parse(words)
    assert header["bus_width_bits"] == 32
    assert header["padding_words"] == 10
    assert header["sync_word"] == F.SYNC_WORD
    assert any(p.ptype == F.PKT_TYPE1 and p.reg == 1 for p in packets)
    assert any(p.ptype == F.PKT_TYPE2 and p.count == 4 for p in packets)
    # parse consumes the stream exactly
    consumed = sum(p.total_words for p in packets)
    assert 13 + consumed == len(words)


def test_pango_rejects_bad_sync() -> None:
    payload = bytearray(make_pango_payload())
    payload[0x34] = 0x00  # break the sync word
    words = list(struct.unpack(">%dI" % (len(payload) // 4), bytes(payload)))
    try:
        F.parse(words)
    except ValueError:
        return
    raise AssertionError("bad sync word must be rejected")


# ---------------------------------------------------------------------------
# CLIs
# ---------------------------------------------------------------------------


def test_tools_run_without_arguments() -> None:
    """Every CLI must print usage (exit 0 or 2) rather than crash."""
    for tool in ("tools/firmware/fx3img.py", "tools/fpga/fpga_bits.py",
                 "tools/ccf/decode_ccf.py", "tools/firmware/compare_fw.py"):
        r = subprocess.run([sys.executable, str(ROOT / tool), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode in (0, 2), (tool, r.returncode, r.stderr[-300:])
        assert r.stdout or r.stderr


def main() -> int:
    failures = 0
    names = [n for n in sorted(globals()) if n.startswith("test_") and callable(globals()[n])]
    for name in names:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except BaseException as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(names) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
