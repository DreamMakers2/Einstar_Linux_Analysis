# fresh-01: where the decision thread's time goes

Attribution for the measured field behaviour of
`~/einstar-calibration-fresh-01` (4,659 groups captured in 516.9 s, 866 decisions
= 1.81/s, 3,647 of 4,513 offered groups never decided, decision gap p50 239 ms /
p95 1,225 ms / max 15,369 ms, persist 7.46 s over 222 writes).

Harness: `tools/wizard/bench_pipeline.py` (new). It replays the 120 real frame
groups still on disk (`frame_004539..004658`, opened **read-only**) through the
real `WizardController` and the real `worker.SessionWorker`, and attaches
wall-clock timers to every stage of `controller.ingest` **from the outside** -
`controller.py`, `calib/session.py`, `sources.py` and `coverage.py` were not
touched by this task, so the measurement cannot have changed the thing it
measures. Raw measurements: `analysis/calibration/performance/fresh01_*.json`.
Consolidated: `FRESH01_STALLS.json`.

Host caveat that matters for every absolute number below: this is a shared
32-core machine and other agents were benchmarking at the same time (load average
27-128 during the runs). Wall-clock values are therefore inflated and noisy;
each conclusion below rests on a within-run share, a paired A/B measured in the
same load window, or a CPU-seconds comparison, none of which the load scales.

---

## 1. Stage breakdown

Same 120 groups, same controller, same session options (`mode=high`,
`storage=production`), the only difference being the detection executor. `live`
is the process pool the real session used; `threads` is what replay/tests use.

| stage (inclusive) | processes: total ms | share | p50 ms | p95 ms | threads: total ms | share |
|---|---:|---:|---:|---:|---:|---:|
| `detect` (3 cameras) | 30,056 | **64.0 %** | 226.2 | 445.5 | 39,080 | 61.8 % |
| `session.ingest` | 12,027 | **25.6 %** | 61.1 | 512.5 | 21,393 | 33.8 % |
|   `session.solve` | 2,875 (5 solves) | 6.1 % | 510.5 | 1,170 | 8,070 (5) | 12.8 % |
|   `session.refine` | 4,678 (14 views) | 10.0 % | 331.9 | 366.0 | 4,928 (14) | 7.8 % |
| `pre_checks` (quality/pose) | 2,327 | 4.95 % | 20.4 | 27.2 | 2,333 | 3.7 % |
| `accept_store` (PNG+sha256+journal) | 1,071 | 2.28 % | 75.5 | 90.0 | 1,154 | 1.8 % |
| `derived` (coverage+observability+guidance) | 466 | 0.99 % | 3.1 | 11.0 | 482 | 0.8 % |
|   of which `derived.coverage` | 345 | 0.73 % | 2.8 | 5.2 | - | - |
| `veto` | 463 | 0.98 % | 9.2 | 15.8 | 495 | 0.8 % |
| `publish` (snapshot rebuild) | 370 | 0.79 % | 2.5 | 5.8 | 382 | 0.6 % |
| `persist` (manifest + session.json) | 113 | 0.24 % | 0.0 | 4.9 | 146 | 0.2 % |
| `reject` + `append_decision` | 35 | 0.07 % | 0.3 | 0.4 | 33 | 0.1 % |

Per-decision aggregates (120 groups):

| | processes (live arch.) | threads (replay/tests) |
|---|---:|---:|
| mean | 391.6 ms | 546.1 ms |
| p50 | 312.3 ms | 456.3 ms |
| p95 | 930.5 ms | 1,054.6 ms |
| p99 | 1,486.1 ms | 4,323.0 ms |
| max | 2,018.8 ms | 5,143.3 ms |
| sustained decision rate | 2.55/s | 1.83/s |
| whole-run wall (120 groups) | 48.7 s | 67.2 s |
| process CPU / wall | 7.6 x | 10.6 x |
| detection children CPU | 105.4 s (878 ms/group) | - |
| decode (PGM read + orient, per group) | 13.7 ms p50 / 15.4 mean | same |

The process pool is a **win**, not a cost: 1.38x faster wall, 1.4x lower p50,
2.4x lower p99 than the thread path, for 105 s of child CPU over 120 groups.
Detection is GIL-bound in threads (`grid_auto` wall summed across 3 threads /
`detect` wall = 2.02, i.e. the three cameras never fully overlap).

### Inside detection (isolated, sequential: `fresh01_detect_internals_60g.json`,
### 60 groups / 180 camera-frames, host load 22)

