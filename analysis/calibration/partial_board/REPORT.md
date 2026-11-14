# Partial-plate and roll-sector recognition: causes, fixes, measurements

Agent A (detection/CV). All numbers below were produced by the code in this
change set, on the read-only session `<calibration-session>`
(real pixels only) plus controlled transforms of those pixels. Nothing here
opens a scanner, writes into a session, or commits private images.

Reproduce with:

```
.venv/bin/python -m tools.wizard.partial_bench --max-views 8 \
    --families base,clip_edge,clip_corner,rotate,tilt,rejected \
    --out analysis/calibration/partial_board/measurements_after.json
.venv/bin/python -m pytest tests/test_detect_partial.py \
    tests/test_orientation_witness.py tests/test_localize.py tests/test_plate_geometry.py -q
```

The machine-readable summary of every before/after number in this file is
`analysis/calibration/partial_board/summary.json`.

---

## 1. What was measured, and against what ground truth

`tools/wizard/partial_bench.py` (new) replays real stored frames and applies
and records a transform family:

| family | transform |
|---|---|
| `base` | the stored frame, unchanged |
| `clip_edge` | 10/20/30/40 % cut from each of the four edges, as a zero **mask** and as a **crop** |
| `clip_corner` | 10/20/30/40 % cut from each of the four corners (crop) |
| `rotate` | 0/90/180/270 deg about the plate centroid (exact, `INTER_NEAREST`) |
| `tilt` | 10/20 deg camera tilt about x and y, `H = K R K^-1`, bilinear |
| `rejected` | the 120 raw frame groups (`frames/frame_*_cam*.pgm`) the capture tool retained from the failed session's tail, including the frames the live path rejected |

Ground truth is exact, not declared: the production store keeps the
observation the controller accepted, whose `image_points` are row-major in
`(v, u)`. A view with the recorded 40/40/40 marker counts has a *verified*
labelling (acceptance required `det(H) > 0` and a 4/4 large-marker witness), so
index `i` is canonical label `(i % 8, i // 8)`. Every transform moves the pixels
and the labels together. For each case the bench reports detection success,
marker count, how many assigned labels equal the true ones, the dominant label
shift of the wrong ones, `det(H)`, the lattice rms and wall time; each variant
also runs `tools.sweep.orient.check_orientation` twice (with the controller's
current no-`image_shape` rule and with the visibility-aware rule).

Three detector variants are compared:

* `strict` — `calib.detect.detect_grid_auto` (what `detect_one` called before),
* `partial` — `calib.detect_partial.detect_grid_partial`,
* `wizard` — `tools.wizard.detection.detect_one`, i.e. the production per-camera
  path including the `prefer_partial` fallback.

Sample: 8 golden views sampled across the recorded roll range x 2 IR cameras x
57 transforms = **912 cases / 32 124 expected marker labels**, plus **240 real
tail cameras** (120 groups x left/right).

---

## 2. Root causes (with counts)

All counts are from `decisions.jsonl` (1416 decisions, 1208 rejected, 208
accepted; the store holds 162 accepted views of the 40/40/40 or partial kind).

### 2.1 The 40-point lattice is symmetric, so "count + residual" cannot choose the labels

The lattice is invariant under the Klein four-group (`identity`, `rot180`,
`mirror_u`, `mirror_v`), so a relabelled fit reproduces the same 40 image
points with the same residual. Two measured consequences:

* **`det(H) < 0` collisions: 170 rejection rows** (91 right, 79 left). On the
  retained tail frames this is reproduced exactly: `detect_grid_partial` used to
  emit a **mirrored** labelling for **86 of 240** cameras (36 %), and the live
  path forwarded **41 of 240** (17 %). The v-sign of the recovered lattice was
  chosen by *how many blobs each sign explains* — on a clipped plate whose
  visible rows are symmetric about the large-marker row the two signs tie, and
  `>` kept the first one, i.e. a coin flip. 16 of the 23 frames with a decision
  in the retained tail were rejected exactly that way
  (`right: mirrored marker geometry (det(H)<0)`).
