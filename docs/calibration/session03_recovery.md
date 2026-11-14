# Session-03 recovery — evidence, fixes and the defensible end state

Date: 2026-11-13.  Subject: the irreplaceable physical session
`~/einstar-calibration-03` (52 GB raw recording, 13,195 trigger groups,
39,585 frames, 22.4 min, 297 live-accepted views).  The recording was kept
read-only; nothing here was activated and the factory profile was not modified
(SHA-256 `8259f97c…` before and after).

Evidence directories:
`analysis/session03/` (forensics), `analysis/calibration/coverage/`,
`analysis/calibration/solver/`, `analysis/calibration/lifecycle/`,
`analysis/calibration/offline/`, `analysis/calibration/recovery/`,
`analysis/fullscan/`.  Private frames/metadata are not published.

## 1. What actually happened

| fact | measurement |
|---|---|
| recording | 51,977,409,627 B, SHA-256 `b3053e33…b3020b`; trailer digest `0x21763E9549FB05CA` recomputed and matched |
| raw stream | 1,266,763 records: 1 metadata, 1,266,761 transfers (8 non-Ok/zero-length), 1 trailer; 51,926,738,976 payload bytes |
| groups / frames | 13,195 / 39,585 complete (one incomplete group device_group 13,196 with cameras 1/2 missing) |
| cadence | 101.97 ms mean, 9.807 fps; capture elapsed 1,345.59 s |
| live decisions | 2,004 of 13,195 groups (15.2 %); 297 accepted, 1,707 rejected |
| reject causes | markers 1,395 · assignment 475 · redundant 319 · motion 287 · pose 283 · saturation 83 · blur 41 · plate 39 · contrast 14 · colour 3 |
| reader stopped | last decision at 17:26:14; capture kept writing 2,405 more groups until 17:29:13 |
| final status | `state=stopping`, cleanup readback verified, recording trailer valid, `frames_manifest.json` absent, child SIGKILLed by the 5 s safety watchdog |

The GUI contradiction was real and is now explained exactly: the *reader*
finished at 17:26 (`SOURCE ENDED`), the *scanner child* kept recording for
180 s, and Advanced Diagnostics correctly read the live child (running,
9.8 fps, laser 100 %, >12,000 groups).  `WizardController.cancel()` did not stop
the capture; window shutdown did, 180 s later.  The C++ child had already
verified device cleanup and written the `.e1r` trailer, then tried to re-read
and fully verify 52 GB inside the 5 s watchdog budget and was SIGKILLed.

## 2. Why the wizard reported 5/8

Readiness actually had **two** unsatisfied dimensions, D4 and D7, over 297
accepted views (the other six, including D8, were satisfied).  It was displayed
as 5/8 because the GUI projection emitted D7 twice (`quality` and
`stationarity`) and dropped D8; the honest pre-fix count is **6/8**.

### 2.1 D4 — the right-hand image corners (recoverable)

* Accepted-view image cells: `[9, 30, 0, 38, 198, 5, 2, 15, 0]`; top-right 0,
  bottom-right 0.
* Guidance defect: `_families()` generated all four corners whenever any corner
  was missing, without excluding already-satisfied cells, and effort-sorted
  them from the current pose.  While only TR/BR were missing it asked for
  top-left 54 times and bottom-left 44 times versus TR 44 / BR 17 (61.6 % of
  corner instructions wasted).  It also always led with the distance carrier
  (“Move to 380–460 mm (further, N mm)”) even though D1 was satisfied
  (`[32, 97, 152]`), and the priority reported to the GUI was `distance` for
  297/297 instructions.
* The operator was physically able to reach the corners: at 475 mm the corner
  cell begins 94.5 mm to the right; the corrected target (553.6 mm, lateral
  149.5/116.3 mm, z 520.2 mm) keeps 40 left / 36 right markers and lands at
  (946.9, 787.9) px.  A 1,568-case tilt/roll sweep at 340/420/510 mm keeps
  ≥ 28 markers in each IR camera in 100 % of cases.
* The complete raw recording contains 108 top-right and 81 bottom-right
  left-centre groups (486 corner observations by marker centroid); 3,943
  groups pass a full both-IR assignment with ≥ 32 markers and PnP RMS ≤ 1 px.
