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

## One entry point

`tools/app.py` is the single command a user runs:

```sh
.venv/bin/python tools/app.py --frames ~/einstar-private/sweep-b/frames   # scan a recording
.venv/bin/python tools/app.py --source live                               # calibrate live
```

It opens a small launcher with three actions: the calibration wizard, the
scanner, and a profile panel (factory path, store path, active revision).  Each
action opens its own top-level window rather than reparenting widgets into
tabs: the wizard and the scanner each own a capture process, a session directory
and worker threads, and keeping them separate windows means one cannot stomp on
the other's lifecycle.  Closing the launcher shuts both down.

## Offline reprocessing

A project stores the frames directory, the reconstruction settings and every
accepted pose, so a scan can be **re-fused at another voxel size without
re-tracking**:

```sh
.venv/bin/python -m tools.scan.cli \
  --project ~/einstar-private/analysis/scan-colour2/scan_project.json \
  --out ~/einstar-private/analysis/scan-reproc --voxel-mm 2.0
```

The recorded pose is authoritative on that path and tracking is skipped
entirely; the report marks each frame `tracking_reason: "replayed pose"` so a
reader can tell.  Measured on a 3-frame project made at 5 mm: reprocessing at
2 mm gives **92 281 vertices / 169 062 faces** against 16 462 / 30 083, with the
poses byte-equal to the original scan.  This is the offline refinement path for
a recording, and it is deterministic because the poses are inputs.

## Live mesh preview

The 3-D view updates *during* the scan, not only at the end: every fifth
integrated frame the session extracts the fused surface and decimates it to
about 6 000 faces for display.  Decimation takes every k-th face and remaps the
vertices it still uses, so it is deterministic; the final mesh written by
`finish()` is never decimated.

Measured on a 10-frame scan: the preview showed 20 250 vertices / 36 927 faces
while the run was still going, and the final mesh is larger.

## Tracking path

Scans now track on the engine's depth maps by default
(`ScanConfig.tracking="projective"`): the depth map is restricted to the near
object with a depth-only band mask and aligned projectively.  Measured against
the calibration plate's own PnP pose on real frames, median tilt error is
**0.24 deg at scale 0.5** against **0.58 deg** for the cloud tracker, at
~200 ms/frame of tracking instead of ~76 ms (~13 % of the scan loop).

It falls back to the cloud tracker when a depth map or its rectified intrinsics
are missing, and the worker never aborts on a tracker result: early exits return
a complete result dict and the session reads defensively (a short dict once
raised `KeyError: 'translation_mm'` and aborted a live scan).

**Verified on the physical scanner** (first live run with the projective
default, 8 groups at scale 0.5, colour-fitted profile, `MemoryMax=8G
CPUQuota=300%`):

```
frames 8  rejected 0  vertices 18 059  faces 32 788  colour on
tracking ok  rms 0.04-0.15 mm  inliers 71-81 %  condition 401
elapsed 8.7 s            after exit: trigger counts 0/0, illuminator 0 %
```

The object was stationary, so a near-zero step is the correct answer rather
than a tracking failure — the same run before the projective default reported
0.45-1.6 mm steps on replay data with real motion.

**Live run with the full signal set** (10 groups, scale 0.5, colour-fitted
profile), verifying that novelty, the stop recommendation and the mesh
statistics all work on hardware:

```
frames 10  rejected 0  vertices 15 766  colour on  elapsed 9.2 s
novelty   100, 3.7, 0.76, 0.58, 0.19, 0.18, 0.06, 0.21, 0.00, 0.05 %
coverage  converged (window 5, mean 0.10 %)
mesh      28 674 faces, 44 439 edges, 2 856 boundary (6.4 %), 0 non-manifold
recoveries 0
after exit: trigger counts 0/0, illuminator 0 %
```

The scene was static, so novelty falls to zero and the verdict says converged;
that is the correct reading, not a failure of the signal.

## New-surface feedback

The scan view reports how much of each frame is **new surface** - the fraction
of its points whose voxel had not been observed before (`tsdf.observed_fraction`,
a voxel lookup, not a surface-distance test).  The caption shows it live and the
finished log reports the last frame's value; every frame's value is in
`scan_report.json`.

Measured on a nearly stationary sweep segment: 100 % on the first frame, then
1.7 %, 1.3 %, 0.2 %, 0.4 %, 0.2 % - the signal tells the operator that more
frames are adding nothing.  On widely spaced frames (0, 40, 80, 120) it reads
100 %, 25.2 %, 18.4 %, 1.2 %, tracking how much new surface each view brings.

