# Einstar FPGA configuration payload — vendor identification and packet format

**Target:** `analysis/firmware/EinScan10_EN_fpga.bin` (1,005,676 bytes, SHA-256
`cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4`), carved at
image offset `0x40190` from the de-interleaved FX3 IAP image
`EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img`; byte-identical in the CH
variant.

**Verdict (confidence: high, vendor certain / device high):** the payload is a
**Pango Microsystems "Logos" family configuration bitstream in pure `.bin`
form, on the wire in Slave-Parallel ×32 format, for a PGL25G**. It is *not*
compressed and *not* encrypted: it is the ordinary, uncompressed bitstream that
Pango's PDS tool emits (UG020005 Table 3-2: `.bin` = “Binary configuration data
without header information (pure bitstream), suitable for user configuration
schemes, such as microprocessor-based FPGA configuration”).

Reference used throughout: *Logos Family FPGAs Configuration User Guide*,
**UG020005 V2.3, 13.07.2022**, Shenzhen Pango Microsystems — the PDF retrieved
at <http://www.pangomicro.com/en/uploads/87196503_1711095867.pdf>.

---

## 1. Why Pango — the discriminating evidence

Four independent, exact matches. None of the other candidate vendors has a
documented stream that begins this way.

### 1.1 The first 8 bytes are a vendor-mandated bus-width probe

UG020005 §3.1.2.1 (*Bus Bit Width Auto Detection*):

> “In the Slave Parallel configuration mode, the data bus bit width is
> automatically detected by the CCS, which performs bit width matching by
> detecting the two-word bus bit width auto detection sequence
> **000000AA 08100020**. … After detecting 0xAA, it continues to check whether
> the next byte is 0x08, 0x10, or 0x20, corresponding to 8-bit, 16-bit, or
> 32-bit data bus.”

The file begins, byte for byte:

```
0x0000: 00 00 00 AA 08 10 00 20      <- 000000AA 08100020  (32-bit bus)
```

The same section prescribes `FFFFFFFF` “padding words” after the sequence; the
file has exactly ten of them (`0x0008..0x002F`).

### 1.2 The next word is the vendor sync word

UG020005 §3.1.2.2 (*Synchronization*): “The synchronization word
**0x01332D94** is used for 32-bit word boundary alignment. Only after
synchronization can the subsequent deframing operations be performed.”

```
0x0030: 01 33 2D 94
```

Table 3-4 of the same document even tabulates how `01332D94` is transmitted at
x8/x16/x32 widths, which is why the file's 32-bit big-endian word order is the
correct reading.

### 1.3 Everything after the sync word is a valid CCS packet stream

UG020005 §3.3.1 defines two packet types. Re-parsing the payload with those
rules consumes **all 251,419 words with zero residue**:

| Type | bits[31:29] | bits[28:27] | bits[26:22] | bits[21:0] |
|---|---|---|---|---|
| 1 (register) | `101` | op: 00 NOP, 01 Write, 10 Read, 11 rsvd | register address (Table 3-7) | number of 32-bit data words |
| 2 (config memory) | `010` | op: 01 Write, 10 Read | — | number of 32-bit data words |

Type 2 must follow a *Type 1 empty packet* (count = 0) that selects the target
register. That single rule explains the “opcode + 24-bit operand” appearance:

```
header byte : 1 0 1 | t t | r4 r3 r2        (t = opcode, r = register)
operand     : r1 r0 | count[21:0]
```

so the 24-bit “operand” is `{reg[1:0], count[21:0]}`. The curious operands
`0x800001 / 0x400001 / 0xC00001 / 0x000001` are simply registers with the same
low two register bits and `count = 1`. Every header byte 0xA0–0xAE decodes to a
real register from Table 3-7 — no exceptions, no unknown opcodes.

### 1.4 The IDCODE written into IDR is PGL25G

At `0x8C` the stream performs `IDR ← 0x00511899` (register 1, one data word).
UG020005 Table 3-8:

| Device | Device ID |
|---|---|
| PGL12G | X0501899 |
| PGL22G / PGL22GS | X0303899 |
| **PGL25G** | **X0511899** |
| PGL50G | X05A1899 |
| PGL50H | X0521899 |
| PGL100H | X0522899 |

`0x00511899` = `X0511899` → **PGL25G**. UG020005 §3.3.2.2: “Before loading the
bitstream, write the IDCODE to the IDR. Subsequent operations can be performed
only if the written IDCODE matches the device IDCODE.” A bitstream carrying
another device's ID would abort configuration.

### 1.5 Size cross-check

