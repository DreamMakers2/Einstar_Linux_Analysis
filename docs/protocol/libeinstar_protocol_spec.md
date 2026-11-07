# libeinstar — SHINING 3D Einstar (EinScan10 / E10) USB protocol specification

Byte-level specification reconstructed **from the libeinstar source only**, with
the repository's own `docs/PROTOCOL.md` used only where the code confirms it.
Every statement below is tagged with a confidence level and a `file:line`
reference. Nothing is copied from vendor binaries or invented.

- Source audited: `<libeinstar>` @ commit `4564bb19` (working tree has
  two uncommitted `#include <algorithm>` fixes, see §12).
- Reference hardware in the repo's own notes: Einstar, firmware
  `EinScan10_01_SC130_FX3_V2.14_FPGA_V3.7_EN` (README:10, docs/PROTOCOL.md:5).
  The firmware images supplied with this workspace are
  `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img`; the driver never checks
  or parses the version string (§10, §11).
- Line numbers are 1-based and refer to the audited working tree.

## Confidence legend

| Tag | Meaning |
|---|---|
| **CV** | Code-verified: the layout is built or parsed by libeinstar code; a unit test or `file:line` shows the exact bytes/offsets. |
| **CD** | Code-declared: the opcode/constant exists in the source but has no call site in this library. Semantics come from the repository's reverse-engineering notes (`docs/PROTOCOL.md`, tagged `[der]`/`[hw]`/`[obs]` there). |
| **INF** | Inferred: a reasonable reading of code/comment, not proven by code or a test. |
| **UNK** | Unknown: not determinable from the source. |

Where the repository's docs and the code disagree, the code is authoritative and
the disagreement is called out.

---

## 1. Scope and architecture

libeinstar speaks a vendor protocol over Cypress FX3 USB endpoints. There are
**no USB control transfers** in the protocol: commands are synchronous interrupt
transfers, flash pages are synchronous bulk transfers, and images are
asynchronous bulk IN transfers.

```
einstar::Device                     src/device.cpp        USB transport, retries, sequencing
├── einstar::LibusbTransport        src/device.cpp:19-49  libusb sync transfers, clear_halt
├── einstar::FrameAssembler         src/stream.cpp        EP 0x83 frame reassembly
├── einstar::protocol               src/protocol.cpp      packet codec, reply validation
└── einstar::calibration            src/calibration.cpp   CCF / .data_cm / device-blob parsing
```

The pure protocol layer (`einstar_core`: `protocol.cpp`, `stream.cpp`,
`calibration.cpp`, `exposure.cpp`) has no I/O and is unit-tested against a fake
scanner (`tests/fake_scanner.hpp`); `device.cpp` is the only libusb user.

---

## 2. USB transport

### 2.1 Identity and enumeration

| Item | Value | Confidence | Reference |
|---|---|---|---|
| Vendor ID | `0x3267` | **CV** | `include/einstar/protocol.hpp:18` (`kVendorId`), used in `Device::enumerate` `src/device.cpp:94` and `Device::open` `src/device.cpp:123` |
| Product ID | **not constrained by code**; every PID under VID `0x3267` is enumerated | **CV** | `src/device.cpp:94`, `123`; docs/PROTOCOL.md:13 claims PID `0x0003` [hw] but no code constant exists |
| Interface | 0, claimed after `libusb_set_auto_detach_kernel_driver(handle, 1)` | **CV** | `include/einstar/protocol.hpp:19` (`kInterface`), `src/device.cpp:156-157` |
| Configuration / alt setting | never selected or changed | **CV** | no `libusb_set_configuration` / `libusb_set_interface_alt_setting` call in the tree |
| Product string heuristics | `"SHINING-CAM"`, or prefix `"EINSCAN10"`, or prefix `"EinScan10"` | **CV** | `src/device.cpp:63-65`; the product string in the V2.10 firmware image is UTF-16LE `"SHINING-CAM"` (image offset ≈57420) |
| Device selection | (1) matching bus+port if requested, (2) first Einstar-looking device, (3) first device with VID `0x3267` | **CV** | `src/device.cpp:109-163` |

Note: `bus`/`port` matching uses `libusb_get_port_number()` only
(`src/device.cpp:98-99`, `124-125`), not the full `port_numbers[]` topology
path; two devices behind different hubs can share bus+port.

### 2.2 Endpoints and transfer types actually used

| Endpoint | Direction | Type | Used for | Reference |
|---|---|---|---|---|
| `0x01` | OUT | interrupt | every command request, and the 100-byte `0xFF` handshake | `protocol.hpp:21`, `device.cpp:203`, `237` |
| `0x81` | IN | interrupt | every command reply and the handshake reply | `protocol.hpp:22`, `device.cpp:194`, `206`, `240` |
| `0x02` | OUT | bulk | flash/custom-info page requests (`0x57`; `0x58` write is not implemented) | `protocol.hpp:23`, `device.cpp:336` |
| `0x82` | IN | bulk | flash/custom-info page replies | `protocol.hpp:24`, `device.cpp:350` |
| `0x83` | IN | bulk | image stream, one transfer per frame chunk | `protocol.hpp:25`, `device.cpp:494` |

No isochronous, no control-transfer vendor requests, no OUT streaming endpoint.
The task's known device facts (interrupt `0x01/0x81`, bulk `0x02/0x82`,
stream `0x83`) match the code exactly.

### 2.3 Linux descriptor quirk (`bInterval`)

The supplied device facts state that Linux must patch the interrupt endpoint's
`bInterval` from 0 to 7. **libeinstar contains no descriptor handling, no
`bInterval` workaround and no libusb/udev quirk** (grep over the tree: only
`libusb_*_transfer`, `libusb_claim_interface`, `libusb_set_auto_detach_kernel_driver`
are used, `src/device.cpp:29-43`, `156-157`). On macOS this is invisible; on
Linux the driver relies on the kernel accepting `bInterval = 0`. This is a
Linux bring-up blocker, not a protocol field — see gap **G1**.

### 2.4 Transfer sizes

| Constant | Value | Meaning | Reference |
|---|---|---|---|
| `kCommandSize` | 16 | in-memory command packet size | `protocol.hpp:27` |
| `kHandshakeSize` | 100 | handshake buffer sent to EP `0x01` | `protocol.hpp:28` |
| `kPageTransferSize` | `0x1400` (5120) | page request and reply transfer size | `protocol.hpp:30` |
| `kPageSize` | `0x1000` (4096) | payload bytes in a page reply | `protocol.hpp:31` |
| `kStreamPackageSize` | `0xA400` (41984) | stream read buffer / nominal transfer size | `protocol.hpp:32` |
| reply read buffer | 1024 | maximum interrupt reply the code reads | `device.cpp:239` |
| bulk chunk | `0x400` (1024) | request/reply chunking for pages | `device.cpp:329` |

All integer protocol fields are **big-endian**. The only little-endian data in
the system are the calibration file formats (CCF/`.data_cm`, §8).

---

## 3. Command packet format

### 3.1 Request (host → device, EP `0x01`, interrupt)

