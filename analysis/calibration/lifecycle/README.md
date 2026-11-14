# Lifecycle evidence (52 GB session regression)

All files here are derived from the read-only recorded session
`einstar-calibration-03` and from local process/fixture tests.  No scanner was
used and no raw recording bytes are copied.

| file | content |
|---|---|
| `session03_observed_sequence.json` | status/stderr/control/timeline/trailer/frames-manifest and the exact contradiction, plus D1/D4 from the recorded view manifest with the stationarity stream |
| `ring_static.json` | final 120 ring groups: sub-pixel centroid variation (0.02–0.03 px), proving the unevaluated tail was a static plate |
| `observed_acquisition_state.json` | the new `AcquisitionEvidence` verdict for the observed status/stderr/trailer/SIGKILL sequence (`stopping_unverified`, not safe) |
| `persistence_benchmark.json` | old vs new manifest/session snapshot cost at the observed 297-view payload scale |
| `tests_results.txt` | final pytest output for the lifecycle/stop/safety suites |
| `tests_gui_results.txt` | GUI smoke / Qt close-teardown output (one coverage-wording failure outside this change) |
| `tests_results.json` | machine-readable summary and known unrelated failures |

Reproduce the trailer/static/coverage facts with the recorded session mounted
read-only; the tests use `analysis/session03` (manifest + decision stream) and
require no scanner.
