# Coverage / stationarity fix — D4 image corners and D7 quality/dwell

Status: implemented and validated against the real session
`<calibration-session>` (297 accepted views, 2 004 live
decisions).  Nothing was committed; the session and factory profile were read
only.  No detection or capture was re-run.  The 52 GB raw recording is verified
and is being scanned separately with a memory-bounded streaming decode; the D4
statement below is conditional on that scan (it covers frames that are not in
the 297 accepted views).

Machine-readable results:

* `analysis/calibration/coverage/session03_validation.json`
* `analysis/calibration/coverage/d4_attainability.json`
* `analysis/calibration/coverage/d7_stationarity.json`
* generator: `analysis/calibration/coverage/validate_session03.py`

## 1. Before / after

| quantity | before | after |
| --- | --- | --- |
| real manifest readiness | 5/8 displayed (D4 + D7 unsatisfied; the GUI double-counted D7 and dropped D8, so the honest count is 6/8) | **D4 + D7 remain unsatisfied** once the recorded observation stream is measured: the stream supplies evidence but only four decision-rate dwell episodes qualify, and the raw 10 Hz recording has only two to three distinct dwell poses |
| D7 quality | every view "passed" because flat `quality.saturation_frac` / `quality.focus` keys do not exist | 297/297 accepted views genuinely pass the nested metrics: focus min **171.8**, saturation max **0.00995**, source `nested_metrics` 297, missing metrics 0 |
| D7 stationarity | 5 "stationary" from drift between consecutive **accepted** poses (speed p50 24.3 mm/s) | accepted views: **0** stationary (284 measured, 13 with no motion record). Decision stream: 138 stationary observations but only **4 qualified dwell episodes** (the other 7 are single samples separated by long gaps; the raw trace inside those gaps moves at 5-1800 mm/s). Raw 10 Hz recording: 2,333 stationary pairs but only **2-3 distinct dwell poses >= 0.3 s**. D7 remains unsatisfied (4 < 6); the old 5 was an accidental artifact, the corrected result is a genuine data limitation |
| D4 corners | `top_right=0`, `bottom_right=0` | still 0 in the 297 accepted views; the raw corpus scan is open and may recover right-corner frames that were never accepted.  Corrected guidance targets only the deficient right corners |
| guidance sentence | "Move to 460-560 mm (closer, 44 mm), roll the plate by 159 deg anticlockwise, put the plate centre in the top-left of the image." | "Put the plate centre in the bottom-right corner of the image." |
| D4 target pose | top-left (already 9 visits), distance carrier first | bottom-right: 553.6 mm, lateral (149.5, 116.3) mm, centroid (946.9, 787.9) px, 40/36 visible left/right markers |

The real manifest's compatibility block omitted D8 and counted D7 as two keys;
the corrected dimension list is D1, D2, D3, D5, D6, D7 (with stream), D8
satisfied and **D4 unsatisfied**.  The session is still not READY because no
recorded view has a plate centre in the top-right or bottom-right cell.

## 2. Root causes

### 2.1 D7 quality read the wrong shape

`tools/wizard/coverage.py` read `quality.get("saturation_frac")` and
`quality.get("focus")`.  The live wizard records
`quality["metrics"]["left.focus"]`, `right.focus`, `left.saturation`,
`right.saturation`, `.active`, `.contrast`, plus motion fields.  The flat keys
were absent, so the old D7 loop left `ok=True` for every view.  `quality.passed`
is itself not evidence: every session03 view has `passed=True` precisely
because the per-frame gate passed; D7 must re-measure the recorded numbers.

### 2.2 D7 stationarity measured the wrong set

The old rule computed translation/rotation between consecutive **accepted**
views.  Accepted views are sparse (median 1.29 s apart, max 66 s) and biased
towards motion; duplicate dwell frames are rejected as redundant before
acceptance.  On session03 that produced 5 accidental stationary flags and made
D7 essentially unreachable.  D7 is an acquisition-quality property, not a pose
coverage property: it must be measured from the consecutive observation stream
(raw frames or live decisions), including frames rejected for geometry.

