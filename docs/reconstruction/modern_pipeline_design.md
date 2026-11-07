# Modern reconstruction pipeline — architecture, budgets, milestones

Status: design proposal. It is grounded in the vendor evidence in
[`vendor_pipeline_analysis.md`](vendor_pipeline_analysis.md), but **every target number below is an
engineering estimate unless marked measured**. Speculative items are marked **[speculation]**.

Target machine: **i9-14900K (8P+16E, 32 threads), 64 GB DDR5, RTX 3080 12 GB, NVMe**.
Camera: 3 × 1280×1024 global-shutter mono (SC130GS), IR speckle projector, RGB texture camera,
FPGA-triggered. Host OS: Linux. Control plane: Rust. GUI: Qt 6.

---

## 0. Design principles

1. **The frame ABI is the product boundary.** Acquisition (Rust/USB) and reconstruction (C++/CUDA)
   must never share C++ types, STL containers, exceptions, or ownership. One versioned C struct,
   one shared-memory ring, zero copies on the hot path.
2. **Every stage is replaceable and measurable.** Stereo is an interface with at least four
   implementations (SGM/ELF, speckle-specific, learned, phase). Fusion has TSDF and surfel
   implementations. Nothing is hard-wired to one algorithm.
3. **Confidence is a first-class output.** A depth sample without a variance estimate is not allowed
   into fusion. Confidence is produced by the stereo backend and propagated through TSDF weights.
4. **Two quality modes, one code path.** Live mode targets 25 fps at reduced resolution/iterations;
   offline “maximum quality” re-runs the same stages with all frames, full resolution, bundle
   adjustment, Poisson and texture optimisation.
5. **No vendor binary is a dependency.** Calibration and formats are re-derived; the vendor install
   is evidence, not a runtime component.

---

## 1. System architecture

```
┌──────────────────────────── Rust control plane ────────────────────────────┐
│  einstar-device (USB/V4L2, FPGA trigger)   einstar-orchestrator (state)     │
│  frame pool + dmabuf export                scan/project DB (SQLite)         │
│  commands (start/stop/config)              telemetry, logging, crash dumps  │
└───────────────▲───────────────────────────────────────────┬─────────────────┘
                │ C ABI v1 (shared memory ring + events)     │ C ABI v1
┌───────────────┴───────────────────────────────────────────▼─────────────────┐
│                     libeinstar-recon (C++23 / CUDA)                          │
│                                                                              │
│  frame_ingest → rectify → stereo backend → depth/confidence → tracking       │
│       │                         │                │              │            │
│       │                         │                │              ▼            │
│       │                         │                │        pose graph (GTSAM) │
│       │                         │                │              │            │
│       │                         └──────► fusion (TSDF/surfel) ◄┘            │
│       │                                        │                             │
│       └──────────────► texture pipeline ◄──────┴──► mesh (MC/Poisson)        │
└───────────────▲───────────────────────────────────────────┬─────────────────┘
                │ CUDA-OpenGL/Vulkan external memory         │ C ABI events
┌───────────────┴───────────────────────────────────────────▼─────────────────┐
│                         Qt 6 GUI (QML + RHI)                                 │
│  viewport, scan state, quality/coverage heat-map, GPU memory, logs           │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Process model

| process | language | responsibility | failure domain |
|---|---|---|---|
| `einstar-deviced` | Rust | USB/FX3 transport, FPGA trigger, frame timestamps, dmabuf pool, watchdog | device loss; restartable |
| `einstar-recon` | C++23/CUDA | all reconstruction, GPU scheduling, mesh/texture | GPU OOM/crash; restartable, project DB is authoritative |
| `einstar-ui` | Qt 6/C++ | viewport, interaction, job submission | user-facing |
| `einstar-ctl` (library) | Rust | typed commands/events, shared-memory ring, version negotiation | in-process |

The GUI never owns the GPU context used for reconstruction. Preview textures are exported as
CUDA external memory and imported by Qt RHI (OpenGL or Vulkan) with a fence; no `glReadPixels`.

---

## 2. Stable frame ABI (v1)

The ABI is a **plain C header** (`include/einstar/frame_v1.h`), generated into Rust with `cbindgen`
and checked by a static-assert test in both languages. Rules:

* fixed-width types, explicit padding, `static_assert(offsetof(...))` for every field;
* no pointers owned by the producer except device/host addresses that remain valid for the lease;
* one `magic` + `version` + `struct_size`; consumers reject unknown major versions;
* all arrays have `count` + `stride`; no implicit camera count;
* the frame is valid until `release(frame_handle)` is called; the consumer must not retain it.

```c
#define EINSTAR_FRAME_MAGIC 0x45494E53u   /* "EINS" */
#define EINSTAR_FRAME_VERSION 1u

