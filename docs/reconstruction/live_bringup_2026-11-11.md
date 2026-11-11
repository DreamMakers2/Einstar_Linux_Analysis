# Live bring-up report — 2026-11-11

First session with the physical Einstar attached to the Linux workstation.
Everything here is labelled by evidence class:

* **HARDWARE** — observed directly on the unit (command output / byte counts).
* **MEASURED** — a statistic computed from the live frames or raw stream.
* **OFFLINE** — produced by replaying the recorded stream offline (same bytes,
  so it re-derives the live result deterministically).
* **OPEN** — not established; explicitly unresolved.

Private artefacts (raw stream, PGMs, factory blob) stay outside this repository.
The factory blob is preserved read-only in a private snapshot with a SHA-256.

## 1. Device and read-only probe

| item | value | class |
|---|---|---|
| USB id | `3267:0003` "SHINING-CAM" (Shining3d) | HARDWARE |
| negotiated speed | High Speed 480 Mbps, bus 1 port 5, bus-powered 100 mA | HARDWARE |
| descriptor quirk | interrupt endpoints declare `bInterval=0`; the kernel repairs it to 7 | HARDWARE |
| firmware | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN` | HARDWARE |
| sensors | 3, each 1280x1024 | HARDWARE |
| factory calibration | 6568 bytes, read via opcode 0x57, four independent reads byte-identical, SHA-256 `557a79…d4e4`, decoded into three camera models with orthonormal rotations (max |R·Rᵀ−I| ≈ 2e-15) | HARDWARE |
| permissions | user-scoped ACL on the device node; no system-wide udev rule added | HARDWARE |
| interface release | after every probe/capture the next open succeeds; no BUSY | HARDWARE |

The probe is read-only: no stream reads, no set commands.  Volatile writes only
happen in the capture path.

## 2. Two wire-format defects found by the bring-up

Both were invisible offline because the simulator reproduced the same wrong
assumption.  Full detail in `docs/protocol/einstar_usb_protocol.md` §3.

1. **No length prefix.**  The stream is `[32-byte header][40,960-byte payload]`,
   one packet per bulk transfer; 9,660/9,660 transfers in a 20 s capture were
   exactly 40,992 bytes.  The documented `[u32 BE len]` exists only as a host
   buffer descriptor in the vendor DLL.  With the wrong framing the parser saw
   358,923,797 parse errors and completed **zero** frames while receiving
   395,982,720 bytes.
2. **The slot counter is 8 bits.**  Header bytes 12..15 wrap 255 → 0 every 256
   trigger groups (measured over 1,200 consecutive groups).  The reassembler
   had used it as a monotonic key, so after 256 groups every frame looked stale
   and capture silently stopped writing while still receiving ~39 MB/s.  The
   fix uses modular (mod-256) ordering plus a rescue from the monotonic u64
   trigger-group counter.  A 600-group run now crosses two wraps with zero
   link-induced loss.

A third, non-protocol defect explained the first dark images: the speckle
projector is **volatile and not enabled automatically**.  The unit was found at
`laser 0 %`, `strobe 0/0` (a previous client had stopped it with lights off).
Calibration frames were near-black (mean 11.0/11.9 DN, only speckle visible,
plate undetectable).  The tool now reads the state in the probe, sets it with
`--laser`/`--strobe`, records it in the manifest, and always returns it to 0 on
exit.  The device is left with trigger counts `0/0`, illuminator `0`, period
200000 µs and unchanged calibration.

## 3. Link, cadence and loss (MEASURED)

| trigger | cadence mean (min..max) | throughput | result |
|---|---|---|---|
| 68 ms | 80.25 ms (79.27..82.98) | 49.3 MB/s | link-limited: the device cannot deliver full groups faster than ~80 ms |
| 100 ms | 100.0 ms (98.9..100.8) | 39.4 MB/s | 600/600 groups over 60 s |
| 150 ms | 149.95 ms (148.99..150.02) | 27.4 MB/s | 20/20 groups |

A three-camera group is 3,932,160 bytes, so the practical USB 2.0 ceiling on
this unit is **≈49 MB/s**, not the 45 MB/s used by the offline model.

Loss over 600 groups / 1,800 frames: 3 frames dropped, all in the first device
group after trigger start (reported as one `incomplete` group, one
`partial_timeout`, one `flush`); 0 transfer timeouts, 0 stalls, 0 transfer
errors, 0 sequence gaps, 0 duplicates, 0 invalid frames, 2 packet-index gaps
(startup only).  The startup transient is deterministic and is why vendor
software discards the first two frame sets.

`slot_id_mismatches = 0` over every group: the three frames of a group carry
the same u64 trigger-group counter **and** the same 8-bit slot id, so grouping
is established from the protocol, not from image similarity.

## 4. Real dataset and live↔replay equivalence (HARDWARE + OFFLINE)

`sweep-b`: 350 groups, 35 s, raw recording 1.38 GB.
`sweep-c`: 600 groups, 60 s, raw recording 2.37 GB.

Replaying `sweep-b`'s raw recording offline reproduces **1,050/1,050 PGMs
byte-identically** (MD5 over every file).  Recording is lossless and the
reconstruction input can be re-derived from the raw stream alone.

## 5. Camera mapping and colour (MEASURED)

* Stream 0 (cam0, IR) must be rotated 180°: plate centroid x = 903 px raw vs
  376 px rotated, while cam1 (no rotation) is at 357 px.
* Stream 1 (cam1, IR) is already in calibration orientation.
* Stream 2 (cam2) is a Bayer mosaic: 2×2 phase spread 8.42 DN vs 0.10/0.15 for
  the mono IR cameras, and it must be rotated 180° after debayer (centroid
  909 → 370 px, matching cam1 at 357 px).
* Colour intrinsics in the factory profile fit the live plate at **0.36 px**
  RMS, but projecting the IR plate pose through the factory colour extrinsic
  is off by **≈49 px** (p95 51–55 px) systematically.  A live estimate of the
  colour↔ir_left transform differs from the factory by 0.19° rotation and
  **18.4 mm translation (almost all in Y, −17.5 mm)**, with per-frame σ of
  0.25/0.41/0.11 mm.  This matches the profile's own `color_status: "stale"`.
  The correction is a candidate only: colour must not be marked `ok` without an
  independent colour validation.

## 6. Plate observation and motion (MEASURED, `sweep-c`)

* 40/40 lattice markers detected in essentially every frame; homography RMS
  0.6–0.9 px.  Large orientation markers are visible but not classified by the
  current detector (see the sweep analysis report for the ambiguity audit).
* Plate distance z = 370.8–397.9 mm, lateral x = −99.7…+30.0 mm.
* Tilt of the plate normal 5.5°–19.4° (the scanner is off-centre on the moving
  platform, so distance and orientation change together).
* Plate marker field occupies 590 × 358 px (46 % × 35 % of the sensor).
  Its centroid sweeps x = 326…710 px while y stays 441…458 px: the sweep is
  essentially one horizontal line in the image.
* Sweep speed 6.46 mm/s median, 6.67 mm/s maximum.  Motion blur during a 1.2 ms
  exposure at maximum speed is **0.032 px**, and sharpness is uncorrelated with
  speed (r = +0.38 in the direction opposite to motion blur).  **Image softness
  on this dataset is optical, not motion-induced.**

## 6a. Factory intrinsics against live imagery (MEASURED)

With the reconstruction profile's own convention (see the note below), the
shipped factory IR/colour intrinsics and 5-coefficient distortion reproduce the
live plate observations to:

| camera | PnP reprojection RMS, factory kc | same with distortion removed |
|---|---|---|
| ir_left | **0.41 px** | 1.06 px |
| ir_right | **0.38 px** | 1.07 px |
| colour | **0.37 px** | 0.88 px |

So the distortion model is doing real work (2.6x better) and there is no
evidence of a live/factory intrinsic mismatch on this unit.

> **Convention trap found here.** The profile JSON stores `skew` as the
> **absolute** pixel term `K[0,1]`, matching the C++ backend
> (`cv::Matx33d(fx, skew, cx, ...)`) and `tools/calib/export_profile.py`.
> `calib.CameraIntrinsics` instead uses a *normalized* skew (`K[0,1] = skew*fx`).
> Mixing them multiplies the term by ~1159 and turns a 0.41 px fit into a
> 10.4 px systematic error.  `calib/recon_profile.py` now loads profiles
> through the correct convention and rejects non-orthonormal rotations.

**The sweep cannot constrain the focal length.** Scaling `fx` and `fy` together
by ±3 % and re-solving the pose scales the recovered plate distance by exactly
the same ±3 % (376.5 → 399.8 mm) while the reprojection residual stays flat
(0.385 → 0.429 px, with no minimum at the factory value).  Focal length and
distance are degenerate on a planar target at (nearly) one distance, so a
"better residual" after an intrinsic fit would be meaningless here — and the
~1 % absolute-scale question cannot be settled this way.

## 7. Stereo extrinsics from live plate poses (MEASURED)

Relative pose cam1 → cam0 over 113 frames:

| quantity | measured | factory | difference |
|---|---|---|---|
| translation (mm) | (−155.51, −1.00, 30.05) | (−155.47, −0.23, 31.14) | ≈1 mm |
| baseline (mm) | 158.39 | 158.60 | 0.2 mm |
| relative rotation | 21.876° | 22.09° | 0.2° |
| per-frame σ | 0.28/0.40/0.21 mm, 0.043° | — | pose noise floor |

Regressing the relative translation against plate velocity gives lags of
−0.6 / −2.9 / +1.5 ms — noise-limited, so **no measurable inter-camera timing
lag** at 6.5 mm/s.  This does not prove simultaneous exposure (the stream
carries a group counter, not a clock); it bounds any effective lag at ≲3 ms.

## 7a. Sweep analysis: trajectory, repeatability, viewpoint diversity (MEASURED)

350-group capture (`sweep-b`), both IR cameras, produced by
`tools/sweep/analyze_sweep.py` (deterministic: two runs byte-identical except
the runtime line; ~14 s wall for 350 groups with 16 workers).  Figures in
`figures/live_2026-11-11/`.

**Detection and label verification.** Plate detected in 350/350 frames in both
IR cameras; 40/40 lattice markers; homography RMS p50 0.66 px (right) /
0.78 px (left); PnP RMS p50 0.38 / 0.41 px.  The four large markers were used
as witnesses of the UV label assignment: identity scored 4/4 on 700/700
camera-frames, `rot180` 0/4, `mirror_v` 0/4 and `mirror_u` 3/4, so **no frame
was ambiguous or flipped**.  This matters because the 40-point lattice is
symmetric under the Klein four-group: the homography/PnP residual alone cannot
detect a mislabelling.

**Trajectory.** x = −99.8…+30.2 mm (130 mm), z = 370.8…397.9 mm, |t| =
385.1…399.9 mm; tilt 5.4…19.5°, pitch 0.8°, yaw 19.2°, roll ≈ −178° ± 1.2°
(the plate hangs essentially upside-down in the image).  Lateral range
subtends 19.1° at the median depth.  **2 turnarounds, 3 legs** (leg 0 −94
frames, leg 1 +209, leg 2 −47; the capture ends mid-leg).  Speed p50
6.7 mm/s.

**Repeatability and noise.** 297 cross-leg nearest-neighbour pairs within
5 mm/1°: translation difference p50 **0.261 mm** / p95 0.458 mm, rotation
p50 **0.045°** / p95 0.158°.  Pose noise floor (local quadratic detrend):
**0.0174 mm / 0.0195°**.  Adjacent frames differ by 0.67 mm / 0.11°, i.e.
motion, not noise.  Revisit scatter is ~15x the noise floor, so repeatability
is limited by real motion between passes and by the pose model (distortion,
plate flatness), not by the detector.

**Distinguishable poses.** Greedy farthest-point selection in
(x, y, z, pitch, yaw, roll, u, v): 146 distinct at 1.25 mm/0.125°, 68 at
2.5/0.25, **33 at 5/0.5**, 17 at 10/1, 9 at 20/2.  88 % of frames have another
frame within 1.25 mm/0.125° and 99.7 % within 2.5 mm/0.25°.  The automatic
selection keeps 33 of 350 frames (90.6 % reduction) at a coverage radius of
0.87 tolerance units.  **Repeated passes over this 1-D trajectory are not
independent geometric viewpoints** and the raw capture is retained
independently of the selection.

**Quality.** Saturation ≥ 250: 0.15 %/0.11 % of pixels; no clipping at 0;
plate-ROI Laplacian variance 802/684 (guidance's IR threshold is 150);
marker edge sharpness 0.450/0.434 px⁻¹ (≈2.2 px blur width) flat to ±2 % over
the 35 s.  Intensity drifted +15 % over the sweep and the Laplacian metric
moved +8 %, but marker sharpness did not — the apparent "focus drift" is
illumination, not focus.

**Factory stereo extrinsic.** Independent plate poses per camera give
|t_rel| = 158.34 ± 0.36 mm (factory 158.56, ratio 0.9986, implying a pitch of
28.04 ± 0.07 mm at 28.0 nominal) and a relative-rotation error of
0.296 ± 0.053°.  The decisive test is cross-camera prediction: pushing one
camera's plate pose through the **factory** extrinsic and comparing with the
other camera's observed pixels gives 5.17 px (L→R) / 4.83 px (R→L), against
0.41/0.38 px for the independent fits.  A single rigid 6-DoF correction
(0.239°, 1.315 mm) restores **0.42 px**.  So the disagreement is a rigid
extrinsic offset, not random error.  The Sampson epipolar residual with the
factory E is only 0.22 px — with a baseline almost along x, a rotation error
moves points *along* the epipolar lines, so **an epipolar-only check is blind
to it**; use cross-camera reprojection.

## 7b. Live stereo reconstruction (MEASURED, production `recon_cli`)

350 groups reconstructed with the factory profile through the unmodified
`recon_cli` (`tools/live_recon/reconstruct_sweep.py`, 297 s wall, 0.384 s wall
per group).  Figures in `figures/live_2026-11-11/`.

| quantity | value (p50, p05…p95) |
|---|---|
| valid depth fraction, whole frame | **42.1 %** (34.7…52.9) |
| valid fraction inside the plate outline | 84.4 % (56.9…99.2) |
| plate plane RMS | **0.237 mm** (0.220…0.246) |
| plane tilt vs the PnP plate normal | 0.58° (0.50…0.66) |
| valid points per group | 137,929 (113,655…173,480) |
| valid points on the plate | 71,910 (49,937…87,015) |
| edge band (3 mm inside the outline) invalid | 32.5 % |
| marker-ring invalid | 12.2 % |

**Pose-normalised repeatability** (each cloud moved into its own plate frame,
so scanner motion is removed): 72 similar-pose pairs across different sweep
legs give nearest-neighbour **p50 0.179 mm / p95 0.456 mm**, plane-offset
difference dz p50 −0.003 mm with σ 0.024 mm.  Pairs at ≥8° different plate
angle (120 pairs) give 0.462 mm / 16.5 mm p95 — the sweep's rotation and
translation are correlated, so those pairs also differ in obliquity.

**Depth bias and the scale gauge.** Stereo places the plate **4.31 mm
(−1.11 %) closer** than the independent marker-based PnP pose
(mean ratio 0.98909, σ 4.8e-4 across groups — extremely repeatable).  The
equivalent implied plate pitch is **27.69 mm** against the nominal 28.0 mm.
That is within a few tenths of a percent of the *CCF-effective* pitch
27.72 mm already documented in `calib/plate.py`, i.e. the reconstruction is
self-consistent with the vendor's own scale convention and the "~1 % scale
discrepancy" is the pitch gauge, not a stereo bug.  It remains unresolved
without an external length standard.  The bias has a real pose dependence
(0.0275 mm per mm of range, r² 0.83; −0.026 mm/deg of obliquity, r² 0.77).

**Pose self-check.** Pushing the right camera's plate pose into the left frame
with the factory extrinsic gives a median 1.82 mm / 0.298° disagreement —
independently reproducing the sweep analysis's 0.24°/1.3 mm rigid offset and
bounding the PnP systematic error.

**Controlled experiments** (one variable at a time, 18 groups, 6 fixed pairs,
229 s; `new on-plane` = fraction of added pixels within ±1 mm of the surface
the baseline already reconstructed):

| change | valid % | plane RMS | new px on-plane | verdict |
|---|---|---|---|---|
| baseline | 35.6 | 0.228 mm | — | reference |
| disparity window 150–1200 | **41.8** | 0.228 mm | **0.94** | the only change that adds coverage with validated pixels |
| full resolution (`scale 1`) | 32.1 | **0.174 mm** | 0.90 | better geometry, 5.5x slower |
| `scale 0.25` | 39.2 | 0.464 mm | 0.06 | coverage bought with wrong matches |
| sensor clipping at 47/255 | 25.8 | 1.728 mm | 0.00 | catastrophic, as expected |
| confidence ≥ 0.2…0.95 | 17.1–25.7 | 0.228 mm | — | removes coverage, does not improve geometry |
| LR tolerance 1.5…3 px | 35.8–36.0 | 0.228 mm | 0.16–0.33 | added pixels are mostly off-surface |
| SGBM block 7 | 36.0 | 0.211 mm | 0.50 | marginal |
| CLAHE / log / gamma / inverse | 30.8–35.6 | 0.212–0.258 mm | 0.49–0.66 | no verified gain |

No configuration beat the baseline plane RMS by more than 0.01 mm while
keeping its new points on the validated surface.  **The 42 % valid fraction is
not a like-for-like improvement over the previously published 17 %**: the old
capture was a close-up of a textured object with 22 % of the left IR stream
saturated, this one is the calibration plate at 0.10 % saturation.  What did
improve demonstrably is the geometry measurement itself: plate plane RMS
0.237 mm against 0.339 mm on the old scene, now measured against a known
target instead of a RANSAC plane, and pose-normalised instead of
frame-to-frame.

## 7c. Which calibration parameters does the sweep constrain? (MEASURED)

`tools/calib_study/observability.py` fits a ladder of models on 72 training
views from `sweep-c` legs 0+2 and validates on geometrically held-out legs
(36 views) and on the whole independent session `sweep-b` (36 views).

| model | free rig params | train RMS | held-out legs | held-out session |
|---|---:|---:|---:|---:|
| M0 factory, poses only | 0 | 1.859 px | 1.805 px | 1.791 px |
| M1 left intrinsics | 4 | 0.400 | 0.408 | 0.393 |
| M2 both IR intrinsics | 8 | 0.366 | 0.376 | 0.360 |
| M3 + k1,k2 | 12 | 0.365 | 0.375 | 0.359 |
| M4 + kc5 + skew | 20 | 0.359 | 0.368 | 0.353 |
| M5 + stereo extrinsic | 26 | 0.359 | 0.368 | 0.353 |
| M6 + colour intrinsics/extrinsic | 37 | 0.409 | 0.375 | 0.360 |

* The step from M0 to M1 is the *rigid offset* (§7a): most of the 1.86 px is a
  single extrinsic disagreement, not per-camera intrinsic error.
* **M6 is worse than M5** — fitting the stale colour record degrades the IR
  calibration, which is why `color_status` stays `stale`.
* The leg split removes temporal duplicates, but every leg covers the *same*
  1-D path, so train ≈ held-out legs demonstrates temporal/session stability,
  **not** extrapolation to unseen poses.  `sweep-b` is the genuine independent
  session test, and it improves the same way.

**Identifiability.** 26 free IR rig parameters, Jacobian rank 26 of 42, scaled
condition number **8.4e5**, scaled marginal-information eigenvalues spanning
7 decades (6.57 … 0, 0, 0).  The weakest direction is dominated by
`right.fy`, `right.tz`, `right.k3`, `right.k2`.  The strongest parameter
correlations are `right.fx↔right.fy` 0.992, `right.fy↔right.tz` 0.988,
`right.cy↔right.rz` 0.997 and `left.k2↔left.k3` −0.984 — exactly the
focal/depth and distortion/skew trade-offs.

**Per-parameter verdicts** (1-σ at the M5 fit, marginalised over all board
poses):

| group | 1-σ | verdict |
|---|---|---|
| IR `fx`, `fy` | 4.7–5.8 px (5.8–7.8 px in the more permissive M6) | **unobservable** |
| IR `cx`, `cy` | 1.6–2.3 px | **unobservable** |
| IR `k1` | 0.0032 (≈0.3 px corner displacement) | **unobservable** |
| IR `k2`, `k3`, `skew` | 0.02–0.05, 0.2 px | **unobservable** |
| `p1`, `p2` (tangential) | 0.15–0.5 px | weak |
| stereo `rx`, `rz` | 0.0005°–0.04° | **constrained** |
| stereo `ry` | ≈0.17° | weak |
| stereo baseline | 0.58 mm | weak |
| colour | — | not fitted (stale record) |

**The focal-length question, settled by experiment.** `corr(fx, mean plate
depth)` = **0.9991**.  A fixed-focal sweep (both IR focals scaled together,
everything else refitted) has its RMS minimum at scale 1.000 and costs only
0.005 px RMS for a ±5 % focal error, while the recovered depth tracks the
focal exactly (354.5 → 393.3 mm for scale 0.95 → 1.05).  Starting the fit from
OpenCV's per-camera initialisation gives **fx = 1271.6 / 957.5 px for two
identical cameras** and a *lower* training RMS (0.346 vs 0.359) at different
focal values — a direct demonstration that a better residual does not select a
focal length at the ~2.6 % level.

**Absolute scale cannot be resolved this way.** Scaling every board point and
every translation by *s* leaves every reprojection *identical*, so no residual
can prefer 28.0 mm over the vendor's CCF-effective 27.72 mm.  §7b's 27.69 mm
implied pitch is therefore reported as a gauge, not a correction.
## 8. What remains open

* Absolute scale: the ~1 % discrepancy between the 28.0 mm plate model and the
  CCF baseline is still unresolved.  A better reprojection residual is not
  evidence of better absolute accuracy.
* Thermal behaviour: the temperature getter returns 0 raw on this firmware, so
  no thermal drift measurement was possible.  Captures are bounded (≤60 s) and
  the illuminator is switched off on exit; unattended long runs are not
  established as safe.
* Simultaneity of the three exposures (see §7).
* Colour activation (see §5).
* The one-axis sweep cannot constrain a full three-camera calibration; see
  `docs/calibration/` and the session plan for the required additional views.