The observed stream also explains the operator's behaviour: **138** decisions
are below 3 mm/s and 1.5 deg/s, and **none** were accepted.  134 of them passed
quality and were rejected only as `redundant view` (102) or `too similar to an
accepted view` (32).  The last 120 ring groups (13075-13194, 10 Hz) were never
evaluated live and are a stationary centred plate with 40 markers in all three
cameras (lead evidence; not re-detected here).  That is at least one more dwell
episode available to a controller that measures the raw stream.

### 2.3 D4 guidance generated already-satisfied corners and led with distance

`_families()` checked only the global `corner_missing` boolean and generated all
four corners, then effort-sorted them.  The nearest corner was top-left (9
visits already), so the operator was repeatedly told to revisit a satisfied
cell while top-right/bottom-right stayed at 0.  `_instruction()` always began
with the distance carrier ("Move to 460-560 mm…") even though D1 was satisfied,
and the carrier roll was 0 instead of the current roll, producing a spurious
159° roll request.

Real D4 region counts: `[9, 30, 0, 38, 198, 5, 2, 15, 0]` (row-major 3x3);
corners `top_left=9, top_right=0, bottom_left=2, bottom_right=0`.

## 3. Code changes

### `tools/wizard/coverage.py`

* `quality_observation(view, spec)` — one row per view:
  * nested metrics; active IR cameras only; `focus_min` and
    `saturation_max` across active left/right; per-camera values and reasons;
  * legacy flat `focus` / `saturation_frac` still accepted when actually
    recorded (`quality_source="legacy_flat"`);
  * missing metrics are a failure, never a pass; `quality.passed` is reported
    as `recorded_passed` but not used as evidence;
  * tex focus/saturation/contrast are reported per view but not gated by D7
    (tex has a different scale: focus ≥ 55, contrast ≥ 5; colour is D6);
  * explicit `quality_source` and `missing_metrics` per view and aggregate.
* `stationarity_observation(view, spec)` — per view, precedence:
  1. explicit `stationary`;
  2. recorded `stationarity_speed_mm_s` / `stationarity_rate_deg_s` /
     `stationarity_window_s`, or nested `speed_mm_s`, `motion_deg`/`dt_s`,
     `motion_mm`, `motion_blur_px`;
  3. `stationarity_source="missing"` → not stationary.
  Accepted-pose deltas are no longer used for D7.
* `stationary_intervals(rows, gap_s)` — groups consecutive stationary
  observations into distinct dwell episodes; 10 Hz frames of one pause count as
  one interval.  `stationarity_interval_gap_s=1.0`.
* `measure(accepted_views, spec, *, stationarity_observations=None)` — D7
  quality from accepted views, D7 stationarity from the stream.  If no stream
  is supplied, stationarity is reported **unestablished** (accepted count still
  shown); it is neither a silent pass nor a default fail.
* `dwell=True` views count for D7 but are excluded from D1-D6 (a duplicate pose
  must not manufacture distance/tilt/region counts).
* D7 now has two checks, `checks["quality"]` and `checks["stationarity"]`, and
  `dimensions["quality"]` / `dimensions["stationarity"]` are copies of their
  own check (own `satisfied`, `measured`, `deficit`, `hint`).  Readiness still
  reports D7 once at dimension level.
* Guidance:
  * `_image_targets()` uses the measured corner counts/region grid and emits
    only genuinely deficient cells;
  * `_families()` generates only families that unlock a missing check; a
    satisfied distance band or roll is an internal carrier, never an unlock;
  * corner lateral offset is `0.27·D` in x and `0.21·D` in y, with
    `z = sqrt(D² - x² - y²)` so the **camera distance norm(t) stays D** (inside
    the 300-560 mm band) rather than exceeding it;
  * ranking is `(unlock_weight, effort)`: a corner (weight 3) outranks a
    distance carrier (1), then nearest family;
  * `_instruction()` states the missing corner/region first and mentions
    distance only when a distance band is genuinely missing;
  * if only D7/D8 remain, guidance returns `mode="hold"` with a dwell/quality
    instruction instead of an endless pose walk.

