# Capture process safety (Linux)

This change protects capture when its controlling application exits, including
GUI `SIGKILL` and default `SIGTERM`, and when its heartbeat stops. Verification
below used real Linux processes and the simulated transport only. No scanner was
connected or used, and no optical or physical illuminator-off claim is made.

## Audit and failure being addressed

Previously `tools/wizard/capture.py` launched capture with
`start_new_session=True`, no parent-death signal, and no heartbeat. Its `atexit`
cleanup sent `SIGINT` and waited up to two seconds. GUI `SIGKILL` bypasses Python
cleanup entirely; default `SIGTERM` also bypasses Python `atexit`. The separate
session prevents terminal/process-group teardown from reliably stopping capture.
The inspected wizard code installs no `SIGTERM` handler. Normal GUI close waits
for workers; this is not a mechanism for catching process death.

`run_capture_session()` already had cooperative `SIGINT`/`SIGTERM` handling and
ordinary stop/pause paths, but installed handlers only after output preparation.
Early returns and exception unwinding could bypass driver cleanup. The live
stop path only stopped the trigger when its `streaming_` flag was set, missing a
partially failed start. Illuminator failures were warnings, while cached laser
and strobe fields were nevertheless overwritten with zero.

A related existing issue is in `driver/src/control_channel.cpp`,
`ControlChannel::stop_streaming()`: failure of the preliminary period write
returns before the zero-trigger-count write. That file is outside this change's
ownership. The live capture cleanup now explicitly attempts `set_trigger_counts(0,
0)` if `stop_streaming()` fails; the underlying function is unchanged.

## Mechanism

1. At the beginning of C++ `main`, before argument dispatch, acquisition, or USB
   setup, `ProcessSafetyGuard` installs checked signal handlers and Linux
   `prctl(PR_SET_PDEATHSIG, SIGTERM)`. A failure refuses capture. Immediately
   after `prctl`, it compares `getppid()` against the expected parent PID.
   The wizard supplies its PID through `EINSTAR_CAPTURE_PARENT_PID`; this also
   covers parent death between Python spawn and C++ startup. A stale parent
   refuses capture before a session directory is created. Standalone CLI runs
   use the parent PID observed at startup; without the wizard environment they
   cannot identify reparenting that already happened before that observation.
2. The wizard passes only the read end of a new nonblocking, close-on-exec pipe
   through `Popen(pass_fds=...)` and `EINSTAR_CAPTURE_HEARTBEAT_FD`. A daemon
   thread writes a byte every 250 ms. No Python `preexec_fn`, CLI argument,
   control-file command, or device setting is added. File-based start, pause,
   resume, stop, recording, and the separate process session remain available.
3. A separate child watchdog polls every 50 ms. Parent PID change, pipe EOF or
   error, or two seconds without a pulse requests cooperative `SIGTERM`
   shutdown. EOF works even if the parent stays alive; timeout works even if
   another process accidentally retains a pipe writer. The watchdog also runs
   for standalone CLI capture without a heartbeat pipe.
4. Signal handlers only update lock-free atomic flags. USB and filesystem work
   is never done in a signal handler. The acquisition loop consumes the signal,
   stops through its driver, finalizes the recording, and publishes status. A
   signal already pending before session start prevents triggering from starting.
5. From the first stop signal/watchdog request, the child allows five seconds
   for teardown. If it cannot finish, the watchdog emits `illuminator state
   UNVERIFIED; forcing SIGKILL`, then kills its own process. This bounds ordinary
   blocked user-space teardown; it does not prove a USB command reached hardware.
6. Live shutdown attempts laser zero and both strobe routes zero before trigger
   shutdown, even for a partial start or `--trigger off` session. Trigger cleanup
   includes the fallback described above. Readback follows the off commands and
   trigger stop. Off command failures, unavailable/nonzero readback, and pipeline
   errors remain errors. Cleanup responsibility covers setup failures after the
   control channel opens, session preparation failures, and exception unwinding;
   driver stop is idempotent. A control channel that cannot open cannot issue off
   commands, and its failure is not treated as a successful shutdown.

Status retains `einstar-live-status-v1` and adds `cleanup_attempted` and
`cleanup_verified`. Verified cleanup also supplies zero `trigger_counts`.
`laser_percent` contains readback, or `null` if unavailable, rather than an
assumed zero. The wizard rejects an attempted but unverified cleanup. With a live
backend, `cleanup_verified` means command success plus device register readback;
it does **not** verify emitted light. With the simulator it means successful
simulator pipeline shutdown and simulated zero state only. Graceful signal
shutdown retains the CLI's existing success exit code and `signal:SIGTERM` or
`signal:SIGINT` stop reason.

## Reproduction and measured results

Run from the repository root. Dependencies were already installed; no packages
were installed. The test host reported Linux `7.3.3-1-cachyos`, Python `3.14.9`,
and GCC `20260810`. The live backend was compiled because libusb was available,
but every launched capture command selected `--backend simulated`.

```sh
cmake -S driver -B build/process-safety \
  -DEINSTAR_WITH_LIBUSB=ON -DEINSTAR_BUILD_TESTS=ON -DCMAKE_BUILD_TYPE=Debug
cmake --build build/process-safety -j 4
ctest --test-dir build/process-safety --output-on-failure
build/process-safety/test_capture_session
PYTHONPATH="$PWD/tools:$PWD" \
  EINSTAR_CAPTURE_TEST_BINARY="$PWD/build/process-safety/einstar-capture-frames" \
  python -m pytest tests/test_capture_safety.py tests/test_wizard_capture.py \
  -q -s --tb=short
git diff --check
```

