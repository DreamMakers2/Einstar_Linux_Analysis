# Executive summary — Einstar Linux analysis

**Question:** what would it take to build a native Linux application that scans at least as well
as the vendor software, and where are the real gains?

**Answer in one paragraph.** The device is now well understood: the firmware container, the FX3
firmware, the FPGA (Pango PGL25G), the USB protocol, the updater and the factory calibration
format are all decoded to a byte-verified level, and the vendor's reconstruction pipeline has
been mapped statically. Two things are still missing before accuracy work can start: the scanner
is not attached, and the calibration *key* is proved only for the rotation and distortion
blocks. The highest-value next step is therefore **hardware bring-up with ground-truth
capture**, not more static analysis. On the performance side, the new asynchronous backend
already shows a **3.7× throughput gain** over the synchronous pattern in simulation
(65.5 → 245 MB/s saturated) and a **1.6× host-CPU reduction** from zero-copy, with no hardware
required to reproduce those numbers.

---

## 1. What works now

| Capability | State | Evidence |
|---|---|---|
| Decode any official firmware `.img` (V1.0 and V2.10, EN/CH) | **works** | `tools/firmware/fx3img.py`, 320/320 checksums, verified image checksum |
| Regenerate an analysable ARM image for Ghidra/IDA | **works** | ELF32 emitter; 518 functions recovered |
| Compare firmware versions | **works** | `tools/firmware/compare_fw.py`; V1.0→V2.10 report |
| Decode the FPGA bitstream structure | **works** | `tools/fpga/fpga_bits.py`; 189 packets, zero residue |
| Decode CCF calibration records and validate geometry | **works** | `tools/ccf/`; 40 invariant checks, 18 tests |
| Model the command/stream/update protocol offline | **works** | `tools/protocol/einstar_proto.py`; 12 tests |
| Asynchronous acquisition backend + simulator + replay | **works (simulated)** | `driver/`; 8/8 test binaries, 44,434 checks, byte-exact replay |
| Offline verification of everything above | **works** | `scripts/verify_all.sh`; 26 passed, 31 fixture-skipped |

## 2. What is incomplete

| Gap | Why it matters | What closes it |
|---|---|---|
| **No hardware session** | Every timing/throughput claim is simulated; flash map, drop behaviour and thermal limits are unconfirmed | Connect the scanner; run `driver/` probe + `ReadData` dumps |
| **Calibration key partly assumed** | Rotation (proved) and distortion (proved) are safe; `fx/fy/cx/cy` and `T` depend on hypotheses that shift the point cloud by up to ~2.5 mm in the rig triangle | Compare with the device's unkeyed 22-double records and a known artefact |
| **Depth pipeline not implemented** | No rectification/stereo yet, so no accuracy claim can be made | Phase 3 of [docs/roadmap.md](docs/roadmap.md) |
| **Vendor algorithms not executable** | Pipeline map is static evidence; parameter values (SGM windows, voxel size) are named but not extracted | Dynamic tracing on a Windows VM, or black-box comparison on hardware |
| **FPGA CRC / frame map** | Only matters for re-generating or patching a bitstream | Not needed for scanning; documented as open |
| **Kernel-side `bInterval` quirk** | libusb cannot patch a descriptor; a udev/usbfs quirk is needed on real Linux | Small kernel/usbfs work at bring-up |

## 3. Where the accuracy comes from

Ranked by expected effect on geometric accuracy, with the evidence behind each:

1. **Calibration-faithful rectification (highest).** The decoded calibration gives a 158.557 mm
   baseline and a 22.138° relative rotation with an epipole at x ≈ 6693 px — the pair is *not*
   rectified. Using the proved key and a Bouguet rectification is a prerequisite for sub-pixel
   matching; getting it wrong dominates every downstream error. The vendor's own numbers are now
   reproducible, so this is engineering, not research.
2. **Sub-pixel, left-right-consistent matching with confidence.** The vendor uses 4-path SGM with
   census 5/7/9 and LR consistency; matching that baseline and then adding inverse-variance
   weighting is the shortest path to parity and then improvement.
