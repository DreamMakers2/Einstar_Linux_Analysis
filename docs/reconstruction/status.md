# Milestone status (M1–M7)

Status: 2026-11-11.  Legend: **implemented** (code builds), **offline-tested**
(deterministic tests/recorded data), **real-data** (genuine private frames),
**live** (scanner attached; nothing below is live), **pending** (blocked on
hardware), **unverified** (no evidence).

| gate | evidence | state |
|---|---|---|
| **M1 — offline integration** | `recon/` consumes the same `FrameSet` for files and replay; calibration enters only as a validated JSON profile; driver/calib/reconstruction interfaces documented in `architecture.md`; audit in `audit.md` | **implemented, offline-tested** |
| **M2 — real-data reconstruction** | 3 genuine triplets → rectified pairs, disparity, confidence, depth, 56 600-point PLY per frame; every output records the profile id and input SHA-256 | **implemented, real-data** |
| **M3a — RGB registration** | raw Bayer (BayerRG) debayer, colour-camera projection/distortion, 180 deg stream rotation, synthetic BGR + Bayer tests (>90 % channel correctness), explicit `color_status` gate; genuine replacement-profile diagnostic (100 % projected) | **implemented, synthetically validated**; real colour photometry/white balance **unverified** |
| **M3 — geometric/numerical validation** | synthetic planes 200–700 mm recovered to ≤0.13 mm; 5 002 automated checks; real-data consistency (92 % planar, 0.34 mm plane RMS, 0.1 mm cross-frame repeatability); `validation_plan_m3.md` states the accuracy limit | **implemented, offline-tested + real-data**; physical accuracy **unverified** |
| **M4 — acquisition/replay integration** | `einstar-capture-frames` (simulated + libusb paths) and `einstar-replay-frames` write the same `frame_%06d_cam%d.pgm` + manifest; a simulated 3-group capture records 295 transfers, replays to 9 frames, and `recon_cli` consumes the replay through the identical loader; corrupted recording refused; **driver ctest 12/12 passes** (224 replay-frames checks) | **implemented, offline-tested**; live libusb path **pending hardware** |
| **M5 — one-session calibration recorder** | `tools/calibsession/calibrate_session.py` on the 25-view vendor corpus: 21 accepted, 16 train / 5 held-out, 4 rejected + 1 session-rejected, colour frames present and plate detected in 21/21, completeness pass, `--verify` re-hashes 75 files (**OK**), versioned host profile + exported reconstruction profile (colour `unknown` by default); stop recommended; held-out median **0.336 px**; factory-orientation guard rejects an upside-down stream (ΔR 179.86°); **19 recorder tests and 176 repo tests pass**, including the probe-style stream mapping/rotation and sequential dwell | **implemented, offline-tested on the vendor corpus + probe-style triplet**; real user session **pending** |
| **M6 — live readiness** | runbook in `acquisition.md` (device detection, metadata, volatile exposure/gain/trigger, capture, record, replay, safety); no-device failure verified: exit 1, clear hint, **no output directory created**; live paths labelled untested | **documented + offline safety test**; live **pending hardware** |
| **M7 — publication** | code, tests, architecture, benchmarks, synthetic figures and non-sensitive results exported to the public repository; pushed as `7e61c29` (51 files, 11 994 insertions) | **done** |

## Exact commands and results

```bash
# build + tests
cmake -S recon -B build/recon -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/recon -j8
ctest --test-dir build/recon --output-on-failure     # recon_tests PASS (5 002 checks)

# real frames
build/recon/recon_cli reconstruct --frames <capture-dir> \
  --id 0 --profile <private-inputs>/einstar-profiles/factory-e10.json \
  --out /tmp/einstar_recon/id0 --scale 0.5
# valid 17.3 %, 56 600 points, depth p05/50/p95 331.6/349.5/368.6 mm,
# plane RMS 0.336 mm, total 0.12 s

# capture -> record -> replay -> reconstruct (simulated, no hardware)
build/driver/einstar-capture-frames --out /tmp/cap --groups 3 --backend simulated \
  --record /tmp/cap.rec --faults clean --seed 1
build/driver/einstar-replay-frames --in /tmp/cap.rec --out /tmp/replay --manifest
build/recon/recon_cli reconstruct --frames /tmp/replay --id 0 \
  --profile <private-inputs>/einstar-profiles/factory-e10.json \
  --out /tmp/replay_recon --scale 0.5

# one-session recorder on the vendor corpus
.venv/bin/python tools/calibsession/calibrate_session.py \
  --vendor-corpus <private-inputs>/vendor-calib-rapid \
  --factory-dir sources/calibration/res_Einscan-E10_200x150 \
  --out /tmp/calibsession_corpus --target 24 --json
.venv/bin/python tools/calibsession/calibrate_session.py --verify /tmp/calibsession_corpus
```

## What is *not* done

* Live scanner capture (device detection, exposure/trigger, triplets) is
  implemented but **untested on hardware**; the USB3 hardware work remains on
  hold.
* The one real guided calibration session has not happened yet (by design: the
  recorder is ready and tested on the corpus).
* Absolute metric accuracy is unverified: no calibrated sphere bar/flatness
  artefact has been measured.  The 28.0 mm pitch scale ambiguity remains.
* Colour registration is implemented but gated; the shipped factory colour
  record is stale, and a replacement profile defaults to `unknown` until a
  held-out colour check is supplied.  The recorder's vendor-corpus fit did
  include the colour camera (plate detected in 21/21 sets) but its exported
  reconstruction profile is intentionally not marked `ok` without an
  independent colour check.
* The recorder's `--frames` stream mapping/orientation (cam0 = right rotated,
  cam1 = left) is unit-tested on a probe-style triplet; a real probe session is
  still pending hardware.
* **GPU accel.:** CPU and OpenCL preprocessing are measured; a separate
  OpenCV+CUDA build was used to benchmark CUDA SGM: 3.47 ms steady vs 5.73 ms
  CPU on a 128-disparity window, but with a longer depth tail and no LR/
  confidence, and CUDA SGM only supports 64/128/256 disparities.  It is **not
  integrated**; no end-to-end GPU speedup is claimed (`gpu_benchmark.md`).
* Tracking, fusion, meshing, Qt6 and CUDA acceleration are future milestones
  (`docs/reconstruction/modern_pipeline_design.md`), not part of this result.

## Claims

* **Can claim:** a working Linux-native path from genuine Einstar IR/RGB
  streams to a repeatable metric point cloud; correct stream/coordinate
  handling; sub-0.13 mm agreement on synthetic planes; 0.34 mm planar RMS and
  0.1 mm repeatability on the real capture; deterministic replay→reconstruction.
* **Cannot claim:** physical sub-millimetre accuracy, ISO/VDI conformity, or
  that the absolute scale is correct.  Those require the artefact protocol in
  `validation_plan_m3.md` on a real unit.
