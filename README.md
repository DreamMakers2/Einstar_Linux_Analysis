# Einstar Linux Analysis

Reverse-engineering research and a native-Linux engineering baseline for the **SHINING 3D
Einstar** 3D scanner (`EinScan10_01`, USB `3267:0003`), produced offline from the shipped
Windows software, official firmware packages and recorded device sessions. The scanner was
attached for controlled bring-up and validation in November 2026; hardware-dependent claims
are labelled with their evidence.

This repository publishes the **findings, tools, specifications, tests and benchmarks**.
It deliberately does **not** contain proprietary vendor binaries, firmware images, factory
calibration records or user data (see [Scope and exclusions](#scope-and-exclusions)).

---

## Current milestone — the Windows EXStar reference (2026-11-14)

A fresh manufacturer calibration on the same scanner gave the first direct
comparison with our recovered Linux rig. The recording is **66.400 s** (edited;
≈85–90 s of the session are missing), with **25 automatic poses in 46.867 s**
(1.87 s/view), a 1.733 s solve and **7.666 s of white balance** — EXStar is only
~2.4× faster per accepted view than our wizard, but asks for **11.9× fewer
views** (25 vs 297). It ticks marks during motion and never rejects a stationary
frame as redundant, the exact rejection that lost our dwell evidence.

EXStar's new CCF is baseline **158.6128 mm** against the device's factory record
**160.0916 mm**; our recovered rig is **160.0137 mm**, i.e. 0.08 mm from the
factory branch, while the CCF's colour extrinsic is 8° off. Distortion is
bit-identical across every vendor calibration, so the manufacturer never
re-estimates it either. Refitting the vendor's own 25 fresh images with our
estimator gives 160.210 mm at 0.314 px RMS; their CCF explains the same images
at 1.420 px. The remaining 1 % is a disagreement *between two vendor datasets*,
not between us and the vendor; only a measured plate pitch or a certified
artefact can decide it.

The calibration is stored **on the device**: a read-only 4 KiB page read after
the Windows run is byte-identical to the new CCF/WB files in the two documented
regions (0x39B, 0x12BC), with 366 bytes changed and the identity/factory records
untouched. White balance was recovered from `Sn3DColorCorrect.dll` (centred
grey-world window, dark-subtracted, green pinned, diagonal gains only) and
implemented as a separate reversible photometric layer; the held-out cast drops
from 34.96 % to 1.93 %. Colour *accuracy* remains unclaimed.

The wizard now captures a normal calibration without a raw stream or
uncompressed view dump: accepted views go through a compact production store
(lossless PNG triplet + refined observations + append-only journal), measured at
**1.689 MB per accepted view vs 3.948 MB as PGM (2.34×)** on 24 real recovered
views, with `verify`/`resume` passing on the same data. The missing-view workflow
(base session opened read-only, planned holds in a separate extension session)
is reachable from the modern window's **Complete calibration…** button, and a
profile is never activated automatically.

* [Final report](docs/calibration/exstar_linux_final_report.md)
* [Video analysis](docs/calibration/exstar_reference_video_analysis.md)
* [EXStar vs factory vs Linux](docs/calibration/exstar_vs_factory_vs_linux.md)
* [Device state and readback](docs/calibration/exstar_hardware_state.md)
* [White balance](docs/calibration/white_balance.md)
* [Missing views](docs/calibration/missing_views_workflow.md)
* [Production storage](docs/calibration/production_capture.md)

## Previous milestone — calibration performance (2026-11-12)

The calibration application was rebuilt around four independent workloads
(acquisition, preview, detection, optimisation). On the preserved 35-view
session the complete finalisation dropped from **38.94 s to 3.47 s (11.2x)**
at the same optimum (0.33037 vs 0.33044 px RMS), with every held-out,
split-half, epipolar, coverage and lifecycle gate still running. On the
physical scanner, preview held **9.75 Hz** (p95 121 ms) while decisions ran at
4.55/s, capture held 9.76 groups/s at 36.9 MB/s with 0 transfer timeouts, and a
GUI `SIGKILL` left capture stopped with verified trigger `0/0` and illuminator
readback `0`. A 120 s soak held 9.96 Hz preview with zero transfer timeouts;
after a first soak exhausted a tmpfs, capture now stops at 2 GB free. Killing
the capture child makes the parent run a volatile-only `--safe-off` recovery
(trigger and illuminator readback 0) and surface the failure.

The acceptance round then closed three gaps. The intermittent Qt exit crash
was reproduced (focused test 37 %) and proven to be an unparented dialog kept
alive by self-capturing lambdas; the use-after-free at PySide shutdown is fixed
and an 8-process exit-code regression passes. A 36-view synthetic session
through the real operator window reached **8/8 READY and a validated candidate**
(33 accepted, 41 s, 8.3 cores, 642 MB); a 60-view run validated **54 accepted**
views in 72.6 s. The incomplete vendor still collection is refused with no
candidate. Pause/resume was broken by a shadowed `resume()` method (capture
stayed paused); it is fixed and verified on hardware at 32.65 MB/s with
verified cleanup. The 35-view finish remains 3.61 s at 0.33037 px RMS. An intermittent
`no_descent` LM result with a negligible gradient was being misclassified as a
failure and silently fell back to SciPy; it is now recognised as a stationary
fast-path solution (100/100 paired fits stayed on LM, equivalence unchanged).

The complete READY/validated sessions are diverse synthetic still sequences.
A 30 s replay of the real moving-plate sweep processed 115 groups and accepted
8 at 3.8 Hz on the default synchronous path. With the opt-in split path
(`--async-decisions`) it held **10.0 Hz preview (p95 114 ms)** and 3.7
decisions/s, but still reached only **2/8 coverage**. A physical moving-plate
calibration remains the main unverified step.

* [Performance and architecture report](docs/calibration/performance_overhaul_2026-11-12.md)
* [Operator UI](docs/calibration/modern_ui.md)
* [Safe automatic exposure](docs/calibration/auto_exposure.md)
* [Capture process safety](docs/calibration/process_safety.md)
* [Established-systems review](docs/calibration/established_systems_review.md)
* Evidence: `analysis/calibration/overhaul/`

The preserved 35-view capture remains **NOT READY** for geometric coverage;
no comprehensive physical calibration is claimed. Absolute dimensional
accuracy still requires an independent length artefact.

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
| Calibration replacement | `calib/`: marker detection, joint robust bundle adjustment, **marginal** covariance (NEES 0.91–1.02 n fixed kc; 1.17–1.32 free kc), Fisher-information view selection, conservative coverage/held-out stopping, versioned host-side profiles; 0.367 px on the vendor corpus, IR baseline 0.014 mm, 0.20° relative rotation | **Offline-validated**; physical accuracy requires a board measurement and a known artefact | [calib/](calib/), [docs/calibration/covariance_and_stopping.md](docs/calibration/covariance_and_stopping.md) |
| Adaptive calibration | Task-space A-optimal guidance over the 3-D volume, dwell/blur/sync gating, two-stage localisation, LM solve, saturation stopping, verification. 10-trial Monte Carlo: **0.205 mm** median 3-D error at 25 views vs **0.238 mm** for EXStar's actual kc-fixed 5×5 plan (6/10 paired, not significant at n=10) and 0.380 mm when that plan frees 5-term distortion; k1k2 distortion is 0.600 mm | **Offline-validated** (synthetic truth + real consistency); physical accuracy unverified | [design](docs/calibration/calibration_system_design.md), [strategy](docs/calibration/strategy_evaluation.md) |
| Acquisition backend | C++23 transport/pipeline/FFI + deterministic simulator; framing repaired, golden-vector conformance, fuzzing, 10 k-frame stress; ASan/UBSan/TSan clean | **Offline-validated**; no hardware run | [driver/](driver/), [docs/readiness.md](docs/readiness.md) |
| Command channel | serial, firmware string, sensor count, resolution, exposure/gain (+ranges), trigger mode/period, temperature, read-only page read; 5 attempts, echo/status validation, per-pipe recovery | **Offline-validated** (22 cases / 4,379 checks against a byte-accurate fake FX3) | [driver/CONTROL_CHANNEL.md](driver/CONTROL_CHANNEL.md) |
| Windows architecture | `EXStar.exe → SnSyncService.exe → einscan10 plugin → CommunicateLayer → SH3DU3Driver/cyusb3`; 741 PE modules mapped | **Verified** (import graph + strings) | [docs/windows/architecture.md](docs/windows/architecture.md) |
| Vendor reconstruction | dense 4-path SGM (census 5/7/9, ZNCC, subpixel, LR), speckle branch, CUDA 10.2/PTX 6.5 (251/230/134 kernels), ICP + particle filter, TSDF, marching cubes, SfM/SLAM | **High** (static evidence) | [docs/reconstruction/vendor_pipeline_analysis.md](docs/reconstruction/vendor_pipeline_analysis.md) |
| **Linux reconstruction (new)** | `recon/` (C++23/OpenCV 5): validated 3-camera profile → IR rectification → padded SGBM + sub-pixel + left-right consistency + confidence → metric depth with uncertainty → PLY/maps/report. Genuine Einstar frames: **56 600 points**, 92 % on one plane at **0.34 mm RMS**, 0.12 s/frame at half resolution, byte-identical reruns; synthetic 200–700 mm planes recovered to **≤0.13 mm**. Colour registration is gated on a profile marked `ok`. | **Offline-validated + real-data consistency**; absolute accuracy **unverified** | [engine](recon/), [real-data report](docs/reconstruction/real_data_report.md), [benchmarks](docs/reconstruction/benchmarks.md) |
| **GPU benchmark (new)** | RTX 3080 / CUDA 13.4: CUDA SGM **3.47 ms** steady vs CPU SGBM **5.73 ms** on a 640x512 pair (128-disparity window), HH4 2.64 ms; 0.125 px median disparity agreement, but lower planar quality and no LR/confidence. CUDA SGM is **not integrated**; OpenCL remap is 4x faster but saves ~0.1 ms. | **Measured**; no end-to-end GPU speedup claimed | [gpu_benchmark.md](docs/reconstruction/gpu_benchmark.md), [recon/tools/cuda_sgm_bench.cpp](recon/tools/cuda_sgm_bench.cpp) |

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
recon/                    C++23/OpenCV 5 reconstruction engine (stereo -> depth -> PLY)
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

# 3) 3D reconstruction engine (needs OpenCV 5; no scanner)
cmake -S recon -B build/recon -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/recon -j"$(nproc)"
ctest --test-dir build/recon --output-on-failure
./build/recon/recon_cli synthetic --profile <profile.json> --out /tmp/synth --depth 350

# 4) Everything at once
scripts/verify_all.sh
```

To run the **evidence-backed** suites you need the vendor artefacts, which are not
distributed here. Put them under `fixtures/` (or set `EINSTAR_FIXTURES`) as documented in
[analysis/README.md](analysis/README.md); the fixture-dependent tests then run instead of
skipping.

## Calibration workflow and replacement engine

The physical scanner calibration was reverse-engineered end-to-end: the 5×5 capture state
machine, the distance/angle gates, the vendor's per-view PnP + joint refinement, the
CCF/device write-back, and the plate geometry. **The plate is resolved**
([docs/calibration/plate_spec_recovered.md](docs/calibration/plate_spec_recovered.md)):
8×5 markers plus one off-lattice marker = **41 holes (37 small + 4 large)**, built on a
17×14 recognition lattice at **14.00 mm** with markers on every second node, so the
**marker pitch is 28.0 mm**; the board frame origin is the marker-field centre and the
plate outline is 8×6 marker pitches = **224×168 mm**. `200x150` is a profile/directory
name, not a plate dimension; the manual's 8×7 drawing is a shared cross-product
illustration, and the official SHINING 3D video shows the physical 8×5 board. The vendor
CCF written on the same corpus implies an effective **27.72 mm** — a 1 % solver-scale
difference that remains **visible and unresolved**. Pitch is a pure scale gauge: a 1 %
error leaves the reprojection RMS at the noise floor while moving the baseline by 1.58 mm,
so no residual or covariance test can settle it.

`calib/` is a Linux-native replacement: sub-pixel marker detection, joint robust bundle
adjustment over three cameras, marginal robust covariance (synthetic NEES 0.91–1.02 × n),
Fisher-information view selection and conservative coverage/held-out stopping, and
versioned host-side profiles that preserve the factory snapshot and never write the
scanner implicitly. The original 25-step sequence is redundant for metric scale (3 views
already recover the baseline to 0.03 mm) but not for intrinsics (≥20 views to approach the
vendor parameters).

Round 2 replaces the fixed procedure with an adaptive, operator-guided one
([design](docs/calibration/calibration_system_design.md)): coverage-first bootstrap →
**task-space A-optimal** next-view guidance (3-D covariance over the 200–600 mm volume,
not parameter covariance) → dwell/blur/sync-gated capture with two-stage localisation
(`centroid_bg` live, `auto`/conic on the committed dwell frame) → LM joint solve
(**0.23 s** vs 6.1 s for the old engine, identical optimum) → saturation stopping →
independent verification (held-out, split-half, epipolar, tilt-slope). In a 10-trial Monte
Carlo the task-space planner reaches **0.205 mm** median model-induced 3-D error at 25
views vs **0.238 mm** for EXStar's actual kc-fixed 5×5 plan (6/10 paired, not significant
at n=10) and **0.380 mm** when that plan is forced to free the 5-term distortion on its
clustered poses; the distortion model dominates — k1k2 with adaptive selection is
**0.600 mm** ([strategy evaluation](docs/calibration/strategy_evaluation.md)). The localiser benchmark
([report](docs/calibration/localization_evaluation.md)) selects `auto`/`conic_perspective`
(0.247 px leave-one-out vs 0.281 px for the centroid) and finds the measured marker radius
is 1.77 mm, so perspective eccentricity on this plate is only 0.004–0.016 px. The solver
benchmark ([report](docs/calibration/solver_evaluation.md)) documents the backend choice,
covariance calibration (fx/baseline/3-D calibrated; cx and relative rotation mildly
optimistic) and the native-C++/Ceres findings. On the real corpus the fit is 0.329 px RMS,
0.392 px held-out, 0.14 px median epipolar; the same corpus **fails** a one-view-drop
stability gate (z = 12) while synthetic data passes it (z = 0.44) — a model/data bias the
covariance cannot reveal.

**Not claimed:** physical sub-millimetre accuracy, ISO/VDI conformity, or the plate's true
pitch. All accuracy numbers are synthetic or CCF-relative; scale is traceable to the plate
pitch only. A physical validation needs a calibrated sphere bar and flatness artefact
(VDI/VDE 2634-2/-3, ISO 10360-13) and a calibration run on the user's unit
([roadmap](docs/roadmap.md)).

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
