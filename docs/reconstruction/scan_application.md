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

1. It scans a **frames directory**, not the USB device directly.  Live scanning
   uses the existing capture tool writing into a session directory, exactly as
   the calibration wizard does; wiring the capture supervisor into this window
   is the next increment.
2. The preview is a QPainter projection, not a rendered 3-D view: no shading, no
   normals, no camera orbit.
3. There is no project save/load yet — a scan leaves `scan.obj` and
   `scan_report.json` in the output directory.
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
