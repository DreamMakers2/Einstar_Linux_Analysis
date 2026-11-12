# Tracking baseline — measured

Status: 2026-11-12.  Milestone B of `docs/scanner_roadmap.md`.  This is the
measured foundation for multi-frame fusion: a frame-to-frame rigid alignment
with an explicit quality signal, not the production tracker.

## What it is

`tools/track/baseline.py`

* reads the reconstruction's point clouds (ASCII/binary PLY),
* voxel-downsamples (default 5 mm) and estimates normals by local PCA oriented
  toward the camera,
* aligns consecutive frames with **robust point-to-plane ICP**
  (trimmed least squares, 80 % of correspondences kept per iteration),
* reports per pair: transform, translation, rotation, inlier count and ratio,
  residual RMS, overlap, **scale-normalised Hessian condition number**,
  runtime, and a boolean `ok` with the reason when it is false.

`track_sequence` composes the chain and reports path length, end-to-end motion,
median step and RMS, and pairs/second.  A pair that fails is reported and never
chained silently.

## Measured on the real one-axis sweep

12 consecutive frames reconstructed with `recon_cli reconstruct`
(`~/einstar-private/sweep-b/frames`, factory profile):

```
frames 12   pairs ok 11/11   wall 0.3 s   32.4 pairs/s
median step   0.915 mm / 0.040 deg
path length  10.02 mm   end-to-end 9.73 mm / 0.378 deg
median RMS   0.49 mm    inliers 80 %
step spread  robust sigma 0.122 mm  (13 % of the median step)
condition    130-150 (scale-normalised)   points 10.4k-11.1k per cloud
median pair runtime 30.8 ms
```

The reconstruction itself costs 0.142 s per frame
(`recon_cli reconstruct`: stereo 0.106 s, depth 0.007 s, 139 336 points,
depth p50 401.5 mm, sigma p50 0.333 mm), so tracking at 31 ms/pair leaves the
10 fps capture budget intact on one core.

Interpretation, stated carefully: the sweep is a one-axis motion of a plate and
the scene includes background structure.  The numbers above are a
**consistency** measure (steps are smooth, the end-to-end path matches the sum
of steps within 3 %), **not** ground-truth tracking accuracy.

## The degeneracy signal (why it matters)

A planar target constrains only three of six degrees of freedom.  An ICP that
reports a confident pose on a plane is guessing the in-plane motion.  The
baseline therefore computes the scale-normalised information matrix
`H = (A/S)^T (A/S)`, with the rotational block divided by the RMS scene radius
so radians and millimetres are comparable, and reports
`condition = lambda_max / lambda_min`.

Threshold calibrated on measured geometry:

| geometry | condition | verdict |
|---|---|---|
| synthetic plane | 1.1e15 | degenerate — rejected |
| synthetic surface, ±12 mm relief | 4.3e2 | well posed |
| synthetic curved cap | 6.2e5 | usable but weak — passes |
| **real sweep clouds** | 130–150 | well posed |

The threshold is `1e6`.  A pair above it is reported as
`degenerate geometry (planar target)` and `ok=False`, so fusion can refuse it
instead of integrating a guessed pose.  This was observed on a *pure* plate
point cloud before the background was included — that is exactly the case a
calibration-only scan would hit.

## Tests

`tests/test_track_baseline.py` — 9 tests, 0.65 s:

* identity alignment (< 0.05 mm / 0.05°);
* recovery of three known transforms (±0.6 mm, ±0.15°, with noise);
* 20 % gross outliers do not move the estimate beyond 1.2 mm;
* a planar target is reported degenerate (`condition > 1e4`, `ok=False`);
* unrelated clouds fail;
* a broken chain is reported rather than chained;
* ASCII PLY round trip.

## Long sequence (100 frames, second recording)

`~/einstar-private/sweep-c/frames`, every fifth frame reconstructed at scale
0.25 (8.3k points per cloud after 5 mm downsampling), tracked end to end:

```
pairs ok 99/99          wall 4.3 s (23 pairs/s)
median step 3.28 mm / 0.124 deg      robust step sigma 0.718 mm
per-pair RMS 1.18 mm     inliers 79 %
camera trajectory planarity: 0.19 mm rms off-plane
path 317.8 mm            end-to-end 20.1 mm / 1.1 deg
```

The path/end-to-end mismatch is the shape of the motion, not a failure: the
platform carries the plate along an arc, so the path accumulates while the
displacement does not.  Rotation per step is under 0.15 deg with alternating
sign — small platform jitter on top of translation.

**Fusion over the same 100 frames** (5 mm voxels, 15 mm truncation):

```
9.4 s total (9.0 s integration, 0.39 s meshing)  = 94 ms/frame
volume 3.28 M voxels, 26 MB, 406 k observed
mesh 126 405 vertices / 245 914 faces
```

### What the planar target can and cannot show