### `calib/guidance.py`

* `evaluate_frame()` rejection now depends on **speed and exposure blur with an
  honest dt**, not absolute inter-decision displacement:
  * motion blur = speed × exposure; thresholds unchanged (0.30 mm / 0.45 px);
  * `max_motion_reject_mm/deg` is only a pose-discontinuity sanity check for
    genuinely adjacent samples (`dt <= max_adjacent_pair_dt_s = 0.15 s`);
  * a sharp frame with a large displacement over seconds is accepted and only
    flags `motion_warning`;
  * when dt is unknown, displacement is reported but never converted into a
    rejection.
* Records `stationarity_speed_mm_s`, `stationarity_rate_deg_s`,
  `stationarity_window_s`, `stationary`, `motion_blur_px` in every
  `FrameQuality.metrics`.
* `quality_stationarity(quality)` and `acceptance(..., admit_dwell=False)`:
  a stationary, quality-passing duplicate can be admitted as `dwell=True`
  evidence; the default stays off so existing callers are unchanged.  The
  controller must route such views to coverage, not to the solver.

Thresholds were not changed: **3 mm/s, 1.5 deg/s, >= 6 distinct dwell
intervals**, focus 150, saturation ≤ 0.05, D4 corners ≥ 1 each, ≥ 6 of 9 image
regions, D5 ≥ 12 shared markers.

## 4. Real-session validation

### D7 quality (accepted views)

* 297/297 pass, `quality_source_counts={"nested_metrics": 297}`,
  `missing_metrics=[]`.
* IR focus min 171.8 (left; right min 238.6), IR saturation max 0.00995.
* tex focus min 100.7, tex saturation max 0.00472 — reported, not gated.
* The old flat path would also have "passed" all 297 views, but for the wrong
  reason (absent keys).  The corrected pass is supported by numbers.

### D7 stationarity

* Accepted views: 0/297 stationary; 284 have a motion record
  (speed min 4.26, p10 8.56, p50 25.63, p95 86.51 mm/s), 13 have no motion
  record (e.g. view 0 and accepted views without a previous pose).
* Decision stream: 2 004 rows, 975 with speed; 138 stationary observations.
  Under the strict dwell rule (>= 2 consecutive samples and >= 0.3 s) only
  **4 intervals** qualify (durations 4.1 s, 1.2 s, 1.0 s, 49.0 s); the other 7
  are single decision samples whose long-dt "speed" only bounds net
  displacement. The raw 10 Hz trace of the same session moves at 5-1800 mm/s
  inside those gaps, so they are not dwells.
  p50/p95 speed 23.53/121.64 mm/s; p50/p95 rate 3.52/17.54 deg/s.
* `measurement_with_stream`: D7 **unsatisfied** (4 qualified intervals) and
  D4 unsatisfied; readiness "2 dimensions unsatisfied -> D4, D7".  `measurement_without_stream`: D7 stationarity unestablished, so D4
  and D7 are both unsatisfied; the accepted-view count alone is never a pass.
* Hypothetical dwell admission of the 134 quality-passing stationary duplicate
  decisions: 431 accepted views, quality 431/431, 134 stationary accepted
  views.  With the strict dwell rule these single decision observations still
  do not form six qualified episodes, so D7 remains unsatisfied; admitting
  duplicates cannot manufacture the missing stationary holds.  This is an
  alternative to the stream, not a way to satisfy D4 or D7.

Caveat: live decisions are ~0.5 s apart and 1 029/2 004 decisions have no motion
record (no pose).  The 11 intervals are a lower bound on the operator's dwells.
A per-raw-frame stream (10 Hz, 13 195 groups written) would refine the count;
the manifest does not contain per-raw-frame poses/motion, so this validation
does **not** invent them.

### D4 attainability (factory profile, 1280×1024, fx≈1159, 224×168 mm plate)

