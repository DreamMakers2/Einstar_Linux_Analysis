# IR stereo matcher

Status: 2026-11-11.  Implementation: `recon/src/stereo.cpp`; tests:
`recon/tests/test_stereo.cpp`.  This is the OpenCV SGBM baseline.  No
projector-pattern-specific branch is required to obtain a usable cloud on the
genuine frames; the speckle acts as texture, and the dominant issue is the
strongly verged geometry, not the pattern.

## Algorithm

1. **Metric window.** Convert the requested depth range to a disparity window
   `[dmin, dmin+num)`, 16-aligned, `dmin` may be negative.  `dmin` is derived
   from `f·B/z + offset`, not guessed.
2. **Padded SGBM.** SGBM can only search non-negative disparities and leaves
   its left border invalid.  The rectified images are padded on the left
   (`num+pad_l` / `num+pad_r`, with `pad_l−pad_r = −dmin`), SGBM searches
   `[0,num)`, and the cropped map is shifted by `+dmin`.  This is the same
   technique as `libeinstar/src/depth.cpp`; the implementation here is
   independent.  `MODE_SGBM_3WAY`, block 5, `P1=8b²`, `P2=96b²`,
   `preFilterCap=63`, `uniquenessRatio=10`, `speckleWindow=100/2`.
3. **Right view.** A second SGBM on the mirrored pair `(flip(right),
   flip(left))`, flipped back.  `cv::ximgproc::createRightMatcher` is optional
   and not used.
4. **Left-right consistency.** The right map is sampled at the physical
   correspondence; a left pixel is valid only if `|dL−dR| ≤ lr_tolerance`
   (default 1 px).  Rejected matched pixels are labelled occluded.
5. **Validity.** Left/right masks, a hard margin near the search-window ends,
   SGBM's speckle filter, and `d > 0`.
6. **Confidence.** `border_factor × (0.65·texture + 0.35·LR_agreement)`, with a
   9×9 local variance texture term; exactly 0 where invalid.  SGBM does not
   expose a cost-margin (uniqueness) measure, so none is claimed.
7. **Convention.** `disparity = (u_l−u_r) + min_disparity`;
   `geometry.disp_offset = min_disparity + P_offset`, so
   `depth_at(d) = f·B/(d−disp_offset)` is exact.  Map values therefore start at
   `2·min_disparity` (half-res real frame: `[224,736)`).

## Measured (real frame 0, factory profile)

| scale | valid | occluded | map p05/50/95 | physical median | depth at median | time |
|---:|---:|---:|---|---:|---:|---:|
| 0.5 | 17.3 % | 3.1 % | 352.1 / 382.0 / 409.2 | 270.0 px | 340.1 mm | 0.10–0.16 s |
| 1.0 | 17.0 % | — | 704.3 / 761.2 / 817.2 | 537.2 px | 341.9 mm | 1.21–1.30 s |

The 17 % figure is **17 % of the full image**, but only ~58 % of the image lies
in the common rectified wedge, so it is ~29 % of the matchable overlap.  The
earlier 39–41 % figure was raw SGBM validity *before* LR consistency and mask
intersection.  Both are correct measurements of different stages.

## Test coverage (all passing)

* known integer shift 60 px, `dmin=16`: map 76.0000, physical 60.0000,
  `depth_at` = f·B/60 = 386.12 mm, valid 89 %, LR agreement 97.4 %;
* half-pixel shift: raw 60.5000, refined 60.5312 (the 3×3 parabola is a small
  refinement; SGBM's own 0.15–0.28 px bias at quarter-pixel shifts is not
  removable this way and is documented, not hidden);
* synthetic planes 300/450/600 mm: 300.05/450.03/599.98 mm (≤0.02 %);
* negative offset (`off=−200`, `dmin=−96`): 300.05 mm;
* occlusion strip: 92 % invalid/occluded; 2.5× exposure: 86 % coverage retained;
* flat vs textured confidence: 0.33 vs 0.76;
* bit-identical maps across runs; ten invalid-input rejections.

## Limitations

* SGBM is a local matcher: large textureless regions and repetitive speckle can
  produce low-confidence or biased disparity; the confidence and LR checks
  reject most of it, but no learned/global optimisation is used.
* The sub-pixel refinement is a 3×3 parabola; it does not remove SGBM's
  quarter-pixel bias.
* Absolute depth accuracy is unverified (no ground-truthed object).
* A future pattern-aware or learned matcher can replace this backend behind the
  same `StereoResult` interface; the rest of the pipeline does not change.
