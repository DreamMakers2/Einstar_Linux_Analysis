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
this looked like a real reference — and it exposed a genuine bug, but not the
one it appeared to.

**Root cause: the wrong intrinsics.**  The engine's depth map lives on the
*scaled rectified* grid (320 x 256 at `--scale 0.25`), while the profile
carries full-resolution intrinsics (fx 1159, cx 624).  Feeding those to the
depth map puts every pixel left of the principal point, so every reconstructed
ray was nonsense and the solver converged to a near-identity transform.  The
engine now writes the scaled rectified geometry into the report:

```json
"rectified": {"scale": 0.25, "width": 320, "height": 256,
              "f": 289.59, "cx": 159.11, "cy": 128.78,
              "baseline_mm": 158.56, "disp_offset": 48.0}
```

`load_depth` reads that block (and refuses to guess), and with the correct
intrinsics the same pairs measure:

| pair | PnP motion | projective ICP (fixed) | projective (wrong K) |
|---|---|---|---|
| 0→5 | 3.37 mm | 2.39 mm | 0.23 mm |
| 5→10 | 3.32 mm | 2.38 mm | 0.18 mm |
| 10→15 | 3.37 mm | 1.95 mm | 0.09 mm |
| 15→20 | 3.12 mm | 2.18 mm | 0.29 mm |

The remaining gap is **not** tracker error: the plate sits on a turntable and
the background is static, so the scene is not one rigid body and ICP reports a
compromise between the moving plate and the stationary surroundings.  This data
can catch a broken tracker; it cannot rank two working ones.

Status: the bug is fixed and covered by a regression test that reconstructs
fresh frames, asserts the depth map's size and focal length, and bounds the
tracked step to 0.3-1.5x of the plate's PnP motion (a wide bound because of the
non-rigid scene).  The scan session still uses the cloud tracker: projective is
not measurably better on this data, and switching would be a guess.

The episode also exposed a trap in the consistency metric: a pose set that
barely moves scores *better* on "distance to the union of other frames", so
consistency alone would have endorsed a tracker that is nearly frozen.

## Tilt validated against the plate on real frames

The plate is rigid, so its PnP tilt is an independent reference; masking the
depth map to the plate polygon makes the comparison like-for-like.  At full
resolution the tracker agrees:

| pair | plate PnP tilt | projective ICP | difference |
|---|---|---|---|
| 0→5 | 0.56° | 0.43° | 0.13° |
| 5→10 | 0.44° | 0.45° | **0.01°** |
| 10→15 | 0.53° | 0.43° | 0.09° |

Depth noise at scale 1.0 is 0.17 mm over ~40-50 k plate pixels.  The same pairs
at **scale 0.25** (noise 2.3 mm over ~2.5 k pixels) gave tilt errors of 0.09°,
0.87° and 1.15° - noise-dominated, exactly as the plane-fit error model predicts
(`sigma / (extent/2)`): ~1.8° at scale 0.25 against ~0.1° at scale 1.0.

So the tracker's *rotation* is validated on real data to a tenth of a degree at
full resolution, and the resolution dependence is understood rather than
anomalous.  What is **not** validated is in-plane translation: a plane cannot
observe it, and the full-scene tracker only gets it from the background
structure, which has no independent reference here.

This is the first positive tracking validation in several rounds, and it came
from matching the measurement to what the geometry can actually observe.

### The two trackers, measured against the same reference

