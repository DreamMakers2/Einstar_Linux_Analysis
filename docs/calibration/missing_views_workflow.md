# Capture Missing Views — complete an existing calibration in minutes

Status: implemented and verified offline against the real session-03 evidence.
Nothing was activated, the factory profile was not modified and the 52 GB
recording was read only.

Related evidence: `docs/calibration/session03_recovery.md`,
`docs/calibration/coverage_stationarity_fix.md`,
`analysis/calibration/coverage/session03_validation.json`,
`analysis/calibration/recovery/session_recovered/`.

## 1. The problem this replaces

Session-03 spent 22.4 minutes and produced a 52 GB recording with 297 accepted
views.  The corrected coverage gate then refused readiness because **D7
(stationarity)** was unsatisfied: the spec requires **6 distinct qualified dwell
intervals** and the raw 10 Hz stream of that recording has only **2** genuine
distinct holds (4.643 s and 225.402 s).  D4 (image corners) is satisfied by four
right-corner views decoded from the same untouched recording (7/8 overall).
Repeating a 22-minute multi-pose walk to collect four one-second holds is not an
acceptable operator experience.

The completion workflow therefore:

1. loads an existing session **read-only** and measures what is genuinely
   missing (offline, before the operator is asked for anything);
2. asks for the **minimum** ordered set of board poses (D7 deficit = 3–4 short
   holds in the real case; hard cap 8 poses);
3. confirms each accepted stationary hold in real time (progress ring, measured
   duration/speed);
4. appends the new evidence to a **new** extension session and combines it with
   the old session;
5. never touches, rewrites or re-records the base session and **never
   activates** a profile.

## 2. What was built

| part | file | role |
|---|---|---|
| pure core | `calib/missing_views.py` | `load_base_evidence`, `plan`, `HoldTracker`, `ExtensionStore`, `combine`, `assess`, `readiness_verdict`, `activation_gate` |
| offline CLI | `tools/wizard/missing_views.py` | human summary + `--json`, `--recovered` overlay, `--extension` re-assessment, exit 1 while not ready |
| controller | `tools/wizard/controller.py` | additive `begin_extension()`, dwell-only `ingest` branch, `extension_status()`, snapshot `extension` key, plan guidance |
| tests | `tests/test_missing_views.py` | tracker, store, plan, real data, synthetic 4-hold extension, controller extension, CLI |

### 2.1 `load_base_evidence(session_dir)` — read-only

* Prefers `session.json["resume"]` (`views`, `accepted`,
  `stationarity_observations`) and rebuilds the per-view records exactly like
  `WizardController.resume`: pose/quality from `accepted`, `image_points` and
  the shared-marker count from the `(label, pixel)` rows in `views`.
* If `stationarity_observations` is absent/empty (session-03 predates it), the
  recorded `decisions.jsonl` rows are used **exactly the way
  `analysis/calibration/coverage/validate_session03.py` does**:
  `coverage.measure(views, stationarity_observations=decisions)`.
* Records SHA-256 for `session.json`, `manifest.json`, `decisions.jsonl`,
  `status.json`, re-checks them after the read, and raises
  `BaseSessionChanged` if anything moved.  `raw.e1r` is recorded by size/mtime
  but deliberately not hashed (76 s at 681 MB/s on the real file; the recorder
  trailer is the authoritative full-file check).

### 2.2 `plan(assessment, base_evidence, spec)` — minimal ordered poses

* D7 deficit is arithmetic: `min_stationary_views − n_intervals`.  For the
  recovered session that is `6 − 2 = 4`; a session with no stream needs 6.
* Each D7 target is a distinct combination of distance band, tilt quadrant,
  roll sector and image cell, chosen so it is **not already covered** by the
  base session's accepted views.  Candidate poses use
  `coverage.family_pose`; the operator sentence uses the plate-relative delta
  (same axes/words as the existing guidance) and
  `calib.guidance.reachable` for the one-movement reachability flag.
* Other dimensions reuse the coverage bins: D1 one pose per missing band,
  D2 one per missing tilt quadrant, D3 one per missing roll sector, D4 the
  missing corner/region cells at the documented `0.27·D / 0.21·D` offsets
  (`coverage_stationarity_fix.md` §4).  D5/D6/D8 become explicit visibility /
  artefact entries with their recorded hint.
* Every entry carries `reason`, `instruction` (operator language), `dimension`,
  `target` / `machine_target` (distance range, tilt/roll sector, image cell,
  hold seconds) and the full target `pose`.  The plan is capped at
  `MAX_PLAN_POSES = 8`; the remainder is reported in `deferred`, never silently
  dropped.

