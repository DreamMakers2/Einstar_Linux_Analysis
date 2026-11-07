#!/usr/bin/env python3
"""Einstar protocol reference model (offline, testable).

Implements the pieces that a Linux stack must get exactly right, independent of any
transport:

  CmdPacket / Reply        command channel framing (EP1)
  ImageStreamParser        EP3 packet framing + per-camera frame reassembly
  UpdateModel              firmware-update state machine (system op 0x06)
  pack_image / unpack_image  round-trip of the 4097-byte record container

Design rules:
  * no I/O in the protocol objects (pure functions over bytes);
  * every rejection raises a specific exception with the offset;
  * the model is exercised by tests/test_protocol.py and by the simulator in driver/.

Field layouts are documented in docs/protocol/einstar_usb_protocol.md.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import Enum

GROUP_SYSTEM = 0x00
GROUP_CAMERA = 0x10

SYS_PRODUCT_NAME = 0x01
SYS_SERIAL = 0x04
SYS_FIRMWARE_VERSION = 0x05
SYS_FIRMWARE_UPDATE = 0x06
SYS_BUTTON_TEMP = 0x07

WIRE_SIZE = {
    (GROUP_SYSTEM, SYS_PRODUCT_NAME): 100,
    (GROUP_SYSTEM, SYS_SERIAL): 64,
    (GROUP_SYSTEM, SYS_FIRMWARE_VERSION): 64,
    (GROUP_SYSTEM, SYS_FIRMWARE_UPDATE): 0x1400,
    (GROUP_SYSTEM, SYS_BUTTON_TEMP): 50,
}

IMAGE_W = 1280
IMAGE_H = 1024
FRAME_BYTES = IMAGE_W * IMAGE_H  # 1_310_720
STREAM_PAYLOAD = 40_960          # 0x A000, 32 per frame
STREAM_HEADER = 32


class ProtocolError(Exception):
    pass


@dataclass
class CmdPacket:
    packet_id: int
    group: int
    opcode: int
    payload: bytes = b""

    def encode(self, wire_size: int | None = None) -> bytes:
        if len(self.payload) > 8:
            raise ProtocolError(f"payload too large for command channel: {len(self.payload)}")
        size = wire_size or WIRE_SIZE.get((self.group, self.opcode), 16)
        if size < 8 + len(self.payload):
            raise ProtocolError(f"wire size {size} cannot carry payload {len(self.payload)}")
        buf = bytearray(size)
        buf[0] = self.packet_id & 0xFF
        buf[1] = 0x00
        buf[2] = self.group
        buf[3] = self.opcode
        buf[4] = 0x00  # status in requests
        buf[7] = len(self.payload)
        buf[8 : 8 + len(self.payload)] = self.payload
        return bytes(buf)

    @staticmethod
    def decode(buf: bytes) -> "CmdPacket":
        if len(buf) < 8:
            raise ProtocolError(f"short command packet: {len(buf)}")
        n = buf[7]
        if 8 + n > len(buf):
            raise ProtocolError("payload length exceeds packet")
        return CmdPacket(buf[0], buf[2], buf[3], bytes(buf[8 : 8 + n]))


@dataclass
class Reply:
    packet_id: int
    group: int
    opcode: int
    status: int
    value: bytes

    LAYOUT = "packet id, const, group, opcode, status, u32 BE value length, value"

    @staticmethod
    def decode(buf: bytes) -> "Reply":
        if len(buf) < 9:
            raise ProtocolError(f"short reply: {len(buf)}")
        n = struct.unpack_from(">I", buf, 5)[0]
        if 9 + n > len(buf):
            raise ProtocolError(f"reply value length {n} exceeds buffer {len(buf)}")
        return Reply(buf[0], buf[2], buf[3], buf[4], bytes(buf[9 : 9 + n]))

    def check(self, request: CmdPacket) -> None:
        if self.packet_id != request.packet_id:
            raise ProtocolError(f"packet id mismatch {self.packet_id} != {request.packet_id}")
        if self.group != request.group or self.opcode != request.opcode:
            raise ProtocolError("group/opcode echo mismatch")
        if self.status != 0:
            raise ProtocolError(f"device status {self.status:#x}")

    def text(self) -> str:
        return self.value.split(b"\x00", 1)[0].decode("latin-1")


# ---------------------------------------------------------------------------
# Image stream
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    camera: int
    frame_id: int
    timestamp: int
    data: bytes


@dataclass
class StreamStats:
    packets: int = 0
    bad_length: int = 0
    missed: int = 0
    completed: int = 0
    dropped_partial: int = 0
    by_camera: dict[int, int] = field(default_factory=dict)


class ImageStreamParser:
    """Packet demultiplexer + per-camera reassembly.

    Packet: [u32 BE len][32-byte header][payload]; len = 32 + payload_len.
    header[2] bits 3..5 = one-hot camera (1,2,4), bits 0..2 = sub-index.
    header[8] == 1 marks the last packet of a frame.
    header[12..15] = u32 BE frame id, header[16..23] = u64 BE timestamp.
    """

    def __init__(self) -> None:
        self.stats = StreamStats()
        self._buf: dict[int, bytearray] = {}
        self._meta: dict[int, tuple[int, int]] = {}

    @staticmethod
    def parse_packet(pkt: bytes) -> tuple[int, int, int, int, bytes]:
        if len(pkt) < 4 + STREAM_HEADER:
            raise ProtocolError(f"short stream packet: {len(pkt)}")
        length = struct.unpack_from(">I", pkt, 0)[0]
        if length < STREAM_HEADER or 4 + length > len(pkt):
            raise ProtocolError(f"bad stream length {length} for packet of {len(pkt)}")
        hdr = pkt[4 : 4 + STREAM_HEADER]
        payload = pkt[4 + STREAM_HEADER : 4 + length]
        cam_sel = (hdr[2] >> 3) & 0x7
        if cam_sel not in (1, 2, 4):
            raise ProtocolError(f"invalid camera selector {cam_sel:#x}")
        camera = {1: 0, 2: 1, 4: 2}[cam_sel]
        frame_id = struct.unpack_from(">I", hdr, 12)[0]
        timestamp = struct.unpack_from(">Q", hdr, 16)[0]
        last = hdr[8] == 1
        return camera, frame_id, timestamp, 1 if last else 0, payload

    def feed(self, pkt: bytes) -> list[Frame]:
        """Feed one packet; return frames completed by it (normally 0 or 1)."""
        self.stats.packets += 1
        try:
            camera, frame_id, ts, last, payload = self.parse_packet(pkt)
        except ProtocolError:
            self.stats.bad_length += 1
            raise
        acc = self._buf.setdefault(camera, bytearray())
        if camera not in self._meta:
            self._meta[camera] = (frame_id, ts)
        if len(acc) + len(payload) > FRAME_BYTES:
            # stream desync: drop and resynchronise on the next last-packet
            acc.clear()
            self._meta[camera] = (frame_id, ts)
            self.stats.dropped_partial += 1
            if not last:
                return []
        acc.extend(payload)
        if not last:
            return []
        if len(acc) != FRAME_BYTES:
            self.stats.missed += 1
            acc.clear()
            self._meta.pop(camera, None)
            return []
        fid, t0 = self._meta.get(camera, (frame_id, ts))
        frame = Frame(camera, fid, t0, bytes(acc))
        acc.clear()
        self._meta.pop(camera, None)
        self.stats.completed += 1
        self.stats.by_camera[camera] = self.stats.by_camera.get(camera, 0) + 1
        return [frame]


def make_stream_packets(camera: int, frame_id: int, timestamp: int, data: bytes,
                        payload: int = STREAM_PAYLOAD) -> list[bytes]:
    """Build the packets the device would send for one frame (for tests/simulator)."""
    if len(data) != FRAME_BYTES:
        raise ProtocolError(f"frame must be {FRAME_BYTES} bytes, got {len(data)}")
    cam_sel = (1, 2, 4)[camera]
    out: list[bytes] = []
    chunks = [data[i : i + payload] for i in range(0, len(data), payload)]
    for i, chunk in enumerate(chunks):
        hdr = bytearray(STREAM_HEADER)
        hdr[2] = (cam_sel << 3) | (i & 0x7)
        hdr[8] = 1 if i == len(chunks) - 1 else 0
        struct.pack_into(">I", hdr, 12, frame_id)
        struct.pack_into(">Q", hdr, 16, timestamp)
        out.append(struct.pack(">I", STREAM_HEADER + len(chunk)) + bytes(hdr) + chunk)
    return out


# ---------------------------------------------------------------------------
# Firmware update state machine
# ---------------------------------------------------------------------------

RECORD = 0x1001


def unpack_image(raw: bytes) -> bytes:
    """Strip the 1-byte checksum after every 4096-byte payload (verifies them)."""
    if len(raw) % RECORD:
        raise ProtocolError(f"size {len(raw)} is not a multiple of 0x1001")
    out = bytearray()
    for i in range(len(raw) // RECORD):
        rec = raw[i * RECORD : (i + 1) * RECORD]
        if (sum(rec[:0x1000]) & 0xFF) != rec[0x1000]:
            raise ProtocolError(f"record {i} checksum mismatch")
        out += rec[:0x1000]
    return bytes(out)


def pack_image(image: bytes) -> bytes:
    """Insert a checksum byte after every 4096 payload bytes."""
    out = bytearray()
    for i in range(0, len(image), 0x1000):
        block = image[i : i + 0x1000]
        if len(block) != 0x1000:
            raise ProtocolError("image size must be a multiple of 0x1000 to pack")
        out += block + bytes([sum(block) & 0xFF])
    return bytes(out)


class UpdateState(Enum):
    IDLE = 0
    ANNOUNCED = 1
    STREAMING = 2
    DONE = 3
    FAILED = 4


UPDATE_WIRE = 0x1400


def make_update_announce(packet_id: int, payload_size: int) -> bytes:
    """System op 0x06 announcement packet (0x1400-byte wire buffer).

    Layout differs from normal commands: bytes 4..7 are the *payload length*
    (5 here, not the byte-7 field), byte 8 is a type/flag, bytes 9..12 the size.
    Verified in einscan10.2.0.0.dll FUN_18000bc80.
    """
    buf = bytearray(UPDATE_WIRE)
    buf[0] = packet_id & 0xFF
    buf[2] = GROUP_SYSTEM
    buf[3] = SYS_FIRMWARE_UPDATE
    struct.pack_into(">I", buf, 4, 5)
    buf[8] = 1
    struct.pack_into(">I", buf, 9, payload_size)
    return bytes(buf)


def make_update_chunk(packet_id: int, chunk: bytes) -> bytes:
    """System op 0x06 data packet: [id][0][0][6][u32 BE len+1][0][data]."""
    if 9 + len(chunk) > UPDATE_WIRE:
        raise ProtocolError(f"chunk of {len(chunk)} bytes does not fit 0x{UPDATE_WIRE:x}")
    buf = bytearray(UPDATE_WIRE)
    buf[0] = packet_id & 0xFF
    buf[2] = GROUP_SYSTEM
    buf[3] = SYS_FIRMWARE_UPDATE
    struct.pack_into(">I", buf, 4, len(chunk) + 1)
    buf[8] = 0
    buf[9 : 9 + len(chunk)] = chunk
    return bytes(buf)


@dataclass
class UpdateModel:
    """Host-side model of the firmware update; emits the real wire packets.

    Sequence (verified in einscan10.2.0.0.dll FUN_18000bc80):
      1. announce: [id][0][0][6][00 00 00 05][01][u32 BE payload_size]
      2. chunks:   [id][0][0][6][u32 BE chunk+1][00][chunk bytes]  (wire 0x1400)
    """

    packet_id: int = 0
    chunk: int = RECORD
    state: UpdateState = UpdateState.IDLE
    total: int = 0
    sent: int = 0
    progress: int = 0
    error: str | None = None

    def _next_id(self) -> int:
        pid = self.packet_id
        self.packet_id = (self.packet_id + 1) % 0xFE
        return pid

    def begin(self, raw_image: bytes) -> bytes:
        if self.state is not UpdateState.IDLE:
            raise ProtocolError(f"cannot begin in state {self.state}")
        image = unpack_image(raw_image)  # verifies every record checksum
        self.total = len(image)
        self.state = UpdateState.ANNOUNCED
        return make_update_announce(self._next_id(), self.total)

    def chunks(self, raw_image: bytes):
        """Yield one command packet per 0x1001-byte source record."""
        if self.state is not UpdateState.ANNOUNCED:
            raise ProtocolError(f"cannot stream in state {self.state}")
        self.state = UpdateState.STREAMING
        for off in range(0, len(raw_image), self.chunk):
            piece = raw_image[off : off + self.chunk]
            self.sent += len(piece)
            # vendor formula: 100 + sent*0x352/total, saturating at 1000
            self.progress = min(1000, 100 + (self.sent * 0x352) // self.total)
            yield make_update_chunk(self._next_id(), piece)
        self.state = UpdateState.DONE
        self.progress = 1000

    def fail(self, reason: str) -> None:
        self.state = UpdateState.FAILED
        self.error = reason


def firmware_version_string(raw: bytes) -> str:
    """Convenience: pull the version string the device reports (system 0x05 reply)."""
    return raw.split(b"\x00", 1)[0].decode("latin-1")