typedef enum { EIN_PIX_MONO8=0, EIN_PIX_MONO10=1, EIN_PIX_MONO12=2,
               EIN_PIX_RGB8=3, EIN_PIX_BAYER8=4 } ein_pixel_format;

typedef enum { EIN_MEM_DEVICE=0, EIN_MEM_HOST_PINNED=1, EIN_MEM_DMABUF=2 } ein_mem_kind;

typedef struct {
    uint64_t address;        /* device VA, host VA, or dmabuf fd */
    uint32_t width, height;
    uint32_t stride_bytes;
    uint32_t pixel_format;   /* ein_pixel_format */
    uint32_t mem_kind;       /* ein_mem_kind */
    uint32_t sequence;       /* camera-local frame counter */
    uint64_t exposure_ns;
    float    gain_db;
    uint32_t flags;          /* EIN_FRAME_FLAG_* */
} ein_image_plane;

typedef struct {
    uint32_t camera_id;
    uint32_t role;           /* 0=left, 1=right, 2=aux_mono, 3=color */
    ein_image_plane plane;
    double   T_cam_from_rig[16];  /* row-major; nominal, refined by calibration */
} ein_camera_frame;

typedef struct {
    uint32_t magic, version, struct_size;
    uint64_t frame_id;
    uint64_t timestamp_ns;       /* CLOCK_MONOTONIC, trigger edge */
    uint64_t trigger_id;         /* FPGA sequence */
    uint32_t camera_count;
    uint32_t flags;              /* projector on, LED mode, etc. */
    ein_camera_frame cameras[4];
    uint64_t projector_strobe_ns;
    uint64_t reserved[8];
} ein_frame_v1;
```

**Zero-copy paths**

1. **dmabuf (preferred):** Rust allocates V4L2 buffers, exports `dmabuf_fd`; C++ imports with
   `cudaImportExternalMemory` + `cudaExternalMemoryGetMappedBuffer`, or maps directly if the device
   writes into CUDA-imported memory. No copy; one `cudaExternalSemaphore` wait per frame.
2. **Pinned host (fallback):** Rust fills a registered pinned ring; C++ issues
   `cudaMemcpyAsync` on the ingest stream. 2.6 MB per mono frame at 25 fps = 65 MB/s/camera, well
   below PCIe Gen4 ×16 (~25 GB/s theoretical).
3. **Device-native (future):** if acquisition moves to a Jetson/GPU-direct NIC, the same ABI carries
   a device VA and a semaphore.

**Frame lease and backpressure.** The ring has 8 slots. If the consumer is more than 3 frames behind,
the producer drops the oldest non-keyframe and increments `dropped_frames`; the reconstruction core
must be able to start from any frame because tracking has a motion model and a keyframe fallback.

---

## 3. Core modules and data structures

### 3.1 Module map

| module | key types | GPU | notes |
|---|---|---|---|
| `frame` | `Frame`, `FramePool`, `ImageView<T>` | ingest | ABI validation, lifetime, stream events |
| `calib` | `RigCalibration`, `CameraIntrinsics`, `RectifyMaps` | map build (CPU/GPU) | per-unit CCF/JSON; online refinement |
| `rectify` | `RectifiedPair`, `RemapLut` | yes | fused undistort+rectify+USM+pyramid |
| `stereo` | `IStereoBackend`, `CostVolume`, `DisparityMap` | yes | pluggable |
| `depth` | `DepthMap`, `ConfidenceMap`, `PointImage` | yes | disparity→depth, normals, filters |
| `tracking` | `Pose`, `Keyframe`, `MotionModel` | hybrid | feature + ICP + factor graph |
| `fusion` | `TsdfVolume`, `SurfelModel`, `VoxelHash` | yes | pose-aware, multi-resolution |
| `mesh` | `Mesh`, `MeshDelta`, `Atlas` | yes/CPU | MC live, Poisson offline |
| `texture` | `TextureAtlas`, `CameraPose`, `ColorProfile` | yes | reprojection, colour correction |
| `io` | `Project`, `FrameStore`, `MeshWriter` | CPU | crash-safe, versioned |
| `telemetry` | `StageTiming`, `GpuMemory`, `Counters` | both | Prometheus/JSON |

### 3.2 Image and depth types

```cpp
template <class T> struct ImageView {          // non-owning, device or host
    T* data; uint32_t w, h, stride; MemKind kind;
};
struct RectifiedPair {
    ImageView<const uint16_t> left, right;      // 10/12-bit mono, rectified
    ImageView<const uint16_t> left_pyramid[4];  // optional
    RectifyMaps maps;                           // fused LUT: float2 per pixel
    cudaEvent_t ready;
};
struct DisparityMap {
    ImageView<float> disp;                      // sub-pixel, left reference
    ImageView<float> cost_curvature;            // confidence proxy
    ImageView<uint8_t> valid;                   // LR-consistent + texture gate
    float min_disp, max_disp;
};
struct DepthMap {
    ImageView<float> z;                         // metres
    ImageView<float> sigma_z;                   // 1-sigma, metres
    ImageView<float3> normal;                   // camera frame
    ImageView<uint8_t> valid;
};
```

`sigma_z` is computed analytically from disparity uncertainty and the stereo geometry:

```
sigma_z = z^2 / (f_px * B) * sigma_d
```

with `sigma_d` from cost-curvature/sub-pixel fit and a floor for quantisation. This is the value
that becomes the TSDF weight; it is not a heuristic “confidence 0..1”.

### 3.3 Pluggable stereo interface

```cpp
class IStereoBackend {
public:
    virtual ~IStereoBackend() = default;
    virtual StereoCaps caps() const = 0;                 // disp range, min res, needs projector
    virtual void prepare(const StereoConfig&, cudaStream_t) = 0;
    virtual void compute(const RectifiedPair&, DisparityMap&, cudaStream_t) = 0;
    virtual const char* name() const = 0;
};
```

| backend | algorithm | when | expected cost |
|---|---|---|---|
| `SgmBackend` | 4/8-path SGM, census 5×5 + ZNCC, WTA, sub-pixel parabola, LR check, median, CCL | default, textured/white-light | 8–15 ms @ 1280×1024, D=256 |
| `ElfBackend` | ELF (Efficient Large-scale Stereo) or tile-based SGM with 8 directions | high quality offline | 20–40 ms |
| `SpeckleBackend` | speckle centre/edge detection, sparse matching, projective consistency, triangulation | IR speckle mode | 4–8 ms, sparse (20–40% pixels) |
| `LearnedBackend` | RAFT-Stereo / CREStereo ONNX→TensorRT FP16/INT8, calibration-aware refinement | low texture, challenging objects | 20–50 ms, 2–4 GB |
| `PhaseBackend` | heterodyne phase + projector correspondence | only if a grating mode is added | 10–20 ms |

The backend contract includes a **calibration-aware refinement** step: after the network produces
disparity, run 2–4 iterations of a differentiable/analytic cost refinement using the rectified
images and the physical disparity range, then LR consistency. This prevents a learned model from
inventing geometry outside the calibrated volume.

### 3.4 Tracking and factor graph

* **Front-end:** ORB (GPU) or SuperPoint/LightGlue (optional) on the texture camera; KLT/optical
  flow on mono for short baselines; projective ICP between the current point image and the fused
  model.
* **ICP:** point-to-plane, projective data association, robust Huber/Tukey, 6×6 normal equations,
  GPU reduction; 10 iterations max, early exit on `delta_T < 1e-5`.
* **Motion model:** constant-velocity prediction; particle filter only as a recovery mode (the vendor
  proved this pattern; modern systems can use it as a fallback, not the default).
* **Back-end:** GTSAM `ISAM2` (or Ceres `SPARSE_SCHUR`) over keyframes: odometry factors, loop
  closures (DBoW2 or learned global descriptors), optional plane/gravity factors (IMU-less, gravity
  from the dominant plane normal), robust noise models.
* **Keyframe policy:** translation > 2 mm or rotation > 1.5°, or inlier ratio < 0.6, or every 0.5 s.
* **Loop closure:** DBoW2 vocabulary (or a learned global descriptor) + geometric verification
  (PnP/ICP); only accept if inlier count and residual pass; then `ISAM2::update`.

### 3.5 Fusion

Two implementations behind `IFusion`:

**TSDF (default).** Voxel-hash (Nießner-style) with 8³ voxel blocks; each voxel stores
`{int16 sdf, uint8 weight, uint8 color[3]}` or a packed 8-byte record. Integrate only within the
truncation band `|sdf| < 3 * voxel`. Weight update:

```
w_new = min(w_old + dw, w_max)
sdf_new = (sdf_old * w_old + sdf_obs * dw) / w_new
dw = 1 / (sigma_z^2 + eps)        // inverse-variance, capped
```

This is the main accuracy lever over the vendor’s uniform weighting (the vendor exposes
`tsdfWeightThr`/`upsampleWeightThr` but the exact rule is unknown).

**Surfel (optional).** 1–3 M surfels, 16–24 B each: position, normal, radius, confidence, color,
timestamp. Faster to update and better for preview; no watertight mesh. Use surfels for live
preview, TSDF for final mesh.

**Multi-resolution.** Coarse 2 mm volume for tracking/preview, fine 0.3–0.5 mm volume for the final
mesh; promote blocks when the camera is close and the surface is stable. This mirrors the vendor’s
`FusionTsdfMuliResolution<MultiLeafTsdf>` / `resolutionLevel` design without copying its code.

### 3.6 Mesh

* **Live:** CUDA marching cubes over active blocks, 1–3 ms; emit `MeshDelta` (new/changed vertices,
  indices) to the GUI; keep a CPU mirror for export.
* **Offline:** Poisson reconstruction (CPU, TBB) or screened Poisson; optional mesh denoising,
  decimation (quadric error), hole filling, and normal refinement.
* **Topology:** half-edge/OpenMesh-compatible export; validate manifoldness and self-intersection
  before texture mapping.

### 3.7 Texture

1. Keyframe selection (sharpness, exposure, baseline).
2. Per-keyframe pose refinement (photometric + geometric) against the mesh.
3. UV atlas generation (xatlas or custom chart packing).
4. Per-texel view selection and blending (visibility test via ray cast; feathering; exposure
   compensation).
5. Colour correction: per-camera CCM, white balance, vignetting, gamma; optional global colour
   equalisation in Lab.
6. Offline: joint texture + geometry refinement (small bundle adjustment over texture cameras),
   Poisson blending.

---

## 4. Threading and GPU scheduling

### 4.1 CPU threads

| thread | affinity | work |
|---|---|---|
| `acq` (Rust) | P-core | USB/FX3, trigger, timestamp, publish frame |
| `ingest` | P-core | ABI validation, dmabuf import, event record |
| `stereo` | P-core | launch stereo kernels, wait, publish disparity |
| `track` | P-core | feature extraction, ICP, factor graph |
| `fusion` | P-core | TSDF integration, mesh delta |
| `texture` | E-core pool | keyframe selection, atlas, colour |
| `io` | E-core | frame store, project DB, mesh export |
| `ui` | — | Qt event loop, RHI uploads |

Use `std::jthread`, a bounded MPMC queue (`moodycamel::BlockingConcurrentQueue` or a custom SPSC
ring), and `std::stop_token` for cancellation. No thread pool for the hot path; TBB only inside
Poisson/decimation.

### 4.2 CUDA streams

```
S0 ingest     : H2D (if needed), rectify+USM+pyramid
S1 stereo     : census/ZNCC → aggregation → WTA → subpixel → LR → median/CCL
S2 depth      : disparity→depth, normals, confidence, point image
S3 tracking   : ICP reduction, SVD, pose
S4 fusion     : TSDF integrate, mesh extract
S5 texture    : reprojection, atlas, colour
```

Events: `S0→S1`, `S1→S2`, `S2→S3`, `S2→S4`, `S3→S4`, `S4→S5`. Use `cudaStreamWaitEvent`, never
`cudaDeviceSynchronize` on the hot path. Fusion may lag tracking by one frame; the pose is stored
with the depth frame so the TSDF integration is always pose-consistent.

### 4.3 Frame budget (live, 25 fps = 40 ms)

| stage | budget | notes |
|---|---:|---|
| acquire + ABI | 0.2 ms | zero-copy |
| rectify + USM + pyramid | 0.4 ms | 3 × 1.3 MP, fused kernel |
| stereo SGM (D=256) | 8–12 ms | 640 MiB cost volume, 4 paths |
| depth + confidence + normals | 1.5 ms | 1.3 MP |
| tracking (ORB + ICP + iSAM2) | 4–6 ms | 1 keyframe every ~10 frames |
| TSDF integrate | 3–5 ms | 1.3 MP, hash insert |
| mesh delta | 1–3 ms | active blocks only |
| texture preview | 1–2 ms | 1 keyframe |
| **total** | **19–30 ms** | 33–50 fps headroom; target 25 fps with margin |

Learned stereo adds 20–40 ms, so live mode is either classical at 25 fps or learned at 10–15 fps
with classical fallback. **[speculation until measured]**

### 4.4 GPU memory budget (12 GB)

| item | size | notes |
|---|---:|---|
| rectified images + pyramids (3 cams) | 0.15 GB | 16-bit, 4 levels |
| cost volume D=256 (uint16) | 0.64 GB | 1280×1024×256×2 B |
| cost volume D=512 (optional) | 1.28 GB | offline/high quality |
| aggregation temp | 0.64 GB | in-place + scratch |
| depth/normal/confidence/point | 0.10 GB | 1.3 MP × (4+12+4+16) B |
| TSDF 0.5 mm, 24 M voxels | 0.3–0.6 GB | 8–12 B/voxel + hash overhead |
| TSDF 0.3 mm, 111 M voxels | 1.3–2.7 GB | offline/high quality |
| mesh buffers (5 M tris) | 0.3–0.6 GB | vertices, normals, colours, indices |
| texture atlas 4K + mips | 0.2 GB | RGB8 |
| learned stereo weights/workspace | 2–4 GB | only if enabled |
| CUDA context + workspace + reserve | 1.5 GB | fragmentation headroom |
| **total (classical)** | **3.5–6.5 GB** | fits 12 GB |
| **total (learned + fine TSDF)** | **7–10.5 GB** | tight; evict fine TSDF during live |

Memory policy: a `GpuBudget` object reserves the fine TSDF lazily; if `cudaMemGetInfo` shows
< 1.5 GB free, reduce mesh resolution / evict texture mips / disable learned backend. The vendor’s
Magic exposes `is_enough_mem`, `maxVoxel`, `get_leaf_use_mem` — the same idea, but with explicit
budgets and telemetry.

---

## 5. Modern approaches compared on this hardware

Notation: `f ≈ 1180 px` (left camera from CCF), baseline `B` assumed **100 mm** (not proven; the
vendor prints `_baseLineD` but the value is runtime). Depth error:

```
sigma_z = z^2 / (f * B) * sigma_d
```

| approach | accuracy @ 0.4 m | detail / coverage | robustness | throughput (1280×1024) | GPU mem | verdict |
|---|---|---|---|---|---|---|
| **Calibrated stereo + subpixel + LR** | 0.14 mm at `sigma_d=0.1 px`; 0.27 mm at 0.2 px | full-res detail, coverage depends on texture; weak on black/shiny | fails on untextured; sensitive to calibration drift | 8–15 ms SGM D=256 | 1.3–1.5 GB | **baseline**, keep |
| **Speckle-assisted stereo** | 0.1–0.2 mm (sparse centres denser than SGM) | 20–40% valid pixels but high confidence; excellent on untextured | sensitive to ambient IR, projector focus, subsurface | 4–8 ms | 0.3–0.6 GB | **must keep** for IR mode |
| **Learned stereo (RAFT-Stereo/CREStereo, TensorRT FP16)** | 0.1–0.3 mm after calibration refinement **[speculation]** | dense, better on low texture; can hallucinate | domain gap to speckle; needs fine-tune; temporal flicker | 20–50 ms FP16 on 3080 **[speculation]** | 2–4 GB | **experiment**, not default |
| **TSDF fusion** | quantisation 0.3–0.5 mm; averaging reduces noise | watertight, good for MC | pose errors smear; needs confidence weights | 3–5 ms/frame | 0.3–2.7 GB | **default** |
| **Surfel fusion** | similar, no watertight mesh | fast preview, view-dependent | noisier, no Poisson | 1–3 ms/frame | 20–80 MB | **preview** |
| **Pose graph + loop closure** | ATE 0.5–2 mm on room-scale **[speculation]** | fixes drift, enables re-visit | false loops if descriptors weak | 5–20 ms/keyframe | <100 MB | **required** |
| **Neural features (SuperPoint/LightGlue)** | better than ORB on blur/low texture | more robust tracking | 10–20 ms/pair, model size | 10–20 ms **[speculation]** | 0.5–1.5 GB | **optional**, fallback ORB |

### 5.1 What the numbers mean

* **Depth noise.** At `z=0.4 m`, `f=1180`, `B=0.1 m`, one disparity pixel is **1.36 mm**. A 0.1 px
  sub-pixel estimate is therefore 0.14 mm — the same order as the product-class 0.1 mm accuracy claim.
  Improving sub-pixel from 0.2 px to 0.1 px halves the noise; improving baseline from 100 to 150 mm
  reduces it by 1/3.
* **Cost-volume bandwidth.** D=256, uint16: 640 MiB. Four aggregation paths read+write it:
  `4 × 2 × 640 MiB = 5.0 GiB` per frame. At 912 GB/s that is **≈5.9 ms** of pure traffic; a real
  implementation with census, WTA, sub-pixel, LR and median lands at **8–15 ms**. D=512 doubles it.
* **Tensor cores.** RTX 3080 12 GB: ~30.6 TFLOPS FP32, ~122 TFLOPS FP16 tensor, 912 GB/s, 12 GB.
  A learned stereo forward at 1.3 MP is plausibly 0.5–2 TFLOP; tensor cores make it compute-light,
  but the correlation volume and memory traffic dominate. 20–50 ms is a realistic range; **measure
  before committing**.
* **TSDF.** 0.5 mm voxel, 24 M surface-band voxels × 12 B ≈ 0.3 GB; 0.3 mm → 111 M × 12 B ≈ 1.3 GB.
  Hash overhead 1.5–2×. Integration is atomic-heavy; 3–5 ms/frame is realistic for 1.3 MP.
* **CPU.** i9-14900K with 32 threads and ~90 GB/s DDR5: Ceres bundle adjustment over 100–300
  keyframes is 0.1–2 s offline; iSAM2 incremental updates are <5 ms. ORB extraction on 1.3 MP is
  3–6 ms CPU; GPU is 1–2 ms.
* **NVMe.** 7 GB/s raw; a 5-minute scan at 25 fps × 3 × 2.6 MB ≈ 58 GB if every frame is stored.
  Store keyframes + compressed depth (FP16 + PNG/zip) instead; target <5 GB/project.

### 5.2 What must be validated experimentally

1. Actual `sigma_d` vs cost curvature on the real sensor (photon noise, speckle contrast, projector
   defocus).
2. Whether learned stereo generalises to the Einstar’s speckle; fine-tune on captured pairs.
3. Real baseline/extrinsics from CCF (layout unresolved) and thermal drift over a 10-minute scan.
4. TSDF voxel size vs mesh detail/accuracy trade-off on a sphere/step gauge.
5. Loop-closure false-positive rate with DBoW2 vs learned descriptors on repetitive surfaces.
6. End-to-end latency and dropped frames under USB/projector jitter.

### 5.3 Top three accuracy/reliability opportunities

These follow directly from the vendor evidence and the error budget; expected gains are estimates
to be validated, not claims.

1. **Uncertainty-weighted fusion instead of uniform weights.**
   The vendor exposes `tsdfWeightThr`/`upsampleWeightThr` but the weighting rule is unknown, and its
   depth path produces a weight from `count_weight_with_depth_linear_*` rather than a calibrated
   variance. Our design computes `sigma_z` from sub-pixel cost curvature + LR margin + speckle
   density, and fuses with `dw = 1/(sigma_z^2+eps)`. This should both reduce noise (more frames
   contribute where they are informative) and reject outliers (a wrong 0.2 px match gets a low
   weight instead of a fixed one). Expected: 20–40% lower surface noise at equal frame count, fewer
   double layers at 1 mm pose error **[speculation; validate with E07/E08]**.

2. **Calibration-aware sub-pixel + left/right consistency + multi-view check.**
   The vendor already has LR consistency and sub-pixel matching, so this is not a missing stage; the
   opportunity is to make it **calibration-aware and uncertainty-aware** (rectified geometry,
   baseline, distortion, per-pixel disparity bounds) and to add a cheap multi-view reprojection
   check before fusion. On low-texture and grazing surfaces this is the difference between a
   plausible-looking but biased depth map and a metrologically usable one. Expected: fewer flying
   pixels at edges and < 0.1 mm bias on planar targets **[speculation; validate with E05/E06]**.

3. **Loop closure + pose graph + texture-aware ICP.**
   The vendor has DBoW2/ORB loop detection and iSAM-style local optimisation in
   `Sn3DTextureSlam`, but the live ICP is a frame-to-model tracker with limited global correction.
   A modern back-end (GTSAM iSAM2, robust odometry + loop factors, optional plane/gravity factors)
   plus texture-aware ICP (photometric residual on the RGB camera) should cut long-scan drift and
   prevent double surfaces. Expected: drift from ~1% to < 0.5% of path length on room-scale scans
   **[speculation; validate with E10 and P10]**.

The common thread: the vendor’s stages are all present and recognisable; the modern wins are in
**uncertainty propagation, calibration discipline, and global consistency**, not in replacing SGM
with a network.

---

## 6. Offline “maximum quality” mode

Same stages, different policy:

| stage | live | offline |
|---|---|---|
| stereo | SGM D=256, 4 paths, half-res optional | ELF/8-path, D=512, full res, learned ensemble |
| confidence | cost curvature + LR | + multi-view consistency, reprojection error |
| tracking | ICP + motion model | full bundle adjustment over keyframes + loop closure |
| fusion | 0.5 mm TSDF, uniform-ish weights | 0.3 mm TSDF, inverse-variance weights, multi-resolution |
| mesh | marching cubes | Poisson / screened Poisson + decimation + hole fill |
| texture | one keyframe, simple blend | all keyframes, visibility, exposure/colour optimisation, Poisson blend |
| time | 20–30 ms/frame | minutes per project (CPU+GPU) |

Offline jobs are queued in the Rust control plane, run in a separate `einstar-recon --offline`
process, and write a versioned project bundle (`project.json`, `frames/`, `mesh.ply`, `texture/`).

---

## 7. Integration with the Rust control plane

### 7.1 C ABI

```c
typedef struct einstar_recon einstar_recon;
einstar_recon* einstar_recon_create(const einstar_config_v1*, char* err, size_t err_cap);
void           einstar_recon_destroy(einstar_recon*);
int            einstar_recon_submit(einstar_recon*, const ein_frame_v1*, uint64_t* ticket);
int            einstar_recon_poll(einstar_recon*, einstar_event_v1*, size_t cap, size_t* n);
int            einstar_recon_cancel(einstar_recon*, uint64_t ticket);
```

* `extern "C"`, `#[repr(C)]` on the Rust side, `cbindgen` generates the header.
* Errors are integer codes + thread-local message; **no exceptions cross the boundary**.
* Events are fixed-size structs (`POSE`, `MESH_DELTA`, `STATS`, `ERROR`, `PROGRESS`) written to a
  shared-memory ring; the GUI/control plane reads without allocation.
