# Acquisition state machine and operator stop contract

Status: implemented 2026-11-13 for the 52 GB session regression.  Evidence:
[`analysis/calibration/lifecycle/`](../../analysis/calibration/lifecycle/).

This document is the authoritative description of **when the wizard may say the
scanner is stopped**, how the GUI derives that claim, and what the operator is
told when coverage stops improving.  No physical calibration session was used;
the evidence comes from the recorded session and from real Linux parent/child
tests with the simulated transport.

## 1. The observed contradiction

The recorded session `einstar-calibration-03` (22 min, 13,195 groups, 9.8 fps)
ended with:

| evidence | value |
|---|---|
| GUI pill | `SOURCE ENDED` |
| Advanced diagnostics | capture state `running`, 9.8 fps, laser 100 %, > 12,000 groups |
| `status.json` | state `stopping`, `stop_reason control:stop`, cleanup attempted/verified true, trigger 0/0, laser 0 |
| `capture.stderr.log` | `process safety: cleanup deadline exceeded; illuminator state UNVERIFIED; forcing SIGKILL` |
| `frames/frames_manifest.json` | **absent** |
| `raw.e1r` | 51,977,409,627 B; CRC-valid trailer present (1,266,761 transfers) |
| decisions | 2,004 of 13,195 groups evaluated; last decision 17:26:13.9 |
| `ui_metrics.json` | written 17:29:14.6 (≈180 s later) |

### Exact code path

1. The operator stopped the **reader/session** at 17:26:14.  The GUI's
   `_on_cancel()` called `WizardController.cancel()`, which set
   `state="cancelled"`, wrote `session.json` and returned.  It did **not** call
   `self.capture.stop()`; `LiveCaptureProcess` stayed alive with the laser on.
   The `SessionWorker` had ended its reader loop, so `_on_worker_done()` set
   `_source_ended=True` and applied the `ended` display state.  That is the
   `SOURCE ENDED` pill.
2. The capture child kept recording for 180 s and added 2,404 unevaluated
   groups (last decided group 10,790; last recorded group 13,194; 13,195 total).  The Advanced diagnostics tab was reading the live
   `status.json` through `controller.snapshot()`, so it correctly showed
   `running`, 9.8 fps, laser 100 %, 12k+ groups.  The two labels described
   different processes: the reader had stopped, the scanner had not.
3. On window close, `shutdown()` wrote `ui_metrics.json` **before**
   `source.close()`; `LiveSource.close()` then called `capture.stop()`, which
   finally wrote `control.txt = stop` at 17:29.  The C++ teardown verified the
   device readback and wrote the `.e1r` trailer, but then spent longer than the
   process-safety watchdog's 5 s budget re-reading/verifying the 52 GB
   recording.  The watchdog logged the deadline-exceeded line and SIGKILLed
   the child.  The final status remained `stopping` and
   `frames_manifest.json` was never written.
4. The old `check_safe_final_state()` only required `state == "stopped"` and
   treated `cleanup_verified` as sufficient; the old diagnostics had no
   acquisition state at all, and `_capture_dict()` read `cleanup_verified` from
   `status()` even though `status()` never exposed it (always `None`).

**Root cause:** the reader/UI lifecycle and the scanner lifecycle were
separate, unowned transitions.  A stopped reader was allowed to imply a
stopped scanner, and a verified device cleanup was allowed to imply a
finalized recording.

## 2. One authoritative state machine

`tools/wizard/capture.py` owns the machine.  `LiveCaptureProcess.acquisition_state()`
returns an `AcquisitionEvidence` dictionary (`schema
einstar.acquisition-state/1`); `WizardController.snapshot()["capture"]["acquisition"]`
is the only value the GUI/ diagnostics may present as the scanner state.

```
                    start()
      idle ─────────────────────► starting ───► running ◄──► paused
        ▲                            │             │           │
        │ no process/status          │ exit        │ stop      │ stop
        │                            ▼             ▼           ▼
        └────────────────────────── failed       stopping ─────┘
                                                    │
                       child exit + device readback + recording finalized
                                    ┌───────────────┴───────────────┐
                                    ▼                               ▼
                            stopped_verified                stopping_unverified
                                 (safe)                    (explicit missing evidence)
```

`terminal` is true for `stopped_verified`, `stopping_unverified` and `failed`.
`safe` is true **only** for `stopped_verified`:

```
verified = child_terminated
        AND device_cleanup_verified
        AND recording_finalized
        AND no orphan capture process for this session
```

* **child termination** — `poll()` returned, or there is no live lock and no
  other capture process for this session.  The evidence records pid, exit code
  and exit signal (`SIGKILL` for the observed session).
