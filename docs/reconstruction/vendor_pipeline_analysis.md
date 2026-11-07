> **Integration note (2026-11-07, after the calibration work completed).**
> The CCF numbers quoted in this survey were read before the record layout was proved and
> come from the *older* `ccf/200x150` copy without the IR integer key. The authoritative,
> geometrically validated decode is `docs/calibration/ccf_format.md`: the record is
> **33 float64** in `Calib_Results` order (`fc, cc, alpha, kc[5], T[3], R[9], error`) plus a
> 256-byte IR-only trailer; IR values carry a **per-field integer offset** whose rotation part
> `(-2,-4,-11,-9,0,5,-11,-12,0)` is proved unique. Decoded `res/…/LeftCCF.txt` is
> `fx=1159.115, fy=1159.021, cx=623.91, cy=534.56`; baseline **158.557 mm**; all rotations
> det = 1 to 1e-15. The "65 doubles / layout unresolved" statement below is superseded.

# Einstar vendor reconstruction pipeline — evidence-backed survey

Status: **first complete pass**, read-only static analysis of the shipped EXStar 1.2.2.0 installation
(2024-06-28 build). No vendor binary was executed. This document separates:

* **[observed]** — bytes, PE structures, exported symbols, embedded strings, fatbin contents read directly;
* **[derived]** — a conclusion computed from observed data (e.g. a kernel name implies a 4-path cost aggregation);
* **[inferred]** — an interpretation of names/behaviour that the bytes do not by themselves prove;
* **[unknown]** — not established offline.

Every claim cites the module and a file offset (string/section) or an exported symbol. Offsets are
**file offsets** in the on-disk DLL, not RVAs. Raw evidence lives in
`analysis/gnu/reconstruction_strings/`; machine-readable metadata is in
`analysis/gnu/reconstruction_survey.json`; the survey is reproducible with
`tools/analysis/survey_modules.py`.

Vendor root used for all citations:
`<EXStar-install>/Shining3d/EXStar` (mounted read-only).

---

## 1. Artifact inventory

Sizes are bytes; “fatbin” is the size of the `.nv_fatb` section; “kernels” is the number of
distinct mangled CUDA kernel symbols recovered from the fatbin/PTX. Full SHA-256 values are in
`reconstruction_survey.json` (truncated here for readability).

| module | size | exports | fatbin | kernels | SHA-256 (prefix) |
|---|---:|---:|---:|---:|---|
| `Sn3DSpeckle.dll` | 32,965,112 | 69 | 30,671,872 | 251 | `b2abd309df6dc5fd…` |
| `Sn3DSpeckleFusion.dll` | 27,599,864 | 26 | 26,140,672 | 230 | `26c706e0528152ef…` |
| `Sn3DPhaseBuild.dll` | 3,680,760 | 47 | 1,527,808 | 134 | `bb39f2ade980aa8f…` |
| `Sn3DGratingRecons.dll` | 2,563,064 | 41 | 770,048 | 71 | `90ba58303ddd38c2…` |
| `Sn3DRealtimeScan.dll` | 8,656,376 | 1003 | 0 | 0 | `456e4480ded19c74…` |
| `Sn3DRegistration.dll` | 9,288,696 | 1729 | 0 | 0 | `7cea821b870ece0f…` |
| `Sn3DGeometricTrackFusion.dll` | 1,824,760 | 302 | 0 | 0 | `fc3acc00355da2b4…` |
| `Sn3DTextureBasedTrack.dll` | 683,000 | 35 | 0 | 0 | `83196c6cb9b816cf…` |
| `Sn3DTextureSlam.dll` | 2,658,808 | 449 | 97,792 | 7 | `99b68e6da34323ee…` |
| `Sn3DTextureSFM.dll` | 6,197,240 | 74 | 171,008 | 0 (compressed cubin) | `97b053d6ba400d1d…` |
| `Sn3DTextureUtil.dll` | 4,992,504 | 89 | 0 | 0 | `fb0139dc6fb7244b…` |
| `Sn3DCloudProcessGPU.dll` | 666,104 | 3 | 4,608 | 0 | `02a17a13b0bd5035…` |
| `Sn3DColorCorrect.dll` | 1,365,496 | 64 | 426,496 | 64 | `1d50e0abf6700303…` |
| `Sn3DAlgorithmWrapper.dll` | 1,772,024 | 147 | 0 | 0 | `5ba900e272721577…` |
| `commonGpu.dll` | 1,189,368 | 372 | 415,744 | 71 | `e7294ef31302592f…` |
| `common.dll` | 5,777,912 | 2669 | 0 | 0 | `f72360f413630857…` |
| `Sn3DMagic.dll` | 7,693,304 | 658 | 1,561,600 | 27 (PTX entries) | `7e7184ef0629ec1b…` |
| `ceres.dll` | 2,220,024 | 285 | 0 | 0 | `eff5555ccb1acff2…` |
| `algorithmLzy.dll` | 16,428,024 | 495 | 8,752,128 | 811 | `5428be44674a83a3…` |
| `algorithmHlj.dll` | 4,895,224 | 427 | 1,614,848 | 205 | `a33b6cb3f40dfe73…` |

Key negative observations:

* **No cuBLAS, cuDNN, TensorRT, NVRTC or OpenCL imports** in any reconstruction module
  (survey import tables; a single `OpenCL not supported in this binary!` string in
  `Sn3DTextureSFM.dll` off `0x4AB6D0` is SIFTGPU’s own diagnostic, not an OpenCL use).
* **No `pcl_*` imports.** FLANN is statically linked (e.g. `Sn3DRegistration.dll` off `0x6BA110`,
  `KMeansTree using params: max_iterations=%d, branching=%d`).
* **No OpenCV CUDA module imports** (`opencv_cuda*`), although `opencv_cudawarping348.dll` exists in
  the installation as an unrelated dependency of other products. Reconstruction uses OpenCV 3.4.8 CPU.

### 1.1 Build/toolchain facts relevant to reconstruction

| fact | evidence |
|---|---|
| MSVC 19.16 / VS2017 15.9, x64, `/MD` | PE linker 14.16 and Rich headers in all vendor modules (see `docs/windows/module_map.md`); confirmed again per module in the survey JSON |
| CUDA 10.2.89 | `cudart64_102.dll`; static runtime banners in modules; PTX `.version 6.5` and `.target sm_50`/`sm_30` (e.g. `Sn3DSpeckle.dll` off `0x2340A7`/`0x2340B5`; `Sn3DPhaseBuild.dll` off `0x227955`) |
| CUDA runtime linked **statically** | 322 `cuda*` API strings inside `Sn3DSpeckle.dll` (e.g. `cudaLaunchKernel_ptsz` around `0x1CEB50`), no `cudart64_102.dll` import |
| Driver API loaded explicitly | string `nvcuda.dll` `Sn3DSpeckle.dll` off `0x1CFEC0`; `cuLaunchKernel` off `0x1D0C68`; same pattern in every CUDA module |
| OpenCV 3.4.8 CPU | imports `opencv_core348/imgproc348/calib3d348/features2d348`; banner inside `opencv_core348.dll` |
| Ceres 1.12.0 | path `D:\.conan\data\Ceres\1.12.0\…` in `Sn3DRegistration.dll` off `0x4921A0` (and `Sn3DTextureSFM.dll`) |
| Eigen (header-only), TBB, OpenMP | `Eigen::Matrix<…>` in every demangled export; imports `tbb.dll`, `VCOMP140.DLL` (`_vcomp_*`, `omp_*`) |
| OpenMesh | `Sn3DRealtimeScan.dll`/`Sn3DRegistration.dll` import `OpenMeshCore.dll` |

