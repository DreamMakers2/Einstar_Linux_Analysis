# Einstar / EXStar CCF calibration format — decoded

Status: **format recovered and validated** (all numbers below are produced by
`tools/ccf/decode_ccf.py` and asserted by `tools/ccf/validate_ccf.py`, 40/40
checks pass, and by `tests/test_ccf.py`, 18/18).

Scope: the `LeftCCF.txt` / `RightCCF.txt` / `TexCCF.txt` records of the
SHINING 3D EinScan10_01 ("Einstar", 1280 × 1024 × 3 cameras) and the 6568-byte
device-info dump that carries them.

---

## 0. TL;DR

* The `.txt` files are **not text**: they are raw little-endian **IEEE-754
  float64** records. 264 B = 33 doubles (colour camera), 520 B = the same
  33 doubles + a 256 B trailer (64 × int32, values 0–9).
* The 33 doubles are a packed copy of the MATLAB Camera-Calibration-Toolbox
  `Calib_Results` structure — SHINING ships the very same data as a *text*
  file (`res/device/mix/LeftLongCCF.txt`), which fixes the field order:
  `fc[2] cc[2] alpha kc[5] T[3] R[9] error`, each padded to 2/4/5-double
  boundaries.
* **The IR records are obfuscated with a per-field integer offset**
  (`stored = true + key`). The colour record is **not** keyed.
* The key for the nine rotation entries is **proved** from the data alone: it
  is the unique integer vector that turns the stored 3×3 block into an
  orthonormal, det = +1 matrix, and it does so to 1e-15 on **both distinct CCF
  calibrations** on disk and on both IR cameras (4 matrices).
* Stereo baseline **158.557 mm**; relative IR rotation **22.138°**.

---

## 1. Evidence base

| file | size | SHA-256 (prefix) |
|---|---|---|
| `sources/calibration/res_Einscan-E10_200x150/LeftCCF.txt` | 520 | `6879a68d6926…` |
| `sources/calibration/res_Einscan-E10_200x150/RightCCF.txt` | 520 | `45b80e2aa8bc…` |
| `sources/calibration/res_Einscan-E10_200x150/TexCCF.txt` | 264 | `7b0477a524b3…` |
| `sources/calibration/ccf_200x150/{Left,Right,Tex}CCF.txt` | 520/520/264 | `a6182110…`, `c59d4698…`, `1d7debea…` |
| `EXStar_v1.0.6.0` / `v1.2.2.0` / `Hub_v2.0.0-44` `ccf/200x150/*` | 520/520/264 | **byte-identical** to the `ccf_200x150` set |

Only **two distinct calibrations** exist on disk (set A = `res_Einscan-E10_200x150`,
set B = `ccf_200x150` and its three duplicate installs); the rest of the
analysis treats them as two independent samples.
| `<device-dump.bin>` (not published) | 6568 | — |

Cross-reference material shipped with EXStar that pins the field semantics:

* `sources/web/extracted/EXStar_Hub_v2.0.0-44/res/device/mix/LeftLongCCF.txt`
  — plain-text `Calib_Results` with **named** fields (`Focal Length: fc`,
  `Principal point: cc`, `alpha`, `Distortion: kc`, `Translation vector: T`,
  `Rotation Matrix: R`, `Standard Error: error`).
* `…/res/EXStarHub/calibration/{Left,Right,Tex,TexCCFSample}_bin.txt`
  — 184-byte (23-double) **unkeyed** CCF variant; its doubles 13…21 are an
  orthonormal rotation, doubles 10…12 a translation, 4…9 the distortion —
  the same order as the text file.

---

## 2. Record container

```
offset        size        type                 content
0x000         8           float64              field 0
…             …           …                    …
0x100         8           float64              field 32  ("error")
--- 264 bytes (colour record) ends here ---
0x108         256         64 × int32 LE        IR trailer, each value 0..9
--- 520 bytes (IR record) ends here ---
```