UG020005 Table 3-3, *Bitstream Sizes of Logos Family Devices*: **PGL25G,
uncompressed, 982 Kbyte**. The payload is **1,005,676 bytes = 982.1 KiB**
(0.02 % from the table value), while PGL50G is listed at 2052 KB — a factor of
two away. The size therefore selects PGL25G independently of the IDCODE.

### 1.6 What the other candidates look like (negative evidence)

* **Xilinx Spartan-6/7-series:** `.bit`/`.bin` start with `FF FF FF FF …` then
  `00 00 00 BB 11 22 00 44` and the `AA 99 55 66` sync word. Neither `AA995566`
  nor `000000BB`/`11220044` occurs in the payload (searched).
* **Lattice ECP5 / MachXO2/3:** bitstreams are `FF`-led and frame-oriented with
  no `000000AA 08100020` preamble; ECP5 frames are 544 bits and MachXO2 parts
  are far too small for a 982 KiB image.
* **Gowin GW1N/GW2A:** `.fs` is a text container; the binary form has no
  `AA+08/10/20` width probe and Gowin's IDCODEs are 32-bit `0x0000xxxx`
  values, not `0x00511899`.
* **Anlogic / Efinix / Intel:** no documented `0x01332D94` sync, no
  `101`/`010` packet headers with this register map, and no matching IDCODE
  table.

The `0xAA` + `0x08/0x10/0x20` + `0x01332D94` combination is unique to Pango in
the public documentation.

---

## 2. Decoded payload layout

All offsets are relative to the start of `EinScan10_EN_fpga.bin`.

| Offset | Size | Content |
|---|---|---|
| `0x00000` | 8 B | bus-width auto-detect `000000AA 08100020` (32-bit) |
| `0x00008` | 40 B | 10 × `FFFFFFFF` padding words |
| `0x00030` | 4 B | sync `01332D94` |
| `0x00034` | 0xD8 B | 17 Type-1 packets: device setup (16 register writes + empty `CMEMIR`), see §3 |
| `0x00108` | 4 B | Type-2 header, `count = 35,360` words |
| `0x0010C` | 141,440 B | configuration frames — **all zero**; ADRR type = `01` (DRM) |
| `0x2298C` | 80 B | 20 × `A0000000` no-op packets |
| `0x229DC` | 20 B | `ADRR ← 0`, `CMDR ← WCMEM`, empty `CMEMIR` packet |
| `0x229F0` | 4 B | Type-2 header, `count = 215,832` words |
| `0x229F4` | 863,328 B | configuration frames, ADRR type = `00` (non-DRM) |
| `0xF5654` | 80 B | 20 × `A0000000` no-op packets |
| `0xF56A4` | 56 B | trailer: `CRCR`, `CMASKR`, `CTRL1R`, `GUP`, `SWAKEUP`, `CRCR`, `DESYNC` |
| `0xF56DC` | 400 B | 100 × `A0000000` no-op packets (tail pad) |
| `0xF586C` | — | end of payload (next byte in the image is `0xFF`) |

The zero run at `0x010C..0x2298B` is *not* padding: it is a genuine 35,360-word
configuration-memory write of empty frames (a static `0x00000000` word run,
longest zero run in the file).

### 2.1 The fields the task asked about

| Field | Found? | Where / value |
|---|---|---|
| magic / vendor marker | **Yes** | `000000AA 08100020` bus-width probe + `01332D94` sync |
| device ID | **Yes** | `IDR ← 0x00511899` at `0x08C` → PGL25G |
| length / size | **Partly** | no global length field; each Type-2 packet carries its own 22-bit word count (`0x8A20` = 35,360; `0x34B18` = 215,832). The payload's end is implied by the packet stream and padded with no-op packets |
| checksum / CRC | **Yes (values), algorithm unknown** | `CRCR ← 0x0C10164C` at `0xF56A4` and `CRCR ← 0x86393275` at `0xF56CC`; no standard CRC-32 variant tested reproduces them (§8/§9) |
| version / date | **No** | nothing in the payload resembles a version or date. The FPGA version string “FPGA_V3.7” lives in the FX3 firmware (image name and `_FPGA_V` string), not in the bitstream. The `0x01332D94` at `0x30` is the *sync word*, not a version |
| block/row/frame address | **Yes** | `ADRR` bits `[26:25]` content type (DRM/non-DRM), `[24:20]` region, `[17:10]` column, `[7:0]` frame (Table 3-16). Both data blocks start at address 0 and rely on the CCS auto-incrementing through the frames |

---

