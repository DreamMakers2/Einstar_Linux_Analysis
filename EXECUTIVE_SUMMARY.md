# Executive summary — Einstar Linux analysis

**Question:** what would it take to build a native Linux application that scans at least as well
as the vendor software, and where are the real gains?

**Answer in one paragraph.** The device is now well understood: the firmware container, the FX3
firmware, the FPGA (Pango PGL25G), the USB protocol, the updater and the factory calibration
format are all decoded to a byte-verified level, and the vendor's reconstruction pipeline has
been mapped statically. This round also repaired a real defect in our own backend: the stream
framing had been decoded backwards (first- vs last-packet flag, camera bits 0..2 vs 3..5, frame
id per packet), and it passed only because the simulator shared the mistake; the new conformance
vectors are independent of the simulator. The calibration *key* is now quantified rather than
assumed: rotation and distortion are proved, the four intrinsics are bounded to ≤1 px (0.26 mm
in depth at 300 mm), and real captures confirm the operational model. What remains is
**hardware bring-up with ground truth**. On performance, async issuance helps only when the link
is the limit (3.2–3.7× saturated in simulation) and changes nothing at the vendor's 68 ms
cadence; zero-copy roughly halves host CPU. Critically, three full frames per 68 ms need
~58 MB/s, more than USB 2.0 HS carries — so the real link speed is an open, decision-changing
question.

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
| Asynchronous acquisition backend + simulator + replay | **works (simulated)** | `driver/`; 11/11 binaries, 90 cases, 57,450 checks, byte-exact replay, ASan/UBSan/TSan clean |
| Command/control channel (serial, firmware, exposure, gain, trigger, page read) | **works (offline)** | `driver/CONTROL_CHANNEL.md`; 22 cases / 4,379 checks vs a byte-accurate fake FX3 |
| Calibration model + rectification + triangulation | **works (consistency-validated)** | `tools/calib/`; 42/42 checks, round trip at 1e-12 mm, real captures align at ~1 px |
| Offline verification of everything above | **works** | `scripts/verify_all.sh`; 83 Python tests + C++ suite |

## 2. What is incomplete

| Gap | Why it matters | What closes it |
|---|---|---|
| **No hardware session** | Every timing/throughput claim is simulated; flash map, drop behaviour and thermal limits are unconfirmed | Connect the scanner; run `driver/` probe + `ReadData` dumps |
| **Calibration key bounded, not proved** | Rotation and distortion are proved; `fx/fy/cx/cy` remain ±1 px (0.26 mm at 300 mm), and the baseline spread contributes ~2.9 mm | Hardware calibration check on a known artefact |
| **Depth pipeline is offline-only** | Rectification/triangulation are validated on real captures for consistency, but no dense stereo → cloud product exists yet | Phase 3 of [docs/roadmap.md](docs/roadmap.md) |
| **USB 2.0 vs 3.0 unresolved** | At 68 ms with three full frames the device needs ~58 MB/s; USB 2.0 HS carries ~40 MB/s, so either the link is SuperSpeed or the frame set is reduced | Measure negotiated speed and frames per trigger group at bring-up |
| **Vendor algorithms not executable** | Pipeline map is static evidence; parameter values (SGM windows, voxel size) are named but not extracted | Dynamic tracing on a Windows VM, or black-box comparison on hardware |
| **FPGA CRC / frame map** | Only matters for re-generating or patching a bitstream | Not needed for scanning; documented as open |

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
| Async transfers, depth 8–16 | **3.2–3.7×** saturated throughput, but only when the link is the limit; at the vendor cadence the frame rate is unchanged | implemented |
| Zero-copy frame path | **~1.6×** less host CPU at depth 8 (measured wall clock, simulation device) | implemented |
| Bounded queues + explicit backpressure | prevents unbounded latency; makes drop policy a decision, not an accident | implemented |
| Separate recording thread | recording cannot stall acquisition | implemented |
| GPU rectification + SGM | expected to move the per-frame budget from CPU-bound to USB-bound | planned |
| USB 3 link | the device exposes USB 3 descriptors; if the real link is SuperSpeed it removes the bandwidth wall, otherwise the frame set must be reduced | **unverified and decision-critical** — measure at bring-up |

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