---

## 2. Executive reconstruction

The Einstar is a **three-monocular-camera, FPGA-triggered scanner** with an IR speckle projector and a
separate colour camera. The vendor software contains **three reconstruction families**, only one of
which is the Einstar’s live path:

1. **Dense stereo / SGM family** (`Sn3DSpeckle.dll`, `Sn3DSpeckleFusion.dll`): rectification →
   census/ZNCC cost → **4-path cost aggregation** → WTA → sub-pixel → left/right consistency →
   median/CCL/hole fill → depth → point image. Both an **IR speckle** and a **white-light** variant exist.
2. **Phase/structured-light family** (`Sn3DPhaseBuild.dll`, `Sn3DGratingRecons.dll`): multi-frequency
   wrapped phase, heterodyne unwrap, projector-camera correspondence (rough/refine), phase→depth.
   Included in the installation but its kernels are the `sm_30` set; the Einstar (speckle) path is (1).
3. **Laser-line family** (`algorithmLzy/Hlj/…`): line-centre extraction, TSDF fusion, etc. These are
   shared/OEM algorithm builds imported by the wrapper, **not** proven to be used by the Einstar.

The live Einstar pipeline is orchestrated by `Sn3DRealtimeScan.dll` (1003 exports) and is split
across:

```
acquisition (FPGA-triggered 3×1280×1024 + colour)
  → rectify / preprocess (CUDA)
  → speckle or white-light stereo (CUDA)
  → depth + confidence + normals + outlier removal (CUDA, partly CPU)
  → point image / cloud with weights (CUDA + common.dll CPU geometry)
  → tracking: frame-to-model ICP + particle filter (CPU), texture ORB/BEBLID/DBoW2 (CPU+CUDA)
  → fusion: voxel-hash TSDF in Sn3DMagic (CUDA), multi-resolution cylinder TSDF (RealtimeScan)
  → mesh: CUDA marching cubes (commonGpu) / Poisson (offline)
  → texture: SIFT/SfM + ORB-SLAM + texture reprojection + colour correction (CPU/CUDA mix)
```

| # | stage | observed artefacts | confidence | open question |
|---|---|---|---|---|
| 1 | acquisition | 1280×1024 XML, three CCFs, FPGA trigger, `activeBox.dat` | **high** | scan-mode exposure/gain table |
| 2 | rectify + preprocess | LUT strings, `cvStereoRectify`/`cvRemap`, CUDA `rectify`, USM, `pyrdown_kernel` | **high** | LUT resolution, border policy |
| 3 | stereo / speckle decode | 251 kernels: census 5/7/9, ZNCC, 4-path aggregation, WTA, sub-pixel, LR, median/CCL; sparse centre/edge kernels; `semiGlobalMatch.cu` | **high** for existence; **medium** for branch selection | IR vs white-light routing; window/penalty defaults |
| 4 | depth + confidence | `dispary_to_depth`, `get_qMatrix`, weights, invalid/hole filters, tangent-plane/one-to-many filters | **high** | confidence formula, thresholds |
| 5 | point cloud / normals | `depth_to_point3d`, `normal_kernel`, `PointImage`/`CloudWithWeight` | **high** | weight semantics |
| 6 | tracking | CPU `IcpBaseOnCam`, `ParticleTrack`, 6×6 normal equations; texture ORB/BEBLID/DBoW2; optional CUDA ICP | **high** for ICP/particle/ORB; **medium** for which path runs | CPU vs CUDA ICP; iteration counts |
| 7 | fusion | Magic voxel-hash SDF/TSDF kernels; RealtimeScan cylinder multi-resolution TSDF; `FusionTsdfIns` | **high** | voxel size, truncation, weight rule |
| 8 | mesh | commonGpu `CUDAMarchingCubes`, `classifyVoxel`, `compactVoxels`; Poisson exports | **high** | live vs offline selection |
| 9 | texture | SFM + Ceres, SIFTGPU, ORB-SLAM/DBoW2, reprojection kernels, colour correction | **high** | atlas/blending policy |

Confidence in the **existence and ordering of these stages is high** (kernels, exports, debug file
names, stage markers). Confidence in **which exact branch is selected at runtime** is medium: the
selection is data-driven (`ScanType`, object type, IR vs white-light mode) and was not observed.

---

## 3. Stage-by-stage evidence

### 3.1 Acquisition

| claim | evidence | confidence |
|---|---|---|
| Images are 1280×1024 | `res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml` `<imgWidth>1280</imgWidth><imgHeight>1024</imgHeight>`; `Sn3DSpeckle.dll` off `0x1C2C90`: `ERROR: image size must be 1280*1024 or 2448*2048 or 2592*1944 or 1280*972.` | observed |
| Three monochrome cameras + colour camera | three CCF files (`LeftCCF.txt`, `RightCCF.txt`, `TexCCF.txt`); three dark/CCM sets (`ParaDark_0..2`, `ParaCCM_0..2`); `TexCCF` has its own 1571 px focal length, consistent with an RGB camera | observed |
| Per-camera gain/exposure are configurable in XML | `rapidCameraCalibrate.xml` `GE1 id=0 gain=400 exp=1500`, `id=1 gain=400 exp=1500`, `GE2 id=2 gain=180 exp=1000`; LED brightness 1000/300 in the `start` command | observed (calibration procedure, not necessarily scan defaults) |
| FPGA-timed triggering | `docs/firmware/format.md` (FX3/FPGA image, trigger threads); device facts supplied with the task | observed/derived from firmware analysis |
| Active area/bounding box file | `res/Einscan-E10/200x150/activeBox.dat` (48 B) | observed |

**Unknown:** the scan-mode exposure/gain table actually used during a handheld scan (the XML above is
the *calibration* command sequence). `res/config.db` would likely hold it but is encrypted (§7).

### 3.2 Rectification and pre-processing

