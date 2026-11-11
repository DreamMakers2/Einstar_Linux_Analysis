# Colour registration — measured and corrected

Status: 2026-11-12.  The shipped colour-camera record is not "slightly stale":
measured on real data it reprojects the calibration plate with **461 px RMS**.
It has now been re-fitted from recorded frames and validated on held-out views.

## Why it can be fixed from existing data

The IR pair is well calibrated, so the colour camera's pose relative to the left
IR camera can be solved directly whenever both cameras see the plate:

1. detect the plate in the left IR frame and solve its pose with the trusted IR
   intrinsics;
2. detect the plate in the colour frame **after the debayer and 180° rotation
   that the reconstruction engine applies** (`recon/src/depth.cpp`), so the
   result lives in the frame the engine samples colour in;
3. solve the colour pose from the same board points;
4. fit one relative transform (6 parameters) over all views by minimising the
   colour reprojection error.

The first attempt at this measurement gave a 180° / 247 mm discrepancy, which was
**my** error: detecting on the raw colour frame instead of the rotated one.  The
engine's convention is the rotated frame.

## Measured result

120 real views from the one-axis sweep (72 train / 48 held out):

| | reprojection RMS |
|---|---|
| shipped colour record | **461.0 px** (held-out 503.4 px) |
| fitted, training views | 0.31 px |
| fitted, **held-out views** | **0.34 px** |

That is a **1502×** improvement on views the fit never saw.  The corrected
transform differs from the shipped one by 247.0 mm and 179.9°.

**Cross-session check.**  Held-out views come from the same recording, so the
fit was also evaluated on an *independent* recording (`sweep-c`, 600 groups,
never used in the fit), 120 views:

| on sweep-c (independent session) | reprojection RMS |
|---|---|
| shipped colour record | 463.1 px |
| fitted on sweep-b | **0.32 px** |

The colour pose holds across sessions to a third of a pixel — the geometry is
right, not fitted to one session's quirks.  A regression test asserts this
(stored > 50 px, cross-session < 2 px).

Consequences in the engine:

```
factory profile : color profile-inconsistent (stale)   color_fraction 0.00
fitted profile  : color ok                             color_fraction 1.00
```

Every valid point now receives a colour sample; before, none did.  A point cloud
written with the corrected profile has plausible colours (plate brighter than
background, no black points).

## The status is earned, not asserted

The reconstruction engine samples colour only when `color_status == "ok"`.  The
fitting tool therefore writes `color_status: "fitted"` by default — which the
engine still refuses — and promotes to `ok` **only** when the fit's own
held-out measurement clears `≤ 1.0 px` on `≥ 10` held-out views.  The evidence
is written into the profile provenance:

```
colour_validation=heldout_rms_px<=1 heldout_views>=10 measured=0.340/48
```

A fit with a bad held-out number, or too few held-out views, stays `"fitted"`
even if the flag is passed — that is tested.

## What this does and does not establish

* **Does**: the colour camera's pose is now correct relative to the IR pair, to
  sub-pixel reprojection on held-out data.  Colour sampling is enabled and
  populated.
* **Does**: generalise to an independent recording (0.32 px on sweep-c).
* **Does not**: validate colour *photometry*.  The colour frames in this
  recording are dim (the illuminator is the IR one), so this establishes
  geometry only.  Correct colour reproduction needs a session lit for the colour
  camera, and that is a different validation.
* The corrected profile is a **host-side record**, not an activated
  calibration.  Running the full lifecycle gate suite on it fails, and that is
  correct: those gates judge the *IR rig against the observations*, and the
  available recording is a one-axis sweep (60 views spanning 4 mm, 6° and one
  roll bin).  Measured on that sweep with correctly oriented frames: factory
  cross-camera 5.22 px (the documented factory figure), stereo consistency
  0.291° / 0.263 mm, pose diversity FAIL (4 mm / 6° / 1 bin), observability not
  supplied.  A colour record cannot be validated by IO gates, and an IO rig
  cannot be validated from a one-axis sweep.
  The colour record therefore carries its **own** validation (held-out and
  cross-session reprojection) and is used as a profile record; promoting it to
  an activated calibration still requires a proper session.

  (Two harness mistakes were made and corrected while measuring this: feeding
  the raw colour frame instead of the rotated one, and feeding the raw right IR
  frame instead of its 180° rotation.  Both showed up as ~180° / hundreds of
  pixels and both were mine, not the data's.)

## Reproduction

The validated record is kept at
`~/einstar-private/analysis/colour-fit/profile-colour-cross.json` and used by
passing it as `--profile` to the reconstruction and scanning applications.

```sh
.venv/bin/python tools/calib/fit_colour_extrinsic.py \
  --frames ~/einstar-private/sweep-b/frames \
  --profile ~/einstar-private/einstar-profiles/factory-e10.json \
  --max-views 120 --holdout 0.4 \
  --write-profile ~/einstar-private/analysis/colour-fit/profile-colour-ok.json \
  --validated-status --out ~/einstar-private/analysis/colour-fit/report.json
```

Tests: `tests/test_colour_fit.py` — synthetic recovery of a known transform
(recovered to < 0.5 mm / 0.05°), the real sweep (shipped > 50 px, held-out
< 2 px), the status-promotion rule including both refusal cases, and the
cross-session check on the second recording.