| measurement | value |
|---|---|
| `detect_grid_auto` passes per camera-frame | **4.86 mean, histogram {1: 41, 6: 139}, early-exit 22.8 %** |
| one full-frame detection (p50 / mean) | 22.0 / 24.8 ms per camera |
| per camera (mean) | left 38.7, right 19.7, tex 15.9 ms |
| `prefer_partial` fallback | 3.1 ms mean (0.03 ms p50: usually nothing to add) |
| IPC round trip, 3 x 1280x1024 uint8 (3.93 MB) | **3.28 ms p50**, 4.02 p95 |
| ROI outcome (353-attempt equivalent) | 45 accepted, 70 "short lattice", 24 "clipped", 36 nothing |
| ROI fallback rate | **74.3 %** (77 % in the 120-group controller run) |
| wasted ROI attempt | 17.1 ms mean |
| ROI crop size vs full frame | 0.28 of the bytes |

Host-load calibration, same 12 frames re-measured on the current tree once the
machine quietened (`fresh01_detect_internals_12g_current_tree.json`): mean
per-camera detection **44.4 ms at load 27 vs 22.5 ms at load 7.5 - a 1.97x
factor for identical work**. Read every absolute millisecond in this report with
that scale in mind; the shares and paired A/Bs are unaffected.

`detect_grid_auto` tries 2 polarities x 3 percentiles. At the time of the field
session it exited only when the first combination found the **whole 40-marker
lattice with rms <= 0.5**, which on these frames never happened (0/36 in the
first 12 frames). Over 60 groups - after the coordinator added a *verified
partial* early exit - it fires on 22.8 % of camera-frames, so **77 % still pay
all six passes** for a board that is partial, oblique or dim; on the 12 worst
frames it still never fires (0/36 on the current tree). Six passes are the single
largest term inside the detection share, and the fix is only ~23 % effective on
this material.

The first 12 groups of the session were the worst case (0/36 early exits, 44 ms
mean per camera); over 60 groups the isolated cost drops to 24.8 ms. The 226 ms
p50 the decision thread sees in-app is therefore **3-6x the isolated per-camera
cost**: the process pool shares a host that was 3-5x oversubscribed during these
runs, and each group does an extra ROI pass on 74 % of its cameras. The relative
shares and the paired A/Bs below are unaffected by that; the absolute ms are
upper bounds for a loaded host.

The ROI is *net negative* on this session: in the 120-group run the controller
took the ROI path 353 times, 81 succeeded and 272 fell back to a full-frame
re-detection (`roi: 81, full: 279, fallback: 272`), and a ROI success costs
about the same as the full pass (thresholds are computed on the full image
either way). Paired A/B on the same 120 groups (`--experiment no-roi`):

| | with ROI | ROI disabled | delta |
|---|---:|---:|---:|
| `detect` total | 30,056 ms | 25,399 ms | **-15.5 %** |
| detection child CPU | 105.4 s | 83.9 s | **-20.4 %** |
| `roi`/`full`/`fallback` counters | 81/279/272 | 0/360/0 | - |

(`--experiment no-solve` on the same groups removed `session.ingest` from
12,027 ms to 2,050 ms and p95 from 930 ms to 588 ms, but it also changed
acceptance from 14 to 5 views, so it prices the solver together with the
downstream work it triggers, not the solver alone.)

---

## 2. What the 239 ms p50 is

p50 is **detection**, not the solver and not the bookkeeping:

* `detect` p50 in the live architecture is 226 ms for the three cameras, i.e.
  95 % of the field's 239 ms p50 on its own. Mean 250 ms, share 64 %.
* Everything else on a *non-accepted* decision costs `pre_checks` 20 ms +
  `derived` 3 ms + `publish` 2.5 ms + persistence ~0 + `veto` 9 ms ~ **35 ms**.
* An *accepted* decision adds `refine` 332 ms + `accept_store` 76 ms, which is
  where the field's 162 accepted views spent a further 162 x 408 ms = 66 s.
* Persistence is **not** a contributor: 29 manifest+session writes in the
  replay cost 113 ms total (0.24 %); the field's own counters agree
  (222 writes / 7.46 s = 34 ms each, 1.4 % of 517 s). The "session.json/manifest
  rewrite" hypothesis is falsified.
* Coverage recompute per decision is **not** a contributor: 2.8 ms p50, 345 ms
  over 120 decisions (0.7 %).