| operation | evidence | confidence |
|---|---|---|
| Rectification via precomputed lookup tables | `Sn3DSpeckle.dll` strings `leftRectifyLookUpTable`, `rightRectifyLookUpTable`, `colorRectifyLookUpTable` around `0x1C3760–0x1C38F0`; `CaliParReadIn::get_rectify_map` / `get_rectify_map_run` exports | observed |
| OpenCV used to build maps | imports `cvStereoRectify`, `cvInitUndistortMap`, `cvRemap`, `cv::pyrDown`, `cv::remap`; `Sn3DSpeckle.dll` off `0x1C11C8`: `A.size() == Size(3, 3) && A.size() == R.size()` | observed |
| GPU rectification kernel | CUDA kernel `Sn3DAlgorithm::Sn3DSpeckleRebuild::SpePreProcess::rectify(PtrStepSz<uchar>, …)`; debug images `leftRect_Orig_%d.bmp`, `leftRect_USM_%d.bmp` off `0x1C6568`/`0x1C6598` | observed |
| Unsharp mask + Gaussian + pyramid down | kernels `unsharp_mask`, `gaussian_blur_USM`, `pyrdown_kernel`, `USM_for_pyramid`, `USM_and_pyramid_kernel`; debug strings `leftPyramid_%d.bmp` off `0x1BC090` | observed |
| Gamma / edge weight / sample ratio are parameters | `Sn3DSpeckle.dll` off `0x1BABA0` `gamma value =`, `0x1BAB68` `edgeWeight =`, `0x1BD4E0` `sampleRatio:`, `0x1BD4F8` `gradientTh:` | observed (names); values unknown |
| Calibration model is the 22-double Bouguet-style vector | `Sn3DSpeckle.dll` off `0x1C0D30`: `leftPar.size() != 22 || rightPar.size() != 22`; print labels off `0x1C0E18–0x1C0FD8`: `fc[2]`, `cc[2]`, `alpha`, `kc[5]`, `T[3]`, `R[9]`, `error` | observed |

**Calibration data caveat.** The shipped `*CCF.txt` files are 65 little-endian `double`s (520 B).
The first values of `LeftCCF` are consistent with `fx≈1179.2`, `fy≈1167.5`, `cx≈616.5`, `cy≈527.2`
(doubles at indices 0,1,4,5), but the remainder does not follow the 22-double print order and the
last 264 B decode as denormal/small-integer patterns (likely uninitialised tail of a fixed-size
serialisation). **The exact CCF field layout is unresolved**; treat intrinsics as approximate and
defer to the calibration workstream. `TexCCF` (33 doubles) is internally consistent
(`fx=fy≈1571 px`, `cx≈641`, `cy≈505`).

### 3.3 Stereo / speckle decoding

This is the most strongly evidenced stage. The module exports and CUDA kernel names expose the
algorithm almost completely.

#### 3.3.1 Dense path (SGM-like)

| element | evidence | confidence |
|---|---|---|
| Census transform 5×5, 7×7, 9×9 | kernels `CostCompute::kernel_census_transform_5x5UChar`, `…7x7Sample`, `…9x9Sample` (`Sn3DSpeckleFusion.dll`); `census_tranform_9X9` (`Sn3DSpeckle.dll`) | observed |
| ZNCC and census cost | `kernel_zncc_cost_fast`, `kernel_zncc_cost_impl_half<512,612>` etc.; `kernel_cnesus_cost_impl*` (vendor typo “cnesus”) | observed |
| Four-direction cost aggregation | kernels `kernel_cost_aggregate_{left2right,right2left,up2down,down2up}_impl<32,8,{4,8,16,32}>`; export methods `CostAggregateLeftToRight/RightToLeft/UpToDown/DownToUp`; `kernel_sum_cost_aggregation_path_4` | observed |
| WTA + sub-pixel | export `WTA_result`; `sub_pix_Match`, `inter_subPix`, `opti_base_sub_pix_match`, `multi_plan_sub_pix`, `subPixMatchKernrl::subPix_match`; debug `subPixMatchWin:` / `subPixCorrWin` labels | observed |
| Left/right consistency | `kernel_left_right_consistancy_check`; export `check_left_to_right_last`; debug `afterleftRightCheckConsis_`, `SGMAfterLeftRightCheck_` off `0x1CBC48`/`0x1C7028` | observed |
| Disparity limits | `Sn3DSpeckle.dll` off `0x1C7F97`: `disparity size must be less 1024`; off `0x1C7E70`: `Warrig: sgm disparity range is not match 4, change to [` | observed |
| Image-size gate | off `0x1C2C90`: only 1280×1024 / 2448×2048 / 2592×1944 | observed |
| Median and speckle filtering | kernels `kernel_median_filter<3>`, `<5>`, `sm_kernel_median_filter<3/5>` | observed |
| Connected-component island removal | `CudaUtil::kernel_ccl_*`; `SpeckleDataTypeGPUKernel::delete_island*`, `label_reduction`, `resolve_labels` | observed |
| SGM source file | `D:/Jenkins/workspace/…/pixMatchGPU/semiGlobalMatch.cu` off `0x1C7EE0` | observed |

#### 3.3.2 Sparse speckle-centre path

`Sn3DSpeckle.dll` also contains a full **speckle-feature** pipeline, distinct from dense SGM:

* `SpecklePointMatchGPUKernel::get_infrared_speckle_center_point`,
  `speckle_center_line_detect`, `speckle_center_point_match`,
  `speckle_edge_point_match`, `speckle_point_project_to_unit_image`,
  `delete_isolated_speckle_point`, `delete_one_match_multiple`,
  `delete_noisy_with_speckle_point_final`.
* Debug dumps `leftImageCenterPoint_%d.txt`, `rightImageCenterPoint_%d.txt`,
  `speckle_edge_and_point_result_%d.txt` (off `0x1CC6B8–0x1CC758`).

This path detects projected speckle centres/edges, matches them across the rectified pair, projects
them back to the unit image for a consistency test, and deletes duplicates/isolated points
[inferred from names; the call graph was not traced end-to-end].

#### 3.3.3 White-light variant

Parallel kernels carry the `_white_light` suffix
(`kernel_cost_aggregate_left2right_impl_white_light`, `white_light_subPix_match_impl_update`,
`white_light_left_right_check`, `pix_match_hair_impl_white_light`). The wrapper exposes
`WLSpeckle…` and `IRSpeckle…` controller topics in `App_EA.xml`
(`WLSpeckleTrackFusionProcessControllerTopic` line 1117,
`IRSpeckleTrackFusionProcessControllerTopic` line 1173), proving **two selectable illumination
modes**. Which mode uses SGM vs sparse centres is **not** proven; the naming suggests:
IR speckle → sparse centre/edge matching; white light → dense SGM.

#### 3.3.4 Phase/grating branch (separate product path)

`Sn3DPhaseBuild.dll` and `Sn3DGratingRecons.dll` expose:

* `cudaWrappedPhaseHeterodyne`, `cudaWrappedPhaseHeterodyne1/2`, `cudaWrappedPhase4500`,
  `cudaPhaseScaleChangeL/R/L3/R3/LM3/RM3`, `cudaPhaseInterpolat`, `cudaPhaseExpand`,
  `cudaDeletePhaseError1`, `cudaDeletePhaseErrorHDR` → multi-frequency heterodyne phase unwrapping
  with HDR phase-error rejection [observed kernel names; algorithm interpretation inferred].