Measured: `33*8 = 264`, `264 + 256 = 520`; `TexCCF.txt` is exactly 264 bytes,
`Left/RightCCF.txt` exactly 520.

There is **no** magic, version, length, CRC or checksum field anywhere in the
record. The only self-describing structure is the size (264 vs 520). A CRC is
also ruled out for the trailer: the trailer is bit-identical between two
*different* calibrations (see §6), while the doubles differ.

---

## 3. Field layout (33 × float64, LE)

Confidence legend: **A** = proved by an exact arithmetic invariant, **B** =
proved by the shipped text cross-reference / independent second encoding,
**C** = plausible by range only.

| # | offset | type | meaning | 1280×1024 sanity | conf. |
|---|--------|------|---------|------------------|-------|
| 0 | 0x000 | f64 | `fx` focal length, px | 1141…1167 (stored) | B |
| 1 | 0x008 | f64 | `fy` focal length, px | 1141…1155 | B |
| 2 | 0x010 | f64 | pad, always 0.0 | 0.0 in all 10 records | B |
| 3 | 0x018 | f64 | pad, always 0.0 | 0.0 | B |
| 4 | 0x020 | f64 | `cx` principal point, px | 618…638 (centre 640) | B |
| 5 | 0x028 | f64 | `cy` principal point, px | 494…547 (centre 512) | B |
| 6 | 0x030 | f64 | pad, always 0.0 | 0.0 | B |
| 7 | 0x038 | f64 | pad, always 0.0 | 0.0 | B |
| 8 | 0x040 | f64 | `alpha` (CC-toolbox skew) | 6.2e-5 / 1.8e-4 / −9.5e-5 | B |
| 9 | 0x048 | f64 | pad, always 0.0 | 0.0 | B |
| 10 | 0x050 | f64 | `k1` radial | −0.162 | B |
| 11 | 0x058 | f64 | `k2` radial | +0.173 / +0.178 / +0.166 | B |
| 12 | 0x060 | f64 | `p1` tangential | +1.9e-4 … +8.4e-4 | B |
| 13 | 0x068 | f64 | `p2` tangential | +1.5e-4 / +2.4e-5 / −7.4e-5 | B |
| 14 | 0x070 | f64 | `k3` radial | +0.018 / +0.012 / +0.031 | B |
| 15–19 | 0x078–0x09F | f64 | pad, always 0.0 (5 slots) | 0.0 | B |
| 20 | 0x0A0 | f64 | `Tx` translation, mm | 48.20 / −7.82 / 3.04 | B |
| 21 | 0x0A8 | f64 | `Ty` | −37.73 / −37.60 / −20.92 | B |
| 22 | 0x0B0 | f64 | `Tz` | 273.15 / 266.00 / 264.20 | B |
| 23–31 | 0x0B8–0x0FF | 9 × f64 | `R` **row-major** world→camera | det = 1 to 1e-12 | A |
| 32 | 0x100 | f64 | reprojection error, px | 0.0765 (colour), 0.0 (IR) | B |
| — | 0x108 | 64 × int32 | IR-only trailer, values 0–9 | constant per role | A |

The paddings are structural, not accidental: the layout is
`double fc[2]; double _p0[2]; double cc[2]; double _p1[2]; double alpha; double _p2;
double kc[5]; double _p3[5]; double T[3]; double R[9]; double error;`.

### Byte-level proof (first and last fields of `TexCCF.txt`)

```
0x000  2d 68 7a 7d 4f d4 91 40  -> 1141.077627098664   fx
0x008  7d 7d 7b 5f 59 d4 91 40  -> 1141.087278298887   fy
0x020  5c cc 35 af d1 c8 83 40  ->  633.102384968089   cx
0x028  61 b8 e5 f4 ff 48 80 40  ->  521.124978823365   cy
0x0B8  62 e2 df 3d 6f 4b 96 3f  ->    0.021772134925   R00
0x100  d1 64 cd de ba 92 b3 3f  ->    0.076457671542   error
```

