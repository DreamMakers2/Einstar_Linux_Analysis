# Calibration performance and architecture overhaul — measured results

Date: 2026-11-12. Host: i9-14900K (32 threads), RTX 3080 12 GB, 64 GB RAM,
Einstar over USB 2.0 High Speed. All timings below are from this host; the
preserved 35-view session (`~/einstar-calibration-02`) was copied to a shadow
directory so the original frames, recording, decisions and candidate were never
modified.

The operator window is documented in
[`modern_ui.md`](modern_ui.md).  The headline: the 35-view finalisation dropped
from **38.94 s to 3.47 s** (**11.2x**), with the same optimum (RMS 0.33037 px vs 0.33044 px), while live
preview now holds **9.75 Hz** independently of a **4.55 decisions/s** detector.
No calibration precision, convergence criterion or validation gate was
weakened to obtain this.

---

## 1. What was measured

Evidence files:

| file | content |
|---|---|
| `analysis/calibration/overhaul/finalization_baseline_35view.json` | pre-change cProfile breakdown |
| `analysis/calibration/overhaul/finalization_after_35view.json` | post-change finish measurement |
| `analysis/calibration/overhaul/detection_thread_30.json` | 30-group detector benchmark |
| `analysis/calibration/overhaul/detection_process_20.json` | process-isolated detector benchmark |
| `analysis/calibration/overhaul/live_validation_2026-11-12.json` | controlled live capture, preview, CPU/GPU and SIGKILL results |
| `tools/bench/bench_finalize.py` | reproducible finish benchmark |
| `tools/bench/bench_detection.py` | reproducible detector benchmark |

The original profile of the finish path:

```
38.94 s finish
├─ 34.34 s  ten calibrate() passes, all on SciPy finite differences
│  ├─ 24.61 s scipy.optimize.least_squares
│  └─  9.25 s covariance
├─ 22.63 s  model_sensitivity_analysis (three identical calls)
├─ 12.26 s  verify.heldout + verify.split_half + quality stability
├─  7.73 s  observability refresh
├─  2.73 s  hashing the 5.6 GB raw recording
└─  ~0.4 s  the final analytic LM solve
```

The finish path is not one solve. It is a final solve plus independent
held-out, split-half, stability, coverage, observability and model-sensitivity
checks. Those checks were implemented on the old `calib.calibrate()` SciPy
finite-difference path, and the same expensive sensitivity was recomputed three
times.

### 1.1 Solver pass cost

| path | 35-view solve | notes |
|---|---:|---|
| SciPy TRF, finite-difference Jacobian | 5.01 s | original verification/reference path |
| analytic-Jacobian LM (`calib.solvers`) | 0.57 s | **7.5x faster**, same optimum |
| final solve at the candidate model | 0.43 s | 8,288 residuals, 234 free parameters, 6 LM iterations |

Parameter agreement between the two paths on the preserved 35-view problem:
maximum absolute difference **4.4e-5** (a principal-point component), residual
RMS difference **4e-8 px**, baseline difference **5e-8 mm**, covariance
diagonal within **2.7%**. `tests/test_solver_equivalence.py` pins this on a
synthetic scene for both free and held-distortion configurations, and the full
suite keeps the SciPy path available as the independent reference
(`calibrate(..., backend="scipy")`).

The Eigen-free C++ reference (`calib/ceres/calib_ref`) was also timed on the
same 35-view problem with all five distortion coefficients free: 0.141 s
reported solver time (0.46 s process wall), RMS 0.32310941 px versus the Python
analytic LM's 0.4399 s / RMS 0.32310931 px. It is **not integrated**: the
native backend cannot express the held-distortion or prior configuration used
by the final profile, and its covariance has a documented tangential-Jacobian
defect. The 3.1x native advantage is recorded in
`analysis/calibration/overhaul/native_solver_35view.json` as the next
optimisation if the Python path becomes the bottleneck again.

### 1.2 Finish path after the change

| item | before | after |
|---|---:|---:|
| finish wall time, 35 views | **38.94 s** | **3.47 s** (repeat 3.56 s) |
| session solve time | 1.12 s (2 solves) | 1.03 s (2 solves) |
| full solver passes inside finish | 10 | 9 (the 3 sensitivity calls became 2 unique analyses, one cached) |
| model-sensitivity analyses | 3 × 7.5 s | 2 unique (1.8 s) |
| raw-recording hashing | 2.73 s serial, whole file in RAM | overlapped with validation, streamed in 8 MiB chunks |
| finish UI | frozen | responsive; progress shown |

The finish path now uses:

1. **Analytic LM for every verification/stability refit.** `calibrate()`'s
   default backend is the analytic LM; the SciPy path remains selectable.
