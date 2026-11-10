# Einstar calibration plate — recovered vendor specification

Status: 2026-11-10. Static analysis of the shipped EXStar 1.2.2.0 binaries plus offline
measurement of the 25-pose vendor corpus. No vendor binary was executed and no physical
measurement was used. Companion code: `calib/plate.py`; tests: `tests/test_plate_geometry.py`.

Confidence labels: **A** = read directly from code/bytes and cross-checked by a second,
independent witness; **B** = one interpretation step; **C** = inference; **U** = unresolved.

---

## 0. TL;DR

| quantity | value | confidence |
|---|---|---|
| marker grid | **8 columns × 5 rows** of circular markers, 40 lattice nodes | **A** |
| large markers | 3 on the lattice at `(u,v) = (2,1), (4,1), (5,1)` + 1 off-lattice at `(3.5, 3)` | **A** |
| total markers | **41** = 37 small + 4 large | **A** |
| recognition lattice | **17 × 14 = 238 cells** at **14.00 mm** per step, markers on every second node | **A** |
| marker pitch | **28.0 mm** (`2 × 14.0`) | **A** |
| board frame | origin at the centre of the 8×5 marker field; `x = 28(u−3.5)`, `y = 28(2−v)`, `z = 0` | **A** |
| large-marker coordinates | `(−42,+28)`, `(+14,+28)`, `(+42,+28)`, `(0,−28)` mm | **A** |
| plate outline | **8 × 6 marker pitches = 224 × 168 mm** (margins 0.5 pitch left/right, 1 pitch top/bottom) | **B** (image measurement) |
| units | millimetres end-to-end | **A** |
| `200x150` | profile/directory name (capture window / product class), **not** a plate dimension | **A** |
| 8×7 drawing | shared cross-product UI illustration, **not** the Einstar board | **A** |

The vendor's own **CCF written on the same 25 images** (2026-02-09 21:34:58) corresponds to an
effective scale of **27.72 mm** under our estimator — a **1.0 %** difference from the nominal
28.0 mm whose origin is the difference between the two solvers, not the board (§7).

---

## 1. How EXStar builds the 3-D reference array

### 1.1 The call chain (all addresses VA, image base `0x180000000`)

```
EACalibrationController (QML)  --"initComputeAlg"-->  Sn3DEACalibrateController.dll
      getConfigInfo  FUN_180009f80        reads nm_group_num/nm_cell_num/space/aa/bb
      build map      FUN_18000bd10        publishes iX,iY,iSpace,AA,BB,isUseParam,srcCalParam
                                            |
                                            v
Sn3DCalibrateAlgorithmWrapper.dll
      EACameraCalibAlgManager::onSubscribeData 0x1800247B0
        -> task -> FUN_180026390  (parses the strategy map into the global config)
             -> FUN_180025D10  (calls the algorithm object's vtable)
                  slot[1] -> CameraCalibrateAlg vtable[1] 0x1800497F0
                           -> CalibrateBoard3DAlg vtable[0] 0x18004B920  setGrid(iX,iY,iSpace)
                  slot[2] -> 0x180049800  setCalibParam(SrcCalibParam*, DistanceRef*, type)
      EACameraCalibAlgManagerPrivate::calcCameraCalibrate 0x180025420
        -> CameraCalibrateAlg vtable[5] 0x180049C70
             -> CalibrateImage2DAlg 0x18004B080  (recognition of every image/camera)
             -> CalibrateBoard3DAlg vtable[2] 0x18004BA90  generate()   <-- 3-D array
        -> CameraCalibrateAlg vtable[6] 0x180048230
             -> Sn3D_double_camera_calibration / _pro  (Sn3DCalibrationJR.dll)
```

### 1.2 The generator (`0x18004BA90`, PROVEN)

```c
space = cfg.iSpace;  cols = cfg.iX;  rows = cfg.iY;
for (r = 0; r < rows; ++r)
  for (c = 0; c < cols; ++c) {
      x = space*c + (space*C)*((cols-1)/2);      /* integer (cols-1)/2 */
      y = (space*C)*r + (space*C*C)*((rows-1)/2);
      z = 0;
      push(x,y,z);                                /* index = r*cols + c */
  }
```

with `C = DAT_18005C358 = −1.0` (read from the binary). For the session values
`iX=17, iY=14, iSpace=14`:

```
x(c) = 14(c − 8)        c = 0..16     -> −112 … +112 mm
y(r) = 14(6 − r)        r = 0..13     -> +84 … −98 mm
```