```
offset  size  field            value / notes
0       1     packet id        rolling 0..253 (mod 254), see §3.4
1       1     marker           0x00 in every request built by this code
2       1     group            0x00 system, 0x10 camera
3       1     opcode           §4
4       1     status           0
5       2     reserved         0
7       1     payload length   L, 0..8 (kMaxPayload = 16 - 8)
8       L     payload          big-endian fields
8+L     ...   zero padding     up to the per-command wire size (§3.3)
```

Confidence **CV**: `include/einstar/protocol.hpp:35-46` (offsets, `kMaxPayload`),
`src/protocol.cpp:69-78` (`build_command`), `src/device.cpp:233-234` (padding to
`wire_size`). The request is always built in a 16-byte `Packet`, then copied
into a `wire_size`-byte zeroed buffer.

The packet id is generated fresh for **every attempt**, not every logical
command: `build_command(ids_.next(), ...)` is inside the retry loop
(`src/device.cpp:230-231`).

### 3.2 Reply (device → host, EP `0x81`, interrupt)

```
offset  size  field            value / notes
0       1     packet id        echo of request byte 0
1       1     marker           0x02 (never validated by the code — §13)
2       1     group            echo of request byte 2
3       1     opcode           echo of request byte 3
4       1     status           0 = success; nonzero = device error (values unknown)
5       3     reserved         zero in observed replies
8       1     value length N   0..255
9       N     value            big-endian; NUL-terminated string for string replies
```

Confidence **CV**: `protocol.hpp:41-45`, `protocol.cpp:91-99` (`validate_reply`),
`protocol.cpp:154-166` (`reply_value`, `reply_uint`), plus captured replies in
`tests/test_protocol.cpp:89-107`.

Important details:

- The transfer length is chosen by the device; libeinstar reads up to 1024 bytes
  and keeps `got` (`device.cpp:239-242`). The 16-byte request size does not
  determine the reply size.
- `reply_value()` clamps `N` to the bytes actually received
  (`protocol.cpp:154-158`), so a lying length byte truncates silently instead of
  over-reading.
- Validation checks only id/group/opcode/status (`protocol.cpp:91-99`); byte 1
  (`0x02`) and byte 8 (`N`) are **not** checked. See gaps **G11/G13**.
- The observed serial reply carries **12** value bytes though only 8 are the
  serial (`tests/test_protocol.cpp:93-94,100`); the trailing `0x44 00 00 00` is
  ignored.

### 3.3 Per-command wire sizes

`wire_size()` (`src/protocol.cpp:42-67`) selects the zero-padded interrupt
transfer length:

| Wire size | Commands | Reference |
|---|---|---|
| 10 | `0x40` get trigger switch, `0x41` set trigger switch, `0x7b` heartbeat, system `0x08` reboot | `protocol.cpp:53-56`, `48` |
| 16 | every other camera opcode (default), including `0x22/0x23`, `0x26/0x27`, `0x50`, `0x51`, `0x57` header | `protocol.cpp:64-65` |
| 50 | `0x48/0x49` trigger period, `0x67/0x68` laser, `0x6f/0x70` strobe | `protocol.cpp:57-63` |
| 64 | system `0x04` serial, system `0x05` firmware | `protocol.cpp:46-47` |
| 100 | system `0x01` product name | `protocol.cpp:45` |

The page request is the exception: a 16-byte command header in a `0x1400`-byte
bulk request (§6).

### 3.4 Packet id

`PacketIdCounter` returns `value_` then increments modulo `0xFE` (254), so ids
run `0..253` and wrap to 0 (`protocol.hpp:147-153`, `protocol.cpp:123-127`,
test `test_protocol.cpp:58-64`). First command after open is the serial query
with id 0. Reply validation requires an exact id echo, so a late reply from a
previous attempt is rejected (and retried).

### 3.5 Optional XOR mask

```
key = n | (n << 4)      for a random nibble n (0..15)
encoded[i] = plain[i] ^ key     for the whole transfer
decode: n = data[1] >> 4; key = n | (n << 4); data[i] ^= key
```

Confidence **CV** for the transform (`protocol.hpp:178-181`,
`protocol.cpp:190-200`), **INF** for the device requiring it (default off:
`OpenOptions::mask_commands = false`, `device.hpp:50`; docs/PROTOCOL.md:73-75
says whether the Einstar enables it is unknown). The scheme works because a
plaintext request has byte 1 = `0x00`, so the decoder can recover `n` from the
reply's byte 1 high nibble (`protocol.cpp:197`). A new random nibble is used per
attempt (`device.cpp:235`).

### 3.6 Timeouts, retries, pipe recovery

| Operation | Timeout | Retries | Backoff / behaviour | Reference |
|---|---|---|---|---|
| Handshake drain (interrupt IN) | 10 ms each | up to 5 transfers, stop at first non-OK | — | `device.cpp:193-194` |
| Handshake drain (bulk IN) | 10 ms each | up to 30 transfers, stop at first non-OK | — | `device.cpp:195-196` |
| Handshake send / reply | `OpenOptions::timeout_ms`, default **1000 ms** each | 0 | one attempt | `device.cpp:201-208`, `device.hpp:51` |
| Command send / reply | 1000 ms each | `retries`, default **5 attempts** | after each failed attempt: `clear_halt(0x01)`, `clear_halt(0x81)`, sleep **20 ms**; new packet id | `device.cpp:230-255` |
| Page bulk OUT | 2000 ms per 0x400 chunk | 5 outer attempts | 1 ms between chunks | `device.cpp:334-342` |
| Page bulk IN | 600 ms per 0x400 chunk | 3 inner tries per outer attempt | 80 ms between tries; `clear_halt(0x82)` on timeout | `device.cpp:344-364` |
| Page whole exchange | — | 5 outer attempts | `clear_halt(0x82)` between attempts | `device.cpp:331`, `366` |
| Stream bulk IN | 200 ms | none | timeout: continue; other error: clear halt, reset assembler | `device.cpp:494-500` |
| Stop-scan settle | — | — | unconditional **1 s** sleep when RGB count is 0 | `device.cpp:448` |

`kTransferTimeout = -7` matches `LIBUSB_ERROR_TIMEOUT`
(`include/einstar/transport.hpp:13`, `device.cpp:184`). Command timeouts map to
`ErrorCode::Timeout`; other libusb errors map to `ErrorCode::UsbError`
(`device.cpp:183-186`).

Worst-case command latency is therefore ~5 × (1000 + 1000 + 20) ms ≈ 10 s while
`command_mutex_` is held (`device.cpp:225`), blocking every other command
caller. A page read is worse: 5 outer attempts × (up to 2 s of request chunks +
3 reply tries × 5 chunks × 600 ms) ≈ 55 s when the device is unresponsive.

### 3.7 Status and error codes

The protocol has a single status byte at reply offset 4. **Only 0 is accepted**;
any nonzero value becomes `ReplyError::DeviceStatus` and then
`ErrorCode::BadReply` with the message `"device reported non-zero status"`
(`protocol.cpp:97`, `device.cpp:244-245`). The numeric meaning of nonzero
status values is **UNK** — the library does not expose or map them.

Library-side error taxonomy (`include/einstar/result.hpp:10-18`,
`src/device.cpp:69-80`):

