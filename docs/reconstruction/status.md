# Milestone status (M1–M7)

Status: 2026-11-11.  Legend: **implemented** (code builds), **offline-tested**
(deterministic tests/recorded data), **real-data** (genuine private frames),
**live** (scanner attached; nothing below is live), **pending** (blocked on
hardware), **unverified** (no evidence).

| gate | evidence | state |
|---|---|---|
| **M1 — offline integration** | `recon/` consumes the same `FrameSet` for files and replay; calibration enters only as a validated JSON profile; driver/calib/reconstruction interfaces documented in `architecture.md`; audit in `audit.md` | **implemented, offline-tested** |
| **M2 — real-data reconstruction** | 3 genuine triplets → rectified pairs, disparity, confidence, depth, 56 600-point PLY per frame; every output records the profile id and input SHA-256 | **implemented, real-data** |
| **M3 — geometric/numerical validation** | synthetic planes 200–700 mm recovered to ≤0.13 mm; 4 984 automated checks; real-data consistency (92 % planar, 0.34 mm plane RMS, 0.1 mm cross-frame repeatability); `validation_plan_m3.md` states the accuracy limit | **implemented, offline-tested + real-data**; physical accuracy **unverified** |
| **M4 — acquisition/replay integration** | `einstar-capture-frames` (simulated + libusb paths) and `einstar-replay-frames` write the same `frame_%06d_cam%d.pgm` + manifest; a simulated 3-group capture records 295 transfers, replays to 9 frames, and `recon_cli` consumes the replay through the identical loader; corrupted recording refused | **implemented, offline-tested**; live libusb path **pending hardware** |
| **M5 — one-session calibration recorder** | `tools/calibsession/calibrate_session.py` on the 25-view vendor corpus: 21 accepted, 16 train / 5 held-out, 4 rejected + 1 session-rejected, colour frames present and plate detected in 21/21, completeness pass, `--verify` re-hashes 75 files, versioned host profile + exported reconstruction profile; stop recommended | **implemented, offline-tested on the vendor corpus**; real user session **pending** |
| **M6 — live readiness** | runbook in `acquisition.md` (device detection, metadata, volatile exposure/gain/trigger, capture, record, replay, safety); live paths are labelled untested | **documented**; live **pending hardware** |
| **M7 — publication** | code, tests, architecture, benchmarks, synthetic figures and non-sensitive results exported to the public repository | **done** (see commit list) |

## Exact commands and results

```bash
# build + tests
cmake -S recon -B build/recon -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/recon -j8
ctest --test-dir build/recon --output-on-failure     # recon_tests PASS (4 984 checks)

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
  held-out colour check is supplied.
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
