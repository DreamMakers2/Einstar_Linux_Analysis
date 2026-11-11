# Scanning application — measured behaviour

Status: 2026-11-12.  Milestone E (first increment) of
`docs/scanner_roadmap.md`.  A working scan application: one pipeline, two front
ends (Qt window and command line), both driving the same `ScanSession`.

## What it does

```
recorded / live frames
      → recon_cli (C++ engine)      depth + point cloud
      → tracking baseline           pose of this frame
      → TSDF fusion                 integrate only if the pose is trusted
      → marching cubes              mesh
      → OBJ + JSON report           scan.obj / scan_report.json
```

* `tools/scan/session.py` — `ScanSession` / `ScanConfig` / `FrameStatus`.  This
  is the only implementation of the pipeline; the GUI and the CLI both call it.
* `tools/scan/gui.py` — Qt window: a depth-coloured preview of the accumulating
  cloud, frame/rejected/voxel counters, depth statistics, tracking status and
  quality, scan/pause/stop controls and a log.
* `tools/scan/app.py` — GUI entry point with `--headless` for tests.
* `tools/scan/cli.py` — pure command line for reproducible runs.

**A frame whose tracking fails is never integrated.**  The session counts it,
reports it in the frame status and in the report, and the model does not absorb
the pose.  That is the failure mode that silently corrupts a scan, so it is
tested explicitly.

## Live scanning (physical device)

`--live` starts the capture supervisor (the same single process the calibration
wizard uses), follows the frame files it writes, and scans each new complete
group.  The application never talks to USB directly; `stop()` sends the control
file stop, waits for the process and verifies the safe final state.

Measured on the physical Einstar, 4 frames, scale 0.25, 5 mm voxels:

```
frames 4   rejected 0   vertices 20 725   faces 37 795   elapsed 2.93 s
tracking steps 1.60 / 0.49 / 0.58 mm     rms 1.16 / 1.15 / 1.00 mm
inliers 78 %   condition 81-88           depth p50 409.4 mm  sigma 0.69 mm
capture: stopped, stop_reason control:stop, 17 groups written
after exit: trigger counts 0/0, illuminator 0 %
```

The depth-coloured preview shows the real captured scene: the calibration plate
(blue) and the platform/background (orange) fused into one 100 776-voxel volume.

On the *simulated* backend the plumbing is exercised without a device and the
run reports 1 integrated / 3 rejected: the simulator's frames reconstruct to
fewer points than tracking needs, so they are correctly refused rather than
silently fused.  That case is covered by a test.

## Measured

Scan of 8 recorded frames (`~/einstar-private/sweep-b/frames`, factory profile):

```
frames 8   rejected 0   vertices 22 686   faces 41 968   wall 4.3 s
per frame: reconstruction 0.035 s (scale 0.25) + tracking ~0.03 s + fusion ~0.06 s
```

The same run through the Qt window, headless, 5 frames at scale 0.25:

```
frames 5 (integrated 5, rejected 0)    voxels 88 816 observed
depth p50 398.48 mm  sigma 0.66 mm     points 3 674
tracking ok  step 0.307 mm / 0.053 deg  rms 0.99 mm  inliers 79 %  condition 86.6
elapsed 1.47 s                          mesh 17 520 vertices / 32 022 faces
```

The depth-coloured preview shows the calibration plate (blue) and the platform
background (orange) in the same cloud, which is what the fusion integrates.

## 3-D view and project browser

`tools/scan/view3d.py` renders the mesh or the live preview cloud with orbit
(drag), zoom (wheel) and reset (double click), depth-coloured and shaded by
facet normal.  It is a QPainter rasteriser with a painter's algorithm rather
than OpenGL, deliberately: it must run in the offscreen tests with no GPU
context, it has no driver dependency, and a scan mesh is redrawn only when the
camera moves or the mesh changes — not per video frame.

Opening a project (`--project scan_project.json`, or after a scan finishes)
loads `scan.ply` into the same view.  A view-only headless run renders a project
without starting a scan, which is what the test exercises.

Measured: a 4-frame scan project (17 133 vertices / 31 376 faces) opens and
renders in the window; orbit and zoom change the view matrix.

## Output formats and projects

Every scan writes, in the output directory:

| file | content |
|---|---|
| `scan.obj` | mesh, Wavefront OBJ, millimetres |
| `scan.ply` | mesh, ASCII PLY with vertex/face elements |
| `scan.stl` | mesh, ASCII STL with computed facet normals |
| `scan_report.json` | per-frame status, poses, tracking quality, counters |
| `scan_project.json` | schema `einstar.scan.project/1`: configuration, poses, statuses, mesh paths |

The project is a reopenable record of the scan; `load_project` validates the
schema and `project_clouds_available` says whether the source frames are still
on disk, so a scan can be re-fused at another resolution later without
re-recording.

Measured on a 4-frame run: OBJ 1.07 MB, PLY 1.03 MB, STL 7.9 MB, project
4.8 kB, 17 133 vertices / 31 376 faces in 1.5 s.

## Tests

`tests/test_scan_session.py` — 6 tests, 4 s:

* a real 4-frame scan produces a mesh and a JSON report with per-frame statuses
  and poses;
* the snapshot contract the GUI consumes (counters, depth, cloud, volume);
* **a failed alignment is not integrated** (monkeypatched failure: frame
  rejected, `n_frames` unchanged, `n_rejected` incremented);
* a missing frame is reported and does not kill the run;
* `finish()` is idempotent;
* the Qt window runs headless and writes a snapshot.

## Limits (stated, not hidden)

1. ~~It scans a frames directory, not the USB device directly.~~  **Done**:
   `--live` owns the capture supervisor; the device path is verified above.
2. ~~The preview is a QPainter projection with no orbit.~~  **Done**: a shaded,
   orbitable 3-D view for both the live preview and the mesh.
3. ~~No project save/load.~~  **Done** — `scan_project.json` plus OBJ/PLY/STL
   export and a validated loader.
4. Meshing is a single final pass; there is no incremental meshing, smoothing,
   decimation or normal estimation.
5. The reconstruction is a subprocess per frame (`recon_cli`), which costs a
   process start per frame; an in-process binding is the obvious next step.
6. **No accuracy claim.**  The tests and the numbers above demonstrate that the
   pipeline runs, tracks, fuses and refuses untrusted poses.  Absolute
   dimensional accuracy remains unverified without an external artefact.

## Reproduction

```sh
# command line
.venv/bin/python -m tools.scan.cli \
  --frames ~/einstar-private/sweep-b/frames \
  --profile ~/einstar-private/einstar-profiles/factory-e10.json \
  --out ~/einstar-private/analysis/scan2 --count 8

# Qt window (interactive)
.venv/bin/python tools/scan/app.py \
  --frames ~/einstar-private/sweep-b/frames \
  --profile ~/einstar-private/einstar-profiles/factory-e10.json \
  --out ~/einstar-private/analysis/scan-gui --count 12 --scale 0.25
```
