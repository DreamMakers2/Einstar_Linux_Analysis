# Milestone 2026-11-14 — from a failed physical session to a working rehearsal

This document reports what was **measured**, what was **changed**, and what is
still **not proven**. Companion evidence:

* [`analysis/calibration/fresh01/REPORT.md`](../../analysis/calibration/fresh01/REPORT.md) — the failed session, reconstructed read-only.
* [`analysis/calibration/performance/FRESH01_STALLS.md`](../../analysis/calibration/performance/FRESH01_STALLS.md) — stage attribution and solve curve.
* [`analysis/calibration/partial_board/REPORT.md`](../../analysis/calibration/partial_board/REPORT.md) — partial-grid identity and transform sweep.
* [`analysis/calibration/auto_exposure/AUDIT.md`](../../analysis/calibration/auto_exposure/AUDIT.md) — live exposure audit.

## 1. The failed session was not one session

`~/einstar-calibration-fresh-01` was created at 04:42:34 and its session was
started again at 04:47:40 **in the same directory**. The retained evidence holds
two (arguably three) attempts at once: 70 of 162 accepted views came from the
previous attempt, `decisions.jsonl` has 117 duplicated group ids, a session
carries two timestamp domains (a 1.79e9 s jump) and two device-group ranges, and
`exposure.jsonl` is append-only with 33/93 entries from an earlier capture.

The real attempt accepted 92 views in ~5 minutes with roll 44.2–171.8°, while
**81 % of its 4,513 offered groups were never decided** (866 decisions at
1.81/s; p50 239 ms, p95 1,225 ms, max 15,369 ms). The missing roll sector was
physically attempted: the last 120 frames contain 303.8° and 317.3° views that
detection resolves, but they were never decided. Both are also motion-blurred
(Laplacian focus 24/28 against p50 746 for accepted views), so a sharp view at
that sector was never captured either.

Three independent defects compounded: reused-session contamination, a
multi-second solver on the decision thread, and a detector that paid six
threshold/polarity passes for every partial board.

## 2. What changed

### 2.1 Session and frame isolation (`tools/wizard/capture.py`, `controller.py`, `sources.py`)

* `LiveCaptureProcess.archive_previous_output()` moves a previous frame ring,
  `raw.e1r`, `status.json`, `exposure-control.txt` and `control.txt` into
  timestamped `*.previous-<utc>` siblings before starting a capture. Nothing is
  deleted; a previous 52 GB recording can never be truncated by reuse.
* `WizardController.begin()` quarantines a previous `session.json`, views,
  decisions and manifests into `previous-<utc>/`; a fresh run cannot append to
  an old coverage verdict.
* Live group timestamps use **one monotonic arrival clock**. The manifest/wall
  clock mixture is gone.
* Tests: `tests/test_session_hygiene.py`.

### 2.2 Solver off the decision thread (`controller.py`, `calib/session.py`, `gui/app.py`, `rehearsal.py`)

* Periodic rig solves run on a `wizard-rig-solve` thread. `ingest` never solves
  inline; a solve result is installed atomically and never after stop.
* The solve schedule is geometric (≥1.5× more views since the last solve), not
  `n % solve_every == 0` with a recomputed interval: measured **10 solves** over
  162 views instead of **33**.
* OpenBLAS/OMP/MKL/NUMEXPR/VECLIB are pinned to **one thread** in the launcher:
  16-view solve **13.36 s → 0.58 s** wall (252 s → 0.67 s CPU), identical
  residual to 12 digits.
* `finish()` forces a full-view solve before the parameter-release decision and
  refits when distortion is held, so validation no longer stays
  `pending: tex.k2/k3 unobservable`.

### 2.3 Partial-board recognition and identity (`calib/detect.py`, `calib/detect_partial.py`, `tools/wizard/detection.py`)

* The labelling is verified by the four large markers and `det(H) > 0`; a
  mirrored or ambiguous assignment is never returned. Out-of-frame witnesses are
  excused with `image_shape`; occluded ones are not.
* The partial detector was rewritten around the 2:1 large-marker triple, chiral
  anchors, an affine basis and independent non-anchor witness evidence.
* Measured on real pixels: 240 tail cameras — mirrored labellings **41 → 0**,
  detected **239 → 240**, 95 → 34 ms/case. 912-case transform sweep: wizard
  identity rate **89.3 % → 95.1 %**, wrong labels **251 → 103** (all remaining
  unverified), mirrored **15 → 0**, 0/90/180/270 rotations still **640/640**
  correct. Per-camera partial detection p50 **218 → 30 ms**; per-frame p50
  **655 → 117 ms**.
