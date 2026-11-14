# Status — what is built, what is measured, what is not known

One page for deciding what to do next.  Every number here is from a recorded run
or a test in this repository; the source document is named so it can be checked.
Where something is *not* established, it says so.

Date: 2026-11-14.  This working tree: **651 Python tests**, 12/12 driver
C++ tests in `build/driver-ninja`, 1/1 reconstruction C++ tests, plus 8 real
parent/child process-safety tests.

---

## 0c. fresh-01: the latest failed physical session (2026-11-14)

Read-only reconstruction: [`analysis/calibration/fresh01/REPORT.md`](../analysis/calibration/fresh01/REPORT.md).
The session directory was **reused**, so it holds two/three attempts at once.

| finding | number | source |
|---|---|---|
| accepted views | 162 total, **70 from the previous attempt**, 92 from this one | view journal clocks |
| duplicated group ids in `decisions.jsonl` | 117 | same |
| decisions actually made | 866 of 4513 groups seen (1.81/s); 3647 superseded | `ui_metrics.json` |
| decision latency | p50 239 ms, p95 1225 ms, max 15369 ms | same |
| measured rig solve | 0.19 s @8, 1.75 s @25, 2.68 s @40, 6.47 s @60, 12.40 s @84 | real observations |
| real high-roll views never decided | group 4631 roll 303.8° (26/34/34 markers, focus 28 vs p50 746), 4627 roll 317.3° (15/30/29, focus 24) | last 120 frames + decisions |
| stationarity | 0 dwell intervals from 2 stationary observations | progress snapshot |
| auto exposure | 93 entries, 29 no-op `rgb_gain 300`, RGB pinned 58/60, 17 IR reversals | `exposure.jsonl`, audit |
| **fixes** | session/frame-dir quarantine; async rig solve; monotonic live timestamps; windowed dwell over every decision incl. vetoes; pose-following guidance; UI roll = coverage roll; auto-exposure no-op/hysteresis/metering | tests below |

New tests: `test_session_hygiene.py`, `test_async_solve.py`, `test_dwell.py`,
`test_roll_sectors.py`, `test_real_view_source.py`, `test_wizard_worker_budget.py`.
Performance evidence: `analysis/calibration/performance/`.
Exposure audit: `analysis/calibration/auto_exposure/AUDIT.md`.

| after-fix measurement | value | source |
|---|---:|---|
| decision p50 / max on 24 real tail frames | **228 / 867 ms** (was 655 / 1601) | live replay |
| views accepted from those 24 frames | **11** (was 0) | same |
| partial camera detection p50 | **30 ms** (was 218) | partial report |
| mirrored labellings, 240 real cameras | **0** (was 41) | same |
| solve @16 views, one BLAS thread | **0.58 s** (was 13.36 s) | performance report |
| solves in a 162-view session | **10** (was 33) | schedule simulation |
| dwell intervals, 7 real holds | **7** (was 0) | rehearsal |
| real-image GUI rehearsal | **37 accepted, 8/8 READY, validated candidate** | hybrid fresh-01 + 3 EXStar real sector-3 views |
| display: snapshot / plate render p50 | **0.61 / 0.57 ms** | `display_latency.json` |

---

## 0b. The Windows reference (2026-11-14)

The user ran a fresh EXStar calibration on Windows on the same scanner.  This
milestone mined it, and the result changes one conclusion materially.

