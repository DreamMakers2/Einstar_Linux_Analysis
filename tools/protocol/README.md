# Offline Einstar protocol model

`einstar_proto.py` is a transport-free reference implementation of the wire protocol,
used by the tests and by the acquisition simulator. It is deliberately small and
dependency-free so it can serve as an executable specification.

| Object | Responsibility |
|---|---|
| `CmdPacket` | command-channel request framing (`id, const, group, opcode, status, len, payload`) |
| `Reply` | reply parsing (`u32 BE` value length at offset 5, value at 9) + echo/status checking |
| `ImageStreamParser` | EP3 packet demux, per-camera reassembly, last-packet validation, gap accounting |
| `make_stream_packets` | synthesise a device-like frame stream (tests, simulator fixtures) |
| `UpdateModel` | firmware-update state machine: announce + 0x1001-byte chunks + vendor progress formula |
| `pack_image` / `unpack_image` | round-trip of the 4097-byte record container with checksum verification |

Run the tests:

```bash
python3 tests/test_protocol.py          # standalone, 12 tests
python3 -m pytest -q tests/             # whole repository suite, 40 tests
```

Notes:

* The firmware-update command does **not** use the normal 8-byte-payload header: bytes 4..7
  are a `u32 BE` length and byte 8 is a type flag (verified in
  `FUN_18000bc80`). `make_update_announce` / `make_update_chunk` encode this exactly.
* `ImageStreamParser` keys its accumulator by camera. The vendor keeps a single accumulator
  and relies on the stream being grouped; per-camera keying is strictly more robust and is
  what the Linux backend should do.
* Timestamps are latched from the first packet of a frame; the frame id is taken from the
  last packet (that is where the vendor marks it valid).