| `ErrorCode` | Meaning |
|---|---|
| `UsbError` | libusb failure other than timeout |
| `NotFound` | no device with VID `0x3267` |
| `NotOpen` | operation on a closed `Device` |
| `Timeout` | libusb transfer timed out (`-7`) |
| `BadReply` | short/mismatched reply, nonzero device status, missing value, bad calibration block |
| `WritesDisabled` | state-changing command without `allow_writes`, or forbidden command |
| `InvalidArgument` | null transport, oversized payload, double `start_stream` |

`ReplyError` categories (`protocol.hpp:119-125`, `protocol.cpp:80-89`):
`TooShort`, `IdMismatch`, `GroupMismatch`, `OpcodeMismatch`, `DeviceStatus`.

`read_page()` makes `DeviceStatus` fatal but retries id/group/opcode mismatches
(`device.cpp:384-388`); every other command retries all failures.

---

## 4. Command catalogue

Column meanings: **Op** = byte 3; **Group** = byte 2; **Wire** = interrupt
transfer length in bytes (§3.3); **Payload** = bytes at offset 8 of the request;
**Reply value** = bytes at offset 9 of the reply. All requests are interrupt OUT
EP `0x01`, all interrupt replies IN EP `0x81`, unless noted. **Direction is therefore host→device for the request and
device→host for the reply in every row.** Because no vendor control transfer is
used (§4.3), `bmRequestType` / `bRequest` / `wValue` / `wIndex` / `wLength` are
**n/a** for every command; the equivalent fields are the packet offsets below. "Used" says whether
libeinstar has a call site; "Conf" uses the legend above.

### 4.1 System group (byte 2 = `0x00`)

| Name | Op | Wire | Payload | Reply value | Meaning | Used | Conf | Source |
|---|---|---|---|---|---|---|---|---|
| ProductName | `0x01` | 100 | none (L=0) | NUL-terminated string, e.g. `"EinScan10_01"` (12 bytes) | device product name | yes, `Device::product_name` | **CV** layout / **CD** semantics | `protocol.hpp:56`; `protocol.cpp:45`, `113-121`; `device.cpp:265-269`; test `test_protocol.cpp:95-96,101` |
| Serial | `0x04` | 64 | none | 8 serial bytes (+4 observed trailing bytes) rendered `%02X` × 8 | device serial | yes, at open | **CV** | `protocol.hpp:57`; `protocol.cpp:46`, `173-183`; `device.cpp:173-177`; test `test_protocol.cpp:93-94,100` |
| FirmwareVersion | `0x05` | 64 | none | NUL-terminated string, e.g. `"EinScan10_01_SC130_FX3_V2.14_FPGA_V3.7_EN"` | firmware/FPGA version string | yes, `Device::firmware_version` | **CV** layout / **CD** semantics | `protocol.hpp:58`; `protocol.cpp:47`; `device.cpp:271-275`; test `test_protocol.cpp:163-167` |
| Reboot | `0x08` | 10 | none (never sent) | — | reboot device | **forbidden** | **CD** | `protocol.hpp:59`; `protocol.cpp:38`; `device.cpp:214-219` |

### 4.2 Camera group (byte 2 = `0x10`)

| Name | Op | Wire | Payload (offset 8) | Reply value (offset 9) | Meaning | Used | Conf | Source |
|---|---|---|---|---|---|---|---|---|
| VendorId | `0x00` | 16 | none? | unknown | vendor id | no | **CD** | `protocol.hpp:64` |
| ProductId | `0x01` | 16 | none? | unknown | product id | no | **CD** | `protocol.hpp:65` |
| MaxWidth | `0x16` | 16 | `mask` u8 | u16 BE, `0x0500` = 1280 observed | sensor max width | yes (`probe`) | **CV** | `protocol.hpp:66`; `device.cpp:304`; test `test_protocol.cpp:90,97` |
| MaxHeight | `0x17` | 16 | `mask` u8 | u16 BE, 1024 | sensor max height | yes (`probe`) | **CV** | `protocol.hpp:67`; `device.cpp:306` |
| MaxExposure | `0x20` | 16 | `mask` u8 | u32 BE (µs assumed) | exposure upper limit | no | **CD** | `protocol.hpp:68`; docs/PROTOCOL.md:102 |
| MinExposure | `0x21` | 16 | `mask` u8 | u32 BE | exposure lower limit | no | **CD** | `protocol.hpp:69` |
| GetExposure | `0x22` | 16 | `mask` u8 | u32 BE, observed 2184 default | read exposure | yes | **CV** | `protocol.hpp:70`; `device.cpp:296`; test `test_protocol.cpp:91,98` |
| SetExposure | `0x23` | 16 | `mask` u8 + u32 BE (L=5) | empty (N=0) | set exposure (µs assumed) | yes | **CV** | `protocol.hpp:71`; `device.cpp:415-419`; byte example `test_protocol.cpp:11-17` |
| MaxGain | `0x24` | 16 | `mask` u8 | u16 BE | gain upper limit | no | **CD** | `protocol.hpp:72` |
| MinGain | `0x25` | 16 | `mask` u8 | u16 BE | gain lower limit | no | **CD** | `protocol.hpp:73` |
| GetGain | `0x26` | 16 | `mask` u8 | u16 BE; default 500 per docs | read gain | yes | **CV** | `protocol.hpp:74`; `device.cpp:298-302`; test `test_protocol.cpp:39-44` |
| SetGain | `0x27` | 16 | `mask` u8 + u16 BE (L=3) | empty | set gain | yes | **CV** | `protocol.hpp:75`; `device.cpp:421-425`; byte example `test_protocol.cpp:19-23` |
| PixelBits | `0x2e` | 16 | `mask` u8 | u8 (8 observed in docs) | bits per pixel | no | **CD** | `protocol.hpp:76`; docs/PROTOCOL.md:20 |
| GetTriggerSwitch | `0x40` | 10 | none | u8: `rgb << 4 \| mono` | frames per trigger | yes | **CV** | `protocol.hpp:77`; `device.cpp:317-321`; test `test_protocol.cpp:153-159` |
| SetTriggerSwitch | `0x41` | 10 | u8 packed counts (L=1) | empty | enable/disable triggering | yes | **CV** | `protocol.hpp:78`; `device.cpp:444-450` |
| GetTriggerPeriod | `0x48` | 50 | none | u32 BE (µs assumed) | trigger period | yes | **CV** | `protocol.hpp:79`; `device.cpp:308` |
| SetTriggerPeriod | `0x49` | 50 | u32 BE (L=4) | empty | set trigger period | yes | **CV** | `protocol.hpp:80`; `device.cpp:427-431` |
| Temperature | `0x50` | 16 | none? | i16 BE at 9, arithmetic `>> 7` | LM75-style temperature | yes | **CV** decode / **INF** units | `protocol.hpp:81`; `protocol.cpp:185-188`; `device.cpp:310-315` |
| SensorCount | `0x51` | 16 | none? | u8 (3 observed) | number of sensors | yes (`probe`) | **CV** | `protocol.hpp:82`; `device.cpp:290-294`; test `test_protocol.cpp:92,99` |
| PageRead | `0x57` | 16 header + bulk | u16 BE page index (L=2) | 4096 bytes in a `0x1400` bulk reply at offset 9 | read 4 KiB flash page | yes, `Device::read_page` | **CV** | `protocol.hpp:83`; `device.cpp:371-393` |
| PageWrite | `0x58` | bulk | data at offset 10 (docs) | — | write flash page | **forbidden** | **CD** | `protocol.hpp:84`; `protocol.cpp:39`; docs/PROTOCOL.md:164-165 |
| ColorMode | `0x5d` | 16 | `mask` u8? | unknown | colour mode | no | **CD** | `protocol.hpp:85`; docs/PROTOCOL.md:112 |
| SetIndication | `0x62` | 16 | 2 bytes (docs) | empty | distance/state indication | no | **CD** | `protocol.hpp:86`; docs/PROTOCOL.md:113 |
| GetLaserBrightness | `0x67` | 50 | none? | u8 | laser/LED brightness | no | **CD** | `protocol.hpp:87` |
| SetLaserBrightness | `0x68` | 50 | u8 percent, clamped to 100 (L=1) | empty | set laser/LED brightness | yes | **CV** payload / **CD** meaning | `protocol.hpp:88`; `device.cpp:439-442`; docs/PROTOCOL.md:144 |
| GetStrobeBrightness | `0x6f` | 50 | none? | u16? | strobe brightness | no | **CD** | `protocol.hpp:89` |
| SetStrobeBrightness | `0x70` | 50 | `route` u8 + u16 BE luminance (L=3) | empty | set strobe route brightness | yes | **CV** payload / **CD** route semantics | `protocol.hpp:90`; `device.cpp:433-437` |
| HeartBeat | `0x7b` | 10 | none? | unknown | heartbeat / button state | no | **CD** | `protocol.hpp:91`; docs/PROTOCOL.md:116 |

