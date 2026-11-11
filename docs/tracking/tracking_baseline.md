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
