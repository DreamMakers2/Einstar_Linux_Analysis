# Real genuine-frame reconstruction report

Status: 2026-11-11.  Data: the three complete genuine Einstar three-camera
triplets in the private capture directory (`frame_00000{0,1,2}_cam{0,1,2}.pgm`,
1280x1024).  No physical ground truth exists for this scene, so every number
below is **consistency, repeatability or plausibility**, not accuracy.

## 1. What was done

1. Loaded the three streams with the hardware mapping: stream 0 = right IR
   rotated 180°, stream 1 = left IR as calibrated, stream 2 = colour rotated
   180°.
2. Loaded the factory calibration profile
   (`factory-e10.json`, id `3d1fd027…`, baseline 158.557 mm, f = 1158.37 px,
   colour status `stale`).
3. Rectified the IR pair (Bouguet, `alpha=-1`, full verged field) and matched
   with SGBM at half resolution (block 5, P1/P2 8/96, search window derived from
   150–800 mm), with left-right consistency and confidence.
4. Converted disparity to metric depth (`Z = f·B/(d−offset)`), rotated the
   points back to the **left IR camera frame**, and wrote a binary PLY plus
   rectified/disparity/depth/confidence maps and a JSON report.
5. Colour registration was **skipped**: the factory profile marks the shipped
   Tex record `stale` (the known 17.66 mm inconsistency).  The report records
   `color_status: profile-inconsistent (stale)`; geometry is unaffected.
6. Evaluated a plane (RANSAC), point spacing, repeatability across the three
   frames, confidence bins and parameter ablations.

## 2. Per-frame results (half resolution, default parameters)

| frame | valid | points | depth p05/50/p95 (mm) | σ p50 (mm) | largest plane | plane RMS (mm) | points on plane |
|---|---:|---:|---|---:|---:|---:|---:|
| 0 | 17.3 % | 56 600 | 331.6 / 349.5 / 368.6 | 0.252 | 52 120 | 0.336 | 92.1 % |
| 1 | 17.2 % | 56 198 | 331.8 / 349.5 / 368.4 | 0.252 | 52 237 | 0.344 | 93.0 % |
| 2 | 17.1 % | 56 195 | 331.7 / 349.5 / 368.4 | 0.252 | 52 208 | 0.345 | 92.9 % |

* Median nearest-neighbour spacing: **0.57 mm** (p95 0.66 mm).
* Repeatability: median depth and p05/p95 agree across frames to **0.1 mm**;
  point counts to 0.7 %; plane RMS to 0.01 mm.  The three frames are different
  instants of a static scene.
* Confidence calibration (local 8-neighbour roughness by confidence quartile):
  Q1 **0.85 mm**, Q2 0.71, Q3 0.70, Q4 **0.70 mm** — the lowest-confidence
  quartile is measurably rougher; the upper three quartiles are close because
  most accepted pixels have confidence ≈ 1.
* Determinism: re-running frame 0 produced a byte-identical PLY
  (`sha256 66e928ad…`).

The 92–93 % planar fraction is expected: the captured scene is dominated by a
large flat surface (box/desk).  The residual 0.34 mm RMS includes depth noise,
the surface's own roughness and the fact that the plane is not perfectly flat.
It is **not** a measurement of the scanner's accuracy.

## 3. Ablations (frame 0)

| scale | equalize | block | valid | points | median depth (mm) | total time |
|---:|---:|---:|---:|---:|---:|---:|
| 0.5 | no | 5 | 17.3 % | 56 600 | 349.5 | 0.123 s |
| 0.5 | no | 7 | 17.5 % | 57 276 | 349.1 | 0.124 s |
| 0.5 | yes | 5 | 17.3 % | 56 682 | 349.9 | 0.125 s |
| 0.5 | yes | 7 | 17.6 % | 57 531 | 349.8 | 0.132 s |
| 1.0 | no | 5 | 17.0 % | 222 721 | 350.4 | 1.341 s |
| 1.0 | no | 7 | 17.5 % | 229 012 | 350.1 | 1.223 s |
| 1.0 | yes | 5 | 17.1 % | 223 606 | 350.6 | 1.256 s |
| 1.0 | yes | 7 | 17.4 % | 227 895 | 350.5 | 1.214 s |

The depth median is stable to **0.4 %** across all eight configurations; block 7
gains ~1–3 % coverage; full resolution quadruples the point count at ~10× the
time.  These are the axes that matter for the live/offline modes.

## 4. Why the valid fraction is ~17 %

* The pair is strongly verged (~22°); after rectification only the overlapping
  wedge is valid (the `valid_left/right` masks).
* The projector speckle is fine and the two IR exposures differ strongly
  (stream 1 is much brighter); saturated regions have no reliable disparity.
* The strict left-right consistency check (±1 px) removes occlusions and
  mismatches; the occluded fraction is ~3 % of the full frame, while the LR
  failure rate among raw SGBM matches is the main reducer.  Only ~58 % of the
  image is in the common rectified wedge, so 17 % overall is **~29 % of the
  matchable overlap**; the earlier 39–41 % figure was raw SGBM validity before
  LR and mask intersection.
* The disparity search window margins remove the censored ends.

Seventeen percent of 1280×1024 at half resolution is **56 000 points per frame**
— a useful cloud, not a failure.  The full-resolution run yields 222 700 points
at the same depth.

## 5. What the colour path does

The shipped Tex CCF is the known stale record: its relative translation differs
from the joint fit by 17.66 mm and the vendor session itself reported
`hasTexture:false`.  The reconstruction profile records `color_status: stale`;
the pipeline therefore **does not apply it**.  The point cloud is uncoloured and
the report states why.  When a replacement profile with a jointly estimated
colour camera is available (`color_status: ok`), the same `compute_depth()` path
samples the colour image through the calibrated colour camera; no geometry code
changes.

## 6. Interpretation

* **Proven here:** the engine loads genuine Einstar stream data, applies the
  correct camera mapping/orientation, rectifies the verged IR pair, produces a
  dense, repeatable, self-consistent metric point cloud, and writes PLY/maps/
  report with calibration provenance.  Synthetic ground truth at 250/350/600 mm
  recovers the plane to ≤0.11 mm, validating the implementation.
* **Not proven:** absolute metric accuracy.  The scene has no calibrated
  artefact; the 1 % plate-pitch scale ambiguity remains; the depth bias between
  250 and 600 mm on *real* data is unknown.
* The 331–369 mm depth range is the actual scanner-to-scene distance for this
  capture (handheld, close), not a calibrated measurement.

## 7. Reproduce

```bash
cmake -S recon -B build/recon -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/recon -j8
.venv/bin/python tools/calib/export_profile.py --source ccf \
  --out <private-inputs>/einstar-profiles/factory-e10.json
for i in 0 1 2; do
  build/recon/recon_cli reconstruct \
    --frames <capture-dir> --id $i \
    --profile <private-inputs>/einstar-profiles/factory-e10.json \
    --out /tmp/einstar_recon/id$i --scale 0.5
done
```

The private frames, profiles and point clouds stay outside the public
repository; only aggregate metrics are published.