* **device cleanup** — live backend: `cleanup_attempted` and
  `cleanup_verified` true, trigger readback exactly `mono=0,rgb=0`, laser
  readback `0 %`.  Simulator: successful simulated shutdown.  Missing fields
  fail closed; `cleanup_verified: true` alone is not enough.
* **recording finalization** — final status `stopped` **and**
  `frames/frames_manifest.json` present and valid.  A CRC-valid `.e1r` trailer
  is supporting evidence (present in the observed session), not a substitute:
  the parent reads only the 72-byte tail and cannot recompute the FNV-1a
  digest over 52 GB within the deadline.
* **orphans** — `/proc` is scanned (rate-limited, only once the child is gone)
  for another `einstar-capture-frames` with the same `--session-dir`.

Every non-verified terminal state lists the concrete missing evidence in
`reasons`.  `conflicts` records contradictions such as the watchdog SIGKILL
line next to a verified cleanup readback.  `evidence_notes` records what *is*
established (cleanup readback, trailer, exit code).

### Stop ordering (the actual fix)

`WizardController.stop_capture()` is the only place the controller terminates
the child.  `cancel()`, `finish()`, GUI close, worker end and `close()` all
route through it.  `cancel()` returns `cancelled` only when the verdict is
verified; otherwise it returns `cancelled_unverified` and keeps the data.
`SessionWorker` stops and verifies the supervised source in its `finally` block
before emitting `finished_ok`, so stopping the reader can no longer leave the
scanner recording.  `snapshot()` downgrades a `cancelled`/`done` state to
`error` if the acquisition evidence says the child is still running.

Parent-death (`PR_SET_PDEATHSIG`), the heartbeat pipe and the child watchdog are
unchanged.  `stop_and_verify()` and `check_safe_final_state()` use the machine's
verdict rather than the status file alone.

## 3. GUI and diagnostics

* The `SOURCE ENDED` label is gone.  A normal replay/synthetic reader end shows
  `RECORDING ENDED`; a live end is rendered from the acquisition verdict:
  * `stopped_verified` → `CAPTURE STOPPED · VERIFIED`, footer states the scanner
    is off and the recording finalized;
  * `stopping_unverified`/`failed` → `SCANNER STATE UNVERIFIED` plus the first
    concrete causes in the error banner, never a safe-stop claim;
  * child still running → `CAPTURE STILL RUNNING — the reader stopped, the
    scanner did not`, and a `StopWorker` stops/verifies it asynchronously.
* The Advanced diagnostics tab shows, independently: session/reader state,
  authoritative acquisition state, raw status state, verified/safe/terminal,
  child pid/exit/signal, stop request/reason/attempts, cleanup attempt/readback,
  trailer + manifest + finalization, decision accounting and guidance age.
* `ui_metrics.json` is written **after** the stop/verification and includes the
  acquisition document, progress and guidance, so it can no longer record a
  running scanner as if it were finished.

## 4. Guidance, progress and safe stopping

The coverage engine (`tools/wizard/coverage.py`) remains the authority for the
next pose.  The controller and window add progress evidence; they never lower
coverage thresholds.

* `_ProgressAdvisor` tracks accepted views, satisfied dimensions, visited image
  corners/regions and the guidance target.  It emits:
  * `stalled` after 25+ accepted views and 120 s / 60 views with no new
    dimension, corner or region;
  * `stop_recommended` after 60+ accepted views and 180 s / 90 views of stall —
    "stop recording and analyse offline; the raw recording and status are
    preserved";
  * `unreachable` when the same target has been requested ≥ 30 times over
    ≥ 75 s with no progress — "the viewpoint appears physically unreachable
    (heuristic, not a physical proof); reposition the scanner or stop and
    analyse offline".
* The window renders the **actual missing information** first: missing image
  corners ("Aim the plate at the image top-right and bottom-right"), then the
  coverage-engine target/priority.  A distance instruction is only allowed when
  D1 is unsatisfied and the priority is distance.  The old window shortened any
  target whose distance differed by >25 mm to "Move the plate farther away",
  which is why the operator was told to move farther while the right-hand
  corners were the missing evidence.
* Guidance carries `age_s`, `current`, `based_on_accepted`, `based_on_view_id`
  and `measurement_views`; the footer notes an ageing measurement during a long
  decision.
* A prominent stop banner plus `Stop & keep data` is shown for the
  stop/unreachable advice.  `cancel()` preserves `decisions.jsonl`, the session
  manifest, the raw recording and the final status in every case.

## 5. Performance / decision backlog

The observed 2,004/13,195 evaluated groups are the newest-frame policy: the
reader publishes every group for preview, but only one decision is in flight
and older queued frames are discarded (the raw recording keeps all of them).
The window/diagnostics now report `evaluated`, `preview_total`, `newest-frame
skips`, decision lag and the `recorded_only` count instead of leaving the gap
unexplained.

