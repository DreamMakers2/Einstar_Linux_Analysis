# Evidence index

Every substantive claim in this repository should be traceable to one of these evidence
classes. Evidence IDs refer to REA records where applicable; file paths are relative to the
repository root.

## E1 — Immutable source artifacts

| Artifact | SHA-256 | Path |
|---|---|---|
| EN factory firmware V2.10/V3.7 | `52346b33c7b0337566d7cfe8d453533c46f18898e4e7f82c1c00c885a8c68c3d` | `sources/firmware/…EN_IAP.img` |
| CH factory firmware V2.10/V3.7 | `0c5a41b5bfdc7f50fb6b55794c36fc4b6200ab28709d25b3e4b82d93fce6e7c5` | `sources/firmware/…CH_IAP.img` |
| V1.0/V2.6 recovery (raw) | `067ce53da1cd00c2bab9b61ba3bc1111b631dbb5faac189c1c4a9e8a3b0c9e45` | `sources/firmware/official_downloads/` |
| Cypress FX3 boot app | `2d3f346c190147727a6ed00753ad4d047c975b9603be9ac5b46d450ebb33713e` | same |
| Private calibration dump | `<redacted>` | `<device-dump.bin>` (not copied into the repo) |
| All captured modules | see manifest | `sources/SHA256SUMS.txt` |
| Official downloads (20 rows) | see manifest | `sources/web/DOWNLOAD_MANIFEST.tsv` |

## E2 — Decompiled / disassembled corpora

| Corpus | Target | Path |
|---|---|---|
| FX3 firmware, 518 functions | `analysis/firmware/EinScan10_EN.elf` | `analysis/ghidra/FX3_FW/clean/` |
| Einstar device plugin, 469 functions | `einscan10.2.0.0.dll` | `analysis/ghidra/Einscan10Device/clean/` |
| Device object, 2369 functions | `Sn3DDeviceEinStar.dll` | `analysis/ghidra/Sn3DDeviceEinStar/clean/` |
| Firmware updater UI, 356 functions | `FirmwareupdateUI.exe` | `analysis/ghidra/FirmwareupdateUI/clean/` |
| PE inventory, 741 modules | EXStar tree | `analysis/gnu/pe_inventory.json`, `docs/windows/module_map.md` |

## E3 — Key function-level evidence

| Claim | Evidence |
|---|---|
| 4097-byte record checksums | `einscan10.2.0.0.dll` `FUN_18000bc80` (sums 4096 bytes, compares the next); 320/320 verified on three images |
| Firmware-update opcode `0x06` | `FUN_18000bc80`: header `[id][0][0][6]`, announcement with `u32 BE total`, then `u32 BE chunk+1` + data |
| Bulk send + retry | `FUN_180002f50`, `CommunicateLayer::sendDataAndWaitReply` |
| Command send + retry (5×, reset pipe 3, 20 ms) | `FUN_180007850` |
| Command header layout | `FUN_180009130` (system `0x01`, 100 B), `FUN_180008f90` (system `0x05`, 64 B), `FUN_180008d00` (system `0x07`, 50 B), `FUN_180009090` (camera `0x01`, 16 B) |
| Reply length `u32 BE` at 5..8 | `FUN_180008f90` / `FUN_180009130` `CONCAT31([5],[6],[7],[8])` |
| Image packet framing | `FUN_18000c8f0` (`ImageStreamThread::run`), `FUN_18000c700` (`image_size=0x140000`, `packet=0xA400`) |
| Transport parameters 0xA400/0x20/100/100000 | `FUN_1800034c0`, `FUN_180007cf0` |
| USB descriptors / `bInterval=0` | de-interleaved image `0xDF18`, `0xDF55`, `0xDF7E`, `0xDF85` |
| FPGA boot thread | `FUN_400057b0` creates `"20:FPGA Boot Thread"` → `FUN_400050cc` |
| FPFA load address slots | `FUN_4000c290` (`0x40000`/`0x1C0000` → `0x80000`/`0x200000`), `FUN_4000b320` (A/B toggle) |
| App flag at flash `0x3F0` | `FUN_4000c290` reads `FUN_40007f64(0x3f0, 0x100, …)` |
| SC130GS sensor | firmware strings `sc130gs_write_table…`, `WriteI2C_seneor_register_for_sc130`, `GPIO_LVDS_CAM1/2/4` |
| Real device behaviour | `analysis/logs/session_2026-02-09.log` (lines cited in `docs/protocol/einstar_usb_protocol.md`) |

## E4 — External documentation

| Document | Use |
|---|---|
| Infineon AN76405 *EZ-USB FX3 Boot Options* (Table 14, USB download example) | FX3 image format and checksum definition |
| `syncservice/doc/3DDigitalSyncInterface-E10.pdf` (vendor, in the install) | E10 device API, properties, `ReadData`/`WriteData`, error codes |
| SHINING 3D support KB 60000782576 / 60001643525 | official download provenance |
| `libeinstar` source + docs | independent protocol implementation used for cross-validation |

## E5 — Negative evidence

| Claim | Basis |
|---|---|
| No FX3 firmware newer than V2.10 | every official installer 2023–2024 contains the same two V2.10 images (hash-equal); KB pages list no firmware package |
| No EXStar newer than 1.2.2.0 for this model | official docs tree 404 for 1.3.x/1.4.x; family routing article still points to 1.2.2.0 |
| No Linux SDK | `github.com/Shining3D/EinstarSDK1.4` private/404; no Linux artefacts in official downloads |
| FX3 bootloader executable not shipped in 1.2.2.0 | only referenced (`/Fx3Bootloader.exe`); payloads present in 1.0.6.0 only |