2. **Content-keyed sensitivity cache.** The release decision, observability
   gate and lifecycle validator ask for the same held-out analysis; the second
   and third requests are cache hits. The cache key includes every observation,
   the fitted parameter vector, pitch, seed and limits.
3. **Overlapped provenance hashing.** The raw recording is hashed on one worker
   while validation runs; hashing is streamed rather than loading 5.6 GB into
   memory.
4. **Unchanged gates.** Held-out, split-half, epipolar, coverage,
   observability, lifecycle and rollback checks all still run.

### 1.3 Acquisition, preview and detection

Controlled live run (12 s, one scanner, no comprehensive calibration):

| metric | measured |
|---|---:|
| capture cadence | 9.76 groups/s |
| throughput | 36.9 MB/s wall |
| transfer timeouts | 0 |
| partial timeouts | 1 |
| frames completed / dropped | 321 / 3 (startup only; 0 sequence gaps) |
| preview rate | **9.75 Hz**, p50 113 ms, p95 121 ms |
| decisions | **4.55/s**, p50 202 ms, p95 527 ms |
| process CPU | 193% average (1.93 cores), top thread 44% |
| GPU | 29–41% observed, **same as the 28–36% idle baseline**; calibration does not use CUDA |

The detector benchmark on the recorded sweep:

| mode | mean | p50 | p95 | note |
|---|---:|---:|---:|---|
| thread pool (replay) | 135.4 ms | 120.5 ms | 151.9 ms | 30 groups, 96 ROI hits / 3 full |
| process pool (live) | 122.7 ms | 106.2 ms | 490.9 ms | 20 groups; p95 includes first spawn |

The original detector measurement was **158 ms/group**. The 15–32% reduction
comes from an early-exit on the first threshold combination when it already
finds the full 40-marker lattice at ≤0.5 px RMS. It does **not** change the
detector or the localiser, and a partial/ambiguous view still runs the full
search.

The architectural change matters more than the per-pass number:

```
capture process (C++, USB)
        │  writes frames/ + raw.e1r at 10 Hz
        ▼
reader thread ──► newest-frame queue (1) ──► DecisionRunner
        │                                    └─ ProcessPool (3 cameras)
        └─► preview signal ──► GUI                └─ Fast LM / validation
```

Before, the acquisition loop called `controller.ingest()` inline and only
published the preview after the decision. A 158 ms decision capped the display
at ~6.3 Hz, and the controller's `record_only()` path shared the decision lock.
The reader now owns the source cadence and preview; a slow decision can only
drop its own stale input, never the next preview. On live hardware this raised
preview from ~6.4 Hz to 9.75 Hz and kept the capture process at 10 Hz.

### 1.4 Auto exposure

The capture process accepts an atomic volatile-settings file
(`exposure-control.txt`). The wizard computes robust per-frame statistics and
writes only exposure/gain/laser changes; the C++ session validates each value
against the device range and applies it through the same volatile control
channel. No persistent device write is used.

Rules:

* IR cameras are adjusted as a pair, RGB independently.
* Exposure first, gain second, illumination last; ±25%/step, 0.8x down.
* Hard saturation protection; two-observation confirmation; 1 s minimum
  interval.
* An accepted calibration view freezes settings for 1.6 s, so the observation
  used by the solver is not taken while exposure moves. Saturation can
  override the freeze.
* Every change is written to `SESSION/exposure.jsonl` and appears in
  `status.json`; the final settings are in the capture manifest.

In the 12 s live run the advisor changed settings three times:
IR 1200 → 1500 → 1875 → 2343 µs; RGB 5000 → 6250 → 7812 → 9765 µs; laser stayed
100%. The capture reported 0 transfer timeouts and the final readback matched
the requested values. This is a short-run functional validation, not a
photometric calibration.

### 1.5 Established-method review

`docs/calibration/established_systems_review.md` records what SHINING 3D,
Artec, Creaform, ZEISS/GOM, Photoneo, OpenCV, Kalibr, AprilCal and the bundle
adjustment literature actually disclose, separates public implementation detail
from marketing claims, and grades each recommendation. It concludes that the
existing architecture already uses the right established pieces (coarse-to-fine
circle detection, rigid three-camera bundle adjustment, task-space
next-best-view, robust losses, held-out validation) and that the measured
bottlenecks were engineering (GIL, repeated verification, serial hashing), not
a wrong estimator. Ceres remains unavailable because Eigen headers are absent;
OpenCV 5 is used where its primitives are proven; no dependency was added.

### 1.5b Sustained operation and failure recovery

A 120 s controlled live run (static plate, no calibration) held:

| metric | measured |
|---|---:|
| capture cadence | 9.96 groups/s |
| throughput | 39.19 MB/s |
| transfer timeouts | 0 |
| frames completed / dropped | 3,558 / 3 (startup only) |
| preview | **9.96 Hz**, p50 112.8 ms, p95 117.3 ms |
| decisions | 4.58/s, p95 262 ms |
| GUI RSS | 264 MB → 352 MB during warm-up, then flat at ~351 MB |
| auto-exposure changes | 3, then stable (IR 1500 µs, RGB 9765 µs) |
| shutdown | `control:stop`, trigger readback 0/0, illuminator readback 0, `cleanup_verified` |

The first attempt (before the disk guard) filled a 32 GB tmpfs to 5.5 MB; the
writer stopped and the final status was lost. The application now stops live
capture with 2 GB free (about 50 s of headroom), and the successful 120 s run
ended with 20.7 GB free. This is recorded as a real failure, not hidden.

Capture-child failure was also tested on hardware: `SIGKILL` of the child while
the GUI stayed alive. The parent detected the unexpected exit, ran the new
volatile-only `--safe-off` mode (trigger readback 0/0, laser 0, strobes 0, readback
verified), surfaced the error, and exited normally with no orphan capture
process. The child's stale status is not represented as a graceful stop.

### 1.5c 49–60-view scaling

Synthetic ground truth, all five distortion coefficients held, covariance on:

| views | solve wall | RMS | baseline σ | point σ at 450 mm |
|---:|---:|---:|---:|---:|
| 20 | 1.55 s | 1.17 px | 0.100 mm | 1.044 mm |
| 35 | 0.92 s | 1.23 px | 0.072 mm | 0.819 mm |
| 60 | 2.53 s | 1.30 px | 0.053 mm | 0.621 mm |

A 49-accepted-view synthetic controller finish (60 rendered poses) completed
in **4.96 s** with RMS 0.413 px and all validation gates run. The 35-view real
session remains the primary comparison because its optimum is independently
known; the synthetic corpus is for scaling.

### 1.6 Process safety

`docs/calibration/process_safety.md` records the mechanism and the unit tests.
On hardware, the GUI was `SIGKILL`ed while capture was running. The capture
child exited, published `stopped`, `cleanup_verified: true`, trigger counts
`0/0`, and illuminator readback `0`. **Optical darkness is not verifiable from
software** and is not claimed; the readback is the strongest available host-side
evidence. The same run found and fixed a real regression: the capture was
originally spawned from the short-lived reader thread, so the kernel's
per-thread `PR_SET_PDEATHSIG` stopped the scanner when that thread exited. Live
capture is now started from the long-lived GUI thread before the reader starts.

---

## 2. What was deliberately not changed

* The original plate geometry, factory calibration and 28.0 mm pitch.
* The three-camera joint model, Huber loss, held-out and epipolar checks.
* The detector's marker identity/orientation witness and partial-plate logic.
* The final solve's convergence tolerances (1e-10) or the covariance
  estimator.
* The factory profile; the candidate remains `validation: pending` and is not
  activated automatically.
* USB 2.0 operation. The measured 9.76 groups/s at 36.9 MB/s is close to the
  practical link ceiling; no driver rewrite is justified by the evidence.

## 3. Remaining limitations

* **No comprehensive physical calibration session was run** after the
  changes. The 35-view session remains `NOT READY` for geometry, exactly as
  before; performance work did not change that verdict.
* Absolute dimensional accuracy is still unverified (no independent length
  artefact).
* The live runs are 60–180 s, not an 8 h soak. The 120 s run had zero transfer
  timeouts and three startup drops; one partial timeout occurred in a 12 s run.
  Longer thermal/USB behaviour is unmeasured. A first soak filled a tmpfs; the
  disk guard now stops at 2 GB free, but power loss and kernel I/O stalls remain
  outside software control.
* Process-pool startup adds ~0.4 s to the first decision. Replay/tests keep the
  thread path to preserve deterministic ordering.
* Auto exposure is validated for control and stability, not for photometric
  colour accuracy. RGB remains gated on a valid colour extrinsic.
* The finish speed measurement uses the preserved session, not a new capture;
  a new session with more views will be slower because the validation set grows.
* GPU utilisation was not reduced or increased by this work; the calibration
  path is CPU-only and the observed variation overlapped background desktop
  load.

## 4. Reproduction

```sh
# 35-view finish benchmark (shadow copy; original untouched)
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/bench/bench_finalize.py \
  --session ~/einstar-calibration-02 \
  --out analysis/calibration/overhaul/finalization_after_35view.json

# detector benchmark
OPENBLAS_NUM_THREADS=1 .venv/bin/python tools/bench/bench_detection.py \
  --groups 30 --out analysis/calibration/overhaul/detection_thread_30.json

# full verification
PYTHONPATH="$PWD/tools:$PWD" \
EINSTAR_CAPTURE_TEST_BINARY="$PWD/build/driver/einstar-capture-frames" \
bash scripts/verify_all.sh
```