* PnP subset study on 109 real 40-marker views: 10 verified markers reproduce
  the full-board pose to 0.09° / 0.12 mm median (p95 0.22° / 0.37 mm). High mode
  now accepts ≥16 markers per camera and ≥16 shared instead of 28, with the same
  witness, focus and coverage gates.

### 2.4 Stationarity and dwell (`controller.py`, `calib/guidance.py`, `coverage.py`)

* Every decided pose is tracked; speed and rotation are fitted over a trailing
  0.45 s window instead of a single frame pair. Vetoed frames now carry a pose
  and a motion metric instead of none.
* Motion-blur rejection only applies to genuinely adjacent samples (≤150 ms).
  Across a sparse decision gap, inter-frame displacement is not exposure blur;
  this removed 28 false blur rejections on the real curated replay.
* A real hold now yields a qualified dwell interval: 0 in fresh-01, **7
  qualified intervals from 7 real holds** in the rehearsal.

### 2.5 Roll and guidance consistency (`controller.py`, `coverage.py`)

* The UI pose summary uses `coverage.roll_deg`, the same geometric in-plane
  angle coverage counts. Previously the UI showed −46.9° where coverage counted
  +44.3° for the same view.
* Guidance is recomputed when the *pose* changes (15 mm / 5° cell, ≤0.3 s
  throttle), not only when an accepted view arrives; a 107 s stale instruction
  cannot persist.
* Four roll sectors are accepted through the normal acceptance path
  (`tests/test_roll_sectors.py`), and the target constructed by `family_pose`
  round-trips to the sector being counted.

### 2.6 Auto exposure (`tools/wizard/exposure.py`, `gui/modern_window.py`)

* Plate-ROI metering with marker-disk masking; a 2 s stale ROI reuses the last
  box but **never raises** exposure from background; full-frame metering only
  gross-clips.
* Hard clipping (≥15 %, p95 ≥250) fast-downs one stop and bypasses the freeze;
  soft saturation/mean drives 0.85× steps after confirmation; a 5 % dead band
  and highlight-headroom hold stop oscillation; strict no-op suppression.
* Requested/readback tracking with a 2 s grace reports a rejected write instead
  of hiding it; a run id is attached; one baseline reset is applied per
  recording run so a new session does not inherit a pinned exposure file.
* Closed-loop simulation from the observed pinned state: legacy **51 writes /
  31 IR reversals / 15 s RGB pinned** vs new **8 writes / 1 reversal / 0 s
  pinned**; no-op fields 19 → 0.

### 2.7 Display and worker (`gui/plate3d.py`, `gui/modern_window.py`, `gui/worker.py`)

* The 3-D plate interpolates rigidly toward the newest measured pose (3×3 SVD,
  60 Hz timer) and dims with an explicit "plate not tracked" state when no
  decision has produced a pose for >0.75 s. Offscreen renders are deterministic.
* Newest-frame policy, decision-duration/lag, busy fraction, overload and
  refused-submission counts are explicit and measured; preview continued for
  114/120 groups while a decision was in flight.

## 3. Before / after

| measurement | before | after | source |
|---|---:|---:|---|
| decision p50 on 24 real tail frames | 655 ms | **228 ms** | this session |
| decision max on the same frames | 1,601 ms | **867 ms** | same |
| views accepted from those 24 frames | 0 | **11** | same |
| partial camera detection p50 | 218 ms | **30 ms** | partial-board report |
| per-frame detection p50 (3 cams) | 655 ms | **117 ms** | same |
| mirrored labellings, 240 real cameras | 41 | **0** | same |
| solve at 16 views | 13.36 s | **0.58 s** | performance report |
| solves in a 162-view session | 33 | **10** | schedule simulation |
| field dwell intervals | 0 | **7** (7 real holds) | rehearsal |
| roll sectors (real acceptance) | 2 | **4** | `test_roll_sectors.py` |
| exposure writes / IR reversals | 51 / 31 | **8 / 1** | audit simulation |

## 4. End-to-end rehearsal on real recorded images