### 2.3 `HoldTracker` — pure state machine

`update(timestamp_s, R, t)` over the PnP board pose of every ingested group:

* `ALIGNING → HOLDING`: two consecutive still samples
  (`speed ≤ 3 mm/s`, `rate ≤ 1.5 deg/s`).
* `HOLDING`: `progress` in 0..1 for the ring; micro-jitter inside
  `jitter_mm = 2.5 mm` / `jitter_deg = 1.0°` keeps accumulating.  A sample
  above the D7 thresholds restarts the clean window (so an accepted hold is a
  contiguous run of genuinely stationary measurements).
* A deliberate move (`> 8 mm` or `> 3°` per sample) returns to `ALIGNING`; a
  hold that ends before `hold_s = 1.0 s` is rejected with
  `hold_too_short`; a pose that drifts beyond the jitter tolerance is **never**
  accepted (`hold_unstable`).
* `ACCEPTED` reports the averaged pose (chordal mean rotation, mean
  translation), measured duration, mean/max speed and rotation rate, sample
  timestamps and the pose spread.

### 2.4 `ExtensionStore` — append-only and crash-safe

New directory with `extension.json` (schema, base session path + base digests,
factory profile digest, pitch, spec, creation time) and `holds.jsonl` (one
accepted hold per line: target, pose, duration, motion metrics, samples,
quality, frame digest).  Existing bytes are never rewritten; each append is
`flush + fsync`.  A crash-truncated last line is ignored on load (reported as a
warning) and the next append first terminates the partial line.  Any change to
the base digests makes `verify_base()` / `append_hold()` refuse with
`BaseSessionChanged`.

### 2.5 `combine` / `assess` — and the activation gate

`combine(base_views, extension_holds)` returns `(views, stationarity_rows)`
(a `Combined` tuple): base accepted views plus one `dwell: True` view per hold
and measured stream rows (`stationary`, speed, rate, window).  Dwell views are
excluded from D1–D6 by the coverage module and counted for D7 only, so
`tools.wizard.coverage.measure(views, stationarity_observations=rows)` works
unchanged.  `assess()` returns the measurement, the readiness verdict and an
explicit activation gate that is **always** `{"activated": False, ...}`;
activation remains `tools.wizard.lifecycle.activate()` after a validated
candidate.

## 3. Offline evidence — measured on the real recording

Command (nothing is written to either session directory):

```bash
.venv/bin/python tools/wizard/missing_views.py \
    --session-dir ~/einstar-calibration-03 \
    --recovered analysis/calibration/recovery/session_recovered
```

| quantity | original session (untouched) | recovered session evidence |
|---|---|---|
| accepted views | **297** | **301** (297 + four raw corner views: groups 89, 5682 TR; 4019, 6241 BR) |
| stationarity stream | **2,004** rows reconstructed from `decisions.jsonl` | **11,620** rows from the stored raw 10 Hz stream |
| qualified dwell intervals | **4** (4.095, 1.188, 0.978, 48.998 s) | **2** (4.643, 225.402 s) |
| D7 quality | **297/297** pass, `nested_metrics`, focus min **171.8**, saturation max **0.00995** | **301/301** pass, same numbers |
| D4 image regions / corners | 7/9, `TL 9 · TR 0 · BL 2 · BR 0` → **unsatisfied** | 9/9, `TL 9 · TR 2 · BL 2 · BR 2` → **satisfied** |
| readiness | **6/8** (D4, D7 unsatisfied) | **7/8** (D7 only) |
| `session.json` SHA-256 | `b3dd1f649aa5…` | `a1a039d338d7…` |
| `manifest.json` SHA-256 | `c41852096009…` | (none in the evidence dir) |
| `decisions.jsonl` SHA-256 | `2e87e3498d1c…` | (none in the evidence dir) |
| raw recording | `51,977,409,627 B`, left untouched, not hashed | same file, untouched |

Why the recovered evidence is the completion base: the decision-rate stream
(one row per live decision, ~0.5 s apart and 51 % without a pose) is a *lower*
bound and happens to overcount (4) for exactly the reason
`coverage_stationarity_fix.md` §2.2 documents: long `dt` gaps bound only net
displacement.  The complete raw 10 Hz trace of the same recording moves at
5–1,800 mm/s inside those gaps and qualifies only 2 distinct dwell poses.  The
plan therefore asks for **4** holds, which is also the conservative choice: a
genuine hold at a genuinely new pose counts under either stream.