| finding | number | source |
|---|---|---|
| reference recording | 66.400 s, 1992 frames, 1920x1080, **audio silent**, **edited by Clipchamp** (~85–90 s of the session is missing) | `calibration/exstar_reference_video_analysis.md` |
| EXStar's real operator work | **25 poses** (5 orientations x 5 distances), **1.87 s/accepted view**, geometric 48.6 s, white balance 7.7 s, **automatic acceptance, zero clicks** during acquisition | same |
| on-disk session | 00:09:52 → 00:14:55 = **5 min 03 s** (3 min 19 s geometry, 1 min 42 s white balance) | same, mtimes |
| EXStar's new calibration | baseline 158.5570 → **158.6128 mm** (+0.035 %), IR rotation 22.1383° → 22.1290°, `fx` −0.67/−1.11/−0.15 px | `calibration/exstar_vs_factory_vs_linux.md` |
| distortion | **bit-identical** across 2024 → 2026-02 → 2026-11 and equal to the device's factory records: EXStar has **never re-estimated distortion** | same |
| white balance | diagonal gains only (slot0 1.6035, slot2 1.7940); CCM and dark **unchanged bytes**; algorithm recovered from `Sn3DColorCorrect.dll` (centred-window grey-world, dark-subtracted, green pinned) | `calibration/white_balance.md` |
| device writes | EXStar **wrote** the new CCF (0x39B) and WB (0x12BC) into the scanner's flash; post-run readback `256dd440…` has byte-identical CCF/WB and the date string `2026-11-14 00:13` | `calibration/exstar_hardware_state.md` |
| **the scale question** | the scanner stores **two** vendor calibrations 1 % apart: factory records **160.0916 mm** and the CCF **158.557/158.6128 mm**.  Our 160.0137 mm reproduces the **factory** record to **0.08 mm** and its colour extrinsic to **0.0005 mm / 0.04°**, while the CCF's colour extrinsic is **8°** off | `exstar_vs_factory_vs_linux.md` §5 |
| vendor corpus refit | our fit on the vendor's own 25 fresh images: baseline **160.210 mm**, RMS **0.314 px**; the vendor CCF explains its own images at **1.420 px** and a rig-scale x1.0100 collapses it to 0.445 px | `calibration/exstar_vendor_corpus_refit.md` |
| model selection | held-out error prefers free PP+distortion (0.7512 vs 0.7567 px) and moves `fx` **down**/baseline **up**, i.e. **away** from the CCF and onto the device record; no model variant reaches 158.56 mm | same |
| missing views | base session **6/8** → recovered overlay **7/8** (D7 only); plan = **4 holds, ~1.6 min** | `calibration/missing_views_workflow.md` |
| production capture | no `raw.e1r` stream, and the **controller now writes the compact `ViewStore` layout by default**: measured on 24 real views, **1.69 MB/accepted view vs 3.95 MB PGM (2.34×)**, 40.5 MB vs 94.8 MB session tree, +95 ms/view encode, zero per-group staging writes; `verify`/`resume` round-trip passes.  `storage="legacy"` keeps the old `groups/*.pgm` path for old sessions | `calibration/production_capture.md` §10 |

The milestone report with the operator's next physical action is
[`calibration/exstar_milestone_report.md`](calibration/exstar_milestone_report.md).

**Not achieved:** an absolute length reference.  The 1 % branch question is now
*localised* (two vendor datasets, not our fit), but only a measured plate pitch
or a certified artefact can decide it.

---

## 0a. Session-03 recovery (2026-11-13)

The irreplaceable 52 GB physical session was recovered read-only and audited;
see [`calibration/session03_recovery.md`](calibration/session03_recovery.md).

| finding | result |
|---|---|
| recording integrity | full 52 GB stream verify: trailer digest MATCH, 1,266,761 transfers, 13,195 groups / 39,585 frames, 76.2 s at 681 MB/s / 5.9 MB RSS |
| displayed 5/8 cause | GUI counted D7 twice and dropped D8; honest pre-fix count was 6/8 (D4 + D7) |
| D4 right corners | **recovered from the raw recording** (groups 89, 5682, 4019, 6241); no new capture needed |
| D7 stationarity | genuinely fail: only 2–3 distinct dwell poses in 22 min; the old 5 was an artifact, the corrected verdict is 2–3 < 6 |
| 297-view evidence | 293 fitted after culling `[6,36,129,161]`; hard-fixed factory principal point + held distortion; RMS 0.6514 px, held-out 0.770 ± 0.019, epipolar 0.333 vs 0.465 factory; **standard target only** |
| scale | unvalidated; IR baseline 160.014 vs factory 158.557 mm (+0.92 %), indistinguishable from the pitch question |
| acquisition contradiction | reader stopped 180 s before the capture child; `cancel()` did not stop it; shutdown SIGKILLed the child during a 52 GB re-verify. Fixed with one authoritative state machine and bounded trailer-only finalisation |
| remaining before activation | 4 short distinct dwell holds (~2 s) + an independent length artefact; do not repeat the 297-view session |
| runtime | full replay 98.9 s / 525 MB/s / 103 MB RSS; feature cache 763.9 s, 17.3 groups/s, 91.5 MB; stage-1 retention 143.6 s |

---

## 0. Performance and architecture (2026-11-12)

