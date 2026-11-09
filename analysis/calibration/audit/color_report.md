# Colour ("Tex") CCF record — provenance, output convention and RGB impact audit

Scope: the ~17 mm translation discrepancy between the shipped `TexCCF.txt` and our free
joint re-optimisation of the vendor corpus. Work was offline and read-only on vendor
artefacts; nothing outside `analysis/calibration/audit/` was modified.

* Inputs: `sources/calibration/res_Einscan-E10_200x150/{Left,Right,Tex}CCF.txt`,
  `<device-dump.bin>` (6568 B), `analysis/logs/calib_window.log`,
  `analysis/calibration/engine/end_to_end.json`, Ghidra dumps under `analysis/calibration/ghidra/dumps/`.
* Reproduce: `python3 analysis/calibration/audit/color_audit.py` → `color_audit.json`.
* Confidence grades: **A** = proved by bytes/instructions/arithmetic invariant,
  **B** = strong (several independent lines, one modelling assumption), **C** = plausible,
  **U** = unknown.

---

## 0. Verdict in one table

Relative pose of the Tex camera w.r.t. the left IR camera, `R_rel = R_tex·R_leftᵀ`,
`t_rel = T_tex − R_rel·T_left` (frame-independent; see §3.3):

| L→Tex source | rotation angle | `t_rel` (mm) | ‖t_rel‖ | Δ vs our fit |
|---|---|---|---|---|
| **shipped CCF** | 17.0049° | (−122.986, **16.894**, 17.134) | 125.318 | **17.66 mm**, 0.12° |
| our joint fit | 16.8847° | (−121.078, **−0.069**, 12.626) | 121.735 | — |
| unit's factory 22-double record | 16.9983° | (−122.241, **−0.108**, 12.486) | 122.877 | **1.17 mm**, 0.11° |

Control, same three sources for the IR pair (L→R): CCF vs fit **0.76 mm / 0.18°**,
factory vs fit **1.54 mm / 0.13°** — i.e. the IR chain is consistent to ≈1 mm while the
Tex CCF is **15× worse and 17 mm off in y**. The rotation is right; only the translation is wrong.

**Conclusion (B):** the 17 mm is *not* a key/decode/frame artifact and *not* a per-camera
reference-view artifact of the documented writer. The shipped Tex record is the only one of
three independent estimates of the same quantity that disagrees, so it is not a valid
estimate of this unit's Tex camera. The root cause is not provable offline (§7).

---

## 1. Provenance

### 1.1 Artefact inventory

| artefact | size | mtime (ns) | sha256 (first 16) |
|---|---|---|---|
| `LeftCCF.txt` | 520 | 2026-11-07 03:04:28.829951301 | `6879a68d6926caeb…` |
| `RightCCF.txt` | 520 | 2026-11-07 03:04:28.830455935 | `45b80e2aa8bcece7…` |
| `TexCCF.txt` | 264 | 2026-11-07 03:04:28.830467372 | `7b0477a524b3af59…` |

The three mtimes are **within 0.5 ms of each other on 2026-11-07**, i.e. they are the
*extraction/copy* time of the install tree, not a vendor calibration timestamp. **A** —
mtime carries no provenance here; the only real timestamp is the one inside the device block (§1.2).

Sizes are the documented 520/520/264 (33 doubles + 256 B IR trailer). **A**.

### 1.2 The 2026 CCF block on the device

`calibration.bin` at `0x39B`, length `0xF21` = 3873 B:

| offset | content | evidence |
|---|---|---|
| `B+0x000` | `u32 4` (block type) | bytes `04 00 00 00` |
| `B+0x108` | `u32 520` + Left record | slot length 520, payload == `LeftCCF.txt` |
| `B+0x610` | `u32 520` + Right record | payload == `RightCCF.txt` |
| `B+0xB18` | `u32 264` + Tex record | payload == `TexCCF.txt` |
| `B+0xA14` | ASCII **`2026-02-09 21:34`** (NUL-padded) | `32 30 32 36 2d 30 32 2d 30 39 20 32 31 3a 33 34` |
| `B+0xF1C` | `"FQFQ"` | `46 51 46 51` |

All three slots are **byte-identical** to the shipped `.txt` files (verified, `bytes_match_txt`
in `color_audit.json`). **A**.

The same dump also holds the **factory 22-double records** (`0x00C`, `0x104`, `0x1FC`;
`R` orthonormal to 1.1e-15, `det = 1`, `|T| ≈ 550 mm`) and the production date
`<redacted-production-date>` at `0x1893` (see `docs/calibration/ccf_format.md` §7). **A**.