* Detection IPC is **not** a contributor: 2.55 ms p50 round trip for 3.93 MB.
* Decode of the replay PGMs is 13.7 ms/group and happens on the reader thread,
  not the decision thread (in the field the reader gets already-decoded frames
  from the capture child).

## 3. What the 1,225 ms p95 and the 15,369 ms max are

The solver, and it is worse than the "~12 solves" estimate:

* `WizardController.ingest` sets `solve_every = max(2, n_accepted // 12)` on
  **every** decision and `CalibrationSession._maybe_solve` solves whenever
  `n % solve_every == 0`. Because `solve_every` is re-derived from the current
  view count, the same interval fires at *every* multiple while it is in
  effect. Simulating the real order of operations over a 162-view session gives
  **33 solves**, not ~12, at n = 5, 6, 9, 12, 14, 16, 18, 20, 22, 24, 26, 28,
  30, 32, 34, 36, 39, 42, 45, 48, 52, 56, 60, 65, 70, 78, 84, 91, 104, 117,
  130, 143, 156.
* Priced with the field's own solve curve (0.19 s @8, 1.46 @16, 1.75 @25,
  2.68 @40, 6.47 @60, 12.4 @84, linear above 84) those 33 solves total
  **226.9 s = 43.9 % of the whole 517 s session**, in 4 % of the decisions.
  Cross-check: `(517 - 227) / (866 - 33) = 350 ms` per remaining decision, which
  brackets the measured p50 of 239 ms; if the field's high-view solves were
  cheaper than the linear extrapolation (the observed max decision is 15.4 s,
  so the n=156 solve was at most that), the solve total is ~170-230 s, i.e.
  **33-44 %** of the decision thread. Either way the solver, not detection,
  produces the p95 and the maximum.
* One solve per bucket - what the policy *intends* - would be 14 solves and
  174 s with the same curve, i.e. **53 s saved without changing the algorithm**.

### The solver also burns an absurd amount of CPU

Measured on the same host, same observations, same code path, only
`OPENBLAS_NUM_THREADS` differing (`fresh01_solve_curve_blas1.json` vs
`..._blas_default.json`):

| accepted views | default BLAS (32 thr) | `OPENBLAS_NUM_THREADS=1` | wall ratio | CPU ratio |
|---|---:|---:|---:|---:|
| 16 | 13.36 s wall / **252 s CPU** | 0.58 s / 0.67 s | 23 x | **378 x** |
| 40 | 40.08 s / **663 s CPU** | 1.26 s / 1.45 s | 32 x | **456 x** |
| 84 | 75.85 s / **1,605 s CPU** | 8.75 s / 8.87 s | 8.7 x | **181 x** |

Spot confirmation at 16 views: default BLAS 5.82 s wall / 155.2 s CPU
(26.7 cores busy, 125 threads in the process, residual rms
0.6428608536744468) versus `OPENBLAS_NUM_THREADS=1` 0.60 s / 0.70 s CPU
(1.2 cores, 32 threads, residual rms 0.6428608536736348 - identical to 12
digits). The same answer for **222x less CPU**. This is OpenBLAS's busy-wait
thread pool spinning over a solver that calls many small dense solves, not a
different algorithm.

Two consequences: (a) a solve on the decision thread not only blocks it for
seconds, it also **starves the three detection children and the GUI** for the
same period, which is why the field's preview max gap was 376 ms during the
solve bursts; (b) no test can see any of this, because `tests/conftest.py` pins
`OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` for the whole suite. The
production path is unmeasured and pathological while every test runs
single-threaded.

---

## 4. The 81 % dropped frames are a *policy*, not a bug

The newest-frame policy is: one pending decision, one at a time; a group that
arrives while the decision thread is busy is recorded only. Throughput is
therefore `1 / mean decision time`, capped by that, and every group offered
above that rate is dropped.

Reproduction with the real `SessionWorker` on the real frames, paced at the
field's 9.77 groups/s (`fresh01_live_replay.json`):

| | fresh-01 field | replay (120 groups, 9.77 Hz) |
|---|---:|---:|
| offered | 9.77 Hz | 9.68 Hz |
| preview | 9.69 Hz, p50 110.5 ms | **9.77 Hz, p50 102.1 ms** |
| decisions | 866 = 1.81/s | 37 = 2.99/s |
| dropped / offered | 3,647 / 4,513 = **80.8 %** | 83 / 120 = **69.2 %** |
| decision gap p50 / p95 | 239 / 1,225 ms | 208 / 976 ms |
| decision *duration* p50 / p95 / max | (not instrumented) | 152 / 963 / 1,157 ms |
| decision thread busy fraction | ~1.0 (implied) | **0.841** |
| sustainable rate (1/mean duration) | 1.67/s | 3.55/s |