Five real frames, every eighth, reconstructed at scale 1.0 and masked to the
plate (median absolute tilt error against the plate's PnP pose):

| pair | plate PnP | projective (err) | cloud (err) |
|---|---|---|---|
| 0→8 | 0.80° | 0.83° (0.03) | 0.34° (0.46) |
| 8→16 | 0.74° | 0.81° (0.07) | 0.28° (0.47) |
| 16→24 | 0.62° | 0.54° (0.08) | 0.09° (0.53) |
| 24→32 | 0.03° | 0.04° (0.01) | 0.01° (0.01) |
| **median** | | **0.048°** | **0.464°** |

The cloud path *under-reports* tilt systematically (0.34 against 0.80): 5 mm
voxel downsampling flattens the surface and the trimmed solve shrinks the
rotation.  Projective association keeps the pixels and gets it right.

### Cost decides where each one is used

| scale | reconstruction | projective ICP | depth sigma |
|---|---|---|---|
| 0.25 | 158 ms | 105 ms (33 k px) | 1.70 mm |
| 0.5 | 259 ms | 409 ms (115 k px) | 0.34 mm |
| 1.0 | 1384 ms | 1634 ms (418 k px) | 0.17 mm |

The live loop at scale 0.5 would go from ~4 fps to ~1.3 fps if the projective
path were used on the full depth map (259 + 409 + ~100 ms of fusion).  Its
advantage at that scale is small (0.32° against 0.46° on a masked pair), so the
split is:

* **live capture keeps the cloud tracker** - it fits the frame budget;
* **reprocessing and offline work can use the projective path** at scale >= 0.5,
  where the measured accuracy is much better and time is not the constraint.

`align_projective(..., max_points=N)` subsamples by stride for that tradeoff
(measured: 6 k points give 32 ms at scale 0.5 masked, 0.43° error).

## Synthetic end-to-end resolution: 1.19 mm / 0.269 deg

The idea was to validate tracking with *known* motion: render the plate at two
poses for both IR cameras, reconstruct through the real engine, track, and
compare against the truth.  The first measurement is the resolution of that
chain - two renders of the **same pose** with different noise seeds:

```
same pose, different render noise -> 1.19 mm / 0.269 deg
```

That is the floor.  Against it, the small-motion attempts are inconclusive:

| truth | measured |
|---|---|
| 2.0 mm closer (z) | 3.31 mm / 0.79 deg |
| 1.0 deg tilt | 2.36 mm / 2.03 deg |

The 2 mm case sits inside the floor (2.0 +/- 1.2 mm), so it proves nothing
either way.  For the tilt case the first suspect is the harness, so it was
checked against the renderer directly: detect the plate in the rendered image
and solve its pose (no reconstruction involved).

```
requested tilt 0.0 deg -> PnP 179.95 deg     (the board faces the camera)
requested tilt 1.0 deg -> PnP 178.82 deg     relative 1.12 deg
requested tilt 2.0 deg -> PnP 177.57 deg     relative 2.38 deg
```

So the harness applies the rotation as intended and the renderer honours it.

Re-running the pair gave yet another answer - cloud tracker 0.85 deg,
projective 0.40 deg, against a 1.1 deg reference - so the spread between repeats
is of the same order as the signal.  The cause is the reconstruction on synthetic
renders, not the convention: only ~30 % of pixels get valid depth, and the flat
parts of the rendered plate give the matcher nothing to lock onto, so the
reconstructed surface is speckled.

Conclusion: the synthetic chain can validate motions well above ~1.2 mm and
~0.3 deg, and nothing below.

### Root cause: the synthetic scene is not stereo-consistent

A textured-target change was tried (`SyntheticSpec.surface_texture`: a pattern
generated once on a millimetre grid over the board and sampled by ray-casting
each plate pixel, so both cameras see the same surface).  It renders correctly
and is now in the renderer, but it does **not** make the chain usable, and the
reason is decisive:

```
plate at 420 mm, rendered, reconstructed at scale 1.0
  plate centre pixel            -> depth 0.0 mm (invalid)
  valid-depth percentiles       -> p05 356, p50 709, p95 791 mm
  plate moved 2 mm closer       -> median plate depth changes by -0.11 mm
  plate tilted 1 deg            -> median plate depth changes by -0.02 mm
```

The rendered plate reconstructs as **sparse marker fragments plus noise on the
flat background**, with the plate's interior mostly invalid, and it does not
move in depth when the plate moves.  The synthetic renderer is a
detection/geometry fixture - it produces correct *images* (PnP recovers the
requested pose to 0.1 deg) but not a stereo-consistent *scene*.

That closes the synthetic route for tracking validation for good: no tracker can
be validated against a reconstruction that ignores the motion.  A real
rigid-target sequence is the only honest option, and it needs physical handling.

What this establishes is narrower but useful: the synthetic - render ->
reconstruct -> track - chain resolves about 1.2 mm and 0.27 deg, so any future
validation of sub-millimetre tracking needs better synthetic renders (texture,
sub-pixel disparity ground truth) or a real rigid-target sequence.  Claiming a
tracker validated on motions near that floor would be meaningless.

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
