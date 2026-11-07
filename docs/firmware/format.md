# Einstar (EinScan10_01 / SC130) firmware package format

Status: **verified** (byte-exact, checksum-proven). Reverse engineering performed on the two
factory images in `sources/firmware/` and two further official images recovered from the
EXStar 1.0.6.0 installer (`sources/firmware/official_downloads/`).

Tool: `tools/firmware/fx3img.py` (extracts, verifies, re-parses, emits ELF32-ARM).

---

## 1. Summary

The `.img` file is **not** a raw FX3 boot image. It is a transport container built from
**4097-byte records**: 4096 payload bytes followed by one 8-bit checksum byte. Removing the
checksum bytes (de-interleaving) yields a **standard Cypress EZ-USB FX3 boot image**
(`"CY"`, `bImageType = 0xB0`) as specified by Infineon AN76405 *EZ-USB FX3 Boot Options*,
Table 14 — with a fully valid section table and a valid 32-bit image checksum.

The de-interleaved image is 1,310,720 bytes (`0x140000`) and contains:

1. a 5-section FX3 (ARM926EJ-S) application image, and
2. an FPGA configuration payload beginning at image offset `0x40190`.

```
.img file (1,311,040 B = 320 x 0x1001)
 ├─ record 0   : payload[0x0000..0x1000) + csum[0x1000]
 ├─ record 1   : payload[0x1000..0x2000) + csum[0x2001]
 ├─ ...
 └─ record 319 : payload[0x13F000..0x140000) + csum[0x14013F]
        │  de-interleave (drop every 4097th byte)
        ▼
FX3 boot image (1,310,720 B)
 ├─ 0x00000 header "CY" 1C B0 + 5 sections ... terminator @0x2E758
 └─ 0x40190 FPGA configuration payload (1,005,676 B, 0xF586C)
```

## 2. Container layer: 4097-byte records

| Field | Offset in record | Size | Meaning |
|---|---|---|---|
| payload | 0 | 4096 | image bytes |
| checksum | 4096 | 1 | `sum(payload) mod 256` |

Verification: **320/320 record checksums valid** in the EN image, the CH image, and (after
conversion) the V1.0 recovery image is stored without this layer (see §7).

Why the naive parse fails: one byte is inserted every 4096 bytes, so any structure that
crosses a 4096 boundary shifts by one byte per boundary. The FX3 section 1 is 0x2528 bytes
of data starting at image offset 0x0C, so it ends at image offset 0x2534; in **file** offsets
that is `0x2534 + 2 = 0x2536` — exactly the "mysterious" header seen at file offset 0x2536
in the initial investigation. Likewise the FX3 terminator at image 0x2E758 appears at file
offset `0x2E758 + 0x2E = 0x2E786`, so the entry word at image 0x2E75C appears at file 0x2E78A
(the "candidate firmware termination record" from the earlier hypothesis). The hypothesis was
correct in location but wrong in interpretation: the 2-byte shift is caused by the container,
not by an unusual section layout.

## 3. FX3 boot image layer

Header (4 bytes, AN76405 Table 14):

| Offset | Value (V2.10) | Meaning |
|---|---|---|
| 0x00 | `43 59` (`"CY"`) | `wSignature` |
| 0x02 | `0x1C` | `bImageCTL`: SPI 400 kHz, 64 KB I²C/SPI address size |
| 0x03 | `0xB0` | `bImageType`: normal firmware image with checksum |

Sections (`dLength` = number of 32-bit words, `dAddress` = load address):

| # | image offset | words | bytes | load address | region | notes |
|---|---|---|---|---|---|---|
| 1 | 0x000004 | 0x094A | 9,512 | `0x00000100` | ITCM | vectors + startup + hot code |
| 2 | 0x002534 | 0x4000 | 65,536 | `0x40003000` | SYSMEM | main code |
| 3 | 0x01253C | 0x4000 | 65,536 | `0x40013000` | SYSMEM | main code continuation |
| 4 | 0x022544 | 0x1B4D | 27,988 | `0x40023000` | SYSMEM | code/data |
| 5 | 0x029280 | 0x1534 | 21,712 | `0x40030000` | SYSMEM | data (strings, tables, globals) |
| — | 0x02E758 | 0 | — | entry `0x40016F50` | — | terminator: `dLength = 0`, `dAddress` = entry |
| — | 0x02E760 | 4 | — | `0x0FE314BC` | — | `dCheckSum` |

