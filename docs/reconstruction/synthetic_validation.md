# Synthetic geometric validation

Status: 2026-11-11.  The synthetic renderer (`recon/src/synthetic.cpp`) draws a
textured fronto-parallel plane at a known depth through the **full camera model**
(intrinsics, Brown-Conrady distortion, each camera's pose), in raw USB stream
orientation.  The pipeline therefore exercises loading, the 180 deg stream
mapping, undistortion, rectification, SGBM, left-right consistency, depth and
PLY exactly as with real frames.

## Plane recovery (factory profile, half resolution)

| truth (mm) | measured p50 (mm) | error (mm) | valid fraction |
|---:|---:|---:|---:|
| 200 | 200.0 | **-0.020** | 24.0 % |
| 250 | 250.0 | **-0.027** | 41.0 % |
| 300 | 300.0 | **-0.038** | 51.6 % |
| 350 | 350.0 | **-0.050** | 58.5 % |
| 400 | 399.9 | **-0.056** | 63.5 % |
| 500 | 499.9 | **-0.083** | 70.3 % |
| 600 | 599.9 | **-0.107** | 74.3 % |
| 700 | 699.9 | **-0.127** | 76.9 % |

Full resolution at 350 mm: measured 350.0 mm, error **-0.014 mm**, coverage
59.2 %, sigma 0.127 mm.  The figure
`figures/synthetic_accuracy.png` plots the error and coverage over 200–700 mm;
the small negative bias grows linearly with depth (consistent with the
sub-pixel disparity bias, not a scale error) and stays below 0.13 mm.  These numbers validate the **implementation of the
geometry**, not physical accuracy: the renderer and the estimator share the same
camera model, so a model error common to both cancels.

```bash
build/recon/recon_cli synthetic \
  --profile <private-inputs>/einstar-profiles/factory-e10.json \
  --out /tmp/synth350 --depth 350 --scale 0.5
```

## Automated tests (`ctest --test-dir build/recon`)

`recon_tests` covers, among others:

* SHA-256 known vectors; profile canonicalisation/id stability and round trip;
* rejection of non-orthonormal rotations, degenerate baselines, wrong image
  size, missing streams;
* hardware stream mapping (0 = right IR rotated, 1 = left IR, 2 = colour);
* projection/triangulation consistency and `f*B/Z` disparity;
* scaled geometry disparity/depth round trip;
* end-to-end synthetic depths at 250/350/600 mm within 0.5 % / 1 mm;
* `z_mm` and `xyz_mm` in the same left-camera frame;
* PLY header/units/vertex count and ASCII/binary round trip;
* unusable input rejection (no silent cloud);
* determinism: two runs of the same input produce an identical PLY hash.

## What this does *not* prove

* absolute metric accuracy (no artefact);
* the real lens model beyond the factory distortion coefficients;
* the plate pitch (a 1 % pitch error is a pure scale and is invisible here);
* behaviour on the scanner's structured-light sequence beyond the recorded
  frames.

The next validation level is the real-data consistency report
(`real_data_report.md`); physical accuracy requires the artefact protocol in
`validation_plan.md`.