* Version negotiation at `create`: the core rejects an ABI major it does not know.

### 7.2 Rust crates

| crate | role |
|---|---|
| `einstar-abi` | `#[repr(C)]` structs, constants, static asserts, cbindgen |
| `einstar-device` | USB/FX3, V4L2, FPGA trigger, dmabuf pool, timestamps |
| `einstar-recon-sys` | bindgen bindings + safe RAII wrapper |
| `einstar-orchestrator` | scan state machine, command bus, project DB, job queue |
| `einstar-telemetry` | tracing, metrics, crash dumps |

Commands: `StartScan{mode, params}`, `StopScan`, `SetExposure`, `SetProjector`, `SubmitOffline`,
`Cancel`, `QueryStatus`. Events: `FrameStats{fps, dropped, latency}`, `Pose`, `MeshDelta`,
`Coverage`, `Error`.

### 7.3 Qt 6 GUI

* QML viewport with `QQuickRhiItem`; CUDA→OpenGL/Vulkan external memory import; one texture per
  view (color, depth heat-map, confidence, normals, mesh).
* Model/View: `ScanController` (C++/Qt) subscribes to the Rust event ring; no reconstruction logic
  in QML.
* Panels: live FPS/latency, GPU memory, coverage heat-map, pose graph, keyframe list, quality
  warnings (“move slower”, “too close”, “low texture”).
