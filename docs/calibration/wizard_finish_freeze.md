# The Finish button froze the window

Status: 2026-11-12.  Reported from a real 35-view live session; measured and
fixed the same day.

## What the operator saw

The wizard ran for about three minutes, captured 35 views, and on pressing
**Finish & build profile** the window stopped responding: no repaint, no button
state, no cursor change, no message.  The process kept running at ~127 % CPU with
51 threads, minutes after the click, and the only way out was to kill it.

The freeze is not the capture and not the GUI refresh: it is the final solve.

## Root cause

`MainWindow._on_finish()` ran the whole finalisation **inline on the GUI thread**:

```python
def _on_finish(self) -> None:
    if self.worker is not None and self.worker.isRunning():
        self.worker.request_stop()
        self.worker.wait(4000)              # up to 4 s, GUI thread
    self.controls.setEnabled(False)
    QtWidgets.QApplication.processEvents()  # one repaint, then nothing
    result = self.controller.finish()       # 38 s of solver work, GUI thread
```

`controller.finish()` does the final bundle adjustment, an identifiability /
observability analysis, the validation gates and the candidate write.  None of
the event loop runs while it does, so Qt cannot repaint or deliver input.

Two separate blocking stretches, not one:

1. `worker.wait(4000)` stops the ingest thread on the GUI thread (up to 4 s if a
   decision is in flight);
2. `controller.finish()` — the measured 38.3 s.

## Measurement

Reproduced offline on the same session (`controller.resume()` then timed
`finish()`), with `cProfile`, on an otherwise idle machine at 3 OMP threads:

```
finish(): 38.3 s   ok=True
33,153,122 function calls

cumtime  calls  function
 38.27 s     1   controller.finish
 34.23 s    10   calib.calibrate.calibrate            <- ten solve passes
 30.42 s 14190   calib.calibrate.residuals            <- residual evaluations
 25.0  s    10   scipy.optimize.least_squares
 22.91 s    56   scipy.optimize._numdiff.approx_derivative
 22.60 s     3   tools.wizard.observability.model_sensitivity_analysis
```

So the cost is dominated by the solver, and specifically by **ten solve passes**
inside finish: the final calibration plus the identifiability study, each pass
re-evaluating residuals numerically (`approx_derivative`).  On the live machine
the same work was slower still because the capture was writing 31 MB/s and the
GUI thread was also trying to repaint.

Growth with view count explains why this had not been seen earlier: the
acceptance threshold for a *quick* check is small, and my earlier live Finish
tests used 4–10 views (where the whole finish completed in about a second).

## Why the existing tests missed it

* The end-to-end rehearsal test runs Finish through `--headless --finish`, which
  never enters a Qt event loop — a frozen GUI is invisible there.
* The "close during a long solve" test covers a long *ingest* decision, not the
  finish path.
* The live smoke runs were 15–25 s with a handful of views, so finish never had
  enough data to take tens of seconds.

## Fix

`FinishWorker(QThread)` now owns the finalisation:

* stopping the ingest worker happens **inside** the thread, not on the GUI
  thread;
* `controller.finish()` runs there, and reports `done`, `failed` or a
  once-per-second `progress` signal;
* the window disables the controls, shows "⏳ building the profile… N s" and
  keeps repainting;
* closing the window during finish is deferred exactly like the earlier
  close-during-solve fix — the window is never destroyed with a running thread.

## Verification

Measured through the real window on the same 35-view session:

```
after 3 s of finishing: 59 UI ticks of a 50 ms timer, worker running: True
finish wall time: 29.9 s, total UI ticks: 596
state: done, controls enabled: True
```

A 50 ms timer fired 59 times in the first three seconds (the expected ~60) while
the solver ran, so the event loop — and therefore painting and input — stayed
alive for the whole finish.  A regression test
(`test_finish_does_not_freeze_the_window`) asserts exactly that: at least 20
timer ticks during a deliberately slow finish, the worker running, and the
controls re-enabled afterwards.

The fix does not make finish faster; it makes it survivable and visible.  At 35
views it is still ~30–40 s of solver work, now shown as progress instead of an
apparent hang.

## Separate finding: an orphaned capture process

While investigating, killing the GUI left the `einstar-capture-frames` child
running with the illuminator armed.  A `SIGTERM` to the capture does shut the
laser off (verified: `laser 0 %`, trigger counts 0/0 afterwards), but a
`SIGKILL`, a crash or an OOM kill of the GUI would not, and nothing would then
stop the capture.

Not fixed in this change.  The robust fix is to set `PR_SET_PDEATHSIG` on the
capture child (Linux) so it dies with its parent, with a hard `--max-seconds`
backstop for the other platforms.  Until then: stop the wizard with the window
controls, not with `kill -9`.