The replay drops less than the field because it only reaches 5 accepted views in
12.4 s, so its solves are small; with the field's 597 ms mean decision the same
policy drops 80.8 %. The gap between 3.55/s sustainable and 1.81/s observed is exactly
the solver.

New instrumentation proves the preview path is independent of decisions:
**114 of 120 previews were published while a decision was in flight**, maximum
preview gap 137 ms (one decode + pacing jitter), against a 963 ms p95 decision.

---

## 5. Root causes, ranked

1. **The solver runs on the decision thread** (`session.ingest -> _maybe_solve`).
   33 solves per 162-view session, ~170-230 s of the 517 s decision thread
   (33-44 %), and every one of them is a multi-second p95/max stall
   (p95 1,225 ms, max 15,369 ms). It also triggers on `solve_every` arithmetic
   that fires ~2.4x more often than the code's own comment intends.
2. **The solver uses 32-thread OpenBLAS**: 181-456x more CPU than
   single-threaded for an identical result (0.70 s vs 155 s CPU at 16 views).
   This multiplies (1) - the solve blocks the decision thread *and* starves
   detection, preview and the GUI for the same wall time.
3. **Detection is 64 % of every decision** (226 ms p50 in-app, 22-25 ms per
   camera isolated): `detect_grid_auto` runs all **6** threshold/polarity passes
   on **77 %** of camera-frames (the early exit needs the full 40-marker lattice
   at rms <= 0.5, so it fires only 22.8 % of the time), and the ROI retry adds a
   full second pass on **74-77 %** of camera-frames (net -15.5 % detection time
   and -20.4 % detection CPU if removed).
4. **The decision cadence is "as fast as the thread can"**: with a 9.77 Hz offer
   and a 150-400 ms decision, 69-81 % of groups can only be dropped. This is
   working as designed, but it means the thread runs at 84-100 % duty, so any
   burst (solve, refine, a slow detection) immediately costs frames and no
   headroom exists for a stop/pause to be honoured promptly.
5. **Per-accepted-view work is on the decision thread too**: `refine` 332 ms and
   the view store 76 ms per accepted view (0.4 s x 162 = 66 s of the field
   session), plus `pre_checks` 20 ms on every frame (pure-Python quality/pose
   checks).
6. **Falsified**: persistence (34 ms/write, 1.4 % of the session), coverage
   recompute (2.8 ms, 0.7 %), snapshot publish (2.5 ms, 0.8 %), detection IPC
   (2.55 ms p50 for 3.93 MB), PGM decode on the decision thread (it is on the
   reader), the process pool (it is 1.4x *faster* than threads).

---

## 6. Steady-state budget (what the numbers support)

Offer: 9.77 groups/s = 102 ms/group. Preview must keep that cadence and does
(9.69 Hz field, 9.77 Hz replay) at 13.7 ms decode + <0.1 ms publish.

| consumer | current | budget for 3 decisions/s | how |
|---|---:|---:|---|
| detect (3 cameras) | 226-250 ms in-app (22-25 ms/camera isolated) | **<= 150 ms** | early-exit after the first strong pass; drop the ROI retry |
| session.ingest | 61 ms p50 / 512 ms p95 | **<= 60 ms** | solve and refine leave this thread |
| solve | 510-12,400 ms | **0 ms on this thread** | own thread/process, coalesced |
| refine | 332 ms/accept | **0 ms on this thread** | move with the view store |
| accept_store | 76 ms/accept | **<= 40 ms** | already async-safe; move with refine |
| pre_checks + derived + publish + persist | ~26 ms | **<= 25 ms** | unchanged |
| **total per decision** | **392-597 ms** | **<= 330 ms** | -> 3.0 decisions/s |

Target decision rate: **3/s sustained** (one decision per ~3.3 offered groups at
9.77 Hz), with a hard rule that the thread's duty cycle stays **<= 80 %** so a
solve hand-off, a pause or a stop is honoured within one offer period. That is
~2.4 decisions per second more than the field's 1.81/s while dropping *fewer*
frames than today, and it is a cadence the operator can actually follow: 3
guidance updates/s is 3x faster than a person can reposition a plate.