`left[0] = 0x40923c762f9e9238 = 1167.115416028666`,
`left[10] = 0xbfc4bd40792c2d35 = −0.162025508086`.

### Why "33 doubles" and not "32 + something"

Every one of the 33 slots is a legal finite double in all records; the 520 B
files merely append 256 bytes that are *not* doubles (as doubles they are
denormal garbage ~1e-314). The 264 B colour record is exactly the first 264
bytes of the same structure, which is why the format is "33 doubles, IR adds a
256 B trailer".

---

## 4. Numeric encoding

* **IEEE-754 binary64, little-endian**, no scaling, no fixed point. Evidence:
  the values land on physically meaningful numbers (f ≈ 1141–1167 px for a
  1280-px sensor, |T| ≈ 265–280 mm) only under this interpretation; float32
  gives 1e-10 or 1e38 garbage.
* **No float32 anywhere** in the records (checking `float32` at any 4-byte
  offset produces non-physical values).
* **No checksum/CRC** (§2).
* The only non-float data is the IR trailer: 64 × **int32 LE**, all in 0…9,
  constant per camera role.

---

## 5. The IR key (`stored = true + key`)

### 5.1 The observation

Subtracting a fixed vector of **integers** from the IR doubles turns them into
a physically valid calibration. Raw, the IR rotation block is not a rotation at
all:

| record | det(stored 3×3) | max&#124;RRᵀ−I&#124; | det after key | max&#124;RRᵀ−I&#124; after key |
|---|---|---|---|---|
| Left | **−921.357919** | **259.581** | 1.000000000000 | **2.00e-15** |
| Right | **−998.327992** | **269.018** | 1.000000000000 | **9.99e-16** |
| Tex (unkeyed) | 1.000000000000 | 5.06e-16 | — | — |

### 5.2 Proof that the key is *the* integer solution

For each of the nine rotation slots only two integers can bring the value into
[−1, 1]; 2⁹ = 512 combinations were enumerated, and exactly **one** makes
*both* IR matrices orthonormal with det = +1:

```
key[23..31] = (-2, -4, -11, -9, 0, 5, -11, -12, 0)
solution count = 1
residual for the winner: max|RRᵀ−I| = 1.998e-15 (Left), 9.992e-16 (Right)
runner-up:              max|RRᵀ−I| = 0.357
```

The same key, unmodified, restores both IR records of both distinct calibrations:

| calibration set | Left det / max err | Right det / max err |
|---|---|---|
| A: `res_Einscan-E10_200x150` | 1.000000000000 / 1.998e-15 | 1.000000000000 / 9.992e-16 |
| B: `ccf_200x150` (= all three EXStar installs) | 1.000000000000 / 1.110e-15 | 1.000000000000 / 9.159e-16 |

A 9-integer coincidence reproducing orthonormality to machine precision for
independent files is not credible; the key is real.

### 5.3 The second, independent line of evidence (distortion block)

The same flash dump also stores an **unkeyed** 22-double camera record per
camera (§7). Its 6-value distortion block `[4..9]` reproduces the CCF's `kc`
exactly once the integer key is removed:

```
Left : stored kc - 22-double kc = [0.000e+00, 6.000e+00, 2.000e+00, 7.000e+00, 1.300e+01]
Right:                            [0.000e+00, 6.000e+00, 2.000e+00, 7.000e+00, 1.300e+01]
Tex  :                            [0.000e+00, 0.000e+00, 0.000e+00, 0.000e+00, 0.000e+00]
```

The residuals are ≤ 6.3e-16 i.e. the differences are **exactly** the integers
(0, 6, 2, 7, 13) — the “+6”, “+2”, “+7”, “+13” are visible directly in the
stored bytes (`6.173297957713`, `2.000186724747`, `7.000146796145`,
`13.018457014016`). This line of evidence is independent of §5.2 and fixes
`key[10..14]`.

