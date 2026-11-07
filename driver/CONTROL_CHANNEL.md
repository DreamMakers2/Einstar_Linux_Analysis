# Einstar control channel

`driver/include/einstar/control_channel.hpp` + `src/control_channel.cpp` implement the
command/control half of the Einstar (EinScan10 / E10) USB protocol. It is the only code in
the backend that speaks the command pipe, and it is deliberately independent of the image
pipeline: the channel never submits streaming reads and never touches endpoint `0x83`.

Every constant and opcode below comes from the two recovered specifications, not from
guesswork:

* `docs/protocol/libeinstar_protocol_spec.md` (code-verified against the Linux driver that
  ran on hardware),
* `docs/protocol/einstar_usb_protocol.md` (vendor binary + libeinstar consolidation).

Where the two sources disagree, the resolution is listed under
[Protocol ambiguities](#protocol-ambiguities).

## Boundary and ownership

| Concern | Owner |
|---|---|
| `Transport::open/close` of the USB device | application / pipeline |
| Interface 0 claim | `ControlChannel::open()` (configurable) |
| Command pipe `0x01` / `0x81` | `ControlChannel` only |
| Bulk pair `0x02` / `0x82` | page read only (synchronous, finite) |
| Image stream `0x83` | pipeline only |

`ControlChannel` never opens or closes the shared `Transport` and never clears the image
endpoint. `close()` releases the interface if the channel claimed it and forgets the cached
identity; it is idempotent and safe from the destructor.

## Wire format

Request (EP `0x01`, interrupt OUT, zero padded to the per-opcode wire size):

```
0    1  packet id      rolling 0..253 (mod 254), fresh value per attempt
1    1  0x00           vendor constant; the device does not validate it
2    1  group          0x00 system, 0x10 camera
3    1  opcode
4    4  u32 BE         argument = payload length
8    L  payload
```

Reply (EP `0x81`, interrupt IN):

```
0    1  packet id echo
1    1  0x02 marker    observed; not validated (the vendor does not either)
2    1  group echo
3    1  opcode echo
4    1  status         0 = success, anything else is a device error
5    4  u32 BE         value length N
9    N  value          big-endian; NUL-terminated strings for string replies
```

The channel validates id, group and opcode echoes and requires `status == 0`. A non-zero
status is a device decision and is **not** retried; it is reported as `ErrorCode::Protocol`
with the status value and opcode in the message. Timeouts and echo mismatches are retried.

## API surface

All fallible calls return `Result<T>` / `Status`; nothing throws.

| Method | Opcode(s) | Purpose |
|---|---|---|
| `open()` | `0xFF` probe, sys `0x04`, `0x05`, cam `0x51` | claim, drain, handshake, cache identity |
| `close()` | — | release the interface, clear state |
| `identity()` | — | cached `{serial, firmware, sensor_count}` |
| `product_name()` | sys `0x01` | `"EinScan10_01"` |
| `serial()` / `firmware_version()` / `sensor_count()` | sys `0x04` / `0x05`, cam `0x51` | fresh reads |
| `max_width(cam)` / `max_height(cam)` | `0x16` / `0x17` | `u16 BE`, mask payload |
| `exposure_us(cam)` / `set_exposure_us(cam, us)` | `0x22` / `0x23` | `u32 BE` microseconds |
| `exposure_range(cam)` | `0x21` min, `0x20` max | `u32 BE` |
| `gain(cam)` / `set_gain(cam, value)` | `0x26` / `0x27` | `u16 BE` |
| `gain_range(cam)` | `0x25` min, `0x24` max | `u16 BE` |
| `trigger_counts()` / `set_trigger_counts(mono, rgb)` | `0x40` / `0x41` | `(rgb << 4) \| mono` |
| `trigger_period_us()` / `set_trigger_period_us(us)` | `0x48` / `0x49` | `u32 BE` microseconds |
| `temperature_raw()` / `temperature_celsius()` | `0x50` | `i16 BE >> 7` |
| `read_page(index, destination)` | `0x57` + bulk `0x02 -> 0x82` | 4096-byte read-only flash page |
| `start_streaming(period)` / `stop_streaming()` | `0x49`, `0x41` | command-level trigger sequence |

Per-camera commands take a 0-based camera index and build `mask = 1 << index`
(`0x01`, `0x02`, `0x04`). Indices outside `0..2` are rejected with
`ErrorCode::InvalidArgument` before any I/O.

Free helpers (`camera_mask`, `pack_trigger_counts`, `unpack_trigger_counts`,
`parse_firmware_version`) are pure and directly unit-tested. `FirmwareVersion` splits
`EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN` into
`{model="EinScan10_01", sensor="SC130", fx3="2.10", fpga="3.7", locale="EN"}` and keeps
`raw`; an unrecognised string yields only `raw` (`parsed() == false`), it is not an error.

## Opcode table mapped to the specification

Wire sizes are per-command interrupt transfer lengths (`libeinstar_protocol_spec.md` §3.3).

| Group | Op | Name | Wire | Request payload | Reply value | Spec conf. |
|---|---|---|---|---|---|---|
| sys | `0x01` | ProductName | 100 | — | NUL-terminated string | CV layout |
| sys | `0x04` | Serial | 64 | — | 8 bytes -> 16 hex digits | CV + HW |
| sys | `0x05` | FirmwareVersion | 64 | — | NUL-terminated string | CV + HW |
| cam | `0x16` | MaxWidth | 16 | mask u8 | u16 BE (`0x0500` = 1280) | CV |
| cam | `0x17` | MaxHeight | 16 | mask u8 | u16 BE (1024) | CV |
| cam | `0x20` | MaxExposure | 16 | mask u8 | u32 BE | CD (declared, no call site) |
| cam | `0x21` | MinExposure | 16 | mask u8 | u32 BE | CD |
| cam | `0x22` | GetExposure | 16 | mask u8 | u32 BE, 2184 default | CV |
| cam | `0x23` | SetExposure | 16 | mask + u32 BE (L=5) | empty | CV |
| cam | `0x24` | MaxGain | 16 | mask u8 | u16 BE | CD |
| cam | `0x25` | MinGain | 16 | mask u8 | u16 BE | CD |
| cam | `0x26` | GetGain | 16 | mask u8 | u16 BE, 500 default | CV |
| cam | `0x27` | SetGain | 16 | mask + u16 BE (L=3) | empty | CV |
| cam | `0x40` | GetTriggerSwitch | 10 | — | u8 = `(rgb<<4)\|mono` | CV |
| cam | `0x41` | SetTriggerSwitch | 10 | packed u8 (L=1) | empty | CV |
| cam | `0x48` | GetTriggerPeriod | 50 | — | u32 BE | CV |
| cam | `0x49` | SetTriggerPeriod | 50 | u32 BE (L=4) | empty | CV |
| cam | `0x50` | Temperature | 16 | — | i16 BE, `>> 7` | CV decode / INF units |
| cam | `0x51` | SensorCount | 16 | — | u8 (3 observed) | CV |
| cam | `0x57` | PageRead | 16 + bulk | u16 BE page index (L=2) | 4096 bytes at reply+9 | CV |
| — | `0xFF` | Handshake | 100 | byte 0 = `0xFF` | `"Shining3"` at +9 | CV + HW |

The range opcodes (`0x20/0x21/0x24/0x25`) are declared in the code-verified table and
documented by the vendor as `CameraExposureRange` / `CameraGainRange`, but libeinstar never
issues them. They are implemented, and a device that rejects them surfaces
`ErrorCode::Protocol`; callers should treat a failure as "range unknown", not fatal.

## Transport integration

`Transport` (transport.hpp) models the inbound streaming path: it can submit bulk-IN reads
but has **no outbound write and no interrupt-IN primitive**. The command channel therefore
declares `CommandIo`, an optional transport capability with four synchronous, deadline-
bounded operations (`write_interrupt`, `read_interrupt`, `write_bulk`, `read_bulk`), and
discovers it in `open()` with `dynamic_cast`. A transport without it is rejected with
`ErrorCode::Unsupported`; nothing falls back to raw libusb.

`LibUsbTransport` implements `CommandIo` (`src/libusb_transport.cpp`): the four calls use
`libusb_interrupt_transfer` / `libusb_bulk_transfer` with the caller's deadline. This is
compile- and link-verified; it has not yet run against a scanner. Two details matter on real
hardware:

* `write_bulk` must split the 4096-byte page request into `0x400`-byte transfers (the vendor
  sends 1 KiB chunks with a 1 ms gap); libeinstar does the same.
* `read_bulk` must loop until the requested 0x1400 bytes arrive, because the device answers
  a page read in several transfers.

The channel is exercised offline by `FakeDevice` (22 cases / 4,379 checks) and is ready for
hardware bring-up; the fake device is a byte-accurate FX3 model, not a mock of our own code.
implements the capability.

### Robustness

* Fixed 10 KiB scratch (`0x1400` request + `0x1400` reply) owned by the channel; the
  success path allocates nothing. Only identity strings and error messages allocate.
* `ControlConfig::attempts` = 5 total attempts (vendor loop `for (attempt < 5)`); each
  attempt uses a fresh packet id, so a late reply to an earlier attempt is rejected by the
  echo check.
* After every failed attempt both command pipes are recovered with
  `Transport::clear_halt(0x01)` and `(0x81)`; page reads clear `0x02`/`0x82`.
* Per-operation deadlines (`timeout_ms`, `handshake_timeout_ms`, `page_timeout_ms`,
  `drain_timeout_ms`); all are validated non-zero in `open()` because a zero timeout is
  "infinite" for libusb. The only wait is the configured 20 ms retry delay, and it never
  happens on the success path.
* Whole operations are serialised by an internal mutex, so a control plane and a diagnostics
  thread may share one channel. The API is not re-entrant.

## Not implemented (and why)

| Omitted | Opcode | Reason |
|---|---|---|
| Flash page write | cam `0x58` | Destroys factory calibration; not needed for acquisition. Not representable in the API. |
| Firmware update | sys `0x06` | Irreversible, can brick the FX3; needs the vendor's record/checksum format and a recovery path. |
| Reboot | sys `0x08` | Persistent side effect; must not be reachable from the control plane. |
| XOR command mask | — | Optional in libeinstar (`mask_commands = false` by default); whether this firmware requires it is unknown. Adding it would change every golden vector. |
| Vendor `ImageStream` property | unknown | No opcode was recovered. The command-level equivalent (trigger period + trigger counts) is `start_streaming()`; bulk streaming stays in the pipeline. |
| 1 s settle after stop | — | libeinstar sleeps 1 s after disabling triggers. A hidden long sleep in a control-plane call is worse than making the caller own it. |
| Calibration blob parsing | — | `read_page()` provides the bytes; interpreting the 6568-byte blob belongs to the calibration layer. |
| C ABI | — | Designed for it (fixed-width scalars, caller-owned spans, `Result<T>`, no callbacks), but not implemented in this change. |

The opcode enums deliberately contain no constant for a write, reboot or update command, so
those operations cannot be expressed at all - stronger than a runtime check.

## Protocol ambiguities and resolutions

1. **Reply value-length field.** libeinstar reads a single byte at offset 8; the vendor reads
   a 32-bit big-endian length from offsets 5..8. Both agree for every observed reply because
   bytes 5..7 are zero. Resolution: read the full `u32 BE` at 5..8 and clamp it to the bytes
   actually received (the `truncated_values` counter makes clamping visible).
2. **"5 retries".** The vendor retry loop runs five iterations, and libeinstar calls that
   value `retries`, although it is the total attempt count. Resolution: `attempts = 5`
   total attempts, configurable; `stats.retries` counts attempts beyond the first.
3. **Request byte 1.** The vendor sends `0x00`, libeinstar sends `0x02`, and the device
   validates neither. Resolution: send `0x00` and never validate the echoed byte.
4. **Serial length.** The serial reply carries 12 value bytes, but only the first 8 are the
   serial (`... 44 00 00 00` trailer). Resolution: render the first 8 bytes as uppercase hex,
   matching EXStar (a synthetic `0123456789ABCDEF`).
5. **Page reply length.** Byte 8 is `0x00` in a page reply even though 4096 data bytes
   follow at offset 9, so the generic value-length field is meaningless there. Resolution:
   the page path ignores it and requires `transferred >= 9 + 4096`, then validates the echo
   and status like any other reply.
6. **Temperature units.** `i16 BE >> 7` is code-verified; the unit is inferred. Resolution:
   expose `temperature_raw()` (shifted integer, 0.5 degC units) and
   `temperature_celsius()` (`raw * 0.5`), documenting the inference.
7. **Firmware tokenisation.** A naive `_`-split breaks `FX3_V2.10` and `FPGA_V3.7` into two
   tokens each. Resolution: locate the `FX3_V` / `FPGA_V` markers in the raw string and take
   the version up to the next `_`.
8. **Page read on the bulk pair.** The device expects 1 KiB request chunks and answers with
   several transfers. Resolution: `CommandIo` puts that chunking obligation on the backend
   (documented contract) instead of hard-coding transfer sizes in the channel.

## Tests

`driver/tests/test_control_channel.cpp` runs 22 cases (no hardware, no libusb) against
`FakeDevice`, an in-memory FX3 that decodes requests with the firmware's offsets and wire
sizes and models per-camera exposure/gain, trigger state, page contents, status errors,
dropped replies, corrupted echoes and the handshake. Coverage:

* golden request bytes for 8 opcodes plus the handshake, serial, firmware and sensor queries
  (wire size, argument, payload and padding all checked);
* firmware-string parsing (exact, no locale, `2.14`/`CH`, garbage);
* trigger packing `(rgb<<4)|mono` and nibble rejection before any I/O;
* exposure and gain round-trips per camera, ranges, invalid camera indices;
* retry on a dropped reply (new packet id, one retry), exhausted timeout (exactly
  `attempts` transfers), bad status (fatal after one attempt), corrupted echo (two retries);
* page read: destination bounds, request header on the bulk pipe, 4096-byte content,
  page `0xFFFF`, status error, echo-mismatch retry;
* stream start/stop command sequence, trigger state on the fake, and no bulk traffic;
* close releases the interface and leaves no outstanding transfers, reopen works;
* open on a closed transport, on a transport without `CommandIo`, on a zero-deadline
  configuration (rejected before any I/O) and on a failed handshake (interface released, no
  identity command sent).

Run with:

```
cmake -S driver -B build/x && cmake --build build/x -j16
ctest --test-dir build/x --output-on-failure
```
