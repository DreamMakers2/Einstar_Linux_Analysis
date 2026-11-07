# tools/calib — Einstar calibration model, rectification and triangulation

Offline, numpy-only pipeline for the SHINING 3D Einstar (EinScan10_01,
3 × 1280 × 1024). Full model and validation numbers:
[`docs/calibration/model.md`](../../docs/calibration/model.md).

| module | purpose |
|---|---|
| `calib_model.py` | load CCF records and/or the device dump, apply the integer key, `Calibration` object (K, dist, R, T per camera), JSON export |
| `rectify.py` | Bouguet stereo rectification (`R1, R2, P1, P2, Q`, inverse maps), numpy only |
| `triangulate.py` | disparity→3D via Q and via DLT, reprojection metrics, PLY export |
| `synth.py` | synthetic round-trip validation (project → distort → rectify → disparity → triangulate) |
| `validate_model.py` | 42 invariant checks per model, pass/fail |
| `key_analysis.py` | feasible sets for the unproved `fx/fy/cx/cy` key entries, depth-uncertainty budget |
| `real_capture_check.py` | real-capture epipolar check and coarse dense disparity (private captures, aggregates only) |

```bash
python3 tools/calib/calib_model.py --source ccf --json-out /tmp/model.json
python3 tools/calib/rectify.py --source ccf
python3 tools/calib/synth.py --source ccf --n 20000
python3 tools/calib/validate_model.py          # 42/42 on ccf and dump
python3 tools/calib/key_analysis.py
python3 -m pytest -q tests/test_calib_model.py
```

`Calibration.load("auto")` prefers the CCF records (the operational,
capture-time calibration) and falls back to the dump; the dump model is the
independent unkeyed cross-check from a different calibration run.