### 5.4 The full 33-entry key

| idx | field | key | confidence |
|---|---|---|---|
| 0, 1 | fx, fy | 8, −4 | **hypothesis**: only `key[0]−key[1] = 12` is forced (the colour lens has fx−fy = −0.0097 px, so the IR lens is square too); 8/−4 is the pair that additionally matches the in-flash record to < 1 px |
| 4, 5 | cx, cy | −6, −1 | hypothesis (both cameras agree with the in-flash record to 0.28 px for cx; cy is only ±4 px) |
| 8 | alpha | −2 | strong: the only integer giving a physical skew (+6.2e-5 rad); stored −1.999938 |
| 10–14 | k1,k2,p1,p2,k3 | 0, 6, 2, 7, 13 | **proved** (§5.3, exact integers) |
| 23–31 | R | −2,−4,−11,−9,0,5,−11,−12,0 | **PROVED** (§5.2, orthonormality 1e-15) |
| 20–22 | Tx,Ty,Tz | 0,0,0 | assumed: with these the rig triangle matches the unkeyed record within 2.4 mm; a ±1 mm integer shift moves a centre by 1 mm and does not improve the match |
| 2,3,6,7,9,15–19,32 | pads, error | 0 | structurally zero in every record |

`decode_ccf.py --key-mode {best,proven,zero}` reproduces each level;
`--print-key` prints the table with the confidence string.

### 5.5 What the key is *not*

The alternatives requested in the brief were tested and are numerically
inconsistent:

* **per-camera gain (multiplicative)** — rejected: a scale cannot map a matrix
  with det = −921 onto SO(3) while leaving the other eight entries at
  residual 1e-15; and the distortion differences are *additive* integers.
* **fixed-point scale/offset** — rejected: a global scale would destroy the
  exact integrality in §5.3 (residuals would be ~1e-16 × scale, not exactly
  the same five integers for two different cameras).
* **vignetting / per-block polynomial** — rejected: no polynomial structure;
  the key is a piecewise-constant per-field integer vector, and its rotation
  part is what makes the matrix orthonormal.
* **the 256-byte trailer as the key** — rejected: the trailer is identical for
  two different calibrations of the same role (§6) and is unchanged in the
  colour record, which needs no key.
* **libeinstar's key** (`src/calibration.cpp`): it applies an integer offset
  per field, i.e. the right *kind* of transform, but the values are wrong.
  Measured on the same files: after subtracting libeinstar's key,
  det = **−75.891310** (Left) / **−80.274901** (Right) and
  max&#124;RRᵀ−I&#124; = **307.064** / **307.005** — i.e. no better than the
  raw data. Its values only accidentally satisfy `key[0]−key[1] = 5−(−7) = 12`,
  which is why its decoded fx/fy (1162.115 / 1162.021) look plausible; every
  other entry is off. Its errors surface exactly as the runtime message
  “camera rotation is not orthonormal”.

---

## 6. What the 256-byte trailer is

* 64 × int32 LE, values 0…9 only (`min = 0`, `max = 9`), e.g. Left:
  `0,0,1,4,9,8,2,5,1,1,5,7,…` (64 digits), Right: `0,0,7,0,4,8,4,5,7,1,2,6,…`.
* **Not calibration data**: byte-identical between the two distinct
  calibrations of the same role (set A vs set B, whose 256 calibration bytes
  differ).
* Left ≠ Right ⇒ role-specific, but unit-independent.
* Only the colour record lacks it.

**Unknown**: its meaning. It is *not* the IR key (§5.5). It behaves like a
per-role constant table or seed programmed at production.

---

## 7. The 6568-byte device dump (`<device-dump.bin>`)

Complete map (offsets are absolute in the file):