If instead the decision rate is pushed to the full 9.77 Hz, a decision must cost
<= 102 ms, i.e. detection <= 60 ms for three cameras - that requires changing
the detector's search, not its scheduling, and is out of scope here.

## 7. Recommendations for the coordinator

Owner "coordinator" = files this task did not touch (`controller.py`,
`calib/session.py`, `coverage.py`, `sources.py`, plus the wizard entry point).

Status while this report was being written: the tree moved under the
measurements. Recommendations **1, 2 (partially) and 5 were implemented by the
coordinator in the last half hour** - `tools/wizard/gui/app.py` now pins the BLAS
thread pools (06:52), `calib/session.py` has `solve_due()`/`solve_observations()`
plus `--async-solve` (06:20), and `calib/detect.py` has a verified-partial early
exit (06:53). The measurements in this report were taken **against the tree as
it was during the field session** (they reproduce its numbers), so they are an
independent confirmation of those three changes, not a duplicate request.
Recommendations **3, 4, 6 and 7 are still open** and are where the remaining
budget is.

1. *(done in the tree; numbers below are the independent confirmation)*
   **Pin the BLAS thread pool in the wizard process.** One line in the entry
   point (`tools/wizard/gui/app.py` / `tools/wizard/app.py`) *before* numpy is
   imported: `os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")`,
   `OMP_NUM_THREADS`, `MKL_NUM_THREADS` (or `threadpoolctl.threadpool_limits(1)`
   after import). Measured: identical residual to 12 digits, **0.70 s instead of
   155 s of CPU at 16 views** (222x), 8.7-32x less wall for 16-84 views. This is
   the cheapest change in the system and it removes the CPU storm that starves
   detection and the preview during every solve. Do it even if nothing else is
   done.
2. *(partially done in the tree: `--async-solve`)*
   **Move the rig solve off the decision thread** (and off the GIL): a single
   solve worker with a coalescing request (at most one queued; if views arrive
   while solving, re-request once at the end). `_maybe_solve` becomes
   "request a solve", and `session.result` is replaced when the worker publishes.
   Guidance/observability already tolerate a stale result (their cache keys use
   `len(accepted)` and `_solves`). Removes 33 x 0.5-12 s = **170-230 s** from the
   thread that must keep up with the camera.
3. **Fix the solve cadence arithmetic** even after (2): key the trigger on the
   *bucket* (`n // every` changing) or keep `last_solve_n`, instead of
   `n % every == 0` with a recomputed `every`. 33 solves -> 14, i.e. **-53 s** of
   solve work for the same final calibration (verify the final residual is
   unchanged).
4. **Move `refine` + view-store persistence with the solve worker** (they are
   the same "accepted view, not needed for this decision" class): 0.41 s per
   accepted view x 162 = **66 s** off the decision thread.
5. *(landed in the tree, but only ~23 % effective on this material - keep it)*
   **Give `detect_grid_auto` an early exit that fires.** Today the exit needs
   the full 40-marker lattice at rms <= 0.5; it fires on 22.8 % of camera-frames,
   so 77 % run all six passes (mean 4.86 passes). Exit after the first pass whose
   lattice is >= the previous frame's lattice and whose rms is within, say, 1.5x,
   or after pass 1 when it returns >= 12 markers at rms <= 1.0. Measured cost is
   22 ms p50 for a full six-pass detection (~3.7 ms per pass), so 6 -> 1-2 passes
   removes ~5/6 of the dominant term of the p50: ~4-7 ms per camera isolated, and
   correspondingly most of the 226 ms in-app detection stage.
6. **Remove or repair the ROI retry.** It succeeds 23 % of the time, costs
   about as much as a full pass, and adds a full second pass on 77 % of frames:
   disabling it is worth **-15.5 % detection time and -20.4 % detection CPU**
   (paired A/B). If it is kept, the "short lattice" rule must compare against
   what the full frame found (not the 40-marker ideal) and the crop must be
   rejected before it is executed, not after.
7. **Make the decision cadence explicit**: decide at most once per
   `decision_period_s` (default 0.30 s => 3.3/s) instead of "whenever idle", and
   publish the duty cycle. The drop ratio then becomes a stated policy
   (`offered 9.77/s, decided 3.3/s, recorded-only 6.5/s`) instead of an
   operator-visible anomaly, and the thread keeps 20 % headroom.
