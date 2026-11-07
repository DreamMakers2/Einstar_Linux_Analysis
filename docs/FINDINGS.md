# Findings register


> **Publication note.** Paths such as `sources/…` and `analysis/logs/…` refer to the private
> research workspace and are not part of this repository; see `analysis/README.md` for the
> fixture layout and the SHA-256 of each excluded input.

Numbered claims with confidence and evidence. Confidence: **A** = mathematically proved /
byte-exact verified, **B** = code-verified in a vendor binary, **C** = cross-validated from two
independent sources, **D** = inference, **E** = open question.

## Firmware container & image

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F1 | The `.img` is a transport container of 4097-byte records (4096 payload + 8-bit sum), 320 records for a 1,311,040-byte file | A | `docs/firmware/format.md` §2, 320/320 checksums, three files |
| F2 | De-interleaving yields a 1,310,720-byte AN76405 FX3 image: `"CY" 1C B0`, 5 sections, terminator, valid 32-bit checksum | A | same §3; stored = computed = `0x0FE314BC` |
| F3 | Sections load to `0x100` (9,512 B), `0x40003000`, `0x40013000`, `0x40023000`, `0x40030000` | A | same §3 |
| F4 | Entry point `0x40016F50`; V1.0 image entry `0x40015488` | A | terminator `dAddress` |
| F5 | The earlier "2-byte shift at 0x2534" is exactly the record container's inserted checksum byte | A | `file = image + floor(image/4096)` |
| F6 | EN vs CH differ in one locale word at SRAM `0x4000D7F0`, the FX3 checksum (−0x602), and two record checksums (−8 each) | A | byte diff + arithmetic, `tests/test_firmware.py` |
| F7 | FPGA payload: image offset `0x40190`, 1,005,676 B, identical EN/CH, differs between V2.6 and V3.7 with the same length | A | SHA-256 equality / diff count 526,936 |
| F8 | FPGA payload entropy is 3.19 (not compressed/encrypted); 62 % of 32-bit words have a zero top byte; command words `AA/A8/AB/AC/AD/AE` and NOP `A0000000` exist | A | `analysis/firmware` measurement, §FPGA doc |
| F9 | The vendor updater verifies the same 4097-byte checksums host-side before sending | B | `einscan10.2.0.0.dll` `FUN_18000bc80` |
| F10 | V2.10 added an in-firmware update path (`ucCheckByte != ucCheckCal`), full USB request handling, pipe/DMA recovery and A/B slots | B | `docs/firmware/firmware_evolution.md`, string diff |
| F11 | Flash layout: app slots `0x40000`/`0x1C0000`, FPGA slots `0x80000`/`0x200000`, app-flag sector at `0x3F000`; the device erases/programs/read-back-verifies 4 KiB pages, then writes the 12-byte flag and reboots | A (code-proven) | `FUN_4000B320`, `FUN_4000C290`, SPI helper `FUN_40007F64` (`addr>>8`) |

## Versions & provenance

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F12 | Newest official EXStar for the Einstar is **1.2.2.0** (2024-06-28); macOS build 1.2.2.5 | A | `docs/firmware/official_versions.md`, CDN Last-Modified |
| F13 | Newest firmware is **FX3 V2.10 / FPGA V3.7**, byte-identical across 1.0.6.0, 1.2.2.0 and the macOS package | A | SHA-256 equality |
| F14 | **No FX3 V2.14+ exists**; the `V2.14` string in `libeinstar` is an example, and the real device reports V2.10 | A | session log + official archive + `3DDigitalSyncInterface-E10.pdf` |
| F15 | Official recovery artifacts exist only in 1.0.6.0: `Fx3BootAppGcc.img` (29,876 B) and a raw V1.0/V2.6 image (1,310,720 B) | A | `sources/firmware/official_downloads/` |
| F16 | No public Linux SDK; `Shining3D/EinstarSDK1.4` and `EXStarSDK1.6` are private | A | negative result, official pages |