* **180-degree relabelling with `det(H) > 0`**: on a real tilted frame the
  RANSAC refinement drifted off the four anchors onto the rot180 labelling and
  won by 0.004 px of rms (`view005-left`, 10 deg tilt: 38 markers, 38 wrong
  labels). Only the large-marker witness can catch this, and the old detector
  did not consult it at all — `assign_grid` never looked at `det(H)`.

### 2.2 "No markers" on a partially visible plate

* **304 rejected decisions had 40/40 markers in both IR cameras**; 205 of those
  are legitimate (`too similar to an accepted view`). The rest are lost at
  assignment: **71 rows** are `ambiguous marker assignment (…, identity score
  0/4)` — the detector returned a full 40-point labelling whose large markers
  were *not* where the labels claimed (a shifted/rotated sub-lattice), and the
  witness correctly refused it. The candidate ranking preferred a wrong-identity
  fit because its rms was marginally lower.
* **Whole-camera losses**: 189 rejected decisions have `left >= 30` and
  `right < 20`; of those 62 are `right: no markers` and 44 `right: mirrored`.
  165 decisions have **both IR cameras at 0 markers while the colour camera
  found 40** — the plate was outside the IR fields (or the IR frames unusable);
  that is a physical loss, not a detector fault, and no count changed it here.
* The partial path itself was fragile in two independent ways:
  * `assign_grid_partial` **required the canonical origin to be visible**
    (`uv.min() > 0.5 -> "labelling does not reach the canonical origin"`). A
    clipped plate that has lost its `u = 0` column or `v = 0` row was therefore
    rejected *although the three large markers fix the origin analytically*.
  * it committed to **one** candidate large-marker triple chosen by gap ratio
    alone. On real view 102, two small markers were 1.6-1.7x the median area
    (brightness variation) and produced a spurious triple with a "better" 2:1
    ratio; the true triple was never tried and the result was 12 markers with 11
    wrong labels — even on the *unmodified* frame.
  * it assumed the two lattice axes are **perpendicular with equal pitch**. A
    projected plane is not a similarity: on view 102 the projected axes are
    101.5 deg apart, so the orthogonal seed missed the far rows by > 0.3 pitch
    (22/40 matched).

### 2.3 Roll sectors

Rotating real 40/40 views by 0/90/180/270 about the plate centroid gives
**640/640 correct labels** in every rotation and identical `det(H)` and rms (a
rotation about the plate centre composes `H` with a rotation, `det = +1`), so
the detector is roll-invariant — a roll sector is not mis-assigned per se. The
roll distribution of the 109 accepted 40-marker views (computed from the stored
labels, correlation 0.9995 with the stored `roll_deg`) is

| sector | 0-90 | 90-180 | 180-270 | 270-360 |
|---|---|---|---|---|
| accepted views | 0 | 4 | 73 | 32 |

so the session covered barely two sectors. The tail frames show *where* the
other rolls were lost: `mirrored` rejections cluster at roll −60 … −136 deg
(224-300 deg in `[0,360)`), and in those frames the **right camera had only
12-19 of 40 markers** (left 28-40). The failure is therefore
partial-visibility, not roll: as the plate reached a frame border, the partial
fallback produced the mirrored labelling described in 2.1 and the view was
rejected. Rotating a real view *in the harness* keeps identity correct at every
roll; what fails is a plate that is only partly in frame.

---

## 3. Changes

Only `calib/detect.py`, `calib/detect_partial.py`, `tools/wizard/detection.py`
and new files were touched.

### 3.1 `calib/detect.py`

* `big_marker_witness()` — scores the four large markers for a labelling:
  three on-lattice at `(2,1),(4,1),(5,1)` plus the off-lattice chiral marker at
  `(3.5,3)`, each hit requiring a large blob (`area > 1.55 x` the small-marker
  median) within `0.30` pitch. `det(H) > 0` is part of the verdict; with an
  image shape, a witness predicted outside the usable frame is excused, exactly
  as `orient.check_orientation` does it.