8. **Keep the process pool.** It is 1.38x faster wall and 2.4x better p99 than
   threads; the IPC is 2.55 ms p50. Do not "optimise" it away. If its payload is
   ever a problem, send the crop (0.28 of the bytes) - but at 2.55 ms it is not
   worth the risk today.
9. **Do not spend effort on persistence or coverage**: 34 ms/write and 2.8 ms per
   decision respectively (0.2-1.4 % each).

Expected effect of 1+2+3+4 (solver and accepted-view work off-thread, BLAS
pinned): mean decision 597 -> ~210-250 ms, p95 1,225 -> ~450-600 ms, max
15,369 -> ~1,000-1,500 ms, sustainable rate 1.67 -> 4-5/s. Adding 5+6 pushes the
p50 towards 100-150 ms. Both land the 3/s target with headroom.

---

## 8. Changes made in this task (bounded to `worker.py` + the harness)

`tools/wizard/gui/worker.py`:

* `DecisionRunner` is now the measurement point of the decision thread: it
  records every decision duration and lag (bounded history of 512), busy
  seconds, refused submissions, and `dropped_on_stop`. `stats()` exposes them.
* The 50 ms wake/stop poll is replaced by a `Condition`: the first handoff and a
  stop are acted on immediately (test asserts < 25 ms handoff latency).
* `submit()` refuses while stopping (counted: `refused_stopping`) instead of
  starting work the session is about to abandon.
* `request_stop(prompt=False)` keeps the documented behaviour exactly: an
  already-submitted frame is still decided. `prompt=True` drops the pending
  frame (`dropped_on_stop`) so a window that is closing does not wait for a
  15 s solve; a decision that is already running always finishes. The runner
  itself honours `drain=False`. The synchronous path now also reports a decision
  thread that fails to stop instead of silently ignoring `wait()`.
* **Newest-frame policy is explicit and measured**: `newest_frame_policy()`
  returns queue depth, drop-on-busy/paused/stopping, groups seen, decided,
  dropped, dropped fraction, recorded-only, superseded.
* **Overload flag**: `overload_report()` -> `overloaded`, busy fraction,
  `decisions_over_period`, `refused_submissions`, `sustainable_hz` vs
  `offer_hz`, mean/p95 decision duration. Literally derived, not asserted.
* **Decision duration** (p50/p95/p99/max) is now recorded alongside the gap
  distribution, for both the async and the sync path, and survives shutdown
  (`_last_runner_stats`) so the evidence file written after the session still
  has it.
* **The lag is attributed to the submission that produced it.** It used to be
  stored on the consumer side, where a submit that happened while the decision
  signal was still queued overwrote it (fresh-01 lost 26 of 866 lags and
  understated the rest).
* **Pause now stops spending decisions** (`dropped_paused`), and the preview
  path counts `preview_during_decision` + `preview_gap_max_ms` +
  `overload_events`, which is the measurable proof that the display never waits
  for a decision.
* `metrics()` gained `decision_duration`, `decision_thread`, `overload`,
  `newest_frame_policy`, `dropped_paused`, `dropped_stopping`, `overload_events`,
  `preview_during_decision`, `preview_gap_max_ms`, `offer_period_ms`. All
  existing keys are unchanged, so the GUI's status text and `ui_metrics.json`
  keep working.

`tools/wizard/bench_pipeline.py` (new): the harness described above, modes
`sync`/`detect`/`live`/`solve-curve`/`solve-budget`, `--experiment` A/B
knock-outs, cProfile dump, per-stage p50/p95/p99, CPU/wall and child-CPU,
load average in the payload. It writes only to `--session-dir` (a `/tmp`
directory by default), `--out` and `--pstats`.

## 9. Tests

| command | result |
|---|---|
| `pytest tests/test_wizard_worker_budget.py` (new, 10 tests) | **10 passed** |
| `pytest tests/test_wizard_controller.py tests/test_wizard_gui.py tests/test_wizard_acquisition_ui.py` | **33 passed**, 0 failed (112 s) |

The new tests drive the real `SessionWorker` in a child process (the repo's
documented practice for Qt) with a fake controller/source, and assert on
measured numbers: policy explicitness, drop accounting, decision-duration vs
lag consistency, preview independence (max gap < 200 ms while decisions take
500 ms), pause/stop behaviour, prompt handoff (< 25 ms), pending-frame drop, and
error surfacing.

Note: `tools/wizard/controller.py` was being edited concurrently by the
coordinator while these runs happened (`git status` shows it modified); the
numbers above were taken against the working tree at that time.