| offset | size | content |
|---|---|---|
| 0x000 | 12 | `u32 1`, `u32 1280`, `u32 1024` (count, width, height) |
| 0x00C | 176 | camera record 0: 22 doubles `fx fy cx cy dist6 R9 T3` |
| 0x0BC | 72 | zero padding (record stride is 0xF8 = 248 B) |
| 0x104 | 176 | camera record 1 (same layout) |
| 0x1FC | 176 | camera record 2 (colour: fx = 1140.349) |
| 0x2F4 | 81 | `"FAFA"` + u32 512 + u8 0 + `ParaCCM_0` (15 f32) + `ParaDark_0` (3 f32) |
| 0x345 | 81 | `"FBFB"` + u32 768 + u8 0 + `ParaCCM_1` + `ParaDark_1` |
| 0x396 | 5 | `"FBFB"` + u8 0 — third block has a **zero payload** (not programmed) |
| 0x39B | 0xF21 | **CCF block**, see below |
| 0x12BC | 4 | `u32 5` |
| 0x12C0 | 72 | `wbdata` (9 doubles) — equals `wbdata.txt` |
| 0x1308 | 72 | second 9-double table of the same shape as `wbdata` (likely the no-LED white balance): `1.625985, 0, 0, 0, 1.0, 0, 0, 0, 1.766387` |
| 0x1350 | 9+8 | `"FWFW"` + u32 1536 + u8 0 + one 8-byte payload |
| 0x1361 | 5 | `"AABB"` + u8 0 |
| 0x1366 | ~800 | `0xFF` erased-flash fill |
| 0x1687 | 4+20 | `u32 7` then ASCII `<redacted-serial>` @ 0x168B |
| 0x178F | 19 | ASCII `<redacted-part-number>` (part number) |
| 0x1893 | 11 | ASCII `<redacted-production-date>` (production date) |
| 0x1998 | 16 | big-endian `u32 1`, big-endian `u32 2`, `00 00 00`, `"DADA"` @ 0x19A3, `00` |
| end | 0x19A8 = 6568 | |

CCF block (base `B = 0x39B`, found by `magic − 0xF1C`, verified `type == 4`):

| offset | size | content |
|---|---|---|
| B+0x000 | 4 | `u32 4` (block type) |
| B+0x108 | 4+520 | `u32 520` then the **Left** record (byte-identical to `LeftCCF.txt`) |
| B+0x610 | 4+520 | `u32 520` then the **Right** record |
| B+0xB18 | 4+264 | `u32 264` then the **Tex** record |
| B+0xF1C | 4 | ASCII `"FQFQ"` |
| B+0xF21 | | end of block (0x39B + 0xF21 = 0x12BC) |

Verified: `block_left[0x4A7:0x6AF] == LeftCCF.txt` exactly (and likewise for
Right/Tex) — the `.txt` files are verbatim copies of the flash slots.

### The 22-double records (unkeyed)

```
d[0..3]  fx, fy, cx, cy
d[4..9]  6 distortion values; d[4..8] == CCF kc exactly (Tex) or mod the integer key (IR)
d[10..18] R row-major  (det = 1.000000000000, max|RRᵀ−I| = 1.11e-15)
d[19..21] T (mm)       |T| ≈ 549…555 mm   (different origin than the CCF's 265…280 mm)
```

They carry the same rig but a slightly different calibration (fx differs by
0.7 %, cy by up to 4 px, T is expressed about 550 mm from the origin instead of
~270 mm). The **relative** geometry agrees with the CCF decode within 2.4 mm
(§8.4), which is what one expects from two calibration states of the same
device.

---

## 8. Geometric validation

### 8.1 Rotation orthonormality

| record | det(R) | max&#124;RRᵀ−I&#124; | ‖RRᵀ−I‖_F | det(stored) | max&#124;RRᵀ−I&#124;(stored) |
|---|---|---|---|---|---|
| Left | 1.000000000000 | 1.998e-15 | 2.587e-15 | −921.357919 | 259.581 |
| Right | 1.000000000000 | 9.992e-16 | 1.918e-15 | −998.327992 | 269.018 |
| Tex | 1.000000000000 | 5.057e-16 | 7.988e-16 | 1.000000000000 | 5.057e-16 |

