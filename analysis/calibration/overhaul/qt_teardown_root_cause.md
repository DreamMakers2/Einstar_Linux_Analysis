# Qt teardown bus error — root cause and fix (2026-11-12)

## Reproduction

The public full suite crashed **after** printing `310 passed, 76 skipped` with
exit 135 (SIGBUS). Five sequential full runs reproduced 1/5; earlier logs show
2/7. The focused test
`tests/test_profile_view.py::test_profiles_dialog_lists_and_gates_the_buttons`
reproduced 22/60 (≈37 %) in a fresh interpreter.

## Root cause

The test created an **unparented** `ProfilesDialog` and closed it without
deleting it. `tools/app.py` connected two lambdas that captured `self`; the
Qt connection manager kept the dialog alive in an uncollectable C++/Python
cycle. At interpreter shutdown PySide6 calls `destroyQCoreApplication`, which
visits every live Python-owned QObject and calls its destructor. When the
parent dialog was visited before its children, Qt freed the child C++ objects
(the gdb evidence shows the freed `QComboBox` at the faulting address) while
the binding manager still held wrappers for them; the later destructor call
used freed memory. Visit order is address/hash dependent, which is why a rerun
often passed. `PYTHONMALLOC=debug` did not change it: this is a C++ use-after-free.

The same lambdas are a real product leak: `Launcher.show_profiles()` created a
new parented dialog on every click and never freed the old one.

## Fix

* `tools/app.py`: replace the two self-capturing lambdas with bound methods;
  set `WA_DeleteOnClose`, clear `_profiles` on `destroyed`, close it in
  `Launcher.closeEvent`, delete the launcher before returning from `main`.
* `tests/test_profile_view.py`: `close(); deleteLater();
  sendPostedEvents(DeferredDelete); assert not shiboken6.isValid(dialog)`.
* `tools/wizard/gui/modern_window.py` and the launchers: delete stopped
  `SessionWorker`/`FinishWorker` QThreads after they have stopped; delete
  headless windows before returning.
* New `tests/test_qt_teardown.py`: runs the dialog test in eight fresh
  interpreters and requires exit 0 with no `Fatal Python error`/`Bus error`.

## Verification

* Focused dialog test: 8/8 fresh processes exit 0 (regression test).
* The public suite must be re-run after export; previous failures were 135/139
  after a green summary, so the exit code is the gate.
* A core dump from PID 131403 and the gdb logs remain the direct evidence of
  the freed `QComboBox`; they are not committed (large, and contain host paths).

## Remaining limitation

PySide6 ≥ 6.13 was not tested; the binding-manager visit order is not part of a
stable API. The fix removes the demonstrated live-wrapper/cycle condition and
adds an exit-code guard for regressions.
