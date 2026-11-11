# `tools/live_recon` — measure a real live-captured sweep

Phase-5 measurement harness.  It drives the **existing** C++ reconstruction CLI
(`build/recon/recon_cli`) over a real three-camera Einstar sweep, evaluates each
reconstructed plate surface against the known calibration-plate geometry, and
measures pose-normalised repeatability plus a set of controlled experiments.

Nothing in `recon/`, `calib/` or `driver/` is modified and no stereo code is
re-implemented for the sweep: the point clouds are produced by the production
CLI, and this package only *drives* it and *measures* its output.

## Requirements

* the project venv (`.venv`, numpy / opencv / scipy / matplotlib)
* a built CLI: `cmake -S recon -B build/recon -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build/recon`
* a frame directory `frame_%06d_cam{0,1,2}.pgm` (1280x1024 P5) and a
  calibration profile JSON (e.g. `~/einstar-private/einstar-profiles/factory-e10.json`)

## Usage

```sh
.venv/bin/python tools/live_recon/reconstruct_sweep.py \
    --frames ~/einstar-private/sweep-b/frames \
    --profile ~/einstar-private/einstar-profiles/factory-e10.json \
    --out ~/einstar-private/analysis/recon/sweep-b \
    --groups 0-349 --scale 0.5 --jobs 6
```

Options: `--groups` accepts `all`, `0-349`, `0,5,9` or `0-349:5`;
`--skip-experiments`, `--skip-first-capture`, `--skip-replica`,
`--selftest-only`, `--subset-pairs`, `--subset-extra`, `--clip-levels`,
`--recon-cli`, `--jobs`.

Run time on the reference machine (32 vCPU, release CLI):
**≈ 300 s** for 350 groups at scale 0.5 including all experiments, first-capture
comparison, 7 figures and the report.  Per-group `recon_cli` internal time is
≈ 0.17 s; the rest is process start-up, map I/O and the experiments.

## What it produces (in `--out`)

| file | content |
|---|---|
| `recon_report.json` | every measured number, per group and aggregated, with provenance (profile id, CLI sha256, frame hashes) |
| `SUMMARY.md` | the rendered report: headline, method/independence, sweep tables, bias/scale analysis, repeatability, experiment table, comparison, limitations, C++ change requests |
| `figures/valid_fraction.png` | valid depth fraction over the sweep, whole frame and inside the plate outline, with the previous capture's 17.3 % reference |
| `figures/depth_quality.png` | depth percentiles, PnP plate range, and the per-group depth bias |
| `figures/plane_rms.png` | plate planarity, planarity vs range, surface tilt vs the PnP plane |
| `figures/repeatability_pose.png` | NN repeatability vs pose difference, p95 histogram, depth-bias consistency |
| `figures/experiments_coverage_vs_rms.png` | coverage vs verified planarity for every experiment |
| `figures/cloud_plate.png` | plate-frame point cloud (matplotlib 2-D + 3-D) of one representative group |
| `figures/example_maps.png` | rectified pair, disparity, depth and confidence, cropped around the plate |
| `groups/gNNNNNN/` | full `recon_cli` outputs (PLY, rectified pair, disparity/depth/confidence maps, per-frame JSON) for ~12 representative groups |
| `plate_points.npz` | 1 mm voxel plate-frame samples for every group with a detected plate (the repeatability input) |

Total output is a few hundred MB (well under the 2 GB budget); the full 350-group
map set is **not** kept.

## Method

1. **Sweep.** For every requested group, `recon_cli reconstruct` is run (one
   process per group) into a scratch directory.  The harness reads
   `recon_report.json`, `recon_depth_mm.tiff`, `recon_confidence.tiff` and
   `recon_disparity.tiff`, then deletes the scratch outputs except for the
   representative subset.
2. **Depth map → points.** `recon_depth_mm.tiff` stores the depth along the
   **left IR camera z axis**, not the rectified z.  `geometry_py` inverts it
   through the rectification rotation `R1` (the same `R1` `stereoRectify`
   produces in `recon/src/rectify.cpp`).  The inversion is checked against the
   CLI's own PLY in the same run (`recon_report.json → selftest`, max coordinate
   difference ≈ 3e-5 mm).
3. **Plate pose (independent of the matcher).** `calib.detect` finds the 40
   lattice markers on the raw left IR image; `calib.geometry.solve_pnp` fits the
   known board geometry (`calib.plate.board_points_mm`) with the **left
   intrinsics only**.  The same detection is repeated on cam0 (right IR,
   rotated 180°), mapped into the left frame through the factory extrinsics, and
   reported as a pose self-check (the floor on the PnP systematic error).
4. **Plate surface statistics.** Points are transformed into the plate frame.
   Reported per group: plane RMS (thickness, robust 3σ fit), tilt vs the PnP
   normal, signed bias vs the PnP plane, range ratio and the implied plate pitch,
   1 mm-cell surface coverage, density, and image-space coverage/edge/marker
   statistics on the scaled depth grid.
