# Roadmap: from calibration to a complete Linux scanner

Status: 2026-11-11.  Written after the calibration wizard reached a validated
end-to-end profile.  Each milestone states what already exists, what is
missing, how it will be measured, and the order of work.  Numbers quoted are
from earlier measured rounds and are repeated here only where they constrain
the design.

## What already works

| layer | state |
|---|---|
| USB acquisition | live 10.00 groups/s at 39.4 MB/s, 32 packets/frame, three cameras, no drops over 350-group runs |
| recording / replay | lossless `.e1r`, deterministic replay (byte-identical manifests), frame manifests with device group ids |
| depth reconstruction | CPU stereo on the IR pair with the factory profile; depth maps produced for the sweep |
| calibration | wizard, coverage/observability gates, validated profile, activation + rollback |
| GUI | Qt6 calibration application: previews, guidance, capture, observability, controls |

## Progress (2026-11-12)

| milestone | state |
|---|---|
| B tracking | **done** — `tools/track/baseline.py`, `docs/tracking/tracking_baseline.md` |
| C fusion/meshing | **done** — `tools/fuse/tsdf.py`, `docs/reconstruction/fusion_and_meshing.md` |
| D performance | **measured** — `docs/reconstruction/pipeline_latency.md` (live ~8 fps best, ~4 fps default) |
| E scanning app | **working** — `tools/scan/`: Qt window + CLI, **live capture on the physical device** (4/4 frames, safe stop), TSDF fusion, OBJ/PLY/STL export and reopenable projects; `docs/reconstruction/scan_application.md` |
| A reconstruction | **measured**: SGBM + sub-pixel + LR already present; confidence thresholding measured (0.30 -> 0.27 mm single-frame, off by default for fusion); saturation negligible (0.1 %); equalise no effect.  Colour extrinsic re-fitted 461 px -> 0.34 px held-out (`docs/calibration/colour_registration.md`) |

## A. Better live stereo reconstruction

Existing: CPU matcher on rectified IR pairs with the factory profile.  The
measured constraint is that depth error scales with `z² / (fx · baseline)`, so
fx and the baseline must be right before matching quality matters.

Work items, in order:

1. **Rectification and photometric normalisation.**  Measure epipolar error on
   real pairs (the calibration validator already reports ~0.5 px p95); add
   per-camera gain/offset equalisation before matching.  Success: epipolar p95
   < 0.7 px, matching cost variance reduced.
2. **Sub-pixel disparity.**  Current matching yields integer-ish disparity;
   parabolic or semi-global refinement should cut the quantisation term.  At
   400 mm with fx 1160 and a 158 mm baseline, 1 px of disparity is ≈ 0.87 mm
   of depth, so sub-pixel refinement is worth more than a larger window.
3. **Confidence, not just depth.**  Left-right consistency, uniqueness ratio and
   a local curvature test, each measured against reconstruction error on
   recorded frames.  Every downstream stage (tracking, fusion) gates on this.
4. **Exposure/saturation handling.**  Detect clipped pixels and exclude them
   from matching; the illuminator already has per-camera exposure control.
5. **Colour registration.**  The shipped colour extrinsic is stale (~18 mm,
   ≈49 px).  A charuco/colour-chart session must re-fit it; until then colour
   is advisory.
6. **GPU only after 1–5.**  The rule is end-to-end latency and depth quality,
   not kernel speed: a CUDA SGM that is 10× faster in isolation but needs a
   30 ms round trip per frame is not an improvement.  Benchmark full
   capture→depth→fusion latency, not the matcher alone.

## B. Real-time scanner tracking

No tracker exists yet.  The design that fits the data already collected:

* **Inputs:** previous depth map + confidence, current depth map, optional RGB.
* **Method:** projective point-to-plane ICP on the confident subset, with
  motion prediction from the previous 2–3 poses and coarse-to-fine pyramids.
  Geometric ICP alone is the baseline; RGB-assisted association is an
  experiment to be measured, not assumed better.
* **Robustness:** trimmed least squares (e.g. 70 % inliers), normal-consistency
  rejection, and a hard failure signal.  A failed frame must **not** be
  chained silently: the fusion stage receives `tracking_ok = False` and the UI
  shows "tracking lost".
* **Metrics:** inlier ratio, residual RMS, estimated overlap, drift per metre
  on a known trajectory.  The existing one-axis sweep gives a *consistency*
  measure only (no ground truth); a turntable or a measured rail is needed for
  absolute tracking accuracy.
* **Relocalisation:** on failure, search keyframes by depth histogram /
  feature descriptor before declaring the scan broken.

## C. Multi-frame fusion

* **Representation:** TSDF with confidence weighting is the default (bounded
  memory, simple meshing); surfels are the alternative if fine detail matters
  more than memory.  Decide with a measured comparison on the same recording,
  not by preference.
* **Integration:** weight each voxel update by depth confidence and view angle;
  reject updates with `tracking_ok = False`; keep a per-voxel observation count
  so revisiting a region refines instead of double-counting.
* **Revisiting and loop closure:** pose-graph over keyframes; loop closure only
  when a geometric verification (ICP over overlap) agrees.
* **Output:** mesh extraction (marching cubes) with a confidence mask, then
  decimation and normal estimation.  Export: PLY/OBJ/STL, plus the project
  format (recording + poses + mesh) so a scan can be reprocessed offline.

## D. Performance

Machine: i9-14900K, RTX 3080 12 GB.  Budgets to hold: 10 fps capture is fixed
by the trigger, so the pipeline must process a group in < 100 ms to stay live;
fusion may lag but must not drop frames silently.

Measure and publish: per-stage latency (decode, rectify, match, confidence,
tracking, fusion), peak RSS, and depth error against the recorded reference.
GPU work is accepted only if the *end-to-end* number improves.

## E. Complete GUI

Extend the existing Qt application rather than writing a second one:

1. start screen with device status, profile selection (factory / validated),
   and calibration entry point;
2. live view with depth/confidence overlay and a 3D point-cloud panel;
3. scan controls (start/pause/resume/finish), tracking status, coverage of the
   object, and a warning when tracking is lost;
4. project save/load with the recording, poses and mesh;
5. offline reprocessing and meshing with progress;
6. export dialog.

## Order of work and gates

| step | gate to pass before moving on |
|---|---|
| A1–A3 | depth error and confidence coverage measured on recorded frames |
| B | drift per metre and failure detection measured on a recorded sweep |
| C | fused mesh of a recorded object, compared against a single-frame cloud |
| E | the calibration wizard's workflow unchanged and still passing its tests |

## Honest limitations carried forward

* Absolute dimensional accuracy is unverified without a length artefact; every
  report must say so.
* The plate pitch (28 mm) and the ~1 % scale discrepancy remain unresolved; a
  profile may be geometrically validated and still carry that scale.
* Tracking accuracy cannot be validated on a one-axis sweep alone.
* The colour extrinsic is stale until a colour session is recorded.