## 3. Register-init block (0x34 … 0x10B)

| Offset | Header | Register | Value | Note |
|---|---|---|---|---|
| 0x034 | `AEC00001` | RCRR (27) | `0x03600000` | region control |
| 0x03C | `AB000001` | SBPIR (12) | `0x0000000B` | master-mode option bits (unused in slave mode) |
| 0x044 | `A0000000` ×10 | — | — | 10 no-op packets ≈ delay |
| 0x06C | `ABC00001` | IRSTCTRLR (15) | `0x00000000` | warm-boot control |
| 0x074 | `AC000001` | IRSTADRR (16) | `0x00000000` | warm-boot address |
| 0x07C | `A8800001` | CMDR (2) | `0x00000000` = NOP | |
| 0x084 | `A8800001` | CMDR | `0x00000001` = RSTCRC | reset CRC accumulator |
| 0x08C | `A8400001` | **IDR (1)** | **`0x00511899`** | **device ID check → PGL25G** |
| 0x094 | `AC400001` | WATCHDOGR (17) | `0x3FFFFFFF` | watchdog reload (max) |
| 0x09C | `ADC00001` | CMASKR (23) | `0xFFFFFFFF` | control-master mask |
| 0x0A4 | `A8C00001` | CTRL0R (3) | `0x00000000` | clock/port options |
| 0x0AC | `AE400001` | OPTION0R (25) | `0x00000080` | pre-load options |
| 0x0B4 | `AE800001` | OPTION1R (26) | `0x00000000` | pre-load options |
| 0x0BC | `AE000001` | FALLBACKR (24) | `0x00000100` | version fallback |
| 0x0C4 | `A8800001` | CMDR | `0x00000002` = SWITCH | switch mclk |
| 0x0CC | `A0000000` ×10 | — | — | delay |
| 0x0F4 | `AAC00001` | ADRR (11) | `0x02000000` | type = `01` (DRM), addr 0 |
| 0x0FC | `A8800001` | CMDR | `0x00000004` = WCMEM | write configuration memory |
| 0x104 | `A9400000` | CMEMIR (5), count 0 | — | empty packet: select data register |
| 0x108 | `48008A20` | Type 2 | — | write 35,360 words |

After the 35,360-word block the sequence repeats with `ADRR ← 0x00000000`
(type `00`, non-DRM) and a 215,832-word Type-2 write.

### Trailer (0xF56A4)

| Offset | Register | Value | Note |
|---|---|---|---|
| 0xF56A4 | CRCR (0) | `0x0C10164C` | CRC checkpoint #1 |
| 0xF56AC | CMASKR (23) | `0xFFFFFFFF` | |
| 0xF56B4 | CTRL1R (4) | `0x00000010` | |
| 0xF56BC | CMDR | `0x00000009` = GUP | enable internal logic |
| 0xF56C4 | CMDR | `0x00000007` = SWAKEUP | start wakeup |
| 0xF56CC | CRCR | `0x86393275` | CRC checkpoint #2 |
| 0xF56D4 | CMDR | `0x0000000B` = DESYNC | end of configuration |

This is a textbook Pango end-of-configuration sequence (UG020005 §3.3.2.3,
Table 3-9).

---

## 4. Size analysis — bitstream vs device capacity

| Quantity | Value |
|---|---|
| file size | 1,005,676 bytes = 982.1 KiB = 8,045,408 bits |
| 32-bit words | 251,419 |
| packets | 189 (187 Type-1, 2 Type-2) |
| configuration words (Type-2 data) | **251,192** = 1,004,768 bytes = 8,038,144 bits |
| — block 1 (ADRR type `01` = DRM) | 35,360 words (141,440 B), all zero |
| — block 2 (ADRR type `00` = non-DRM) | 215,832 words (863,328 B) |
| overhead (headers, register writes, pads) | 908 bytes |
| vendor table, PGL25G uncompressed | **982 KB** (UG020005 Table 3-3) |

**Frame stride.** Both data blocks are exact multiples of **68 words
(272 bytes)**: 35,360 = 520 × 68 and 215,832 = 3,174 × 68, so the payload is
**3,694 frames** of 2,176 bits. Independently, the non-zero-byte indicator of
the data region shows a strong autocorrelation peak at lag **272 bytes**
(0.382, the largest non-trivial lag) and at its multiples 544/816. This is
consistent with a 68-word configuration frame, but the exact frame map
(region/column/frame → bits) is not published in UG020005 and is **not
confirmed**.

