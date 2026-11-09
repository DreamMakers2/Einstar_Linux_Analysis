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
| Official versions | newest EXStar found for this model **1.2.2.0**; newest firmware **FX3 V2.10 / FPGA V3.7**; no official artifact contains V2.14 (verified negative within the sources searched) | **Verified** (hash-equal across 2023–2024 installers) | [docs/firmware/official_versions.md](docs/firmware/official_versions.md) |
| Firmware evolution | V2.10 adds in-firmware IAP with per-package checksum + read-back verify, full USB request handling, pipe/DMA recovery, A/B slots | **Verified** (string diff + both sides decompiled) | [docs/firmware/firmware_evolution.md](docs/firmware/firmware_evolution.md) |
| USB transport | 5 endpoints `01/81` INT, `02/82` BULK, `83` stream; `bInterval=0` descriptor bug **repaired to 7 by Linux USB core** — no patch or udev rule needed | **Verified** (descriptors + kernel source + field use) | [docs/protocol/einstar_usb_protocol.md](docs/protocol/einstar_usb_protocol.md) §3.2 |
| Measured cadence | With the working libeinstar driver on the physical scanner: HS ceiling **45.2 MB/s**, three-frame group floor **86.2 ms**; (1,1) at 90–100 ms sustainable with **zero loss**, (3,1) = 3 × period with **7 frames/trigger**; header[16..23] is a trigger-group counter, not a timestamp | **Measured** | [docs/benchmarks/measured_cadence.md](docs/benchmarks/measured_cadence.md) |
| USB 3.0 SuperSpeed | Firmware **implements and enables** SS (SS device descriptor, BOS, 103-byte SS config with 1024-byte packets and EP3 maxburst 15, `CyU3PConnectState(1,1)`, SS PHY programming, `USB3_LNKFAIL` handler), but the connected scanner negotiates **HS 480 Mbps**; the host port is SS-capable, so the block is the device-side physical path | **Verified** (firmware + live link) | [docs/protocol/usb3_capability.md](docs/protocol/usb3_capability.md) |
| **USB 3.0 hardware feasibility** | The mainboard carries a **CYUSB3014-BZXI** (121-ball, 0.8 mm BGA) whose SuperSpeed balls **A3–A6** have no AC-coupling pair, no SS ESD array, no test pad and no four-conductor route to any connector; the 7-pin external connector has **1 spare contact against 4 required**. A hand-soldered tap cannot meet 90 Ω ±7 %, < 76 mm and zero-via rules — the only properly engineered options are a new daughterboard (if SS is later *proved* on J4) or a board re-spin | **High** (package markings + vendor ball map + measured geometry + SI budget) | [docs/hardware/usb3_mainboard_feasibility.md](docs/hardware/usb3_mainboard_feasibility.md) — annotated figures in [hardware/annotated](hardware/annotated) |
| Command protocol | `[id][?][cmd u16 BE][arg u32 BE][payload]`; reply status at 4, `u32 BE` value length at 5..8 | **Verified** (host + device decoded independently) | same |
| Image stream | `[u32 BE len][32-byte header][payload]`; 32 × 40,960 B per 1280×1024 frame; **last**-packet flag at byte 8; **one-hot camera at bits 3..5** (sub-index 0..2); frame id valid only on the last packet; timestamp latched from the first | **Verified** (vendor code + hardware-proven parser + golden vectors) | same §3.1 |
| Firmware update | system op `0x06`; `len = arg − 2`; device erases/programs/read-back-verifies 4 KiB pages, writes app flag at flash `0x3F000`, reboots | **Verified** (device code) | same §5 |
| Flash layout | app slots `0x40000`/`0x1C0000`, FPGA slots `0x80000`/`0x200000`, flag sector `0x3F000`, 1 MiB bulk region `0x400000` | **High** (constants + write path; not read from hardware) | [docs/firmware/format.md](docs/firmware/format.md) §7 |
| Calibration format | 33 × float64 (`Calib_Results` order) + 256-byte IR trailer | **Verified** (matches vendor text twin) | [docs/calibration/ccf_format.md](docs/calibration/ccf_format.md) |
| IR calibration key | per-field integer offset; rotation key `(-2,-4,-11,-9,0,5,-11,-12,0)` is the **unique** solution of all 2⁹ candidates (det = 1 to 2e-15) | **Proved** (exhaustive + second calibration + independent record) | same |
| Stereo geometry | baseline **158.557 mm**, L–Tex 125.318, R–Tex 38.628, relative IR rotation **22.138°**, epipole 6693 px → rectification required | **Proved** (from decoded poses) | same |
| Calibration model | operational CCF model + rectification/triangulation; intrinsic key **bounded** to ≤1 px (depth effect 0.26 mm at 300 mm vs 2.9 mm from the baseline spread); 42/42 invariants; synthetic round trip at 1e-12 mm; real captures rectify with ≈1 px median vertical residual | **Validated for consistency**; physical accuracy unmeasured | [docs/calibration/model.md](docs/calibration/model.md) |
| Calibration workflow | EXStar's physical calibration = **5 groups × 5 distance bands (240/320/400/480/560 mm ±40) × 3 cameras = 75 images**; only the vertical group enforces ±8°; no per-step retry; measured session **2 min 56 s**, last capture→result ~1.3 s, device write ~0.34 s; the result overwrites the CCF files **and** the device block at `tagDevData+0x39B` with no backup | **Verified** (XML + code + real session log) | [docs/calibration/workflow_and_replacement.md](docs/calibration/workflow_and_replacement.md) |
| Calibration replacement | `calib/`: marker detection, joint robust bundle adjustment, NEES-calibrated covariance (1.06 × n), Fisher-information view selection, versioned host-side profiles; 0.367 px on the vendor corpus, IR baseline reproduced to 0.014 mm, 0.20° relative rotation | **Offline-validated**; physical accuracy requires a board measurement and a known artefact | [calib/](calib/), [tests/test_calib_engine.py](tests/test_calib_engine.py) |
| Acquisition backend | C++23 transport/pipeline/FFI + deterministic simulator; framing repaired, golden-vector conformance, fuzzing, 10 k-frame stress; ASan/UBSan/TSan clean | **Offline-validated**; no hardware run | [driver/](driver/), [docs/readiness.md](docs/readiness.md) |
| Command channel | serial, firmware string, sensor count, resolution, exposure/gain (+ranges), trigger mode/period, temperature, read-only page read; 5 attempts, echo/status validation, per-pipe recovery | **Offline-validated** (22 cases / 4,379 checks against a byte-accurate fake FX3) | [driver/CONTROL_CHANNEL.md](driver/CONTROL_CHANNEL.md) |
| Windows architecture | `EXStar.exe → SnSyncService.exe → einscan10 plugin → CommunicateLayer → SH3DU3Driver/cyusb3`; 741 PE modules mapped | **Verified** (import graph + strings) | [docs/windows/architecture.md](docs/windows/architecture.md) |
| Vendor reconstruction | dense 4-path SGM (census 5/7/9, ZNCC, subpixel, LR), speckle branch, CUDA 10.2/PTX 6.5 (251/230/134 kernels), ICP + particle filter, TSDF, marching cubes, SfM/SLAM | **High** (static evidence) | [docs/reconstruction/vendor_pipeline_analysis.md](docs/reconstruction/vendor_pipeline_analysis.md) |