## USB protocol

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F17 | Device is `3267:0003`, interface 0, class `EF/02/01`, 5 endpoints `01/81` (int), `02/82` (bulk), `83` (bulk IN) | A | firmware descriptors at image `0xDF18/0xDF55/0xDF7E` |
| F18 | `bInterval = 0` on the interrupt endpoints is a firmware descriptor bug (invalid for HS); Linux must patch to 7 | A | descriptor bytes + libeinstar workaround |
| F19 | Command channel: EP1 interrupt, header `[id][?][cmd u16 BE][arg u32 BE][payload]`; replies carry status at 4 and `u32 BE` value length at 5..8, value at 9 | A | host `FUN_180009130/FUN_180008f90` + device `FUN_40008D70`, independently decoded |
| F20 | Retry policy: 5 attempts, `resetCommandPipe(3)`, 20 ms sleep; success requires echoed id/group/opcode and status 0 | B | `FUN_180007850` |
| F21 | Image stream: `[u32 BE len][32-byte header][payload]`, 32 packets × 40,960 B per 1280×1024 frame, `len = 0xA020` | C | `FUN_18000c8f0`, `FUN_18000c700`, libeinstar §7 |
| F22 | Stream header: byte 2 = one-hot camera (bits 3..5) + sub-index (bits 0..2); byte 8 = last-packet flag; u32 BE frame id at 12; u64 BE timestamp at 16 | C | vendor + libeinstar agree |
| F23 | Vendor transport settings: 0xA400 transfer size, 32 packets/unit, 100 cached units, 100 ms grab timeout | B | `FUN_1800034c0`, `FUN_180007cf0` |
| F24 | Real-world reliability: 115 `grabImage failed. errorCode: 1034` timeouts in a 1.5 h vendor session | A | `analysis/logs/session_2026-02-09.log` |
| F25 | Vendor scan settings: `TriggerMode {MONOTRIGCOUNT:3, RGBTRIGCOUNT:1}`, `ScanTriggerPeriod 68000 µs` | A | session log |
| F26 | Firmware update: system op `0x06`, announce with `u32 BE payload_size`, then `u32 BE chunk+1` + 0x1001-byte records over bulk; the FX3 strips the checksum byte, programs the de-interleaved page, reads it back and compares the sum | A | host `FUN_18000bc80` + device `FUN_4000B320` |
| F27 | `ReadData(0,6568)` on a live device returns the 6568-byte calibration blob | A | session log line 21:31:25.202 |
| F28 | File-name patterns `*.img`, `_*_*_`, `_JAP.img` and the external `Fx3Bootloader.exe -i … -t SPI` recovery path | B | `Sn3DDeviceEinStar.dll` strings, `restoreBlock` |
| F29a | No vendor `bRequest 0xA0` handler exists in the app; requests are dispatched on `bmRequestType` and the real protocol is data-endpoint based | A | `FUN_40004210` | 
| F29b | FPGA has two interfaces: bitstream read over SPI (`FUN_40008218`, 0x180 × 4 KiB from the FPGA slot, GPIO `0x33` toggling) and a runtime register interface over I2C slave `0x20` (8-bit register + 32-bit BE value) used by LED/work-mode/status commands | A | FX3 analysis §8 |
| F29 | Scanner ownership chain: `EXStar.exe → Sn3DEinStarController → Sn3DDeviceEinStar → QProcess/QtTunnel → SnSyncService.exe → einscan10 plugin → CommunicateLayer → SH3DU3Driver.sys/cyusb3.sys` | B | PE import graph + module strings |

## Calibration

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F30 | `LeftCCF.txt`/`RightCCF.txt`/`TexCCF.txt` are **binary** little-endian float64 records, not text | A | `docs/calibration/ccf_format.md` |
| F31 | Record = 33 doubles (MATLAB Calib_Results order: `fc, cc, alpha, kc, T, R, error` with pads); IR records append 256 B (64 int32, 0..9) | A | exact match to EXStar's text `LeftLongCCF.txt` |
| F32 | IR doubles are obfuscated by a **per-field integer offset**; the rotation key `(-2,-4,-11,-9,0,5,-11,-12,0)` is the *unique* solution of 2⁹ candidates making both IR rotations orthonormal det=+1 | A | exhaustive search, residual 2e-15; reproduced independently |
| F33 | Distortion key `(0,6,2,7,13)` is proved by an independent unkeyed 22-double record in the flash blob (exact integer residuals ≤ 6.3e-16) | A | `validate_ccf.py` 40/40 |
| F34 | Colour record needs no key (det = 1, residual 5e-16) | A | same |
| F35 | Geometry: baseline L–R **158.557 mm**, L–Tex 125.318, R–Tex 38.628; relative IR rotation 22.138°; all rotations det = 1 to 1e-15 | A | independent recomputation from the decoded poses |
| F36 | libeinstar's 32-element key is the right *kind* but wrong *values*: it leaves det = −75.9 / −80.3, i.e. the reported "rotation is not orthonormal" | A | numeric check |
| F37 | The 6568-byte blob is fully mapped: header `(1,1280,1024)`, 3×22-double records, CCM/dark blocks `FAFA/FBFB`, CCF block at `0x39B`, wbdata, `FWFW`, `AABB`, serial `<redacted-serial>`, P/N `<redacted-part-number>`, date `<redacted-production-date>` | A | byte-exact offsets |
| F38 | Remaining calibration unknowns: absolute `fx/fy/cx/cy` key (only `key[0]−key[1]=12` forced), `key[20..22]`, meaning of the 64-digit trailer, where the key is applied | E | §10 of the calibration doc |

## Acquisition engineering

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F39 | The vendor stack is synchronous per-frame with a 100 ms timeout and a single reassembly accumulator shared by three cameras | B | `FUN_18000c8f0` |
| F40 | An asynchronous, per-camera, bounded-queue pipeline with clear_halt recovery is required to do better; the design is implemented in `driver/` with a deterministic simulator | — | `driver/` |

## Corrections issued

1. **FX3 V2.14 does not exist** (F14).
2. The `.img` "malformed section chain" is a container artefact, not a firmware defect (F5).
3. The 6-byte EN/CH difference is locale + derived checksums only (F6).
4. libeinstar's calibration key values are wrong (F36) even though its *approach* (per-field
   integer offsets on the same 32 doubles) was directionally right.
5. The stream "first packet" flag is actually the **last**-packet flag (F22).