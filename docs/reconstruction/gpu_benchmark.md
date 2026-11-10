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

## Why no CUDA number is reported

* The installed OpenCV 5.0 build has **no CUDA modules** (`cv::cuda` is
  unavailable in C++; the Python build reports 0 CUDA devices).  Merely having
  `nvcc` and an RTX 3080 is not enough.
* OpenCV's CUDA module (when built) provides `StereoBM`, `StereoBeliefPropagation`
  and `StereoConstantSpaceBP`, **not SGBM**.  A CUDA SGBM/SGM port or a
  different GPU matcher is a separate implementation, not a configuration flag.
* Building OpenCV with CUDA 13.4 and benchmarking a *different* GPU matcher
  would not answer "how fast is this pipeline on the GPU" for the current SGBM
  algorithm; it would answer a different quality/latency trade-off.

## What would be benchmarked next

1. Build OpenCV 5 with `WITH_CUDA=ON` (or a standalone CUDA SGM/census
   matcher) and run the same rectified pair at 640x512 and 1280x1024.
2. Compare **quality** (valid fraction, plane RMS, L-R consistency) at equal
   disparity range, not only wall time.
3. Include PCIe upload/download in the end-to-end number; the pair is 2.6 MB
   per frame at 8-bit, so transfer is small relative to matching.
4. Only then decide whether a GPU path is worth the complexity for live
   scanning; the CPU half-resolution path already meets a ~8 fps preview budget.

**Claim boundary:** CPU timings are measured; the OpenCL preprocessing timings
are measured; **no GPU acceleration of this matcher is claimed**.