* `cudaBuildStereo`, `cudaCorresFindRough`, `cudaCorresFindRefine`,
  `cudaCorresFindRoughWithGuide`, `cudaCorresFindRefineWithGuide` → projector-camera
  correspondence, coarse-to-fine, optionally guided [observed names].
* Parameter print labels: `param.frangeNum[0..2]` (`Sn3DPhaseBuild.dll` off `0x1BAC08–0x1BAC48`),
  `hdr_gauss_sigma` (`0x1BAD0E`), `hdr_gauss_window` (`0x1BAD3E`), `gauss_sigma2` (`0x1BAD6E`),
  `gauss_window2` (`0x1BAD96`) → three frequencies + HDR Gaussian parameters [observed names,
  values unknown].
* `CreateLookupTable` / `CreateLookupTableOptim` exports → projector LUT calibration [observed].

This branch **appears** to be a separate structured-light/grating path shipped for other Shining3D
products; there is no evidence that it runs in the Einstar IR-speckle mode [inferred]. It is documented because the task asks for the
full installation, but it should not be mixed into the Einstar baseline.

### 3.4 Depth, confidence and point cloud

| operation | evidence | confidence |
|---|---|---|
| Disparity → depth with Q matrix | export `get_qMatrix`, `globalMatchTest::dispary_to_depth`, `depthMap_generate_pointImage`, `depthImage_generate_pointImage` (`Sn3DSpeckleFusion.dll`) | observed |
| Depth range is a runtime parameter | exports `reset_depth_range(void*, float, float)` in both speckle modules; debug `findRange:` and `imageRange:` | observed |
| Per-point weight/confidence | `PointImage::set_vWeight/get_vWeight` (imported from `common.dll`); kernels `count_weight_with_depth_linear_pointImage`, `count_weight_with_depth_linear_cloudWithWeight`, `update_Point_weight` | observed |
| Invalid/hole handling | kernels `fill_disparity`, `fill_disparity_enhance`, `fill_point`, `fill_hold`, `save_invaild`, `delete_invaild_point`, `disparity_clear` | observed |
| Tangent-plane / one-to-many / through-plane filters | `delete_point_with_tanget_plane_distance`, `delete_one_match_multiple_point`, `delete_the_through_only_match`, export `reset_delete_distance_ratio_with_tanget_plane(float)` | observed |
| Normals | `normal_kernel`, `normal_kernel_with_normal`, `normal_generate_cross/normal`, `computeNmapKernelEigen` (`commonGpu.dll`) | observed |
| Point cloud container | `Sn3DAlgorithm::PointImage`, `Cloud`, `CloudWithWeight`, `RigidMatrix`, `CameraInParam` exported by `common.dll`; `.asc`/`.txt` debug writers | observed |

Typical debug dump order in `Sn3DSpeckle.dll` (offsets `0x1BC900–0x1BF5F8`, `0x1CC618–0x1CC8E8`):

```
leftRectify / rightRectify
→ leftPyramid / rightPyramid
→ pixMatchPyramid / depthValuePyramid…
→ pointResutlBeforSubPix → pointResutlAfterSubPix
→ after remove_nosie_with_parallax_consistency
→ after right_to_left_to_vertify_match
→ after remove_isolated_island
→ after smooth normal
→ speckle_point_result_final
```

This is **observed naming order**, not a proven execution order; it strongly supports the stage
sequence in §2 [derived].

### 3.5 Tracking

`Sn3DGeometricTrackFusion.dll` (302 exports, no fatbin) is the CPU tracking core:

| element | evidence | confidence |
|---|---|---|
| Point-to-plane ICP against a fused/reference cloud | `IcpBaseOnCam::run_one_icp`, `find_correspond`, `get_corrPoint`, `get_mat`, `getmat_parallel`, `get_iterRT(RigidMatrix&, Matrix<double,6,6>, Matrix<double,6,1>)`, `computer_disAvg`, `count_residual` | observed |
| 6×6 normal-equation solve (SVD/eigen) | `get_iterRT` signature with `Eigen::Matrix<double,6,6>` and `Eigen::Matrix<double,6,1>`; `fastSVD` kernel in `Sn3DMagic.dll` | observed/derived |
| Robust gating | strings `corrThresholdCloudFix =` (`0x144E68`), `corrThresholdRaCloudMove =` (`0x144E8A`), `icp False, because disAvg > disThread`, `icp False, vaildCorrNum too small`, `covMat.determinant() is too small` | observed names |
| ICP iteration counter is printed | `ICP iter=` off `0x1464A0` | observed (value unknown) |
| Particle-filter pose tracking | class `Track::ParticleTrack`: `generate_partical`, `generate_rotation_partical`, `generate_sparse_partical`, `generate_supplementary_particle`, `track_supplementary_particle`, `count_residual`, `get_result`; string `partical_track_pyramid` off `0x147F3D` | observed |
| Frame-to-model / model-to-frame update | `fusion_and_track`, `track`, `track_pre_frame`, `fusion_and_track_with_speed_limit`, `check_track_is_stable` | observed |
| Texture tracking | `Sn3DTextureBasedTrack.dll` imports `Sn3DTextureSlam.dll`; `Sn3DRealtimeScan.dll` imports both; `fusion_and_track(ColorAndPoint…)` and `TexFusionTrackParam` overloads | observed |
| ORB + BEBLID + DBoW2 SLAM | `Sn3DTextureSlam.dll`: classes `ORBextractor`, `ORBExtractorFAST`, methods `computeOrbDescriptor`, `computeBEBLID`, `compute_keypoints_octTree`, `HarrisResponses`, `img_enhance`; `DBoW2::Result`, `DBoW2::FORB`, `ts_loopdetect`, `ts_loopupdate`, `ts_localopot`, `ts_relocateframe`, `ts_trackframe`; strings at `0x1C9CD3` (`loopdetect`), `0x1D5003` (`ORBextractor`), `0x23BFF3` (`BEBLID`), `0x2358F7` (`DBoW2`) | observed |
| GPU ORB matching/extraction | 7 kernels: `matchUnrolledCached<16,64,HammingDist>`, `calcKeypoints`, `nonmaxSuppression`, `select_by_topN`, `select_by_flag`, `norm_mass_center`, `usm_enhance` | observed |
| Global registration | `Sn3DRegistration.dll` 1729 exports, Ceres 1.12, `CloudPairRegistrationWithCheck`, `robust_triMesh_icp`, `multi_TriMesh_icp`, `KMeansTree` (FLANN), `featureWeights`, `maxRadius`/`minRadius`/`searchRadius`/`convergence` | observed |
| Loop closure / pose graph | `Sn3DTextureSlam.dll` exports `ts_loopdetect`/`ts_loopupdate`/`ts_localopot`; DBoW2 vocabulary; `Sn3DTextureUtil.dll` SIFT vocabulary (`util_create_voc`, `SiftVocOptions`, `util_add_voc`) | observed |

