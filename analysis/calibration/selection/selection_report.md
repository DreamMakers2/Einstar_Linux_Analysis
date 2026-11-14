# Selection comparison: what the 297 accepted views were worth

Status: offline comparison.  No activation, no commit.  The candidate model is
the hard-PP model of `analysis/calibration/solver/final/` (factory distortion
held, `cx/cy` hard-fixed at the factory value, `fx/fy` per camera + the six-DoF
right/tex extrinsics free, per-view poses nuisance).

## 1. What was built

| stage | artefact | numbers |
|---|---|---|
| feature cache | `analysis/calibration/offline/features/features.jsonl` | 13,195 groups, real 10 Hz `host_time_ns`, PnP pose + RMS, quality, marker counts, lattice-labelled uv |
| selection (b) | `groups_b.json` | from the 297 live-accepted groups: 215 eligible -> **150** selected |
| selection (c) | `groups_c.json` | from the full corpus: 8,091 eligible -> **200** selected (the greedy picked the four raw corner views 89/5682/4019/6241 on merit) |
| extraction | `einstar-stream-frames --groups-file ... --single-pass --fast-pacing` | union of (b)+(c) = 343 groups, 1,029 PGMs, **54 s**, 42.3 GB replayed at 784 MB/s (`extraction.json`) |
| detection | production localiser: `tools.wizard.detection.detect_one` + `calib.localize.refine_grid(method="auto")` | 343/343 groups with both IR, **313 s** |
| fits | hard-PP model, identical validation protocol for all three variants | §3 |

Selection rule (both variants): eligible = both-IR markers >= 28, shared >= 12,
IR focus >= 150, contrast >= 14, saturation <= 0.02, pose RMS <= 1 px; then a
greedy round-robin over the `(distance band x 3x3 image cell)` grid that always
takes the candidate farthest from the selected set in the descriptor
`rotvec_deg + distance/50mm + plate_centre/250px`, with a 0.6 separation floor.
This is a coverage-stratified farthest-point selection, not a blind top-N.

## 2. Coverage (tools.wizard.coverage, one common measurement source)

| variant | views | D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | satisfied |
|---|---|---|---|---|---|---|---|---|---|---|
| (a) 297 accepted | 276 | ok | ok | ok | **FAIL** (8/9 regions, top-right 0) | FAIL | ok | **FAIL** | ok | 5/8 |
| (b) 150 of the 297 | 150 | ok | **FAIL** | ok | **FAIL** | FAIL | ok | **FAIL** | ok | 4/8 |
| (c) 200 of the corpus | 200 | ok | ok | ok | **ok** (9/9, corners 21/5/14/17) | FAIL | ok | **FAIL** | ok | **6/8** |

- **D4 is the headline**: the full-corpus selection satisfies the image-corner
  requirement the live set could not (top-right: 0 views accepted, 5 selected
  from the corpus).  The corner poses were in the recording; the acceptance rule
  never took them.
- **D7 fails in all three and is not hidden**: the raw 10 Hz trace gives **2
  qualified dwell episodes** (>= 2 samples, >= 0.3 s) against the required 6.
  17.7 % of samples are stationary, median speed 44.9 mm/s.  D7's quality
  sub-check reads `focus`/`saturation_frac` at the view level, which this
  offline adapter did not supply, so that half is "unestablished" (measurement
  gap), while the dwell half is a genuine fail.
- **D5 (stereo overlap) fails everywhere** because it is computed from the
  cache's `assignment_ok`, which flags 202/297 of the live-accepted views as
  ambiguous while the live decisions recorded all 297 as assigned.  D5 is
  therefore *not* comparable with the live run; D1-D4, D6, D8 are.

## 3. Identical validation of the three variants