**Command count:** 28 camera-group + 4 system-group = **32 distinct opcodes**,
plus the `0xFF` handshake (§5.1) = **33 protocol operations**. 18 opcodes have a
call site in this library; 14 are declared-only or blocked (`0x00`, `0x01`
camera, `0x20`, `0x21`, `0x24`, `0x25`, `0x2e`, `0x5d`, `0x62`, `0x67`, `0x6f`,
`0x7b`, `0x08`, `0x58`).

Camera selector (`mask`) is `1 << camera_index` (`protocol.cpp:101-103`), i.e.
camera 0 = `0x01`, camera 1 = `0x02`, camera 2 = `0x04`. This is identical to
the vendor expression `cam == 2 ? 4 : cam + 1` quoted in the repository docs
(docs/PROTOCOL.md:118-119) and asserted in `test_protocol.cpp:31-36`.

### 4.3 Control-transfer table

**None.** The protocol uses no `bmRequestType`/`bRequest`/`wValue`/`wIndex`/
`wLength` vendor control transfers. All commands are the interrupt packet of
§3.1. (libusb itself issues standard control requests when enumerating and
reading string descriptors; those are USB-standard, not Einstar protocol.)

### 4.4 Trigger-count packing

```
byte = ((rgb & 0x0F) << 4) | (mono & 0x0F)
mono = byte & 0x0F ; rgb = byte >> 4
```

Confidence **CV**: `protocol.hpp:137-140`, `protocol.cpp:105-111`, test
`test_protocol.cpp:153-159`. `0x11` = 1 IR + 1 colour frame per trigger;
`0x00` = trigger off. The vendor sequence uses `0x49 = 200000` immediately
before `0x41` and (for scanning) `0x49 = 90000` after it (§5.2).

---

## 5. Initialization and scan sequencing

### 5.1 `Device::open` sequence (exact order)

1. `libusb_init`, get device list, filter `idVendor == 0x3267`, optionally match
   bus/port, `libusb_open` (`device.cpp:109-149`).
2. `libusb_set_auto_detach_kernel_driver(handle, 1)`; `libusb_claim_interface(0)`
   (`device.cpp:156-161`). Return value of auto-detach is ignored.
3. **Handshake** (`device.cpp:188-212`):
   1. drain up to 5 interrupt-IN transfers of 1024 bytes, 10 ms timeout each,
      stop at first non-OK (`device.cpp:193-194`);
   2. drain up to 30 bulk-IN transfers of 1024 bytes, 10 ms each, stop at first
      non-OK (`device.cpp:195-196`);
   3. `clear_halt(0x81)`, `clear_halt(0x82)` (`device.cpp:198-199`);
   4. send a 100-byte buffer: byte 0 = `0xFF`, bytes 1..99 = 0, to EP `0x01`,
      timeout 1000 ms (`device.cpp:201-204`);
   5. read 100 bytes from EP `0x81`, timeout 1000 ms (`device.cpp:205-208`);
   6. require ASCII `"Shining3"` at reply bytes 9..16 (`device.cpp:209-210`;
      `protocol.cpp:168-171`). The V2.10 firmware image contains the vendor
      string `"Shining3D"` in ASCII, consistent with an 8-byte truncation.
4. **Serial query**: system opcode `0x04`, wire 64, require ≥8 value bytes,
   render the first 8 as 16 uppercase hex digits (`device.cpp:173-177`,
   `protocol.cpp:173-183`). No product-name, firmware-version or sensor-count
   check is made at open.

Nothing else is sent automatically. Exposure, gain, lighting, trigger and stream
start are explicit calls.

### 5.2 Scan start / stop (library sequence)

`start_scanning(period = 90000)` (`device.cpp:452-455`) calls
`set_trigger_counts(1, 1)` and then `set_trigger_period(period)`:

```
1. SetTriggerPeriod 0x49 = 200000        (wire 50, payload u32 BE)
2. SetTriggerSwitch 0x41 = 0x11          (wire 10, payload 1 byte; 1 IR + 1 RGB)
3. SetTriggerPeriod 0x49 = trigger_period (default 90000; wire 50)
```

`set_trigger_counts(mono, rgb)` (`device.cpp:444-450`) is the reason step 1
exists: it always writes period 200000 first, then the packed counts, then
sleeps **1 s only if rgb == 0**. `start_scanning` then overrides the period with
90000.

`stop_scanning()` = `set_trigger_counts(0, 0)` (`device.cpp:457`):

```
1. SetTriggerPeriod 0x49 = 200000
2. SetTriggerSwitch 0x41 = 0x00
3. sleep 1000 ms
```

Consequences: stopping always takes ~1 s and resets the trigger period to
200000 on the device; a later `trigger_period()` read returns 200000, not the
90000 last used. The first frame set after starting is overexposed; tools discard
two sets (`integrations/rtabmap/CameraEinstar.cpp:85-87`,
`tools/einstar-rgbd.cpp:128`). The probe does not discard them.

### 5.3 Reference vendor sequence (from the repository's notes) vs library

docs/PROTOCOL.md:148-159 lists EXStar's connect order:
`openDevices{E10} → Serial → FirmwareVersion → SensorCount → ReadData(0, 6568)
→ ProtectCapture → EnableRGB → YGamma → TriggerMode{1,1} → ScanTriggerPeriod
90000 → ImageStream on → exposure/gain/strobe/indication updates`.

libeinstar performs only: handshake → Serial → (optional) page reads → optional
stream start → exposure/gain/strobe/laser → trigger period/counts → period.
It never sends `HeartBeat 0x7b`, `ColorMode 0x5d` or `SetIndication 0x62`;
`ProtectCapture`/`EnableRGB`/`YGamma` are host-side/vendor concepts with no
identified opcode (docs/PROTOCOL.md:146). The driver works on the reference unit
without them, which is itself an implicit firmware assumption (§10).

