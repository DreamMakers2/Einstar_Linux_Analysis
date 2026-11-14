# EXStar's fresh Windows calibration vs the factory and vs our Linux candidate

Date: 2026-11-14.  Subject: the calibration EXStar wrote on 2026-11-14 00:13–00:14
on the same physical Einstar, compared with the vendor calibrations already on
disk, with the calibration records stored **inside the scanner**, and with the
Linux candidate recovered from session-03.

Numbers come from `analysis/exstar_reference/ccf_decoded_all.json`,
`analysis/exstar_reference/rig_geometry.json`, `tools/ccf/decode_ccf.py`,
`tools/ccf/validate_ccf.py` (40/40 checks) and `docs/calibration/ccf_format.md`.
No parameter below was estimated by fitting in this document; §6 quotes one
earlier fit and names it.

---

## 0. Answer first

| question | answer |
|---|---|
| Does EXStar expose calibrated parameters? | **Yes.** Plain (obfuscated) IEEE-754 records on disk — the `CCF` format we decoded earlier. We read EXStar's own numbers exactly. |
| Did the new calibration change the rig? | **Not materially.** IR baseline 158.5570 → 158.6128 mm (+0.035 %); IR relative rotation 22.1383° → 22.1290° (−0.0093°). |
| Did it change the intrinsics? | **Slightly.** `fx` −0.67 px (left), −1.11 px (right), −0.15 px (colour); `cx/cy` by up to 1.52 px. |
| Did it change distortion? | **No — bit-exactly identical** in all three cameras, and identical to the *device's* factory records. EXStar has never re-estimated distortion on this unit; it inherits it. |
| Did it change white balance? | **Yes.** Diagonal gains R 1.6227 → 1.6035, B 1.7661 → 1.7940. The 3×3 colour-correction matrix was not re-derived (byte-identical). |
| Does this explain our +0.92 % baseline? | **Yes — and it inverts the conclusion.** The scanner itself stores *two* vendor calibrations that differ by 1 %: an older unkeyed factory record (160.092 mm) and the CCF that EXStar writes on recalibration (158.557 / 158.613 mm). **Our 160.014 mm matches the device's factory record to 0.08 mm (0.05 %)**; it is the CCF that is the outlier. §5. |
| Is EXStar's new calibration independent evidence? | **No.** It froze distortion bit-exactly and moved the baseline by 0.035 %, consistent with a rapid recalibration anchored to the previous CCF. Strong *stability* evidence, weak *absolute* evidence. |

---

## 1. Provenance

All files were read from a read-only NTFS mount
(`<EXStar-install>/Shining3d/EXStar/res/Einscan-E10/`).
Nothing on that volume was written.  The scanner was not touched for this
comparison.

| set | where | written | meaning |
|---|---|---|---|
| **B — install (2024)** | `sources/calibration/ccf_200x150/` | 2024-06-28 | CCF shipped with the installed EXStar build |
| **A — previous EXStar calibration (2026-02)** | `sources/calibration/res_Einscan-E10_200x150/` ≡ `…/backup/bak/` | 2026-02-09 21:31–21:35 | the CCF that was live until 2026-11-14 |
| **F — device factory records** | `~/einstar-private/calibration.bin` (6568 B, read twice, identical) | read 2026-11-07 | the device's own unkeyed 22-double camera records |
| **N — new EXStar calibration (2026-11-14)** | `…/200x150/` | 00:09:52 → 00:14:55 | this run |
| **L — Linux candidate** | `analysis/calibration/solver/final/profile.json` | 2026-11-13 | our session-03 fit; never activated |

SHA-256 of the CCF records:

```
A  Left  6879a68d6926caebe19bdaee9a09dd05c50ee03f23cf6c5158801dcf8b5621d7
A  Right 45b80e2aa8bcece7fe400d79f42c852f67824de0b478f0ea1a0c3aa0b6cf1dc9
A  Tex   7b0477a524b3af59d6907445f0e289da6f73891ac1714451de502bb240f84e64
N  Left  7061f68a14809f48b592a2e9e37c9cf194d06c253786f0832e25fa3e8be73f13
N  Right a31a0119ed19cf575efbd740b5738bfb8a59427cd319df2b1dec46e346ef33bd
N  Tex   6e8a74169f79ad8481ec4f51116fe3e0a3cecae0f0a42b39107992454c4ffec9
N  wbdata b083003d6ef2351ecc390fb5cd2ea40da30466149f9099fde12563c853d6ed85
N  ParaCCM_0 cc2133df… == A ParaCCM_0 (UNCHANGED CONTENT)
N  ParaDark_0 e675f08e… == A ParaDark_0 (UNCHANGED CONTENT)
```

Write timeline observed on disk (+01:00):

```
00:09:52.65  ParaCCM_0 / ParaDark_0 rewritten with identical bytes   (session start)
00:13:11.94  LeftCCF, RightCCF, TexCCF committed                      (geometric result)
00:13:12.50  calibrate_image_read_rapid/  (25 x 3 calibration images)
00:14:54.31  wbdata.txt / wbdata_noled.txt committed                  (white balance)
00:14:55.00  calibrate_image_wb/          (2 x 3 white-balance images)
```

The on-disk session is therefore **5 min 03 s**: 3 min 19 s before the geometric
result, 1 min 42 s between the geometric and white-balance results.  The supplied
screen recording is 66.4 s and its container is stamped by a consumer video
editor (`encoder=https://clipchamp.com`), so it cannot be assumed to be an
unedited real-time capture of those five minutes; see
[`exstar_reference_video_analysis.md`](exstar_reference_video_analysis.md).

### 1.1 A decoding trap worth recording