* `_fit_from_anchor()` — keeps the affine seed (a four-point projective solve is
  *degenerate* for three collinear anchors: measured `det ~ 1e-15`), then
  refines **only the small lattice points**, leaving the four large markers out
  of the fit so that the witness stays independent evidence; a refinement that
  moves an anchor by more than `0.35` pitch is rejected (kills the rot180
  drift).
* `assign_grid()` — ranks candidates by `(det>0, witness ok, witness score, n,
  -rms)` instead of `(n, -rms)`, and stops at the first candidate that is
  verified on `>= 34` markers (`early_n`), which keeps the common case at one
  fit. A mirrored labelling is still *returned* when it is the only one (so the
  witness can report `mirrored_geometry`), but it can never outrank a
  front-facing one.
* `GridObservation` gains `witness`, `witness_score`, `identity_ok`.
* `_pattern_candidates()` enumerates anchor triples over the **large blobs
  only**. This is behaviour-preserving (every other triple was discarded by the
  existing threshold) and removed the dominant cost: at the 98.5 percentile a
  clipped frame has ~126 blobs, i.e. 325 000 triples; now it is ~20.
* `detect_grid_auto()` — cross-combination ranking is
  `(identity_ok, witness score, n, -rms)` and the early exit requires a verified
  identity. Two exits: the caller's full-lattice rule, and an *evidence-based
  partial* rule — verified, `n >= VERIFIED_EARLY_MIN = 12`, `rms <= 0.5`.
  12 is the smallest support the wizard uses (`min_shared_markers`) and a free
  PnP fit with 10-12 of the 40 markers reproduces the 40-marker pose to
  **0.093 deg / 0.121 mm median (p95 0.22 deg / 0.37 mm)** on the 109 recorded
  full-board views (farthest-point sampling over the lattice).

### 3.2 `calib/detect_partial.py` (rewritten)

* Enumerates **every** 2:1 collinear triple among the large blobs (not one), and
  — for corner clips that remove exactly one large marker — every **pair** with
  both gap readings (2-pitch / 1-pitch) and both directions. A permutation bug
  in the triple order was fixed in the process (it produced a 3-pitch "long
  gap" for every other blob ordering: pitch 53.7 px instead of 79.3 px on the
  synthetic render).
* `det(H) > 0` is a **hard gate**: the opposite v-sign is a mirror and is never
  selected, whatever its blob count.
* `_refine_basis()` replaces the orthogonal seed with the affine basis the
  blobs actually show (absorbs the 101.5 deg shear; view 102 goes from 22/40 to
  34/40 raw and from 0/15 to 34/34 correct labels).
* Independent witness: for the triple anchor the chiral marker is the witness;
  for the pair anchor the third on-lattice marker *and* the chiral marker are.
  A verified identity needs at least one witness in frame (`witness_ok`), and
  the ranking is `(witness_ok, hits, n, -rms)`.
* Ambiguity is reported, not resolved: two surviving labellings with equal
  support and different labels produce `ambiguous large-marker assignment
  (two labellings with N markers)`.
* The over-strict "must reach the canonical origin" rule is **removed** — the
  anchors already fix the origin.
* The polarity/threshold sweep keeps the best result (it used to return the
  first success) and uses one histogram pass for all six thresholds.

### 3.3 `tools/wizard/detection.py`

* `prefer_partial()` never forwards a mirrored observation, prefers a verified
  candidate over an unverified one, and **skips the partial sweep when the
  strict result is already verified with `>= 12` markers**
  (`MIN_VERIFIED_MARKERS`), which is where most of the decision-path time went.
* `DetectionOutcome` gains `identity_ok` and `reason`.

### 3.4 Thresholds that changed, and why