## 4. Actual CLI output

### 4.1 Original session, no overlay (exit status 1)

```
$ .venv/bin/python tools/wizard/missing_views.py --session-dir ~/einstar-calibration-03

Base evidence
  session             : <calibration-session>
  state               : cancelled
  accepted views      : 297 (297 coverage views)
  stationarity stream : 2004 row(s) from decisions_jsonl
  factory profile     : 8259f97cc513eea6…
  digests             : session.json b3dd1f649aa5…, manifest.json c41852096009…, decisions.jsonl 2e87e3498d1c…, status.json 12caa220e835…
  note                : stationarity stream: 2004 observation(s) reconstructed from decisions.jsonl (the session predates the stored stream)
  note                : raw recording present (51977409627 B) and left untouched; its digest is not recomputed here
  coverage            : 6/8 satisfied — NOT READY
    D4 image-plane regions and corners: unsatisfied
      - image_corners: Aim the plate at the image corners: top_right (needs 1 more), bottom_right (needs 1 more).
    D7 stationarity / quality: unsatisfied
      - stationarity: Stationarity: only 4 distinct dwell interval(s) from 138 stationary stream observation(s) (need 6); median speed 23.5 mm/s, median rotation 3.52 deg/s.  Hold each pose still for ~0.3 s and keep the dwell frames.

warning: this is the decision-rate stream (one row per live decision).
  The complete raw 10 Hz stream of the same recording qualifies fewer intervals;
  use --recovered <recovered-session> (or point --session-dir at it) for the
  authoritative completion base (docs/calibration/missing_views_workflow.md).

Plan: 4 pose(s) (cap 8, ~1.6 min of scanning)
  1. D4 — put the plate at ~510 mm, with the plate centre in the top-right of the image
     move    : Move the plate right 301 mm, up 134 mm, closer by 44 mm, tilt 25 deg left, tilt 24 deg down, rotate the plate 159 deg anticlockwise, put the plate centre in the top-right of the image and capture one sharp still
     target  : 510 mm [460.0, 560.0], tilt +x+y (0/0 deg), roll sector 0 (0 deg), image cell [0, 2] (top-right)
     reason  : D4 genuinely lacks a view with the plate centre in the top-right cell.
  2. D4 — put the plate at ~510 mm, with the plate centre in the bottom-right of the image
     move    : Down 214 mm, put the plate centre in the bottom-right of the image and capture one sharp still
     target  : 510 mm [460.0, 560.0], tilt +x+y (0/0 deg), roll sector 0 (0 deg), image cell [2, 2] (bottom-right)
     reason  : D4 genuinely lacks a view with the plate centre in the bottom-right cell.
  3. D7 — hold the plate at ~510 mm, tilt 20 deg right and 20 deg up, rotate it 180 deg clockwise, with the plate centre in the bottom-left of the image
     move    : Move the plate right 51 mm, down 60 mm, closer by 44 mm, tilt 5 deg left, tilt 4 deg down, rotate the plate 21 deg clockwise, then hold it still for 1.0 s.  This is hold 1 of 2 needed to reach 6 distinct dwell intervals.
     target  : 510 mm [460.0, 560.0], tilt +x+y (20/20 deg), roll sector 2 (180 deg), image cell [2, 0] (bottom-left), hold 1.0 s
     reason  : D7 stationarity needs 2 more distinct dwell interval(s): 4 recorded, 6 required.  A dwell counts only when it is a genuine still hold at a pose not already covered by the session.
  4. D7 — hold the plate at ~420 mm, tilt 20 deg right and 20 deg down, rotate it 90 deg clockwise, with the plate centre in the bottom-right of the image
     move    : Move the plate right 205 mm, up 15 mm, closer by 90 mm, tilt 40 deg down, rotate the plate 90 deg anticlockwise, then hold it still for 1.0 s.  This is hold 2 of 2 needed to reach 6 distinct dwell intervals.
     target  : 420 mm [380.0, 460.0], tilt +x-y (20/-20 deg), roll sector 3 (270 deg), image cell [2, 2] (bottom-right), hold 1.0 s
     reason  : D7 stationarity needs 2 more distinct dwell interval(s): 4 recorded, 6 required.  A dwell counts only when it is a genuine still hold at a pose not already covered by the session.

Activation: never by this command.  Readiness is a measurement; activation stays an explicit lifecycle call after a validated candidate.
```

### 4.2 With the recovered corner evidence (exit status 1; this is the
completion base)