5. **Repeatability.** Legs of the out-and-back sweep are segmented from the
   translation velocity; pose-space nearest neighbours (different legs preferred)
   and large-angle pairs are selected, both clouds are expressed in their own
   plate frame, and symmetric nearest-neighbour distances and plane-offset
   differences are reported.
6. **Experiments** (see below), then figures and the report.

### What is independent, and what is not

* The depth **bias** and **range ratio** compare the PnP range (from the apparent
  size/perspective of the known 196 x 112 mm marker field) with the stereo range
  (from `f·B` and disparity).  These are different data, so a scale or
  relative-orientation error is visible.  They share the left intrinsics, so a
  common focal-length error is invisible to them.
* The **plate pitch is a gauge**: PnP range ∝ assumed pitch, so the range ratio
  absorbs `pitch_true / 28.0`.  The report quotes the implied pitch and keeps
  the degeneracy explicit.
* **Plane RMS** is measured on the matcher's own smoothed surface; SGBM's
  smoothness prior biases it low, so it bounds planarity, not absolute accuracy.
* **Repeatability** uses one calibration for both clouds: it measures stability,
  not correctness.
* The **cam0 → cam1 pose self-check** is a genuinely independent orientation
  check (different image, different distortion model, factory extrinsics).

## Experiments

One variable at a time, baseline re-run in the same session, every score on the
pose-derived plane rather than on coverage:

| family | knob | path |
|---|---|---|
| exposure | right IR × {0.5, 0.7, 1.4, 2.0} | modified PGMs → real CLI |
| saturation | both IR clipped at the level that reproduces the old capture's 22.2 % saturated pixels, plus 128 and 64 DN | modified PGMs → real CLI |
| disparity window | `--min-depth/--max-depth` {250-600, 300-500, 150-500, 150-1200} | real CLI |
| confidence | keep points with confidence ≥ {0.2 … 0.95} | post-hoc on the CLI maps |
| L/R tolerance | {0.5, 1.5, 2.0, 3.0} px | Python replica (`matcher_py`), validated |
| scale | 0.25 and 1.0 | real CLI |
| preprocessing | `--equalize` (CLAHE), `--block 7`, log / gamma-0.5 / inverse response | real CLI, modified PGMs → real CLI |

The SGBM *speckle* window/range (100 px / 2) is active but not exposed by the
CLI, so it was not varied; `--block 7` is included as the nearest available
matcher-shape knob, and a post-hoc connected-component speckle filter was **not**
measured in this run (it needs either a CLI flag or a post-processing stage —
see `cxx_change_requests`).

The L/R tolerance is the only knob with no CLI flag; `matcher_py` reproduces
`recon/src/stereo.cpp` line by line and is validated against the CLI on the same
frames in every run (`recon_report.json → replica_validation`: mask IoU ≈ 0.998,
|Δdisparity| p50 = 0.0 px, |Δdepth| p50 = 0.0 mm).  Its numbers are labelled as
replica-derived.

For every experiment the **newly accepted** pixels are separated from the
already-accepted ones and tested against the surface the baseline reconstructed
(`new_frac_on_plane`, ±1 mm, only inside the plate outline — a background pixel
has no geometric reference and is counted separately).  A coverage gain with
`new_frac_on_plane` far below the baseline's bulk on-plane fraction is reported
as *not* an accuracy gain.

## Module map

```
reconstruct_sweep.py   CLI / orchestration
common.py              paths, hashing, JSON, percentiles, fast plane fits
geometry_py.py         profile parsing, rectification model, depth→points inversion
plate_eval.py          marker detection, PnP, plate-frame and image-space statistics
recon_runner.py        recon_cli subprocess driver, map readers, frame rewriting, PLY
matcher_py.py          Python replica of stereo.cpp (L/R knob only; validated)
sweep.py               per-group processing, aggregation, voxel plate-frame samples
repeatability.py       leg segmentation, pose-space pairing, NN/plane comparison
experiments.py         experiment definitions and runner
first_capture.py       Nov-2026 capture evaluation + published-number table
figures.py             matplotlib Agg figures
report.py              recon_report.json helpers and SUMMARY.md renderer
```

## Notes / limitations

* The profile `skew` field is an **absolute** `K[0,1]` in pixels (0.0718), not a
  normalised alpha_c; `geometry_py` uses it directly.  A wrong interpretation
  shows up immediately as a 10 px PnP residual instead of 0.6-0.9 px.
* The colour camera is not evaluated: the factory colour record is stale and
  `recon_cli` correctly skips it.
* `numpy.linalg.svd`/`lstsq` on tall matrices is pathologically slow on this
  machine; every fit here uses a 3×3 covariance / normal-equation solve.
* C++ changes that would remove workarounds are listed in
  `recon_report.json → cxx_change_requests` and in `SUMMARY.md`.