---

## 6. Flash / custom-info page access (bulk `0x02` → `0x82`)

### 6.1 Request

A `0x1400`-byte zeroed buffer whose first 16 bytes are a normal command packet
(`device.cpp:376-380`):

```
0      packet id
1      0x00
2      0x10
3      0x57            PageRead
4      0x00
5..6   0x00
7      0x02            payload length
8..9   page index      u16 BE
10..   zero padding to 0x1400
```

### 6.2 Reply

`0x1400` bytes (`device.cpp:381-390`):

```
0      id echo
1      0x02 marker
2      group echo 0x10
3      opcode echo 0x57
4      status (0 = ok)
5..7   zero
8      0x00 (unused by the code; page data starts at 9)
9..   4096 bytes of page data
```

The code validates bytes 0/2/3/4 exactly like an interrupt reply and then
requires `reply.size() >= 9 + 4096` (`device.cpp:384-390`). Byte 8 is not used.
The fake scanner reproduces this layout (`tests/fake_scanner.hpp:107-112`).

### 6.3 Chunking and retry

The request is sent in `0x400`-byte bulk chunks with a 2 s timeout and 1 ms gap;
the reply is read in `0x400`-byte chunks with a 600 ms timeout, up to 3 tries
80 ms apart, and the whole exchange is retried up to 5 times with
`clear_halt(0x82)` (`device.cpp:327-369`). See gap **G8** for the short-read
bug in this loop.

### 6.4 Page mapping and the 6568-byte blob

`read_custom_info(size)` reads pages `0, 1, …` until `size` bytes are collected
(`device.cpp:395-405`). `read_calibration()` reads exactly
`kDeviceInfoSize = 6568` bytes (`calibration.hpp:109`, `device.cpp:407-413`),
which is pages 0 (4096 B) and 1 (first 2472 B). EXStar reads the same 6568 bytes
at connect (docs/PROTOCOL.md:155-156, 191). The blob layout outside the
calibration block is **UNK**.

### 6.5 Page writes

Opcode `0x58` with data at offset 10 is described in the repository notes
(docs/PROTOCOL.md:164-165) but **not implemented**. `is_forbidden()` rejects it
even with `allow_writes` (`protocol.cpp:37-40`, `device.cpp:214-219`), as does
system `0x08` reboot.

---

## 7. Image stream (EP `0x83`, bulk IN)

### 7.1 Transfer/frame header

Every USB transfer starts with a **32-byte (`0x20`) header**; payload follows at
`0x20`. Offsets are relative to the **raw USB transfer** (the vendor's 4-byte
length prefix described in `stream.hpp:7-9` is not present on the wire and is
not read by libeinstar).

| Offset | Size | Field | Encoding | Confidence | Source |
|---|---|---|---|---|---|
| `0x00..0x01` | 2 | unknown | — | **UNK** | not read; docs/PROTOCOL.md:250 open question 2 |
| `0x02` | 1 | camera field + sub-index | bits 3..5 = one-hot camera (`1`, `2`, `4`); bits 0..2 = sub-index | **CV** | `stream.hpp:22`, `stream.cpp:46-47`; docs/PROTOCOL.md:178 |
| `0x03..0x07` | 5 | unknown | — | **UNK** | not read |
| `0x08` | 1 | last-transfer flag | exactly `1` means "last transfer of this frame" | **CV** | `stream.hpp:23`, `stream.cpp:25` |
| `0x09..0x0b` | 3 | unknown | — | **UNK** | not read |
| `0x0c..0x0f` | 4 | frame id | BE32, read from the **last** transfer | **CV** read / **INF** meaning | `stream.hpp:24`, `stream.cpp:44` |
| `0x10..0x17` | 8 | timestamp | BE64, read from the **first** transfer | **CV** read / **UNK** units/epoch | `stream.hpp:25`, `stream.cpp:32` |
| `0x18..0x1f` | 8 | unknown | — | **UNK** | not read |
| `0x20..` | n | image payload | 8-bit samples, row-major; no stride field (no-padding is inferred from the PGM output) | **CV** for 8-bit & 1280×1024; **INF** for no padding | `stream.hpp:20-21`; docs/PROTOCOL.md:184; `tools/einstar-probe.cpp:212` |

There is **no magic number and no size field** in the header. The expected frame
size is supplied by the caller (`start_stream(cb, frame_size)`, default
`1280*1024`, `device.hpp:117`) and the frame only completes when a last-flagged
transfer brings the accumulated payload to exactly that size.

Camera id mapping (`FrameInfo::camera_index`, `stream.cpp:9-16`):

| `camera_field` | index | meaning (docs) |
|---|---|---|
| `1` | 0 | IR camera 0 → calibration index `kIrRightCamera` (2) |
| `2` | 1 | IR camera 1 → calibration index `kIrLeftCamera` (1) |
| `4` | 2 | colour camera → calibration index 0 |
| other | `nullopt` | unrecognised |

### 7.2 Assembly rules

`FrameAssembler::push` (`stream.cpp:23-54`):

1. `size <= 0x20` → `TooShort` (header-only transfer rejected).
2. `last = transfer[0x08] == 1`; `chunk = size - 0x20`.
3. If `last` and `filled + chunk != frame_size` → reset, `SizeMismatch`
   (whole frame discarded).
4. If `filled == 0`, latch `timestamp = BE64(0x10)`.
5. If `filled + chunk > frame_size` → reset, `Overflow`; then copy
   `min(frame_size, chunk)` bytes at offset 0 and set `filled = chunk`.
6. Complete only when `filled == frame_size && last`; latch `frame_id` (BE32 at
   `0x0c`) and the camera field from the **last** transfer; return
   `FrameComplete`.
7. A last-flagged transfer that does not complete the frame resets assembly
   (`stream.cpp:52`).

There is **one assembler for all three cameras** (`device.cpp:490`); see gap
**G3**. There is no per-camera, per-frame-id or per-sub-index state.

### 7.3 Frame size, format, orientation

- Size: 1280 × 1024, 8-bit, fixed by constants/defaults, not negotiated
  (`calibration.hpp:75-76`, `device.hpp:117`, `tools/einstar-probe.cpp:212`).
- Pixel format: 8-bit; the colour stream is raw Bayer and is debayered with
  OpenCV `COLOR_BayerRG2BGR` (`src/depth.cpp:28-32`).
- Stream→calibration mapping (`calibration.hpp:33-38`): stream 0 = IR right
  (calibration 2), stream 1 = IR left (calibration 1), stream 2 = colour
  (calibration 0).
- Orientation (`calibration.hpp:40-45`): streams 0 and 2 arrive rotated 180°
  relative to the calibration; stream 1 arrives as calibrated. Applied with
  `cv::rotate(..., ROTATE_180)` (`depth.cpp:21-26`).
- Transfer size: read buffer `0xA400`; a 1280×1024 frame is 32 transfers of
  41 984 bytes (31 full + 9 216) if the firmware uses the nominal size.

### 7.4 Throughput and timing observations

- Stream reads use a 200 ms timeout and are **not** retried; a timeout just
  continues (`device.cpp:494-495`).