The **exported/default ICP orchestration is CPU** in `Sn3DGeometricTrackFusion.dll` (no fatbin), but a CUDA ICP path exists:
`IcpBaseOnCamCuda` strings in the same module (`0x154370–0x154538`) and GPU matrix/SVD kernels in
`Sn3DMagic.dll` (`find_coors_mat_kernel`, `get_mat`, `get_mat_p2n`, `fastSVD`, `getRT_from_VU`).
Which one runs per frame is **unknown**; the CPU path is the one with the exported orchestration.

### 3.6 Fusion

The **real-time speckle fusion helper** is implemented in **`Sn3DMagic.dll`**; `Sn3DRealtimeScan.dll` orchestrates it and also exposes its own multi-resolution/cylinder TSDF API:

| element | evidence | confidence |
|---|---|---|
| Real-time speckle fusion helper | exports/strings `RealTimeFusionSpeckleHelper` (`0x465C57`), `RealTimeFusionCloudHelperCuda::fusion_mesh` (`0x46602F`), `fusion_one_speckle`, `ray_casting`, `get_cloud` | observed |
| Voxel-hash SDF/TSDF | recovered PTX entry names: `resetHashKernel`, `addHashDataAndSdf`, `addBulkHashDataAndSdf`, `updateSdfKernel<UpDevicePkg>`, `updateSdfKernelImg`, `getSdfDataKernel`, `sdfInterPos`, `resetHashBucketMutexKernel`, `compactHashId`, `clearHashDataAndCalHashValue`, `mapHashOnFrame`, `deleteHashEntryData`, `recoverHashDataNoHostIdChange`. The fatbin stores compressed PTX (no ELF section names); the curated dump lists 27 intact entry symbols, and the remaining names were recovered by a full-file `_Z…` scan of the same PTX text | observed |
| Hash data structure | `Sn3DGeometricTrackFusion.dll` exports `HashData::SDFBlockToVirtualVoxelPos`, `SDFBlockToWorld`, `delinearizeVoxelIndex`, `addVoxel`, `getVoxel`, `deleteVoxel`, `Hilbert2::_hilbert2` | observed |
| Depth + colour integration | kernels `imageFusionKernel<imageFusionCuda…>`, `imageGetCloudKernel`, `imageAllocKernel`, `updateSdfKernelImg`; colour fusion class `ColorFusion::color_interp_bgr` | observed |
| Multi-resolution cylinder TSDF | `Sn3DRealtimeScan.dll` exports `integrate_tsdfvolume_cylinder_density_muli_octree`, `integrate_tsdfvolume_cylinder_multi_frame_muli_octree`, `FusionTsdfIns`, `FusionTsdfMuliResolution<MultiLeafTsdf>`, `ini_muli_octree`, `create_leaf_at_specified_level_muli_octree` | observed |
| Multi-resolution parameters (names only) | `Sn3DRealtimeScan.dll` off `0x61661A–0x616792`: `upsampleWeightThr`, `tsdfWeightThr`, `resolutionLevel`, `maxCylinderRadius`, `minCylinderRadius`, `curvature_threshold`; `fusionRange` off `0x616E58` | observed names; **values unknown** |
| Fusion step markers | off `0x617B40`: `5. compute tsdf`; off `0x617B90`: `7. marching cubes` | observed |
| Voxel size and double-voxel flags | `Sn3DMagic.dll` off `0x465BD8` `voxelDis`, `0x465698` `useDoubleVoxel`, `0x4656A8` `useFusionBorder`, `0x4656B8` `minFusionRadius`, `0x466758` `maxVoxel`, `0x466748` `usedVoxel =` | observed names; **values unknown** |
| Memory management | `get_voxel_usage`, `get_leaf_use_mem`, `get_generate_mesh_mem`, `get_is_enough_mem_for_gen_cld`, `is_enough_mem`; `gpu mem is not enough!` off `0x4660D8` | observed |

`Sn3DRealtimeScan.dll` itself has **no fatbin** but contains 322 static cudart API strings and
`IcpBaseOnCamCuda`; the actual TSDF/mesh kernels live in `Sn3DMagic.dll` and `commonGpu.dll`.
`Sn3DRealtimeScan` calls `RealTimeFusionSpeckleHelper::~…` and `get_cloud` from `Sn3DMagic`
(import table), confirming the dependency direction [observed].

### 3.7 Mesh extraction

| element | evidence | confidence |
|---|---|---|
| CUDA marching cubes | `commonGpu.dll`: `CUDAMarchingCubes` (`0x8A8EE`), `classifyVoxel<0,0>/<0,1>/<1,0>/<1,1>`, `compactVoxels`, `generateTriangleVerticesSMEM`, `generateTriangleNormals`, `computeTriangleAreas`, `FastSweepKernel` | observed |
| Mesh from TSDF | `Sn3DMagic.dll` kernels `getWholeMeshKernel`, `getPartialMeshKernel`, `getWholeMeshAndPkgKernel`, `rebuildVertCoordAndSDF`, `calculateMeshVnAndFn`, `cudaMeshcullingCompressKernel`, `updateMeshTopoPackage` | observed |
| Mesh data type | `BaseTriMesh` in `common.dll`; `Sn3DRealtimeScan` exports `get_mesh`, `get_show_cloud_mesh`, `mesh_normal_opt`, `fusion_for_mesh` | observed |
| Poisson (offline / high quality) | `Sn3DRealtimeScan` exports `poisson_memory_estimate_octree`, `fusion_multigrid`, `run_cylinder_fusion_density_tbb_poisson_initialize` strings; `Sn3DRealtimeScan` off `0x619228` `inside_get_tsdf_poisson` | observed names; algorithm use inferred |

**Unknown:** the default meshing path for a normal handheld scan (marching cubes is proven present
and is the obvious live path; Poisson is proven present but its selection policy is not).

### 3.8 Texture / colour

| element | evidence | confidence |
|---|---|---|
| Structure-from-motion with Ceres | `Sn3DTextureSFM.dll` exports `sfm_gm_sparse_rebuild`, `sfm_tm_auto_run`, `TextureSFMParam`, `sfm_tm_register_flag`; Ceres 1.12 path; `Bundle adjustment report`, `Global bundle adjustment` strings | observed |
| SIFT vocabulary / DBoW2 | `Sn3DTextureUtil.dll` exports `util_create_voc`, `util_add_voc`, `util_feature_voc`, `SiftDatas`, `SiftVocOptions`; `Sn3DTextureSlam` DBoW2 | observed |
| SIFTGPU | `Sn3DTextureSFM.dll` off `0x4AB6D0`: `OpenCL not supported in this binary! Define CL_CUDA_SIFTGPU_ENABLED to..`; fatbin 171 KB | observed |
| Texture reprojection / fusion | `Sn3DRealtimeScan` exports `texFusionTrack`, `cloud_paint_color`, `model_reproject_for_point_color_multi_times`, `get_fusion_vertex_coloring_teeth_mesh`; `Sn3DCloudProcessGPU` exports `cloud_reproject_for_point_color_multi_times_GPU` | observed |
| Colour correction | `Sn3DColorCorrect.dll` 64 kernels: `cuda_color_correct`, `kernel_color_correct`, `kernel_color_correct3`, `kernel_bilatFilter`, `kernel_cal_guideFilter_coef/output`, `color2Lab`, `kernel_gama`, `kernel_find_Expo`, `kernel_get_overExp`, `kernel_opt_overExp_hor/ver`, `kernel_uniformization` | observed |
| Calibration colour data | `ParaCCM_0/1/2.txt` = 15 float32 (a 3×5 colour-correction matrix; values ≈ 1.78/−0.28/…), `wbdata.txt` = 18 float32 (white balance), `ParaDark_*` = 3 float32 (black level ≈0.045) | observed (float decode) |
| Texture camera gain/exposure helper | `Sn3DTexCameraHelper.dll` exports `get2XTexCameraGainMaxValue`, `get2XTexCameraScanGainValue`, `get2XTexCameraGainMinValue` | observed |

