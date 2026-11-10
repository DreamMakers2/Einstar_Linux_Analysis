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

| F58 | Measured HS acquisition ceiling with libeinstar: **45.1–45.2 MB/s (34.4–34.5 frames/s, frame-to-frame; 45.7 wall-clock including start-up)**. Requesting 68, 30 or 10 ms all yield the same 86.2 ms three-frame group period: the device clamps the cadence rather than dropping frames | A (measured) | `docs/benchmarks/measured_cadence.md` |
| F59 | `(1,1)` at 90–100 ms is sustainable: 39.2–43.6 MB/s (86–96 % of the ceiling), 3 frames/group, exactly the requested period, **zero loss over 30 s** | A (measured) | same |
| F60 | `(3,1)` produces **7 wire frames per trigger** (3 mono slots × 2 IR + 1 colour), so the group period is **3 × the requested period** (68 → 222.5 ms, 90 → 270 ms). The period applies per exposure slot | A (measured) | same |
| F61 | The vendor session's capture thread logs one **slot** per line (2–3 images at 68/90/100 ms). At 68 ms with two IR frames plus a colour frame every third slot the average is **~45 MB/s = 99 % of the measured ceiling**; the 115 `errorCode 1034` timeouts are consistent with operating at the wall | A (log) / B (causal link) | `analysis/cadence/vendor_log_slots.json` |
| F62 | Header[16..23] is a **trigger-group counter**, not a timestamp; header[12..15] is a **slot counter** shared by the IR pair. Loss detection must use the slot stride | A (measured) | same; protocol §3.1 |

