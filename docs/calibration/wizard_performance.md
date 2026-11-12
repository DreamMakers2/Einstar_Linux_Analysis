# Wizard performance — where the time goes

Measured on the recorded sweep (`sweep-b`, 350 groups) through the real
controller and window.  Every number is from a run, not an estimate.

## Per-frame cost of the decision path

| stage | serial | after ROI + histogram thresholds |
|---|---|---|
| detect (3 cameras) | 172 ms | **93 ms** |
| pre_checks (pose, quality) | 21 ms | 25 ms |
| solve (amortised, 8 per 200 groups) | 1.4 ms | 1.4 ms |
| materialise (disk) | 0.8 ms | 0.8 ms |
| publish (snapshot rebuild) | 1.1 ms | 1.3 ms |
| **frame** | **230 ms** | **158 ms** |

The live stream offers one group every 100 ms, so the decision path alone cannot
keep pace.  Two exact optimisations were taken and one tempting one rejected:

* **ROI detection** - the detector runs on a crop around the previous plate
  position, taking its threshold from the *full* frame and shifting coordinates
  back, so it is exactly equivalent (max marker difference 1.1e-13 px); a
  detection within 8 px of the crop border is redone full-frame, and 98 % are
  served by ROI.
* **One-pass thresholds** - the auto search needs six thresholds per camera
  (three percentiles x two polarities); a single 256-bin histogram gives the same
  interpolated quantiles in 2.0 ms instead of 12.9 ms (6.6x), including the
  inverted polarity as `255 - quantile(100 - p)`.
* **rejected: early exit** - returning the first successful combination when it
  found the full lattice reached 103 ms/frame but moved poses by up to **0.67 mm**
  and changed which views were accepted.  Not shipped.

## Acquisition loop cost

| item | cost |
|---|---|
| iterate one group from the replay/live directory | **13.3 ms** |
| of which `read_pgm` x3 | 1.8 ms |
| of which debayer + grey of the colour frame | **11.1 ms** |
| frame I/O and orientation in the decision path | ~1 ms |

The colour conversion happens in the *source*, so it is paid for every group even
when the group is skipped by the scheduler.  Deferring it into the decision path
is the next measured optimisation (saves ~11 ms/group of acquisition time; it
does not change decisions because skipped groups need no colour at all).

## Concurrency

| configuration | decisions/s | groups consumed/s (10 arrive) |
|---|---|---|
| inline decisions, unpaced | 6.3 | source-limited |
| inline + adaptive spacing | 1.6 | 6.8 |
| decision thread, bounded queue (opt-in) | **3.5** | **7.0** |

The decision thread removes the blocking, but acquisition is still short of the
stream because of its own 13 ms/group cost plus GIL contention with the decision
and GUI threads.  The scheduler is opt-in (`min_decision_interval_s > 0`):
enabling it by default changed which frames are analysed and the vendor-corpus
scenario dropped from 5 accepted views to 4.

## Memory

Python heap flat: tracemalloc 11.5 -> 12.6 MB over 200 groups, all controller
state under 0.4 MB.  RSS grows to ~300 MB (controller) / ~575 MB (window) and
plateaus — native allocator caching, not a Python leak.

## Crash

Closing the window during a solver pause used to destroy a running `QThread`
after `worker.wait(5000)` timed out, which aborts the process.  The close is now
deferred until the worker ends (`closeEvent` refuses, retries every 200 ms, and
the window stays visible); measured close time 0.00 s with the solve still
running, clean exit afterwards.


## Six-minute soak (2x the failed session)

A looped 10 Hz replay into the real window, decision thread and adaptive spacing
enabled, `MemoryMax=6G CPUQuota=300%`, sampled every 30 s:

```
t= 30s  rss 361 MB  decisions 3.40/s  groups 6.87/s  refresh p95 6.5 ms
t=120s  rss 396 MB  decisions 3.93/s  groups 7.90/s  refresh p95 8.9 ms
t=240s  rss 418 MB  decisions 3.77/s  groups 7.50/s  refresh p95 8.6 ms
t=360s  rss 435 MB  decisions 3.83/s  groups 7.70/s  refresh p95 8.2 ms
shutdown 0.04 s, final rss 441 MB
```

No crash, no progressive degradation (decisions and refresh latency are flat while
RSS slows to a plateau), and shutdown after six minutes is immediate.  The accepted
count stops at 16 because the loop replays the same 350 frames: once the poses are
covered nothing new is accepted, which is the duplicate gate doing its job.

Degradation over time was the reported symptom; the same measurement now shows
none.

## Failure-mode behaviour (tested)

| failure | behaviour |
|---|---|
| disk write fails | every frame is vetoed with `io: cannot materialise the frame triplets`; the session stays usable (test) |
| solver raises | the error is recorded in `last_solve_error`, the session continues without a result (test) |
| window closed during a long solve | close deferred, window visible, worker joined, clean exit (test) |
| crash mid-session | autosave block resumes the session; final calibration identical to an uninterrupted run (test) |
| capture stops producing | source drains and ends; the header reports the stall rather than claiming RECORDING (test) |

## Live scanner runs (short, capped)

Three short capped runs on the physical scanner, after the offline checklist:

| run | outcome |
|---|---|
| 30 groups, 8 s | 4 accepted, 15 rejected, device left safe |
| 25 s + Finish | 4 accepted, 84 rejected, candidate written, validation refused, factory sha unchanged |
| 20 s + Finish | 10 accepted, 49 rejected, state `done`, validation refused |

They found two defects that no replay run could reach:

1. **Every live run was labelled `replay`.**  `make_controller` passed the source
   kind only to the demo controller, and the controller's empty-snapshot
   template hard-coded an empty source, so the header, acquisition panel and
   readiness footer all claimed replay.
2. **The controller never saw the capture process** (it belongs to the
   `LiveSource`), so it reported `fps 0 · idle · 0 groups` and the stall rule
   fired permanently: `RECORDING · STALLED` while frames arrived at 9.6 fps.

Both are fixed and tested.  Measured after the fix, during a live run:
`live · 9.9 fps · 4.6 decisions/s`, state running, no false stall.

This is why the brief asks for a short live test before trusting the wizard: the
replay path has no capture process, so it cannot exercise the status plumbing
that the operator actually watches.

## GUI behaviour under load

| element | rule |
|---|---|
| camera tiles | 10 Hz refresh, images come from the last decision (no re-detection) |
| 3-D preview | 10 Hz, lengthened to 5 Hz when the measured decision rate drops below 2/s |
| instruction | one line, font scaled to its length |
| feedback | 1.8 s after each decision, plain language, unit-tested |
| readiness | a verdict with named gaps, never a bare percentage |
| diagnostics | collapsed by default |