The fused plate surface measures 2.05 mm thick at 10 frames and 4.3 mm at 50-100
frames.  That number is **not** a drift measurement: fusing with every frame
aligned *directly to frame 0* gives the same thickness (2.07 mm at 10 frames,
2.43 mm at 20) as fusing with the chained poses.  A plane cannot reveal
tangential error — and the chained and direct poses differ by a median 6.3 mm,
up to 30.7 mm, over 19 pairs.  So:

* **there is real chain deviation**, up to 31 mm over 19 pairs, in directions the
  plane cannot constrain;
* **surface thickness is the wrong metric for it** on a planar target;
* evaluating drift properly needs a 3-D object (or a measured trajectory
  reference), which is exactly what the roadmap says.

This is why the fusion quality signal must not be inferred from a thin plate
model: a scan of a wall would look perfect while the pose chain slid along it.

## Drift bounding: implemented, measured, and *not* enabled

Because chaining lets error accumulate in directions a plane cannot see,
`track_sequence` gained an optional keyframe mode: every N-th frame becomes a
keyframe, each later frame is also aligned directly to it, and when the direct
pose disagrees with the chained one beyond a threshold the direct pose is used
and the event is recorded.

The mechanics are tested, but the **evidence does not support enabling it**.
Two measurements on the long sequence:

| metric (20 frames) | chain only | direct to frame 0 | keyframe every 8 |
|---|---|---|---|
| cross-frame surface consistency (median) | **1.10 mm** | 1.27 mm | 1.14 mm |

The cross-frame metric transforms every frame into the reference frame and
measures each point's distance to the union of the other frames — unlike
thickness, it *is* sensitive to tangential error.  The plain chain wins: the
keyframe path makes larger-baseline ICP calls, which are individually less
accurate than a chain of small steps over this length.

On 60 frames the divergence between a chain and direct-to-reference poses grows
to a median 68 mm (max 119 mm), and keyframe re-anchoring reduces it (16-20 mm
median, 27-31 mm max) — but divergence from a reference that is itself
estimated is not ground truth, so that is not evidence that the re-anchored
poses are more accurate.

Conclusion: **keep the chain as the default**, keep the keyframe mode available
and documented, and stop short of claiming an improvement the data does not
show.  Deciding between them needs a 3-D object with a measured trajectory —
the same missing reference as everywhere else in this project.

## Projective association: implemented, and it fails its own check

`tools/track/projective.py` implements the standard organised-depth variant:
a source point projects to a *pixel*, so the correspondence is a lookup, the
target normal comes from adjacent depth samples, and the step is vectorised.
The engine already writes `recon_depth_mm.tiff`, so the input exists.

It looked promising — on six frames it gave a cross-frame consistency of
0.25 mm against 0.83 mm for the cloud chain, and per-pair RMS 0.37-0.46 mm.  It
was **not** integrated, because an independent check contradicted that:

| pair | plate PnP motion (rigid-reference truth) | projective ICP | cloud chain |
|---|---|---|---|
| 0→5 | 3.37 mm | 0.23 mm | ~1.6 mm |
| 5→10 | 3.32 mm | 0.18 mm | — |
| 10→15 | 3.37 mm | 0.09 mm | — |
| 15→20 | 3.12 mm | 0.29 mm | — |

The plate is a rigid target whose motion is measurable from its own markers, so
this is a real reference.  The projective path reports a tenth of the true
motion: it is under-estimating, not merely imprecise.

It also exposes a trap in the consistency metric: a pose set that barely moves
scores *better* on "distance to the union of other frames", so consistency alone
would have endorsed a tracker that is nearly frozen.  That metric is fine for
comparing equally-moving pose sets, not for validating one on its own.

Status: the module and its unit tests stay (synthetic recovery, normals,
degeneracy, consistency), the independent check is kept as a **strict xfail**
so the failure is visible in every test run and the test will fail loudly when
someone "fixes" it by loosening it.  The scan session keeps the cloud tracker,
and `ScanConfig.tracking` documents why.

## What is still missing for the production tracker

1. **Projective association on the organised depth map** instead of KD-tree
   nearest neighbours: cheaper and better conditioned, and it removes the
   dependence on voxel size.
2. **Motion prediction** (constant-velocity or IMU-free constant-warp) so the
   search is centred and larger inter-frame motion is tolerated.
3. **Keyframes and relocalisation** — the baseline chains every frame; the
   production tracker should keep keyframes and re-localise after a failure.
4. **RGB assistance** — to be measured, not assumed better than geometry.
5. **Ground-truth trajectory** — a turntable with a known angle or a measured
   rail is required before any drift-per-metre claim.  The one-axis sweep
   cannot provide it.

## Reproduction

```sh
# reconstruct 12 frames (0.14 s each)
for i in $(seq 0 11); do
  ./build/recon/recon_cli reconstruct \
    --frames ~/einstar-private/sweep-b/frames --id $i \
    --profile ~/einstar-private/einstar-profiles/factory-e10.json \
    --out ~/einstar-private/analysis/track/clouds/$i --ascii-ply
done
# align them
.venv/bin/python -m tools.track.baseline \
  --clouds ~/einstar-private/analysis/track/clouds/*/recon_cloud.ply \
  --out ~/einstar-private/analysis/track/report12.json --json
```