Source: `tools/wizard/real_view_source.py`, which replays selected **real**
accepted PNG triplets from fresh-01 at a live-like cadence, with seven real
stationary holds (the same recorded view repeated at 10 Hz). The physical
session never captured a sharp 270–360° view, so one real centred view is
rotated 180° in-plane as an explicitly labelled surrogate; no synthetic pixels
are used.

Command (headless modern GUI, async solve, final validation):

```bash
.venv/bin/python tools/wizard/gui/app.py --source views \
  --views-dir ~/einstar-calibration-fresh-01 --select 30 --holds 7 \
  --hold-repeats 8 --rotated-views 180 --async-solve --headless --finish \
  --mode high --session-dir /tmp/e2e-final --duration 900
```

### 4.1 fresh-01 alone: 7/8 and a truthful refusal

34 of 90 groups accepted, all nine underlying checks pass except
**in-plane roll sectors**; the modern GUI refuses Finish ("Finish is locked
until coverage is complete") and no candidate is written. This is correct: the
recording never captured a *sharp* 270–360° view (the two tail frames are
motion-blurred, focus 24/28 vs p50 746).

A rotation surrogate was built and then **rejected as physically invalid**:
rotating each camera image independently about its own optical axis is not a
rigid rig transform, and the solver residual rose from **0.66 px to 5.05 px**
when the rotated view entered the fit. It is not used in any validated result.

### 4.2 fresh-01 + three real EXStar sector-3 views: 8/8 READY, validated

The EXStar 25-view session of the **same scanner and original plate** contains
sharp 40-marker views at roll 272–280°. Appending three of them (indices
14/22/4, all real pixels, no resampling) to the 34 fresh-01 views and seven real
holds gives **37 accepted views, coverage 8/8, READY**, and a candidate that
passes the independent lifecycle validation (`passed: true`,
`would_activate: true`) with one advisory (`quality status fail`):

| metric | value |
|---|---:|
| accepted views | 37 (34 fresh-01 + 3 EXStar) |
| held-out / cross-camera worst RMS | 0.49 px (p95 0.92) |
| stereo baseline | 160.358 mm |
| baseline sigma | 0.0486 mm |
| stereo rotation sigma | 0.0067° |
| colour translation | 0.050 mm (0.145 px @400 mm) |
| roll bins covered | 4/4 |
| stationarity intervals | 7 (7 real holds) |
| fit RMS (all views, robust) | 2.34 px |

The two recordings are the same physical scanner and plate under the same
factory profile; the hybrid is labelled as such and is not presented as one
physical session.

The fully synthetic 36-view `coverage-mid` GUI test also reaches 8/8 and a
validated candidate (`tests/test_wizard_gui_ready.py`), which is the
guidance-driven workflow proof.

## 5. Tests

Baseline before this milestone: 601 passed / 8 skipped. New tests:
`test_session_hygiene.py` (5), `test_async_solve.py` (2), `test_dwell.py` (2),
`test_roll_sectors.py` (5), `test_real_view_source.py` (2, private-data guarded),
`test_wizard_worker_budget.py` (10, performance task), plus the exposure suite
(26, was 6) and partial-detection additions.

Full suite: **651 passed, 8 skipped** (8 min 06 s, `.venv/bin/python -m pytest tests -q`);
driver C++: **12/12** (`build/driver-ninja`, ctest). The single earlier failure
(`test_live_capture_source_plumbing`) reproduced only while a heavy GUI
rehearsal was running concurrently and passes in the clean run.

## 6. Limits and unresolved questions

* **No scanner was used after the changes.** Exposure and detection improvements
  are proven on recorded pixels, unit tests and a synthetic plant, not by a new
  physical run. A physical rehearsal is the next step, not a claim of success.
* The missing roll sector required a **rotated real view** in the rehearsal
  because the only 270–360° frames in the recording are motion-blurred. The
  product fix is a responsive pipeline that lets the operator hold still at the
  highlighted orientation; that has been rehearsed, not physically repeated.
* RGB remains dark on 21/35 real accepted frames by the exposure classifier
  (would request more light); at 20000 µs / gain 300 the colour channel is at
  its limit and the device rejected at least one gain write (`device status 3`).
  The wizard now reports that mismatch instead of hiding it.
* Detection still cannot verify a plate whose large markers are **occluded**
  rather than out of frame, by design.
* The ~1 % vendor calibration-branch difference is unchanged and unresolved.
* `exposure.jsonl` remains append-only; new rows carry a run id but old rows
  cannot be retroactively separated.
