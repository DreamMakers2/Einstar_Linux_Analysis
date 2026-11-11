# Calibration wizard — measured results (offline rehearsal + live validation)

Status: 2026-11-11.  Everything below was executed on this machine.  Sections
are labelled **OFFLINE** (recorded/сynthetic data, no device), **LIVE** (physical
scanner) and **LIMIT** (known weakness).

## 1. What was built

| component | file | role |
|---|---|---|
| frame sources | `tools/wizard/sources.py` | replay directory, raw recording, live capture, synthetic scenes; orientation applied once |
| live supervisor | `tools/wizard/capture.py` | one capture process, control file (start/pause/resume/stop), ring, safe-final-state check |
| session controller | `tools/wizard/controller.py` | wraps `calib.session.CalibrationSession`; one decision path for live and replay |
| coverage gate | `tools/wizard/coverage.py` | D1–D8 measurement, operator hints, readiness |
| observability | `tools/wizard/observability.py` | marginal σ per parameter, condition number, verdicts, staged release |
| profile lifecycle | `tools/wizard/lifecycle.py` | factory / candidate / validated, 9 gates, activation, rollback |
| session IO | `tools/wizard/session_io.py` | atomic manifests, resume, hash verification |
| Qt6 GUI | `tools/wizard/gui/` | live previews, marker overlays, guidance, coverage map, observability, controls |
| capture tool | `driver/tools/capture_frames.cpp` | `--control`, `--ring`, `--max-seconds`, `--session-dir`, SIGINT/SIGTERM graceful stop |

## 2. OFFLINE — replay of the real 350-group sweep

```
tools/wizard/gui/app.py --source replay --frames <private-data>/sweep-b/frames
                      --pace 20 --mode quick --headless --duration 20
```

* 350 groups ingested, **16 accepted**, 334 rejected as redundant, 14 solves.
* Accepted subsample: roughly one view per 8–15 mm of plate motion along the
  1-D path — the expected behaviour, not a failure.
* Coverage: **4 / 8** dimensions.  Missing (verbatim): distance bands; tilt
  about the x axis (positive and negative); tilt about the second axis (y);
  tilt quadrants; in-plane roll sectors; image-plane 3×3 regions; image corners.
* Observability: condition number ≈1e6, `left.fx` 1-σ ≈ 9 px, verdict
  **unobservable**; stereo rotation partly constrained; gates FAIL
  `stereo_rotation`, `baseline`, `colour`.
* Readiness verdict: **NOT READY** — the one-axis sweep is correctly rejected.

## 3. OFFLINE — vendor 25-view corpus (real EXStar capture set)

```
tools/wizard/vendor_source.py  (25 still triplets, continuous=False)
```

* **17 accepted**, 8 rejected with a specific reason (mirrored geometry in the
  known view 9, ambiguous/partial witnesses elsewhere).  The corpus is stored
  already in calibration orientation (verified: rotating the right frame breaks
  stereo consistency from 164 mm / 22.8° to 785 mm / 164°).
* Coverage: distance bands satisfied (264–610 mm), stereo overlap and colour
  satisfied; tilt-two-axes, roll sectors and image regions still missing.
* Final solve: rms 0.36 px; candidate written with `validation: pending`.
* Phase-7 gates (independent implementation in `lifecycle.validate`):

| gate | result | measured |
|---|---|---|
| cross_camera | **PASS** | 0.46 px held-out L→R / R→L vs **5.16 px** for the factory profile |
| triangulation_quality | **PASS** | plane RMS 0.075 mm, scale error +0.05 % |
| repeatability | **PASS** | baseline spread 0.46 mm, rotation 0.066° |
| numerical_stability | **PASS** | — |
| stereo_consistency | FAIL | baseline error 0.62 mm vs 0.50 limit |
| pose_diversity | FAIL | 17 views, 305 mm, 74°, 3 of 4 roll bins |
| observability | FAIL | 3 constrained / 19 weak / 17 unobservable; right.ry 0.231° vs 0.20 |
| model_sensitivity | FAIL | freeing distortion makes held-out **worse by 4.1 %** (0.391 → 0.407 px) |
| overall | **REJECTED** | activation refused; profile store byte-identical |

This is the intended behaviour: the vendor data genuinely cannot support a
full three-camera recalibration, and the wizard says so instead of shipping it.

## 4. LIVE — physical scanner, 25 s