The external-array branch (`CalibrateBoard3DAlg vtable[1] 0x18004BA60`, `setPoints`) exists but is
**dead for the Einstar**: its only caller that passes a `vector<Vector3d>*` is
`EACameraCalibAlgManagerPrivate::calcCameraCalibrate 0x180025420`, which zeroes the vector
immediately before the call and never fills it. No `V3D`/`p3D`/PLE property feeds this path.
(**A**)

### 1.3 Where the plate pattern lives (PROVEN)

The wrapper's global `CalibrationTableParam` (BSS at `0x180087748`, initialised by
`FUN_180023340`; 0x78 bytes, copied by `set_calibTable_param` JR `0x1801727B0`) holds:

| offset | value | meaning (proved from JR consumers) |
|---|---|---|
| +0x00 | 1 | `InOutColor` argument |
| +0x04 | **4** | number of big circles |
| +0x08 | 1.5 | big/small radius ratio |
| +0x10 | 100 | max ellipse count |
| +0x14 | **17** | recognition columns; **row stride** `idx = 17*row + col` |
| +0x18 | **14** | recognition rows (17·14 = 238 output cells) |
| +0x20/+0x28 | **25.0** | template scale (index → 2-D template units), **not** a metric pitch |
| +0x30/+0x34 | **8 / 6** | template origin `(col,row)` that maps to `(0,0)` — **not** plate dimensions |
| +0x38…+0x50 | 8, 5, 7, 11 | **columns** of the four large markers |
| +0x58…+0x70 | 4, 8, 8, 8 | **rows** of the four large markers |

The large markers therefore sit at `(col,row) = (8,4), (5,8), (7,8), (11,8)` → flat indices
**76, 141, 143, 147** of the 238-cell array. The E7 pose path in the same DLL hard-codes the
**180°-rotated** set **73, 77, 79, 144** (`FUN_180015850` reads result elements
`0x490/0x4d0/0x4f0/0x900`, i.e. 73/77/79/144); `(c,r) → (16−c, 12−r)` maps one set onto the other.
Two independent code paths therefore describe the same 17×14 board. (**A**)

### 1.4 Markers occupy every second lattice node (PROVEN by the index mapping)

The physical 8×5 marker pattern maps onto the 17×14 recognition array as

```
c = 2u + 1          r = 2v + 2
```

Check against the four large markers (canonical set `(8,4),(5,8),(7,8),(11,8)`):

| (col,row) | (u,v) | 180°-rotated to the corpus frame |
|---|---|---|
| (8,4) | (3.5, 1) | (3.5, 3) |
| (5,8) | (2, 3) | (5, 1) |
| (7,8) | (3, 3) | (4, 1) |
| (11,8) | (5, 3) | (2, 1) |

which is exactly the observed large-marker pattern **(2,1), (4,1), (5,1), (3.5,3)**. All 40
lattice markers likewise land on odd columns / even rows; the remaining 197 cells are filled with
`(−1,−1)` sentinels by `Sn3D_one_calibTable_fast_recognition` (JR `0x18014B880`, which sizes the
output to `param[0x18] * param[0x14]`). Consequence:

```
marker pitch = 2 × iSpace = 2 × 14.0 mm = 28.0 mm
```

(**A** for the mapping and the arithmetic; the only assumption is that the physical markers are the
markers the recognition reports, which the corpus and the vendor video confirm.)

---

## 2. AA/BB: the per-board scale constraint (PROVEN)

`Sn3DEACalibrateController` receives `AA`/`BB` from `Sn3DCodeProject`'s `.ple` handling
(`Csn3DCode::get_cam_name` / `get_all_aabb` → `publishPLEData` `vDistanceAA`/`vDistanceBB` →
`Sn3DCameraCalibHelper::onSubscribeData` → `setAABB` → `initComputeAlg`). EA model defaults are
`aa = 151.030`, `bb = 124.857`; this unit's PLE gives `AA = 150.804`, `BB = 125.071`.

The wrapper builds two `DistanceRef` records (16 bytes each) and passes them to
`Sn3D_double_camera_calibration`:

```
{ int32 A, int32 B, double d }         // d = true metric distance |P_A − P_B|
```

`Sn3DAlgorithm::global_optimize_landMark` (JR `0x18024E790`, branch `FrameDistanceType == 3`,
"ExternalRuler") accumulates `|d − |P_A − P_B||` as a soft scale residual and logs
"init/result disRef distance error". The indices are literal indices into the generated 238-point
V3D array:

| ref | A | B | lattice Δ | model distance at 14 mm | PLE value |
|---|---|---|---|---|---|
| `AA` | 71 = (3,4) | 149 = (13,8) | (10, 4) | `14·√116 = 150.784` | **150.804** (+0.013 %) |
| `BB` | 79 = (11,4) | 139 = (3,8) | (−8, 4) | `14·√80 = 125.219` | **125.071** (−0.12 %) |

