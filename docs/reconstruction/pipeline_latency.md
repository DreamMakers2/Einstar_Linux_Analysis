# End-to-end pipeline latency — measured

Status: 2026-11-12.  Milestone D of `docs/scanner_roadmap.md`.  What one second
of scanning actually costs today, stage by stage, and where the budget goes.

Machine: i9-14900K, 32 threads, 62 GB RAM, RTX 3080 12 GB.  Capture is fixed at
10.00 groups/s by the trigger, so the live budget is **100 ms per group**.
Numbers are measured on recorded frames, single process, `OMP_NUM_THREADS=4`.

## Per-stage cost

| stage | implementation | measured | notes |
|---|---|---|---|
| rectification | C++/OpenCV 5 | 6 ms | at scale 0.5 |
| stereo matching (SGBM + subpixel + LR) | C++/OpenCV 5 | 107 ms | scale 0.5, 1280×1024 |
| depth conversion | C++ | 7 ms | |
| **reconstruction total** | | **132 ms** (scale 0.5) / **35 ms** (scale 0.25) | 135 802 / 33 011 points |
| tracking (point-to-plane ICP) | Python/numpy | **31 ms** | 10.7k points, 5 mm voxels |
| fusion (TSDF, spherical kernel) | Python/numpy | **105 ms** | 5 mm voxels, 10.9k points |
| fusion, coarser input | Python/numpy | 60 ms | 8 mm input voxels (4.8k points) |
| fusion, offline quality | Python/numpy | 157 ms | 3 mm voxels |
| meshing (marching cubes) | Python/scikit-image | 0.23 s | once per session, not per frame |

## What the budget says

* At scale 0.5 the reconstruction alone is **132 ms**, already over the 100 ms
  budget before tracking or fusion.  It is also the only multi-threaded stage.
* A plausible live configuration is: reconstruction at **scale 0.25** (35 ms) +
  tracking (31 ms) + fusion with 8 mm input at 5 mm voxels (60 ms) =
  **~126 ms**, i.e. about **8 fps** on one core.  That is below the 10 fps
  trigger, so frames would be dropped, not corrupted — the ring keeps the raw
  stream and the session still records everything.
* The offline path (scale 0.5, 2–3 mm fusion) costs ~0.3 s per frame and is
  meant for post-processing, not the live loop.

## Where the time actually goes

The C++ reconstruction is already efficient; the Python stages dominate the
*live* budget because fusion is a per-point, per-kernel-tap loop over numpy.
Two cheap wins were implemented and measured:

1. **Spherical instead of cubic kernel**: radius 3 is 123 taps instead of 343
   (2.8× fewer), measured fusion 5 mm: 224 → 105 ms/frame; 2 mm: 440 → 228 ms.
2. **Coarser input points for the live view**: 8 mm input gives 60 ms/frame at
   the same 5 mm volume (4.8k points instead of 10.9k).

## Confidence filtering: measured, and why it is off by default

The stereo engine already carries a per-pixel confidence map into the point
cloud (the PLY has `confidence` and `sigma_mm` vertex properties).  Filtering by
it was measured on four real frames, plane fitted to the plate:

| threshold | points kept | plate points kept | plane RMS |
|---|---|---|---|
| 0.0 | 100 % | 100 % | 0.293–0.300 mm |
| 0.4 | 61 % | 96 % | **0.259–0.272 mm** |
| 0.5 | 55 % | 95 % | 0.257–0.270 mm |
| 0.6 | 55 % | 95 % | 0.257–0.269 mm |

So a 0.4 threshold buys ~12 % lower single-frame noise for almost no loss of
plate coverage — the discarded points are background and weak matches.

**But it hurts fusion.**  In the scanning application at scale 0.25 a 4-frame
mesh fell from **17 133 to 2 233 vertices** with the filter on, because the
fused surface needs the points and voxel averaging already suppresses
low-confidence contributions.  The flag therefore exists
(`--min-confidence`, exposed by `recon_cli`, the scan CLI and the GUI) and is
**off by default**; 0.4 is documented as the value for single-frame analysis.

## GPU: not the first lever

The CUDA SGM path exists (`recon/tools/cuda_sgm_bench.cpp`) and the vendor
pipeline used CUDA 10.2 fatbins.  But in the current pipeline the matching
kernel is 107 ms of a 126–260 ms frame, and a large part of the remainder is
Python bookkeeping and memory traffic that a faster matcher cannot remove.
A GPU matcher is only worth it after the live path moves fusion and tracking
into C++ (or at least out of per-tap numpy), because then matching becomes the
dominant term again.  The rule from the objective stands: measure the complete
pipeline, including transfers, before adopting CUDA SGM.

## Honest summary

* **Offline reconstruction + tracking + fusion + meshing works** at measured
  speeds: 0.13–0.30 s per frame for the offline path, 0.23 s to mesh.
* **Live at 10 fps is not achieved yet**: the best single-core configuration
  measured here is ~8 fps, and the default quality settings give ~4 fps.
* No GPU measurement is claimed here; the CUDA bench tool has not been run in
  this round.
* Photometric normalisation (`--equalize`) was measured on a real frame and
  makes no difference: plane RMS 0.300 mm without it and 0.302 mm with it, with
  slightly fewer valid points (136 968 vs 139 336).  The IR illumination is
  already uniform enough, so it is not made a default.
* Saturation is negligible on the recorded frames (0.10–0.15 % of pixels
  ≥ 250), so saturation masking is not implemented — it would change nothing
  measurable on this data and would be speculative until a scene with
  highlights is recorded.
* These numbers are for *this* implementation, on recorded frames, one process.
  They are not a claim about the vendor's performance.