`Sn3DTextureMapper.dll` is an 83 KB Qt plug-in (2 exports, `qt_plugin_instance`) and is **not** the
texture-mapping algorithm; the real mapping/reprojection is in `Sn3DRealtimeScan` and
`Sn3DCloudProcessGPU` [observed].

---

## 4. Numeric parameters: proven, named-but-unknown, guessed

### 4.1 Proven values

| parameter | value | evidence |
|---|---|---|
| Image size | **1280 × 1024** | `rapidCameraCalibrate.xml`; `Sn3DSpeckle.dll` `0x1C2C90` |
| Supported image sizes (speckle core) | 1280×1024, 2448×2048, 2592×1944, 1280×972 | `Sn3DSpeckle.dll` `0x1C2C90` |
| Maximum disparity | **< 1024 px** | `Sn3DSpeckle.dll` `0x1C7F97` |
| SGM disparity alignment | warning says the range must be a multiple of **4** (inferred from the warning text) | `Sn3DSpeckle.dll` `0x1C7E70` |
| Census windows | **5×5, 7×7, 9×9** | kernel names in `Sn3DSpeckle`/`Sn3DSpeckleFusion` |
| Median windows | **3×3, 5×5** | `kernel_median_filter<3>`, `<5>` |
| Cost-aggregation paths | **4** (L→R, R→L, U→D, D→U) | kernel names; `kernel_sum_cost_aggregation_path_4` |
| Cost aggregation tile parameters | template args `<32,8,{4,8,16,32}>` — likely block/tile geometry, not proven semantics | mangled template args |
| Calibration parameter vector | **22 doubles**: `fc[2] cc[2] alpha kc[5] T[3] R[9]` | `Sn3DSpeckle.dll` `0x1C0D30`, print labels `0x1C0E18–0x1C0FD8` |
| Calibration camera 0 (approx.) | `fx≈1179.2`, `fy≈1167.5`, `cx≈616.5`, `cy≈527.2` | `LeftCCF.txt` doubles 0,1,4,5 (`sources/calibration/ccf_200x150/LeftCCF.txt`) |
| Calibration camera 1 (approx.) | `fx≈1167.6`, `fy≈1156.2`, `cx≈638.1`, `cy≈546.6` | `RightCCF.txt` doubles 0,1,4,5 |
| Texture camera | `fx=fy≈1571 px`, `cx≈641.3`, `cy≈505.3` | `TexCCF.txt` doubles 0,1,4,5 |
| Calibration acquisition exposure/gain | cam0/1 gain 400, exp 1500; cam2 gain 180, exp 1000 | `rapidCameraCalibrate.xml` |
| CUDA toolkit / PTX | CUDA 10.2.89, PTX ISA **6.5** | `cudart64_102.dll`; `.version 6.5` strings |
| CUDA targets present | **sm_50** (`Sn3DSpeckle`), **sm_30** (Phase/commonGpu), a compressed `sm_7*` PTX variant in Speckle | `.target` strings; fatbin magic `0xBA55ED50` |
| Ceres version | **1.12.0** | path in `Sn3DRegistration.dll` `0x4921A0` |
| OpenCV | **3.4.8** CPU | imports + banner |
| ORB descriptors | ORB extractor + Hamming matching kernel `matchUnrolledCached<16,64>` (standard ORB is 32 B; template semantics not proven) | kernel/export names |
| BEBLID | present | `computeBEBLID`; string `0x23BFF3` |
| Colour matrix | 3×5 float32 per camera | `ParaCCM_*.txt` (15 floats) |

### 4.2 Named parameters whose values are not in strings

These are real runtime knobs proven by exported setters/print labels; the **numeric defaults are not
stored as printable strings** and were not recovered without executing the code:

| parameter | where named | meaning (inferred) |
|---|---|---|
| `findRange` | `Sn3DSpeckle` `0x1BD510`, `set_findRange(Eigen::Matrix<int,2,1>)` | disparity search range |
| `pixMatchWin` | `0x1BD528` | dense matching window |
| `subPixMatchWin` | `0x1BD540` | sub-pixel matching window |
| `imageRange` | `0x1BD558` | image/active range |
| `sampleRatio` | `0x1BD4E0` | pyramid/downsample ratio |
| `gradientTh` | `0x1BD4F8` | gradient threshold for speckle centres |
| `edgeWeight` | `0x1BAB68` | edge weighting |
| `gamma value` | `0x1BABA0` | radiometric gamma |
| `_baseLineD` | `0x1BD3A0` | rectified baseline |
| `subPixCorrWin` | `0x1BC020` | sub-pixel correlation window |
| `corrThresholdCloudFix`, `corrThresholdRaCloudMove` | `Sn3DGeometricTrackFusion` `0x144E68`, `0x144E8A` | ICP correspondence distance gates |
| `disThreshold` | `0x14D8B0` | ICP distance threshold |
| `vertexsNumThreshold` | `0x149368` | minimum overlap for stable tracking |
| `ICP iter=` | `0x1464A0` | actual iteration count (runtime log) |
| `voxelDis` | `Sn3DMagic` `0x465BD8` | TSDF voxel size |
| `minFusionRadius`, `maxVoxel`, `useDoubleVoxel`, `useFusionBorder` | `0x4656B8`, `0x466758`, `0x465698`, `0x4656A8` | fusion volume / memory policy |
| `resolutionLevel`, `upsampleWeightThr`, `tsdfWeightThr`, `maxCylinderRadius`, `minCylinderRadius` | `Sn3DRealtimeScan` `0x6166BA`, `0x61661A`, `0x616642`, `0x6166F2`, `0x61670A` | multi-resolution cylinder TSDF |
| `curvature_threshold`, `fusionRange` | `0x616792`, `0x616E58` | mesh/fusion filtering |
| `frangeNum[0..2]`, `hdr_gauss_sigma`, `hdr_gauss_window`, `gauss_sigma2`, `gauss_window2` | `Sn3DPhaseBuild` `0x1BAC08–0x1BAD96` | phase frequencies + Gaussian filters |
| `maxRadius`, `minRadius`, `searchRadius`, `convergence`, `featureRadius`, `adjDisThreShold` | `Sn3DRegistration` `0x6AE130`, `0x6AD1E0`, `0x6DDD38`, `0x6DDE68` | ICP/search options |
| `k1..k5`, `p1,p2` (model) | print label `kc = [%lf ×5]` | distortion model; actual coefficients in CCF (layout unresolved) |

