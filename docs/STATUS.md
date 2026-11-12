# Status — what is built, what is measured, what is not known

One page for deciding what to do next.  Every number here is from a recorded run
or a test in this repository; the source document is named so it can be checked.
Where something is *not* established, it says so.

Date: 2026-11-12.  This working tree: **429 Python tests**, 13/13 driver
C++ tests, 1/1 reconstruction C++ tests, plus 8 real parent/child process-safety
tests.  The previous published HEAD was `aa030c7`; this milestone is committed
separately.

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
.venv/bin/python -m pytest tests/ -q      # 372 passed
ctest --test-dir build/recon              # 1/1
ctest --test-dir build/driver             # 13/13
scripts/verify_all.sh                     # includes the static check
```

A clean clone of the public repository builds both C++ trees and passes its own
`verify_all.sh` (278 passed, 70 skipped without private fixtures) - see
`docs/readiness.md`.

## 5. The honest summary

The calibration and scanning software works, is measured, and refuses what it
cannot support.  The two questions a buyer would ask - *how accurate is it in
absolute terms* and *how does it compare with EXStar* - cannot be answered from
what is available here, and this project does not answer them by assertion.