The avoidable part was persistence: every decision rewrote a 3.4 MB
`manifest.json` and a 3.5 MB `session.json`, with `_jsonable()` recursion,
`indent=1`, a full old-session read and a full manifest re-hash.  At 297 views
that measured **~0.5 s per decision**.  The fix:

* accepted views force a durable snapshot; reject-only traffic is debounced by
  `autosave_interval_s` (2 s default, 0 forces every decision as in tests);
* every lifecycle transition / `flush_progress()` forces a write;
* compact JSON, no recursive conversion on already-plain payloads, cached
  manifest digest and cached sibling-session headers.

Measured at the 297-view payload scale: manifest 169 → 21 ms, session
169 → 43 ms, bytes 3.1 → 1.05 MB (`persistence_benchmark.json`).  Estimated
write time over the observed 2,004 decisions: ~678 s → ~57 s.  `LiveSource`
now polls the ring at 30 ms instead of 100 ms; the measured 8.45 Hz preview
was partly a polling artifact.  USB 2.0 operation, decision cadence and the
3–4 s final solve are unchanged.

## 6. Test matrix

| Scenario | Test |
|---|---|
| normal stop with all evidence | `test_capture_acquisition_state.py::test_normal_stop_is_verified_only_with_all_three_evidence_groups`, `test_real_simulated_stop_is_verified_end_to_end` |
| stopped status but missing manifest | `test_stopped_without_frames_manifest_is_unverified` |
| observed 52 GB sequence | `test_observed_52gb_sequence_is_never_reported_safe` |
| stale `running` status, dead child | `test_stale_running_status_with_dead_child_is_not_running` |
| live child / non-terminal UI | `test_live_child_keeps_running_state_and_terminal_false` |
| orphan capture process | `test_orphan_capture_process_for_the_same_session_is_evidence` |
| trailer parser / truncation | `test_trailer_parser_rejects_truncation` |
| source exhaustion | `test_source_exhaustion_reaches_a_verified_final_verdict` |
| cancel stops/verifies fake child | `test_wizard_lifecycle_stop.py::test_cancel_stops_and_verifies_a_live_capture` |
| cancel unverified | `test_cancel_without_verified_stop_is_explicitly_unverified` |
| running capture keeps session non-terminal | `test_live_capture_keeps_a_cancelled_session_non_terminal` |
| debounced/forced persistence | `test_persistence_is_debounced_but_transitions_force_a_flush` |
| recorded D1-D8 / D7 stream / D4-only | `test_wizard_lifecycle_coverage_wiring.py` |
| rejected dwell increments D7 | `test_rejected_dwell_observation_increments_d7_not_accepted_views` |
| operator wording (no "farther" when D1 satisfied) | `test_wizard_guidance_view.py` |
| no-progress / unreachable / target change | `test_wizard_progress.py` |
| GUI reader-ended vs running/unverified/verified | `test_wizard_acquisition_ui.py` |
| real parent-child safety (unchanged) | `tests/test_capture_safety.py` (8 passed with the built binary) |

Final suite results are in
[`analysis/calibration/lifecycle/tests_results.txt`](../../analysis/calibration/lifecycle/tests_results.txt):
**120 passed, 1 failed** (259 s) with the built `build/driver` binary and the
real parent/child tests.  The single failure is
`test_observability_refuses_overparameterised_distortion`, which exercises
`calib.observability`/`calib.solvers` and is outside the files owned by this
change; it reproduced unchanged before/after this work and belongs to the
solver/observability owner.  GUI smoke (`tests_gui_results.txt`): 7 passed,
1 failed (`test_vendor_corpus_session_if_available`, missing-label wording from
the coverage engine, also outside this change); the Qt close/teardown test
passed.  The focused GUI-ready positive test still reports 25 accepted views
where its fixture expects ≥30, again from the coverage/guidance acceptance
policy rather than the lifecycle path; the truthful-unvalidated GUI-ready test
passes.

## 7. Remaining unverified hardware behaviour

* The parent verifies the `.e1r` trailer **record** and its declared byte count;
  it does not recompute the transfer digest.  The observed trailer byte count
  equals `status.bytes_received` (51,926,738,976), which is strong but not the
  C++ verifier's full proof.
* `cleanup_verified` is command success plus device register readback.  It says
  nothing about emitted optical power or hardware interlocks, and no scanner was
  used for this change.
* A child that is SIGKILLed after publishing `stopped` is reported with the
  exit signal; the recording/finalization evidence decides safety, not the exit
  code alone.
* Power loss, kernel failure, a blocked uninterruptible teardown, or a SIGKILL
  of the whole process group remain outside software control.  An independent
  firmware watchdog or hardware interlock is required for a physical guarantee.
