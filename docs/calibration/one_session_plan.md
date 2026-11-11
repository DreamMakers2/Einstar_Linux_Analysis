# Plan for the single comprehensive calibration session

Status: 2026-11-11.  Based on the first live capture (see
`docs/reconstruction/live_bringup_2026-11-11.md` and
`analysis/sweep-b/REPORT.md`), not on simulations.

## 1. What the current one-axis sweep already gives

Measured on 600 live groups (60 s) and 350 groups (35 s), same settings:

| dimension | current coverage | what it constrains |
|---|---|---|
| lateral translation | x = −99.8…+30.2 mm (130 mm) | board pose, per-frame; a little on the IR baseline |
| distance | z = 370.8…397.9 mm (27 mm) | *nothing separable* — degenerate with focal length |
| out-of-plane tilt | 5.4…19.5° about **one** axis | plate normal in one direction only |
| in-plane roll | fixed at ≈178° ± 1.2° | nothing |
| yaw about the plate normal | 19.2° | weak |
| image-plane position | x 326…710 px of 1280, y 441…458 px of 1024 | nothing at the borders |
| plate area | 12.7–14.6 % of the sensor | — |
| motion | 6.5 mm/s, 1.1 °/s, 2 turnarounds | — |
| distinct poses | 33 at a 5 mm / 0.5° separation | — |

Consequences, measured rather than assumed:

* **Focal length is unobservable.** Scaling `fx` and `fy` together by ±3 %
  moves the recovered distance by exactly ±3 % (376.5 → 399.8 mm) with the
  residual flat (0.385 → 0.429 px, no minimum at the factory value).  Fitting
  intrinsics on this data would invent a calibration, not measure one.
* **Distortion cannot be refined.** The plate never visits the image borders,
  so pose and distortion are not separable; the factory model already fits live
  data at 0.38–0.41 px, so there is nothing to gain here from more sweep views.
* **The IR stereo extrinsic is measurable but the sweep only exposes part of
  it.** A global rigid correction of 0.239° and 1.3 mm reduces cross-camera
  reprojection from 5.18 px to 0.42 px.  The epipolar (Sampson) residual is
  blind to it (0.216 px with the factory E) because the baseline lies almost
  along x — a useful warning against validating stereo with epipolar residuals
  alone.
* **The colour extrinsic is wrong by ≈18 mm** (mostly Y), visible as a ≈49 px
  reprojection error, while the colour intrinsics are good to 0.37 px.

## 2. The pose dimensions the final session must add

Each row is a *measurable* requirement, derived from what is unobservable now.

| # | dimension | required range | what it unlocks | why it cannot be omitted |
|---|---|---|---|---|
| D1 | distance | ≥ 3 bands spanning ≈300–700 mm (≥ 2×) | separates focal length from distance; brings distortion gradients into view | currently degenerate (measured) |
| D2 | out-of-plane tilt, **two axes** | ±30–35° about two orthogonal axes | decouples fx/fy, principal point, plate normal; makes the plate plane constrain pose in 3-D | current tilt is 14° on one axis |
| D3 | in-plane roll | ≥ 4 orientations spread over 0–360° (e.g. 0/90/180/270 ±20°) | principal point, tangential distortion, and independent confirmation of the marker-label orientation | roll is fixed at 178° now |
| D4 | image-plane coverage | plate centre within the central 70 %, plus all four corners and the four edge midpoints (≥ 9 regions) | radial distortion across the field, principal point | plate stays in a horizontal band now |
| D5 | stereo overlap | both IR cameras see all 40 markers at every accepted pose | joint three-camera bundle adjustment | already satisfied, must be preserved |
| D6 | colour participation | colour camera sees the plate at every accepted pose, properly exposed, plus a colour chart in ≥ 3 poses | RGB/IR correspondence, white balance/gamma, colour extrinsic | colour extrinsic currently 49 px off |
| D7 | static dwell (optional but cheap) | ≈0.3 s hold at each pose | averages detector noise (√N), removes residual motion | noise floor is 0.017 mm / 0.02° |
| D8 | scale artefact | a known-length artefact (e.g. a calibrated bar or gauge block pair) imaged in ≥ 5 poses at different distances | settles the ~1 % absolute scale independently of the 28.0 mm plate pitch | the plate cannot validate its own scale |

D1–D4 are *pose* dimensions; D5–D6 are *camera* dimensions; D8 is the only way to
close the scale question, and it is a 30-second addition if an artefact exists.

## 3. Session script — few interventions, maximum information