- The observed capture rate in the repository notes is ~6 frames/s live
  (docs/HARDWARE_BRINGUP.md:107) and EXStar used a 90000 trigger period
  (~11 Hz) (docs/PROTOCOL.md:158).
- There is no flow control, no sequence-gap detection and no statistic exposed.

---

## 8. Calibration data formats

Three ingestion paths converge on `CameraModel`:

| Path | Entry point | Input | Reference |
|---|---|---|---|
| On-device blob | `Device::read_calibration` → `parse_device_calibration` | 6568-byte custom-info dump | `device.cpp:407-413`, `calibration.cpp:164-179` |
| EXStar CCF cache | `load_exstar_ccf` → `parse_exstar_ccf` | `TexCCF.txt`, `LeftCCF.txt`, `RightCCF.txt` | `calibration.cpp:150-162`, `188-191` |
| EXStar project model | `load_camera_model` → `parse_camera_model` | `*.data_cm`, 692 bytes | `calibration.cpp:131-148` |

`load_calibration(path)` sniffs: directory → CCF cache; 6568-byte file →
custom-info blob; otherwise → `.data_cm` (`calibration.cpp:181-186`).

### 8.1 Device custom-info blob

| Item | Value | Reference |
|---|---|---|
| Blob size read | `6568` (`0x19A8`) | `calibration.hpp:109` |
| Calibration block offset | `0x39B` | `calibration.hpp:110`, `calibration.cpp:168` |
| Block size | `0xF21` (3873) | `calibration.cpp:167` |
| Block type | int32 LE at block `0x000`, must be `4` | `calibration.cpp:166`, `170` |
| Magic | `"FQFQ"` at block `0xF1C` | `calibration.cpp:166`, `170` |
| Left CCF slot | int32 LE size at `0x108`, then 520 bytes | `calibration.cpp:166`, `172-176` |
| Right CCF slot | int32 LE size at `0x610`, then 520 bytes | `calibration.cpp:166` |
| Tex CCF slot | int32 LE size at `0xB18`, then 264 bytes | `calibration.cpp:166` |

The slot sizes must equal exactly 520/520/264 or parsing fails with
`BadDeviceBlock` (`calibration.cpp:172-177`). The block is then parsed as three
CCF files (`calibration.cpp:178`), so the CCF decoding below applies to the
on-device calibration too. All other blob regions (white balance, colour
correction, activation, date) are **UNK**.

### 8.2 CCF files

- Sizes enforced by the parser: `TexCCF.txt` 264 bytes, `LeftCCF.txt` and
  `RightCCF.txt` 520 bytes each (`calibration.hpp:99-100`,
  `calibration.cpp:151-152`).
- Encoding: little-endian `float64`.
- The parser reads **32 fields** per file (`kCcfFields = 32`,
  `calibration.cpp:30`, `36-48`). Field order (`calibration.cpp:29`,
  `read_ccf`):
  `fx fy 0 0 cx cy 0 0 d5 0 k1 k2 p1 p2 k3 0 0 0 0 0 T(3) R(9)`.
  Mapping: `fx=f[0]`, `fy=f[1]`, `cx=f[4]`, `cy=f[5]`,
  distortion `{k1,k2,p1,p2,k3,d5} = {f[10],f[11],f[12],f[13],f[14],f[8]}`,
  `T=f[20..22]`, `R=f[23..31]` (row-major).
- IR files are de-obfuscated by **subtracting** a fixed 32-element integer key:

```
stored[i] = true[i] + kCcfIrKey[i]
kCcfIrKey[32] = { 5, -7,  0,  0,  8, -3,  0,  0,  5,  0,  1,  0, 11,  9, 13,  0,
                  0,  0,  0,  0,  1,-10, -1, -7, -6,-10, -1, 14, -3,-15,-11, -4 }
```

  Reference: declaration `src/calibration.cpp:33-34`; application
  `src/calibration.cpp:38`; comment "recovered by comparison with an EXStar
  .data_cm of the same scanner" `src/calibration.cpp:31-32`. The key is applied
  to **all** 32 fields, including R and T.
- `TexCCF.txt` is **not** keyed (`ir == false` at `calibration.cpp:154`).
- Every camera rotation must be orthonormal to 1e-3 and have determinant 1, or
  the whole parse fails with `NotARotation` (`calibration.cpp:72-81`,
  `137-138`, `157-158`). This is the only calibration sanity check; there is no
  reprojection/consistency check.

**Observed limitation (reproduced):** the two real CCF sets in this workspace
(`sources/calibration/res_Einscan-E10_200x150/` and
`sources/calibration/ccf_200x150/`) are **rejected** by this parser with
`"camera rotation is not orthonormal"` (see §12.3). The IR files are 65 doubles;
only the first 32 are decoded, and bytes 256..519 are **66 little-endian
uint32 values in 0..9**, not doubles (verified byte-wise). The tail differs
between Left and Right of the same unit, so it is per-file data, not padding.
Whether it is a per-file encoding key or unrelated metadata is **UNK**; see gap
**G2**.

### 8.3 EXStar `.data_cm` (692 bytes, little-endian)

Exact size arithmetic: 82 × float64 (656 B) + 5 × float32 (20 B) + 4 × int32
(16 B) = 692 B. The parser reads the doubles and the final int32s; the five
float32 values are **unparsed**.

| Offset (bytes) | Type | Content | Reference |
|---|---|---|---|
| `0`..`31` | 4 × f64 | header, unverified (looks like rectified cx, cy, f, f) | `calibration.hpp:6-7`, `calibration.cpp:134` |
| `32`..`207` | 22 × f64 | camera 0 = colour: fx fy cx cy, d0..d5, R(9) row-major, T(3) mm | `calibration.cpp:27`, `56-66`; offset 4 doubles |
| `208`..`303` | 12 × f64 | pair block: R(9) unverified semantics, T(3) = camera 1 → camera 0 (mm) | `calibration.cpp:54`, `140-141`; offset 26 doubles |
| `304`..`479` | 22 × f64 | camera 1 = IR left (LeftCCF), same layout | offset 38 doubles |
| `480`..`655` | 22 × f64 | camera 2 = IR right (RightCCF), same layout | offset 60 doubles |
| `656`..`675` | 5 × f32 | unparsed tail | not read |
| `676`..`691` | 4 × int32 | `640 512 320 256` | `calibration.cpp:142`; `parse_camera_model` reads at `size-16` |

Per-camera layout inside its 22 doubles (`read_camera`, `calibration.cpp:56-66`):
`fx fy cx cy` at +0..+3, distortion `d0..d5` at +4..+9, `R` at +10..+18,
`T` at +19..+21. The comment says the distortion order is `k1 k2 p1 p2 k3` plus
one unverified term (`calibration.hpp:12`); `depth.cpp` uses only the first five
(`distortion_coeffs`, `depth.cpp:16-19`).

Transform convention: `x_cam = R * x_world + T` (`calibration.hpp:13-14`),
`CameraModel::relative(from, to) = cameras[to] ∘ cameras[from]⁻¹`
(`calibration.cpp:117-119`).

### 8.4 Derived model and stream mapping

`parse_exstar_ccf` fills the pair translation from
`relative(kIrLeftCamera, kColorCamera).T` and the tail sizes `{640,512,320,256}`
(`calibration.cpp:159-160`); the CCF files have no pair block/header, so
`CameraModel::header` and `pair_rotation_raw` stay zero.