This is deliberately a *voxel* measure, so it says "you have seen this volume
before", not "your model is complete".

**Stop recommendation.** `ScanSession.coverage_verdict(window=5, threshold=0.02)`
reports `converged` when the last five integrated frames each brought less than
2 % new surface on average.  The bootstrap frame is excluded - every voxel is new
by definition on frame 0, so including its 100 % would keep the verdict at
"adding" forever.  Measured on eight frames of a near-stationary segment:

```
frame  0    1     2     3     4     5     6     7
new  100%  1.7%  1.3%  0.2%  0.4%  0.2%  0.3%  1.0%?
verdict: insufficient ... converged (window 5, mean 0.31 %)
```

The window shows up in the report as `coverage`, and the window caption says
`converged (nothing new for a while)` while the scan is running.

It is a **hint, not a proof**: nothing here checks closure or holes, and the
model may still be missing surfaces the operator never pointed at.  A stricter
threshold keeps it "adding" - which is the honest behaviour and is tested.

### Open-boundary fraction: a model-level hint

Novelty is a volume measure; a scan can stop learning new voxels while the
surface is still wide open.  The report therefore also carries
`mesh_stats`: vertices, faces, edges, **boundary edges** (shared by one triangle),
non-manifold edges (shared by more than two) and the boundary fraction.

Measured on an 8-frame scan of the recorded scene:

```
vertices 23 358   faces 43 257   edges 66 686
boundary edges 3 601 (5.4 %)     non-manifold 0
```

A closed tetrahedron reports 0 boundary edges and a single triangle reports
100 %, so the range is meaningful.  The caveat matters: **an object resting on a
table always has an open boundary where it meets the table**, and a scene scan
includes the room's own edges, so the fraction is only interpretable for a
scanned object in isolation - it is a hint that the surface ends somewhere, not
a closure test.

## Tracking loss and recovery

A frame whose alignment fails is rejected, never integrated, and **the reference
frame stays at the last accepted one**.  That last part was a real bug: the
session used to move its reference to the rejected frame, so one glitch made
every later frame align against a pose that was never trusted - a permanent loss
instead of a recoverable one.  It is now covered by a test that fails the
alignment for one frame, checks the reference did not move, and then checks the
next frame integrates and is reported as `tracking_reason: "recovered"`.

Recovered frames are counted in the report (`recoveries`), so a scan that had to
re-anchor is distinguishable from one that tracked cleanly throughout.

## Determinism

Two independent runs of the same recording with the same settings produce a
**byte-identical** `scan.ply` (SHA-256 equal, verified in the tests).  A scan is
a measurement, not a random process, and the pipeline is deterministic through
reconstruction, tracking and fusion.

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
| `scan.obj` | mesh, Wavefront OBJ, millimetres (extended per-vertex colour when available) |
| `scan.ply` | mesh, ASCII PLY with vertex/face elements and per-vertex RGB when the profile has a validated colour record |
| `scan.stl` | mesh, ASCII STL with computed facet normals |
| `scan_report.json` | per-frame status, poses, tracking quality, counters |
| `scan_project.json` | schema `einstar.scan.project/1`: configuration, poses, statuses, mesh paths |

The project is a reopenable record of the scan; `load_project` validates the
schema and `project_clouds_available` says whether the source frames are still
on disk, so a scan can be re-fused at another resolution later without
re-recording.

Measured on a 4-frame run: OBJ 1.07 MB, PLY 1.03 MB, STL 7.9 MB, project
4.8 kB, 17 133 vertices / 31 376 faces in 1.5 s.

## Colour on the mesh

When the reconstruction profile has a validated colour record (see
`docs/calibration/colour_registration.md`), the engine samples colour for every
valid point; the scan session keeps those colours, colours every mesh vertex
from the nearest input point, and writes them into the PLY (`red/green/blue`)
and the OBJ (`v x y z r g b`, 0-1).  With the factory profile the mesh is
geometry-only, because the engine refuses to sample a record it cannot trust.

Measured on a 3-frame scan with the colour-fitted profile: 16 462 vertices and
30 083 faces exported with colour in both formats.  The values are dark in this
recording because the illuminator is the IR one — geometry is validated, colour
photometry is not (documented in the colour note).

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