A numbered list of every finding with confidence and evidence is in
**[docs/FINDINGS.md](docs/FINDINGS.md)**. Corrections to earlier assumptions are listed there
too (no V2.14 in official artifacts; the container is not malformed; the "first packet" flag is
a *last*-packet flag; the camera field is bits 3..5; `bInterval=0` needs no kernel change;
libeinstar's calibration key has the right form but wrong values; the app-flag sector is
`0x3F000`).

## What is measured (and what is not)

The acquisition backend in [`driver/`](driver/) is benchmarked with a **deterministic
simulator**. Every number is labelled `SIMULATED`; no hardware measurements exist yet. Earlier
reports quoted a single generic "300 MB/s" model — that is neither USB 2.0 nor USB 3.0 and is
**not** a scanner result. The tool now separates the link explicitly
([full report](docs/benchmarks/offline_benchmarks.md)):

| Scenario (simulated device; host columns real) | agg MB/s | fps/camera | p50 / p99 µs |
|---|---:|---:|---:|
| USB 2.0 HS (~40 MB/s), 3 cameras, depth 8 | **40.0** | 10.16 | 7,347 / 8,397 |
| USB 3.0 SS hypothetical (~350 MB/s), depth 8 | 229.7 | 58.41 | 980 / 1,460 |
| Vendor 68 ms trigger, 3 cameras, depth 1 | 58.2 | **14.81** | 620 / 10,344 |
| Host-only ceiling (link removed), depth 8 | 627.3 | 159.53 | 517 / 534 |