The PGL25G is the 25K-class member of the Logos family (model-name
designation; UG020005 does not tabulate LUT counts); 982 KB is the family-documented
uncompressed size for that part, and the PGL50G entry (2052 KB) is
approximately twice as large, so the payload cannot belong to the larger part.

---

## 5. Compression / entropy — quantified

**No compressed section.** Measurements:

| Region | Entropy | Zero bytes |
|---|---|---|
| whole file | 3.189 bit/B | 62.1 % |
| `0x22000..end` (data) | 3.579 bit/B | 56.1 % |
| Type-2 block 2 (863,328 B) | 3.588 bit/B | 55.9 % |
| Type-2 block 1 (141,440 B) | 0.000 bit/B | 100 % |

A compressed or encrypted stream would sit near 7.9–8.0 bit/byte; this sits at
3.2–3.6 and is dominated by long zero runs, which is exactly what an
*uncompressed, mostly-empty* Pango bitstream looks like (unused frames carry
`0x00000000`). Supporting facts:

* the longest zero-word run is **35,360 words** (`0x010C..0x2298B`) — precisely
  the first Type-2 packet's payload;
* `0x00000000` words: **94,329 of 251,419** (37.5 %);
* `0xA0000000` words: **258**, of which 160 are Type-1 no-op packet headers and
  98 are data words that happen to equal the pattern; 103 runs, longest
  **100 words** at `0xF56DC` (the tail pad);
* byte histogram is dominated by `00` (62 %) with the single-bit values
  `01/02/04/…/80` each at 2.2–3.1 %, i.e. sparse configuration bits, not
  cyphertext;
* the 272-byte periodicity is a frame artefact, not a codec block size
  (a codec would not align to 2,176 bits and would not preserve an 88 %-zero
  bit density).

The apparent “0xA0 padding everywhere” of the initial survey is an artefact of
counting the top byte of *data* words; only 160 of the 999 `0xA?`-topped words
are actually packets.

---

## 6. Where the configuration bits start

The first configuration-memory word is at **`0x010C`** (immediately after the
`0x48008A20` Type-2 header), but that entire first block is zero. The first
**non-zero** configuration word is at `0x229F8` (second word of block 2);
the first non-zero bytes of block 2 are `80 00 14 40 …`. Everything before
`0x010C` is wire header + register programming; everything from `0x229F4`
on is real design data.

---

## 7. Relationship to the FX3 firmware (context, medium confidence)

The payload sits in the IAP image directly after the FX3 application image
(whose last section ends with the FX3 terminator `0x40016F50 / 0x0FE314BC` at
`0x2E75C`..`0x2E763`).
In `FX3_FW`:

* `FUN_400057b0` creates the `"20:FPGA Boot Thread"` (`FUN_400050cc`);
* `FUN_400050cc` initialises SPI (30 MHz), GPIO, UART (115200), I2C (100 kHz),
  reads/writes the SPI-flash status register (`flash_read_status`,
  `before/after write SR`), then calls `FUN_40008218`;
* `FUN_4000c290` (`"GetFpgaLoadAddr: 0x%x"`) reads the boot header at flash
  offset `0x3F0` and returns **`0x80000` or `0x200000`** — the SPI-flash slot
  holding the FPGA image;
* `FUN_40008218` reads `0x180` × `0x1000` bytes (1.5 MB) from that address in
  4 KiB SPI-FIFO bursts;
* `FUN_40005bac` emits an 8-byte record — `20 | reg | 02 | 00` followed by a
  32-bit value — through a byte-serial FX3 port (`FUN_400108a0` writes one byte
  at a time to `0xE000042C` while polling status flags at `0xE0000404`) and
  prints `"write data To FPGA Failed %d"` on error.  That is the FPGA's
  register/control channel, not a bulk path.

What is **certain** is that the payload itself is a complete, self-describing
Pango Slave-Parallel ×32 bitstream that begins with the vendor-mandated bus
probe and sync, i.e. it is designed to be pushed by an external master
(UG020005 Table 3-2: `.bin` is “suitable for user configuration schemes, such
as microprocessor-based FPGA configuration”).  Which routine actually pushes
the 1 MB stream, and over which port, was **not** located: `FUN_40008218` reads
1.5 MB from SPI flash in 4 KiB bursts into a single 4 KiB buffer
(`DAT_40030778`) and both its callers (`FUN_400050cc`, `FUN_40003e44`) are
reconfiguration flows, so it may be a verify/readback stage that runs around
the configuration rather than the configuration transport itself.  The
`GetFpgaLoadAddr` flash slots (`0x80000` / `0x200000`) and the `.img` container
placement are storage locations; the `.bin` carries no flash-file header.