The IR records store `true + key` where the key is a vector of integers.  The key
is only *proven* for the rotation and distortion blocks (orthonormality, and an
exact integer difference against the device's unkeyed records).  For `fc` only
the **difference** `key[fx] − key[fy] = 12` is forced, and the pair that makes the
lens square and matches the device records is `(8, −4)`.

Quoting the stored doubles as if they were focal lengths gives a *wrong* `fx`
that is 8 px too large and a `fy` that is 4 px too small (1167.115 / 1155.021
instead of **1159.115 / 1159.021**) — a 1 % error in `fx` that looks like a real
disagreement between calibrations.  The repo's factory profile already used the
correct key; an earlier draft of this document did not, and the correction
changed the conclusion materially.  Always decode with `key_mode="best"`.

---

## 2. What EXStar writes

| file | size | content | notes |
|---|---:|---|---|
| `LeftCCF.txt`, `RightCCF.txt` | 520 B | 33 f64 (`fc, cc, alpha, kc[5], T[3], R[9], error`) + 256 B int32 trailer | IR records are keyed; the trailer is a constant per role |
| `TexCCF.txt` | 264 B | the same 33 f64, **not** keyed | colour camera |
| `wbdata.txt`, `wbdata_noled.txt` | 72 B | 9 f64 = `diag(R,1,B)` channel gains | LED-on and LED-off |
| `ParaCCM_0.txt` | 60 B | 15 f32 = 3×3 CCM + 3 offsets + 3 reference gains | slot `_0`; `_1`,`_2` untouched since 2024 |
| `ParaDark_0.txt` | 12 B | 3 f32 per-channel dark level `[0.045, 0.0455, 0.045]` | unchanged |
| `activeBox.dat` | 48 B | 6 f64 `[-3000,-3000,-3000,-3000,200,200]` | working-volume box, not a calibration result |
| `calibrate_image_read_rapid/image{Left,Right,Tex}1..25.bmp` | 25×3 | 1280×1024 8-bit raw frames | **the vendor's own calibration observations** |
| `calibrate_image_wb/image{Left,Right,Tex}{1,2}.bmp` | 2×3 | same | white-balance frames (colour = raw Bayer) |

Not written: any per-view residual, acceptance log, coverage map, covariance or
quality score.  The only self-reported number is field 32 (`error`), which is
**0.0 in both IR records** (as in every previous vendor record) and **0.0559 px
in the new colour record**.  An identically-zero IR residual is not a live
quality measure.

---

## 3. Intrinsics before/after (decoded correctly)

| camera | quantity | A (2026-02) | N (2026-11-14) | N−A | L (Linux) | L−A |
|---|---|---:|---:|---:|---:|---:|
| IR left | fx | 1159.1154 | 1158.4407 | **−0.6747** | 1159.3463 | **+0.2309** |
| | fy | 1159.0213 | 1158.9799 | −0.0414 | 1158.0798 | −0.9415 |
| | cx | 623.9122 | 617.0906 | −0.8216 | 623.9122 | 0 (held) |
| | cy | 534.5629 | 533.4196 | −0.1433 | 534.5629 | 0 (held) |
| IR right | fx | 1158.0869 | 1157.8806 | **−1.1063** | 1158.5513 | **+0.4644** |
| | fy | 1157.7186 | 1157.4722 | −0.2464 | 1157.0961 | −0.6225 |
| | cx | 635.5014 | 629.2741 | −0.2273 | 635.5014 | 0 (held) |
| | cy | 494.5091 | 493.1237 | −0.3853 | 494.5091 | 0 (held) |
| colour | fx | 1141.0776 | 1140.9293 | −0.1483 | 1141.3168 | +0.2392 |
| | fy | 1141.0873 | 1141.3785 | +0.2912 | 1139.9735 | −1.1138 |
| | cx | 633.1024 | 631.5846 | **−1.5178** | 633.1024 | 0 (held) |
| | cy | 521.1250 | 521.6097 | +0.4847 | 521.1250 | 0 (held) |

**Our intrinsics agree with the manufacturer's to ≤1.1 px on every camera and
every quantity.**  The earlier appearance of a −7.8 px `fx` disagreement was the
decoding trap of §1.1, not a calibration difference.

Two behaviours remain worth noting:

* EXStar *does* re-estimate `fx` on recalibration (−0.67 px, −1.11 px): a
  "rapid" recalibration is not "intrinsics untouched".
* `cx/cy` in our candidate are the factory values by construction (the model
  hard-fixes them); the vendor's own refinement moved `cx` by up to 1.5 px, which
  bounds how much a fixed principal point can be costing us.

---

## 4. Distortion: held by the vendor, inherited from the device

All five Brown–Conrady terms in `N` are **bit-identical** to `A`, for all three
cameras.  Stronger: decoding the device's unkeyed factory records and removing
the integer key reproduces the CCF `kc` **exactly** (`tools/ccf/validate_ccf.py`
checks this, residual ≤ 6.3e-16).  The chain is

```
device 22-double factory record  --(same kc, exactly)-->  CCF 2026-02  --(bit-identical)-->  CCF 2026-11
```

so **distortion has not been re-estimated on this unit since it was manufactured**;
every vendor recalibration inherits the factory distortion.  Our decision to hold
distortion and free only the remaining parameters is therefore the manufacturer's
own structural choice, independently of ours.  It also means the vendor CCF
cannot be used to adjudicate a free-distortion model — EXStar never fits one.

---

## 5. Rig geometry: the 0.92 % is between two vendor datasets

Relative pose between the two IR cameras, from `R_rel = R_R·R_Lᵀ`,
`t_rel = T_R − R_rel·T_L`.  `T`,`R` are per-record and their world frame is the
last target pose, so only the **relative** transform is comparable (e.g. `|T_L|`
is 279.9 mm in A and 381.7 mm in N — the last pose differed).

| quantity | F (device factory) | A (CCF 2026-02) | N (CCF 2026-11) | L (Linux) |
|---|---:|---:|---:|---:|
| IR baseline \|t_rel\| (mm) | **160.0916** | 158.5570 | 158.6128 | **160.0137** |
| IR relative rotation (°) | 22.0859 | 22.1383 | 22.1290 | 22.1300 |
| colour↔left translation (mm) | **122.8774** | 125.3176 | 125.3362 | **122.8779** |
| colour↔left rotation (°) | 16.9983 | 17.0049 | 17.0004 | 17.0836 |
| right↔colour translation (mm) | **37.2149** | 38.6281 | 38.6443 | **37.1367** |
| L − F | | | | **−0.078 / +0.0005 / −0.078 mm** |
| L − A | | | | **+1.457 / −2.440 / −1.491 mm** |

Relative translation vectors (mm):

```
        [−157.143, −0.202, 30.583]  F      [−157.064, −0.216, 30.582]  L
        [−155.469, −0.235, 31.139]  A      [−157.064, −0.216, 30.582]  L
        [−122.241, −0.108, 12.486]  F      [−122.234, −0.077, 12.562]  L
        [−122.986, +16.894, 17.134] A      [−122.234, −0.077, 12.562]  L
```

**Our independently fitted rig reproduces the device's factory records to
0.08 mm on the IR baseline and 0.0005 mm on the colour↔IR translation.  The CCF
differs from the factory records by 1 % (1.46 mm and 2.44 mm).**  In other words:

* the scanner carries **two mutually inconsistent vendor calibrations**, 1 %
  apart in baseline and 2.4 mm apart in the colour extrinsics;
* EXStar's recalibrations reproduce the *CCF* branch (158.56 → 158.61 mm,
  0.035 %) and leave the factory branch untouched;