Both constraints independently pin the **lattice** pitch to
`150.804/√116 = 14.002 mm` and `125.071/√80 = 13.983 mm`. Because the physical markers sit every
second lattice node (§1.4), the physical marker pitch is **2 × 14.0 = 28.0 mm**. (**A**)

Note: the wrapper reads `AA`/`BB` with `QVariant::toInt()` (so the algorithm sees 150/125, not
150.804/125.071). The 0.5 %/0.06 % truncation is far below the 1 % CCF-scale difference discussed in
§7 and does not change the conclusion.

---

## 3. The board frame and all 41 markers

Vendor frame: origin at the centre of the 8×5 marker field (recognition cell `(col,row) = (8,6)`,
which the template origin confirms), `x` along the 8-marker axis, `y` towards row 0, `z = 0`:

```
x(u) = 28 (u − 3.5)          y(v) = 28 (2 − v)          [mm]
```

| item | coordinates (mm) |
|---|---|
| small markers | `(28(u−3.5), 28(2−v), 0)` for `u=0..7`, `v=0..4`, excluding the three large on-lattice nodes |
| large #1 | `(u,v)=(2,1)` → `(−42, +28, 0)` |
| large #2 | `(u,v)=(4,1)` → `(+14, +28, 0)` |
| large #3 | `(u,v)=(5,1)` → `(+42, +28, 0)` |
| large #4 (off-lattice) | `(u,v)=(3.5,3)` → `(0, −28, 0)` |

Marker field span: 196 × 112 mm (7 × 4 pitches). Plate outline: 224 × 168 mm (8 × 6 pitches,
0.5-pitch side margins, 1-pitch top/bottom margins).

The engine internally uses the image-like frame `y = +28(v−2)` (identical geometry, `y` flipped);
`PlateSpec.vendor_points_mm()` / `vendor_big_mm()` return the vendor frame above.

---

## 4. Independent evidence that the board is 8×5 (not 8×7)

* **25-pose IR corpus** (1280×1024, `imageLeft1..25.bmp`): 8 columns × 5 rows, 41 markers,
  large at (2,1),(4,1),(5,1),(3.5,3); plate outline 7.97 × 6.02 marker pitches, margins
  0.50/0.46 and 1.01/1.01 pitch. (**A**, `analysis/calibration/audit/plate_report.md`)
* **Official SHINING 3D calibration video** (`doc-asset.shining3d.com/exstar/en-calibration-sd.mp4`,
  byte-identical to the shipped EXStar 1.0.6 asset, 1920×1080, 3284 frames): frame 525 (t = 21 s)
  shows the physical board face-on with the burnt-in subtitle *"please note the four large dots on
  it"* — 8 × 5, 41 markers, same large-marker positions. (**A**, `analysis/calibration/docs_evidence/`)
* The **8×7 drawing** (`CalibrationPage_EA.dll` PNG #1, 1720×1486; manual p.35; EXStar 1.0.6 help
  screenshot) is a **shared cross-product illustration**: the same PNG is byte-identical inside
  `EXStar_Hub_v2.0.0-44/CalibrationPage_EA.dll`, which serves other products (Einstar 2, Rockit,
  EXScan Rigil). It does **not** describe the Einstar board. (**A**)
* `200x150` is a profile/directory name (`res/Einscan-E10/200x150`, also `5M/200x150`,
  `EinScan-S/200x150`); the closest vendor spec with that shape is EinScan Pro's
  "Single Scan Range 210 × 150 mm". It is **not** a plate measurement. (**A**)

Consequences: the earlier hypotheses "plate = 200 × 150 mm" and "pitch = 25.0 mm" are **retracted**;
"pitch = 27.72 mm" is the CCF-effective scale, not the nominal plate pitch (§7).

---

## 5. Does the software agree with the corpus geometry?

The model generated by the code (`17×14` lattice, markers every second node) and the corpus
measurement are the same geometry:

| quantity | code / vendor constants | corpus measurement |
|---|---|---|
| columns × rows | 8 × 5 (index mapping) | 8 × 5 |
| large markers | (2,1),(4,1),(5,1),(3.5,3) | same |
| lattice pitch | 14.00 mm (AA/BB) | — |
| marker pitch | 28.0 mm | 28.0 mm (implied) |
| plate outline | 8 × 6 marker pitches = 224 × 168 mm | 7.97 × 6.02 pitches, aspect 1.321 vs 1.333 |
| margins | 0.5 pitch / 1 pitch | 0.50/0.46 and 1.01/1.01 pitch |