* Offline job dialog: quality preset, output path, progress, cancel.

---

## 8. Milestones

Assumes 2 engineers (C++/CUDA + Rust/Qt) and access to the device for the later milestones.
Durations are calendar weeks; acceptance is testable offline until M6.

| milestone | deliverable | acceptance |
|---|---|---|
| **M0** (1 w) | repo skeleton, ABI v1, synthetic data generator, CI, benchmark harness | `ctest` green; ABI static asserts; 100 synthetic frames/s with dummy backend |
| **M1** (2 w) | rectification + SGM backend + confidence v0 | synthetic disparity RMSE < 0.25 px; epipolar error < 0.1 px; 12 ms/frame |
| **M2** (2 w) | depth/point/normals, LR check, filters, uncertainty | synthetic depth RMSE < 0.3 mm at 0.4 m; bad-pixel < 2% |
| **M3** (2 w) | tracking: ORB + ICP + GTSAM iSAM2, keyframes | TUM RGB-D ATE < 2 cm (mono-ish); synthetic ATE < 1 mm; 6 ms/frame |
| **M4** (2 w) | TSDF voxel hash + CUDA MC + mesh delta | synthetic sphere Chamfer < 0.2 mm; 5 ms/frame; 1 GB budget |
| **M5** (2 w) | texture atlas + colour correction + offline Poisson | texture PSNR > 30 dB on synthetic; Poisson watertight |
| **M6** (2 w) | device integration: dmabuf, FPGA timestamps, Rust control plane, Qt viewport | 25 fps end-to-end, 0 copies, <1% dropped frames on 10-min scan |
| **M7** (2 w) | offline max-quality mode, project format, crash recovery | 5-min scan processes in <5 min; deterministic output hash |
| **M8** (2 w) | calibration refinement, thermal drift, accuracy validation | sphere/step gauge error < 0.1 mm; repeatability < 0.05 mm |