* our recovered calibration lands on the **factory branch**, not on the CCF.

The "+0.92 % scale discrepancy" is therefore not evidence that our fit is wrong.
It is evidence that the two vendor datasets disagree, and that we agree with one
of them.  The disagreement is angular as well as metric, and here the factory
records are unambiguous about *which* datum our fit reproduces:

| relative-translation direction, angle between datasets (°) | F vs A | F vs N | F vs L | A vs L |
|---|---:|---:|---:|---:|
| IR left ↔ IR right | 0.3132 | 0.3034 | **0.0073** | 0.3077 |
| colour ↔ IR left | **8.0739** | 8.0490 | **0.0385** | 8.0509 |
| IR right ↔ colour | **26.8837** | 26.8331 | **0.1278** | 26.8058 |

Our rig reproduces the device's factory records to **0.007° / 0.039° / 0.128°**,
while the host CCF is 0.31° off on the IR pair and **8°** off on the colour
camera.  An 8° colour-extrinsic error is not a calibration subtlety: it is a
defect, and it is the same defect our earlier colour work measured from the other
side (the shipped CCF colour extrinsic gives 31.3 px RGB epipolar residual on our
images, versus 0.63 px for the fitted one —
`analysis/calibration/solver/final/validation.json`).  The device's own records and
our independent fit agree with each other and disagree with the CCF.

This also withdraws an earlier draft's "0.30° baseline-direction error": that
number is the CCF's error, not ours.

Which branch is *physically* correct is still unknown, and this document does not
settle it.  What can be said:

* the factory records are older, unkeyed, and were written at a different
  working distance (`|T| ≈ 550 mm` vs `≈ 270 mm` for the CCFs);
* the CCF branch is what EXStar has produced twice, but the second time with
  distortion frozen and only a 0.035 % baseline move — consistent with anchoring,
  not with an independent re-measurement;
* no vendor dataset is an absolute length reference, so neither branch can be
  declared metrologically correct from these files.

---

## 6. What would arbitrate, and what our own numbers say

Pre-existing evidence on our side (quoted, not re-derived here):

1. **Epipolar consistency on our own images** (session-03, 293 views,
   `analysis/calibration/solver/final/validation.json`): Sampson RMS **0.333 px**
   with our rig against **0.465 px** with the CCF rig.
2. **Refitting the vendor's own 25 calibration images** with our estimator
   reproduces a baseline ~1 % above the CCF (`docs/calibration/plate_spec_recovered.md`
   §7, `analysis/calibration/audit/pitch_sensitivity.json`) — the same branch our
   session-03 fit lands on, on data we did not capture.  The fresh 2026-11 vendor
   corpus is re-fitted in [`exstar_vendor_corpus_refit.md`](exstar_vendor_corpus_refit.md).