* Four high-quality corner views were recovered and refined:
  groups 89 (TR, 35/32), 5682 (TR, 36/34), 4019 (BR, 38/37), 6241 (BR, 40/40).
  With them, D4 is satisfied: regions 9/9, corners TR 2, BR 2, TL 9, BL 2.
  **No new corner capture is needed.**  Groups 72/89/92 were rejected live by
  the old absolute-displacement motion gate even though their exposure blur was
  ≤ 0.3 px; groups 4019/6241 were never decided at all because of the backlog.

### 2.2 D7 — stationarity and quality

Quality was never measured: the old D7 read `quality["saturation_frac"]` and
`["focus"]`, keys that do not exist; the real values are nested under
`quality["metrics"]["left.focus"]` etc.  Every view therefore “passed”.  The
corrected measurement uses the active IR cameras: 297/297 accepted views pass
(focus min 171.8, saturation max 0.00995; tex is reported but not gated by D7
because its scale differs and colour participation is D6).

Stationarity was measured from pose deltas between *accepted* views, which are
sparse (mean 3.7 s apart) and biased towards motion; dwell frames were rejected
as redundant before acceptance.  The old “5 of 297” was an artifact three times
over: wrong sample set, a 3.7 s denominator, and an off-by-one flag reuse.

The honest measurement needs consecutive observations, a real duration, and a
distinct pose.  Decision-rate data alone is not enough: a single decision with
a long `dt` only bounds net displacement, and the raw 10 Hz trace inside those
gaps moves at 5–1,800 mm/s.

| source | stationary observations | qualified dwell episodes | verdict |
|---|---|---|---|
| 297 accepted views | 0 | 0 | fail |
| 2,004 live decisions | 138 | 4 (4.1, 1.2, 1.0, 49.0 s; 7 single samples disqualified) | fail (4 < 6) |
| complete raw 10 Hz stream | 2,333 pairs | 2 fully qualified (4.6 s, 225.4 s) plus one borderline ~0.3 s | fail (2–3 distinct poses < 6) |

The 225 s tail dwell is the post-stop segment (groups 10,940–13,194); it is one
pose, not six, and largely duplicates the opening pose.  **The operator had
only two or three genuine distinct holding poses.**  The old 5 was wrong, but
the corrected conclusion is still a genuine data limitation: D7 is not
satisfied.  It is not a software defect that can be fixed by reprocessing.

## 3. Reprocessing the complete recording

The recording is fully readable without loading it: `einstar-stream-frames`
validates the header/record CRCs and the FNV trailer while streaming, with
O(1) memory.  Verified on the real file:

| operation | wall | throughput | peak RSS |
|---|---:|---:|---:|
| full verify-only (52 GB) | 76.2 s | 681 MB/s | 5.9 MB |
| full replay stats (13,195 groups, 39,585 frames) | 98.9 s | 525 MB/s | 103 MB |
| full-corpus feature cache (20 workers) | 763.9 s | 17.3 groups/s | max worker 1.24 GB, 97 MB parent |
| stage-1 retention (1,200 groups / 4.5 GB) | 143.6 s | 624 MB/s | ~100 MB |