```bash
$ .venv/bin/python tools/wizard/missing_views.py \
    --session-dir ~/einstar-calibration-03 \
    --recovered analysis/calibration/recovery/session_recovered \
    --json /tmp/missing_views_session03.json
```

```
Original session (untouched)
  session             : <calibration-session>
  state               : cancelled
  accepted views      : 297 (297 coverage views)
  stationarity stream : 2004 row(s) from decisions_jsonl
  factory profile     : 8259f97cc513eea6…
  digests             : session.json b3dd1f649aa5…, manifest.json c41852096009…, decisions.jsonl 2e87e3498d1c…, status.json 12caa220e835…
  note                : stationarity stream: 2004 observation(s) reconstructed from decisions.jsonl (the session predates the stored stream)
  note                : raw recording present (51977409627 B) and left untouched; its digest is not recomputed here
  coverage            : 6/8 satisfied — NOT READY
    D4 image-plane regions and corners: unsatisfied
      - image_corners: Aim the plate at the image corners: top_right (needs 1 more), bottom_right (needs 1 more).
    D7 stationarity / quality: unsatisfied
      - stationarity: Stationarity: only 4 distinct dwell interval(s) from 138 stationary stream observation(s) (need 6); median speed 23.5 mm/s, median rotation 3.52 deg/s.  Hold each pose still for ~0.3 s and keep the dwell frames.

Recovered overlay: accepted views from the same recording are included below; the original directory is not modified.

Base evidence
  session             : <repo>/analysis/calibration/recovery/session_recovered
  state               : recovered
  accepted views      : 301 (301 coverage views)
  stationarity stream : 11620 row(s) from stored_stream
  factory profile     : 8259f97cc513eea6…
  digests             : session.json a1a039d338d7…
  note                : stationarity stream: 11620 stored observation(s) from session.json resume
  coverage            : 7/8 satisfied — NOT READY
    D7 stationarity / quality: unsatisfied
      - stationarity: Stationarity: only 2 distinct dwell interval(s) from 2333 stationary stream observation(s) (need 6); median speed 44.9 mm/s, median rotation 5.89 deg/s.  Hold each pose still for ~0.3 s and keep the dwell frames.

Plan: 4 pose(s) (cap 8, ~1.6 min of scanning)
  1. D7 — hold the plate at ~340 mm, tilt 20 deg left and 20 deg down, rotate it 90 deg anticlockwise, with the plate centre in the bottom of the image
     move    : Move the plate left 105 mm, up 48 mm, closer by 162 mm, tilt 8 deg left, tilt 14 deg down, rotate the plate 96 deg anticlockwise, then hold it still for 1.0 s.  This is hold 1 of 4 needed to reach 6 distinct dwell intervals.
     target  : 340 mm [300.0, 380.0], tilt -x-y (-20/-20 deg), roll sector 1 (90 deg), image cell [2, 1] (bottom), hold 1.0 s
     reason  : D7 stationarity needs 4 more distinct dwell interval(s): 2 recorded, 6 required.  A dwell counts only when it is a genuine still hold at a pose not already covered by the session.
  2. D7 — hold the plate at ~420 mm, tilt 20 deg left and 20 deg up, rotate it 180 deg clockwise, with the plate centre in the right of the image
     move    : Move the plate right 92 mm, up 58 mm, further by 80 mm, tilt 40 deg up, rotate the plate 90 deg anticlockwise, then hold it still for 1.0 s.  This is hold 2 of 4 needed to reach 6 distinct dwell intervals.
     target  : 420 mm [380.0, 460.0], tilt -x+y (-20/20 deg), roll sector 2 (180 deg), image cell [1, 2] (right), hold 1.0 s
     reason  : D7 stationarity needs 4 more distinct dwell interval(s): 2 recorded, 6 required.  A dwell counts only when it is a genuine still hold at a pose not already covered by the session.
  3. D7 — hold the plate at ~510 mm, tilt 20 deg right and 20 deg up, rotate it 90 deg clockwise, with the plate centre in the top-right of the image
     move    : Move the plate right 20 mm, up 87 mm, further by 90 mm, tilt 40 deg right, rotate the plate 90 deg anticlockwise, then hold it still for 1.0 s.  This is hold 3 of 4 needed to reach 6 distinct dwell intervals.
     target  : 510 mm [460.0, 560.0], tilt +x+y (20/20 deg), roll sector 3 (270 deg), image cell [0, 2] (top-right), hold 1.0 s
     reason  : D7 stationarity needs 4 more distinct dwell interval(s): 2 recorded, 6 required.  A dwell counts only when it is a genuine still hold at a pose not already covered by the session.
  4. D7 — hold the plate at ~420 mm, tilt 20 deg right and 20 deg down, with the plate centre in the top-left of the image
     move    : Move the plate left 205 mm, down 15 mm, closer by 90 mm, tilt 40 deg down, rotate the plate 90 deg anticlockwise, then hold it still for 1.0 s.  This is hold 4 of 4 needed to reach 6 distinct dwell intervals.
     target  : 420 mm [380.0, 460.0], tilt +x-y (20/-20 deg), roll sector 0 (0 deg), image cell [0, 0] (top-left), hold 1.0 s
     reason  : D7 stationarity needs 4 more distinct dwell interval(s): 2 recorded, 6 required.  A dwell counts only when it is a genuine still hold at a pose not already covered by the session.

Activation: never by this command.  Readiness is a measurement; activation stays an explicit lifecycle call after a validated candidate.

wrote /tmp/missing_views_session03.json
```

