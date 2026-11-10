# Acquisition-to-reconstruction architecture

Status: 2026-11-11.  Implementation: `recon/` (C++23 + OpenCV 5).  This is the
**implemented first stage**; the broader target design (Rust control plane,
CUDA backends, tracking, fusion, Qt) remains `modern_pipeline_design.md`.  This document
is the contract between acquisition, calibration and reconstruction; the Qt6
frontend, tracking and fusion are future consumers of the same backend.

## 1. One data path for live and replay

```
 USB transport ─┐
 simulator ─────┼─► packets ─► reassembly ─► FrameSet (3 streams + group id)
 recording ─────┘                                   │
                                                   ▼
                                   CalibrationProfile (JSON, SHA-256 id)
                                                   │
                     ┌─────────────────────────────┴──────────────────────┐
                     ▼                                                    ▼
             rectify()  ──►  compute_stereo()  ──►  compute_depth()  ──►  make_cloud()
                     │            │                    │                     │
                 valid masks  disparity/confidence  Z/σ/XYZ/colour        PLY
                     └─────────────────────────────┬──────────────────────┘
                                                   ▼
                         write_outputs(): maps, preview, report.json, PLY
```

There is no separate "live" geometry path.  The capture tool writes the same
`frame_%06d_cam{0,1,2}.pgm` + manifest that offline replay writes, and both are
loaded by `load_frame_set()`/`load_frame_set_manifest()` into the same
`FrameSet`.  Everything downstream is a pure function of `FrameSet` +
`CalibrationProfile` + options.

## 2. Data model

| type | content | invariants |
|---|---|---|
| `Frame` | one raw USB stream image (`CV_8UC1`), stream index, size, host time, device frame id, source path | raw stream orientation, unmodified pixels |
| `FrameSet` | three `Frame`s, trigger-group id, source string | all streams present, same size; group id from the device's group counter |
| `CalibrationProfile` | 3 cameras: `fx,fy,cx,cy,skew,dist[5],R[9],T[3]`, image size, units, pitch, `kind`, `color_status`, provenance, SHA-256 `id` | orthonormal rotations, det=+1, finite values, positive fx/fy, plausible IR baseline; colour warnings do not block geometry |
| `RectifiedPair` | rectified left/right, valid masks, `f,cx,cy,baseline,disp_offset`, `R1,R2,P1,P2,Q` | full resolution; masks eroded by the matching window |
| `StereoResult` | disparity, confidence, valid, occluded, scaled `StereoGeometry`, stats | disparity in scaled pixels; `d = f·B/Z + offset` |
| `DepthResult` | `z_mm`, `sigma_mm`, `sigma_total_mm`, confidence, validity, `xyz_mm` (left IR frame), optional BGR | 0 = invalid; sigma separates random and scale terms |
| `PointCloud` | points, optional colours, confidence, sigma | left IR camera frame, millimetres |
| `ReconResult` | all of the above + `ReconStats` | embeds profile id/kind and input hashes |

Frame naming and frame conventions are documented in `frame_set.hpp` and
`profile.hpp`; the key rules are repeated here because they caused the original
real-data failure:

* USB stream 0 = **right IR**, rotated 180° to calibration orientation;
* stream 1 = **left IR**, already in calibration orientation;
* stream 2 = **colour**, rotated 180°;
* the 8-byte header counter is a **trigger-group counter**, not a timestamp;
  the device frame id is valid only on the last packet of a group.

## 3. Coordinate frames

| frame | definition | where it appears |
|---|---|---|
| world | left IR camera at the calibration reference pose | calibration profile `R,T` (each camera maps world -> camera) |
| left IR camera | `x` right, `y` down, `z` forward (OpenCV) | profile frame; output point cloud |
| rectified left | `R1` applied to the left camera; baseline along +x | disparity/depth maps, `StereoGeometry` |
| colour camera | its own OpenCV frame | colour sampling only |

The rectified frame differs from the left camera frame by the rotation `R1`.
`compute_depth()` rotates every 3-D point back with `R1^T`, so the exported cloud
is in the **left IR camera frame** and is directly comparable with the
calibration profile.  Every PLY header records `frame_name=ir_left` and
`units=mm`.

## 4. Calibration profiles and scale

Two profiles are selectable and interchangeable:

* **factory** — exported from the recovered CCF (CCF -> `R,T` per camera, world
  = left IR).  The shipped colour record is marked `stale`; colour is skipped by
  default.
* **replacement** — exported from a `calib/` joint optimization result
  (`tools/calib/export_profile.py --result`).  The exporter keeps the result's
  own pitch/scale metadata: it does **not** force the factory baseline and does
  not rewrite 28.0 mm to 27.72 mm.

The nominal plate pitch (28.0 mm) is metadata, not a scale correction.  A 1 %
pitch error is a pure scale gauge: it moves the baseline but leaves the
reprojection residual unchanged.  `sigma_total_mm` therefore adds the scale term
`Z·σ_scale` (default 1 %) separately from the disparity-noise term
`Z²/(fB)·σ_d`, and reports say "accuracy unverified; scale traceable to the plate
pitch only".

## 5. Reconstruction stages

1. **Load** — PGM/PNG/JPEG triplet or JSON manifest; reject missing streams,
   size mismatches and unsupported content.
2. **Validate profile** — strict; a bad rotation/baseline is fatal.
3. **Rectify** — `cv::stereoRectify` Bouguet, `alpha=-1` (keep the full verged
   field), `CALIB_ZERO_DISPARITY`; maps + valid masks eroded by the matching
   window.  The 22° vergence makes `alpha=0` zoom to ~1.9× focal length.
4. **Match** — SGBM at `scale` (default 0.5) with a disparity window derived from
   the metric depth range, padded search (SGBM cannot search negative
   disparities), optional CLAHE, 1/16 px output, left-right consistency,
   occlusion mask, confidence.
5. **Depth** — `Z = f·B/(d-offset)`, `X=(u-cx)Z/f`, `Y=(v-cy)Z/f`, rotate by
   `R1^T`; per-pixel `σ_random` and `σ_total`; optional colour sampling through
   the colour camera with distortion.  Stream 2 is raw Bayer: it is debayered
   with `COLOR_BayerRG2BGR` before the 180 deg rotation, matching libeinstar;
   no white balance or colour-correction matrix is applied, and the shipped
   factory colour record is not trusted.
6. **Cloud + outputs** — confidence-filtered points; PLY (ASCII/binary LE) with
   calibration id/kind, source, frame, units and plate pitch; rectified pair,
   disparity/depth/confidence PNGs and float TIFFs, invalid mask, z-buffered
   preview, JSON report with timings and stats.

## 6. Provenance and determinism

* `ReconStats.frame_sha256` hashes the three raw stream images (stream order).
* `ReconStats.profile_id` is the SHA-256 of the canonical profile (geometry +
  size + pitch + kind + colour status), independent of comments.
* `report.json` records both, the source, the frame group, stage timings and
  validity/depth/σ statistics.
* No randomness in the pipeline; identical inputs produce identical bytes
  (verified by a repeated-run test).  PNG/TIFF encoders are deterministic for
  the same OpenCV build; the PLY writer is fully deterministic.

## 7. Performance targets and measurements

Target workstation: i9-14900K, RTX 3080 12 GB, 64 GB.  CPU reference first:

| stage | expected | measured (to be filled by the benchmark report) |
|---|---|---|
| load 3 × 1.3 MP | ~10 ms | |
| rectify | ~5 ms | |
| SGBM half-res | ~30–60 ms | |
| depth + cloud | ~10 ms | |
| PLY + maps + preview | ~30–80 ms | |

GPU work (CUDA SGBM/WLS, ICP, TSDF) is a later optimisation; the CPU reference
must meet the live preview budget before any GPU path is added.

## 8. Extension points

* **Tracking** — `ReconResult` already carries timestamps, frame ids, the exact
  calibration id and a confidence-weighted cloud; a future frame-to-frame pose
  estimator consumes `DepthResult` + `PointCloud` without changing the matcher.
* **Fusion** — confidence/σ per point are designed for TSDF/surfel weighting.
* **Qt6** — the backend exposes plain structs and functions; the future
  frontend calls `reconstruct()` and `write_outputs()`, never geometry code.
* **Calibration recorder** — feeds the same `CalibrationProfile` schema; a
  replacement profile becomes selectable without touching the reconstruction.

## 9. Deliberate non-goals

* No application framework in this phase.
* No USB3 hardware modification.
* No device-side calibration write.
* No claim of absolute accuracy without an artefact.