Checksum: 32-bit sum of **every data word of every section** (header, `dLength`, `dAddress`,
terminator and checksum itself excluded). Computed = stored = `0x0FE314BC` → **valid**.

The image occupies `0x000000..0x02E764`; the rest of the 1,310,720-byte payload is the FPGA
region (0x2E764..0x4018F is 0xFF padding).

## 4. FPGA configuration payload

| Property | Value |
|---|---|
| image offset | `0x40190` (first non-0xFF byte after the FX3 image) |
| size | 1,005,676 bytes (`0xF586C`), ends at image `0x1359FC` |
| SHA-256 (EN and CH, identical) | `cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4` |
| first bytes | `00 00 00 AA 08 10 00 20` then `0x28` bytes of FF, then a 32-bit big-endian packet stream (`A0` = NOP, `A8/AA/AB/AC/AD/AE` = commands) |

### 4.1 Device identification — Pango Microsystems "Logos" PGL25G

The payload is a **Pango Microsystems Logos-family bitstream** in the documented
"Slave Parallel" configuration format:

| Evidence | Bytes / value |
|---|---|
| bus-width auto-detect sequence (documented) | `00 00 00 AA 08 10 00 20` at offset 0 |
| ten padding words (documented) | `FF FF FF FF` × 10 at `0x08..0x2F` |
| synchronization word (documented) | `01 33 2D 94` at `0x30` |
| IDR write carrying the device IDCODE | `A8 40 00 01` `00 51 18 99` → IDCODE `0x00511899` = **PGL25G** |
| payload size vs device capacity | 1,005,676 B ≈ 982 KB, matching the PGL25G uncompressed bitstream size |

After the header the stream is 32-bit **big-endian** Type‑1/Type‑2 configuration packets
(Type‑1: bits 31:29 = `101`, opcode 28:27, register 26:22, count 21:0; Type‑2: `010`, opcode,
count 26:0) writing the documented registers `CRCR`, `IDR`, `CMDR`, `CTRL0R`, `CTRL1R`,
`CMEMIR`, `ADRR`, `IRSTCTRLR`, `IRSTADRR`, `WATCHDOGR`, `CMASKR`, `FALLBACKR`, `OPTION0/1R`,
`RCRR`. The stream ends with `CRCR`, `IRSTCTRLR`, `CTRL1R`, `CMDR` (GUP/SWAKEUP), a second
`CRCR` and `CMDR` (DESYNC).

Because the format is *Slave Parallel*, the FX3 is the configuration master: it reads the
bitstream from SPI flash (`GetFpgaLoadAddr` → `0x80000`/`0x200000`) and drives the FPGA's
parallel configuration port. This is consistent with the observed flash layout and with the
absence of any FPGA master-SPI mode in the firmware.

The payload is **byte-identical between the EN and CH images** (same SHA-256), so the FPGA
configuration is language-independent; it differs between FPGA V2.6 and V3.7 with the same
length.

**Independent parse check** (this repository, `analysis` script): after the 13-word header the
stream is 251,406 32-bit words; decoding Type‑1 (bits 31:29 = `101`, opcode 28:27, register
26:22, count 21:0) and Type‑2 (`010`) packets **consumes every word with zero residue** —
187 Type‑1 packets, 2 Type‑2 packets, 251,192 data words, touching registers
`0,1,2,3,4,5,11,12,15,16,17,23,24,25,26,27`. That matches the register map and the
`1,005,676 / 4 = 251,419` word count exactly (13 header words + 251,406 stream words).

Not yet matched: the CRC polynomial and the exact frame map. Full analysis:
`docs/firmware/fpga_bitstream.md`.

The FX3 firmware programmes this region itself: thread `"20:FPGA Boot Thread"` (entry
`FUN_400050cc`, created by `FUN_400057b0`) configures SPI flash access and the FPGA interface;
`GetFpgaLoadAddr` (`FUN_4000c290`) computes the FPGA slot base from the flash app-flag block.

