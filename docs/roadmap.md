# Roadmap: from evidence to a production Linux Einstar application

Status: 2026-11-07. Constraints: scanner currently disconnected; target firmware FX3 V2.10 /
FPGA V3.7 (the final release, see `docs/firmware/official_versions.md`); hardware i9-14900K,
64 GB RAM, RTX 3080 12 GB, NVMe.

Each phase has an explicit **exit gate** that is measurable without the device where possible,
and a hardware-dependent gate where not. Nothing advances on "looks plausible".

---

## Phase 0 — baseline (done)

* Firmware container solved, checksum-verified, ELF generated, 518 functions decompiled.
* Protocol cross-validated (vendor + libeinstar), frame framing and update protocol documented.
* Calibration format decoded and geometrically validated (baseline 158.557 mm,
  all rotations det = 1 to 1e-15).
* Windows architecture and 741-module map complete.
* Async acquisition backend + simulator (see `driver/`).

**Exit gate:** `python3 tests/test_firmware.py` and `ctest` in `driver/` pass; every headline
claim in `README.md` cites a file.

## Phase 1 — hardware bring-up and truth capture (needs the scanner)

1. `driver/` probe: enumerate 3267:0003, claim interface (the kernel already repairs
   `bInterval` to 7 — nothing to patch), run the documented init sequence (`FirmwareVersion`,
   `Serial`, `SensorCount`, resolution, exposure/gain limits, `ReadData(0,6568)`), and record the
   negotiated link speed plus frames per trigger group.
2. Capture ground truth:
   * full flash dumps via `ReadData` at `0x0`, `0x3F0`, `0x40000`, `0x80000`, `0x1C0000`,
     `0x200000` (validates/refutes the flash-layout hypothesis in `docs/firmware/format.md` §7);
   * 10-minute raw stream recording per camera at the vendor settings
     (`TriggerMode {3,1}`, `ScanTriggerPeriod 68000`);
   * exposure/gain sweeps for characterisation (linearity, noise, saturation).
3. Compare our `ReadData(0,6568)` with `<device-dump.bin>`
   (must be byte-identical) and with `res/.../CCF.txt` (must match after key removal).

**Exit gate:** ≥ 1e6 frames captured with 0 unrecovered stalls; flash map confirmed or
corrected; calibration round-trip exact.

## Phase 2 — deterministic replay and a real benchmark

1. Replay the Phase-1 recordings through `SimulatedTransport`; assert byte-identical frame
   reassembly and timestamp monotonicity.
2. Measure: sustained fps at 68 ms trigger, p50/p99 transfer latency, drop rate, CPU per frame,
   memory high-water mark. Publish as `docs/benchmarks/hardware_baseline.md`.
3. Fault injection on recorded data: drop 1 in N packets, stall endpoints, truncate frames —
   measure recovery time.

**Exit gate:** replay reproduces the live capture exactly; failure recovery < 100 ms with no
frame corruption; numbers labelled `mode: hardware`; the USB 2.0 vs 3.0 question in
`docs/benchmarks/offline_benchmarks.md` settled with measurements.

## Phase 3 — calibrated depth (accuracy core)

1. Implement rectification from the decoded CCF (Bouguet) with the *proved* key; unit-test
   against synthetic projections (known 3-D points → both cameras → rectify → triangulate).
2. Stereo matcher v1: SGM/ELF on rectified IR pairs, sub-pixel, left-right consistency,
   confidence from uniqueness + texture. Validate on synthetic speckle and, if available,
   public stereo datasets with known ground truth.
3. Validate against a calibrated artefact (sphere/plane/step gauge) once hardware is available;
   report RMS error, flatness, and scale error against a CMM/known standard.

**Exit gate:** synthetic round-trip error < 0.05 px; measured depth RMS ≤ vendor on the same
artefact (target: < 0.1 mm at 300 mm for the plane test), with uncertainty reported.

## Phase 4 — tracking and fusion

1. Frame-to-frame tracking: projective ICP on depth + RGB features (ORB/SuperPoint), robust
   loss (Huber/Cauchy), constant-velocity motion model.
2. Pose graph: local window + loop closure; Ceres/GTSAM; IMU absent, so rely on geometric
   consistency and scan-path priors.
3. Fusion: TSDF (voxel hashing) or surfel; pose-aware reintegration; per-voxel weight from
   stereo confidence.
4. Offline "maximum quality" mode: all frames, global optimisation, Poisson/marching-cubes
   meshing, texture baking.

**Exit gate:** drift < 0.3 % of path length on a closed loop of a known object; fused mesh
completeness ≥ vendor on the same scan; runtime within 1.5× vendor.

## Phase 5 — performance and productisation

1. CUDA: rectification, SGM/ELF, TSDF integration, ICP kernels; measure against CPU baseline.
2. TensorRT path for a learned stereo/refinement model (only if it beats classical on the
   validation set — no "AI because AI").
3. Rust control plane (C ABI to `driver/`), Qt 6 GUI, project format, export (PLY/OBJ/STL/3MF).
4. Packaging: udev rule for 3267:0003 (no root), AppImage/Flatpak, reproducible builds.

**Exit gate:** sustained 15 fps end-to-end at 1280×1024×3 on the reference machine, GUI
responsive, clean shutdown with no leaked USB transfers; crash-free 8 h soak.

