#!/usr/bin/env python3
"""Regression tests for the offline Einstar protocol model.

Run standalone: python3 tests/test_protocol.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "protocol"))
sys.path.insert(0, str(ROOT / "tools" / "firmware"))
sys.path.insert(0, str(ROOT / "tests"))

import einstar_proto as P  # noqa: E402
from support import FIXTURES, Skip, require  # noqa: E402

FW = FIXTURES / "firmware" / "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img"


def test_command_roundtrip() -> None:
    pkt = P.CmdPacket(0x2A, P.GROUP_SYSTEM, P.SYS_FIRMWARE_VERSION)
    buf = pkt.encode()
    assert len(buf) == 64
    assert buf[0] == 0x2A and buf[2] == 0x00 and buf[3] == 0x05 and buf[7] == 0
    back = P.CmdPacket.decode(buf)
    assert (back.packet_id, back.group, back.opcode, back.payload) == (0x2A, 0, 5, b"")


def test_camera_command_payload() -> None:
    # camera 1 set-exposure style payload: mask + u32 BE
    pkt = P.CmdPacket(7, P.GROUP_CAMERA, 0x23, bytes([0x02]) + struct.pack(">I", 2184))
    buf = pkt.encode(16)
    assert len(buf) == 16
    assert buf[7] == 5
    back = P.CmdPacket.decode(buf)
    assert back.payload == bytes([0x02]) + struct.pack(">I", 2184)


def test_payload_overflow_rejected() -> None:
    try:
        P.CmdPacket(0, P.GROUP_CAMERA, 0x23, b"x" * 9).encode(16)
    except P.ProtocolError:
        return
    raise AssertionError("payload > 8 bytes must be rejected")


def _reply(req: P.CmdPacket, status: int, value: bytes) -> bytes:
    buf = bytearray(9 + len(value))
    buf[0] = req.packet_id
    buf[2] = req.group
    buf[3] = req.opcode
    buf[4] = status
    struct.pack_into(">I", buf, 5, len(value))
    buf[9:] = value
    return bytes(buf)


def test_reply_decode_and_check() -> None:
    req = P.CmdPacket(3, P.GROUP_SYSTEM, P.SYS_FIRMWARE_VERSION)
    text = b"EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN\x00"
    rep = P.Reply.decode(_reply(req, 0, text))
    rep.check(req)
    assert rep.text().startswith("EinScan10_01_SC130_FX3_V2.10")
    bad = P.Reply.decode(_reply(req, 0x0D, b""))
    try:
        bad.check(req)
    except P.ProtocolError:
        pass
    else:
        raise AssertionError("non-zero status must raise")


def test_reply_length_beyond_buffer() -> None:
    req = P.CmdPacket(1, 0, 5)
    buf = bytearray(_reply(req, 0, b"abcd"))
    struct.pack_into(">I", buf, 5, 100)
    try:
        P.Reply.decode(buf)
    except P.ProtocolError:
        return
    raise AssertionError("oversized value length must raise")


def test_stream_three_cameras_roundtrip() -> None:
    parser = P.ImageStreamParser()
    frames = []
    for cam in range(3):
        data = bytes([0x10 + cam]) * P.FRAME_BYTES
        pkts = P.make_stream_packets(cam, frame_id=100 + cam, timestamp=1_700_000_000_000 + cam, data=data)
        assert len(pkts) == 32
        for p in pkts:
            frames.extend(parser.feed(p))
    assert len(frames) == 3
    for cam, fr in enumerate(frames):
        assert fr.camera == cam
        assert fr.frame_id == 100 + cam
        assert fr.timestamp == 1_700_000_000_000 + cam
        assert len(fr.data) == P.FRAME_BYTES and fr.data[0] == 0x10 + cam
    assert parser.stats.completed == 3 and parser.stats.missed == 0
    assert parser.stats.by_camera == {0: 1, 1: 1, 2: 1}


def test_stream_loss_is_detected() -> None:
    parser = P.ImageStreamParser()
    data = b"\x00" * P.FRAME_BYTES
    pkts = P.make_stream_packets(0, 1, 1, data)
    out = []
    for i, p in enumerate(pkts):
        if i == 5:
            continue  # drop one mid-frame packet
        out.extend(parser.feed(p))
    assert out == []
    assert parser.stats.missed == 1


def test_stream_bad_length_rejected() -> None:
    parser = P.ImageStreamParser()
    try:
        parser.feed(struct.pack(">I", 10) + b"\x00" * 10)
    except P.ProtocolError:
        assert parser.stats.bad_length == 1
        return
    raise AssertionError("short declared length must raise")


def test_pack_unpack_roundtrip() -> None:
    image = bytes(range(256)) * (0x1000 * 2 // 256)
    packed = P.pack_image(image)
    assert len(packed) == 0x2002
    assert P.unpack_image(packed) == image
    corrupt = bytearray(packed)
    corrupt[10] ^= 1
    try:
        P.unpack_image(bytes(corrupt))
    except P.ProtocolError:
        return
    raise AssertionError("corrupt record must raise")


def test_update_model_on_real_image() -> None:
    raw = require(FW).read_bytes()
    model = P.UpdateModel()
    announce = model.begin(raw)
    assert len(announce) == 0x1400
    assert announce[2] == P.GROUP_SYSTEM and announce[3] == P.SYS_FIRMWARE_UPDATE
    assert struct.unpack_from(">I", announce, 4)[0] == 5
    assert announce[8] == 1
    assert struct.unpack_from(">I", announce, 9)[0] == 0x140000
    chunks = list(model.chunks(raw))
    assert len(chunks) == 320
    assert model.state is P.UpdateState.DONE
    assert model.progress == 1000
    # each chunk carries exactly one 4097-byte source record
    for i, c in enumerate(chunks[:3]):
        assert c[2] == 0 and c[3] == P.SYS_FIRMWARE_UPDATE and c[8] == 0
        assert struct.unpack_from(">I", c, 4)[0] == 0x1001 + 1
        assert c[9:9 + 0x1001] == raw[i * 0x1001 : (i + 1) * 0x1001]


def test_update_model_rejects_corrupt_image() -> None:
    raw = bytearray(require(FW).read_bytes())
    raw[0x10] ^= 0xFF
    model = P.UpdateModel()
    try:
        model.begin(bytes(raw))
    except P.ProtocolError:
        assert model.state is P.UpdateState.IDLE
        return
    raise AssertionError("corrupt record must abort the update before announcing")


def test_update_progress_monotonic() -> None:
    raw = require(FW).read_bytes()
    model = P.UpdateModel()
    model.begin(raw)
    last = -1
    for _ in model.chunks(raw):
        assert model.progress >= last
        last = model.progress
    # the vendor formula saturates at ~950 while streaming and is forced to 1000
    # only when the last chunk has been accepted (FUN_18000bc80).
    assert model.progress == 1000
    assert last <= 1000


def main() -> int:
    failures = 0
    skipped = 0
    names = [n for n in sorted(globals()) if n.startswith("test_") and callable(globals()[n])]
    for name in names:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Skip as exc:
            skipped += 1
            print(f"SKIP {name}: {exc}")
        except BaseException as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(names) - failures - skipped} passed, {skipped} skipped, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
