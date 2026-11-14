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

The `validation: "pending"` field is the **lifecycle** state (a profile is never
activated automatically); the authoritative gate results are
`passed: true` / `would_activate: true` with one quality advisory
(`sigma_fx` 0.568 px against the high-mode 0.35 px target).

Independent verification reproduced every number above from a clean
`git archive` of the published commit, twice, to ~1e-7 px
([`analysis/calibration/verification/INDEPENDENT.md`](../../analysis/calibration/verification/INDEPENDENT.md)).

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

Full suite: **655 passed, 8 skipped** (8 min 35 s, `.venv/bin/python -m pytest tests -q`);
driver C++: **12/12** (`build/driver-ninja`, ctest). The single earlier failure
(`test_live_capture_source_plumbing`) reproduced only while a heavy GUI
rehearsal was running concurrently and passes in the clean run.

Public checkout (`DreamMakers2/Einstar_Linux_Analysis`, sanitized):
**517 passed, 79 skipped** (`pytest tests -q`, 6 min 20 s).

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


## 7. Commits and publishing

| repository | commit | content |
|---|---|---|
| private research tree | `abf483b` | the milestone: detection, solver, dwell, roll, exposure, GUI and evidence |
| private research tree | `4666798`, `ffb0859` | export-only path-scrub hygiene (home-relative paths kept intact for tests) |
| private research tree | `533d790`, `90aad45` | independent verification, stale-ROI/duplicate-write fixes, stereo rig gate, partial-vs-full accuracy study |
| public `DreamMakers2/Einstar_Linux_Analysis` | `2ab35df`, `0000873`, `c54ef03` | sanitized mirror of the above |

The public tree contains no recordings, no firmware, no factory calibration
backup, no scanner serial and no absolute private path. `scripts/export_public.sh`
performed the copy, untracked-file pruning, path scrub and forbidden-content
check; the sanitized checkout was then tested from its own working tree.


## 8. Independent verification and the bug it found

`analysis/calibration/verification/INDEPENDENT.md` (Agent G) reproduced the
session-isolation, factory-protection and real-E2E claims from a clean export:
**all hold**, including 37 accepted / 8/8 / `passed=true` / `would_activate=true`
and 55 targeted regression tests.

It also found one real defect the exposure audit missed: `RoiHold` aged the
plate box from the **poll clock**, and the GUI re-presents the cached
`detections` list at 10 Hz while detections only refresh on a decision. A stale
plate box therefore stayed labelled `"roi"` forever and could still raise
exposure from the dark background after detection was lost (probe: same list at
t=0 and t=100 s both `"roi"`, IR 1200 → 1500 µs where `"stale"` holds).
Fixed by stamping detections with a monotonic time at ingest
(`detections_age_s` in the snapshot) and ageing the ROI from that stamp; a
detection older than the hold window is now `"none"`, never fresh. Identical
in-flight requests inside the 2 s readback grace are also suppressed (two
duplicate writes for a frozen value were observed).

A second verification nuance: `--vendor-views` positions are **0-based list
positions**, not `imageLeftNN` file numbers. The command above is the exact one
that reproduces the table. `--rotated-views` must be omitted (or empty), not
`none`.

Two further safeguards were added after the verification pass:
* a per-view **stereo rig-consistency** check: both IR cameras must imply the
  same left→right transform (4 deg / 20 mm), so a lattice slip in one camera cannot
  reach the solver; unit test with a one-pitch label slip.
* a **frozen tilt centre** was tested and **reverted**: freezing the centre
  after eight mixed views regressed the validated real-image rehearsal from
  8/8 to 7/8 because those first views were not neutral. The optional
  `tilt_center_deg` API stays, but the live wizard keeps the dynamic centre.
  The moving-centre behaviour (a dimension can flip as views are added) remains
  a known limitation of the relative tilt definition.


## 9. Does partial-board acceptance cost accuracy? (independent study)

`analysis/calibration/accuracy/PARTIAL_VS_FULL.md` (Agent D) re-detected all 162
fresh-01 accepted views with the chiral identity witness (99.72 % of the stored
label rows reproduced exactly), then compared full-board-only, all-views and
bounded-hybrid arms under one **held-out** protocol: 3 repeats × 4 folds, fixed
seed; the held-out board pose is PnP-solved from its left view only and the
right/colour pixels are predicted through the fitted rig; every arm uses the
production solve plus the finish prune and one refit.

| arm (train views) | held-out cross RMS median | p95 | stereo baseline sigma | stereo rotation sigma |
|---|---:|---:|---:|---:|
| full-board only (60) | 0.891 px | 2.248 px | 0.059 mm | 0.070 deg |
| all accepted (122) | **0.860 px** | **2.182 px** | **0.039 mm** | **0.044 deg** |
| hybrid: core + 10 best partial (70) | 0.906 px | 2.228 px | 0.052 mm | 0.062 deg |
| hybrid: core + 20 best partial (80) | 0.872 px | 2.219 px | 0.047 mm | 0.058 deg |
| matched 60 views, 1/3 full + 2/3 partial | 0.858 px | 2.280 px | 0.054 mm | 0.062 deg |

