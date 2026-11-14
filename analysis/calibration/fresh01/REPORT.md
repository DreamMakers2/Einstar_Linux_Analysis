# fresh-01: why the latest real calibration failed

Read-only reconstruction of `~/einstar-calibration-fresh-01` (2026-11-14).
Machine-readable evidence: [`fresh01_analysis.json`](fresh01_analysis.json).
Nothing in the session directory was modified.

## Headline

The session was not one calibration attempt. The directory was **created at
04:42:34 and reused at 04:47:40**, and its append-only artifacts contain **two
(or three) physical attempts interleaved**:

* 162 accepted views, of which **70 were written from the previous attempt's
  frames** (monotonic timestamps five minutes before the new session started),
  and 92 belonged to the attempt the operator actually performed;
* one `decisions.jsonl` with **117 duplicated group ids**, two timestamp domains
  (monotonic uptime and epoch) and two device-group ranges;
* a single coverage verdict that mixed both attempts.

The operator's real session therefore had ~92 accepts, not 162, and a large part
of its first three minutes was spent deciding stale images. That alone explains
why the on-screen indicators did not track what the operator was doing.

## Root causes, in order of impact

### 1. Reused session directory mixed two attempts (data integrity)

| evidence | value |
|---|---|
| directory created / `session.json` created | 05:42:34 local / 04:47:40 UTC |
| accepted views from the previous attempt | 70 of 162 |
| duplicated group ids in `decisions.jsonl` | 117 |
| timestamp domains inside one session | 2 (uptime ~5.2e3 s and epoch ~1.79e9 s) |
| device-group ranges | 4..1813 and none (manifest entry absent) |

The capture tool writes dense frame ids from 0 into `SESSION/frames` and a new
`frames_manifest.json`. Reusing the directory without moving the old ring meant
the reader consumed old images while believing they were live, and the old
manifest supplied the other attempt's timestamps and device-group ids. When the
new manifest replaced it, groups not yet listed fell back to `time.time()`,
producing a **1.79e9 s jump** inside the session. Every velocity, dwell and
arrival metric downstream is invalid after that jump.

Fix: `LiveCaptureProcess.archive_previous_output()` renames a previous frame
ring, `raw.e1r` and `status.json` aside (never deletes); `WizardController.begin()`
quarantines a previous `session.json`, views, decisions and manifests into
`previous-<utc>/`. Live group timestamps use **one monotonic arrival clock**.
Tests: `tests/test_session_hygiene.py`.

### 2. Multi-second rig solves ran inside the decision thread

| views | solve time (real fresh-01 observations, analytic LM) |
|---|---|
| 8 | 0.19 s |
| 16 | 1.46 s |
| 25 | 1.75 s |
| 40 | 2.68 s |
| 60 | 6.47 s |
| 84 | 12.40 s |

`solve_every = max(2, n_accepted // 12)` schedules ~12 of these. Measured
decision-worker cost: **p50 239 ms, p95 1225 ms, max 15369 ms**; **3647 of
4513 groups were superseded** (only 866 decided, 1.81/s against a 9.77 groups/s
stream). While a solve ran, no tracking, guidance, stationarity or persistence
could happen.

Fix: live acquisition runs the solve on a dedicated `wizard-rig-solve` thread.
`CalibrationSession.ingest(..., solve=False)` keeps the decision path to
detection + PnP + acceptance; the worker snapshots observations, solves without
holding the decision lock, then installs the result atomically and refreshes
guidance. `finish()` stops and joins the worker before the final solve.
Tests: `tests/test_async_solve.py`, `analysis/calibration/performance/`.

### 3. The missing roll sector was physically presented and never decided

The fresh attempt's accepted rolls only reached **171.8°** (bins
28 / 64 / 0 / 0), but the last 120 real frames still on disk contain two
genuinely high-roll views that **detection found and no decision ever saw**:

| group | roll (coverage convention) | markers left / right / colour | decisions recorded |
|---|---|---|---|
| 4627 | 317.3° | 15 / 30 / 29 | none |
| 4631 | 303.8° | 26 / 34 / 34 | none |

`decisions.jsonl` stops at group 4509 while the capture reached 4658: the tail
was superseded while the decision thread was busy. The sector was not a roll
math bug in the detector: on real images, rotating a 40-marker view by
0/90/180/270° still detects 40/40 markers with unchanged RMS.