### 1.3 Did the 2026 run write all three records?

Yes — one atomic write of the whole block, and the log contains that write:

| evidence | line | content |
|---|---|---|
| `deviceStatus` from device | `calib_window.log:466` | `"hasTexture": QVariant(bool, false)`, `calibNum <redacted-part-number>`, `activeTime <redacted-production-date>` |
| CCF handed to the UI | `:17763` | `{"CCF":{"leftCCF":null,"rightCCF":null,"texCCF":null}, "calibDateTime":…"2026-02-09T21:34:58"}`, `errorCode 0` |
| **device write** | `:17767` | `writeCustomInfo` `"offset":923` (=0x39B), value starts `\u0004\0\0\0`, contains literal `2026-02-09 21:34` at the `0xA14` position, ends `FQFQ\0`; length 3873 |
| compute finished | `:17905` | `{"cmd":"calibComputeFinished","currentCalibType":4,"errorCode":"","success":true}` |
| second event | `:18867` | `currentCalibType:2`, success; `:18833` writes `u32 5` at `0x12BC` only (validity marker, no CCF content) |

So the Tex record **bytes** were written in the 2026-02-09 21:34:58 session, as part of the
same block as Left/Right. **A** — but *written* ≠ *re-estimated* (§1.5).

### 1.4 How the block is produced (code)

`Sn3DEACalibrateController.dll`:

* `FUN_18000e0a0` (`:14119`) — the CCF write-back. Builds `…/200x150/LeftCCF.txt`
  (`:14219/14228`), `…/RightCCF.txt` (`:14296/14305`), `…/TexCCF.txt` (`:14373/14382`),
  reads the three files, then calls the packer at `:14469`.
* `FUN_180003940` (`:5785`) — the packer:
  `memset(param_1,0,0xf21)` (`:5831`); three records at `+0x10C/+0x614/+0xB1C`
  with their `u32` lengths at `+0x108/+0x610/+0xB18` (`:5833-5842`);
  `param_1[0x3c7] = 0x51465146` ("FQFQ", `:5844`); `*param_1 = 4` ;
  `QDateTime::currentDateTime()` + format `"yyyy-MM-dd hh:mm"` copied to `param_1 + 0xa14`
  (`:5847-5859`); `FUN_180003540(param_1, 0x39b, …)` — the device write at **0x39B** (`:5866`).

Hence: **device block ← the three on-disk files; the timestamp is the packer's clock.** **A**.

### 1.5 Was the Tex record *re-estimated* in 2026? (mixed evidence)

| evidence | direction | grade |
|---|---|---|
| `hasTexture:false` in `deviceStatus` (`:466`) — the unit reports no texture camera | stale | B |
| log window has **0** hits for `imageTex`, `imageLeft`, `readPic`, `imageQueue`; capture is not logged | cannot decide | U |
| QML calibration struct carries `"hasTexture":true, "pTextureImage":null` (`:1746,1758,1766`) — a UI-side default, no image pointers | cannot decide | C |
| Tex `error` field = **0.0765 px**, IR = 0.0 (`ccf_format.md` §3) — only the Tex record carries a non-zero residual | different code path / different run | C |
| Tex distortion block is **bit-identical** to the factory record (5/5 values, ≤1e-9) — but so are Left/Right (vendor holds `kc` fixed; `end_to_end.json` `info.free_kc = false`) | **not** evidence of copying | A (as a negative) |
| Tex `fx` = 1141.0776 vs factory 1140.3490 (+0.73 px); `cx/cy` −0.53/+1.03 px | K *was* touched | B |
| the whole Tex pose differs from both the factory record and our fit by ≈17 mm in y | content is anomalous | A |

**Reading (B):** the K block of the Tex record moved relative to the factory record, so the
record is not a verbatim copy of the 2024 block; but its pose is inconsistent with both
independent estimates of the same relative geometry. Byte-write in 2026 is proved; fresh
estimation from 2026 tex observations is **not** proved and is contradicted by the pose (§5).

---

## 2. Output convention — how R and T reach each `CalibParam`

### 2.1 `CalibrationCCF::TribleCCF` (0x1802e6a20), body `Sn3DCalibrationJR.dump.txt:82640-82980`

Signature (mangled, `:82644`): `bool TribleCCF(const vector<vector<double>>& K5,
const vector<vector<double>>& D5, const vector<vector<double>>& rvec, const vector<vector<double>>& tvec,
const vector<vector<double>>& rvec_rel, const vector<vector<double>>& tvec_rel,
CalibParam&, CalibParam&, CalibParam&)`.

API shape is **proved by the input checks**, not guessed:

| check | line | consequence |
|---|---|---|
| `param_1` inner vectors are 5 doubles and the outer has **3** entries | `:82744, 82763` | K5 per camera, 3 cameras |
| `param_2` likewise | `:82745, 82779` | D5 per camera |
| `param_3`/`param_4` are N × 3 doubles, N > 2 | `:82751, 82754` | per-view rvec / tvec of camera 1 |
| `param_5`/`param_6` outer size ∈ [0x30,0x48) ⇒ **2** entries of 3 doubles | `:82752-82753` | **relative** poses (cam 2, cam 3), *not* 25 per-view poses |

Assignment, camera 1 (`param_7`):

```
v* = first v with tvec[v][2] != -1.0            JR:82819-82838
R  = Rodrigues(rvec[v*])   written row-major    JR:82840-82851, 82856-82859
T  = tvec[v*]                                   JR:82854-82855, 82861
```

Cameras 2 and 3 (`param_8`, `param_9`), same loop over the two relative poses `k`:

```
R_k = R_rel(k) · R_1(v*)                        JR:82904-82918   (3×3 product, row-major store)
T_k = t_rel(k) + R_rel(k) · T_1(v*)             JR:82919-82924
store into param_8 for k=0, param_9 for k=1     JR:82936-82962
```

**Answer to the question posed:** T is **not** a per-camera "first valid view" tvec and **not**
a fused/optimised world frame. It is camera 1's first-valid-view tvec; cameras 2/3 are
*composed* into that one frame through the relative pose. Therefore **all three records share
one world frame** (camera 1's reference board pose). **A** for this function.

> Note the API cannot express per-camera reference views at all: `param_5/param_6` are
> 2-element relative-pose arrays (`:82752`), not N-element pose arrays.

### 2.2 Which path actually wrote the shipped trio (B)

* `Sn3D_Trible_CCF` (0x180138170, `JR_core2.dump.txt:30816`) only checks the dongle and
  forwards to `TribleCCF` (`:30894`). It has **no importer** in any dumped module
  (`grep EXT … Sn3D_Trible_CCF` → none), and `Sn3D_E10_camera_calibration` (0x1801371d0)
  forwards to `FUN_1802dca70` without calling it. **A** (absence of a caller in these binaries).
* The production CCF writers are per **pair**:
  * `Sn3DCalibrateAlgorithmWrapper!FUN_18000eb50` ("`E7CameraCalibAlgManagerPrivate.cpp`",
    `:17617`) — `Sn3D_multi_camera_calibration` (`:18087`) → `Sn3D_double_CCF` (`:18151`) →
    writes `LeftCCF.txt` (`:18160`) and `RightCCF.txt` (`:18167`). No Tex here.
  * `Sn3DCalibrateAlgorithmWrapper!FUN_180048230` ("`CameraCalibrateAlg.cpp`", `:65388`) —
    logs **both** `[DP 2022][LR]residual:` and `[DP 2022][LT]residualT:` and writes
    `LeftCCF.txt` (~`:65828`), `TexCCF.txt` (`:65849`) and calls `Sn3D_write_bina_ccffile`
    (`:65882`) / `Sn3D_double_CCF` (`:65897`).
* `Sn3D_double_CCF` / `Sn3D_write_bina_ccffile` use the **same** "first valid view + relative
  pose" pattern (inline copy of the scan at `:18180-18216`, sentinel `tvec[v][2] != −1`).

**B:** the shipped trio is most plausibly produced by an `[LR]` solve (Left, Right) plus an
`[LT]` solve (Left, Tex) inside `CameraCalibrateAlg`, i.e. **two sub-calibrations, one shared
write**. This matters for §3 — but it does not by itself explain 17 mm.

### 2.3 `WriteCcf` / binary writer (no hidden transform)

* `CalibrationCCF::WriteCcf` (0x1802e74e0, `:82984-83019`) emits the **text** twin in the order
  `fc, cc, alpha, kc, T, R, error` (`:82998-83015`); no rotation, no sign flip, no transpose.
* `Sn3D_write_bina_ccffile` (0x18014eb60, `JR_core2.dump.txt:30902`) takes `K5`, `D5`, a
  **`RigidMatrix` (R[9] + T[3])** and the error, and calls `WriteCcf_bin`; the 33-double order is
  `fc[2] pad[2] cc[2] pad[2] alpha pad kc[5] pad[5] T[3] R[9] error` (matches the bytes).
  `RigidMatrix` is `get_translation()`-based (`:18222`), i.e. T is the translation of the
  transform, never the camera centre.

---

## 3. Frame / rebasing

### 3.1 Is the CCF world the board plane at one capture?

Yes. `T = tvec[v*]` and `R = Rodrigues(rvec[v*])` are the PnP pose of the **object points**
(the plate, `z = 0`, `calib/plate.py`), so the world frame is the plate frame at the reference
capture and `X_cam = R·X_board + T`. All three records are world→camera in that one frame
(§2.1). **A** for the writer; **B** that the shipped trio followed it.

Consequence, and a useful cross-check: **absolute** `T`/`R` are only comparable inside one
frame convention. The CCF's `Left` record and our fitted view poses differ by ≥132° of
rotation even for the best-matching view (`color_audit.json`), because the vendor's plate model
origin/orientation is not ours (we recover it in `calib/plate.py`); the CCF's `|T|` is 279.9 mm
while our view distances span 268–603 mm. This is exactly why the audit only compares the
**frame-invariant** `R_rel`, `t_rel` (§3.3).

### 3.2 Could the mismatch be a per-camera reference view? — bounded, and excluded

If the Tex record were expressed in a *different* reference view than the Left record, the
implied relative rotation would pick up the board rotation between the two views,
`R_rel' = R_tl · (R_lj R_liᵀ)`, and the implied translation would pick up the corresponding
board displacement. From our 25 fitted board poses:

| statistic over 25×24 view pairs | value |
|---|---|
| smallest board rotation between two views | **1.048°** (views 0,1) |
| board displacement for that pair | 20.06 mm |
| median board rotation / displacement | 68.5° / 123.3 mm |

Any reference-view mismatch therefore costs **≥1.05°** of relative rotation, while the
CCF-vs-fit rotation agrees to **0.12°** (and the CCF-vs-factory to 0.11°). **The
per-camera-reference-view hypothesis is excluded at ≈9× the observed rotation error.** **A**
(arithmetic), given the fitted view poses.

> The parent's hypothesis is nevertheless the right *shape*: a frame mismatch that is a pure
> **translation** of the world origin would leave `R` untouched and shift `t_rel` by `R_tl·g`.
> The observed offset is consistent with `g ≈ (−3.1, 17.0, 3.7) mm` — a 17.6 mm origin shift
> along the plate's y. No such origin shift exists in the documented writer; it would require
> the Tex record to come from a solve whose plate model origin differs (C, §7).

### 3.3 Frame invariance of the quantity we compare

For `X_c = R_i X_w + T_i`, a common world change `X_w' = G X_w + g` gives
`R_i' = R_i Gᵀ`, `T_i' = T_i − R_i Gᵀ g`, hence `R_rel` and `t_rel = T_d − R_rel T_s` are
**invariant**. So the L→Tex comparison in §0 is frame-free and the 17.66 mm cannot be
"rebasing". **A** (algebra, confirmed numerically: CCF↔factory agrees to 17.2 mm on the same
quantity while their absolute `T` differ by 44.7 mm).

---

## 4. Model / decoding error — ruled out

| hypothesis | test | result | grade |
|---|---|---|---|
| IR integer key mis-applied | det/orthonormality after key | det = 1.000000, max‖RRᵀ−I‖ = 2.0e-15 (L), 1.0e-15 (R); Tex unkeyed det = 1, 5.1e-16 | **ruled out** A |
| Tex record keyed by mistake | Tex det = 1, orthonormal as stored; key would destroy that | **ruled out** A |
| R transposed | recompute rig triangle under R→Rᵀ | 158.56/125.32/38.63 (stored) vs a 56.5 mm IR "baseline" if the block were read transposed/centre-style | **ruled out** A |
| T = camera centre | `C = T` gives IR baseline ‖T_l−T_r‖ = **56.5 mm**, vs 158.6 mm for `C = −RᵀT`; factory records independently give 160.03 mm | **ruled out** A |
| homogeneous / scale convention | `|T|` 265–273 mm for a 200×150 plate at 270–600 mm; mm end-to-end (`ccf_format.md` §8) | consistent A |
| row- vs column-major of R | writer stores `M·R₁` in row-major order — re-derived from the store order at `:82842-82861` and from the composition at `:82909-82918` (column *k* of `R₁` multiplies row *k* of `M`, emitted as three consecutive slots) | row-major, **no transpose** A |
| IR control | CCF vs fit 0.76 mm / 0.18°; factory vs fit 1.54 mm / 0.13° | decode chain sane A |

**A:** the decode and the documented conventions are correct; the discrepancy lives in the
Tex record's numbers, not in how we read them.

---

## 5. Quantitative RGB impact

Method: take the 40 plate holes (`calib/plate.py`, pitch 27.72 mm, `z = 0`), place them with
each of our 25 fitted board poses, project into the Tex camera with the **same** intrinsics
(`K_tex` from the CCF) once with the CCF Tex pose and once with our fit's, and measure the
pixel distance. `color_audit.py`, section `rgb_impact*`.

| quantity | value |
|---|---|
| board-point distance range (all views) | 269 – 603 mm |
| representative view (median distance 443.8 mm; points at 390.7–472.7 mm) | **46.6 px** mean, 52.1 px max |
| all 25 views, per-view mean | 33.6 – 77.5 px (median 46.6) |
| all 25 views, per-view max | 34.8 – 85.6 px (median 53.5) |
| direction | almost purely vertical: mean `Δv = +49.4 px`, mean `Δu = −3.1 px` |
| object-space equivalent | **17.6 mm** (mean), 18.4 mm (max) at the median view |
| as a fraction of the plate | 0.63 hole pitch (17.6 / 27.72) |

The rotation difference alone (0.12°) accounts for only ≈2.4 px; **>95 % of the shift is the
17 mm translation.** Physically: with the shipped CCF, every depth pixel's colour would be
sampled ≈46–50 px (≈0.6 pitch, ≈18 mm on the object) **above** where it belongs — a visible
texture ghosting/double-edge on any RGB-mapped scan, and a systematic vertical colour offset
in the IR↔RGB overlay.

---

## 6. Recommendations

1. **Do not use the shipped `TexCCF.txt` as ground truth** for the colour camera, and do not
   "fix" our solver to reproduce it (`color_report.md` §0/§5). Our joint fit is supported by
   the unit's own factory record to 1.17 mm; the CCF Tex record is the outlier.
2. **Code (our side, `calib/`)** — keep CCF I/O byte-exact, but add a
   *provenance + consistency* guard rather than trusting the record:
   * a `ccf.relative_pose()` helper and a check `‖t_rel(ccf) − t_rel(fit)‖ < τ` (τ ≈ 2 mm for
     IR, ≈ 3 mm for Tex) that fails loudly with the numbers in §0;
   * a `reference_view` field in any re-emitted record so the world frame is explicit
     (`R_ref, t_ref` or the view index), because the writer's frame is *implicit*
     (`TribleCCF` uses camera 1's first valid view — §2.1).
3. **Doc** — `docs/calibration/ccf_format.md`: state that the CCF world frame is the plate
   frame of camera 1's first valid view, that `T` is a translation (not a centre), and that
   all three records are composed into that one frame; add the §0 table as a worked example
   of a *bad* Tex record, and note that `mtime` on the shipped `.txt` is copy time.
4. **Test** — add a regression test asserting the §0 relations for the shipped set:
   `|‖t_rel‖(CCF_Tex) − ‖t_rel‖(fit_Tex)| ≈ 3.6 mm` and `Δy ≈ 17 mm` must be *reported*, and
   a `hasTexture == false` unit must not be allowed to claim a fresh Tex estimate.
5. **Field action** — re-run the colour calibration on the unit with texture capture enabled
   (or accept that the Tex record is factory/stale and mark it as such in the profile).
   Nothing offline can decide between "stale record" and "biased `[LT]` solve" (§7).

---

## 7. What remains unknown without the unit

| # | question | why it matters | how to settle |
|---|---|---|---|
| U1 | Did the 2026 `[LT]` stage run with real tex images? | decides stale vs biased | re-run with logging of `hasTexture`/image count, or dump the pre-2026 `TexCCF.txt` |
| U2 | Which sub-calibration wrote the final `LeftCCF.txt` (`[LR]` or `[LT]`)? | a shared-frame assumption | trace `CameraCalibrateAlg` `[LR]`/`[LT]` order at runtime, or instrument `write_bina_ccffile` |
| U3 | Does the `[LT]` solve use a different plate origin / point set? | would explain a pure-`y` origin shift `g` (§3.2) | decompile `FUN_180048230` `[LT]` branch fully (not done here) |
| U4 | Are the tex images geometrically consistent with `K_tex` (ROI/crop offset)? | a 40 px vertical image offset reproduces 17 mm exactly | capture a tex image, compare detected grid vs model |
| U5 | Is `Sn3D_Trible_CCF` dead code in this build? | changes which writer semantics apply | runtime breakpoint / import table of the shipping EXE |

**Bottom line:** provenance of the *bytes* is fully explained (2026-02-09 21:34:58, one block,
three files, all stamped); the *content* of the Tex record is not a valid 2026 estimate of this
unit's colour camera, and the 17 mm is a real relative-translation error of ≈46–50 px, not a
convention artifact.