---

## 8. Confidence summary

| Claim | Confidence | Basis |
|---|---|---|
| Vendor = Pango Microsystems, Logos family | **Certain** | exact `000000AA 08100020` probe, `FFFFFFFF` padding, `01332D94` sync, packet grammar, register map all from UG020005 |
| Device = **PGL25G** | **High** | IDCODE `0x00511899` = Table 3-8 PGL25G; 982 KB size matches Table 3-3 (PGL50G is 2052 KB) |
| Wire format = Slave Parallel ×32, pure `.bin` | **High** | §3.1.2.1 probe is the Slave-Parallel variant; `.bin` described as “without header information (pure bitstream), suitable for … microprocessor-based FPGA configuration” |
| Packet decode (types, registers, commands) | **Certain** | all 251,419 words parse with zero residue; every register/opcode hits Table 3-7/3-9 |
| Uncompressed, unencrypted | **High** | entropy 3.2–3.6 bit/B, 62 % zero bytes, 35,360-word zero block, size equals the *uncompressed* table entry |
| 68-word / 3,694-frame structure | **Medium** | exact divisibility of both blocks and 272-byte autocorrelation peak; frame map unpublished |
| Meaning of the all-zero DRM block | **Low** | ADRR type field = `01` (DRM) per Table 3-16; content is zero, so either the DRM region is unused or the block is a clear operation |
| CRC algorithm behind `0x0C10164C` / `0x86393275` | **Unknown** | no standard CRC-32 variant (zlib/ISO, MPEG-2, BZIP2, JAMCRC, POSIX, CRC-32C) over the config blocks or the whole stream reproduces either value |
| FX3→FPGA transport details | **Low/Medium** | see §7 |

---

## 9. What remains unknown, and the tests that would settle it

1. **CRC polynomial / coverage.** The two `CRCR` writes are the vendor's
   bitstream CRC checkpoints. Test: take a known-good Pango `.sbit` produced by
   PDS for any Logos part, run `fpga_bits.py` on it, and fit the documented
   CRC register value against candidate polynomials (including non-reflected
   `0x04C11DB7`, init `0xFFFFFFFF`, over the 32-bit word stream in transmission
   order, possibly seeded by `RSTCRC`). A `.sfc`/`.sbit` pair from the same
   design would also reveal whether the CRC covers frames or the raw packet
   payload.
2. **Frame map.** `ADRR` exposes `type[26:25]`, `addr_region[24:20]`,
   `addr_column[17:10]`, `addr_frame[7:0]` (Table 3-16) but not the frame size.
   Test: read back configuration memory on a live PGL25G (`RCMEM`/`CMEMOR`,
   UG020005 §4.2) with a known bitstream and correlate frame addresses with
   bit positions; or obtain Pango's PDS device database.
3. **DRM block.** Whether the 520 all-zero frames are a deliberate clear of the
   eFuse-DRM region or simply the first frames of a flat bitstream re-tagged as
   DRM. Test: compare with an `.sbit` built by PDS with encryption off; the
   ADRR type of its first write will show the tool's convention.
4. **Bitstream encryption.** UG020005 §7.2 describes eFuse-based AES
   encryption; the payload shows none (no high-entropy block, IDCODE in clear).
   Test: check whether the Einstar board's eFuse is programmed; if it were, the
   configuration data would be ciphertext and this decode would not be possible.
5. **Which FX3 routine streams the 1 MB.** `FUN_40008218` reads exactly
   1.5 MB (both flash slots) into a 4 KiB buffer; the routine that actually
   pushes the payload to the FPGA over GPIF was not located. Test: trace
   `FUN_400108a0` call sites with a size > 4 KiB, or instrument
   `_DAT_e000042c` writes.

---

## 10. Repro

```bash
python3 tools/fpga/fpga_bits.py --info
python3 tools/fpga/fpga_bits.py --opcodes
python3 tools/fpga/fpga_bits.py --hist
python3 tools/fpga/fpga_bits.py --dump-words --start 0x30 --count 0xe0
python3 tools/fpga/fpga_bits.py --info --json-out /tmp/fpga.json
python3 -m pytest -q tests/test_fpga.py
```

`--info` prints the identification, register/command sequence and sizes;
`--opcodes` prints the naive top-byte histogram next to the real decoded
Type-1 table; `--hist` prints byte/word histograms, entropy per 4 KiB block,
zero/`0xA0` run statistics and the autocorrelation peaks; `--dump-words` prints
a structural disassembly of any window; `--json-out` dumps the full packet list.