The stream mapping and 180° orientation are hardcoded from hardware observation,
not read from calibration (`calibration.hpp:33-45`). The depth pipeline picks
which IR camera is geometric left by the sign of the baseline x component:
`swap = relative(kIrLeftCamera, kIrRightCamera).T[0] > 0` (`depth.cpp:90-92`).

---

## 9. Exposure, gain and projector control

### 9.1 Defaults and sequencing

`ScanSettings` (`device.hpp:38-46`) defaults, described as EXStar scanning values
observed in logs:

| Field | Value | Applied as |
|---|---|---|
| `strobe_route0` | 1000 | `0x70` route 0, u16 BE |
| `strobe_route1` | 300 | `0x70` route 1, u16 BE |
| `laser_percent` | 100 | `0x68`, clamped to 100 |
| `ir_exposure_us` | 4000 | `0x23` for cameras 0 and 1 |
| `ir_gain` | 105 | `0x27` for cameras 0 and 1 |
| `color_exposure_us` | 5000 | `0x23` camera 2 |
| `color_gain` | 180 | `0x27` camera 2 |

`Device::apply()` order (`device.cpp:459-469`): IR cam 0 exposure, IR cam 0 gain,
IR cam 1 exposure, IR cam 1 gain, colour exposure, colour gain, strobe route 0,
strobe route 1, laser. Any failure aborts and returns the error.

`lights_off()` (`device.cpp:471-475`): strobe route 0 = 0, route 1 = 0,
laser = 0. There is no automatic call on destruction.

### 9.2 IR auto-exposure

`AutoExposure` (`include/einstar/exposure.hpp:24-53`, `src/exposure.cpp:9-44`)
is a host-side control loop, not a device feature:

| Parameter | Default | Meaning |
|---|---|---|
| `percentile` | 0.99 | steer on the 99th brightness percentile |
| `target` | 220 | wanted percentile value |
| `band_low` / `band_high` | 195 / 240 | dead band: no change inside |
| `clipped` | 250 | at/above: shorten by `max_shorten` |
| `unlit` | 40 | below: lengthen by `max_lengthen` |
| `max_shorten` / `max_lengthen` | 0.7 / 1.25 | per-update ratio limits |
| `min_us` / `max_us` | 200 / 6000 | hard clamps |
| `sample_step` | 31 | histogram sampling stride |

Correction is `sqrt(target / level)` clamped to the ratio limits, rounded and
clamped to `[min_us, max_us]` (`exposure.cpp:24-31`). With two IR views, the
shorter of the two wanted exposures wins so the dimmer view cannot lengthen the
shared exposure (`exposure.cpp:40-44`, comment `exposure.hpp:43-48`).

These clamps are **not** derived from the device: the `MaxExposure`/`MinExposure`
(`0x20/0x21`) and `MaxGain`/`MinGain` (`0x24/0x25`) opcodes are declared but
never called, and `set_exposure_us`/`set_gain` do not clamp (`device.cpp:415-425`).
See gap **G5**.

### 9.3 Projector / lighting model

Only three setters exist: laser/LED brightness (`0x68`, 0..100), strobe
brightness route 0 and route 1 (`0x70`, route + u16 luminance). There is no
per-frame projector trigger, no pattern selection, no enable/disable opcode and
no read-back path in the library. Which physical emitter each route drives is
**UNK** (docs/PROTOCOL.md:29-31, 252). The repository notes say the light pulses
follow the exposure and that `set_laser_brightness` maps to EXStar's
`LDRGBLuminance` (docs/PROTOCOL.md:144, inference).

---

## 10. Implicit firmware assumptions

These are behaviours the code relies on but never checks. "Breaks on" is the
expected failure if the assumption is false.

| # | Assumption | Where | Breaks on |
|---|---|---|---|
| A1 | Handshake reply contains ASCII `"Shining3"` at bytes 9..16 and is ≥17 bytes | `protocol.cpp:168-171`, `device.cpp:209-210` | firmware changing the marker/offset → open fails `BadReply` |
| A2 | Command replies echo id/group/opcode and use status 0 / value length at byte 8 | `protocol.cpp:91-99`, `154-166` | different reply framing → every command retried 5× then fails |
| A3 | Packet id range 0..253, echoed exactly | `protocol.hpp:33`, `protocol.cpp:123-127` | wider/random ids → `IdMismatch` retries |
| A4 | Interrupt requests are zero-padded to 10/16/50/64/100 bytes | `protocol.cpp:42-67` | different wMaxPacketSize/length policy → short/long transfer errors |
| A5 | Three cameras, one-hot fields `1/2/4`, sub-index in bits 0..2 | `stream.cpp:9-16`, `46-47` | different camera count/mapping → frames dropped or attributed to the wrong camera |
| A6 | 1280×1024, 8-bit frames, no row padding; frame size known a priori | `device.hpp:117`, `calibration.hpp:75-76` | different resolution/bit depth → `SizeMismatch`/`Overflow`, no depth |
| A7 | 32-byte stream header, exact offsets, last flag `== 1` | `stream.hpp:20-25`, `stream.cpp:23-54` | header/layout change → no complete frames, no diagnostic |
| A8 | Transfers of different cameras never interleave (single assembler) | `device.cpp:490` | interleaving firmware → corrupt/dropped frames |
| A9 | Frame id is BE32 at `0x0c` of the last transfer; timestamp BE64 at `0x10` of the first | `stream.hpp:24-25`, `stream.cpp:32`, `44` | different header semantics → mis-paired frames; probe assumes an 8-bit counter for file naming (`tools/einstar-probe.cpp:184`, `204-209`) |
| A10 | Flash page = 4096 B, reply carries it at offset 9, blob = 6568 B, CCF block at `0x39B` with `FQFQ`/type 4 | `device.cpp:371-393`, `calibration.cpp:164-179` | different flash geometry/layout → `read_calibration` fails `BadReply` |
| A11 | CCF files are 264/520 B and the first 32 LE doubles are the model, IR keyed by `kCcfIrKey` | `calibration.cpp:30-48`, `150-162` | different per-unit key/format → `NotARotation`, no calibration (observed, §8.2) |
| A12 | `.data_cm` is exactly 692 B with the 4/38/60 camera offsets | `calibration.hpp:26`, `calibration.cpp:27`, `131-132` | format change → `WrongSize` |
| A13 | Exposure/gain values are µs/gain units in the ranges the host assumes (1..10000 µs, 1..800 gain per docs) | `exposure.hpp:33-34`, docs/PROTOCOL.md:19-20 | device clamps or rejects → `BadReply`, AE loop stalls |
| A14 | Trigger start order is period 200000 → counts 0x11 → period 90000; stop is period 200000 → counts 0 → 1 s settle | `device.cpp:444-457` | different scheduling → no frames or wrong rate; stop always costs 1 s |
| A15 | A trigger period of 0 satisfies the device only after 200000 was written | `device.cpp:445` | firmware rejecting 0 directly → stop/settle path fails |
| A16 | First frame set(s) after start are overexposed and can simply be discarded | `tools/einstar-rgbd.cpp:128`, `integrations/rtabmap/CameraEinstar.cpp:85-87` | different sensor settling → lost good frames or retained bad ones |
| A17 | The device survives an abrupt host exit with trigger/lighting left on | no destructor stop/lights-off (`device.cpp:181`) | laser/light stays on, device keeps streaming |
| A18 | Command replies fit in 1024 bytes; stream transfers fit in `0xA400` | `device.cpp:239`, `491` | larger reply/transfer → `LIBUSB_ERROR_OVERFLOW`, retries/stream drops |
| A19 | No vendor control transfers are needed after enumeration | whole tree | firmware requiring a control-mode switch → unusable |
| A20 | Firmware/FPGA version is irrelevant as long as the framing matches | no version check anywhere (`device.cpp`) | protocol drift between V2.10/V2.14/other → silent misbehaviour |