Target offset: `x=0.27·D`, `y=0.21·D`, `z=sqrt(D²-x²-y²)` (so `norm(t)=D`).

* Per-band target visibility (zero tilt/roll; visible-left centroid in the
  requested corner):

  | D (mm) | corner | left markers | right markers | shared | visible region |
  | --- | --- | --- | --- | --- | --- |
  | 340 | top-right | 35 | 36 | 32 | (0,2) |
  | 340 | bottom-right | 35 | 38 | 34 | (2,2) |
  | 420 | top-right | 40 | 35 | 35 | (0,2) |
  | 420 | bottom-right | 40 | 35 | 35 | (2,2) |
  | 510 | top-right | 40 | 35 | 35 | (0,2) |
  | 510 | bottom-right | 40 | 35 | 35 | (2,2) |

* Tilt/roll robustness: 7 tilt-x × 7 tilt-y × 8 roll × 4 corners = 1 568 cases
  per band centre.  **100 %** land in the requested region with ≥ 28 markers in
  each IR camera and ≥ 12 shared at 340, 420 and 510 mm; right-IR minimum
  29/30/33, median 36/39/40.
* Actual corrected session target: D=553.6 mm, lateral (149.5, 116.3) mm,
  z=520.2 mm; 40 left / 36 right / 36 shared markers; visible-left centroid
  (946.9, 787.9) px → bottom-right.  It is inside the 460-560 mm band.
* Full 40/40 visibility in both IR cameras **is** attainable for the right
  corners at every band with a smaller offset (e.g. 340 mm: (90, 63) mm;
  420 mm: (105, 84) mm; 510 mm: (120, 107) mm), with 52-86 px region margin.
  Left corners do not keep all 40 in the right camera, but they already have
  9 and 2 visits.

Conclusion: the right corners are physically attainable at 300-560 mm; the
recorded misses are a guidance/operator-loop failure, not an FOV or plate-size
limit.  **Do not weaken D4.**  Keep D5 ≥ 12 shared and the live per-camera
floor of 28 markers; if a future policy insists on all 40 markers in both
cameras for corner views, use the smaller offsets above, not a looser D4 rule.

No accepted view has a plate centre in top-right or bottom-right, so the 297
accepted views alone cannot be made READY retroactively.  This conclusion is
conditional on the raw-corpus scan: the 52 GB recording is verified and is
being decoded with a memory-bounded stream, and it may contain right-corner
frames that were never accepted live.  Corrected guidance now asks for exactly
those cells.

## 5. Controller integration contract

`WizardController.measure_coverage()` now calls:

```python
measurement = coverage.measure(
    coverage_views,                       # accepted geometry views (+ dwell=True)
    spec,
    stationarity_observations=stream,     # every ingested observation
)
```

* **`stream`** is ordered by timestamp and contains every ingested group/raw
  frame, accepted or rejected, with:
  * `timestamp` (or `timestamp_s`), `group` and/or `view_id`;
  * `quality` = `FrameQuality.as_dict()` including
    `metrics.speed_mm_s`, `metrics.motion_deg`, `metrics.dt_s`,
    `metrics.motion_blur_px`, and optionally `metrics.stationary`;
  * alternatively top-level `stationary`, or `metrics.stationary`, plus `stationarity_speed_mm_s`,
    `stationarity_rate_deg_s`, `stationarity_window_s`, `motion_blur_px`.
  * No dt → no stationarity observation → missing, not pass.
  * Do not filter the stream to accepted views, and do not fill missing fields.
* **`coverage_views`** is the accepted geometry list.  If the controller admits
  stationary duplicates via `acceptance(..., admit_dwell=True)`, mark them
  `dwell=True` and keep them out of `session.accepted` / the solver.  `measure`
  then counts them for D7 but not D1-D6.
* **GUI compatibility**: the controller currently maps D7 to the `quality` key
  and rebuilds `stationarity` from `checks["stationarity"]`.  With the split,
  prefer `measurement["dimensions"]` or map `quality` to
  `checks["quality"]` and `stationarity` to `checks["stationarity"]`; do not
  copy D7's combined hint into both sub-indicators.