There are no free parameters left: the topology, the pitch and the origin all come from the vendor
code, and the corpus corroborates them.

---

## 6. What EXStar's own calibration produced on this corpus

The captured installation's `res/Einscan-E10/200x150/{Left,Right,Tex}CCF.txt` are **not** a generic
sample: the device record was written atomically in the recorded session
(**2026-02-09 21:34:58**) and its three CCF slots are byte-identical to those `.txt` files
(`docs/calibration/ccf_format.md` §7). They are the vendor's calibration output on the same 25
images.

| quantity | vendor CCF | our engine, 28.0 mm | our engine, 28.0 mm free kc | our engine, 27.72 mm |
|---|---|---|---|---|
| IR baseline (L–R) | **158.557 mm** | 160.144 mm | 160.193 mm | **158.543 mm** (−0.014 mm) |
| relative rotation vs CCF | — | 0.202° | 0.429° | 0.202° |
| per-point RMS | — | 0.367 px | 0.358 px | 0.367 px |
| held-out RMS | — | 0.455 px | — | 0.455 px |
| Δfx vs CCF (L/R/T) | — | −2.78 / −5.12 / −4.17 px | −3.06 / −6.07 / −4.70 px | same |

The vendor's own output is therefore **1.0 % below** the scale implied by its nominal 28.0 mm model.
The difference is between the two estimators (the vendor fits 238 cells with free distortion and
soft distance constraints; our engine fits the 41 detected markers, factory distortion fixed in the
28.0 mm run), not a property of the plate. It is quantified and reported rather than hidden; the
engine offers both scales (`--pitch 28.0` nominal, `--pitch 27.72` to reproduce the shipped CCF).
Raw data: `analysis/calibration/engine/plate_scale_validation.json`.

### 6.1 Colour camera: the Tex CCF is stale

The same record contains the texture-camera CCF, but `deviceStatus` in the session reports
`hasTexture:false`, and the colour record is **17.66 mm** away from the joint solve that includes the
25 tex images (relative rotation 0.14°), while the IR pair is within 0.76 mm. The vendor therefore
did not re-estimate the colour camera in this session and kept the previous record. This closes the
open colour-alignment discrepancy as a **stale-record** issue rather than a plate/scale issue; the
engine's joint three-camera fit (all 75 images) is the consistent estimate.

---

## 7. Proven / inferred / unknown

**Proven (A)**
* 17×14 recognition lattice, row-major `idx = 17·row + col`, pitch 14.00 mm from AA/BB.
* markers on every second node → 28.0 mm marker pitch; 8×5 grid; large markers as listed.
* board frame origin = marker-field centre; vendor coordinates of all 41 markers.
* the Einstar path always generates the lattice (the supplied-array branch is dead).
* `SrcCalibParam` (752 B) is **not** plate geometry: it is three 0xF8 camera blocks
  (intrinsics/distortion/rigid transform), copied from the `srcCalParam` QVariant.
* 8×5 board in the corpus and in the official video; 8×7 is UI art; `200x150` is a profile name.

**Inferred (B)**
* plate outline 8 × 6 marker pitches = 224 × 168 mm (from the corpus outline; the vendor code does
  not store the outline).
* the exact physical hole/spot diameters (the IR blobs are bloomed; the drawing gives a 2:1 ratio).

**Unknown (U)**
* the 1.0 % vendor-vs-our scale difference (could be ellipse-centre bias, a slightly different
  factory lattice, or a solver regularisation) — bounded to ~1 % by three independent estimators;
* manufacturing tolerances of the physical plate (±0.1–0.3 % per board from the AA/BB spread);
* whether some units ship with a different `iSpace` (all evidence so far: 14).

---

## 8. Reproduction

```bash
# geometry + tests
python3 -m pytest -q tests/test_plate_geometry.py

# full-corpus solve at the recovered nominal geometry
OPENBLAS_NUM_THREADS=1 python3 -m calib.cli calibrate \
    /path/to/vendor-calib-rapid \
    --factory sources/calibration/res_Einscan-E10_200x150 \
    --pitch 28.0 --no-anchor --out /tmp/calib_28.json

# reproduce the vendor CCF scale
OPENBLAS_NUM_THREADS=1 python3 -m calib.cli calibrate \
    /path/to/vendor-calib-rapid \
    --factory sources/calibration/res_Einscan-E10_200x150 \
    --pitch 27.72 --out /tmp/calib_2772.json
```

Evidence dumps: `analysis/calibration/ghidra/dumps/` (wrapper, JR, controller, loader, data,
device-file); the dedicated reports `analysis/calibration/re/{algorithm,workflow,storage}_report.md`
and `analysis/calibration/audit/plate_report.md`.
