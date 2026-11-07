# Einstar (EinScan10 / E10) USB protocol — consolidated specification

This document merges two independent evidence sources:

* **W** — the vendor Windows implementation (`syncservice/device/einscan10.2.0.0.dll`,
  `syncservice/CommunicateLayer.dll`, `Sn3DDeviceEinStar.dll`, decompiled with Ghidra),
  the vendor's own interface manual `syncservice/doc/3DDigitalSyncInterface-E10.pdf`, and a
  637,523-line real device session log (`analysis/logs/session_2026-02-09.log`);
* **L** — the open-source Linux driver `libeinstar` (code-verified protocol, see
  `docs/protocol/libeinstar_protocol_spec.md`).

Where they agree, confidence is raised; where they disagree, both readings are shown with the
resolution. Method and per-item sources are given inline. Confidence codes: **CV** (code
verified in vendor binary or libeinstar), **HW** (observed on hardware, from the session log),
**DOC** (vendor documentation), **INF** (inference), **UNK** (unknown).

---

## 1. Identity, descriptors and endpoints

| Item | Value | Source |
|---|---|---|
| VID:PID | `3267:0003` | W: firmware device descriptor @ image `0xDF18`; official `SH3DU3DRIVER.inf` maps `USB\VID_3267&PID_0003` → `SHINING3D_E10 = "Einstar"` |
| bcdUSB | `0x0200` (device descriptor) and a USB 3.0 descriptor set (`0x0300`, 1024-byte packets) also present | W: image `0xDF18` / `0xDF2C` |
| Class | `EF/02/01` (miscellaneous, IAD composite) | W: config descriptor @ image `0xDF55` |
| Interfaces | 1 (interface 0, alt 0, 5 endpoints) | W |
| Endpoints | `0x01` INT OUT, `0x81` INT IN, `0x02` BULK OUT, `0x82` BULK IN, `0x83` BULK IN | W: endpoint descriptors @ image `0xDF7E..0xDF9F`; L §2.2 |
| Max packet | 512 B (HS set), 1024 B (SS set) | W |
| `bInterval` | **0** for `0x01`/`0x81` — invalid per USB 2.0; **Linux USB core repairs it to 7 automatically**, no kernel patch or user-space override needed | W (descriptor), Linux `usb_parse_endpoint()`, libeinstar (captured images on a stock kernel) |
| Endpoint roles | EP1 = command channel (request/reply), EP2 = bulk data (page read/write, firmware update), EP3 IN = image stream | W (function roles), L |

The five-endpoint set is **exactly** what the vendor firmware declares, which independently
confirms libeinstar's endpoint use and rules out a second hidden interface.

## 2. Command packet (EP1, interrupt)

### 2.1 Request (host → device)

| Off | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | packet id | increments modulo 0xFE; vendor uses a per-device counter starting at 0 (`FUN_180005950`) |
| 1 | 1 | constant | vendor sends `0x00`; libeinstar sends `0x02` (not validated by the device) |
| 2 | 2 | command word, **u16 BE** | high byte = group (`0x00` system, `0x10` camera), low byte = opcode |
| 4 | 4 | argument, **u32 BE** | for normal commands this is the payload length `L` (so byte 4 = 0, byte 7 = L for L < 256) |
| 8 | L | payload | little-endian fields; first payload byte is a flag in the update command |

The device side (`FUN_40008D70` on EP1, `FUN_4000BE60` on EP2) decodes exactly this:
`[seq][?][cmd BE16][arg BE32][payload]` — an independent confirmation of the host layout.

Wire sizes are fixed per opcode (10, 16, 50, 64, 100 or 0x1400 bytes); the device replies with
the same buffer contents overwritten. The host validates: reply byte 0 = request byte 0,
reply byte 2 = request byte 2, reply byte 3 = request byte 3, reply byte 4 = 0. On mismatch it
resets the command pipe (`resetCommandPipe(handle, 3)`) and retries up to **5** times with a
20 ms delay (W: `FUN_180007850`). libeinstar applies the same rules (L §3.6).

