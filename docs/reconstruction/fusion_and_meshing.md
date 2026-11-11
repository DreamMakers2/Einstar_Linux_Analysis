# Fusion and meshing — measured

Status: 2026-11-12.  Milestone C of `docs/scanner_roadmap.md`.  Confidence-
weighted TSDF fusion of reconstructed frames with meshing, and what it measures
on analytic surfaces and on real recorded data.

## What it is

`tools/fuse/tsdf.py`

* a dense axis-aligned volume (`make_volume`) with a hard voxel cap and a
  reported memory allocation,
* `integrate`: each frame's points and normals are transformed by the tracking
  pose and folded into the volume with a confidence weight,
* `extract_mesh`: marching cubes (`scikit-image`) on the fused field, with two
  corrections that were needed to make it accurate (below),
* `write_obj` and a CLI: `python -m tools.fuse.tsdf --clouds … --poses … --out m.obj`.

### Two corrections that mattered

1. **Nearest-point signed distance, not tangent-plane splatting.**  Splatting
   every point's tangent plane into the volume (the usual point-cloud trick)
   biases a convex surface inward by up to the splat radius: a 40 mm sphere
   meshed **2.4 mm small**.  Evaluating `s = (v - p_near)·n_near` from the
   *nearest* point is unbiased at the zero crossing by construction.
2. **Band trimming.**  Point-cloud fusion knows nothing beyond the observed
   region, so marching cubes closes an open surface with an invented wall at
   the rim; and the unobserved interior reads as "outside", producing a
   spurious inner shell.  Triangles are now kept only where their vertices lie
   within the measured band of a real surface point, and (optionally) away from
   the observed/unobserved border.  Without this the sphere was 2.4 mm small;
   with it, 0.03 mm.

## Analytic accuracy

Sphere, radius 40 mm at 300 mm, 6000 points with exact normals:

| voxel | vertices | mean radius | σ | p95 \|error\| |
|---|---|---|---|---|
| 3.0 mm | 3 336 | 40.00 | 0.01 | 0.03 mm |
| 2.0 mm | 7 594 | 40.01 | 0.01 | 0.04 mm |
| 1.5 mm | 13 460 | 40.01 | 0.01 | 0.04 mm |

The surface is recovered to a few hundredths of a millimetre, i.e. far below a
voxel, because the signed distance field is exact and the crossing is
interpolated.

A planar patch meshes flat (median 400.0 mm, p95 deviation < 0.5 mm), and the
rim is trimmed rather than sealed.

Pose sensitivity is tested explicitly: fusing the same sphere twice with a
*correct* rotation about its own centre keeps p95 error < 0.4 mm; corrupting
that pose by 6 mm more than doubles the error.  A fusion insensitive to pose
would be worthless, and one that hid a bad pose would corrupt the model.

Weighted averaging is also tested: five noisy observations (σ = 0.5 mm) of one
plane give a mesh no thicker than a single observation.

## Measured on real recorded data

12 frames from the one-axis sweep (`~/einstar-private/sweep-b/frames`),
reconstructed with `recon_cli`, aligned by `tools/track/baseline.py`, fused at
3 mm voxels with 9 mm truncation:

```
frames integrated   12        volume            228 x 224 x 183 = 9.3 M voxels
observed voxels 357 295       memory            74.8 MB
integration       3.67 s      meshing           0.23 s
mesh              75 946 vertices / 140 325 faces
```

**Consistency of the fused surface** (the plate slab, 385–420 mm, 4 036
vertices, plane fitted to the fused mesh):

```
thickness p50 0.60 mm   p95 2.67 mm   (3 mm voxels)
```

Twelve independently posed observations of the same plate overlapped to a
0.60 mm median sheet.  If the tracker poses were wrong the observations would
smear; they do not.  This is a **self-consistency** measure, not accuracy
against a ground-truth object — the scene is a plate plus background, not a
scanned object with known dimensions.

## Tests

`tests/test_fuse_tsdf.py` — 7 tests, 1.3 s: sphere accuracy at the default
voxel, plane flatness, two-view agreement plus deliberate pose corruption,
failed-tracking frames never integrated, the volume cap, OBJ round trip, and
noise averaging.

## What is still missing

1. **True projective integration** from the depth map (KinectFusion-style) would
   also carve free space, which removes the need for band trimming and handles
   sawtooth/occlusion correctly.  The current path consumes point clouds because
   that is what the engine emits; exposing the depth map is the enabling change.
2. **Sparse voxel hashing** instead of a dense volume: at 2 mm over a 0.5 m cube
   the dense grid is already 125 MB, and a whole-room scan needs hashing.
3. **Mesh post-processing**: smoothing, decimation and normal estimation are not
   implemented; the OBJ is the raw marching-cubes output.
4. **A real object scan** is required before any claim that fusion improves
   geometric fidelity — a planar plate cannot demonstrate that.
