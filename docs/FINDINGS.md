# Findings register

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
| F12 | Newest official EXStar found for the Einstar is **1.2.2.0** (2024-06-28); macOS build 1.2.2.5 (negative within the official sources searched: 2023–2024 installers and the 2025–2026 KB pages) | A | `docs/firmware/official_versions.md`, CDN Last-Modified |
| F13 | Newest firmware is **FX3 V2.10 / FPGA V3.7**, byte-identical across 1.0.6.0, 1.2.2.0 and the macOS package | A | SHA-256 equality |
| F14 | **No official artifact contains FX3 firmware newer than V2.10** (searched: all EXStar 1.x installers, the macOS package, KB pages); the `V2.14` string in `libeinstar` is an example, and the real device reports V2.10. This is a verified negative, not a proof of non-existence | A | session log + official archive + `3DDigitalSyncInterface-E10.pdf` |
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

## Streaming, kernel and benchmark findings (session 3)

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F41 | Frame completion is marked by `header[8] == 1` on the **last** packet; the frame is delivered only when the accumulated payload equals the frame size | A | vendor `FUN_18000c8f0`; libeinstar `kOffStreamLastFlag`; hardware-proven parser |
| F42 | Camera id is the **one-hot** field at `header[2]` bits **3..5**; bits 0..2 are a sub-index/chunk counter | A | vendor `(pkg[6] >> 3) & 7`; libeinstar `(t[2] >> 3) & 7`; hardware |
| F43 | The frame id at `header[12..15]` is valid **only on the last packet**; the timestamp at `header[16..23]` is latched from the first packet | A | vendor completion branch + libeinstar |
| F44 | The published driver initially had all three of F41–F43 inverted and passed only because its simulator shared the same error. Fixed; `driver/tests/test_conformance.cpp` now pins the semantics with hand-written golden vectors independent of the simulator | A | `driver/` diff, conformance tests |
| F45 | `bInterval = 0` is repaired by **Linux USB core** to 7 at high speed (`usb_parse_endpoint`: `fls(0*8) == 0 -> 7`); no kernel patch, udev rule or descriptor override is required, and libeinstar captured images on a stock kernel | A | Linux `drivers/usb/core/config.c`; observed CachyOS behaviour |
| F46 | At `ScanTriggerPeriod 68000 µs` with three full frames per trigger the device needs **57.9 MB/s** aggregate; practical USB 2.0 HS (~40 MB/s) cannot carry it, so either the link is SuperSpeed in normal use or the transmitted frame set is reduced | A (arithmetic) / E (which branch is true) | `docs/benchmarks/offline_benchmarks.md` |
| F47 | Async issuance raises saturated throughput 3.2–3.7× over one outstanding transfer **only when the link is the limit**; at the vendor cadence all depths deliver the same frame rate and differ in tail latency only. Zero-copy roughly halves host wall time | A (simulated) | benchmark tables |
| F48 | The vendor session shows 115 `errorCode 1034` timeouts in ~1.5 h; recovery must be per-pipe (`clear_halt`), not device reset | A | session log + vendor reset paths |

| F49 | The command channel is implemented and byte-validated offline: request id/group/opcode, u32 BE argument = payload length, reply status at 4 and u32 BE value length at 5..8, per-opcode wire sizes, five total attempts with per-pipe `clear_halt` recovery | A (offline) | `driver/src/control_channel.cpp`, `driver/tests/test_control_channel.cpp` |
| F50 | The CCF intrinsic key is bounded, not proved: feasible integer keys are fx∈{8,9}, fy∈{-4,-3}, cx∈{-7,-6}, cy∈{-2,-1} (left) and the square-pixel constraint fixes key(fx)−key(fy)=12. A 1 px focal-key error is 0.26 mm in depth at 300 mm, versus 2.9 mm from the baseline spread | A (bound) / E (choice) | `tools/calib/key_analysis.py`, `docs/calibration/model.md` |
| F51 | Real captures (three complete camera triples plus one extra frame) rectify with a median absolute vertical residual of **0.90–1.0 px** under the decoded CCF model; a deliberately mismatched pairing produces no accepted matches, and the dump model only produces window-edge matches (2.0 px) | B (few correspondences; integer-quantised) | `tools/calib/real_capture_check.py` |

| F52 | The FX3 firmware implements and **enables** SuperSpeed: it installs a `bcdUSB 0x0300` device descriptor, a BOS with the SuperSpeed capability, and a 103-byte SS configuration (1024-byte packets, EP 0x83 `bMaxBurst 15`), calls `CyU3PConnectState(CyTrue, CyTrue)` on the real init path, programs the SS PHY registers, and handles a `USB3_LNKFAIL` event. No code path connects with SS disabled | A | `analysis/usb3/firmware_descriptors.txt`; `docs/firmware/fx3_analysis.md` §3; decompiled `0x40004BEC`, `0x40003E44`, `0x4001A86C`, `0x40003E88` |
| F53 | With the scanner connected, the link is **High Speed 480 Mbps**, `bcdUSB 2.00`, device qualifier 2.00, and a BOS request stalls. The host port is SS-capable (usb1-port5 ↔ usb2-port5 peer; other SS devices work at 10/5 Gb/s) | A (observed) | `analysis/usb3/{lsusb_v,sysfs,kernel_log,control_queries}.txt` |
| F54 | The supplied cable has a **USB 3.0 Type-A plug** (blue, 9 contacts, user inspection); the scanner end is a proprietary keyed push-pull connector with 12 V/5 A power injection. The manual says "Interface: USB 2.0", the brochure "USB 2.0 or above", and the successor Einstar 2 uses USB-C | B | manual p.11–12 and §Environmental Requirements; distributor brochure; user observation |
| F55 | SuperSpeed is therefore blocked by the **device-side physical path** (connector and/or PCB), not by firmware or the host. If the PCB does carry SS pairs, the existing firmware would train SS automatically with a suitable cable | C (inference from F52–F54) | this report |
| F56 | At 68 ms and three full frames the HS practical ceiling (~40 MB/s) is **1.45× short** of the required 57.9 MB/s; the vendor log's 115 `errorCode 1034` timeouts are consistent with that deficit | A (arithmetic + log) | `docs/protocol/usb3_capability.md` §6 |
| F57 | The exact FX3 variant (CYUSB3011/3012/3013/3014) is **not recoverable from the firmware image**; a teardown/package marking is required | A (negative) | this report §3 |

## Corrections issued

1. **FX3 V2.14 does not exist** (F14).
2. The `.img` "malformed section chain" is a container artefact, not a firmware defect (F5).
3. The 6-byte EN/CH difference is locale + derived checksums only (F6).
4. libeinstar's calibration key values are wrong (F36) even though its *approach* (per-field
   integer offsets on the same 32 doubles) was directionally right.
5. The stream "first packet" flag is actually the **last**-packet flag (F41).
6. The camera field is bits 3..5, not 0..2 (F42).
7. The frame id is not usable per packet (F43).
8. `bInterval=0` needs no kernel change; USB core repairs it (F45).
9. "300 MB/s simulator" is a modelling choice, not scanner performance (F46/F47).
