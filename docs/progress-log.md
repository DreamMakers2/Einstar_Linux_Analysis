# Progress log


> **Publication note.** Paths such as `sources/…` and `analysis/logs/…` refer to the private
> research workspace and are not part of this repository; see `analysis/README.md` for the
> fixture layout and the SHA-256 of each excluded input.

Chronological, UTC-ish. Newest last.

## 2026-11-07 — session 1

1. **Workspace** created under `<repo>`; sources copied read-only from
   the Windows mount; SHA-256 manifest generated (`sources/SHA256SUMS.txt`).
2. **Firmware puzzle solved.** The `.img` is a 4097-byte-record container. De-interleaving gives
   a valid AN76405 FX3 image: 5 sections, entry `0x40016F50`, checksum `0x0FE314BC` verified.
   Root cause of the earlier "parse fails at 0x2534" and the "2-byte shift": one checksum byte
   is inserted after every 4096 bytes, so file offset = image offset + floor(image/4096).
   Tool: `tools/firmware/fx3img.py`; tests: `tests/test_firmware.py` (10 pass).
3. **EN/CH explained** exactly: locale word at image `0xCD2C` (SRAM `0x4000D7F0`), FX3 checksum
   delta `0x602`, two record checksums delta 8 each. FPGA payload byte-identical.
4. **Official version archaeology** (delegated, verified): newest EXStar for Einstar `1.2.2.0`
   (2024-06-28), firmware V2.10/V3.7 identical across 1.0.6.0, 1.2.2.0 and the macOS 1.2.2.5
   package. **No V2.14+ exists.** New official artifacts archived: `Fx3BootAppGcc.img` and a
   raw V1.0/V2.6 recovery image (both parse and checksum-verify).
5. **Windows architecture**: scanner is owned by `SnSyncService.exe`, not the UI; the device
   plugin `einscan10.2.0.0.dll` is the authoritative protocol implementation;
   `CommunicateLayer.dll` exposes the transport API; drivers are `SH3DU3Driver.sys` /
   `cyusb3.sys`. Full 741-module PE inventory produced (`docs/windows/module_map.md`).
6. **Protocol**: vendor E10 manual recovered (`3DDigitalSyncInterface-E10.pdf`); decompiled the
   device plugin (469 functions) and recovered the image-stream framing, transport parameters,
   command header, firmware-update opcode and page read/write API. Cross-validated against
   libeinstar (independent code-verified implementation) — layouts agree.
7. **Real device log** (132 MB, from the Windows install) recovered and analysed: 637,523 lines,
   full command history, frame sizes, trigger parameters, 115 frame timeouts. Confirms firmware
   string, serial `<redacted-device-serial>`, `ReadData(0, 6568)` for calibration.
8. **FX3 firmware** imported into Ghidra via a generated ELF (correct load addresses); 518
   functions decompiled. Located ThreadX threads, FPGA boot thread, `GetFpgaLoadAddr`, SPI flash
   access, SC130GS sensor strings, A/B slot logic.
9. Delegated: calibration decode, libeinstar audit, FPGA bitstream identification, asynchronous
   acquisition backend, reconstruction pipeline survey, FX3 functional map.

### Open at end of session 1

* Calibration layout not yet proven (agent in progress).
* FPGA vendor/family not yet identified (agent in progress).
* Driver backend implementation in progress.
* Flash layout `0x80000`/`0x200000` is structurally inferred, not observed on hardware.

## Session 1 — later work

10. **Offline protocol model** (`tools/protocol/einstar_proto.py`, 12 tests): command/reply framing,
    per-camera stream demux, firmware-update state machine with the real 0x1400 wire layout,
    record pack/unpack. Plus `tests/test_log_replay.py`, which cross-checks the model against the
    real 630k-line vendor session log (all operations/properties modelled; firmware string,
    `ReadData(0,6568)`, `TriggerMode (3,1)` and `ScanTriggerPeriod 68000` confirmed).
11. **Calibration independently verified** by a second implementation: exhaustive 2^9 search finds
    a unique integer key for the IR rotations (det = 1 to 2e-15), the distortion key is proved by
    an independent unkeyed record, and the rig triangle (158.557/125.318/38.628 mm) reproduces.
12. **FPGA identified** as Pango Logos **PGL25G** (Slave Parallel bitstream; header, 10 padding
    words, sync word `0x01332D94`, IDCODE `0x00511899`, size 982 KB). Documented in
    `docs/firmware/format.md` §4.1.
13. **Firmware evolution** V1.0→V2.10 documented from the layout-independent string diff:
    in-firmware IAP with per-package checksum verification, full USB request handling, pipe/DMA
    recovery, A/B slots.
14. **Verification entry point**: `scripts/verify_all.sh` (firmware, EN/CH arithmetic,
    calibration, protocol, log replay, driver when built) — currently 44 Python tests pass.
15. **Reconstruction survey** (delegated, partial): dense 4-path SGM (census 5/7/9, ZNCC,
    subpixel, LR consistency) + sparse speckle matching, CUDA 10.2/PTX 6.5 fatbins,
    frame-to-model ICP + particle filter, voxel-hash and cylindrical TSDF, CUDA marching cubes,
    texture SFM (Ceres 1.12, SIFTGPU) and ORB/BEBLID/DBoW2 SLAM.
16. **Acquisition backend** (delegated, in progress): C++23 library with transport abstraction,
    descriptor patch codec, frame pool, bounded queues, reassembler, recorder, simulator, C ABI,
    libusb backend behind a CMake option.

## Session 1 — final integration

17. **FX3 functional map integrated** (`docs/firmware/fx3_analysis.md`, 739 lines): memory map
    (ITCM/code/.rodata/.bss), ThreadX thread table, USB descriptors and DMA channel table
    (EP1 0x200, EP2 0x1400, EP3 0xA020 — the stream channel size matches the packet framing),
    45-opcode EP1 dispatcher, device-side update writer with read-back verification.
18. **Proven correction applied**: the app-flag/boot-parameter sector is at flash **0x3F000**, not
    0x3F0, because the SPI helper scales its argument by 256. Updated `format.md` §7 and
    `FINDINGS.md` F11.
19. **Device-side protocol confirmed**: command word is a `u16 BE` at bytes 2..3 and the argument
    is a `u32 BE` at 4..7; the update payload length is `arg − 2` with the checksum byte at
    `9 + len`. No vendor `bRequest 0xA0` handler exists (the real protocol is endpoint-based).
20. **FPGA runtime interfaces**: bitstream read over SPI (0x180 × 4 KiB from the FPGA slot, i.e.
    the 1.5 MB window `0x80000..0x200000`) and a register interface over I2C slave `0x20`.
21. **Independent Pango parse**: 251,406 stream words consumed with zero residue after the
    13-word header; 187 Type‑1 + 2 Type‑2 packets, 251,192 data words.
22. Repository verification: `scripts/verify_all.sh`; 44 Python tests pass
    (`test_firmware` 10, `test_protocol` 12, `test_ccf` 18, `test_log_replay` 4).

### Still in flight at the end of session 1

* `driver/` — library, simulator, tests and benchmarks (headers + most sources present; pipeline,
  libusb transport, FFI, tests/tools/README pending). The app-flag/flash corrections above are
  already reflected in the design.
* `docs/firmware/fpga_bitstream.md` — dedicated Pango analysis (the identification itself is
  recorded in `format.md` §4.1 with independently reproduced parsing).
* `docs/reconstruction/modern_pipeline_design.md` and `validation_plan.md` — survey is complete.