| claim | number | source |
|---|---|---|
| 35-view finalisation | **38.94 s → 3.47 s**, 11.2x, same optimum (RMS 0.33037 vs 0.33044 px) | `docs/calibration/performance_overhaul_2026-11-12.md`, `analysis/calibration/overhaul/` |
| 35-view analytic solve | 0.57 s vs 5.01 s SciPy reference (7.5x), parameter agreement 4.4e-5 | `tests/test_solver_equivalence.py` |
| verification passes | SciPy FD → analytic LM; 3 sensitivity analyses → 2 unique (content-keyed cache); raw hash overlapped | same |
| live preview | **9.75 Hz**, p50 113 ms, p95 121 ms; decisions 4.55/s | `analysis/calibration/overhaul/live_validation_2026-11-12.json` |
| live acquisition | 9.76 groups/s, 36.9 MB/s, 0 transfer timeouts, 1 partial, 3 startup drops | same |
| GUI finish | responsive, final solve on `FinishWorker`; modern operator window replaces the engineering dashboard | `tools/wizard/gui/modern_window.py` |
| auto exposure | IR 1200→2343 µs, RGB 5000→9765 µs in 12 s; volatile only; every change logged | `docs/calibration/auto_exposure.md` |
| process death | GUI `SIGKILL` → capture stops, trigger 0/0, illuminator readback 0, `cleanup_verified` | `docs/calibration/process_safety.md` |
| child death | capture-child `SIGKILL` → parent runs volatile `--safe-off`, trigger readback 0/0 and illuminator readback 0 verified, GUI error surfaced, no orphan | same, `analysis/calibration/overhaul/capture_child_sigkill.json` |
| Qt teardown | proven unparented-dialog/lambda cycle; fixed deletion + bound methods; 8/8 fresh-process exit checks | `analysis/calibration/overhaul/qt_teardown_root_cause.md` |
| realistic E2E | 33 accepted, 8/8 READY, **would_activate true**, 41.1 s, 8.3 cores, 642 MB | `analysis/calibration/overhaul/end_to_end_33view.json` |
| 54-view E2E | 54 accepted, 8/8, validated with quality advisory, 72.6 s, 864 MB | `analysis/calibration/overhaul/end_to_end_60view.json` |
| refusal | vendor stills 16 accepted, 5/8, no candidate written | `analysis/calibration/overhaul/vendor_refusal.json` |
| pause/resume | shadowed `resume` fixed; 188 groups, 32.65 MB/s, cleanup verified | `analysis/calibration/overhaul/live_pause_resume.json` |
| moving replay | 10.0 Hz split preview p95 114 ms on real sweep; 2/8 coverage, not READY | `analysis/calibration/overhaul/moving_replay_30s.json` |
| fast-solver fallback | stationary `no_descent` no longer triggers SciPy; 100/100 LM, equivalence unchanged | `analysis/calibration/overhaul/solver_stationary_fix.json` |
| 120 s soak | 9.96 groups/s, 39.2 MB/s, 0 transfer timeouts, 3 startup drops, preview 9.96 Hz p95 117 ms, cleanup verified | `analysis/calibration/overhaul/sustained_soak_120s.json` |
| auto-exposure stability | 3 changes in 60 s and 120 s, then fixed; plate-ROI metering removed the 55-change oscillation | `docs/calibration/auto_exposure.md` |
| 49–60-view scaling | 60-view solve 2.53 s; 49-accepted synthetic finish 4.96 s, RMS 0.413 px | `analysis/calibration/overhaul/solver_scaling_20_60.json`, `synthetic_finish_49view.json` |
| CPU/GPU | 193% process average (1.93 cores), top thread 44%; GPU unchanged from idle, calibration is CPU-only | live validation JSON |

**Still not achieved:** a full physical calibration session, absolute accuracy,
an 8 h soak, and a stated GPU speedup (none is justified for this problem size).
A 120 s soak passed; a first longer attempt exhausted a tmpfs, which led to the
2 GB disk guard. Power loss and kernel-level I/O stalls remain outside software
control.

---

## 1. Calibration: complete and validated

| claim | number | source |
|---|---|---|
| end-to-end synthetic session | 33 accepted views, 8/8 coverage dimensions, 9/9 lifecycle gates, 18 free parameters, condition 1600, 80 s | `docs/calibration/wizard_results.md` |
| activation writes a revision | revision 1, state `validated` | same |
| rollback restores factory | factory files byte-identical | same |
| recovered stereo baseline | 161.244 mm against a truth of 161.314 mm (**0.070 mm**) | same |
| cross-camera residual | ground truth 1.15 px, candidate 0.55 px | same |
| partial-plate recognition | 39/40 markers labelled from a corner view that loses the chiral marker | `docs/calibration/partial_plate_recognition.md` |
| orientation witness | identity must beat every lattice symmetry; 10 regression tests | same |
| refusal behaviour | 17 insufficient cases refused with named reasons | `docs/calibration/wizard_failure_matrix.md` |

