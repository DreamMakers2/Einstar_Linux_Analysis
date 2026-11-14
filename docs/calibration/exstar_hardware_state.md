# EXStar 2026-11-14 calibration — scanner-side hardware state and storage provenance

Investigation date: 2026-11-14 (01:34–02:00 Europe/Amsterdam).
Author: `usb-agent` (USB/hardware specialist), DeepSeek Harness session.
Repository state: `04aa6a4` — clean except for this investigation's two outputs.
Scope: **read-only**. No firmware flash, no calibration write, no EEPROM/persistent
command, no USB 3.0 attempt, no driver change. The Windows partition was used only
through its existing `ro` mount.

Confidence labels used throughout: **A** = observed on the wire / in a file / in
executed code, independently cross-checked; **B** = one interpretation step from
A-level facts; **C** = inference with no direct confirmation.

---

## 0. Answer first

| question | answer | confidence |
|---|---|---|
| Where did EXStar put the 2026-11-14 calibration? | **Both**: the host files under `res/Einscan-E10/200x150/` *and* the scanner's non-volatile calibration slots | **A** |
| Did EXStar write to the scanner? | **Yes.** `writeCustomInfo` at offset **923** (0x39B, 3873 B = the CCF block) at 00:13:12 and at offset **4796** (0x12BC, 153 B = white balance) at 00:14:54; the device answered `WriteData result.0` both times (zero = the vendor's success code) | **A** |
| Is the manufacturer calibration still intact? | **The production block is.** Bytes `0x000..0x39A` (3 × 22-double camera records, ParaCCM/ParaDark, active data) and `0x1355..0x19A7` (device ids, board PN, production date) are **byte-identical** to the pre-run dump. Only the two vendor-writable calibration slots changed | **A** |
| Is the device-side CCF now the new calibration? | **Yes.** The blob's CCF records are byte-identical to the new `LeftCCF.txt`/`RightCCF.txt`/`TexCCF.txt`; the embedded calibration string changed `2026-02-09 21:34` → `2026-11-14 00:13` | **A** |
| Firmware/identity match the records? | **Yes.** `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`, serial `<redacted-device-serial>`, product `EinScan10_01`, 3 sensors, 1280×1024 — same before and after, no update performed or logged | **A** / **A-** for "no update" |
| Which calibration is "factory"? | **Two device-side layers.** The unkeyed 22-double production records (`0x00C..0x2AB`, dated <redacted-production-date>) are never written by EXStar; the CCF slot (`0x39B`) is the app-level calibration EXStar regenerates. Our own `factory-e10.json` is built from the **CCF**, not the production records | A (bytes) / B (naming) |
| Is the scanner safe/idle now? | **Yes.** Trigger counts `0 / 0`, illuminator readback laser `0 %`, strobe `0 / 0`, no streaming, no process holding the device, no kernel driver bound, no USB reset/error in the kernel log after the readback | **A** |

The single most important consequence: **the pre-run CCF is no longer what the scanner
will hand to any host.** It survives only in the host-side backup, in this repo, and in
the private 6568-byte dumps. See §3 for the exact identity of each copy.

---

## 1. Device identity and firmware: now vs records

### 1.1 Enumeration now (observed)

| property | value | source |
|---|---|---|
| bus / port / devnum | 1 / 5 / 5 (`/dev/bus/usb/001/005`) | `lsusb`, sysfs |
| VID:PID | `3267:0003` | `lsusb -v` |
| manufacturer / product | `Shining3d` / `SHINING-CAM` | `lsusb -v`, kernel log |
| serial string descriptor | none (`iSerial = 0`); the logical serial comes from system opcode `0x04` | `lsusb`, probe |
| speed | High Speed, 480 Mbit/s | sysfs `speed=480`, `lsusb` |
| bcdUSB / bcdDevice | `0x0200` / `0x0000` | `lsusb -v` |
| configuration | 1 interface, class `ef/02/01` (IAD), 5 endpoints (`0x01`, `0x81`, `0x02`, `0x82`, `0x83`) | `lsusb -v` |
| endpoint quirk | `0x01`/`0x81` declare `bInterval = 0`; Linux repairs to 7 | kernel log 01:24:37 |
| kernel driver bound | none | `/sys/bus/usb/devices/1-5:1.0/driver` absent |
| enumerated at | 2026-11-14 01:24:37 (boot) | `journalctl -k` |

No re-enumeration, reset, or disconnect happened during this investigation
(`journalctl -k` since 01:30 contains no `usb 1-5` line, and the device number
stayed 5).

### 1.2 Device readback now (observed)

Two independent userspace implementations were used, both read-only:

* `einstar-probe --info --dump-flash` (`tests/libeinstar-scratch`, rebuilt at repo HEAD
  into `/tmp/einstar-probe-build`): handshake, `Get*` queries, then camera opcode
  `0x57` page reads for pages 0 and 1.
* `build/driver/einstar-capture-frames --backend libusb --probe-only` (the project's
  own documented probe, the same one that produced the 2026-11-11 factory snapshot).

| item | value |
|---|---|
| serial | `<redacted-device-serial>` |
| product | `EinScan10_01` |
| firmware | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN` (parsed: FX3 2.10, FPGA 3.7, locale EN) |
| sensor count | 3 |
| per-camera mode | 1280 × 1024, exposure 5000 µs, gain 500 (all three) |
| trigger | period 66666 µs; counts mono **0**, rgb **0** |
| illuminator | laser **0 %**, strobe route0 **0**, route1 **0** |
| temperature | 0 raw |
| calibration blob | **6568 B**, sha256 `256dd440554387da8fcd5b48baa0b2e8c314a1eb90a89b85b2987bb0f9121148`, fnv1a64 `0x17CB529DD59CCB6A` |

The driver probe's fnv1a64 of the blob (`0x17CB529DD59CCB6A`) equals the value computed
from the probe's dump — two independent readouts of the same bytes. The private
pre-run dump has fnv1a64 `0x1879897CA84286A1`, i.e. the blob changed (§3).

### 1.3 Now vs records

| item | records | read now | verdict |
|---|---|---|---|
| serial | `<redacted-device-serial>` (`~/einstar-private/second-read.log`, 2026-11-07; EXStar `deviceStatus` 2026-11-14 00:09:52) | same | match (A) |
| product | `EinScan10_01` (`docs/protocol/einstar_usb_protocol.md` §4.1) | same | match (A) |
| firmware | `...FX3_V2.10_FPGA_V3.7_EN` (docs, factory snapshot README, EXStar log) | same | match (A) |
| device ids in blob | `<redacted-serial>`, board `<redacted-part-number>`, date `<redacted-production-date>` (ccf_format §7; EXStar `activeData` log) | same bytes | match (A) |
| calibration blob | `557a7992…e724dd4` (2026-11-07/11 reads, 4 consecutive reads identical) | `256dd440554387da8fcd5b48baa0b2e8c314a1eb90a89b85b2987bb0f9121148` | **changed** (expected, §3) |

### 1.4 Firmware was not touched

* No `UpdateFirmware` call appears in any 2026-11-14 log. The updater process only
  *compares* the local image:
  `EXStar_20261114_000944_607.log:549 FirmwareUpgradeHelperEA.cpp:114 checkFirmwreUpgrade
  strLocalFirmware = C:\Shining3d\EXStar\fabu_UPDATE\Configure\EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img`.
* `deviceStatus` reports `isForceFWUpdate=false`, and the banner read before
  (2026-11-07) and after (2026-11-14) is identical.
* Firmware lives in application slots `0x40000`/`0x1C0000` with FPGA images at
  `0x80000`/`0x200000`; calibration lives in a separate data region at `0x400000`
  (`docs/firmware/format.md` §7, `docs/firmware/fx3_analysis.md` §4.2).

**Conclusion:** identity and firmware unchanged; **A** for identity, **A−** for
"no firmware update" (absence of a logged update plus an unchanged banner, not a
flash-image hash read back from the device).

---

## 2. Host-side or device-side? Evidence and confidence

### 2.1 What EXStar does, from its own logs (observed)

`scanservice_20261114_000948_049.log` (plaintext, 919,209 B):

```
00:09:52.647  Sn3DDeviceBase.cpp:291              ReadData result.0
00:09:52.654  [2022]cmd=ReadDataRet
...
00:13:11.941  Sn3DEACalibrateDataController.cpp:372  [DP 2023] Send CCF
00:13:11.941  Sn3DEACalibCompute.cpp:179             writeCalibrateResultToDev,bRet = 1
00:13:11.941  [2022]cmd=writeCustomInfo
00:13:11.941  Sn3DDeviceBaseController.cpp:314       write data , offSet.923
00:13:12.423  Sn3DDeviceBase.cpp:291                 WriteData result.0
00:13:12.424  Sn3DEACalibCompute.cpp:139             WriteDataRet calibration.
...
00:14:54.314  Sn3DEACalibCompute.cpp:212             writeWhiteBalanceResultToDev,bRet = 1
00:14:54.314  [2022]cmd=writeCustomInfo
00:14:54.314  Sn3DDeviceBaseController.cpp:314       write data , offSet.4796
00:14:54.996  Sn3DDeviceBase.cpp:291                 WriteData result.0
00:14:54.997  Sn3DEACalibCompute.cpp:139             WriteDataRet calibration.
```

The same log shows the connect-time read of the 6568-byte blob before any calibration
work (`ReadData result.0` at 00:09:52), and `EXStar_20261114_000944_607.log:477/505`
show the reported calibration time moving from the host-file value
`2026-02-09T21:34:58.486` (offline, before connect) to the **device-block** value
`2026-02-09T21:34:00.000` (after the device read — minute precision, exactly the
string stored at CCF block `+0xA14`), then to `2026-11-14T00:13:11.940` after the write.
That is direct evidence that the device copy is read and used at connect and that
the write is the authoritative record.

### 2.2 What the device has, from the existing reverse engineering (observed)

`analysis/calibration/re/storage_report.md` §4 (Ghidra 12.1.4 dumps of the **exact
DLLs installed on this machine** — see §3.4):

* `Sn3DEACalibrateController::FUN_180003940` packs the three host CCF records into a
  `0xF21` (3873 B) block: `u32 type=4`, Left at `+0x10C` (len `+0x108`), Right at
  `+0x614`, Tex at `+0xB1C`, ASCII `FQFQ` at `+0xF1C`, `yyyy-MM-dd hh:mm` at `+0xA14`.
* `FUN_180003540` publishes `writeCustomInfo` with `offset = 0x39B`, `value = 0xF21 B`,
  `additionInfo = "calibData"`; the device answers `WriteDataRet`.
* `Sn3DEACalibCompute.cpp:212` (`writeWhiteBalanceResultToDev`) writes at `0x12BC`.

`docs/firmware/fx3_analysis.md` §4.2 proves the device side: the EP2 dispatch table at
`0x40030324` holds

| row | cmd | required arg | handler | role |
|---|---|---|---|---|
| 0 | `0x0006` | 5 | `FUN_4000B22C` | start firmware update |
| 1 | `0x1057` | 2 | `FUN_4000BB00` | **read one 4 KiB block** of the data region |
| 2 | `0x1058` | any | `FUN_4000BC28` | **erase + program one 4 KiB block** |

with flash address `(0x400 + index) * 0x1000` = `0x400000 + index·0x1000`,
`index < 0x100` — a 1 MiB read/write region at `0x400000..0x4FFFFF`, independent of
the firmware slots. `0x1057`/`0x1058` are the group-`0x10` opcodes `0x57`/`0x58`,
i.e. the `ReadData`/`WriteData` the vendor API exposes (`docs/protocol/einstar_usb_protocol.md` §6).

### 2.3 Evidence chain

| # | evidence | what it proves | confidence |
|---|---|---|---|
| E1 | RE of `Sn3DEACalibrateController`/`Sn3DCalibDevFile` (pre-existing, `analysis/calibration/re/`) | EXStar packs the host CCF set and publishes a **device write** at `0x39B`, plus WB at `0x12BC` | A |
| E2 | 2026-02-09 vendor session log (`analysis/logs/session_2026-02-09.log` lines 32, 11233, 11812) | the vendor really issues `ReadData(0,6568)` at connect and `WriteData(923, 3873 B)` + `WriteData(4796, 153 B)` after calibrating. Decoding those base64 payloads gives bytes **byte-identical** to regions `0x39B..0x12BB` and `0x12BC..0x1354` of our pre-run dump (sha256 `ead93b35…`, `167ffaf1…`) | A |
| E3 | 2026-11-14 `scanservice` log (quoted above) | the same two writes happened in **this** run, both acknowledged with `result.0` | A |
| E4 | this investigation's 6568-byte readback | the blob differs from the pre-run dump **only** in those two regions, and the new bytes equal the new host files exactly | A |
| E5 | FX3 firmware dispatch table (`docs/firmware/fx3_analysis.md` §4.2) | the device implements block read **and** block erase/program over EP2; device-side calibration storage is not merely plausible, it is wired | A |

### 2.4 Verdict

**Calibration is stored on both sides; the scanner's copy was overwritten during the
2026-11-14 run. Confidence A.**

Structure of the 6568-byte record, by role:

| range | content | changed 2026-11-14? |
|---|---|---|
| `0x000..0x00B` | `u32 1`, width 1280, height 1024 | no |
| `0x00C..0x2F3` | 3 × 22-double **production** camera records (fx, fy, cx, cy, dist, R, T) | no (byte-identical) |
| `0x2F4..0x39A` | `FAFA`/`FBFB` tagged CCM + dark tables (ParaCCM/ParaDark) | no |
| **`0x39B..0x12BB`** | **CCF block** (`type=4`, Left/Right/Tex, `FQFQ`, date string) | **yes — rewritten** |
| **`0x12BC..0x1354`** | **white balance**: `u32 5`, wbdata 9×f64, wbdata_noled 9×f64, `FWFW` | **yes — rewritten** |
| `0x1355..0x19A7` | `AABB` marker, erased-fill, device id, board PN, `<redacted-production-date>` | no |

**Interpretation (B, expanded in §2.5):** the 22-double production block (dated `<redacted-production-date>` in the same
record) is the factory calibration; the CCF/WB slots are the application-level
calibration that EXStar regenerates and is allowed to overwrite. The vendor's own
program treats `0x39B` and `0x12BC` as writable slots, which is why a re-calibration
does not touch the production records. We have no evidence of any other persistent
calibration state (no flag/time field outside the CCF block changed).

### 2.5 The two device-side calibration layers

The blob contains two independent calibrations of the same rig, in regions with
different ownership:

| layer | offset | content | rewritten by EXStar? |
|---|---|---|---|
| **production records** | `0x00C..0x2AB` (3 × 176 B, stride `0xF8`, padding to `0x2F3`) | unkeyed 22 × f64 per camera: `fx fy cx cy dist6 R9 T3`; `T` is expressed about ~550 mm from the rig origin | **no** — byte-identical before/after |
| **app calibration (CCF)** | `0x39B..0x12BB` | `u32 type=4`, 520/520/264-byte records, `FQFQ` magic, `yyyy-MM-dd hh:mm` | **yes**, on every calibration (2026-02-09 and 2026-11-14 observed) |
| **white balance** | `0x12BC..0x1354` | `u32 5`, 9 × f64 wbdata, 9 × f64 wbdata_noled, `FWFW` | **yes**, on every white-balance step |

Geometry of the two layers, computed from the pre-run dump (same path as
`tools/ccf/decode_ccf.py --bin`, `R_rel = R₁R₀ᵀ`, `t_rel = T₁ − R_rel·T₀`):

| quantity | production 22-double records | CCF set A (2026-02-09) | difference |
|---|---:|---:|---:|
| left `fx` | 1158.761 | 1167.115 stored / 1159.115 best-key (see §3.5) | −8.35 / −0.35 px |
| IR baseline (cams 0–1) | **160.092 mm** | **158.557 mm** | 1.53 mm (0.96 %) |
| IR relative rotation | 22.086° | 22.138° | 0.052° |
| Tex–left distance | 122.877 mm | 125.318 mm | 2.44 mm (1.9 %) |

Both are valid rotations (`det = 1.000000000000`, `max|RRᵀ−I| ≤ 2.0e−15`), so neither
is corrupt data. What the two layers share and what they do not:

| property | production records vs CCF A |
|---|---|
| five distortion terms `kc` | **bit-identical** (max absolute Δ ≤ 5.3e−16, all three cameras) |
| `fx`, `fy`, `cx` | agree within 0.9 px under `key_mode=best` (≤ 0.35 / 0.82 / 0.73 px for L / R / Tex) |
| `cy` | agree except the right camera: 4.19 px |
| IR baseline | **160.092 vs 158.557 mm → 1.53 mm (0.96 %)** |
| Tex–left | 122.877 vs 125.318 mm → 2.44 mm (1.9 %) |

The bit-exact distortion means the CCF's distortion was **inherited** from the
production calibration, not re-solved — the same behaviour EXStar shows across its own
two CCF calibrations (distortion frozen, pose and a few intrinsics moved by ≤ 0.0558 mm
and ≈ 1 px, per `docs/calibration/exstar_vs_factory_vs_linux.md`). The 1.53 mm baseline
difference is ~27× that CCF-to-CCF movement, so the production record and the
2026-02-09 CCF are **different solves of the same rig that share a distortion model**,
not two copies of one another. The 2.44 mm Tex–left difference matches the "agrees
within 2.4 mm" statement already in `docs/calibration/ccf_format.md` §7.

**Which is the factory calibration?** — *production records*, **confidence B**:

* for: they sit in the region EXStar never writes, next to the ParaCCM/ParaDark tables,
  the board PN (`<redacted-part-number>`) and the production date (`<redacted-production-date>`); EXStar's
  `deviceStatus` `activeData` (`serialNum`, `activeTime <redacted-production-date>`,
  `calibNum <redacted-part-number>`, `dataVersion 2`) is read from exactly this block; and
  EXStar's calibration code writes only `0x39B` and `0x12BC`.
* against: no vendor document calls these records "factory", and a production tool could
  in principle rewrite them.

The CCF slot is the **app-level (EXStar) calibration** — **confidence A**, because we see
EXStar write it on two independent dates. So the device does not store one calibration:
it stores the immutable production record **and** a rewritable application calibration.

### 2.6 Hazard note for any future write-back from Linux

`0x58` erases and programs **whole 4 KiB blocks**. The CCF block starts at `0x39B`,
i.e. 923 bytes into block 0, and ends in block 1; the WB block lies in block 1.
A correct write is therefore a **read-modify-write of blocks 0 and 1 that preserves
`0x000..0x39A` and `0x1355..0x19A7`**. A naive "write 6568 bytes with zero fill"
would destroy the factory intrinsics/extrinsics and the Para tables. The vendor
behaves correctly here: our dump shows every byte outside the two slots intact.
This is recorded as a hazard, not as a recommendation to write anything.

---

## 3. Before/after provenance and hashes

### 3.1 The three calibration identities

| id | what it is | where |
|---|---|---|
| **A** | this unit's calibration in force **before** 2026-11-14, written 2026-02-09 21:34–21:35 (same values as the device blob before the run) | `res/Einscan-E10/backup/bak/` (host copy made at run start); `sources/calibration/res_Einscan-E10_200x150/` (repo); device blob `0x39B..0x1354` **before** the run |
| **B** | installer-shipped CCF set (2024-06-28), **not this unit's** — colour `fx ≈ 1571` vs `≈ 1141` | `EXStar/ccf/200x150/`; `sources/calibration/ccf_200x150/` (byte-identical) |
| **C** | the **new** EXStar calibration of 2026-11-14 00:13–00:14 | `res/Einscan-E10/200x150/` (live files); device blob `0x39B..0x1354` **now**; private copy `~/einstar-private/exstar-20261114/device-readback/calibration.bin` |

`res/Einscan-E10/backup/bak/` has directory mtime 2026-11-14 00:09 and preserves the
files' 2026-02-09 timestamps, i.e. it is a timestamp-preserving copy made at the start
of the run, before the 00:13 write. EXStar logs no backup step, so **the attribution to
EXStar is inferred (B)**; the content equality with A is observed (A).

### 3.2 The device blob, before and after

| | before (2026-11-07 read) | after (2026-11-14 01:36 read) |
|---|---|---|
| path | `~/einstar-private/calibration.bin` (also `calibration-second.bin`, and `factory-snapshot-20261111/calibration.bin`) | `~/einstar-private/exstar-20261114/device-readback/calibration.bin` |
| size | 6568 | 6568 |
| sha256 | `<redacted>` | `256dd440554387da8fcd5b48baa0b2e8c314a1eb90a89b85b2987bb0f9121148` |
| fnv1a64 | `0x1879897CA84286A1` | `0x17CB529DD59CCB6A` |
| CCF slot sha256 | `ead93b352015a6e4…` | `699051f14cb1e83a…` |
| WB slot sha256 | `167ffaf102741964…` | `0649fce68e905f88…` |
| identity region `0x000..0x39A` | `bc47878b59f4f886…` | **identical** |
| tail `0x1355..0x19A7` | `24263b4ebb12887c…` | **identical** |
| differing bytes | — | **366** in 53 runs, all inside the two slots |
| CCF date string | `2026-02-09 21:34` | `2026-11-14 00:13` |

Region hashes use half-open ranges (`0x39B..0x12BC`, `0x12BC..0x1355`) exactly as
written by the vendor (`WriteData(923, 3873)`, `WriteData(4796, 153)`).

### 3.3 Host-side file hashes

**C — EXStar 2026-11-14 calibration (Windows `res/Einscan-E10/200x150`, live files)**

| bytes | mtime | sha256 | files |
|---:|---|---|---|
| 520 | 2026-11-14T00:13:11 | `7061f68a14809f48b592a2e9e37c9cf194d06c253786f0832e25fa3e8be73f13` | `LeftCCF.txt` |
| 60 | 2026-11-14T00:09:52 | `cc2133df272e51f96d87d54d0c1b5352641e27f1bd62f245b52ae44da68a9fc1` | `ParaCCM_0.txt`, `ParaCCM_noled_0.txt` |
| 60 | 2024-06-28T08:43:32 | `ead3721a8eba1a2171688635fe07c7af87dc15e00236ebb1310d050a3c24614e` | `ParaCCM_1.txt`, `ParaCCM_2.txt`, `ParaCCM_noled_1.txt`, `ParaCCM_noled_2.txt` |
| 12 | 2026-11-14T00:09:52 | `e675f08ebee1a4e725adda40db6c0da50fcecbfe7596df00de161c6a524d8854` | `ParaDark_0.txt`, `ParaDark_noled_0.txt` |
| 12 | 2024-06-28T08:43:32 | `7e54178190c0c3db16c6336d6db74e4b857e4e42139b09176afcbaa48797ce69` | `ParaDark_1.txt`, `ParaDark_2.txt`, `ParaDark_noled_1.txt`, `ParaDark_noled_2.txt` |
| 520 | 2026-11-14T00:13:11 | `a31a0119ed19cf575efbd740b5738bfb8a59427cd319df2b1dec46e346ef33bd` | `RightCCF.txt` |
| 264 | 2026-11-14T00:13:11 | `6e8a74169f79ad8481ec4f51116fe3e0a3cecae0f0a42b39107992454c4ffec9` | `TexCCF.txt` |
| 48 | 2026-11-14T00:13:11 | `abcad604662d65bea56674df4ec352cacea959d947b99e3dc497e8a449b7f4ba` | `activeBox.dat` |
| 72 | 2026-11-14T00:14:54 | `b083003d6ef2351ecc390fb5cd2ea40da30466149f9099fde12563c853d6ed85` | `wbdata.txt` |
| 72 | 2026-11-14T00:14:54 | `add1c3748783384830ed206edd1524e051dd1a2157590497f244235c823c34fe` | `wbdata_noled.txt` |

**A — pre-run host copy (Windows `res/Einscan-E10/backup/bak`)**

| bytes | mtime | sha256 | files |
|---:|---|---|---|
| 520 | 2026-02-09T21:34:58 | `6879a68d6926caebe19bdaee9a09dd05c50ee03f23cf6c5158801dcf8b5621d7` | `LeftCCF.txt` |
| 60 | 2026-02-09T21:31:25 | `cc2133df272e51f96d87d54d0c1b5352641e27f1bd62f245b52ae44da68a9fc1` | `ParaCCM_0.txt`, `ParaCCM_noled_0.txt` |
| 12 | 2026-02-09T21:31:25 | `e675f08ebee1a4e725adda40db6c0da50fcecbfe7596df00de161c6a524d8854` | `ParaDark_0.txt`, `ParaDark_noled_0.txt` |
| 520 | 2026-02-09T21:34:58 | `45b80e2aa8bcece7fe400d79f42c852f67824de0b478f0ea1a0c3aa0b6cf1dc9` | `RightCCF.txt` |
| 264 | 2026-02-09T21:34:58 | `7b0477a524b3af59d6907445f0e289da6f73891ac1714451de502bb240f84e64` | `TexCCF.txt` |
| 72 | 2026-02-09T21:35:20 | `65072f996ac2473219da4a030fd54720b62fa4f5e4eb16c7a30ec61df402470d` | `wbdata.txt` |
| 72 | 2026-02-09T21:35:20 | `22b8ef3b2753450de5dc09e44c643a40e87a2a083a634bd030398069cb851dbf` | `wbdata_noled.txt` |

**A — repo `sources/calibration/res_Einscan-E10_200x150` (committed 2026-11-07, baseline 36da380)**

| bytes | mtime | sha256 | files |
|---:|---|---|---|
| 520 | 2026-11-07T03:04:28 | `6879a68d6926caebe19bdaee9a09dd05c50ee03f23cf6c5158801dcf8b5621d7` | `LeftCCF.txt` |
| 60 | 2026-11-07T03:04:28 | `cc2133df272e51f96d87d54d0c1b5352641e27f1bd62f245b52ae44da68a9fc1` | `ParaCCM_0.txt`, `ParaCCM_noled_0.txt` |
| 60 | 2026-11-07T03:04:28 | `ead3721a8eba1a2171688635fe07c7af87dc15e00236ebb1310d050a3c24614e` | `ParaCCM_1.txt`, `ParaCCM_2.txt`, `ParaCCM_noled_1.txt`, `ParaCCM_noled_2.txt` |
| 12 | 2026-11-07T03:04:28 | `e675f08ebee1a4e725adda40db6c0da50fcecbfe7596df00de161c6a524d8854` | `ParaDark_0.txt`, `ParaDark_noled_0.txt` |
| 12 | 2026-11-07T03:04:28 | `7e54178190c0c3db16c6336d6db74e4b857e4e42139b09176afcbaa48797ce69` | `ParaDark_1.txt`, `ParaDark_2.txt`, `ParaDark_noled_1.txt`, `ParaDark_noled_2.txt` |
| 520 | 2026-11-07T03:04:28 | `45b80e2aa8bcece7fe400d79f42c852f67824de0b478f0ea1a0c3aa0b6cf1dc9` | `RightCCF.txt` |
| 264 | 2026-11-07T03:04:28 | `7b0477a524b3af59d6907445f0e289da6f73891ac1714451de502bb240f84e64` | `TexCCF.txt` |
| 72 | 2026-11-07T03:04:28 | `65072f996ac2473219da4a030fd54720b62fa4f5e4eb16c7a30ec61df402470d` | `wbdata.txt` |
| 72 | 2026-11-07T03:04:28 | `22b8ef3b2753450de5dc09e44c643a40e87a2a083a634bd030398069cb851dbf` | `wbdata_noled.txt` |

**B — installer-shipped set: repo `sources/calibration/ccf_200x150` (identical to Windows `EXStar/ccf/200x150`)**

| bytes | mtime | sha256 | files |
|---:|---|---|---|
| 520 | 2026-11-07T03:04:28 | `a6182110954130331bd3e311a6fdc339bf41b4fcbc81d562b6202069b8b54513` | `LeftCCF.txt` |
| 520 | 2026-11-07T03:04:28 | `c59d46981980a79212348cdb3838f0ad37dfa03a5ba705af59699c0e23ac4da3` | `RightCCF.txt` |
| 264 | 2026-11-07T03:04:28 | `1d7debeae6ce4fd4000a6eb987bc28950e522ff13c80794702fdd248ffbf5980` | `TexCCF.txt` |

Installer-time Para copies (`EXStar/backup/Einscan-E10/200x150`, 2024-06-28) are
byte-identical across cameras: all six `ParaCCM*` files share
`ead3721a8eba1a2171688635fe07c7af87dc15e00236ebb1310d050a3c24614e`, all six
`ParaDark*` files share `7e54178190c0c3db16c6336d6db74e4b857e4e42139b09176afcbaa48797ce69`.

`activeBox.dat` (48 B, six little-endian doubles = the working box
`x,y ∈ [−1500,1500] mm`, `z ∈ [200,1500] mm`, per `storage_report.md` §2.2) is
rewritten with the CCF; it has no pre-run counterpart in `backup/bak`, so no A/B
comparison is possible.

### 3.4 Cross-checks that make the provenance airtight

1. `win_current/{Left,Right,Tex}CCF.txt` are **byte-identical** to the CCF records
   inside the current device blob; `win_current/{wbdata,wbdata_noled}.txt` are
   byte-identical to the blob's WB region.
2. `win_backup_bak/*` are **byte-identical** to `repo res_Einscan-E10_200x150/*` and to
   the pre-run device blob's CCF/WB records.
3. The 2026-02-09 vendor log's `WriteData` payloads decode to the **same bytes** as the
   pre-run blob's two slots (sha256 `ead93b35…`, `167ffaf1…`), so the pre-run dump is
   provably the state the vendor wrote on 2026-02-09.
4. The EXStar binaries that performed the run are byte-identical to the copies the
   reverse engineering was done on: `Sn3DEACalibrateController.dll`
   `1e3d33af7faa56e2…`, `Sn3DCalibDevFile.dll` `c9258681f87260cc…`,
   `Sn3DCalibrateData.dll` `d0773e02cc35dc98…`, `syncservice/device/einscan10.2.0.0.dll`
   `d02e794b16e4f59a…` — installed 2024-06-28, version 1.2.2.0, matching
   `sources/web/extracted/EXStar_v1.2.2.0/`.
5. Numeric comparison of A vs B vs C (intrinsics, distortion, relative pose) is the
   numerics team's deliverable, `docs/calibration/exstar_vs_factory_vs_linux.md`; this
   document only fixes **which bytes are which**.

### 3.5 "Factory" is ambiguous in our own tooling

**Observed (A).** `~/einstar-private/einstar-profiles/factory-e10.json` has
`kind = "factory"`, `world_frame = "ir_left"`, `skew_mode = "bouguet"`, and a
`provenance` block reading `source=ccf`, `key_mode=best`, with the SHA-256 of CCF
set A's three files. **Our "factory" profile is therefore the app-level CCF of
2026-02-09, not the production 22-double records.**

**Observed (A).** In `tools/ccf/decode_ccf.py`, `IR_KEY_BEST` carries `+8, −4, −6, −1`
for `fx, fy, cx, cy` under the convention `stored = true + key`, i.e. the best-key
decode is `fx − 8, fy + 4, cx + 6, cy + 1`. `KEY_CONFIDENCE` labels those four entries
*hypothesis* (only `fx − fy = 12` is forced; distortion and rotation entries are marked
proven against the in-flash 22-double records). Hence, for the same left camera:

| source | left `fx` |
|---|---:|
| CCF A stored value (= `key_mode=proven`) | 1167.115 |
| CCF A with `key_mode=best` (= our `factory-e10.json`) | 1159.115 |
| production 22-double record | 1158.761 |

All three are defensible-looking numbers for "the factory left camera". **Differences
between calibrations are unaffected** (every set is decoded with the same key), but
**absolute intrinsics are not**: the numerics document
(`docs/calibration/exstar_vs_factory_vs_linux.md`) used `key_mode=proven`, while
`factory-e10.json` used `key_mode=best`, so the two quote `fx` values 8 px apart for
the same physical camera.

**Hypothesis (C, for the numerics team to test, not a conclusion):** our Linux
candidate's baseline was reported about +0.92 % versus CCF A
(`docs/calibration/exstar_vs_factory_vs_linux.md`), and the production
records' baseline is +0.96 % versus CCF A. If the candidate's *scale* sits closer to
the production records than to the 2026-02-09 CCF, that would suggest the app-level
CCF — not the rig — is the outlier, or that the two layers are produced by different
procedures. It says nothing yet about absolute accuracy, which needs a reference
object.

---

## 4. Other EXStar state written in the window (read-only survey)

Window searched: 2026-11-13 22:00 – 2026-11-14 00:40, on the `ro` mount of
`<EXStar-install>`. Nothing was copied into the repository.

| path (under `…/Shining3d/EXStar/` unless noted) | bytes | mtime | sha256 (prefix) | note |
|---|---:|---|---|---|
| `log/DeviceHelper_2026_11_14_00_09_46_120.txt` | 760 | 00:09:48 | `64fc50af6e89fc37` | connect/attach/read-base-param/trigger reads; clean disconnect |
| `log/algLog/CAL_20261114_000951_076930_info.log` | 4120 | 00:10:14 | `3044838bcce28f57` | algorithm log, base64/ciphertext — not decoded |
| `log/algLog/CAL_20261114_000951_076930_stts.log` | 68336 | 00:14:54 | `96b60bb674c58539` | algorithm statistics log |
| `log/scanservice_20261114_000948_049.log` | 919209 | 00:23:59 | `9372674e4f2518cb` | **the decisive write evidence** |
| `log/EXStar_20261114_000944_607.log` | 1409600 | 00:23:59 | `fc6ab8cfccd76f6b` | deviceStatus, caliInfoChanged times, firmware check |
| `log/softwareUpgrade_20261114_000951_135.log` | 35034 | 00:23:55 | `b16c8285c354a726` | updater process, no image written |
| `log/Sn3DprocessManager_20261114_000942_975.log` | 39546 | 00:23:59 | `d89d50da68577af1` | process manager |
| `einscaninfo/2026-11-14T00-09-55.info2` | 1435 | 00:23:55 | `710a0e6248a5cf1e` | usage telemetry (ver 1.2.2.0, install path) |
| `einscaninfo/2026-11-13T23-14-58Z.info2` | 1950 | 00:14:58 | `e3aee0e1db393113` | calibration telemetry: binocular **successed**, texture **successed** |
| `res/config.db` | 204800 | 00:10:11 | `4f2b0c2215147701` | **SQLCipher** (no SQLite header; same 16-byte header salt as the 2024 backup copy) — unreadable without the key |
| `res/NavigatorConfig.db` | 8192 | 00:23:49 | `23be125a991a4ea6` | 3Dconnexion navigator, unrelated |
| `res/system.ini` | 147 | 00:10:11 | `49879021afd15b2f` | telemetry/updater flags (`forceUpdateFirm=true` is a *policy* flag; no update ran) |
| `ScanSets/scansetting.ini` | 88 | 00:09:53 | `aef41cd68d2fbdcc` | UI language / first-start |
| `setting.ini` | 205 | 00:09:56 | `fe347f456e27c400` | file-dialog history only |
| `passport/config/account.json` | 510 | 00:10:10 | `9b27884e5c5b96fa` | account/session metadata — **contents deliberately not recorded** |
| `res/einscan_net_svr/informationCollect_20261114_000954_611.log` | 40855 | 00:23:55 | `e5db2483f4d77f06` | information collector |
| `res/einscan_net_svr/20261114-000958.10336` | 3423 | 00:23:58 | `73f466196ffb7012` | queued telemetry payload |

Calibration raw data written in the same window (useful for re-deriving the solve, but
large and not copied):

| path | files | bytes | mtime | note |
|---|---:|---:|---|---|
| `res/Einscan-E10/calibrate_image_read_rapid/` | 75 | 98,384,850 | 00:13:12 | 25 × (Left, Right, Tex) 1280×1024 BMP, 1,311,798 B each |
| `res/Einscan-E10/calibrate_image_wb/` | 6 | 7,870,788 | 00:14:55 | 2 × (Left, Right, Tex) from the WB step |
| `res/texcamera/` | 4 | 10,488,024 | 00:14 | `raw_0/1.bmp` (1,311,798 B) and `rgb_0/1.bmp` (3,932,214 B) |

Absent state (checked, nothing found):

* `ProgramData`: no `Shining*`/`EXStar`/`Einstar` directory; nothing EXStar-related was
  written there in the window.
* `Users/DV3/AppData`: only Qt caches (`App_EA.xml/cache/qmlcache`, `qtshadercache`) and
  unrelated app state at 00:09–00:10; `AppData/Local/EXStar Hub` is untouched since
  2026-08-03; no Roaming EXStar state.
* `Users/DV3/Documents/EXStar` is an empty directory whose mtime moved to 00:09.

**Interpretation (B):** EXStar 1.2.2.0 is effectively portable — its persistent state
lives in the install tree (`res/`, `log/`, `einscaninfo/`, `passport/`), not in
AppData/ProgramData. The encryption of `res/config.db` means the only unexamined
EXStar store is that SQLCipher database; everything calibration-relevant that it might
mirror is already available in plaintext (`200x150/*`, device blob, logs).

---

## 5. USB protocol knowledge relevant to calibration storage/readback

| item | fact | status |
|---|---|---|
| Read `0x57` | camera group `0x10`, bulk `0x02`→`0x82`, `0x1400`-byte buffers, request = 16-byte command header (`opcode 0x57`, length 2, page index u16 BE at 8) + zero fill; reply = status at 4, 4096 B page at 9 | verified in libeinstar, in the FX3 handler `FUN_4000BB00`, and used for this readback |
| Write `0x58` | same channel; firmware `FUN_4000BC28` erases the sector and programs 4096 B from request offset 10 | proven in firmware, **declared but forbidden** in libeinstar; never executed here |
| Data region | `0x400000 + index·0x1000`, `index < 0x100` → 1 MiB, 4 KiB blocks | firmware-proven formula; physical part/size unknown |
| Vendor API | `execute("ReadData",[offset,size]) → base64`, `execute("WriteData",[offset,base64])`; offsets are byte offsets into the flat blob | documented in the E10 manual and observed in the 2026-02-09 log |
| Vendor usage | `ReadData(0,6568)` on every connect; `WriteData(923,3873)` after the CCF solve; `WriteData(4796,153)` after white balance | observed twice (2026-02-09 and 2026-11-14) |
| Other persistent commands | system `0x06` firmware update (bulk streaming), system `0x08` reboot | implemented in vendor, **forbidden** in our driver; not issued |
| XOR command mask | reconstructed from the macOS build; not needed by any Linux read so far (`reply[1] = 0x02` plaintext) | unverified requirement; irrelevant to readback |
| Page reply byte 8 | `0x00`, unused by libeinstar | unknown semantics |

**What is *not* verified on our side:** the exact USB framing `einscan10.2.0.0.dll` uses
for `WriteData` (the plugin's `execute` handler is not fully mapped), whether `0x58`
requires an exactly-4 KiB payload and how partial writes are handled inside the device,
and whether the data region lives on the same SPI part as the firmware. Nothing in this
investigation executed a write.

**Consequence for our stack:** the read side (`0x57`) is safe, documented, and used by
the vendor on every connect; the write side (`0x58`) is real and reachable, which is
exactly why `is_forbidden()` in `tests/libeinstar-scratch/src/protocol.cpp` blocks it and
why `driver/` never implements it. Any future Linux write-back would need a deliberate
read-modify-write design and a fresh full 6568-byte (and ideally full 1 MiB) backup.

---

## 6. Safety verification

### 6.1 What was executed (all read-only)

| action | tool | scope |
|---|---|---|
| enumeration only | `einstar-probe` (no args) | libusb enumeration, no claim |
| identity + blob read | `einstar-probe --info --dump-flash /tmp/…bin` | handshake, `Get*` queries, 2 × `0x57` page read |
| identity + blob read (independent) | `build/driver/einstar-capture-frames --backend libusb --probe-only` | `ControlChannel` open, `Get*` reads, calibration read, close |

The coordinator explicitly authorised **one** bounded read-only `read custom info`
operation, on condition that the request be re-verified read-only from the repo. That
verification is §5 plus `tests/libeinstar-scratch/src/protocol.cpp`
(`is_read_only(PageRead) == true`, `is_forbidden(PageWrite) == true`, and
`allow_writes` stays false without `--scan`); the same read is what EXStar issues at
every connect (`ReadData(0,6568)` in both the 2026-02-09 and 2026-11-14 logs). One read
was taken with the libeinstar probe and cross-checked with the project's own driver
probe — two reads, zero writes. The dump is at
`~/einstar-private/exstar-20261114/device-readback/calibration.bin` (the shared
`~/einstar-private/exstar-20261114/` directory also holds another agent's copy of the
calibration BMPs; the readback lives in the `device-readback/` subdirectory).

Both tools rebuild/come from the reviewed sources at repo HEAD; the libeinstar probe was
rebuilt in `/tmp` immediately before use so the executed binary provably matches the
source that was inspected (`is_read_only()` classifies `PageRead` as read-only,
`PageWrite` and reboot as forbidden, and `--scan`-less operation never sets
`allow_writes`).

### 6.2 What was not executed

`0x58` PageWrite, system `0x06` firmware update, system `0x08` reboot, any
strobe/laser/trigger/exposure/gain write, any EEPROM or descriptor change, any USB 3.0
attempt, any write to the Windows partition (it is mounted `ro`; no mount was created or
removed). The only repository writes are this document and
`analysis/exstar_reference/hardware_state.json`.

### 6.3 State after the readback (observed)

| check | result | how |
|---|---|---|
| trigger | counts **mono 0, rgb 0** | driver probe and libeinstar probe, two reads |
| illuminator | laser **0 %**, strobe route0 **0**, route1 **0** | driver probe readback (`ControlChannel`) |
| streaming | off; the probes never start it | no `ImageStream` command issued |
| volatile settings | untouched; device reports defaults (period 66666 µs, 5000 µs / gain 500), consistent with the power cycle between the Windows run and this boot | readback |
| holder process | none (`lsof` empty) | — |
| kernel driver | none bound | sysfs |
| kernel log after the run | no `usb 1-5` reset/error line | `journalctl -k --since 01:30` |
| enumeration | unchanged, bus 1 port 5 device 5 | `lsusb` |

**Safety conclusion (A):** the scanner is idle, dark, and not triggering; the readback
left no observable device state change. EXStar itself ended the session with the stream
disabled (`Sn3DDeviceEinStar.cpp:505 enable Image Stream. enable: false` at 00:14:58).

### 6.4 Residual risk

The device was opened and claimed twice for a few seconds. Power state, temperature and
illumination were not physically inspected; "dark" is a register-readback claim, not a
photometric one. No command that can persist state was issued, so the only way this
investigation could have altered the device is through an undocumented side effect of
`0x57`/`Get*`; there is no evidence of that: the same bytes came back from two independent
implementations, the pre-run state was reproducibly identical across four reads in
2026-11-07/11, and the vendor issues the same read at every connect.

---

## 7. Open questions and the evidence that would settle them

| # | question | why still open | evidence that would settle it |
|---|---|---|---|
| Q1 | Is set A the unit's *original factory* calibration, or already a previous EXStar recalibration? | the only factory-dated artefact is `<redacted-production-date>` inside the production block; the CCF slot has no earlier copy | SHINING service/shipment record, or an older host backup (2024) |
| Q2 | Does the device keep a calibration-valid flag / time outside the CCF block? | only blocks 0–1 were touched and only the two known slots changed; the rest of the 1 MiB data region was not read | read the full 1 MiB data region before/after a calibration (read-only, 256 page reads), or a vendor document |
| Q3 | Exact USB framing and read-modify-write behaviour of `WriteData` (`0x58`) | plugin `execute` handler not fully mapped; we never issue writes | USBPcap/Wireshark capture of a Windows calibration, or a complete map of `einscan10.2.0.0.dll` |
| Q4 | Physical location/size of the `0x400000` data region | `docs/firmware/format.md` assumes a 4 MiB part, yet the region starts exactly at 4 MiB | FX3 SPI part number from the board photos/teardown, or a read of block `0xFF` |
| Q5 | Would a fresh EXStar install with different host files adopt the scanner's new CCF? | connect-time read is proven; precedence when host and device disagree is not directly observed | a Windows session with the `200x150` directory moved away (host-destructive, requires the user) |
| Q6 | Is `res/config.db` (SQLCipher) holding a third copy of the calibration/time? | no key; header is encrypted | vendor key/`SQLCipher` schema, or a memory dump of a running EXStar (out of scope here) |

| Q7 | Which CCF key hypothesis is correct for `fx/fy/cx/cy`? | only `fx − fy = 12` is forced; `best` (+8/−4/−6/−1) and `proven` (0) differ by up to 8 px in absolute terms | an independent absolute measurement (reference object / known-distance target) or a vendor statement; the in-flash 22-double records favour `best` to <1 px but are a different calibration |
| Q8 | Which layer should our pipeline trust — production records or the EXStar CCF? | they differ by ~1 % in baseline and we have no accuracy ground truth; the repo profile uses the CCF, the device's immutable record says otherwise | calibrated reference-object scans solved with each model, compared on the same captured data |

---

## 8. Reproduction

All commands are read-only; the first needs `libusb` only.

```bash
# independent read-only probe, rebuilt from the reviewed source at repo HEAD
cmake -S tests/libeinstar-scratch -B /tmp/einstar-probe-build -G Ninja \
      -DEINSTAR_WITH_OPENCV=OFF -DEINSTAR_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/einstar-probe-build --target einstar-probe
/tmp/einstar-probe-build/einstar-probe --info --dump-flash /tmp/einstar.bin

# the project's own probe (same read path, prints illuminator readback)
./build/driver/einstar-capture-frames --backend libusb --probe-only

# device blob diff (no device access)
python3 - <<'EOF'
import pathlib
a=pathlib.Path('<device-dump.bin>').read_bytes()
b=pathlib.Path('<private-inputs>/exstar-20261114/device-readback/calibration.bin').read_bytes()
print(sum(x!=y for x,y in zip(a,b)), 'bytes differ')
EOF
```

Machine-readable form of everything above:
`analysis/exstar_reference/hardware_state.json`.

---

### Appendix — exact vendor log excerpts (verbatim, shortened)

```
I1114 00:13:11.941125 10296 Sn3DEACalibrateDataController.cpp:372]  [DP 2023] Send CCF
I1114 00:13:11.941125 10296 Sn3DEACalibCompute.cpp:179]  writeCalibrateResultToDev,bRet = 1
I1114 00:13:11.941125 10296 Sn3DEinStarController.cpp:303]  [2022]cmd=writeCustomInfo
I1114 00:13:11.941125 10296 Sn3DDeviceBaseController.cpp:314]  write data , offSet.923
I1114 00:13:12.423637 20148 Sn3DDeviceBase.cpp:291]  WriteData result.0
I1114 00:13:12.424638 10296 Sn3DEACalibCompute.cpp:139]  WriteDataRet calibration.
I1114 00:14:54.314435 10296 Sn3DEACalibCompute.cpp:212]  writeWhiteBalanceResultToDev,bRet = 1
I1114 00:14:54.314435 10296 Sn3DDeviceBaseController.cpp:314]  write data , offSet.4796
I1114 00:14:54.996411 20148 Sn3DDeviceBase.cpp:291]  WriteData result.0
```

```
2026-02-09 21:31:25.202 : 24020 : SnSyncService : info : 505 : InternalE10Device::execute
  : execute start. cmd: "ReadData" , param: (QVariant(double, 0), QVariant(double, 6568))
2026-02-09 21:34:58.489 : 26404 : ... : cmd: "WriteData" , param: (QVariant(double, 923), ...)
2026-02-09 21:35:20.516 : 26404 : ... : cmd: "WriteData" , param: (QVariant(double, 4796), ...)
```

```
# EXStar_20261114_000944_607.log:477 (host-file view, before device connect)
calibrateTime "2026-02-09T21:34:58.486" ... offline true
# :505 (after the device blob was read; minute precision = the blob's own string)
calibrateTime "2026-02-09T21:34:00.000" ... offline false
# after the write
calibrateTime "2026-11-14T00:13:11.940"
```