* `readiness(measurement)` still reports D7 once at dimension level;
  `unsatisfied` carries the two sub-check labels and each has its own hint.
* If a caller omits the stream (older controller or a partial replay), D7
  shows **"Stationarity unestablished"** — this is intentional and visible,
  not a silent fail.  The integrated controller passes
  `self._stationarity_observations` and records rejected/vetoed observations
  through `_record_stationarity_observation()`; the contract test
  `tests/test_wizard_lifecycle_coverage_wiring.py` passes.

## 6. Tests

```
.venv/bin/python -m pytest tests/test_wizard_coverage.py tests/test_guidance.py -q
# 46 passed
```

New/updated coverage:

* nested quality aggregation (per camera, active cameras, tex reported not
  gated, missing metrics never pass, legacy flat path);
* per-view stationarity from motion fields; missing motion is not a pass;
  accepted-only is unestablished; a separate stream satisfies D7 while accepted
  views are moving; 10 frames of one pause are one interval; six separated
  pauses are six intervals;
* `dwell=True` frames count for D7 and not D1-D6;
* D4 guidance with the real `[9,30,0,38,198,5,2,15,0]` pattern targets only
  top-right/bottom-right, never distance, never satisfied left corners;
* factory-intrinsics projection proves the col=2 lateral offset lands
  image-right and that the corner target keeps ≥ 28 markers in each IR camera
  and ≥ 12 shared;
* D7 sub-indicators have distinct hints and readiness reports D7 once;
* D7-only failure returns a dwell hold, not a new pose;
* guidance motion gate differentiates 30 mm/0.1 s (blur reject) from
  30 mm/5 s (accept) and 200 mm/5 s (accept);
* dwell admission only when opted in, quality-passing and stationary.

Dependent wizard tests: the readiness end-to-end tests
(`tests/test_wizard_scale.py::test_sixty_view_workflow_reaches_ready_and_validates`,
both `tests/test_wizard_gui_ready.py` cases) and
`tests/test_wizard_lifecycle_coverage_wiring.py` pass against the integrated
controller, as do `tests/test_wizard_failures.py` colour checks,
`tests/test_wizard_session_io.py` and `tests/test_wizard_controller.py`.
One unrelated failure remains in this working tree:
`tests/test_wizard_failures.py::test_observability_refuses_overparameterised_distortion`
expects ≥ 10 unobservable parameters but the concurrently changed
`calib/solvers.py`/observability gate reports 4-6.  It does not import or
exercise coverage/guidance and is outside this change's ownership.

## 7. Limitations / unresolved risks

* D4 cannot be satisfied by the existing 297 frames; a short new capture of the
  top-right and bottom-right cells is required.  The corrected first target is
  bottom-right at 553.6 mm.
* D7 stream evidence is decision-rate, not raw-frame-rate; 51 % of decisions
  have no motion record because no board pose was available.  A raw-frame
  implementation would produce a larger, cleaner interval count.  The verified
  raw recording and streaming decode make that possible; this validation did
  not decode it.
* The last 120 ring groups are reported by the lead as a stationary centred
  plate with 40 markers in all three cameras.  They are not decoded here; if
  they carry usable timestamps they are another dwell episode.  The raw-corpus
  scan may also recover right-corner frames, which is why the D4 verdict is
  stated as "none in the 297 accepted views" rather than "physically absent".
* tex is deliberately not part of D7 quality; if the colour camera's focus is
  to become a gate it needs its own thresholds and belongs with D6.
* The D4 target offset (0.27/0.21) is an evidence-based heuristic validated on
  the factory rig at the three band centres; a future controller with live
  intrinsics could invert the projection exactly instead.
* No READY claim is made for the recorded session: D4 is unsatisfied in the
  297 accepted views.  The raw-corpus scan is the only remaining source that
  could change that without a new capture.
