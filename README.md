# Einstar Linux Analysis

Reverse-engineering research and a native-Linux engineering baseline for the **SHINING 3D
Einstar** 3D scanner (`EinScan10_01`, USB `3267:0003`), produced offline from the shipped
Windows software, official firmware packages and a recorded device session — with no scanner
attached.

This repository publishes the **findings, tools, specifications, tests and benchmarks**.
It deliberately does **not** contain proprietary vendor binaries, firmware images, factory
calibration records or user data (see [Scope and exclusions](#scope-and-exclusions)).

---

## What is established

| Area | Result | Confidence | Report |
|---|---|---|---|
| Firmware container | `.img` = 320 × (4096-byte payload + 8-bit sum); de-interleaves to a 1,310,720-byte FX3 boot image | **Verified** (320/320 checksums, 3 images) | [docs/firmware/format.md](docs/firmware/format.md) |
| FX3 image | 5 sections at `0x100`, `0x40003000`, `0x40013000`, `0x40023000`, `0x40030000`; entry `0x40016F50`; image checksum `0x0FE314BC` | **Verified** (checksum recomputed) | same |
| EN vs CH | one locale word at SRAM `0x4000D7F0` + derived checksums; FPGA byte-identical | **Proved** (byte arithmetic) | same §5 |
| FPGA device | **Pango Microsystems Logos PGL25G**, Slave-Parallel bitstream; IDCODE `0x00511899`; 189 packets, 251,419 words, zero residue | **High** (vendor-doc byte match) | [docs/firmware/fpga_bitstream.md](docs/firmware/fpga_bitstream.md) |
| Official versions | newest EXStar for this model **1.2.2.0**; newest firmware **FX3 V2.10 / FPGA V3.7**; **no V2.14 exists** | **Verified** (hash-equal across 2023–2024 installers) | [docs/firmware/official_versions.md](docs/firmware/official_versions.md) |
| Firmware evolution | V2.10 adds in-firmware IAP with per-package checksum + read-back verify, full USB request handling, pipe/DMA recovery, A/B slots | **Verified** (string diff + both sides decompiled) | [docs/firmware/firmware_evolution.md](docs/firmware/firmware_evolution.md) |
| USB transport | 5 endpoints `01/81` INT, `02/82` BULK, `83` stream; `bInterval=0` descriptor bug | **Verified** (descriptors in firmware) | [docs/protocol/einstar_usb_protocol.md](docs/protocol/einstar_usb_protocol.md) |
| Command protocol | `[id][?][cmd u16 BE][arg u32 BE][payload]`; reply status at 4, `u32 BE` value length at 5..8 | **Verified** (host + device decoded independently) | same |
| Image stream | `[u32 BE len][32-byte header][payload]`; 32 × 40,960 B per 1280×1024 frame; last-packet flag; u64 BE timestamp | **Verified** | same |
| Firmware update | system op `0x06`; `len = arg − 2`; device erases/programs/read-back-verifies 4 KiB pages, writes app flag at flash `0x3F000`, reboots | **Verified** (device code) | same §5 |
| Flash layout | app slots `0x40000`/`0x1C0000`, FPGA slots `0x80000`/`0x200000`, flag sector `0x3F000`, 1 MiB bulk region `0x400000` | **High** (constants + write path; not read from hardware) | [docs/firmware/format.md](docs/firmware/format.md) §7 |
| Calibration format | 33 × float64 (`Calib_Results` order) + 256-byte IR trailer | **Verified** (matches vendor text twin) | [docs/calibration/ccf_format.md](docs/calibration/ccf_format.md) |
| IR calibration key | per-field integer offset; rotation key `(-2,-4,-11,-9,0,5,-11,-12,0)` is the **unique** solution of all 2⁹ candidates (det = 1 to 2e-15) | **Proved** (exhaustive + second calibration + independent record) | same |
| Stereo geometry | baseline **158.557 mm**, L–Tex 125.318, R–Tex 38.628, relative IR rotation **22.138°**, epipole 6693 px → rectification required | **Proved** (from decoded poses) | same |
| Windows architecture | `EXStar.exe → SnSyncService.exe → einscan10 plugin → CommunicateLayer → SH3DU3Driver/cyusb3`; 741 PE modules mapped | **Verified** (import graph + strings) | [docs/windows/architecture.md](docs/windows/architecture.md) |
| Vendor reconstruction | dense 4-path SGM (census 5/7/9, ZNCC, subpixel, LR), speckle branch, CUDA 10.2/PTX 6.5 (251/230/134 kernels), ICP + particle filter, TSDF, marching cubes, SfM/SLAM | **High** (static evidence) | [docs/reconstruction/vendor_pipeline_analysis.md](docs/reconstruction/vendor_pipeline_analysis.md) |

A numbered list of every finding with confidence and evidence is in
**[docs/FINDINGS.md](docs/FINDINGS.md)**. Corrections to earlier assumptions are listed there
too (no V2.14; the container is not malformed; the "first packet" flag is a *last*-packet flag;
libeinstar's calibration key has the right form but wrong values; the app-flag sector is
`0x3F000`).

## What is measured (and what is not)

The acquisition backend in [`driver/`](driver/) is benchmarked with a **deterministic
simulator**. Every number is labelled `SIMULATED`; no hardware measurements exist yet.

| Experiment (simulated: 300 MB/s link, 40 ms trigger, 1280×1024) | Result |
|---|---|
| Triggered, depth 1 → 16 | 25.1 fps at every depth; p50 latency 0.64 → 2.46 ms |
| Saturated (trigger removed), depth 1 → 16 | 65.5 → **245.0 MB/s**, 50.0 → **186.9 fps** (3.7×) |
| Zero-copy vs copy (depth 8) | host wall time 13.1 → 8.3 ms per 60 frames (~1.6× less CPU) |
| Record → replay | 39,356,160 bytes, digest identical, **byte-exact** |
| Test suite | 8/8 binaries, 53 cases, 44,434 checks, 10,000-frame stress |

Tail latency grows with queue depth (p99 ≈ 38 ms at depth 16 in the triggered model): deeper
queues buy throughput, not latency. That trade-off is the main tuning decision for a real
deployment.

## Repository layout

```
docs/                     technical reports (firmware, FPGA, protocol, calibration, Windows, reconstruction)
  FINDINGS.md             numbered findings with confidence + evidence
  roadmap.md              phased plan with measurable exit gates
  blockers.md             unresolved problems and what would resolve them
tools/firmware/           fx3img.py (container/sections/ELF), compare_fw.py, fx3_annotate.py
tools/fpga/               fpga_bits.py (Pango packet parser)
tools/ccf/                decode_ccf.py, validate_ccf.py (calibration format + invariants)
tools/protocol/           einstar_proto.py (command/reply/stream/update model)
driver/                   C++23 asynchronous acquisition backend + simulator + tools + tests
tests/                    fixture-free synthetic suite + evidence-backed suites (skip without fixtures)
analysis/                 derived metadata (PE classifications, FX3 symbol map, summaries)
scripts/verify_all.sh     one-command verification
```

## Reproduce

```bash
# 1) Python analysis suite (no proprietary data needed)
python3 -m pytest -q tests/           # 26 passed, 31 skipped (fixture-backed)

# 2) C++ acquisition backend: build, tests, benchmark
cmake -S driver -B build && cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure
./build/einstar-bench --out bench_results.json

# 3) Everything at once
scripts/verify_all.sh
```

To run the **evidence-backed** suites you need the vendor artefacts, which are not
distributed here. Put them under `fixtures/` (or set `EINSTAR_FIXTURES`) as documented in
[analysis/README.md](analysis/README.md); the fixture-dependent tests then run instead of
skipping.

## Scope and exclusions

**Not published** (deliberately):

* SHINING 3D binaries (`*.dll`, `*.exe`, drivers) and firmware images (`*.img`).
* Factory calibration records (`*CCF.txt`) and the 6568-byte device calibration dump.
* The vendor session log (132 MB) and any decompiled-code or vendor-string dumps.
* Device-specific identifiers: serial numbers, part number, production date, local paths.

**Published instead:** hashes, offsets, structure, numeric results, confidence levels and
reproduction steps — enough for an independent reviewer to re-derive every claim from their
own licensed copy of the software.

## Confidence scheme

* **Verified / Proved** — byte-exact check or a mathematical invariant that cannot pass by chance
  (checksums, orthonormality uniqueness, exhaustive search).
* **High** — follows from verified facts plus vendor documentation, no contradicting evidence.
* **Inferred** — one step of interpretation, explicitly flagged.
* **Open** — listed in [docs/blockers.md](docs/blockers.md) with a discriminating test.

## Licence and trademarks

Our code is MIT ([LICENSE](LICENSE)). Vendor documentation is referenced, not redistributed;
Einstar, SHINING 3D, Pango and Cypress/Infineon are trademarks of their respective owners
(see [NOTICE.md](NOTICE.md)).