## USB 3.0 hardware feasibility (mainboard photos)

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F63 | The Einstar mainboard carries a **CYUSB3014-BZXI** (marking `CYUSB3014-BZX` / `I 2237` = 2022 W37) in the 121-ball 10×10 mm 0.8 mm BGA, plus a **Pango PGL25G** (484-ball), a **Micron D9PTK** DDR3L frame buffer and a **Winbond W25Q64JVSIQ** 8 MB SPI boot flash | C | package markings, `hardware/annotated/A1`, `A3`; ball map `hardware/analysis/fx3_ballout.md` |
| F64 | FX3 orientation in the supplied photographs is fixed three independent ways (A1 laser dot, two silkscreen direction arrows, upside-down marking): **A1 is the bottom-right corner, row A the bottom edge**, so the SuperSpeed balls A3–A6 sit 2.6–5.0 mm from the package's right edge | A | `hardware/annotated/A2`, `hardware/crops/P1_silk_bottom.png` |
| F65 | **Corrected by Addendum 2.** The component side carries a **two-pad footprint across the TX pair** at image (376–470, 988–1084). Both traces are continuous on both sides, so it is **not in series**: it is an unpopulated footprint or a differential shunt. **No on-board series AC coupling is proved.** No SuperSpeed ESD array and no labelled SS test pad were found; the labelled test points remain `DP`, `DN`, `CLK`, `PCLK` | B (topology) / C (value) | `docs/hardware/usb3_feasibility_addendum_2.md` §A2.3.2; `hardware/analysis/reopen/agents/B_traces.md` §5 |
| F66 | The four 0603 capacitors beside J4 are a **shunt** decoupling bank (common rail on both sides), **not** series AC coupling — the one observation that could have inverted the conclusion | B | `hardware/crops/P2_acbank_zoom.png` |
| F67 | The 7-pin external connector's measured allocation (12 V, GND, 4 × USB 2.0, 1 NC) leaves **one** spare contact against the **four** a SuperSpeed link needs; no re-pinning of the supplied cable can bridge that | A (arithmetic) | `docs/hardware/usb3_mainboard_feasibility.md` §6 |
| F68 | Infineon's own USB-2.0-only practice permits A3–A6 **and** B3 (R_USB3, 200 Ω) to be left open, while U3TXVDDQ/U3RXVDDQ must stay powered — so a compliant USB-2.0-only FX3 board may have **no SS PHY bias at all** | A (vendor doc) | AN70707 Rev.*Q App. C, p. 40 |
| F69 | A hand-soldered SuperSpeed tap cannot work: the channel budget is −4…−18 dB at 2.5 GHz, the cable alone takes −7.5 dB, and a 5–10 cm lead violates the length (< 76 mm), 90 Ω ±7 %, reference-plane, no-stub and skew (0.7 ps) rules simultaneously → predicted fallback to HS | C | `hardware/analysis/fx3_ss_layout_requirements.md` §7 |
| F70 | SHINING 3D's own software treats 480 Mb/s as degraded (`AppCommon_E7.dll`: "The current USB dirve is USB2.0" / "Notice: Please change to USB3.0"), and the driver package is branded **U3** — the platform expects USB 3.0 while this model's manual documents USB 2.0 | B | `hardware/analysis/product_teardown_corroboration.md` §6 |
| F71 | The FX3's lower package edge carries **four conductors in two coupled pairs**, not two. Hough lines `c = x + 0.85y` = 2174.1 / 2190.6 / 2234.1 / 2249.1 (r.m.s. 0.6–1.0 px, 18 rows) extrapolate at row A to 1362.4 / 1378.9 / 1422.4 / 1437.4, i.e. **A6, A5, A4, A3** within 8 px and with symmetric inward pair convergence (0.8 mm ball pitch → 0.42 mm coupled pitch) | A (measurement) / B (ball assignment) | `docs/hardware/usb3_feasibility_addendum_2.md` §A2.2; `hardware/analysis/reopen/figs/N1_edge_netmap.png` |
| F72 | The two pairs run left as horizontal pairs at y = 1029/1041 (TX) and y = 1084/1096 (RX), pass a **two-pad footprint across the TX pair** (not in series), and route up the left corridor into the via field beside the component-side 40-pin connector. The final connector-pin connection is on inner layers and is **not** optically established | B (route) / C (pin identity) | Addendum 2 §A2.3, §A2.5 |
| F73 | The conditional daughterboard path has two unpowered gates: (M1/M2) <1 Ω continuity from the two-pad footprint / RX runs to two connector pins per pair, and (M3) a 200 Ω ±1 % B3 reference. Either failing makes the NO-GO final | B (inference) | Addendum 2 §A2.7 |
| F74 | An operator continuity report (annotated inspection photo, `hardware/analysis/reopen/evidence/user_continuity_overlay.png`) shows **six connector-side points in three pairs** that beep toward the FX3 region, plus the two-pad footprint and the "six-trace corridor". This corroborates the connector destination but does not identify the nets; six points for a four-wire SS interface suggests SSTX + SSRX + a third pair (probably USB 2.0 DP/DM) | C (unverified physical report) | Addendum 2 §A2.5.1 |

## Corrections issued

0. **The USB 3.0 hardware NO-GO is withdrawn for the daughterboard path.** The "no bundle of four"
   claim (main report §3.6, F65) was wrong: the FX3 lower edge carries four conductors in two
   coupled pairs mapping to A3–A6, with a two-pad footprint across the TX pair and a route to the
   component-side connector. A conditional GO replaces the NO-GO; the passive external adapter
   remains NO-GO.
   See `docs/hardware/usb3_feasibility_addendum_2.md`.
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

## Physical calibration workflow (2026-11-09)