**Verified through the real application** (launcher → wizard, replay): the
window accepted 8 views, rejected 33 as redundant, reported `3/8` coverage and
declared **NOT READY** with the exact missing dimensions - all 41 markers were
detected in all three cameras, so the refusal is about geometry, not detection.

**Not done:** the one comprehensive physical recording.  The preserved
35-view capture (`~/einstar-calibration-02`) remains **NOT READY** for
distance/tilt/roll/region/stationarity coverage; the performance overhaul did
not change that verdict.  The procedure is in
`docs/calibration/wizard_operator_guide.md`.

## 2. Scanner: working and measured

| claim | number | source |
|---|---|---|
| single-frame plane accuracy | **0.30 mm RMS** (0.27 mm with confidence thresholding) | `docs/reconstruction/*` |
| tracking rotation, full-res | **0.048 deg** median vs the plate's own PnP pose | `docs/tracking/tracking_baseline.md` |
| tracking rotation, depth-only mask | **0.06 deg**, no plate detection used | same |
| tracking rotation at scale 0.5 | 0.24 deg (cloud path 0.58 deg) | same |
| tracking loop cost | ~90 ms/frame (ICP capped at 30 k points) | `docs/reconstruction/pipeline_latency.md` |
| fusion accuracy | sphere p95 **0.03-0.04 mm** | `docs/reconstruction/fusion_and_meshing.md` |
| fusion at scale | 100 frames in 9.4 s = **94 ms/frame**, 3.28 M voxels | same |
| whole loop | **1.58 fps** at scale 0.5 (recon 200 ms, tracking 90 ms, fusion 60 ms) | `docs/reconstruction/pipeline_latency.md` |
| 8-frame scan | 5.1 s wall, same vertex count as before the optimisation | same |
| colour geometry | **0.32 px** on an independent session (shipped record: 461 px) | `docs/calibration/colour_registration.md` |
| colour cast | **29.2 % -> 2.5 %**, 0.32 % clipping | same |
| determinism | two runs of the same recording produce a **byte-identical** mesh | `docs/reconstruction/scan_application.md` |
| live verification | 10 frames, 0 rejected, colour on, novelty 100->0 %, `converged`, 6.4 % open boundary, device safe afterwards | same |

**Not achieved:** 10 fps.  The reconstruction itself (~200 ms) is the floor at
this scale; the objective's "do not adopt CUDA because an isolated kernel is
faster" rule still applies, and no end-to-end GPU measurement has been made.

## 3. What is not known

| unknown | why it matters | what would settle it |
|---|---|---|
| **absolute dimensional accuracy** | every claim above is relative or against a derived truth | a manufactured artefact of known size scanned by this rig |
| **comparison against EXStar** | the objective asks for a measured comparison, not an assertion | EXStar output for the same object, or a same-object scan pair |
| plate pitch (28 mm) vs a ~1 % scale discrepancy | a profile can be geometrically validated and still carry this scale | the same artefact, or the vendor's own metrology sheet |
| colour *photometry* | geometry is fixed, but colour reproduction is untested | a session lit for the colour camera (ambient reaches p99 luminance 37/255) |
| object-completeness criterion | novelty and open-boundary fraction are hints, not proofs | a model-level criterion (closure, holes) or a user-marked region |

## 4. How to check all of it

```sh
.venv/bin/python -m pytest tests/ -q      # 601 passed, 8 skipped
ctest --test-dir build/recon              # 1/1
ctest --test-dir build/driver             # 13/13
scripts/verify_all.sh                     # includes the static check
```

A clean clone of the public repository builds both C++ trees and passes its own
`verify_all.sh` (463 passed, 79 skipped without private fixtures; driver 14/14) - see
`docs/readiness.md`.

## 5. The honest summary

The calibration and scanning software works, is measured, and refuses what it
cannot support.  The two questions a buyer would ask - *how accurate is it in
absolute terms* and *how does it compare with EXStar* - cannot be answered from
what is available here, and this project does not answer them by assertion.
