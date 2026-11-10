# Reconstruction validation plan (M3)

Three independent levels.  They answer different questions and must never be
conflated in a report.

| level | question | evidence | what it can prove |
|---|---|---|---|
| 1. synthetic ground truth | does the geometry/matcher implement the intended model? | rendered textured planes with known depth, exact camera model, distortion | correctness of conventions, rectification, disparity, depth, PLY; **not** physical accuracy |
| 2. real-data consistency | does the shipped calibration reconstruct a plausible, repeatable surface from genuine frames? | the private three-camera triplets; L-R consistency, plane fits, cross-frame repeatability, confidence calibration | internal consistency and gross-failure detection; **not** absolute accuracy |
| 3. physical metrology | is the metric result true in millimetres? | calibrated sphere bar / flatness artefact, measured on the user's unit | physical accuracy and scale (only with hardware) |

## 1. Synthetic tests (automated)

* projection/triangulation round trip: a known 3-D point projects to the exact
  disparity `f·B/Z` in both cameras (1e-9);
* rectification: epipolar lines horizontal, `P2` baseline sign/scale, valid
  masks cover the field, `alpha=-1` keeps the full verged view;
* disparity: integer and half-pixel shifts recovered within 0.1 px; depth at
  250/400/600 mm within 3 % (target 2 mm);
* occlusion: an occluder present in one view is invalid/occluded, not a wrong
  depth;
* exposure: 2.5x brightness difference retains >70 % of the equal-exposure
  coverage;
* confidence: higher bins have lower L-R residual and lower plane error;
* PLY: vertex count matches, units/frame/calibration id in the header, ASCII and
  binary round trip;
* invalid inputs: missing camera, wrong size, non-orthonormal profile,
  degenerate baseline, empty depth range → explicit rejection, no output;
* determinism: repeated reconstruction is byte-identical (cloud/PLY);
* record/replay: replayed stream produces the same `FrameSet` hashes as the
  recording; corrupted recording is rejected.

## 2. Real-data acceptance criteria (consistency, not accuracy)

For each available triplet, with the factory profile:

* stereo valid fraction ≥ 20 % at scale 0.5 (measured prototype: ~40 %);
* median depth in [150, 800] mm and p95 ≤ 800 mm;
* left-right consistency of valid pixels: median |Δ| ≤ 0.5 px;
* largest planar patch (RANSAC) RMS ≤ 2 mm and thickness ≤ 5 mm;
* repeatability across the three triplets: median depth differs by ≤ 5 %;
* confidence calibration: top-quartile confidence has ≤ 0.7x the plane RMS of
  the bottom quartile;
* no crash/NaN; report colour status explicitly.

Failure of any criterion is documented with the cause (saturation, occlusion,
texture-less region, calibration mismatch, unsupported environment), not hidden
by parameter tweaking.

## 3. Physical metrology (future, hardware-dependent)

Follow VDI/VDE 2634 Part 2/3 and ISO 10360-13:

1. **Sphere-spacing / length test** — a calibrated sphere bar or length standard
   measured at 200/300/400/500/600 mm and at field edges; report the deviation
   from the calibrated length (scale + distortion + depth error).
2. **Flatness / form test** — a calibrated flat plate at several distances and
   tilts; report flatness deviation and noise.
3. **Repeatability** — repeated captures of the same artefact (ISO 5725):
   within-session and between-session standard deviations.
4. **Probing / reversal** — approach direction and hysteresis at the volume
   extremes.
5. **Scale traceability** — the plate pitch (28.0 mm nominal) is the only scale
   anchor in the current system; a 1 % pitch error moves the baseline by 1.58 mm
   and is invisible in reprojection.  The artefact test is the only way to
   decide between 28.0 and 27.72 mm effective scale.

Until step 3 is executed on the user's unit, every report must say:

> **Accuracy unverified.**  Results are geometrically consistent with the
> recovered calibration; absolute metric accuracy is traceable only to the
> nominal 28.0 mm plate pitch.

## 4. Failure modes to test explicitly

| failure | expected behaviour |
|---|---|
| wrong stream order / missing 180° rotation | real-data coverage collapses; the pipeline must not "succeed" with a plausible cloud |
| stale/missing colour profile | geometry proceeds; `color_status` explains the skip |
| saturated IR image | valid mask drops the saturated pixels; no invented depth |
| texture-less patch | low confidence / invalid; no smooth false surface |
| occluded region | invalid/occluded, not a wrong depth |
| wrong pair (left-left) | low valid fraction and L-R inconsistency; rejected by acceptance criteria |
| corrupted recording | replay tool refuses before processing |
| calibration pitch change | depth scales; the report must state the profile id and pitch |