The four targets are one per tilt quadrant and use four different roll
sectors and four different image cells; none of the 432 possible bin
combinations is already covered by the base evidence in the same way.  The
`~1.6 min` figure is `n_poses × 0.4 min`, an estimate, not a measurement.

## 5. Operator workflow

1. **Offline check** (before touching the scanner), as above.  Exit status is
   `1` while not ready and `0` when ready; `--json OUT` writes the machine
   readable assessment + plan outside the evidence directories.
2. **Start an extension session** (GUI action; the controller API is
   `begin_extension(base_session_dir)`):

   ```python
   ctl = WizardController(factory_profile=factory,
                          session_dir="~/einstar-extension-01", mode="high")
   ctl.begin_extension(
       "analysis/calibration/recovery/session_recovered")
   for group in source:                 # live: the normal capture loop
       ctl.ingest(group)
   ctl.extension_status()               # plan, target, ring progress, holds
   ```

   The new directory gets its own `session.json`/`manifest.json` and
   `extension/extension.json` + `extension/holds.jsonl`.  The base session is
   opened read-only; its digests are recorded and every append re-checks them.
3. **Hold each requested pose for ~1 s.**  A frame must pass the normal
   focus/saturation quality gate; colour participation is not required for a
   dwell frame (D6 is already satisfied over the base session and dwell views
   are excluded from D6), so a corner pose where the colour camera loses the
   plate does not throw away valid D7 evidence.  The GUI ring shows
   `ALIGNING → HOLDING (progress) → ACCEPTED`.  Accepted holds are appended
   immediately; a rejected attempt is reported with one of the stable reasons
   `hold_pending` (still accumulating), `hold_unstable` (the pose drifted out of
   tolerance) or `not_needed_dwell_only` (this frame/pose adds nothing: a
   duplicate pose or a movement frame).
4. **Finish** exactly as before: the plan's D7 evidence is combined with the
   base session, `finish()` solves the base views once and writes a candidate
   with `validation: pending` and `would_activate` governed by the existing
   verdict.  An unsatisfied plan cannot produce an activatable candidate.
5. **Activation stays separate and explicit**
   (`tools.wizard.lifecycle.activate(...)`); no code in this workflow writes,
   installs or activates a profile.

## 6. What was verified

### 6.1 Unit and integration tests

`tests/test_missing_views.py` (22 tests) covers:

* `HoldTracker`: a deliberate move resets to `ALIGNING`; micro-jitter is
  tolerated; a 0.5 s hold is rejected as `hold_too_short` (with its measured
  duration in the detail); an accepted 1.0 s hold reports duration 1.0 s and the
  held pose; a pose drifting 2.8 mm/s (below the D7 speed limit, each step far
  below the move limit) is never accepted and is reported as `hold_unstable`; a
  fast move into the pose is not counted as hold time (its 500 mm/s entry
  sample is excluded, so the accepted hold's measured speed stays below
  3 mm/s); a second hold is only possible after a real move.
* `ExtensionStore`: append/load/ids; manifest schema and base digests;
  byte-prefix append-only property; changed-base refusal (`BaseSessionChanged`
  on verify, append and reopen); crash-truncated last line tolerated and the
  next append recovers.
* `load_base_evidence`: read-only (mtimes and directory listing unchanged),
  decisions reconstruction, digest verification.
* `plan`: cap of 8 respected with the remainder in `deferred`; D7 deficit
  arithmetic (`2 recorded, 6 required → 4 holds`); targets distinct and not
  already covered by the base views.