The full CTest run passed **13/13 targets**, including replay/recording,
control-channel, stress, and capture-session tests (57.91 s on this host).
After final safety changes the capture-session target was rebuilt and rerun;
see the safety timings below. Python passed **28 tests** (5.40 s in the measured
run). The existing wizard tests require `tools` on `PYTHONPATH` for two legacy
`from wizard import capture` imports. Integration tests skip if the default
binary is absent; an explicitly configured missing binary fails. The measured
run explicitly selected the newly built binary and skipped no tests.

| Scenario | Observed delay | Evidence |
| --- | ---: | --- |
| GUI-equivalent parent `SIGKILL` | 0.020 s | Child exited; stopped status, simulator cleanup, manifest and recording present |
| Parent `SIGTERM` | 0.010 s | Same cleanup evidence |
| Parent `SIGSTOP`, heartbeat stalled | 2.033 s | Parent still alive, pipe still open; child logged heartbeat expiry and cleaned up |
| Heartbeat pipe EOF, parent alive | 0.007 s | Child logged pipe closure and published verified simulator cleanup |
| Deliberately blocked driver teardown | 5.058 s | Stop-entry marker present; real child died by `SIGKILL`; stderr explicitly unverified |

These delays are representative samples, not worst-case guarantees. Python
exit checks and C++ child waits poll at 10 ms resolution; the stall case includes
the two-second watchdog interval.

The Python parent-death tests launch the real wizard supervisor in a separate
Python process and the real capture executable as its child. They wait for
running/paused status before injecting faults, assert exit and cleanup evidence,
and reap orphaned children using a temporary Linux subreaper. Healthy heartbeat
is tested across more than two seconds while paused, followed by resume and
control-file stop without signal escalation. Direct child `SIGINT` and `SIGTERM`
without a heartbeat are also tested. A stale expected PID tests startup refusal.
Recordings are finalized by the unchanged recording verification path.

`driver/tests/test_capture_safety.cpp` is included by the existing
`test_capture_session.cpp` target, avoiding a build-system change. Its eight
safety cases check preparation/start failures, exceptions, pending signals,
unverified cleanup, `PR_GET_PDEATHSIG == SIGTERM`, and bounded blocked teardown.
The final real parent/child experiment observes a byte written **only by an
actual SIGTERM handler after parent SIGKILL**, independently proving kernel
parent-death signal delivery rather than inferring it from watchdog exit.

## Limits

- Direct `SIGKILL` of capture, fatal runtime corruption, `abort`, host power loss,
  kernel failure, an uninterruptible kernel I/O wait, or stopping the entire child
  cannot be repaired by a handler/watchdog in that child. An independent firmware
  watchdog or external hardware interlock is required for a physical guarantee.
- USB requests can fail, time out, or block. The five-second cutoff can interrupt
  cleanup before every command/readback finishes, or interrupt recording
  finalization. An unverified/missing final status is not evidence of darkness.
  No real hardware timing was measured. File logging and process scheduling are
  not hard real-time guarantees.
- A GUI event-loop stall while its heartbeat thread continues running is not
  detected. `SIGSTOP`/whole-process stalls and loss of heartbeat are detected.
  Debugger pauses longer than two seconds intentionally stop capture.
- Linux parent-death signals are associated with the thread that created the
  process. A short-lived spawning thread can therefore cause a conservative
  stop while the rest of the parent process is alive. PID checking and heartbeat
  supplement this kernel behavior; they do not make it portable to other OSes.
- The wizard and capture executable must both contain this change. The private
  environment/pipe contract is not a negotiation protocol for older executables.
- Setup cleanup can only issue commands after the control channel opens. A
  failed/unavailable transport provides no way to confirm a pre-existing scanner
  state. Forced exit diagnostics and the lack of verified final status must be
  treated as an unresolved hardware state.

## Parent-side recovery after capture-child SIGKILL (2026-11-12)

A killed child cannot run its signal handler. The supervisor now has an
explicit recovery path for that case:

* If the capture process has exited while its last published state is not
  `stopped`, the parent runs `einstar-capture-frames --backend libusb
  --safe-off` after the child is gone. That mode opens the control channel,
  stops streaming / zeroes the trigger, turns the laser and both strobes off,
  reads them back, and exits. It sends volatile commands only.
* The GUI surfaces the failure as an acquisition error. The recovery result is
  recorded in `ui_metrics.json` (`capture_safe_off`) and in the final verdict.
* The child's last `status.json` remains stale; it is never presented as a
  graceful stop. The safe-off readback is the recovery evidence.

Hardware test: with the GUI running live, the capture child was `SIGKILL`ed.
The GUI exited normally (code 0), no capture process remained, and the recorder
reported:

```
GUI error: acquisition reader: SourceError: capture process exited unexpectedly:
           emergency safe-off verified (volatile readback)
safe-off:  trigger readback 0/0, illuminator readback 0, volatile only
```

Evidence: `analysis/calibration/overhaul/capture_child_sigkill.json`. Optical
darkness is still not claimed. Power loss, kernel failure, or `SIGKILL` of the
whole process group remains outside software control.

## Storage guard

Live capture now stops when the volume falls below **2 GB** free (about 50 s of
headroom at 39 MB/s). This prevents the failure observed in the first soak
attempt: a 32 GB tmpfs filled to 5.5 MB, the writer stopped and the final status
was lost. The guard is tested as a pure decision (`disk_guard_reason`) and the
subsequent 120 s live run used 24 GB free and finished with verified cleanup.