The important arithmetic: a frame is 1,311,872 B on the wire (payload + 32×36 B framing), so
three full frames per 68 ms need **57.9 MB/s** — more than practical USB 2.0 HS (~40 MB/s).
The scanner was connected on 2026-11-07 and negotiates **High Speed 480 Mbps**; the firmware does
enable SuperSpeed but the physical path does not train it
([details](docs/protocol/usb3_capability.md)). A direct measurement through libeinstar on the real
device ([measured_cadence.md](docs/benchmarks/measured_cadence.md)) replaces the model:

| requested | measured | outcome |
|---|---|---|
| (1,1) 90–100 ms | 39.2–43.6 MB/s, exact period, 0 lost | sustainable (86–96 % of the ceiling) |
| (1,1) 68 / 30 / 10 ms | clamped to **86.2 ms**, 45.1–45.2 MB/s | link-limited |
| (3,1) 68 ms | 7 frames/trigger, **222.5 ms** per trigger | period applies per slot |
| (3,1) 90 ms | 7 frames/trigger, **270.0 ms** per trigger | 3 × period |

The link's measured ceiling is **45.2 MB/s** (34.5 frames/s); EXStar's 68 ms slot cadence with two
IR frames per slot plus a colour frame every third slot averages ~45 MB/s — **99 % of the
measured ceiling**, which is why the vendor log contains 115 frame-grab timeouts.

Async issuance raises *saturated* throughput 3.2–3.7× over one outstanding transfer, but at the
vendor cadence every queue depth delivers the same frame rate and only the latency tail changes.
Zero-copy roughly halves host wall time. No hardware-dependent performance claim is made.

**Test suite:** 11 binaries / 90 cases / 57,450 checks (hand-written protocol golden vectors,
deterministic fuzzing, 10,000-frame stress) plus 96 Python tests. The C++ suite is clean under
AddressSanitizer + UndefinedBehaviorSanitizer and ThreadSanitizer.

## Repository layout

```
docs/                     technical reports (firmware, FPGA, protocol, calibration, Windows, reconstruction, hardware)
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
hardware/                 USB 3.0 feasibility: board photos, annotated figures, 121-ball map, analysis
scripts/verify_all.sh     one-command verification
```

## Reproduce

```bash
# 1) Python analysis suite (no proprietary data needed)
python3 -m pytest -q tests/           # 83 passed, 7 skipped (fixture-backed)

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

## Calibration workflow and replacement engine

The physical scanner calibration was reverse-engineered end-to-end: the 5×5
capture state machine, the distance/angle gates, the vendor's per-view PnP +
joint refinement, the CCF/device write-back, and the plate geometry. The vendor
corpus recovers an **8×5 plate, 27.72 mm pitch, four large markers**; a free
re-optimisation reproduces the IR baseline to **0.014 mm** and the relative IR
rotation to **0.20°** at 0.367 px RMS. The shipped colour record is *not*
co-registered with the IR pair (17 mm y offset), so the colour extrinsic must be
re-estimated on the user's unit.

`calib/` is a Linux-native replacement: sub-pixel marker detection, joint
robust bundle adjustment over three cameras, analytic covariance (synthetic
NEES ≈ 1.06 × n), Fisher-information view selection and uncertainty-driven
stopping, and versioned host-side profiles that preserve the factory snapshot
and never write the scanner implicitly. Ablations show the original 25-step
sequence is redundant for metric scale (3 views already recover the baseline to
0.03 mm) but not for intrinsics (≥20 views to approach the vendor parameters).
A greedy 8-view plan reduced the synthetic focal error 1.8×–6.4× versus a
fixed 8-view subset, depending on the scene and the distance/tilt diversity
constraint. Held-out views reproject at **0.378 px** vs 0.368 px training, and
the IR epipolar consistency is **0.138 px** median (CCF: 0.181 px). An
optional guarded ellipse sub-pixel mode with per-view rejection reaches
0.351 px.

**Not claimed:** physical sub-millimetre accuracy or superiority over the
vendor. That needs the actual board variant (8×5 vs the 8×7 drawn in the
manual), a measured length standard, a calibration run on the user's unit and a
known artefact. The roadmap's calibration track lists the gates.

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