* real data: original session → 6/8 with D4+D7 unsatisfied and 297 accepted;
  recovered evidence → 7/8, D4 satisfied (`TL 9 · TR 2 · BL 2 · BR 2`), D7 the
  only unsatisfied dimension, 2 qualified intervals, deficit 4, plan 4.
* synthetic controlled extension: a 57-view base scoring 7/8 plus **four
  synthetic 2 s holds at distinct poses** goes to **8/8 READY**, 6 qualified
  intervals, 4 dwell views, while `activation["activated"]` stays **False**;
  **two** holds leave it **7/8 NOT ready** (4 intervals).
* controller extension: `begin_extension` restores the base state, one plan
  pose held for ~1.2 s through the real detection/PnP path is accepted and
  stored (duration 1.0 s, 3 remaining), a hold at an already-covered pose is
  refused as `not_needed_dwell_only` without adding a dwell view, the reason
  counters classify as `motion`/`redundant`, and the base `session.json` is
  byte-identical with digests still verifying.
* CLI: `--extension` re-assembles a 4-hold extension to 8/8 (exit 0) and writes
  JSON outside the evidence directories; writing inside the base session is
  refused (exit 2).

### 6.2 Synthetic numbers (measured, not estimated)

| evidence | views | qualified intervals | D7 | readiness |
|---|---|---|---|---|
| synthetic base | 57 | 2 | unsatisfied | 7/8 |
| base + 2 holds (2 s each) | 59 | 4 | unsatisfied | 7/8 |
| base + 4 holds (2 s each) | 61 | 6 | satisfied | **8/8 READY** |

The activation flag is false in all three assessments and in the extension
manifest.

### 6.3 Full Python suite

Before this change (baseline at the start of the work): **492 passed, 8 skipped**
in 481 s.  After all changes, including the other agents' concurrent test
additions: **572 passed, 8 skipped** in 456 s, exit code 0:

```
$ .venv/bin/python -m pytest tests -q
572 passed, 8 skipped, 93 warnings in 456.22s (0:07:36)
```

`tests/test_missing_views.py` contributes **22 of those passes** and is also
clean on its own (`22 passed in 4.6 s`).  The suite grew by 80 tests in total
because this branch also adds other engineers' new tests
(`test_white_balance.py`, `test_production_storage.py`, ...).

## 7. The remaining physical action

**Four ~1 s stationary holds at the four listed board poses** (340 mm and
420 mm and 510 mm, one per tilt quadrant, four roll sectors, four image cells),
roughly **1–2 minutes** including moving between them.  No re-recording of the
52 GB session, no re-detection of its 297 views and no new corner capture: the
right corners are already in the recovered evidence from the same recording.

After the holds, `finish()` produces the candidate from the base views and the
new dwell evidence; activation is a separate explicit lifecycle call.  Absolute
scale is still unvalidated (no length artefact was imaged; D8 is optional in
the spec) — the holds do not change that.

## 8. Limitations

* **GUI wiring is not part of this change.**  The controller exposes
  `begin_extension()`, `extension_status()` and `snapshot()["extension"]`; the
  GUI files are owned by another engineer and the "Complete Calibration" button
  still has to call them.
* **Extension-session resume** restores the stationarity stream (so D7 stays
  satisfied) but not the dwell views, because they are deliberately kept out of
  `session.accepted`.  The durable record is `extension/holds.jsonl`; reopening
  it with the CLI re-assesses the combined evidence.  A resumed extension
  session does not re-run the solves.
* **Decision-rate caveat**: the original session's `decisions.jsonl` stream is
  an honest reconstruction but overcounts dwell intervals relative to the raw
  10 Hz trace.  The workflow therefore uses the recovered evidence as the
  completion base and asks for 4 holds; the CLI prints a warning when only the
  decision-rate stream is available.
* A hold's pose comes from the left-camera PnP; a group with no usable board
  pose is vetoed (reason `extension: no board pose from the left camera`) and
  never fed to the tracker, so it cannot fake progress.
* `HoldTracker`'s thresholds are configurable (`HoldConfig`); the defaults are
  1.0 s hold, 3 mm/s and 1.5 deg/s (the D7 thresholds), 2.5 mm / 1.0 deg jitter,
  8 mm / 3 deg movement and a 15 s attempt timeout.
* The base `raw.e1r` is not hashed by this workflow; a full identity check
  remains `einstar-stream-frames --verify-only`.