Row norms of the decoded IR rotations are exactly 1 to 12 decimals;
`R·Rᵀ−I` is at the double-precision noise floor, i.e. the recovered matrices are
exact to the last bit of the stored data. **No transposition, Euler or
quaternion reinterpretation is needed** — the stored block is simply
`R + integer`, and the integer-only hypothesis is confirmed by the uniqueness
test in §5.2. (The non-orthonormality is therefore *not* a field-mapping error:
a wrong mapping would give a permuted but still orthonormal matrix.)

### 8.2 Intrinsics (decoded)

| | fx | fy | fx/fy | cx | cy | α |
|---|---|---|---|---|---|---|
| Left | 1159.115416 | 1159.021303 | 1.0000812 | 623.912232 | 534.562945 | +6.19e-5 |
| Right | 1158.086917 | 1157.718563 | 1.0003183 | 635.501437 | 494.509060 | +1.83e-4 |
| Tex | 1141.077627 | 1141.087278 | 0.9999915 | 633.102385 | 521.124979 | −9.55e-5 |

* fx ≈ fy to 0.008 % / 0.03 % (square pixels) — this is what forces
  `key[0]−key[1] = 12` (§5.4).
* principal points lie −16.1/+22.6, −4.5/−17.5 and −6.9/+9.1 px from the
  image centre (640, 512) — normal for a 1/3" sensor with a slightly tilted
  mount.
* horizontal FOV = 2·atan(640/1159.115) = **57.810°**, vertical =
  2·atan(512/1159.115) = **47.664°** for the IR pair.

### 8.3 Distortion

| | k1 | k2 | p1 | p2 | k3 |
|---|---|---|---|---|---|
| Left | −0.162026 | +0.173298 | +1.867e-4 | +1.468e-4 | +0.018457 |
| Right | −0.162050 | +0.177925 | +8.379e-4 | +2.423e-5 | +0.012360 |
| Tex | −0.151010 | +0.166436 | +5.756e-4 | −7.376e-5 | +0.031133 |

All in the normal range for a moulded asphere (|k1| < 0.2, small tangential
terms, k3 ≈ 0.01–0.03). The two IR cameras agreeing to 0.015 % on k1 (and 2.7 % on k2) is a further
sign that the decode is correct.

### 8.4 Pose, baseline, stereo consistency

Camera centres `C = −RᵀT`:

| | C (mm) |
|---|---|
| Left | ( 59.628, −130.876, 240.148 ) |
| Right | ( 64.961, 26.412, 259.445 ) |
| Tex | ( 47.218, −7.877, 260.687 ) |

The CCF triangle (158.557 / 125.318 / 38.628 mm) and the triangle formed by
the 22-double records' centres (160.092 / 122.877 / 37.215 mm) are the same
physical rig to within 2.44 mm; the two encodings differ only by a slightly
different calibration state and by the origin of their T vector.

```
|C_left − C_right| = 158.557 mm      (IR stereo baseline)
|C_left − C_tex  | = 125.318 mm
|C_right − C_tex | =  38.628 mm
same triangle from the 22-double records: 160.092 / 122.877 / 37.215 mm
                                            (max deviation 2.44 mm, 1.5 %)
```

Relative IR pose (independent of the key, since both use the same one):

```
R_rel = R_left · R_rightᵀ ,  rotation angle = 22.138282°
t_rel = T_left − R_rel·T_right = (155.742278, −0.044664, 29.743433) mm
|t_rel| = 158.557027 mm   (== |C_left − C_right|, consistency 1e-12)
```