The feature cache (`analysis/calibration/offline/features/features.jsonl`,
13,195 rows, 91.5 MB) holds real host timestamps, markers, ROI quality and a
left-camera PnP pose per group.  Replayed frames are byte-identical to the live
capture where both exist (360/360 files), and the accepted views are reproduced
exactly (left frames match the live hashes; right/colour are the same raw bytes
before the source's rotation/debayer).

Stage-1 found **3,943** groups with full both-IR assignment, ≥ 32 markers and
PnP RMS ≤ 1 px — far more evidence than the 297 live accepts.  A 1,200-view
diverse subset is retained (`selection_summary.json`).

An independent selection comparison then fitted the same hard-PP model to three
sets with an identical protocol (`analysis/calibration/selection/`):

| selection | views fitted | RMS px | held-out px | condition (scaled) | σ(fx) px |
|---|---:|---:|---:|---:|---:|
| (a) live accepted, culled | 293 | 0.651 | 0.770 ± 0.019 | 1,804 | 0.203 / 0.182 / 0.197 |
| (b) 150 diverse from the accepted | 145 | 0.700 | 0.837 ± 0.065 | 1,772 | 0.268 / 0.238 / 0.242 |
| (c) 200 diverse from the full corpus | 165 (35 culled) | 1.002 | 1.204 ± 0.089 | 804 | 0.220 / 0.198 / 0.208 |

(c) reaches D4 (9/9 regions, 21/5/14/17 corner views) and the best conditioning,
but has worse residuals and held-out error: 17.5 % of its corpus views are
unusable outliers and many 560-800 mm views are small in the image.  More views
are not automatically better; (a) remains the best validated candidate, and the
right change is to *add* corner/edge views to the accepted set, not replace it.
The selection comparison's D5 is
not comparable because the feature-cache adapter's witness flag marks many
live-assigned views ambiguous; the live controller's own assignment verdict is
used for the authoritative coverage numbers above.  D7 fails in all three.

## 4. Does the recovered data close the calibration?

Two independent analyses were run on the exact live observations and on the
recovered session:

* **Live 297 views, hard-PP model** (factory distortion held, `cx/cy` fixed
  bit-identically at factory, `fx/fy` + right/tex extrinsics free, per-view
  poses nuisance).  Four gross-outlier views `[6, 36, 129, 161]` culled with
  the production `> max(2 px, 3×median)` rule; 293 views fitted.
  RMS 0.6514 px, per-view median 0.469 / p90 0.953 / max 1.93; held-out
  (3 folds, free PnP pose) 0.770 ± 0.019 px; epipolar Sampson 0.333 px vs
  factory 0.465 px; IR rotation 22.130° vs 22.138°; baseline 160.014 mm vs
  factory 158.557 mm (**+1.457 mm, +0.92 %, σ 0.018 mm**); RGB translation
  122.878 vs 125.318 mm; RGB epipolar 0.63 px vs factory 31.3 px (the shipped
  colour extrinsic cannot explain these images).  Gate passes: 17 constrained,
  1 weak (`tex.tz` 0.064 vs 0.06 mm), 0 unobservable, condition 1,804,
  rank 18/18, model-sensitivity pass.  `σ(fx)=0.20 px`; split-half
  reproducibility is 1.2–2.3× that, so the candidate is defensible at the
  **standard** target, not at **high**.
* **Recovered session: 297 + 4 corner views** (301), same model.  RMS 0.673,
  median 0.469 / p90 1.027, held-out 0.799 ± 0.029, epipolar 0.337 px,
  condition 1,821, gate passes.  The corners satisfy D4 and add no measurable
  calibration gain (they slightly raise held-out error, mainly the colour
  camera); the numerically best candidate remains the 293-view fit.

The controller's own end-to-end finish on the recovered session produces a
candidate but its quality status fails (RMS inflated by the un-culled texture
outlier) and coverage is not ready; the GUI correctly **locks Finish** at
7/8.  The analysis candidate is written only under
`analysis/calibration/solver/final/`; it was never activated.

**Verdict:** the candidate is internally consistent and independently
validated at the standard target.  It is **not** activatable: D7 fails, and
absolute scale is unvalidated.

## 5. Diagnostics: what the numbers meant

| diagnostic | cause | status |
|---|---|---|
| condition 9.83e+03 | unit-free scaled condition number of the 33-parameter rig marginal information; threshold 1e7 | informational, never blocked |
| rank 33/33 | `rank_rtol=1e-10` (1e-20 relative eigenvalue) over the same matrix | misleading as an observability count; it reports full rank even with 6 unobservable distortion parameters |
| weak focal length | covariance bug: the gate inverted the unscaled Schur complement instead of the rig block of the published covariance, inflating σ by ~2.8× (0.735 vs 0.20 px) | fixed; gate σ now equals `param_std` |
| unknown principal point | `cx/cy` held by the controller's 1 px prior; the gate has no verdict for held parameters | expected; the 2 px MAP drift of a soft prior was eliminated by hard-fixing the factory value |
| unobservable radial | with all five Brown-Conrady terms free, `k2/k3` on all cameras are 5.8–7.1× the corner-pixel budget | genuine; do not free |
| `freed_parameters` failed | the live model freed distortion at 8 views and never revisited it | genuine; correctly blocking |
| `model_sensitivity` failed | SciPy FD with `max_nfev=400` at ~1,460 parameters never completed one Jacobian and compared the initial guess with itself | fixed; converged analytic refit gives −0.014 %, so holding distortion is correct and the gate now passes |

## 6. Acquisition lifecycle and guidance fixes

* One authoritative `AcquisitionEvidence` state machine
  (`idle → starting → running ⇄ paused → stopping → stopped_verified |
  stopping_unverified | failed`).  `verified` requires child termination **and**
  device readback (trigger 0/0, laser 0) **and** recording finalization
  (status `stopped` + manifest).  A stopped reader no longer implies a stopped
  scanner; the observed session is correctly reported
  `stopping_unverified` with the real reasons, not “safe”.
* `cancel()`/`finish()`/GUI close/worker end all stop the child first;
  `SessionWorker` stops and verifies the source in `finally`.
* The C++ stop path no longer re-reads 52 GB inside the 5 s watchdog: `auto`
  uses full verification only for simulated/small recordings (≤ 512 MiB) and a
  bounded 64+72-byte trailer check for large real files, recording
  `digest_verified=false` honestly.  Full verification stays available via
  `einstar-stream-frames --verify-only`.
* Guidance now asks for the actual deficient cells (bottom-right first for the
  recorded state), never an already-covered corner, and never leads with
  distance when D1 is satisfied.  `_ProgressAdvisor` recommends stop/offline
  analysis after 60 views + 180 s / 90 views without a new dimension, corner or
  region, and flags a repeatedly-unreachable target.
* Decision persistence was writing 6.98 MB per decision (~14 GB over the
  session, ~92 ms measured) and stalled the loop during solves (max 14.2 s).
  Accepted views still persist immediately; reject-only traffic is debounced,
  transitions force a flush.  Manifest 169→21 ms, session 169→43 ms at the
  297-view payload; estimated write time 678→57 s.  This does not change the
  deliberate one-in-flight newest-frame policy, which is now reported as
  `evaluated` vs `recorded_only`.
* GUI: “SOURCE ENDED” removed; verified/unverified/running-reader states are
  distinct, diagnostics show reader and scanner independently, and
  `ui_metrics.json` is written after stop/verification.

## 7. Real-data validation performed

* 398-frame GUI replay of the 297 accepted + four recovered corners + three
  dwell runs through the actual window/controller: corners accepted, D4
  satisfied, GUI reached **7/8** and correctly refused Finish because D7 was
  missing; no candidate was written from the GUI path.
* Resume of the reconstructed 301-view session through the real GUI:
  7/8, missing only `stationarity (dwell intervals)`.  This also fixed a real
  resume defect: resuming used to rebuild neither the coverage views nor the
  D7 observation stream (a resumed 297-view session reported no coverage at
  all).
* Full-corpus streaming replay/verify numbers above; C++ ctest 14/14
  (including 8 streaming cases / 281 checks), Python offline tests 6/6.

## 8. What is still missing before activation

1. **D7 — four short stationary holds.**  The existing evidence has only two
   to three distinct genuine dwell poses (plus the long post-stop tail at one
   pose).  Append four ~0.3–0.5 s holds at distinct board poses to the existing
   session (resume; no repeat of the 297-view capture).  This is a specific,
   minimal measurement: ~2 s of scanning.
2. **Absolute scale.**  No length artefact was imaged.  The +0.92 % IR
   baseline difference (1.457 mm, σ 0.018) is indistinguishable from the
   28.0 vs 27.72 mm effective pitch question; a known-length artefact (or a
   vendor metrology sheet) is required.  D8 remains optional in the coverage
   spec, but the candidate's `scale` stays `unvalidated`.
3. **A decision on the factory principal point/distortion.**  They are held at
   the factory values by construction, not measured here.  Freeing them would
   need the recovered field-edge evidence plus an independent PP/scale check;
   the corner counterfactual improves `cx` σ by 43 % and `k1/k2` by 56/82 %,
   but focal length by nothing.
4. No automatic activation, ever: even a READY session writes a candidate and
   `validation: pending`; activation stays an explicit lifecycle step.

## 9. Bottom line

The 297 accepted views are enough reliable geometry for a defensible
**standard-target** calibration with factory distortion/principal point held.
The complete recording contains much more usable evidence (3,943 eligible
groups; four refined right-corner views that close D4).  It does **not**
contain six distinct stationary holds, so D7 — the remaining gate — is a
genuine, small data gap, not a software defect.  The software no longer
misreports the scanner, no longer loses the D7 evidence on resume, no longer
rejects sharp corner frames on absolute displacement, and no longer calls a
1 px-prior MAP principal point “held”.  With four short holds and a scale
artefact, the same session could be completed without repeating the physical
calibration.