3. **Multi-view depth validation instead of single-frame filtering.** Rejecting depth that is
   inconsistent across poses (rather than only within a frame) removes the "double surface"
   artefacts that plague hand-held scans.
4. **Confidence-weighted TSDF fusion.** Weight each voxel update by stereo uncertainty; this is
   the single cheapest change that improves surface smoothness *without* eroding detail.
5. **Pose-graph loop closure.** Long scans drift; the vendor ships SLAM/SfM modules but the
   opportunity is a proper factor graph with robust loss.

## 4. Where the performance comes from

| Lever | Measured/estimated effect | Status |
|---|---|---|
| Async transfers, depth 8–16 | **3.7×** saturated throughput (65.5 → 245 MB/s, simulation) | implemented |
| Zero-copy frame path | **1.6×** less host CPU at depth 8 (13.1 → 8.3 ms/60 frames) | implemented |
| Bounded queues + explicit backpressure | prevents unbounded latency; makes drop policy a decision, not an accident | implemented |
| Separate recording thread | recording cannot stall acquisition | implemented |
| GPU rectification + SGM | expected to move the per-frame budget from CPU-bound to USB-bound | planned |
| USB 3 link | the device exposes USB 3 descriptors but ships at HS; a working SS link could remove the USB bottleneck entirely | **unverified** — test at bring-up |

Caveat: deeper queues improve throughput but worsen tail latency (p99 ≈ 38 ms at depth 16 in the
triggered model). For live scanning, depth 4–8 is the sensible operating point; depth 16 is for
offline capture.

## 5. Recommended plan (next 90 days)

1. **Bring-up (week 1–2).** Probe, init, flash reads at `0x0/0x3F000/0x40000/0x80000/0x1C0000/0x200000`,
   calibration round-trip, 10-minute captures. Gate: flash map confirmed, `ReadData(0,6568)`
   byte-identical to the factory dump.
2. **Replay + hardware benchmark (week 2–3).** Feed captures through the simulator; publish the
   first *hardware* latency/throughput table. Gate: replay byte-exact, recovery < 100 ms.
3. **Rectification + depth v1 (week 3–6).** Bouguet rectification from the decoded calibration,
   SGM/ELF with LR consistency. Gate: synthetic round-trip < 0.05 px; plane RMS on an artefact.
4. **Tracking + fusion (week 6–10).** ICP + pose graph + confidence-weighted TSDF. Gate: closed-loop
   drift < 0.3 % of path.
5. **GPU + productisation (week 10–13).** CUDA kernels, C ABI control plane, Qt 6 shell, packaging.

## 6. Risks that could invalidate parts of the plan

* **The IR calibration key is only partly proved.** If `fx/fy` or `T` offsets are wrong, the first
  point clouds will be biased. Detectable immediately at bring-up by comparing against the
  device's own unkeyed records; do not ship before that check.
* **The vendor may use a non-stereo depth path** (structured-light/speckle decoding rather than
  plain stereo). The pipeline survey shows both branches exist; the Einstar's live branch is not
  proven. Mitigation: compare classical stereo against vendor depth on the same frames early.
* **USB 3 may not be usable.** If it is, the performance ceiling changes completely; if not, the
  HS link (~33 MB/s for 3 cameras at 25 fps) is the hard limit.
* **Firmware is final (V2.10).** No upstream fixes; robustness must live in the host.

## 7. What a reviewer can check without hardware

* `python3 -m pytest -q tests/` — 26 tests, including format round-trips and the protocol model.
* `ctest --test-dir build` — 8 C++ binaries, 44k checks, 10,000-frame stress, byte-exact replay.
* `tools/firmware/fx3img.py` on any official `.img` — must print 320/320 and a valid checksum.
* `tools/ccf/validate_ccf.py` on CCF files — 40 invariants including the uniqueness proof.
* `tools/fpga/fpga_bits.py --info` on the extracted payload — 189 packets, IDCODE `0x00511899`.

Every claim above is traceable through [docs/FINDINGS.md](docs/FINDINGS.md) and
[docs/evidence-index.md](docs/evidence-index.md); open items are in
[docs/blockers.md](docs/blockers.md).
