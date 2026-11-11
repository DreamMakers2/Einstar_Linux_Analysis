# `tools/calib_study` — calibration observability on real live sweeps

A reproducible study that answers *what the live data can and cannot determine*
about the three-camera calibration.  It does not change any calibration; it
measures.

```bash
cd <repo>
.venv/bin/python tools/calib_study/observability.py \
  --sweep  ~/einstar-private/sweep-c/frames \
  --sweep2 ~/einstar-private/sweep-b/frames \
  --profile ~/einstar-private/einstar-profiles/factory-e10.json \
  --vendor-corpus ~/einstar-private/vendor-calib-rapid \
  --out ~/einstar-private/analysis/calib_study
```

Outputs (all under `--out`):

| file | contents |
|---|---|
| `observability_report.json` | every number: detection, trajectory, ladder, spectra, correlations, bootstrap, profile likelihood, design sensitivity, vendor contrast, downstream |
| `REPORT.md` | tables, per-parameter verdicts, limitations, required future diversity |
| `figures/*.png` | parameter sigma, information spectrum, f-vs-depth, bootstrap, held-out, detection, trajectory, design sensitivity |
| `candidates/*.json` | candidate reconstruction profiles with `validation: pending` and full provenance (never written into `einstar-profiles/`) |
| `recon/<candidate>/g<id>/` | `recon_cli` outputs for the downstream comparison |
| `cache/*_detections.json` | per-image marker detections (delete to re-detect) |

## Modules

| module | role |
|---|---|
| `frames.py` | `einstar-frames-v1` manifest, stream→camera mapping with the 180° rotations and Bayer decode, marker detection, and the **(u,v) chirality audit** |
| `motion.py` | plate trajectory from PnP, sweep-leg segmentation, geometric train/held-out splits, coverage statistics |
| `models.py` | `Problem`/`fit`/`linearise`: the `calib.calibrate` residual model with a per-parameter free mask and a pixel-space skew slot; robust sandwich covariance + Schur marginal information |
| `identify.py` | scaled information spectrum, correlation table, per-parameter verdicts, f↔depth covariance, profile-likelihood table |
| `export.py` | `einstar.recon.profile/1` writer/reader, `recon_cli` driver, PLY reader, robust plane fit, pose-normalised repeatability |
| `observability.py` | the CLI and the ten stages |
| `report.py` | `REPORT.md` + matplotlib figures from the JSON |

## Why a wrapper instead of `calib.calibrate`

`calib.calibrate.calibrate` has two switches that act on **all** cameras at once
(`free_intrinsics`, `free_kc`), no switch for the rig extrinsics, and it never
varies `skew` (`_Problem.unpack` builds a `CameraIntrinsics` without one, so the
fitted `K[0,1]` is always 0).  Four of the six ladder models cannot be expressed
with it.  `models.Problem` is the same residual model — same
`calib.geometry` projection convention, same parameter meaning, same robust
Huber covariance estimator — with:

* a free mask over `fx, fy, cx, cy, skew_px, k1, k2, p1, p2, k3` per camera,
* a free mask over `(rx, ry, rz, tx, ty, tz)` per rig camera,
* `x_scale="jac"` in `least_squares` (see `REPORT.md` §9: the default
  `x_scale=1.0` with a sparse Jacobian does not converge on this data),
* `linearise()`: the covariance/Schur information at a *given* state with no
  optimisation, which is what the "at the factory solution" diagnostics use.

`observability.py` runs a `wrapper_verification` stage that solves the same
problem with `calib.calibrate._Problem` and with the wrapper from the same
starting point and the same solver settings, and reports the difference.

## Conventions that matter

* **Skew.** The reconstruction profile JSON stores `skew = K[0,1]` in *pixels*
  (the C++ `CameraProfile::K()` uses it raw); `CameraIntrinsics.skew` is the
  dimensionless MATLAB `alpha_c` with `K[0,1] = skew·fx`.  `export.py` converts
  at the boundary.  Reading the JSON without dividing by `fx` multiplies the
  skew by ~1160 and costs ~10 px of RMS on live data.
* **Streams.** `stream0` = right IR (180°), `stream1` = left IR (upright),
  `stream2` = raw BayerRG colour (180°, `COLOR_BayerRG2BGR`).  `frames.load_view`
  returns the calibration-oriented grayscale image.
* **Held-out.** Never random.  Whole sweep legs are held out, plus the whole
  second session.  Held-out board poses are re-solved from the held-out view
  itself (a pose is a nuisance parameter), and a stricter variant predicts the
  right camera from a pose fitted on the left camera only.
* **Scale.** The plate pitch is an input.  A global rescale of the board and all
  translations leaves every reprojection exactly unchanged, so no residual can
  favour 28.0 mm over 27.72 mm.  The study makes no scale claim.