---

## Prioritised risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Calibration key assumptions for `fx/fy/cx/cy` and `T` are hypotheses (R and distortion are proved) | Once hardware is present, compare against the device's unkeyed 22-double records and a known artefact; do not ship until validated |
| R2 | Vendor depth may use a non-standard speckle decoder (not plain stereo) | Phase 3 starts classical; evaluate learned/speckle-specific only against measured ground truth |
| R3 | Image stream has no flow control; drops under load (115 documented timeouts) | async queue + clear_halt recovery + explicit drop accounting (already in `driver/`) |
| R4 | FPGA bitstream format unknown → cannot reflash FPGA independently | not required for scanning; keep vendor update path as the only writer until identified |
| R5 | Firmware is final (V2.10) | no upstream fixes will come; robustness must live in the host |
| R6 | USB3 vs USB2: device exposes both descriptor sets but the Einstar is used at HS | test on a USB3 port at Phase 1; if SS works, re-benchmark (potentially 5-10× headroom) |

## What would change the plan

* A flash dump showing the FPGA bitstream at `0x80000` with a different offset (R1/format §7).
* Evidence that the E10's IR cameras are hardware-synchronised to the RGB camera in a way the
  protocol does not expose (would affect Phase 3/4).
* A working USB3 link (R6) — would shift the bottleneck from USB to GPU and change Phase 5.

## Immediate next actions (ordered)

1. Land the `driver/` backend + simulator and its benchmark table (in progress).
2. Publish the FX3 functional map (in progress) so the flash/FPGA path is fully documented.
3. Identify the FPGA bitstream format (in progress) and record the discriminating test.
4. Extend the calibration decoder to emit a validation report for the *device* blob, not just
   the `.txt` records; wire it into the driver's probe output.
5. Write the hardware bring-up checklist as a runnable script (`driver/tools/probe`).

## Calibration track (2026-11-09)

Status: **offline prototype complete; physical validation blocked on the
disconnected scanner and on a board measurement.**

What is done:

* the EXStar workflow is recovered end-to-end (5×5×3, acceptance gates,
  state machine, real-session timings, compute/write-back);
* the plate geometry and 27.72 mm pitch are recovered from the vendor corpus;
* the vendor numerical core is mapped (`Sn3D_E10_camera_calibration` →
  `FUN_1802dca70` → `FUN_1800e0180` → `TribleCCF`), including the camera model,
  the Ceres PnP stage and the fixed-distortion behaviour;
* `calib/` implements a replacement engine with detection, robust joint bundle
  adjustment, calibrated covariance, Fisher-information view selection and
  versioned profiles, evaluated on the vendor corpus and on synthetic truth.

Exit gates (in order):

1. **Board measurement** (5 min, no scanner): count the holes (8×5 vs 8×7),
   measure the pitch and outline, record the four large-hole coordinates.
   Gate: `PlateSpec` matches the physical board; detector finds ≥ 38/40 holes
   on a real photo.
2. **Scale anchor**: measure a known length (caliper/artefact) and compare with
   the factory baseline. Gate: |scale error| < 0.05 %.
3. **User-unit calibration** (scanner connected): capture 12–25 images,
   calibrate, compare to the factory CCF, run a bootstrap repeatability test.
   Gate: baseline repeatability < 0.05 mm, reprojection < 0.5 px.
4. **Physical accuracy**: scan a calibrated artefact (plane/sphere/step gauge)
   with the factory and replacement CCFs. Gate: no worse than factory on the
   same artefact, with the uncertainty interval reported.
5. **Device write**: only after gates 1–4; the `profiles.export_plan` describes
   the exact 0x39B/0xF21 write, and the factory snapshot is restored on any
   failure.

Do **not** claim a physical accuracy improvement before gate 4. The current
evidence supports relative geometry, covariance calibration, capture-count
reduction and reversibility — not sub-millimetre absolute accuracy.

### Calibration audit follow-up (2026-11-10)

The published engine's covariance/stopping was audited and corrected:

* `rig_covariance` now returns the **marginal** rig block (the old conditional
  formula under-reported fx σ by ~11×); `rescaled` uses `J C Jᵀ` and propagates
  the anchor; the robust sandwich uses `Jᵀdiag((w·r)²)J`.
* Stopping now requires views, distance/tilt/image coverage, per-camera
  observations, Jacobian rank/condition, parameter stability, an independent
  held-out check and covariance thresholds. Covariance alone cannot pass.
* Synthetic NEES/n is 0.91–1.02 after the correction; vendor high-target stop
  fails at 25 views (σfx 0.52 px, held-out 0.455 px), standard-target passes.
* The `8–12 captures` claim is **under re-evaluation** with independent
  train/held-out splits; do not quote a capture count without its target and
  protocol.
* The colour CCF is proved stale/mixed-generation (17.66 mm, 46.6 px); the
  plate is 41 holes (8×5) with a 57-hole 8×7 drawing variant, and the metric
  pitch (27.72 vs 25.0) is unresolved pending calipers.

Next gates: physical hole count/pitch/outline; user-unit calibration with
held-out validation; colour re-calibration with texture enabled; known-artefact
depth test. No device write before those pass.