### 2.2 Reply (device → host)

| Off | Size | Field |
|---|---|---|
| 0 | 1 | packet id echo |
| 1 | 1 | constant |
| 2 | 1 | group echo |
| 3 | 1 | opcode echo |
| 4 | 1 | status: `0` = success, non-zero = error |
| 5 | 4 | value length, **u32 big-endian** |
| 9 | N | value |

> **Cross-validation / correction of L:** libeinstar reads the value length as a single byte at
> offset 8. The vendor code reads a 32-bit big-endian length from offsets 5..8
> (`FUN_180008f90`, `FUN_180009130`: `CONCAT31([5],[6],[7],[8])`). For all observed replies
> N < 256 so both agree; the vendor layout is the general form.

### 2.3 Transport parameters (vendor)

`connectDeviceBySerial` (W: `FUN_1800034c0`) sets, after attach:

```
setDriverAttemptPkgSize (0xA400)   // per-transfer packet size
setDriverPkgCountPerUnit(0x20)     // 32 packets per unit
setDriverCacheUnitCount (100)      // 100 cached units
setMaxGrabWaitTime      (100000)   // 100 ms frame timeout
```

`CommunicateLayer.dll` exposes these as named exports and talks to the Cypress `cyusb3.sys`
driver (`CreateFileW`, `DeviceIoControl`, `ReadFile`, `WriteFile`, `SetupDi*`), i.e. the vendor
path is a **kernel driver with overlapped I/O**, not WinUSB/libusb.

## 3. Image stream (EP `0x83`, bulk IN)

Reverse engineered from `ImageStreamThread::run` (W: `FUN_18000c8f0`) and the stream setup
`FUN_18000c700` (`image_size = 0x140000`, `packet_size = 0xA400`).

Each packet:

```
+0x00  u32 BE  len          = 32 + payload_len          (range 33..0xA400 valid)
+0x04  u8[32] header
+0x24  u8[len-32] payload
```

Header fields observed:

| Header off | Packet off | Meaning | Confidence |
|---|---|---|---|
| 2 | 6 | one-hot camera id in bits 3..5 (`1`, `2`, `4`) and a sub-index in bits 0..2 | CV (W + L agree) |
| 8 | 0x0C | `1` = **last** packet of a frame (frame is delivered on this packet) | CV — vendor `FUN_18000c8f0` and libeinstar agree |
| 12..15 | 0x10..0x13 | u32 BE **slot counter** (valid on the last packet): +1 per mono exposure slot; both IR cameras share the slot id, the colour frame carries the last slot's id | **Measured** (2026-11-07) |
| 16..23 | 0x14..0x1B | u64 BE **trigger-group counter**, latched from the first packet of the group: +1 per trigger group, identical for every frame in it. **Not a clock** | **Measured** (2026-11-07) |
| others | — | not yet decoded | UNK |

Framing rules (CV): a frame is 32 packets × 40,960 payload bytes (`len = 0xA020`,
1,310,720 bytes total). The frame is delivered only on the packet whose header byte 8 is `1`
("last"); if that last packet arrives when `accumulated + payload != image_size`, the vendor
logs `Error:missed hasReceived…` and drops the partial frame. The camera selector is the
one-hot field in header byte 2 bits 3..5 (`1/2/4`), with bits 0..2 as a sub-index; libeinstar
(`libeinstar_protocol_spec.md` §7.1) reads the same fields. The vendor keeps one reassembly
accumulator and relies on the stream being grouped per camera.

### 3.1 Definitive framing semantics (three independent sources)

The three primary sources agree, and the earlier implementation in `driver/` did not:

| Question | Answer | Evidence |
|---|---|---|
| Which header byte marks the end? | `header[8] == 1` on the **last** packet; the frame is delivered only then | W: `FUN_18000c8f0` completes only when `pkg[0x0C] == 1` **and** accumulated size equals the frame size; L: `kOffStreamLastFlag = 0x08`, `last` required for `FrameComplete`; HW: libeinstar captured frames on real hardware |
| Camera encoding | **one-hot** in `header[2]` bits **3..5** (`1`,`2`,`4`), sub-index in bits 0..2 | W: `(pkg[6] >> 3) & 7` extracted as one field; L: `(t[2] >> 3) & 0x7` with `{1:0, 2:1, 4:2}`; HW-proven |
| Frame id validity | `header[12..15]` is **only valid on the last packet** | W: the id is read in the completion branch; L: `read_be32(t, kOffStreamFrameId)` only when `last` |
| Timestamp | `header[16..23]` u64 BE, latched from the **first** packet of the frame | W: latched when `received == 0`; L: `if (filled_ == 0) timestamp_ = read_be64(...)` |
| Start of frame | **no start flag**; a frame starts implicitly after the previous one completes | W/L; therefore a decoder must keep one in-flight slot per camera and must not key on the frame id |

The published `driver/` initially implemented the opposite (`first_of_frame`, camera in bits
0..2, frame id on every packet). That was a real defect: it passed only because the simulator
shared the same wrong convention. It is fixed, and `driver/tests/test_conformance.cpp` now
locks the semantics with hand-written golden vectors that do not involve the simulator.

### 3.2 `bInterval = 0` is repaired by Linux, not patched by user space

`drivers/usb/core/config.c::usb_parse_endpoint()` contains:

```c
case USB_SPEED_HIGH:
    n = fls(d->bInterval * 8);
    if (n == 0)
        n = 7;              /* 8 ms = 2^(7-1) uframes */
    j = 16;
...
if (d->bInterval < i || d->bInterval > j) {
    dev_notice(..., "has an invalid bInterval %d, changing to %d", ..., n);
    endpoint->desc.bInterval = n;
}
```

With `bInterval = 0` at high speed, `fls(0) == 0` so the kernel assigns **7** to both
interrupt endpoints before any driver claims the interface. libusb still *reports* the raw
descriptor value (it reads the cached descriptor bytes), which is why a detector may see 0;
nothing needs to be installed into the kernel. libeinstar worked on a stock CachyOS kernel for
exactly this reason. The backend therefore reports the quirk and continues — it must not
refuse to claim, and no kernel/udev change is required.

Observed reliability: the session log contains **115 `grabImage failed. errorCode: 1034`**
(timeout) events over ~1.5 h of scanning — the vendor stack itself drops frames under load.
That is a concrete argument for the asynchronous, loss-aware backend (§`driver/`).

## 4. Command catalogue (opcode table)

Camera selector for pixel commands: `mask = 1 << camera` (`0x01`, `0x02`, `0x04`).

### 4.1 System group (`group = 0x00`)

