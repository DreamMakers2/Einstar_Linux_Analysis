# Status — what is built, what is measured, what is not known

One page for deciding what to do next.  Every number here is from a recorded run
or a test in this repository; the source document is named so it can be checked.
Where something is *not* established, it says so.

Date: 2026-11-12.  Private HEAD `aa030c7`, public HEAD `678eeda`, 372 tests.

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

**Not done:** the one comprehensive physical recording.  The software is ready;
the procedure is in `docs/calibration/wizard_operator_guide.md`.  It needs the
plate physically moved through the coverage families.

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