The pair is **not** rectified: the relative rotation has a 22.1° yaw and the
baseline has a 29.7 mm z component, so rectification (`initUndistortRectifyMap`)
is mandatory before block matching — consistent with `libeinstar/src/depth.cpp`.

Epipolar sanity: the epipole of the right camera in the left image is
`K_left · t_rel / t_rel,z` = **(6693.26, 532.82) px**, i.e. 79.19° off the
optical axis and far outside the 1280×1024 frame — the pair is nearly, but not
exactly, parallel (22.14° relative yaw), so rectification is required and the
epipole is not observable in the image. No inconsistency was found.

---

## 9. Record variants / how many

* Three records per device: `LeftCCF` (IR 0), `RightCCF` (IR 1), `TexCCF`
  (colour). Two encodings on disk: **520 B keyed IR** and **264 B unkeyed
  colour**.
* A **third, newer encoding** exists in EXStar Hub: 184 B = **23 doubles**,
  `fx fy cx cy alpha kc[5] T[3] R[9] error`, **unkeyed** (verified: its R is
  orthonormal as stored). Example: `LeftCCF_bin.txt` fx = 1314.383682 — a
  different (higher-resolution) sensor generation. So the key was dropped in
  later firmware/software.
* Per device there is exactly **one** variant of each record; the older
  `ccf_200x150` set is a second calibration of the same model (different
  doubles, identical trailer).

---

## 10. Unknowns (explicit)

1. **Absolute key for fx, fy, cx, cy** — only `key[0]−key[1] = 12` is forced by
   the data. The shipped default (8, −4, −6, −1) is a hypothesis chosen to
   agree with the in-flash 22-double record; a common additive shift of all
   four would leave every geometric invariant intact. Confidence: medium.
2. **key[20..22] (T)** — assumed 0 because the rig triangle then matches the
   unkeyed record to 2.4 mm and no integer shift improves it; not proved.
3. **Meaning of the 256-byte / 64-digit trailer** — constant per camera role,
   independent of calibration; not the key; purpose unknown.
4. **Meaning of the u32 in the `FAFA`/`FBFB`/`FWFW` headers** (512, 768, 1024,
   0, 1536) — not a payload length (payloads are 73 / 73 / 0 / 8 bytes).
5. **Why the third `FBFB` block (camera 2 colour correction) is empty** in this
   device — not programmed, or overwritten by the CCF block that starts 5 bytes
   later.
6. **The origin/units of the 22-double records' T** (|T| ≈ 550 mm vs the CCF's
   ≈ 270 mm for the same rig) — different reference origin, not explained.
7. **Who applies the key** — it is not in `Sn3DCalibDevFile.dll`,
   `Sn3DCalibrateData.dll`, `EXStar.exe` nor in the FX3 firmware image as a
   literal array (searched int8/int16/int32/float32/float64), so it is
   presumably generated on the device or derived from a seed.
8. Whether the reprojection-error field (index 32) is used by EXStar at all —
   the IR records store 0.0.

---

## 11. Reproducing

```bash
python3 tools/ccf/decode_ccf.py --txt sources/calibration/res_Einscan-E10_200x150/LeftCCF.txt
python3 tools/ccf/decode_ccf.py --bin <device-dump.bin> --json-out /tmp/ccf.json
python3 tools/ccf/decode_ccf.py --print-key
python3 tools/ccf/validate_ccf.py        # 40/40 checks
python3 tests/test_ccf.py                # 18/18 (no pytest needed)
python3 -m pytest tests/test_ccf.py -q   # same 18 tests under pytest
```

Requirements: Python ≥ 3.10 and **numpy** (installed, 2.5.4). `pytest` is
optional (`pip install --user pytest`); the test file carries its own runner.
No scipy, no network, no device access.

Confidence vocabulary used above: *proved* = exact arithmetic invariant
(orthonormality at the 1e-15 level / exact integer residual); *strong* =
unique plausible integer; *hypothesis/assumed* = consistent with all available
evidence but not uniquely determined.