| # | Finding | Conf | Evidence |
|---|---|---|---|
| F75 | EXStar's quick calibration is **5 groups × 5 steps × 1 frame × 3 cameras = 75 images**. Groups: 0°, x−18°, x+18°, y+18°, y−18°. Steps: distance bands centred at **240/320/400/480/560 mm** (±40 mm) inside the 200–600 mm gate. The angle gate is active **only for group 0** (0/0/180° ±8°); groups 1–4 carry `valid=false`. | A | `rapidCameraCalibrate.xml`; `Sn3DCalibrateLoader` `0x18000b2a0` region (ladder 9420–9460); `analysis/calibration/re/workflow_report.md` |
| F76 | A real successful session (2026-02-09) took **2 min 56 s** from first height event to `calibComputeFinished`, with stage times 7.7 / 11.2 / 58.0 / 78.3 / 35.9 s. The solve took **~1.3 s** and the device write **~0.34 s**. The two y-tilt stages dominate. | A | `log/EXStar_20260209_213118_746.log`, `analysis/logs/calib_window.log` |
| F77 | The vendor's own 25-image corpus is an **8 × 5** grid of markers plus 4 large markers at (2,1), (4,1), (5,1) and the off-lattice **(3.5,3)**; the official SHINING 3D video confirms the physical board. The **marker pitch is 28.0 mm** (17×14 lattice at 14.00 mm, markers every second node; AA/BB per-board distances 150.804/√116 = 14.002, 125.071/√80 = 13.983). The 8×7 drawing is a shared UI illustration, not the Einstar board. | A | `calib/plate.py`, `docs/calibration/plate_spec_recovered.md` |
| F78 | The vendor solve is **per-view extrinsic with K/D frozen** (`sn3DExtrinsicSolver`, Ceres, **loss = nullptr**, `tvec.x += 100 mm`), followed by a joint refinement gated by `isOptK`/`isOptP3D`; the 3-camera path is `CalibrationCCF::TribleCCF` (dongle-gated). The camera model is `K5=[fx,cx,cy,fy/fx,alpha]`, `D5=[k1,k2,p1,p2,k3]`. | A/B | `Sn3DCalibrationJR.dll` `0x1801371d0`, `0x1802dca70`, `0x1800e0180`, `0x180138170`; `analysis/calibration/re/algorithm_report.md` |
| F79 | The result **overwrites the factory `Left/Right/TexCCF.txt` in place and writes the same 3873-byte block to the device at `tagDevData+0x39B`**. There is **no CCF backup** and no joint rollback on failure. | A | `analysis/calibration/re/storage_report.md` §4–6; `0x180003940`, `0x180003540`, `0x180023c40` |
| F80 | A free re-optimisation of the vendor corpus (factory distortion fixed, K + rig + poses free) reaches **0.367 px** per-point RMS, reproduces the IR baseline to **0.014 mm** (158.543 vs 158.557), and the IR relative rotation to **0.20°**. Intrinsics differ from the CCF by 2.8–5.1 px (focal) and 3–9 px (principal point): the CCF is near-optimal but not the reprojection optimum of this corpus. | A | `analysis/calibration/engine/vendor_ablation.json`, `end_to_end_summary.json` |
| F81 | The shipped CCF's **colour record is inconsistent** with the IR pair: relative rotation matches our fit to 0.12°, but translation differs by **17.66 mm** (CCF vs fit), a near-pure y shift; the unit's factory 22-double Tex record agrees with our fit to **1.17 mm**. The 46.6 px mean projection error (range 33.6–77.5) is a stale/mixed-generation record effect, not a frame/decode error. | A (discrepancy) / B (cause) | `analysis/calibration/audit/color_report.md`, `color_audit.json` |
| F82 | The replacement engine's parameter covariance is statistically calibrated: synthetic NEES / n = **1.06–1.09** at 5/10/25 views, and the reported fx σ matches the empirical spread (0.395 vs 0.381 px at 25 views). | A | `analysis/calibration/engine/nees.json` |
| F83 | **Superseded (2026-11-10):** the earlier 1.8×–6.4× adaptive gains were computed with the conditional covariance and are not a valid selection result. With the marginal correction, a 10-view run gave fixed fx 0.105 px vs adaptive 0.114 px (baseline 0.062 vs **0.002 mm**), i.e. adaptive is not consistently better on focal length; the rigorous held-out comparison is in `analysis/calibration/audit/viewcount_report.md`. The diversity constraint is still required. | B / superseded | `analysis/calibration/engine/adaptive_exp.json` (old), viewcount audit |
| F84 | The 25-view sequence is **redundant for scale but not for intrinsics**: 3 views recover the baseline to 0.03 mm, while 5/10/20 views leave 10.5/5.7/3.7 px mean focal bias against the CCF. No single orientation or distance band is sufficient. | A (corpus) | `analysis/calibration/engine/vendor_ablation.json` |
| F85 | The physical plate is an **8×5 lattice of 40 nodes + 1 off-lattice = 41 holes (37 small + 4 large)**; the three on-lattice large markers are (2,1),(4,1),(5,1) and the off-lattice one is (3.5,3). The 8×7 drawing is the same design with rows 0 and 6 added: 57 holes (53 small + 4 large) at (2,2),(4,2),(5,2),(3.5,4). The device corpus and video are 8×5. | A (corpus/assets) / B (same design family) | `analysis/calibration/audit/plate_report.md`, `plate_summary.json` |
| F86 | Held-out validation (5 folds, held-out pose solved from the left camera only) gives **0.378 px** mean held-out RMS vs 0.368 px training — a 3 % gap; the calibration generalises across views. | A | `analysis/calibration/engine/heldout.json` |
| F87 | The guarded ellipse sub-pixel mode plus a per-view rejection gate (`RMS > max(2 px, 3×median)`) reaches **0.351 px** on the vendor corpus vs 0.367 px for the intensity centroid; the gate is required because one ellipse view is grossly wrong. | B | `calib/detect.py`, `calib/pipeline.py`, `analysis/calibration/engine/subpixel.json` |
| F88 | Epipolar consistency (Sampson distance, 871 IR correspondences): the CCF rig gives median **0.181 px** / p90 0.873 px; our rig gives median **0.138 px** / p90 0.753 px. Independent of the reprojection fit; relative geometry only. | A | `analysis/calibration/engine/epipolar.json` |
| F89 | A global 2-D shift of one view is absorbed by its board pose (10 px shift → 0.15–0.30 px fx, 0.019 mm baseline); a non-rigid 8-point corruption is detected by the per-view gate (RMS 4.35 vs 0.31 px median). The gate targets gross failures, not ordinary noise. | B | `analysis/calibration/engine/sensitivity.json` |
| F90 | Covariance-only stopping is insufficient: on a synthetic scene it would stop at 5 views with a 1.72 px focal error. With a minimum-view guard (8) and a held-out check it stops at 8 views with 0.33 px error; the reported σ (0.056 px) is still far below the actual error, so variance is not bias. | A (synthetic) | `analysis/calibration/engine/stopping_demo2.json` |
| F91 | The metric pitch is **resolved from the vendor binaries**: EXStar's calibration lattice is 17×14 at **14.00 mm** (pinned by the per-board `AA`/`BB` distance constraints: 150.804/√116 = 14.002, 125.071/√80 = 13.983), and the physical markers occupy every second node ⇒ **marker pitch 28.0 mm**. The `200x150` profile name is **not** a plate size (shared with 5M/EinScan-S; EinScan Pro spec is 210×150 single-scan range), so the earlier 25.0 mm hypothesis is retracted. The vendor CCF written on the same corpus is 1.0 % below the nominal model (effective 27.72 mm) — a solver scale difference, not a plate property. | A (geometry) / B (1 % interpretation) | `docs/calibration/plate_spec_recovered.md` |
| F92 | **Covariance correction:** `rig_covariance` must be the marginal top-left block, not the conditional `C_rr−C_rv C_vv⁻¹ C_vr`. On 10 synthetic views the conditional fx sigma is **0.0537 px** vs the marginal **0.584 px** (10.9× over-confident). The marginal information equals the Schur complement of the Jacobian to 1e-12 in the plain case. | A | `docs/calibration/covariance_and_stopping.md`; `tests/test_calibration_audit.py` |
| F93 | **Scale-transform correction:** `rescaled` must apply `J C Jᵀ` with `J=diag(1,s)` on translation entries; the old `s²C` inflated intrinsic/rotation variances. Anchor propagation adds `σ_b0² j_b j_bᵀ` and the `∂b/∂x` cross terms. | A | same |
| F94 | The E10 driver calls **`Sn3D_Quar_CCF`/`CalibrationCCF::QuarCCF` (0x1802e5e70)**, not `TribleCCF`; the latter has no importer in the dumped modules. Both use camera 1's first valid view as the shared world frame and relative poses for the other cameras. | A | `FUN_1800e0180` tail; `Sn3DCalibrationJR.dump.txt` 82266+ |
| F95 | After the robust-sandwich correction (`B=Jᵀdiag((w·r)²)J`), synthetic NEES/n is **0.91 / 1.01 / 0.92 / 1.02** at 5/8/12/20 views with fixed kc (3 trials each); with free kc the 60-trial audit gives **1.17–1.32** and 68% coverage 0.64–0.68 (mildly optimistic). | A (synthetic) | `analysis/calibration/audit/uncertainty_report.md`; `tests/test_calibration_audit.py` |
| F96 | Pitch is a pure scale for the planar target: with the baseline anchored, 25.0/27.72/28.0/30.0 mm give identical fx (<1e-6 px), IR rotation (0.2020337°) and RMS (0.3669054); only the raw baseline changes (142.99/158.54/160.14/171.58 mm). Pitch cannot be validated from reprojection — hence the vendor-constant route. | A | `analysis/calibration/audit/pitch_sensitivity.json`; `docs/calibration/plate_spec_recovered.md` |
| F97 | Real-corpus EXStar-prefix capture counts: marginal fx σ = **3.8–4.1 px at k=8**, 1.17–1.31 at 12, 0.74–0.89 at 20, 0.48–0.52 at 25; Δfx vs CCF 2–5 px, RMS 0.37–0.43. Eight fixed-order captures are not a defensible calibration; 20–25 are needed for the standard target on this corpus. | A (CCF-relative) | `analysis/calibration/audit/prefix_quick.json` |
| F98 | Detector audit (52 synthetic stress cases): Canny+sub-pixel edge / conic back-projection reach **0.018–0.029 px** synthetic RMS vs centroid 0.055 px under severe tilt and **0.544 px** with neighbouring holes; on the real vendor corpus the residual floor is **model-limited**, so centroid remains best (0.367 px). Threshold-ellipse should be retired. | B (synthetic) / A (vendor) | `analysis/calibration/audit/detector_report.md` |
| F99 | Solver benchmark (vendor25, 189 params): SciPy dense **9.44 s**, SciPy+analytic **1.21 s (7.8×)**, native C++ LM **0.055 s (172×)**, OpenCV 0.137 s (weaker rig-imposed problem). `OPENBLAS_NUM_THREADS=1` gives 3.4× on the engine for free. Ceres 2.2.0 is installed but **cannot compile** (Eigen headers absent), so it is not production-ready here. | A (runtime) | `analysis/calibration/audit/solver_report.md` |
| F100 | Quick real held-out comparison (5 held-out views, 20 train): k=8 prefix σfx 3.8–4.1 px / held-out 0.575; uniform/random k=8 σ≈1.2 / 0.38–0.42; k=12 σ≈1.0–1.3 / 0.37–0.42; k=20 σ 0.74–0.89 / 0.376; k=25 σ 0.48–0.52 / 0.354. CCF-relative Δfx is non-monotonic (2–5 px), so CCF is not ground truth. | A (CCF-relative) | `analysis/calibration/audit/real_holdout_quick.json` |
| F101 | Held-out view-count audit (10 synthetic scenes × 5 seeds, 5-fold real, UCB95): **8–12 are not a universal replacement**. fx≤1 px UCB95 needs uniform 8 / adaptive 12 / fixed 15; fx≤0.5 px UCB95 is **not reached at 25 by any non-oracle method** (mean 20–25; oracle 8). Baseline/held-out saturate at k≈8; intrinsics dominate. Adaptive beats fixed at equal k (8: 0.89 vs 1.44 px mean) but not oracle (0.35 at 8). The "8–12" headline is withdrawn. | A/B (synthetic + real held-out) | `analysis/calibration/audit/viewcount_results.json` |
| F102 | 60-trial uncertainty audit: with **free_kc=True** NEES/n is 1.17–1.32 and 68% coverage 0.64–0.68 (mildly optimistic); `free_kc=False` is 0.91–1.02. View bootstrap: 4-view resampling changes fx by 1.26–1.60 px vs reported 0.48–0.52 px (ratio 0.32–0.44). Pitch +0.1% gives baseline z≈4.7, +1% z≈42; 1 mm board bow fx z≈6; 10% fixed-kc bias cx z≈12. | A (synthetic) | `analysis/calibration/audit/uncertainty_report.md`, `uncertainty_results.json` |
