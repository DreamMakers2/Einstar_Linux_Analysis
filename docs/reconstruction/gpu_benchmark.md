# CPU/GPU benchmark and acceleration assessment

Status: 2026-11-11.  Host: RTX 3080 12 GB, NVIDIA driver 620.70, CUDA 13.4,
OpenCV 5.0.0 **without CUDA modules** (the system build has no
`opencv2/cuda*` headers or libraries), OpenCL 3.0 NVIDIA available.

Reproduce:

```bash
cmake --build build/recon -j8
./build/recon/recon_ocl_bench /tmp/einstar_recon/id0
```

The benchmark reads the real rectified pair and measures the stages OpenCV can
dispatch to OpenCL.  The SGBM matcher is included explicitly to show that the
`UMat` call is **not** GPU-accelerated: OpenCV has no OpenCL SGBM kernel, so it
copies the data back and runs the CPU implementation.

## Measured (RTX 3080 / OpenCL, 10 repetitions after warm-up)

| stage | CPU (Mat) | OpenCL (UMat) | note |
|---|---:|---:|---|
| remap 1280x1024 | 0.132 ms | **0.031 ms** | ~4.2x, but absolute saving is 0.1 ms |
| resize 0.5 (INTER_AREA) | 0.019 ms | 0.030 ms | slower on GPU |
| Sobel 3x3 (half res) | 0.129 ms | 0.999 ms | slower on GPU |
| SGBM 640x512, 512 disparities | 8.84 ms | 8.41 ms | UMat path = CPU matching + copies; **no GPU gain** |
| UMat upload 1280x1024 | — | 0.183 ms | |
| UMat download 1280x1024 | — | 0.133 ms | |

End-to-end reconstruction (real frame, half resolution) is **~0.12 s**, with
the matcher and confidence stage dominating.  Even if remap were free, the
saving is below 1 % of the frame time.  A meaningful GPU path therefore needs a
**CUDA disparity implementation**, not OpenCL preprocessing.

## CUDA SGM (measured)

The system OpenCV 5.0 has no CUDA modules, so a separate OpenCV 5.2 build was
made with CUDA 13.4 and `opencv_contrib/modules/cudastereo`
(`WITH_CUDA=ON`, `WITH_HARFBUZZ=OFF`, `CUDA_ARCH_BIN=8.6`).  CUDA `StereoSGM`
implements semi-global matching with a centre-symmetric census cost, i.e. the
same algorithm family as the CPU SGBM.  `cuda_sgm_bench.cpp` compares it with
the CPU SGBM on the same 640x512 rectified pair, same window
`[200,328)` (128 disparities around the 280-460 mm scene range).

| matcher | time | valid | depth p05/50/95 (mm) | largest plane | plane RMS |
|---|---:|---:|---|---:|---:|
| CPU SGBM (single pass) | 5.73 ms | 17.3 % | 280.8 / 330.0 / 366.4 | 78.8 % | 0.347 mm |
| CUDA SGM `MODE_HH` | **3.47 ms** steady (12.2 ms first) | 27.3 % | 285.0 / 344.4 / 459.2 | 67.3 % | 0.430 mm |
| CUDA SGM `MODE_HH4` | **2.64 ms** steady (8.5 ms first) | 32.9 % | — / ~347.5 / — | — | — |

* "Steady" = compute + device→host download after warm-up; "first" also includes
  upload and context work.  The CPU number is a single SGBM pass; the production
  CPU matcher is ~100 ms because it runs two padded passes plus left-right
  consistency, masks and confidence.
* On the 14.8 % of pixels where both are valid, the disparity agreement is
  **0.125 px median / 3.8 px p95**, 94.5 % within 2 px.  The GPU map is not a
  drop-in replacement: it has no LR/occlusion stage or confidence, its depth
  tail is longer (p95 459 mm), and its planar RMS is 24 % worse.  Its higher
  valid fraction is partly unverified matched pixels.
* CUDA `StereoSGM` accepts only 64/128/256 disparities.  This benchmark uses a
  128-disparity window around the observed scene; the production
  `[150,800] mm` range needs ~512 half-res disparities, so a general GPU path
  needs tiling/rescaling or a custom kernel with LR and confidence.  **No
  end-to-end GPU speedup is claimed.**

Build/reproduce (the tool is not part of the normal CMake build):

```bash
git clone --depth 1 -b 5.x https://github.com/opencv/opencv /tmp/opencv-cuda-src
git clone --depth 1 -b 5.x https://github.com/opencv/opencv_contrib /tmp/opencv-contrib
cmake -S /tmp/opencv-cuda-src -B /tmp/opencv-cuda-build -G Ninja \
  -DWITH_CUDA=ON -DCUDA_ARCH_BIN=8.6 -DWITH_HARFBUZZ=OFF \
  -DOPENCV_EXTRA_MODULES_PATH=/tmp/opencv-contrib/modules \
  -DBUILD_LIST=core,imgproc,imgcodecs,stereo,cudastereo,cudaarithm,cudawarping,cudev \
  -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF
cmake --build /tmp/opencv-cuda-build -j"$(nproc)" --target opencv_cudastereo
# then compile recon/tools/cuda_sgm_bench.cpp against that build tree (see the
# file header); run:
/tmp/cuda_sgm_bench --left recon_rectified_left.png --right recon_rectified_right.png \
  --scale 0.5 --min-disp 200 --num-disp 128 --out /tmp/cuda_sgm
```

**Claim boundary:** CPU, OpenCL preprocessing and CUDA SGM timings/quality are
measured as above.  The CUDA matcher is **not integrated** into the production
pipeline; no end-to-end GPU acceleration is claimed.