### 4.3 Guesses (explicitly speculative)

* **Voxel size** for the live Einstar scan is likely in the **0.3–1.0 mm** range, driven by the
  claimed 0.1 mm accuracy and the `200×150` field of view. **Not proven.**
* **Pyramid levels** are likely 3–4 (debug files show `_%d` levels and `depthValuePyramidRefine1/2/3`),
  but the count is data-dependent. **Not proven.**
* **ICP iterations** are likely 5–15 per frame (real-time budget) and the code prints `ICP iter=`.
  **Not proven.**
* The Einstar’s live depth range is likely ~200–1500 mm; `reset_depth_range` exists but no value is
  embedded. **Not proven.**

### 4.4 Calibration file (CCF) — what is actually known

* 520 B = **65 little-endian doubles** (`LeftCCF`, `RightCCF`); `TexCCF` = 33 doubles.
* First 8 values of `LeftCCF`: `[1179.161, 1167.470, 0, 0, 616.485, 527.151, 0, 0]`.
  These are too close to plausible `fx, fy, cx, cy` to be accidental, but the interleaved zeros and
  the non-physical values at indices 8–31 (`-2.0001`, `6.1699`, `13.029`, …) mean the **field order is
  not the 22-double print order**.
* The last 264 B decode as denormal doubles whose bit patterns are small integers
  (`1,4,9,8,2,5,…`), consistent with an **uninitialised tail of a fixed-size serialisation**, not
  encrypted data.
* The 22-double model is proven by `leftPar.size() != 22` and the print labels; the mapping from CCF
  bytes to those 22 fields remains **unknown**. Do not use CCF extrinsics until decoded.

---

## 5. Acceleration: what is actually present

### 5.1 CUDA, not TensorRT/cuBLAS/OpenCL

| mechanism | evidence | verdict |
|---|---|---|
| CUDA runtime (static) | 322 `cuda*` API strings per CUDA module; `cudaLaunchKernel_ptsz` `Sn3DSpeckle` `0x1CEB50`; no `cudart64_102.dll` import | **present** |
| CUDA driver API (dynamic) | `nvcuda.dll` `0x1CFEC0`, `cuLaunchKernel` `0x1D0C68`, `cuCtx*`, `cuMem*` strings | **present** |
| CUDA fatbins | `.nv_fatb` sections: 30.7 MB (`Speckle`), 26.1 MB (`SpeckleFusion`), 1.56 MB (`Magic`), 415 KB (`commonGpu`); fatbin magic `50 ED 55 BA` at `Sn3DMagic` `0x5D1000` | **present** |
| PTX / cubins | `.version 6.5`, `.target sm_50`, `.target sm_30`; 84 embedded ELF images in `Sn3DSpeckle` fatbin | **present** |
| Thrust/CUB | `thrust::cuda_cub::…`, `cub::DeviceRadixSort…`, `cub::ScanTileState…` kernels | **present** |
| cuBLAS | no `cublas*` import/string in any reconstruction module | **absent** |
| cuDNN | no `cudnn*` | **absent** |
| TensorRT | no `nvinfer*`/`tensorrt` | **absent** |
| OpenCL | only SIFTGPU diagnostic string; no OpenCL API imports | **absent** |
| OpenCV CUDA | no `opencv_cuda*` imports | **absent** |
| OpenMP | `VCOMP140.DLL` imports (`_vcomp_*`, `omp_*`) | present, CPU |
| TBB | `tbb.dll` imports in most modules | present, CPU |
| Eigen | header-only, mangled `Eigen::Matrix` everywhere | present, CPU |
| Ceres | `ceres.dll` imports, 1.12.0 | present, CPU |
| FLANN | static `KMeansTree`/`KDTree` strings; no DLL | present, CPU |
| OpenMesh | `OpenMeshCore.dll` in registration/realtime | present, CPU |

### 5.2 Kernel counts and what they prove

| module | kernels | dominant evidence |
|---|---:|---|
| `Sn3DSpeckle.dll` | 251 | rectification, census/ZNCC, 4-path SGM, WTA, sub-pixel, LR check, median, CCL, normals, speckle centres |
| `Sn3DSpeckleFusion.dll` | 230 | same family + depth-image fusion, colour/image conversion |
| `Sn3DPhaseBuild.dll` | 134 | heterodyne phase unwrap, LUT, correspondence rough/refine, phase-error/HDR |
| `Sn3DGratingRecons.dll` | 71 | phase-build GPU kernels reused (same `Sn3DPhaseBuildGpu` namespace) |
| `commonGpu.dll` | 71 | CUDA marching cubes, CCL, normals, mesh ops |
| `Sn3DColorCorrect.dll` | 64 | bilateral/guided filter, gamma, over-exposure, Lab, uniformisation |
| `algorithmLzy.dll` | 811 | TSDF, ICP RMSE, laser line extraction, normal fitting (shared/OEM) |
| `Sn3DTextureSlam.dll` | 7 | ORB keypoints, non-max suppression, Hamming matching, USM |
| `Sn3DMagic.dll` | 0 symbol names (compressed PTX) | voxel-hash TSDF + mesh kernels recovered from PTX: `addHashDataAndSdf`, `updateSdfKernel`, `getWholeMeshKernel`, `fastSVD`, `imageFusionKernel` |
| `Sn3DRealtimeScan/Registration/GeometricTrackFusion` | 0 | CPU orchestration/tracking; static cudart strings only, no fatbin |

### 5.3 GPU vs CPU by stage