| constant | before | after | justification |
|---|---|---|---|
| `VERIFIED_EARLY_MIN` | — | 12 | wizard `min_shared_markers`; 10-12 verified markers give 0.09 deg / 0.12 mm PnP agreement with the 40-marker pose |
| `VERIFIED_EARLY_RMS` | — | 0.5 px | same order as the recorded lattice residual (accepted views 0.52-0.88 px); the *exhaustive* sweep changes nothing measurable when the first pass already verifies (3 of 217 stored 40-marker checks gained 1-2 markers) |
| partial `min_lattice` | 12 | 12 (unchanged) | — |
| partial `max_rms_px` | 1.5 | 1.5 (unchanged) | — |
| partial `tol_pitch` | 0.32 | 0.32 (unchanged) | — |
| partial `ratio` (large/small area) | 1.55 | 1.55 (unchanged) | measured corpus 3.4-5.0; 1.55 keeps brightly-lit small markers (up to 1.7x) in the candidate pool, and the witness/lattice fit now rejects the spurious triples they create |
| `big_marker_witness.large_ratio` | — | 1.55 | as above |
| `big_marker_witness.tol_pitch` | — | 0.30 | matches the witness tolerance used by `orient.check_orientation` |
| `assign_grid.early_n` | — | 34 | first verified candidate wins; 34 of 40 leaves room for a clipped corner |
| `min_shared_markers` | 12 | **12 (unchanged)** | see §6: the evidence supports 10 *only together with a verified identity*, which is a controller change, not a detector change |

---

## 4. Measurements

### 4.1 Transform sweep — 912 cases, 32 124 expected labels (8 real views x 2 cameras)

| variant | detected | labels correct | wrong labels | mirrored returns | s/case |
|---|---|---|---|---|---|
| `strict` before | 785 (86.1 %) | 28 182 (87.7 %) | 287 | 16 | 0.035 |
| `strict` after | 785 (86.1 %) | 28 236 (87.9 %) | 118 | **0** | **0.018** |
| `partial` before | 676 (74.1 %) | 23 620 (73.5 %) | 353 | 5 | 0.011 |
| `partial` after | **846 (92.8 %)** | **30 067 (93.5 %)** | 78 | **0** | 0.024 |
| `wizard` before | 800 (87.7 %) | 28 684 (89.3 %) | 251 | 15 | 0.040 |
| `wizard` after | **867 (95.1 %)** | **30 601 (95.1 %)** | 103 | **0** | **0.022** |

Per family, `wizard` after (detected / expected labels correct):

| family | before | after |
|---|---|---|
| `base` | 16/16 cases, 640/640 labels | 16/16, 640/640 |
| `clip_edge` (4 sides x 4 fracs x mask/crop) | 464/512 cases, 16 550/18 172 labels (91 %) | 499/512, 17 613/18 172 (97 %) |
| `clip_corner` (4 corners x 4 fracs) | 192/256, 6 837/8 192 (83 %) | 222/256, 7 572/8 192 (92 %) |
| `rotate` 0/90/180/270 | 64/64, 2 560/2 560 (100 %) | 64/64, 2 560/2 560 (100 %) |
| `tilt` x/y at 10 and 20 deg | 64/64, 2 097/2 560 (82 %) | 64/64, 2 163/2 560 (84 %) |

The 15 residual `wrong_labels` cases are all 30-40 % clips and 20 deg tilts that
retain 6-20 markers; every one of them is returned with `identity_ok = False`
(checked individually), so the witness rejects them and nothing is accepted on a
wrong labelling. `det(H) <= 0` is no longer returned by any variant.

### 4.2 The 240 real tail cameras (the failed session's last 120 groups)

| variant | detected | mirrored (`det<=0`) | s/case |
|---|---|---|---|
| `strict` before | 215 | 15 | 0.089 |
| `strict` after | 215 | **1** | 0.030 |
| `partial` before | 219 | **86** | 0.011 |
| `partial` after | **232** | **0** | 0.042 |
| `wizard` before | 239 | **41** | 0.095 |
| `wizard` after | 239 | **0** | 0.037 |

The one remaining strict `det<0` is a frame where the mirrored labelling is the
only one the anchors support; the wizard drops it (`identity_ok=False`).

### 4.3 Timing (same 24/120 rig frames)

Per **camera** (`detection.detect_one`, 240 tail cameras, 3-camera serial
equivalent):