* all-views vs full-only: **Δ −0.030 px** (paired 95 % CI −0.043…−0.019), and the
  gain is concentrated on held-out *partial* views (0.927 → 0.870 px);
* at a matched view budget the difference is within noise (CIs include 0), so a
  partial view is worth slightly less than a complete board but is not harmful;
* conditioning improves significantly (baseline sigma 0.059 → 0.039 mm) because
  the same 39 rig parameters see more observations;
* a positive control shows the metric resolves a 0.02° stereo rotation
  (+0.045 px), so every arm difference above is below 0.02° equivalent.

**Verdict:** enabling partial views does not regress accuracy or conditioning on
this dataset; the safeguards that make it safe are the chiral identity witness and
the 3 px finish prune. Limitations: one session, mostly mild clipping
(median 36/40), reconstructed partial labels, and only four heavily-clipped
probes; it says nothing about boards clipped to a third of the field or about
EXStar.


## 10. Acceptance criteria status

| # | criterion | status | evidence |
|---:|---|---|---|
| 1 | partial-board tracking with unambiguous identity | **met** | chiral witness; 240 real cameras 95.1 % identity, 0 mirrored; `tests/test_detect_partial.py` |
| 2 | valid partial views accepted when geometrically justified | **met** | real partial acceptance; held-out study §9 |
| 3 | automatic exposure operational and effective near/far | **partly proven** | control path proven on hardware 2026-11-12; policy/ROI fixes verified by tests, real-frame audit and a closed-loop 420→550→320 mm simulation; **no post-fix hardware run** |
| 4 | all four roll sectors via normal acceptance | **met** | real hybrid 4/4 (44–277°); synthetic roll tests |
| 5 | stationary holds recognized | **met** | 7 qualified intervals from 7 real holds; `tests/test_dwell.py` |
| 6 | responsive 3-D view independent of optimization | **met** | async solve; snapshot p50 0.61 ms, render p50 0.57 ms; tracking-loss state |
| 7 | no multi-second stale guidance | **met** | pose-keyed guidance, 0.30 s throttle; pose age p50 9.2 ms |
| 8 | stalls explained and corrected | **met** | async solve, BLAS pinning, partial-detector early exit; decision p50 228 ms on the real tail |
| 9 | selection prioritizes missing evidence, no endless useless requests | **partly met** | coverage family guidance, satisfied-tilt latch, progress/stall/unreachable advisor; a cooperative **guidance-following operator still stops at 7/8** (four signed tilt quadrants) after 45 targets — see `analysis/calibration/guided_operator/REPORT.md` |
| 10 | realistic from-scratch offline READY with small view count | **met for a curated replay, not for guidance** | 37 real-image views (34 fresh-01 + 3 EXStar sector-3, 7 holds) reach 8/8 + validated candidate, and a 36-view synthetic GUI rehearsal does; but a guidance-following operator reaches only 7/8 in 45 targets |
| 11 | independent validation not regressed | **met** | held-out study: all-views 0.860 vs 0.891 px full-only; conditioning improved |
| 12 | factory, recordings, hardware protected | **met** | independent verification: byte-identical factory, nothing deleted, no activation call |
| 13 | bounded storage and RAM | **met** | production store unchanged; 1.69 MB/view; bounded ring/queues |
| 14 | complete suite + clean clone | **met** | 655 passed / 8 skipped private; 518 passed / 79 skipped public (final latch commit); driver 12/12 |

**Remaining before a physical claim:** a short real session that (a) exercises
auto exposure across the distance range with the fixed ROI ageing, and (b) shows
the guidance-driven workflow reaching READY in 25–40 held views on the device.
The offline rehearsal that the milestone required before asking for that session
is complete.


## 11. Guidance-driven operator: a negative result worth keeping

After the curated replay, a synthetic operator was made to follow the live
guidance exactly (same rig, requested pose rendered, short hold, retry on
rejection). It reaches **7/8** in 45 targets: D1 and D3–D8 pass, the four signed
tilt quadrants do not; final fit RMS is good (0.42 px) but
`would_activate=false`.

The cause is the moving relative tilt centre: the guidance is shown a quadrant
as satisfied while the operator's accepted cluster shifts it, so it keeps
issuing small signed-tilt nudges ("tilt 3 deg right and down") instead of
driving to the least-covered signed quadrant. Latching the check hides the gap
from guidance (the final validation then finds it), and giving guidance a fixed
bootstrap reference alone does not help because the *measurement* still reports
the quadrant as transiently satisfied. The failed variants were not shipped;
the evidence is `analysis/calibration/guided_operator/REPORT.md` and its two
JSON logs.

This is the remaining product gap: the **curated** 37-view rehearsal proves the
gate is satisfiable, not that guidance finds it. The next change should count
the four **absolute** tilt quadrants from accepted poses against a fixed
reference and request the least-covered one explicitly, then re-run this
operator; that is a guidance-selection design fix, not a threshold tweak.