3. **The device factory records** (§5) are a second, older vendor statement that
   puts the IR baseline at 160.09 mm and the colour camera 122.88 mm from the left
   camera — which is where our fit lands, to 0.08 mm and 0.0005 mm.

So two of the three vendor statements (both the ones that did not come from
EXStar's host-side rapid recalibration) agree with our recovered rig, and the
third is internally inconsistent with the device records on the same unit.

Both point the same way: this is a **vendor-dataset disagreement plus a solver
convention question**, not a defect of our capture.  It still cannot be
adjudicated numerically:

> reprojection error is invariant to the target's metric scale, and both vendor
> branches were produced with the same nominal plate (28.0 mm pitch, proven from
> the vendor's own `iSpace = 14.00`).  No local artefact has a certified length.

**What would settle it:** one independent length measurement — the plate pitch
measured with a calibrated instrument (caliper/CMM, ±0.05 mm on 28 mm is enough),
or a vendor metrology sheet.  That is a small, well-defined physical action, and
it is the only one that can turn "standard-target candidate" into a
dimensionally-validated one.

Until then: no candidate is activated, and agreement with EXStar is not treated
as proof of correctness in either direction.

---

## 7. White balance before/after

Channel labels follow the vendor file, not our debayer: EXStar's slot 0 is the
Bayer phase at (odd, odd), which in a `COLOR_BayerRG2BGR` buffer lands in slot 2.
The doc therefore says "slot 0 / slot 2" and never "R / B"; the conversion and
its 10 % trap are in [`white_balance.md`](white_balance.md) §5.

| quantity | A (2026-02) | N (2026-11-14) | change |
|---|---:|---:|---:|
| `wbdata` slot 0 gain | 1.622664 | 1.603466 | −1.18 % |
| `wbdata` slot 2 gain | 1.766063 | 1.794047 | +1.58 % |
| `wbdata_noled` slot 0 / slot 2 | 1.625985 / 1.766387 | 1.605239 / 1.796383 | −1.28 % / +1.70 % |
| CCM 3×3, offsets, reference gains | unchanged bytes | unchanged bytes | — |
| dark levels | unchanged bytes | unchanged bytes | — |

Only the diagonal channel gains changed.  White balance is therefore a
**separate, versioned, reversible gain layer** in the vendor design — not part of
the geometric CCF and not part of the CCM.  The measurement method was recovered
from the shipped binary (centred-window grey-world, dark-subtracted, green
pinned to 1, clipped pixels rejected) and our implementation is in
[`white_balance.md`](white_balance.md).

---

## 8. What cannot be recovered

* **EXStar's acceptance criteria** — no per-view log exists.
* **The IR residual** — field 32 is 0.0 for every IR record.
* **Which vendor branch is physically correct** — needs an external length.
* **The vendor's weighting/robust loss/priors** — the distortion freeze and the
  0.035 % baseline move bound the anchoring, they do not reveal its form.
* **Whether EXStar uploads the new CCF to the device** — the 2026-11 dump was not
  available at the time of writing; see
  [`exstar_hardware_state.md`](exstar_hardware_state.md).

---

## 9. Actions this justifies

1. **Keep the candidate research-only.**  Nothing here changes the activation
   verdict: standard-target, scale now *corroborated but not certified*, D7 still
   open.
2. **Stop calling it a "0.92 % baseline discrepancy".**  Call it what it is: our
   fit agrees with the device's factory calibration to 0.05 %; the vendor's CCF
   branch differs from its own factory branch by 1 %.
3. **Record the decoding trap (§1.1)** so no future comparison repeats the 8 px
   `fx` error.
4. **Adopt the vendor's proven structural choices:** hold distortion; keep white
   balance a separate diagonal layer; expect only sub-pixel intrinsic movement
   from a recalibration.
5. **Add "measure the plate pitch with a calibrated instrument" to the list of
   small physical actions** — it is cheaper than a length artefact and would
   resolve the branch question directly.
6. **Use the vendor's fresh 25-image corpus as a regression dataset** — it is a
   real, independent capture of the same plate and rig.