**Critical path:** M1→M2→M4→M6. Texture (M5) can run in parallel after M4.

---

## 9. Risks and mitigations

| risk | impact | mitigation |
|---|---|---|
| CCF layout / extrinsics unresolved | wrong depth scale, stereo mismatch | decode CCF via calibration workstream; fallback to a controlled calibration target; validate against known sphere |
| projector/sensor sync jitter | motion artifacts, dropped frames | FPGA trigger timestamp in ABI; reject frames with `trigger_id` gaps; expose `projector_strobe_ns` |
| learned stereo domain gap | hallucinated/biased depth | calibration-aware refinement, LR check, confidence gate, classical fallback, fine-tune on captured speckle |
| GPU OOM at 0.3 mm TSDF + learned | crash | explicit `GpuBudget`, lazy fine volume, eviction, quality presets |
| pose drift / false loop closure | double surfaces, bad mesh | robust factors, loop verification, pose-graph outlier rejection, offline BA |
| Rust↔C++ ABI drift | crashes | single header, cbindgen, static asserts, fuzz the frame parser |
| Qt/CUDA interop | stalls/copies | external memory + fences; never `glFinish`; triple-buffered preview |
| vendor-format lock-in | can't import old projects | read-only importer for vendor project format (separate milestone, not on critical path) |
| calibration thermal drift | accuracy loss over 10 min | periodic re-calibration check; temperature log; online extrinsics refinement |

---

## 10. What to build first (concrete first sprint)

1. `include/einstar/frame_v1.h` + Rust `einstar-abi` + static-assert tests.
2. `apps/recon_bench` that reads a synthetic sequence from disk and runs rectify→SGM→depth→TSDF→MC.
3. `stereo/sgm_cuda.cu` with census 5×5, 4-path aggregation, WTA, parabola sub-pixel, LR check.
4. `fusion/tsdf_hash.cu` with 8³ blocks, inverse-variance weights, active-block MC.
5. `tests/synth/` generator: planes/spheres/steps with analytic depth, simulated speckle, noise.
6. `docs/reconstruction/validation_plan.md` executed as `ctest` where possible.

The vendor evidence says the hard parts are **stereo confidence** and **pose-aware fusion**, not the
kernel plumbing. Spend the first two sprints on those, not on GUI polish.