| Name | Op | Wire | Request payload | Reply value | Confidence / evidence |
|---|---|---|---|---|---|
| ProductName | `0x01` | 100 | — | NUL-terminated string `EinScan10_01` | **CV-W** `FUN_180009130` + **CV-L** |
| Serial | `0x04` | 64 | — | 8 bytes rendered as 16 hex digits (example synthetic: `0123456789ABCDEF`) | **CV-L**, **HW** log |
| FirmwareVersion | `0x05` | 64 | — | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN` | **CV-W** `FUN_180008f90` + **HW** log + **CV-L** |
| FirmwareUpdate | `0x06` | 0x1400 | see §5 | — | **CV-W** `FUN_18000bc80` |
| ButtonTemp | `0x07` | 50 | — | bytes 9,10,11 = BUTTON0/1/2 state; bytes 12..13 = u16 BE board temperature / 100.0 | **CV-W** `FUN_180008d00` (new, not in L) |
| Reboot | `0x08` | 10 | — | — | **CD-L** (never sent by libeinstar) |

### 4.2 Camera group (`group = 0x10`)

The full 28-opcode table with wire sizes and semantics is in `libeinstar_protocol_spec.md`
§4.2 (code-verified). Windows-verified additions/refinements:

| Name | Op | Wire | Verification |
|---|---|---|---|
| Camera u16 query | `0x01` | 16 | **CV-W** `FUN_180009090`: no payload, reply u16 BE at 9..10. libeinstar declares this opcode ("ProductId") but has no call site; it is a valid 16-byte query returning a u16. |
| GetFirmwareish string | `0x05`?? | — | not a camera opcode — see system `0x05` |

Everything else in the camera group (exposure `0x22/0x23`, gain `0x26/0x27`, trigger switch
`0x40/0x41`, trigger period `0x48/0x49`, temperature `0x50`, sensor count `0x51`, page read
`0x57`, laser `0x67/0x68`, strobe `0x6f/0x70`, heartbeat `0x7b`) is consistent with the vendor
API semantics documented in `3DDigitalSyncInterface-E10.pdf` (properties `CameraExposure`,
`CameraGain`, `ScanTriggerPeriod` in µs, `TriggerMode {MONOTRIGCOUNT, RGBTRIGCOUNT}`,
`StrobeLuminance {ROUTEINDEX, LUMINANCE}`, `LDRGBLuminance`, `Indication`, `Progress`).

## 5. Firmware update protocol (W: `FUN_18000bc80`, `FUN_180002f50`)

The host reads the whole `.img`, verifies the 4097-byte record checksums locally, then:

1. **Announce** (`FUN_4000B22C` on the device): system `0x06`, wire 0x1400, header
   `[id][0][0x00][0x06][00 00 00 05][0x01][u32 BE total_payload_size]`.
   The device copies bytes 9..12 (big-endian) into its total-size counter, enters update mode
   and logs `Firmware size is %d, update starting...`. `total_payload_size` = file size − number
   of 4097-byte records = the size of the de-interleaved image (0x140000).
2. **Stream** (`FUN_4000B320`): each packet `[id][0][0x00][0x06][u32 BE chunk+2][flag][chunk bytes][checksum]`
   carries one 4097-byte record: the device computes `payload_len = arg − 2`, takes the checksum
   byte at offset `9 + payload_len`, erases one 4 KiB flash sector, programs `payload_len` bytes,
   **reads them back**, sums them and compares with the checksum byte. On success it advances the
   page counter; a mismatch logs
   `Firmware update package ucCheckByte(0x%x) != ucCheckCal(0x%x)` and fails the update.
   After the last page it erases sector `0x3F`, writes the 12-byte app-flag at `0x3F000`,
   clears update mode and sets the reboot flag (serviced by the EMC watchdog thread).
3. **Progress** is exposed as device property `Progress` (0..1000): the host updates it from
   `(sent * 0x352) / total + 100` after the announcement and to 1000 at the end.

The device writes the de-interleaved image to its SPI flash using the A/B slot scheme in
`docs/firmware/format.md` §7. The FX3 boot image inside the payload is what the FX3
bootloader will execute on the next reset; the FPGA region is used by the FX3's own
"FPGA Boot Thread".

**Recovery path**: the vendor's `Sn3DDeviceBase::restoreBlock` launches an external
`Fx3Bootloader.exe -i <file> -t SPI` (or a `cmd.exe /k "cd /d … && Fx3Bootloader.exe -i …"`),
which talks to the FX3 in **Cypress boot mode** (VID `04B4`, PID `00F3`, per the bundled
`cyusb3.inf`). The recovery payloads are in `sources/firmware/official_downloads/`
(`Fx3BootAppGcc.img`, `EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img`). The tool itself is not
shipped with EXStar 1.2.2.0 (only referenced); it is present as a separate recovery bundle in
the 1.0.6.0 installer.

## 6. Bulk page access (flash/custom info), W: `Execute("ReadData"/"WriteData")`

Vendor API (`3DDigitalSyncInterface-E10.pdf` §6):

* `execute("ReadData",  [offset:int, size:int]) -> QString` (Base64 of the raw bytes)
* `execute("WriteData", [offset:int, data:QString])` (Base64)

Observed in the real session log:

```
ReadData  offset=0     size=6568   -> 6568 bytes  (the factory calibration blob)
WriteData offset=923   len=3873    -> record starting with u32 4, then zeros
WriteData offset=4796  len=153     -> record starting with u32 5, then coefficients
```

This confirms the 6568-byte private `calibration.bin` is **device page 0, read verbatim over
`ReadData(0, 6568)`**, and that the storage is addressed as a flat byte space. libeinstar
implements this as camera opcode `0x57` (page read, 4096-byte pages over bulk `0x82`)
(L §6); the vendor offsets are byte offsets into the same space.

## 7. Vendor API semantics (from the official E10 manual)

Properties (get/set): `CameraGain` (per camera 0/1/2, unit 1 % dB), `CameraGainRange`,
`CameraExposure` (µs), `CameraExposureRange`, `CameraExposureLevel`, `CameraResolution`,
`CameraSerial`, `SensorCount`, `VendorName`, `Vid`, `ProductName`, `Pid`, `FirmwareVersion`,
`Serial`, `ScanTriggerPeriod` (µs), `HeartBeatPeriod`, `Temperature`, `LDRGBLuminance`,
`StrobeLuminance` (indexed), `TriggerMode {MONOTRIGCOUNT, RGBTRIGCOUNT}`, `ConvertParam`
(per sensor: HORFLIP, VERFLIP, ENABLEROTATE, CLOCKWISEROTATE, ROTATEANGLE), `Progress`
(firmware update, 0..1000), `Indication {DISTANCE, DEVICESTATE}`.

Flags: `ImageStream` (enable/disable video streaming).
Execute: `UpdateFirmware(filePath)`, `WriteData(offset, base64)`, `ReadData(offset, size)`.
Callbacks: `imageData` (device, serial, `GrayImage`/`RGBImage`, shared-memory names per camera,
extra params) and `buttonData` (`BUTTON0/1/2`, `NoClick/SingleClick/DoubleClick/LongClick`).
Error space: `SyncService*` codes, notably `SyncServiceFoundCypressFX3Error` (device stuck in
FX3 boot mode), `SyncServiceFirmwareVersionTooLow/HighError`, `SyncServiceUsbNotU3Error`.

The vendor session log additionally shows the operational parameters used by EXStar for
scanning: `TriggerMode {MONOTRIGCOUNT:3, RGBTRIGCOUNT:1}` and `ScanTriggerPeriod: 68000 µs`
(the first calibration session used `{1,1}` and `100000 µs`). libeinstar's default `66666` is
therefore a library default, not the vendor scanning value.

## 8. Corrections to earlier assumptions

1. **"FX3 V2.14"** appears in `libeinstar` documentation only as an *example string*. The real
   device reports `…FX3_V2.10_FPGA_V3.7_EN` (HW log, 2026-02-09), and every official installer
   from 2023 to 2024 ships V2.10 (see `docs/firmware/official_versions.md`). There is no
   evidence that V2.14 exists for this product.
2. The "invalid interrupt `bInterval=0`" is a **firmware descriptor bug** (confirmed at
   image 0xDF7E/0xDF85), not a Linux quirk.
3. The 6-byte EN/CH difference is a locale tag plus two derived checksums — no functional
   difference (§`docs/firmware/format.md` §5).

## 9. Open questions

* Header bytes 0..1, 3..7 and 24..31 of the image packet are undecoded.
* Whether the image stream carries any flow control (the vendor relies on 100 ms timeouts).
* Exact semantics of camera opcode `0x01` (returns u16) and system opcode `0x07` bit fields.
* The device-side `ReadData`/`WriteData` command encoding (vendor implements it inside the
  closed-source `einscan10` plugin; libeinstar's `0x57` page read is code-verified but the
  write opcode is blocked in libeinstar).