## 5. EN vs CH: complete mathematical explanation

The two factory images differ at exactly **six bytes in the file**, which reduce to **two
32-bit words in the de-interleaved image** plus the two record checksums they affect:

| File offset | Image offset | Meaning | EN | CH |
|---|---|---|---|---|
| 0xCD38, 0xCD39 | 0xCD2C, 0xCD2D | locale string in the version block | `45 4E` ("EN") | `43 48` ("CH") |
| 0xD00C | — | record 12 checksum | `0x77` | `0x6F` |
| 0x2E78E, 0x2E78F | 0x2E760, 0x2E761 | FX3 `dCheckSum` word | `0x0FE314BC` | `0x0FE30EBA` |
| 0x2F02E | — | record 46 checksum | `0x26` | `0x1E` |

Derivation:

* The locale field is a 32-bit word `0x00004E45` ("EN\0\0") vs `0x00004843` ("CH\0\0") at
  SRAM address **`0x4000D7F0`** (section 2: `0x40003000 + (0xCD2C - 0x2534)`).
* Its word difference is `0x4E45 - 0x4843 = 0x602`, so the FX3 image checksum decreases by
  exactly `0x602`: `0x0FE314BC - 0x602 = 0x0FE30EBA`. ✔
* Record 12 contains that word, so its 8-bit payload sum decreases by the sum of the changed
  bytes (`0x45+0x4E = 0x93` vs `0x43+0x48 = 0x8B`, i.e. 8): `0x77 - 8 = 0x6F`. ✔
* Record 46 contains the FX3 checksum word; its byte sum also decreases by 8
  (`0xBC+0x14 = 0xD0` vs `0xBA+0x0E = 0xC8`): `0x26 - 8 = 0x1E`. ✔

So there are **no functional differences other than the language tag**. Both images configure
the same FPGA bitstream and run the same FX3 code.

## 6. Firmware variants recovered from official installers

| Artifact | FX3 | FPGA | Size (bytes) | SHA-256 | Notes |
|---|---|---|---|---|---|
| `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img` | V2.10 | V3.7 | 1,311,040 | `52346b33…c68c3d` | factory, block-checked container |
| `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_CH_IAP.img` | V2.10 | V3.7 | 1,311,040 | `0c5a41b5…6e7c5` | factory, block-checked container |
| `EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img` | V1.0 | V2.6 | 1,310,720 | `067ce53d…0c9e45` | **raw** image (no 4097 layer), `fabu_UPDATE/writeBootLoader/` |
| `Fx3BootAppGcc.img` | Cypress boot app | — | 29,876 | `2d3f346c…33713e` | FX3 boot/recovery application (see §7) |

The V1.0/V2.6 image parses cleanly as a raw FX3 image:

| # | words | bytes | load address |
|---|---|---|---|
| 1 | 0x08C8 | 8,960 | `0x00000100` |
| 2 | 0x4000 | 65,536 | `0x40003000` |
| 3 | 0x4000 | 65,536 | `0x40013000` |
| 4 | 0x10E0 | 17,280 | `0x40023000` |
| 5 | 0x10A2 | 16,648 | `0x40030000` |

entry `0x40015488`, checksum `0x91AEFEAA` (verified). Its FPGA payload also begins at image
offset `0x40190`; trimming trailing 0xFF gives the same 1,005,676-byte extent as V2.10 (the
rest of the 0x140000 image is erased-flash padding).

> **Important:** the newer "≥ FX3 V2.14" firmware hypothesised in the task brief does not
> exist in any official artifact. See `docs/firmware/official_versions.md` — the current and
> final official release for the original Einstar is EXStar 1.2.2.0 (2024-06-28) containing
> FX3 V2.10 / FPGA V3.7, byte-identical in the 2023 1.0.6.0 build, the 2024 1.2.2.0 build and
> the 2024 macOS package.

## 7. Flash organisation (inferred, high structural confidence)

Evidence from the firmware itself:

