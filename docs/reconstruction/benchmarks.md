# Reconstruction benchmarks (CPU reference)

Status: 2026-11-11.  Host: current development machine (not the target
i9-14900K/RTX 3080); absolute times are indicative, relative stage costs are the
useful result.  OpenCV 5.0, Release, `OPENBLAS_NUM_THREADS=1`.  Input: the three
genuine private triplets (1280x1024 x 3), factory profile, SGBM block 5,
P1/P2 factors 8/96, CLAHE on, LR tolerance 1 px.

## Per-frame pipeline

| stage | half resolution (0.5) | full resolution (1.0) |
|---|---:|---:|
| rectify (maps cached per profile) | 5 ms | 5 ms |
| SGBM + LR + confidence | 102 ms | 1 229 ms |
| depth + uncertainty + cloud (uncoloured) | 3 ms | 14 ms |
| colour debayer + projection + sampling | 2 ms | 5 ms |
| **pipeline total** | **122 ms** | **1 262 ms** |
| process wall time (incl. load/write) | 245 ms | 1 411 ms |
| peak RSS | 160 MB | 266 MB |
| points | 56 600 | 222 721 |
| PLY size | 1.13 MB | 4.45 MB |
| median depth | 349.5 mm | 350.4 mm |
| median sigma (random part) | 0.252 mm | 0.127 mm |

Matching dominates (>80 %).  Half resolution is the live-preview setting; full
resolution is an offline/quality setting.  The available GPU paths are
benchmarked in [`gpu_benchmark.md`](gpu_benchmark.md): OpenCL remap is 4x faster
but saves only ~0.1 ms; the matcher has no OpenCL kernel and the installed
OpenCV has no CUDA modules.  No GPU acceleration of this matcher is claimed; a
CUDA SGM/BM port is future work and is not needed for the current preview
budget.

## Repeatability and consistency (half resolution)

| frame | points | z p05/p50/p95 (mm) | largest plane fraction | plane RMS (mm) |
|---|---:|---|---:|---:|
| 0 | 56 600 | 331.6 / 349.5 / 368.6 | 92.1 % | 0.336 |
| 1 | 56 198 | 331.8 / 349.5 / 368.4 | 93.0 % | 0.344 |
| 2 | 56 195 | 331.7 / 349.5 / 368.4 | 92.9 % | 0.345 |

The three frames are nominally different instants of a static scene, but frames
1 and 2 are near-duplicates (mean absolute grey-level difference 1.7), so the
repeatability evidence really rests on **two distinct instants**.  Even so,
median depth and the 5/95 percentiles agree to **0.1 mm**, the plane RMS to
0.01 mm, and the point counts to 0.7 %.  This is repeatability, not accuracy.

## Determinism

Re-running frame 0 produced a byte-identical `recon_cloud.ply`
(`sha256 66e928ad…`), and the report's input hash is stable
(`sha256 89e96a7b…`).

## Test suite

`ctest --test-dir build/recon` → `recon_tests` PASS (4 747 checks, including the
per-pixel `z_mm`/`xyz_mm` frame-consistency loop).  The stereo matcher's own
tests are reported separately by the stereo task.

## Memory and threads

The process is single-threaded; OpenCV's internal parallelism is not used for
SGBM.  At half resolution the working set is ~160 MB, dominated by the
rectified pair, masks, SGBM buffers and the point cloud.  This leaves ample
headroom for acquisition buffers, tracking and fusion on the target machine.
