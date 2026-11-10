# `recon/` — Linux-native Einstar reconstruction engine

C++23 + OpenCV 5 implementation of the acquisition-to-point-cloud path.  One
`reconstruct()` function is used for recorded streams, the driver simulator and
(when the scanner is attached) live frames.  See
[`docs/reconstruction/architecture.md`](../docs/reconstruction/architecture.md)
for the data model and conventions and
[`docs/reconstruction/audit.md`](../docs/reconstruction/audit.md) for the
integration audit.

## Build

```bash
cmake -S recon -B build/recon -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/recon -j8
ctest --test-dir build/recon --output-on-failure
```

OpenCV 5 is required (`find_package(OpenCV 5)`); on this host it lives in
`/usr/include/opencv5` and `/usr/lib/cmake/opencv5`.

## Calibration profiles

The backend consumes only JSON profiles (schema `einstar.recon.profile/1`).
Export the factory profile (private, never committed):

```bash
.venv/bin/python tools/calib/export_profile.py --source ccf \
  --out <private-inputs>/einstar-profiles/factory-e10.json
```

A profile from the calibration engine:

```bash
.venv/bin/python tools/calib/export_profile.py --result calibration_result.json \
  --kind replacement --out replacement.json
```

Profiles are interchangeable and carry a SHA-256 `id`; the factory profile marks
the shipped colour record `stale`, so colour registration is skipped by default
with an explicit status.  Geometry never depends on the colour profile.

## Commands

```bash
# validate a profile and print its geometry
build/recon/recon_cli profile --profile factory-e10.json

# list complete three-camera groups in a directory
build/recon/recon_cli list --frames /path/to/frames

# rectification only
build/recon/recon_cli rectify --frames /path/to/frames --id 0 \
  --profile factory-e10.json --out /tmp/rect

# full reconstruction (PLY + maps + report + preview)
build/recon/recon_cli reconstruct --frames /path/to/frames --id 0 \
  --profile factory-e10.json --out /tmp/recon --scale 0.5

# synthetic ground-truth demo (no data needed)
build/recon/recon_cli synthetic --profile factory-e10.json --out /tmp/synth \
  --depth 350

# benchmark several groups / repeats
build/recon/recon_cli bench --frames /path/to/frames --ids 0,1,2 \
  --profile factory-e10.json --repeat 3
```

`--scale` is the matching resolution (0.5 = half resolution); `--min-depth` and
`--max-depth` define the disparity window; `--equalize` enables CLAHE;
`--no-color` disables colour sampling; `--ascii-ply` writes an ASCII cloud.

## Outputs

`recon_<prefix>_cloud.ply` (binary little-endian by default; `x y z
[red green blue] confidence sigma_mm`; header comments record units, frame,
calibration id/kind, source and plate pitch), `_rectified_{left,right}.png`,
`_valid_*.png`, `_disparity.png`, `_depth.png`, `_confidence.png`,
`_invalid.png`, `_preview.png`, `_depth_mm.tiff`, `_disparity.tiff`,
`_confidence.tiff`, `_report.json`.

## Status

| item | state |
|---|---|
| profile load/validate/export | implemented, offline-tested |
| stream mapping + 180° orientation | implemented, real-data demonstrated |
| rectification | implemented, offline-tested + real-data |
| SGBM + subpixel + LR consistency + confidence | implemented (`stereo.cpp`), see the stereo report |
| metric depth, uncertainty, point cloud, PLY | implemented, offline-tested + real-data |
| colour registration | implemented, gated on `color_status == "ok"`; the factory profile is stale → skipped |
| live capture | pending hardware; replay/simulator path only |
| absolute accuracy | **unverified** (no artefact) |

## Safety

Read-only inputs; host-side outputs only.  No device writes, no firmware
flashing, no persistent calibration changes.