The rig already has the plate stationary and the scanner on a moving platform.
The session adds **four short passes**, each one a single operator action, with
the software capturing continuously and selecting online:

| pass | operator action (one each) | covers | expected duration |
|---|---|---|---|
| P0 | none — the existing left/right sweep | D5, baseline poses | 20 s (already proven) |
| P1 | move the scanner (or the plate) through three distances, slowly | D1 | 40 s |
| P2 | tilt the plate (or the scanner head) ±30° toward/away and left/right in a slow figure-eight | D2 | 40 s |
| P3 | rotate the plate in-plane to four quadrants (0/90/180/270), briefly pausing each | D3 | 40 s |
| P4 | aim the plate at the four corners and four edge midpoints | D4 | 40 s |
| P5 | place the colour chart and the scale artefact next to the plate | D6, D8 | 30 s |

That is **five physical interventions** (P1–P5) plus one automated pass, about
3.5 minutes of motion in total.  The capture runs continuously; nothing is
recorded "for later checking" — every accepted view is validated online.

## 4. Acceptance — information-driven, not a frame count

The session stops when all of the following hold (no fixed 12/25/40 target):

1. **Coverage**: every one of D1–D6 has its minimum measured range, checked
   from the accepted poses (not from the operator's intent).
2. **Marginal uncertainty**: the 1-σ marginal for each *freed* parameter is
   below its accuracy budget (e.g. fx/fy ≤ 0.05 %, cx/cy ≤ 0.3 px, k1/k2
   ≤ 0.01, stereo rotation ≤ 0.02°, baseline ≤ 0.1 mm, colour extrinsic
   ≤ 0.5 mm), computed from the joint covariance.
3. **Held-out prediction**: views never used in the fit (a distinct pass, and
   the other camera's pixels as in the sweep analysis) reproject at ≤ 0.5 px
   and the cross-camera prediction matches the individual pose fits (the
   sweep's 5.18 px → 0.42 px test is the template).
4. **Stability**: bootstrap over pose groups changes every freed parameter by
   less than half its 1-σ; no parameter drifts monotonically with view order.
5. **Task-space**: reconstruction of the plate at held-out poses has plane RMS
   and repeatability no worse than with the previous profile, in the plate
   frame, on ≥ 20 pose pairs.
6. **Scale**: the artefact length is recovered within its own tolerance; if it
   is not, the calibration is *not* activated and the ~1 % scale issue is
   reported as still open.

Diminishing returns is measured, not guessed: capture continues while the
**worst-case marginal σ** over the freed parameters is still falling by more
than 5 % per 5 accepted views.  With the diversity above, that is expected to
land at roughly 30–50 accepted views; the exact number is whatever the
uncertainty says.

## 5. Model-selection policy (conservative by default)

Freed only when the data supports it, decided by held-out improvement *and*
stability, never by training residual alone:

| stage | parameters | requirement |
|---|---|---|
| base | per-view poses | always |
| +1 | IR intrinsics (fx, fy, cx, cy) | held-out improves ≥ 10 %, coherent across passes, bootstrap drift < 0.5 σ |
| +2 | radial k1, k2 | D1 and D4 satisfied; held-out improves; distortion residual at the borders decreases |
| +3 | IR stereo extrinsic | cross-camera held-out improves, and the correction is stable between passes |
| +4 | colour intrinsics/extrinsic | D6 and an independent colour check pass; otherwise colour stays `unknown` |
| never on this data | full kc5 + skew + tangential | only with corner coverage *and* a demonstrated held-out gain ≥ 10 % |

The factory profile is the fallback at every stage; a candidate profile is only
activated after all six acceptance gates pass, is versioned by the existing
profile store, and can be rolled back in one command.

## 6. What the operator sees

A live checklist (the same dashboard as `tools/liveview`): one bar per
dimension D1–D6 showing the measured range so far, the marginal 1-σ for each
parameter still being estimated, the count of accepted/duplicate/rejected
views, and a single "keep moving / this pose is new / stop" cue.  No numeric
tuning, no frame targets, no manual bookkeeping.

## 7. Explicitly out of scope for one session

* Multi-exposure HDR capture (the vendor's `{MONO:3, RGB:1}` mode) — not needed
  for calibration; it is a scanning-mode question.
* Absolute accuracy certification (ISO/VDI) — needs a calibrated artefact
  protocol beyond D8.
* Hand-eye / scanner-to-platform calibration — requires a fixed external
  reference, which this rig does not have.