Firmware-image cross-check (read-only): the V2.10 image contains ASCII
`"Shining3D"` and `"2.10"`, and UTF-16LE `"Shining3d"` + `"SHINING-CAM"` USB
strings; the version string is assembled from the parts `EinScan10_01`,
`_SC130_FX3_V`, `2.10`, `_FPGA_V`, `3.7`, `EN`. So the version is a build
constant, not a capability negotiation.

---

## 11. What would obviously break on another firmware/FPGA version

1. **Resolution/format change** (e.g. 16-bit pixels, different dimensions):
   `start_stream` is called with a fixed frame size and the assembler rejects
   every frame whose total is not exactly that size (`stream.cpp:28-31`).
2. **Stream header change** (camera bits, last flag, frame-id/timestamp offsets):
   no frames complete, or frames are attributed to the wrong camera; nothing is
   logged because `FrameAssembler` events are ignored by `stream_loop`
   (`device.cpp:501-502`).
3. **Interleaved camera transfers**: single assembler mixes payloads
   (`device.cpp:490`), producing `SizeMismatch`/`Overflow` storms.
4. **Different trigger semantics/timing**: `start_scanning` sends a fixed
   3-command sequence and never verifies that frames arrive; no watchdog, no
   retry, no fallback period.
5. **Different calibration blob layout / CCF key**: `read_calibration` fails
   hard (`BadDeviceBlock`/`BadReply`), and there is no fallback except an
   explicit `--calib` path.
6. **Different reply framing** (no `0x02` marker at byte 1, different length
   byte position): replies may still be accepted (marker unchecked) or all
   commands fail; either way no diagnostic distinguishes protocol drift from a
   dead device.
7. **Different `bInterval`/descriptor behaviour on Linux**: not handled at all
   (§2.3).
8. **Different device with the same VID**: enumeration may select it
   (`device.cpp:123-141`); open then fails at the handshake with a generic
   `BadReply`.

---

## 12. Build and test status (summary; details in the code map)

Environment: CachyOS Linux 7.3.1, x86-64, 32 CPUs, GCC 16.2.1, CMake 4.4.5,
Ninja, libusb 1.0.30, OpenCV 5.0.0 installed but its CMake components are named
`calib`/`geometry` (no `calib3d`).

1. Scratch build with `-DEINSTAR_WITH_OPENCV=OFF`: **success** (17 targets).
2. `ctest --test-dir build-libeinstar --output-on-failure`: **1/1 passed**,
   1.28 s; direct run: **40 tests, 0 failed checks**.
3. Original CMakeLists with `-DEINSTAR_WITH_OPENCV=ON
   -DOpenCV_DIR=/usr/lib/cmake/opencv5`: **depth silently disabled**
   (`OpenCV not found: einstar::depth disabled`) because the code requests
   component `calib3d`, which OpenCV 5 no longer provides.
4. Scratch-patched CMake (`calib geometry`): **success** (25 targets);
   `ctest`: **2/2 passed**, 1.60 s; depth test prints
   `coverage 100.0%, median depth 300.00 mm`,
   `slanted plane: coverage 95.2% (plain matching 65.6%), median |error| 0.53 mm`.
5. Clean `HEAD` (without the two uncommitted `<algorithm>` includes): **build
   fails** with `'copy_n' is not a member of 'std'` in `src/device.cpp:233` and
   `tests/fake_scanner.hpp:118`.
6. No hardware: `einstar-probe` reports `0 device(s) with VID 0x3267`; no real
   USB transfer was attempted.

---

## 13. Known unknowns

1. Meaning of stream header bytes `0x00..0x01`, `0x03..0x07`, `0x09..0x0b`,
   `0x18..0x1f`; timestamp clock/epoch/units; frame-id width (BE32 read,
   8-bit assumed for filenames).
2. Numeric device status codes and their mapping to error causes.
3. Whether the XOR mask is ever required by a firmware (default off).
4. Which strobe route drives which emitter; `LDRGBLuminance` semantics.
5. Whether `kCcfIrKey` is universal; what the 264-byte CCF tail encodes; why IR
   CCF files are 65 doubles while only 32 are used.
6. Units of exposure (µs assumed), gain, trigger period and temperature
   (0.5 °C assumed).
7. Contents of the 6568-byte custom-info blob outside the calibration block.
8. `0x58` flash-write payload layout (data at offset 10 per docs; not
   implemented or tested).
9. Whether `0x5d`, `0x62`, `0x67`, `0x6f`, `0x7b` are required for any mode.
10. Product ID(s) the Einstar uses; the code accepts any PID under VID `0x3267`.
11. Whether the device tolerates the 1 s stop-scan sleep being skipped, and
    whether the period is truly µs.
12. Behaviour of the firmware when the host disappears mid-trigger.

---

## Appendix A — Byte examples from the test suite

Set exposure, camera 1 (`mask = 0x02`), 5000 µs (`test_protocol.cpp:11-17`),
packet id 7, wire 16:

```
07 00 10 23 00 00 00 05 02 00 00 13 88 00 00 00
```

Set gain, camera 2 (`mask = 0x04`), 400 (`test_protocol.cpp:19-23`), id 0,
wire 16:

```
00 00 10 27 00 00 00 03 04 01 90 00 00 00 00 00
```

Captured replies (`test_protocol.cpp:89-107`), id 0, marker `0x02`:

```
max width:  00 02 10 16 00 00 00 00 02 05 00              -> u16 0x0500 = 1280
exposure:   00 02 10 22 00 00 00 00 04 00 00 08 88        -> u32 2184
sensors:    00 02 10 51 00 00 00 00 01 03                 -> 3
serial:     00 02 00 04 00 00 00 00 0c 12 34 56 78 9a bc de f0 44 00 00 00
product:    00 02 00 01 00 00 00 00 0c 45 69 6e 53 63 61 6e 31 30 5f 30 31
```

## Appendix B — Reference tables

Command groups: `0x00` system, `0x10` camera (`protocol.hpp:49-52`).

Packet offsets: id 0, group 2, opcode 3, status 4, length 7, payload 8
(`protocol.hpp:35-40`); reply length 8, reply value 9 (`protocol.hpp:43-44`).

Stream offsets: header 0x20, camera 0x02, last 0x08, frame id 0x0c,
timestamp 0x10 (`stream.hpp:20-25`).

Calibration offsets: `.data_cm` cameras at doubles 4/38/60, pair at 26, tail
int32 at byte 676 (`calibration.cpp:27`, `54`, `142`); device block at 0x39B
(`calibration.hpp:110`).