| | p50 | p95 | max |
|---|---|---|---|
| before (lead's cProfile: 34 `detect_grid` calls per ingest, 655 ms/frame) | ~218 ms | — | ~530 ms |
| after, all cameras | **35 ms** | 150 ms | 292 ms |
| after, partial cameras (`n < 40`, 206 of 240) | **30 ms** | 137 ms | 221 ms |

Per **frame** (3 cameras): p50 **117 ms**, p95 269 ms, max 821 ms (was p50
~655 ms, max ~1600 ms). Two effects: the anchor-triple pool is now the large
blobs (325k -> ~20 triples at the 98.5 percentile) and the partial sweep is
skipped when the strict result is already verified with 12+ markers.

A/B of the partial early exit on the same frames: p50 43 -> 30 ms for partial
cameras, at a cost of mean **0.46 markers** (max 5) on 49/240 cameras, with **no
change in identity** and no change in the verified count (202/240 both ways).
On the 217 stored 40-marker checks, 3 lose 1-2 markers and 0 change identity.

### 4.4 Sweep-b replay (must keep its acceptance)

`tools/wizard/rehearsal.py --source replay-dir --frames ~/einstar-private/sweep-b/frames`:

| run | accepted | rejected | wall |
|---|---|---|---|
| old detection, first 25 groups | 4 | 21 (`too similar to an accepted view`) | 6.5 s |
| new detection, first 25 groups | 4 | 21 (`too similar`) | 5.2 s |
| old detection, full 350 groups | **15** | 335 (`too similar`) | 64.5 s |
| new detection, full 350 groups | **15** | 335 (`too similar`) | 52.8 s |

Acceptance on that corpus is governed by the duplicate gate, not by detection,
and it is identical (15/350, and 4/25 on the same window) before and after; the
end-to-end rehearsal is 18 % faster. The lead's "16/25" figure corresponds to a
different selection window than the default first-N replay, so this table pins
the comparison on identical inputs instead.

### 4.5 The two frames the lead flagged

* **group 4631** (`roll 303.8 deg`, stored: left 26 / right 34 / tex 34,
  rejected `left: mirrored marker geometry (det(H)<0)`): the old strict fit chose
  the mirrored labelling for the left camera. Now: left **26 markers,
  `det(H)=+2353`, `identity_ok=True`**, `orient` = `verified`, 4/4
  (identity 4, mirror_u 2); right 34 markers, `verified`. Recovered.
* **group 4627** (`left 15 / right 30 / tex 29 / shared 10`, rejected only for
  `stereo overlap 10 < 12`): right is **30 markers, verified 4/4**. Left is
  **19 markers but `identity_ok=False`**: the frame has 20 "large" blobs because
  the plate's border at the frame corner produces blobs 2-3x the small-marker
  area, and the chosen labelling puts `(4,1)` and `(5,1)` on 1.0x small markers
  (`area_ratio = [2.0, 1.02, 1.00, 3.19]`). The old 15-marker left observation
  passed the witness only because the old witness was circular for its own
  anchors. So the `shared = 10` that the gate rejected was **not trustworthy**:
  keeping the 12 gate and rejecting the view is the correct outcome, and
  lowering the gate would have admitted a mislabelled camera.

  Independent evidence for a *future* rule: with a verified identity, 10
  markers is geometrically enough (0.093 deg / 0.121 mm vs the 40-marker pose,
  p95 0.22 deg / 0.37 mm; 6 markers already give 0.12 deg / 0.19 mm, so the
  binding constraint is identity, not count). A safe rule would be
  `shared >= 10 AND both IR observations identity_ok AND shared PnP
  reprojection rms <= 1.0 px`, which needs `shared` and the witness flags to be
  computed from *verified* observations in `controller._pre_checks`.

---

## 5. Identity safety

* A mirrored (`det <= 0`) observation is never **selected** by
  `detect_partial.assign_grid_partial`, is never **preferred** over a verified
  candidate by `tools.wizard.detection`, and is always **rejected** by the
  witness (`mirrored_geometry`). When nothing else verifies it is still
  returned, so the operator sees "mirrored marker geometry" instead of a vague
  "no markers detected" (this also keeps
  `tests/test_wizard_failures.py::test_mirrored_marker_layout_is_rejected_at_ingest`
  green).
* `identity_ok` requires `det(H) > 0` **and** every visible large-marker witness
  to be a large blob at its predicted position, with the four large markers
  **excluded from the fit** (otherwise the check is a tautology — that is what
  the old 4/4 score was for the strict path).
* No missing marker is fabricated: the returned points are always detected blob
  centres, and the labels come from a fitted lattice; points that are not
  detected are absent.
* Ambiguity is reported (`PartialResult.ambiguous`, `reason`) rather than
  resolved by a coin flip; ties in support between different labellings are
  rejected.
* New tests pin the contract: a mirror-image frame yields no det>0 labelling
  (`test_partial_never_returns_a_mirrored_labelling`), truncated rows are
  disambiguated by chirality not counts
  (`test_truncated_rows_are_disambiguated_by_chirality`), a clipped-away large
  marker is recovered from the pair + chiral witness
  (`test_one_big_marker_out_of_frame_uses_pair_and_chiral_witness`).

---

## 6. Required change outside this scope (controller)

The live path still calls `orient.check_orientation(gray, grid)` **without
`image_shape`** (`tools/wizard/controller.py:1389`), i.e. the strict 4/4 rule. A
plate whose chiral marker is genuinely out of frame is therefore rejected as
`partial witness 3/4` even though detection verified it with the
visibility-aware rule. Measured on the 240 real tail cameras: **6 cameras**
(2.5 %) are accepted by the visibility-aware rule and rejected by the current
one (`partial_witness`), with 23-32 markers each. The one-line change is

```python
check = module.check_orientation(gray, grid, image_shape=gray.shape)
```

`tools/sweep/orient.py` already implements and tests that rule; it is just not
used by the controller. I did not make this change (out of my file scope).

---

## 7. Remaining risks

1. **Controller witness shape** (§6): partial views whose chiral marker is out
   of frame are still rejected in the live path: 6/240 tail cameras measured.
2. **Unverified observations still flow to non-wizard callers.**
   `tools/calibsession/calibrate_session.py` and `tools/sweep/*` call
   `detect_grid_auto` directly; an observation there can be returned with
   `identity_ok = False` when no verified candidate exists (unchanged behaviour,
   now visible). The wizard path refuses them.
3. **Plates whose only visible large markers are occluded** (not out of frame)
   cannot be verified: the witness rule deliberately does not excuse occlusion.
   `group 4627`'s left camera is an example (a plate-border blob census, not a
   detection bug).