| stage | GPU | CPU | evidence |
|---|---|---|---|
| Rectify / undistort / USM / pyramid | **yes** | map construction | `SpePreProcess::rectify`, `unsharp_mask`, `pyrdown_kernel`; OpenCV `cvStereoRectify`/`cvInitUndistortMap` |
| Dense stereo (census/ZNCC/SGM/WTA/subpixel/LR/median/CCL) | **yes** | — | 251 kernels; `semiGlobalMatch.cu` |
| Sparse speckle centres/edges | **yes** | — | `SpecklePointMatchGPUKernel::*` |
| Phase unwrap / correspondence | **yes** | LUT setup | 134 kernels |
| Depth→point image / normals / weights | **yes** | container ops | `depth_to_point3d`, `normal_kernel`, `count_weight_*` |
| ICP / particle filter | optional (`IcpBaseOnCamCuda`, Magic `fastSVD`) | **default path** | `Sn3DGeometricTrackFusion` no fatbin; exports CPU ICP/particle |
| Texture ORB/BEBLID/DBoW2 | feature extraction/matching partly GPU (7 kernels) | loop closure, relocalisation, pose graph | `Sn3DTextureSlam` kernels + DBoW2 |
| TSDF fusion | **yes** (Magic, voxel hash) | multi-resolution cylinder orchestration | Magic PTX kernels; `RealtimeScan` exports |
| Marching cubes | **yes** (commonGpu) | — | `CUDAMarchingCubes`, `classifyVoxel`, `compactVoxels` |
| Poisson | — | **yes** | `poisson_memory_estimate_octree`, `run_cylinder_fusion_density_tbb_poisson_initialize` |
| SfM bundle adjustment | SIFTGPU (CUDA) | **Ceres CPU** | `CL_CUDA_SIFTGPU_ENABLED` string; Ceres 1.12 |
| Colour correction | **yes** | CCM/white-balance data | 64 kernels; `ParaCCM`/`wbdata` |
| Texture reprojection | **yes** | mesh/UV setup | `cloud_reproject_for_point_color_multi_times_GPU` |

---

## 6. Pipeline control and configuration

### 6.1 App_EA.xml

`App_EA.xml` is a **message-bus/topic manifest**, not an algorithm parameter file. It contains:

* `config` block: `./res/config.db`, password `2021shining3D` (plaintext, as shipped), table `config`
  (lines 5–12);
* 71 topics, e.g. `IRSpeckleTrackFusionProcessControllerTopic` (line 1173),
  `WLSpeckleTrackFusionProcessControllerTopic` (line 1117),
  `SequenceTrackFusionProcessController` (line 1247), `MeshGenerate` (line 2382),
  `MeshGenerateRet` (line 2506), `Sn3DGlobalRegister`, `GenerateDataProcess`;
* `libpath` entries for the controller plug-ins.

This proves the **control-plane split**: scan controllers publish frames/commands over a
QtTunnel/MQTT-like bus; reconstruction runs in worker processes/plug-ins. It does **not** expose
algorithm tuning values.

### 6.2 `res/config.db`

* 204,800 B (50 × 4096), entropy 7.98 bit/byte, header `e9 56 dc 1d 2f aa 3e 44 …` — no SQLite
  header and no SQLCipher page-1 header decrypted with the shipped password under the tested KDF variants
  (tested PBKDF2-SHA1/SHA512/SHA256/MD5, 1–256,000 iterations, page 1024/4096, reserves 16–84 B,
  UTF-8/UTF-16 passwords). **Encrypted with a key not present in `App_EA.xml`**, or a custom container.
* Therefore **config keys inside it remain unknown**. The plaintext password is evidence that the
  app framework *intended* SQLCipher, but this particular DB does not open with it.

### 6.3 Other config files

| file | content | relevance |
|---|---|---|
| `config/SystemCfg.json` | plug-in dirs, tunnel port 1883, log level, shared-memory size 2 GiB | control plane |
| `config/toolcfg.cfg` | device model `9m-sd`, language, paths, support emails | UI only |
| `config/featureConfig.ini` | dot/arrow/plane colours, `planeMinThresholdValue=0.01` | UI only |
| `ScanSets/scansetting.ini` | language/first-run only | **no scan parameters** |
| `res/calibrateConfig/Einscan-EA/rapidCameraCalibrate.xml` | 1280×1024, gains/exposures, LED/IR brightness, calibration command sequence | calibration/acquisition |
| `res/calibrateConfig/Einscan-EA/wbCalibrate.xml` | white-balance command sequence | colour calibration |
| `res/Einscan-E10/200x150/ParaCCM_*.txt` | 3×5 float32 CCM | colour |
| `res/Einscan-E10/200x150/ParaDark_*.txt` | 3 float32 black level ≈0.045 | colour |
| `res/Einscan-E10/200x150/wbdata*.txt` | 18 float32 white balance | colour |

---

## 7. What this survey does **not** prove

1. **Runtime branch selection** (IR speckle vs white light; dense SGM vs sparse centres; MC vs Poisson;
   CPU vs CUDA ICP). The code contains all branches; no execution was observed.
2. **Numeric defaults** for `findRange`, windows, voxel size, ICP thresholds, pyramid levels, depth
   range. They are runtime data, not printable strings; the parameter structs are passed in from the
   wrapper/UI.
3. **CCF field layout** and exact extrinsics/baseline. The 22-double model is proven; the file mapping
   is not.
4. **`config.db` contents.** Encrypted; password does not open it.
5. **GPU memory budget actually used.** Magic exposes `maxVoxel`, `get_leaf_use_mem`,
   `get_generate_mesh_mem`, `gpu mem is not enough!`, but no numeric budget is embedded.
6. **Performance.** No timing strings with values; `infar_pre_process cost`, `infar_pix_match cost`,
   `infar_subPix_match cost` are log labels only.

### Discriminating experiments (when hardware/software is available)

| question | experiment |
|---|---|
| which stereo branch runs | run a scan with the IR projector on/off and with white-light mode; capture the debug dump names (`leftImageCenterPoint_*` vs `pixMatchPyramid_*`) or attach a debugger to `speckle_rebuild_infar` vs `speckle_rebuild_white_light` |
| numeric defaults | breakpoint `speckle_rebuild_ini`/`globalMatchTest::ini` and dump `SpeckleRebuildInfarParameter`; breakpoint `ini_fusion` and dump `FusionParameter`; log `ICP iter=` |
| voxel size | breakpoint `RealTimeFusionSpeckleHelper` construction; read `voxelDis`; or infer from mesh vertex spacing at known scale |
| config.db | run the vendor app under a debugger at `sqlite3_key`/`sqlcipher` and capture the key; or dump the decrypted pages from process memory |
| CCF layout | breakpoint `CaliParReadIn::Read_Camera_Para` and dump the 22 doubles after parsing; compare with CCF bytes |
| GPU/CPU split | Nsight Systems trace of a scan; CUDA kernel timeline vs CPU threads |
| accuracy | scan a calibrated sphere/step gauge; compare against the vendor’s reported accuracy |

---

## 8. Reproduction

```bash
python3 tools/analysis/survey_modules.py \
  --root "<EXStar-install>/Shining3d/EXStar" \
  --out analysis/gnu/reconstruction_strings \
  --json analysis/gnu/reconstruction_survey.json
```

This writes, per module:

* `<Module>.imports.txt` — DLL → function list, MSVC-demangled where applicable;
* `<Module>.exports.txt` — RVA, ordinal, mangled name, demangled signature;
* `<Module>.cuda.txt` — `.nv_fatb` offset/size, embedded ELF count, demangled kernel names, driver API;
* `<Module>.curated_strings.txt` — offset, encoding, keyword categories, string;
* `<Module>.params.txt` — strings containing numeric tokens and a tuning keyword.

Demangling uses `llvm-undname` for MSVC names and `c++filt` for Itanium (CUDA) names. The script
does not execute any vendor binary and does not write to the vendor tree.