Both tail frames are also **motion-blurred**: their Laplacian focus is 24 and
28, against a p50 of 746 (minimum 177) for the stored accepted views. A sharp
calibration view at that roll was therefore never captured, and even with a
responsive decision path these two frames would (correctly) fail the focus
gate. So the complete cause of the missing sector is: the operator did rotate
the plate, the only high-roll frames are blurred, and 81 % of the frames in
that period were never evaluated at all. The fix is a responsive pipeline that
lets the operator hold still at the highlighted orientation — proven by
`tests/test_roll_sectors.py` (four real acceptance paths) — not a lower focus
threshold.

There *was* a real display/coverage inconsistency: the UI pose summary reported
view 84 as **−46.9°** while coverage counted **+44.3°**, because the display used
a bare image-plane `atan2` and coverage used the projected in-plane angle. It is
fixed by delegating `_pose_summary` to `coverage.roll_deg`, so the number the
operator sees is the number being counted.

### 4. Stationarity was starved, and vetoed frames carried no motion

D7 reported **0 dwell intervals from 2 stationary stream observations**. The
accepted set cannot supply dwell evidence: accepted poses are 1.2 s apart and
move 33.9 mm/s (median) — by design, because they are the *diverse* views. D7
needs the stream, and the stream was decided at 1.8/s with second-scale stalls;
a 0.3 s hold produced zero or one decision.

Two further defects: controller vetoes (the 428 "no markers" and other
pre-check rejections) were evaluated **without** `previous_pose`, so they had no
speed/rotation metrics at all; and pair-wise speed divides PnP jitter by a short
interval.

Fix: every decided pose is tracked in a short history and dwell speed/rotation
are fitted over a 0.45 s window (`_window_motion`), then overlaid on the
quality record for accepts, rejects and vetoes. Veto evaluation now receives
`previous_pose`/`previous_timestamp` and can record a real pose when one camera
still sees the plate.

### 5. Exposure drifted to its limits with no-op changes

93 exposure changes; final **IR 2325 µs**, **RGB 20000 µs / gain 300** (all at
or near their limits). The log contains repeated `{"rgb_gain": 300}` entries
with reason "within target" for a value that is already 300, and repeated
"IR dark (p50 58)" up-steps. 62 decisions were rejected for saturation.
Details and fixes: `analysis/calibration/auto_exposure/AUDIT.md`.

### 6. 81 % of captured groups were never analysed

Only 866 of 4513 groups reached the decision path. That is a direct consequence
of 1.8 decisions/s (cause 2) against a 9.77 groups/s stream. On full-board
frames detection is 30–60 ms per camera; the process-pool path already runs the
three cameras in parallel. **Partial** frames are much worse: cProfile over 12
real ingests shows 414 `detect_grid` calls (34 per ingest for three cameras)
because all six threshold/polarity passes run when the lattice is incomplete,
and a clipped ROI is re-detected on the full frame. Replaying the 24 tail frames
gives a decision p50 of ~655 ms and a maximum of ~1.6 s. Both the solver and the
partial-detection explosion have to go.

## What was *not* the cause

* **Not a roll-detection limitation.** All four in-plane rotations detect all 40
  markers with identical RMS on real images.
* **Not a USB/transfer problem.** 0 sequence gaps, 0 queue drops, 0 lost
  groups; 320 transfer timeouts were recovered inside the capture tool and did
  not drop frames.
* **Not insufficient camera count.** The high-roll frames had 26/34/34 and
  15/30/29 markers; both IR cameras and the colour camera saw the plate.
* **Not a user error.** The operator produced valid high-roll views; the
  software never evaluated them.

## Reproduce

```bash
.venv/bin/python -m pytest tests/test_session_hygiene.py tests/test_async_solve.py -q
.venv/bin/python tools/wizard/rehearsal.py --source replay \
    --frames ~/einstar-calibration-fresh-01/frames --out /tmp/fresh01-replay \
    --async-solve --force
```

## Remaining unknowns

* The reason the operator's early-attempt directory was reused is outside the
  logs (launch command not recorded). The wizard now isolates it automatically.
* PnP noise on a still plate at 10 Hz is not directly measured; the 0.45 s
  window estimate assumes zero-mean jitter and is tested against synthetic
  dwells, not against a new physical hold.