```
tools/wizard/gui/app.py --source live --session-dir <private-data>/live-wizard2 \
                      --mode standard --headless --duration 25
```

* Device opened, three-camera live preview with **40/40 markers** in both IR
  cameras, plate pose and quality updated continuously.
* Guidance produced "Hold still for a moment (moving at 7.2 mm/s)" from the
  measured motion, then moved on to pose-family targets.
* **6 accepted**, 48 rejected (mostly "too similar" — the platform sweep), 2/8
  coverage, NOT READY with the four missing pose families.
* Capture: 242 groups written, ring 120 on disk, 122 evicted, **0 dropped**,
  10.0 fps, stop reason `control:stop`.
* After exit: trigger counts 0/0 and illuminator 0 % (verified by a read-only
  probe) — the scanner is never left triggering or projecting.
* Earlier 45 s run with the default 30 s safety timeout exposed a real bug
  (`stop_reason: error` at 30 s); fixed by not passing the fixed timeout when a
  control file governs the session.

## 5. Behavioural findings that shaped the code

1. **Bootstrap deadlock (fixed).** Requiring bootstrap views to be mutually
   diverse (the engine's 8° / 40 mm rule) blocks the first solve forever on a
   continuous capture: 2 accepted views, then everything rejected, **zero
   solves**.  The wizard now seeds the first views and rejects only genuine
   duplicates, using the same separation rule as normal acceptance.
2. **Motion gate vs still collections (fixed).** The engine's "moving" gate
   rejected 15 of 25 vendor views because consecutive *independent* stills are
   far apart.  Sources now declare `continuous`; the gateway is disabled for
   still collections only.
3. **Wizard session must never be keyed on wall time alone.** Replays produce
   identical decisions for identical frames (manifest hash equality is tested).
4. **`lifecycle.validate` provenance crash (fixed).** The factory profile stores
   `provenance` as a list of `key=value` strings while the wizard candidate
   stores a mapping; the validator assumed the mapping and crashed, silently
   reporting "not available".
5. **Fixed ≠ constrained (reporting).** Parameters that are not estimated are
   labelled `fixed`, not `constrained`.

## 6. LIMIT — the synthetic renderer cannot yet rehearse a full session

The synthetic scene generator (`sources.SyntheticSource`) renders the plate by
projection and is deterministic, but at several poses the *right* camera view is
rejected by the orientation witness (`det(H) < 0`, "mirrored marker geometry"),
and wide lateral offsets push the plate out of frame.  The best observed
rehearsal was 12/16 accepted with 5/8 coverage.

Consequences:

* the readiness gate is verified in **both** directions at unit level
  (`tests/test_wizard_coverage.py` asserts `ready=True` for a constructed
  full-coverage view set and `ready=False` for the sweep), but
* a synthetic *end-to-end success* (session → READY → validated candidate) is
  **not demonstrated**; the vendor corpus exercises the full pipeline only up
  to an honest validation failure.

This is a rehearsal-instrument limitation, not a gate limitation, and it is the
main reason the next step is a real session rather than more synthetic work.

## 7. Performance (measured)

| action | cost |
|---|---|
| `coverage.measure` per snapshot (≤20 views) | ~1–3 ms |
| `observability.marginal_sigmas` | 0.30 ms median |
| `observability.identifiability` | 0.97 ms median |
| `observability.observability_gate` | 2.5 ms median (max 4.0) |
| detection + pose per live group | ~90–120 ms (the dominant cost) |
| GUI refresh | 10 Hz, < 5 ms |
| final solve (17 views, 3 cameras) | ~12 s |
| `model_sensitivity_analysis` at finish (25 views) | ~14 s |

## 8. Test status

* `driver` ctest: **13/13** (includes the new `test_capture_session`: 6 tests /
  134 checks for control file, ring eviction, status schema and signal flags).
* `tests/test_wizard_*`: coverage 18, observability 15, lifecycle 13,
  session_io 11, GUI 6 — all pass.
* Full Python suite: see the final report; the two bootstrap-sensitive tests
  were updated when the deadlocking rule was removed.

## 9. What is still required before the real session

1. A **real operator session** with the coverage families the sweep cannot
   provide (this is the next step, not a code gap).
2. Ideally an external length artefact so absolute scale can be labelled rather
   than assumed.
3. A colour check (chart or known-colour target) before colour registration is
   enabled in a production profile.
4. The synthetic renderer fix (right-camera chirality, wide lateral poses) if
   synthetic end-to-end rehearsals are wanted in future.