* `FUN_4000c290` ("GetFpgaLoadAddr") reads a 0x100-byte structure and uses
  `{u32 uiNewAppAddr; u32 uiOldAppAddr; u8 ucIsBootNewApp;}` — addresses `0x40000` and
  `0x1C0000` are the two application slots. **Address correction (proven by the FX3 write
  path):** the SPI helper `FUN_40007F64` takes `addr >> 8`, so the call with `0x3F0` reads
  flash **`0x3F000`**, the last 4 KiB sector below slot A — not `0x3F0`.
* Its return value is the FPGA slot base: `0x80000` or `0x200000` (A/B slots 0x180000 apart).
* `FUN_4000b320` toggles the current/next slot between `0x40000` and `0x1C0000` for A/B
  (power-fail-safe) updates.
* The combined update payload is laid out as `[application @ 0x0][FPGA @ 0x40000]`; the FPGA
  region's first non-0xFF byte is at `0x40190`, i.e. 0x190 bytes of 0xFF padding inside the
  FPGA region. Written at an app slot base this maps to `0x80000`/`0x200000` exactly.

Resulting map (4 MB SPI flash assumed; `0x200000 + 0xF586C = 0x2F586C`):

| Flash range | Content |
|---|---|
| `0x03F000..0x03FFFF` | app-flag sector (12-byte `{uiNewAppAddr, uiOldAppAddr, ucIsBootNewApp}`) |
| `0x040000..0x06E764` | application slot A (FX3 image) |
| `0x080000..0x17586C` | FPGA configuration slot A |
| `0x1C0000..0x1EE764` | application slot B |
| `0x200000..0x2F586C` | FPGA configuration slot B |

The update writer proves the mapping: `FUN_4000B320` erases sector
`newApp + page*0x1000`, programs 4096 bytes, reads back, compares the 8-bit sum with the
record's checksum byte, advances `page`, and finally erases sector `0x3F` and writes the
12-byte app flag at `0x3F000` before rebooting. Because the payload is written sequentially
from `newApp`, the FPGA region at image offset `0x40000` lands at `newApp + 0x40000` =
`0x80000` (slot A) or `0x200000` (slot B) — exactly what `GetFpgaLoadAddr` returns.

Confidence: **proven** for the slot addresses, the 0x3F000 flag sector and the sequential
write; **open** only for the meaning of the 0x190-byte 0xFF prefix inside the FPGA region
(`0x40000..0x4018F`), which is padding regardless. Discriminating test: read
`0x80000..0x80200` from a real device over the `ReadData` execute command.

## 8. Reproduction

```bash
python3 tools/firmware/fx3img.py sources/firmware/EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img \
    --json analysis/firmware/EN_summary.json \
    --extract-image analysis/firmware/EinScan10_EN_deinterleaved.bin \
    --extract-fpga analysis/firmware/EinScan10_EN_fpga.bin \
    --elf analysis/firmware/EinScan10_EN.elf
```

Expected output: 320/320 record checksums OK, FX3 checksum OK, entry `0x40016F50`, FPGA
payload offset `0x40190`. The emitted ELF32-ARM places the five sections at their real load
addresses so Ghidra/IDA/objdump analyse correct absolute addresses; Ghidra finds 518
functions, and the decompiled corpus is kept in `analysis/ghidra/FX3_FW/`.

## 9. Evidence index

| Claim | Evidence |
|---|---|
| 4097-byte record format | verified 320/320 sums, three images; matches host code `FUN_18000bc80` in `einscan10.2.0.0.dll` which sums 4096 bytes and compares the next byte |
| FX3 section format | Infineon AN76405 Table 14 + successfully validated chain and checksum |
| Entry point 0x40016F50 | FX3 terminator `dAddress` |
| FPGA payload identical EN/CH | SHA-256 equality, `analysis/firmware/*_fpga.bin` |
| Locale-only difference | byte-diff + checksum arithmetic (§5) |
| Flash slot constants | `FUN_4000c290`, `FUN_4000b320` in the decompiled FX3 corpus |
| Update transport | `FUN_18000bc80` / `FUN_180002f50` in `einscan10.2.0.0.dll` |