| metric | (a) 297 accepted | (b) 150 of the 297 | (c) 200 of the corpus |
|---|---|---|---|
| views fitted (after cull) | 293 (4 culled) | 145 (5 culled) | 165 (35 culled; 6 left-camera groups were uninitialisable and dropped) |
| residual RMS px | **0.651** | 0.700 | 1.002 |
| view RMS median / p90 / max | 0.469 / 0.953 / 1.930 | 0.481 / 1.150 / 1.913 | 0.625 / 1.714 / 2.264 |
| rig condition (scaled) | 1804 | 1772 | **804** |
| rank / weak directions | 18/18, 0 | 18/18, 0 | 18/18, 0 |
| sigma_fx L / R / T (px) | **0.203 / 0.182 / 0.197** | 0.268 / 0.238 / 0.242 | 0.220 / 0.198 / 0.208 |
| gate counts (c/w/u) | 17 / 1 / 0 | 16 / 2 / 0 | **18 / 0 / 0** |
| held-out (3 folds) mean +/- sd | **0.770 +/- 0.019** | 0.837 +/- 0.065 | 1.204 +/- 0.089 |
| held-out per camera | L 0.41, R 0.46-0.48, T 1.11-1.18 | L 0.42-0.43, R 0.48-0.50, T 1.15-1.41 | L 0.43-0.48, R 0.54-0.56, T 1.77-2.06 |
| epipolar Sampson IR fitted / factory | **0.333 / 0.465** | 0.366 / 0.501 | 0.551 / 0.727 |
| epipolar Sampson RGB fitted / factory | 0.63 / 31.3 | 0.745 / 31.96 | 1.106 / 33.09 |
| baseline fit vs factory | 160.014 vs 158.557 (+0.92 %) | 160.079 (+0.96 %) | 160.002 (+0.91 %) |
| IR relative rotation | 22.130 vs 22.138 deg | 22.135 | 22.136 |
| runtime (main fit / total) | 104 s / - | 10.4 s / 37 s | 32.5 s / 63 s |

All three fits report full rank and zero weak directions; the differences are
in the *size* of the covariance, not its rank.

## 4. Is (c) measurably better than (a)?

**Better on coverage and conditioning, worse on precision.  Not uniformly
better — different.**

- (c) satisfies D4 (image corners) and has the best-conditioned rig
  (condition 804 vs 1804 — the corner/edge and depth diversity really does
  decorrelate the intrinsics from the poses).
- (c) is worse where it counts for the candidate: residual RMS 1.00 vs 0.65 px,
  held-out 1.20 vs 0.77 px, epipolar 0.55 vs 0.33 px, sigma_fx 0.22 vs 0.20 px.
  The loss is not the selection rule: 35 of the 200 corpus-selected views
  (17.5 %) are unusable outliers (plus 6 left-camera detections that cannot even
  be initialised) and the corpus
  contains many 560-800 mm views where the plate covers little of the image, so
  the texture camera alone contributes 1.8-2.1 px of held-out error.
- (b) is strictly worse than (a): the same acceptance pool with a quarter of the
  views loses D2 and D4 as well as precision.  Redundancy, not diversity, is
  what the 297 were buying.

**Were the 297 sufficient?**  For *parameter precision* at the level the
candidate claims (sigma_fx 0.20 px analytic, ~0.3-0.4 px split-half), yes.
For *corner/principal-point observability*, no: D4 was unsatisfiable from the
accepted set, and the recording did contain qualifying corner views.
The right fix is not to replace the 297 with 200 corpus views but to add the
corner/edge views to the accepted set: adding ~20 views to 293 changes
sigma_fx by < 4 % (1/sqrt(N) scaling) while satisfying D4, whereas replacing
the set costs ~20 % of the residual quality and ~55 % of the held-out quality.

## 5. Honest limits

1. The deep-zoom views (560-800 mm) in (c) are genuinely less informative per
   view; a selection rule that maximises coverage alone will keep taking them.
   A precision-aware rule should weight by predicted variance reduction, not by
   cell emptiness.
2. D5/D7 in this offline adapter are not comparable with the live session
   (assignment witness and quality-key shape differ); D4's fail in (a) and pass
   in (c) *is* comparable because it only uses poses and marker pixel positions.
3. The 35 culled views and 6 uninitialisable left-camera groups in (c) are a
   property of the corpus detections, not of the model; a production selection
   would have to reject them at acceptance time (the live orientation gate does).
4. No activation, no commit; these are analysis artefacts only.