4. **Extreme clips (30-40 % of the frame) and 20 deg tilts** still produce
   unverified 6-20 marker labellings (15 of 912 bench cases). They are rejected
   downstream, but no positive identification is possible there.
5. **Marker count can drop by 1-5** on partial views because of the partial
   early exit (measured 49/240 cameras, mean 0.46). Identity is unaffected;
   `min_shared_markers = 12` is never crossed by this effect on the recorded
   data, but it is a real trade.
6. **Sweep-b acceptance** is unchanged on identical inputs (4/25 first window,
   15/350 full corpus, both before and after; the duplicate gate dominates).
   The lead's "16/25" figure depends on the selection window used by their
   harness, which I could not reproduce with the default first-N replay.
7. The bench's tilt transform simulates a *camera* rotation (`H = K R K^-1`),
   which is exact for a plane; a real plate tilt also changes perspective
   relative to the illuminator, which the bench does not model.
8. **`tests/test_wizard_failures.py::test_missing_markers_scene_cannot_calibrate`
   now fails its `accepted <= 4` bound** (measured 6 of 12 half-visible views
   accepted, was 3). That is the intended effect of the partial-identity work
   and the test's own comment records the old measurement as a number, but the
   bound is in a file outside my edit scope, so I left it untouched for the tree
   owner. The rest of that test (no usable calibration) is unaffected.
9. Concurrent edits by other agents were present in this checkout
   (`controller.py`, `modern_window.py`, `capture.py`, `exposure.py`,
   `calib/session.py`, new `test_roll_sectors.py`, `real_view_source.py`).
   `tests/test_wizard_gui_ready.py::test_full_window_reaches_ready_and_writes_validated_candidate`
   fails in isolation on `validation: pending` (a candidate profile is written
   with 98 kept points and zero outliers); that path is not touched by this
   change set.
