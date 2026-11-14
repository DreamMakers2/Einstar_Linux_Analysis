# EXStar calibration — frame-by-frame analysis of the reference screen recording

Source: `screencapture/ScreenCapture_EXStar_Calibration.mp4` (read-only, not committed).
Machine-readable companion: [`analysis/exstar_reference/video_timeline.json`](../../analysis/exstar_reference/video_timeline.json).
Representative frames: [`analysis/exstar_reference/frames/`](../../analysis/exstar_reference/frames/) (14 full frames + 3 detail crops, ≈600 KB total).

Written 2026-11-14 by the video/UX analysis agent. Every timing below was measured
from the decoded frames, not estimated; every claim is tagged **observed**,
**inferred** or **not observable**. Frame numbers are 0-based at 30.000 fps, so
`t = frame / 30`.

---

## 1. What the file actually is

### 1.1 Stream facts (`ffprobe`, exact)

| property | value |
|---|---|
| container | MP4, `isom`, minor_version 512, `isomiso2avc1mp41` |
| duration | **66.400 s** (1992 frames at exactly 30/1) |
| video | H.264 Main, `avc1.4d4029`, 1920×1080, `yuv420p`, 8 bit, progressive, 3 295 815 bit/s |
| B-frames | 0 |
| audio | AAC LC, 48 000 Hz, **stereo**, 66.388 s, 192 005 bit/s |
| container tags | `encoder=https://clipchamp.com`; `comment=Create videos with https://clipchamp.com/en/video-editor - free online video editor, video compressor, video converter.`; **no** `creation_time`, no capture-tool handler name |
| picture area | the app content occupies roughly y = 110…960 of the 1080 lines; the rest is black |

### 1.2 Audio: the track is digital silence

**Observed.** The AAC track decodes to 1 062 200 samples per channel that are all
exactly zero.

* `ffmpeg -af ebur128`: integrated loudness **−70.0 LUFS**, LRA 0.0 LU, true peak **−inf dBFS**.
* `ffmpeg -af silencedetect`: `silence_start: 0` … `silence_end: 66.3875`.
* Independent decode of each channel to WAV: `max|sample| = 0`, `0` non-zero samples, for c0 and c1.

There is therefore **no speech, no narration, and no UI sound** in this file. The
brief asked whether speech carries instructions — it does not, because there is
nothing on the track at all. No transcript can be extracted and none is invented
here. Audio also cannot be used to align UI events, which is why the timeline is
built from pixels only.

### 1.3 Is the file edited or trimmed? — **YES** (high confidence)

The 66.4 s file is **not** a continuous raw screen capture of one session.

Evidence, in order of strength:

1. **Container provenance (observed).** Encoded by `https://clipchamp.com`, a consumer
   video editor, with its promotional comment and no `creation_time`. Whatever else
   is true, the bitstream has been through an editor and re-encoded (3.3 Mbit/s,
   Main profile, no B-frames — an editor export profile rather than a typical capture
   profile).
2. **It cannot span the session it depicts (observed on screen + supplied filesystem
   evidence).** The video contains *both* completions: the geometric
   `Calibration succeeded` modal at **48.600 s** and the
   `White balance calibration successful` modal at **60.633 s**. On the read-only
   Windows partition those two commits are **102.4 s apart**
   (`LeftCCF/RightCCF/TexCCF.txt` 00:13:11.94; `wbdata.txt` 00:14:54.31). The video
   shows **12.03 s** between them. About **85–90 s of the session is missing from
   the file.**
3. **A progress bar that never finishes (observed).** The `Calibrating` dialog rises
   smoothly and monotonically from 1 % (46.87 s) to **83 % at 48.50 s** and the last
   visible value is 85–88 % at 48.53 s; two frames later the dialog is gone and the
   success modal is up. No frame shows 100 % or the completion of that bar
   (≥ 0.2 s of frames missing if the bar is linear to 100 %).
4. **A second bar that stops at ~22 % (observed).** `Quit calibration` advances from
   8 to 86 px of a ≈395 px track between 64.40 s and 65.23 s, then **holds/pulses at
   84–86 px for 0.47 s** and is replaced by `Scan Initializing…`. ~78 % of that bar
   is never shown.

What is **not** evidence of editing, although it looks like it:

* 448 of 1991 consecutive frame pairs are exactly identical — but they are
  concentrated in genuinely static screens (the two reminder modals hold 124 and
  103 identical frames). Static UI produces duplicate frames in any encoder.
* There is no 60→30 fps duplication pattern, so there is no whole-file frame-rate
  conversion.

What cannot be determined:

* Whether any segment is also **time-compressed** rather than cut. The only clocks
  in the picture are EXStar's own progress bars, whose real-time rate is unknown.
* **Where** the missing time was. Both success modals are pixel-static, so a cut
  inside them leaves no trace at 30 fps. The arithmetic (§9.3) says the loss is
  between the geometric success modal and the end of white balance.
* The only absolute time on screen is the header at 65.77–66.40 s:
  **`Nov.14 - 00:13` + green check** under *Calibration*. It agrees with the
  filesystem's 00:13:11.94 at minute resolution, but it is a *record timestamp*, not
  a running clock — it still reads 00:13 after the white-balance stage, which the
  filesystem dates to 00:14:54.

> **Consequence for the "about one minute" claim.** The video is 66.4 s, and the
> geometric acquisition in it is plausibly real-time (25 views × 1.87 s, §3.2).
> But the interval from the geometric commit to the white-balance commit is
> **102.4 s on disk**, while the clip devotes **12.0 s** to the equivalent span. So
> "EXStar needs about a minute including white balance" is true *of the edited clip*
> and false *of the session*: the interaction is ≈49 s of geometry plus something
> like 1.5 min of white balance, on top of ≈2.5 min of startup/setup that the clip
> does not contain at all.

---

## 2. Screen layout and vocabulary (all observed)

The window is the Windows **EXStar** app with a dark theme.

```
┌ EXStar ────────────────────────────────────────────────────────────── _ □ × ┐
│ SHINING 3D │ Device    │ Calibration │ Scan  │ Post-Processing │ Measurement │   ← 5 top tabs,
│            │ Einstar   │ In Progress │  -    │       -         │     -       │     status text under each
├────────────┴─ Calibration ─┬─ White Balance ─┐                              │   ← 2 sub-tabs
│  ┌──────────────┐  Height(0/5) ┌ 1│2│3│4│5 ┐ │  instruction text            │
│  │  live view   │  [ ][ ][ ][ ][ ]          │ │  3-D guidance illustration   │
│  │ (plate +     │                           │ │  "Calibration Board No.      │
│  │  reticles)   │                           │ │   <redacted-part-number>"        │
│  └──────────────┘                           │ │                              │
└──────────────────────────────────────────────┴───────────────────────────────┘
```

* **Top tabs:** `Device` / `Calibration` / `Scan` / `Post-Processing` / `Measurement`,
  each with a status line underneath (`Einstar`, `In Progress`, `-`). A progress rail
  with a dot per tab runs along the top.
* **Sub-tabs:** `Calibration` and `White Balance`.
* **Step chevrons `1 2 3 4 5`** at the top of the settings pane; the active one is blue.
* **Distance widget (Calibration):** five stacked outlined boxes plus the caption
  `Height(N / 5)`.
* **Distance widget (White Balance):** **seven** stacked outlined boxes with no
  caption; the **middle (4th)** is the one that ticks (see
  `frames/15_detail_wb_pane.jpg`).
* **Live view:** a white plate with an 8×8 dot grid, a translucent **grey target disc**
  and a translucent **blue current-position disc**, each with a centre dot.
* **Right pane:** instruction text (4 short lines), a 3-D illustration of the scanner
  over the plate with a blue up-arrow, a red dashed vertical reference, and an angle
  badge (**90°** on step 1, **30°** on steps 2–5), and the board number.

**Observed, and important:** the plate graphic is **pixel-static**. The dot-grid pitch
is exactly 38 px at 960-wide (76 px at 1920) for the whole 47 s, and cross-correlation
of the dot mask against the 0.5 s frame is **1.000 at 0 px offset** at 2.0 s and
≥ 0.99 everywhere else. A raw camera view of a plate observed by a scanner that is
being moved would change scale and perspective. The pane is therefore a
**normalised/rectified rendering (or a synthetic plate diagram)**, not the raw camera
image. During white balance the same pane is a plain white field with only the two
reticles — consistent with the board's blank reverse side. *(Interpretation; the
observation is the static pitch and the 1.000 correlation.)*

The large grey dots in the grid are also static in every frame, so they are part of
the printed pattern (coded fiducials), not a detection-confidence overlay.

---

## 3. Timestamped workflow state table

Durations are `end − start` at 30 fps. Frame ranges are inclusive.

| # | start | end | dur | state | what the operator did | what the app showed | how it ended |
|---|---|---|---|---|---|---|---|
| 0 | 0.000 | 2.533 | 2.53 | geometric, step **1/5**, `Height(0/5)` | held the scanner over the plate, nudging it into position | blue disc offset from the grey target disc; `Please, make sure you are pointing at the target circle green.` at 2.367–2.500 | automatic: first tick |
| 1 | 2.533 | 4.033 | 1.50 | step 1, `Height(1/5)` | held still / crept upward | **green disc + check**; box 3 ticked | green lost at 4.033 |
| 2 | 4.033 | 4.933 | 0.90 | step 1 | repositioned | blue again; 2-frame banner at 4.63 | — |
| 3 | 4.933 | 5.600 | 0.67 | step 1 | moved the scanner **closer** | `Too Far` / `Hold the scanner steadily and move it downwards.` | banner cleared |
| 4 | 5.600 | 7.933 | 2.33 | step 1, → `Height(5/5)` | walked up through the remaining marks | green at 6.93, ticks 4 and 5 | step complete |
| 5 | 7.933 | 8.733 | 0.80 | step 1 complete | waited | `Height(5/5)`, all five green | chevron 2 |
| 6 | 8.733 | 11.667 | 2.93 | step **2/5** (30° tilt), `Height(0/5)` | re-oriented the scanner to the 30° pose | chevron 2 blue, instruction unchanged, `Please make sure…` at 10.43 | first tick |
| 7 | 11.667 | 19.867 | 8.20 | step 2 → `Height(5/5)` | swept up through 5 marks (order 4,5,3,2,1) | green/blue alternation, 5 ticks | chevron 3 |
| 8 | 19.867 | 28.100 | 8.23 | step **3/5** | re-oriented, swept 5 marks (order 2,3,4,5,1) | as above | chevron 4 |
| 9 | 28.100 | 36.867 | 8.77 | step **4/5** | re-oriented, swept 5 marks (order 2,3,4,5,1) | as above | chevron 5 |
| 10 | 36.867 | 46.867 | 10.00 | step **5/5**, last view | re-oriented, swept 5 marks (order 2,3,4,5,1); last accept 46.367 | `Height(5/5)`, chevron 5 blue | automatic: solve starts |
| 11 | 46.867 | 48.600 | 1.73 | **geometric solve** | none — no button, no cancel affordance | `Calibrating`, bar + % bubble: 1 %→83 % (last visible 85–88 %) | modal |
| 12 | 48.600 | 52.967 | 4.37 | **success modal 1** | read it, then clicked `Next` at ≈52.97 | `Reminder / Calibration succeeded / … / Next`, backdrop dimmed | click |
| 13 | 52.967 | 59.733 | 6.77 | **white balance**, 7-block widget | turned the board white side up (not visible), aligned, moved up to the middle block | white field + reticles; `Too close` at 56.67, `Please make sure…` at 57.07 | automatic capture |
| 14 | 59.733 | 60.633 | 0.90 | WB capture | held | middle block ticks green | modal |
| 15 | 60.633 | 64.400 | 3.77 | **success modal 2** | read it, then clicked `Next` at ≈64.40 | `Reminder / White balance calibration successful / … / Next` | click |
| 16 | 64.400 | 65.733 | 1.33 | **quit / cleanup** | none | `Quit calibration` + bar (reaches ~22 %) | auto |
| 17 | 65.733 | 65.767 | 0.03 | transition | — | `Scan Initializing…` (one frame) | auto |
| 18 | 65.767 | 66.400 | 0.63 | **Scan tab** | — | `Calibration  Nov.14 - 00:13 ✓`, `In Progress` under Scan, `New project group` / `Open project group` | video ends |

*Two rows are partly inferred.* The modal dismissals at 52.967 s and 64.400 s are
**user-triggered** (the screen changes to the next stage in one frame and the modal's
only control is `Next`), but the pointer itself is not visible at 52.967 s — it is
first seen at 53.47 s — so the click is not directly observed, only its effect. The
"operator action" column for rows 12 and 15 therefore describes what must have
happened, not something visible in a frame.

### 3.1 Per-step summary

| step | start | end | dur | angle badge | first tick after step start | last tick | tail before next step | ticks |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.000 | 8.733 | 8.73 s | **90°** | 2.53 s | 7.93 s | 0.80 s | 5 |
| 2 | 8.733 | 19.867 | 11.13 s | 30° | 2.93 s | 19.07 s | 0.80 s | 5 |
| 3 | 19.867 | 28.100 | 8.23 s | 30° | 3.13 s | 27.30 s | 0.80 s | 5 |
| 4 | 28.100 | 36.867 | 8.77 s | 30° | 2.27 s | 36.07 s | 0.80 s | 5 |
| 5 | 36.867 | 46.867 | 10.00 s | 30° | 2.70 s | 46.37 s | 0.50 s | 5 |

The 0.80 s tail is remarkably constant — it is an app-driven transition (the step is
closed and the chevron advances), not operator hesitancy.

### 3.2 All 25 accepted views

Ticks are listed as `t (s) → box number` (box 1 = top). Order is the order in which
the five marks of that step were filled.

| step | ticks | order |
|---|---|---|
| 1 | 2.53→3, 3.93→2, 4.53→1, 6.93→4, 7.93→5 | 3,2,1,4,5 |
| 2 | 11.67→4, 11.77→5, 15.97→3, 16.57→2, 19.07→1 | 4,5,3,2,1 |
| 3 | 23.00→2, 23.10→3, 24.20→4, 25.20→5, 27.30→1 | 2,3,4,5,1 |
| 4 | 30.37→2, 30.77→3, 31.77→4, 33.17→5, 36.07→1 | 2,3,4,5,1 |
| 5 | 39.57→2, 41.27→3, 42.77→4, 44.07→5, 46.37→1 | 2,3,4,5,1 |

**25 accepted views in 46.867 s = 1.87 s per accepted view.** Five different fill
orders were accepted, which shows the app does **not** require the marks in a
particular order (inference from the observation that all five orders completed).

Alignments per step settle into a rhythm: 2.27–3.13 s to get the new orientation
aligned, then the five marks at intervals of **0.10–4.20 s (median 1.35 s)**, then an
automatic 0.80 s step close. The short intervals (0.10 s, 0.40 s) are single marks
taken almost back-to-back; the long ones (4.2 s, 2.9 s) are where the operator
repositioned or lost alignment.

### 3.3 Banner events (every one, with text)

| start | end | message |
|---|---|---|
| 2.367 | 2.500 | `Please, make sure you are pointing at the target circle green.` |
| 4.633 | 4.700 | `Please, make sure you are pointing at the target circle green.` |
| **4.933** | **5.600** | **`Too Far`** / **`Hold the scanner steadily and move it downwards.`** |
| 10.433 | 10.633 | `Please, make sure you are pointing at the target circle green.` |
| 14.167 | 14.633 | `Please, make sure you are pointing at the target circle green.` |
| 14.767 | 15.033 | `Please, make sure you are pointing at the target circle green.` |
| 17.967 | 18.233 | `Please, make sure you are pointing at the target circle green.` |
| 18.567 | 19.033 | `Please, make sure you are pointing at the target circle green.` |
| 21.700 | 21.867 | `Please, make sure you are pointing at the target circle green.` |
| 28.667 | 28.833 | `Please, make sure you are pointing at the target circle green.` |
| 35.267 | 36.033 | `Please, make sure you are pointing at the target circle green.` |
| 37.367 | 37.533 | `Please, make sure you are pointing at the target circle green.` |
| 54.033 | 54.200 | `Please, make sure you are pointing at the target circle green.` (WB) |
| **56.667** | **58.200** | **`Too close`** / **`Hold the scanner steadily and move it upwards.`** (WB) — alternates with the one-line message inside the interval: 56.67–57.00 `Too close`, 57.07–57.80 `Please make sure…`, 57.87–58.20 `Too close` |

Only **two** corrective distance messages occur in the entire recording (`Too Far`
once, `Too close` once). Everything else is the generic "point at the target" prompt.

---

## 4. Every instruction shown, verbatim

Transcribed from full-resolution crops with `tesseract` and checked by eye. Region
coordinates are for the 1920×1080 frame.

1. **Calibration (all five steps)** — right pane, x 1195–1890, y 281–410; visible
   0.000–46.8 s:

   > Note: Please place the calibration board on a horizontal surface.
   > Hold the device as shown in the figure below. Move it slightly until the dots
   > are aligned and turn green.
   > Move the scanner vertically upwards until all distance marks are ticked.

2. **White Balance** — right pane, x 1195–1890, y 281–439; visible 52.967–60.6 s:

   > Please turn the calibration board to the white side, place it horizontally. Hold
   > the scanner as shown in the figure below until the dots are aligned and turn
   > green. Then move the device vertically upward to the middle marked block.
   > The white balance calibration is competed.

   *(`competed` is what the application renders — OCR with several page-segmentation
   modes and a direct visual read agree. It appears to be a typo for "completed".)*

3. **Reminder modal 1** — centred, 48.600–52.93 s:

   > Reminder
   > **Calibration succeeded**
   > Once the calibration is finished, it is recommended to store the plate carefully
   > to avoid scratches or damage to the plate.
   > `Next`

4. **Reminder modal 2** — centred, 60.633–64.37 s:

   > Reminder
   > **White balance calibration successful**
   > Once the calibration is finished, it is recommended to store the plate carefully
   > to avoid scratches or damage to the plate.
   > `Next`

5. **Progress dialogs:** `Calibrating` (46.867–48.57 s), `Quit calibration`
   (64.400–65.70 s), `Scan Initializing…` (65.733 s). None has a Cancel button.

6. **Scan tab** (65.767–66.400 s): `New project group`, `Open project group`.

7. **Persistent identifiers:** `Height(N / 5)`; chevrons `1 2 3 4 5`;
   `Calibration Board No. <redacted-part-number>`; header `Nov.14 - 00:13 ✓`.

Everything else in the live view is graphical. No other message could be found in
any of the 1992 frames: the banner strip was colour-segmented over the whole clip
and every run was OCR-ed.

---

## 5. Pose-by-pose reconstruction — what was actually requested

**Observed from the illustration and the chevrons.** The calibration asks for **five
scanner orientations**, drawn in the 3-D illustration:

* **Step 1 — 90°.** Scanner axis normal to the plate, head pointing straight down.
  The angle badge reads `90°`.
* **Steps 2–5 — 30°.** Scanner tilted 30° from vertical; the four renders show the
  body at four different rotations about the plate normal (the illustration's
  scanner orientation differs in each step while the badge stays `30°`).

**Per orientation, five distance marks.** The instruction is explicit: *"Move the
scanner vertically upwards until all distance marks are ticked."* The five boxes
fill one by one as the scanner passes marked distances, and `Height(N / 5)` counts
them. The blue arrow in the illustration points **up**, away from the plate.

**Total requested: 5 orientations × 5 distances = 25 accepted views.** This is
consistent with — but not proven by — the supplied filesystem evidence that
`calibrate_image_read_rapid/` holds **25 × 3** images: 25 groups of three frames
(left IR, right IR, colour). The "3" is not visible anywhere in the UI, so the
correspondence is an inference.

**White balance uses a third pose rule (observed).** The WB illustration's angle
badge reads **90°** (the same normal pose as calibration step 1) and the instruction
asks for *"the middle marked block"* of a **seven**-block distance widget, i.e. one
view at the middle distance. So the WB stage is a single capture at a prescribed
pose, not a sweep.

**Who moves?** The scanner. The instruction says to *hold the device* and *move the
scanner vertically upwards*; the plate is only *placed* on a horizontal surface. The
illustration shows the scanner moving while the plate stays flat. For white balance
the operator additionally **turns the board over** to its white side (instruction
text), which is the only time the plate itself is handled.

**Motion pattern actually executed (observed):**

* The operator does **not** sweep monotonically. Mark orders were 3,2,1,4,5 /
  4,5,3,2,1 / 2,3,4,5,1 / 2,3,4,5,1 / 2,3,4,5,1. In steps 2–5 the **top box (1) is
  filled last every time**, i.e. the operator reaches the extremes first and returns
  to one end to finish.
* Between orientations there is a 2.3–3.1 s repositioning pause and the alignment
  drops to blue for 2.3–3.1 s.
* **45 % of the acquisition time is spent aligned (green): 21.10 s green vs 25.77 s
  blue, across 19 green episodes (median 0.80 s, longest 4.60 s).** There is no long
  "pose and hold" phase; the app is capturing almost as soon as the operator stops
  moving.

**No centre/corner/roll/rotation coverage is requested anywhere.** The recording
contains no instruction about in-plane rotation, image-position coverage, tilt on
two axes, or "fill the corners" — unlike our eight-dimension coverage model. The
only spatial demands are the five fixed orientations and the five distances.

---

## 6. Acceptance mechanics

### 6.1 Automatic, not user-triggered

**Observed.** Nothing is clicked during the 46.9 s acquisition:

* **the mouse pointer is not on screen at all** during the acquisition. It is absent
  at 10.0 s, 30.0 s and 46.4 s, and the parked pointer first appears at **53.47 s**
  (full-res position ≈ (1132, 549)), i.e. only after the first `Next` click at
  52.97 s. The right-hand pane also differs only at the five step transitions
  (2.13, 8.73, 19.87, 28.10, 36.87 s) and at the full-screen dialogs;
* there is no shutter/capture button anywhere in the Calibration screen;
* ticks and the counter increment by themselves as the operator moves;
* the only mouse-driven controls used in the flow are the two `Next` buttons.

The solve also starts by itself 0.50 s after the 25th mark (46.367 → 46.867).

### 6.2 What success looks like

1. The two live-view discs (grey target + translucent blue current) **collapse into
   one bright-green disc with a large check mark**, drawn at the pattern centre
   (first seen 2.533 s).
2. The corresponding distance box goes from an empty outlined rectangle to a
   **filled green box with a check**; the most recently filled box is drawn
   blue-on-blue (e.g. 2.533, 3.93).
3. `Height(N / 5)` increments **in the same frame** as the box changes.
4. At 5/5 the chevron advances **0.80 s later**, automatically.

### 6.3 What failure looks like

* Blue disc + grey target disc, no check, no tick → not aligned.
* One-line banner `Please, make sure you are pointing at the target circle green.`
* Out-of-range distance → a two-line banner with an icon and an imperative:
  `Too Far` / `Hold the scanner steadily and move it downwards.` or
  `Too close` / `Hold the scanner steadily and move it upwards.`

All three are *actionable*: they say what to do, not what is missing.

### 6.4 Hold requirement — not directly observable

The first tick of step 1 occurs in the **same frame** as the first green state
(2.533 s). Any stability requirement is therefore shorter than one frame interval
(33 ms) or is evaluated internally before the visual state changes. The screen
recording cannot separate "accept on first aligned frame" from "require k frames of
stability that had already elapsed".

### 6.5 What a capture actually is — not observable

The UI gives no per-view confirmation beyond the tick. There is no thumbnail, no
image count, no per-view score, and no indication of how many cameras contributed.
The 1.87 s/view rhythm is the only evidence of the capture cadence.

---

## 7. Duration breakdown, with the geometric/white-balance split

| phase | start | end | duration | share of 66.4 s |
|---|---|---|---|---|
| **Geometric acquisition** (5×5 views) | 0.000 | 46.867 | **46.867 s** | 70.6 % |
| **Geometric solve** (`Calibrating`) | 46.867 | 48.600 | **1.733 s** | 2.6 % |
| Operator reads modal 1 | 48.600 | 52.967 | 4.367 s | 6.6 % |
| **White-balance alignment** | 52.967 | 59.733 | **6.766 s** | 10.2 % |
| WB capture → modal | 59.733 | 60.633 | 0.900 s | 1.4 % |
| Operator reads modal 2 | 60.633 | 64.400 | 3.767 s | 5.7 % |
| Quit / cleanup | 64.400 | 65.733 | 1.333 s | 2.0 % |
| Scan init + Scan tab | 65.733 | 66.400 | 0.667 s | 1.0 % |

**The split the brief asked for:**

* **Geometric calibration = 48.600 s** (46.867 s acquisition + 1.733 s solve) = **86.4 %**
  of the 56.3 s of non-operator work.
* **White balance = 7.666 s** (6.766 s alignment + 0.900 s capture) = **13.6 %**.
* Operator-paced modal reading (8.134 s) and cleanup/transition (2.0 s) are separate.
* **App startup is not in the video at all (0 s visible).**

**Cross-check against the user's estimate.** "About one minute including white
balance" ≈ 66.4 s of clip. The *interactive* span from the first accepted view
(2.533 s) to the WB success modal (60.633 s) is **58.1 s** — so the estimate is a
good description of the clip. It is **not** a description of the session (§9).

---

## 8. Progress, completion and error handling

### 8.1 Progress signals (three, all visible at once)

1. `Height(N / 5)` + five boxes — progress **within** one orientation.
2. Chevrons `1…5` — progress **across** orientations.
3. Top-tab status (`In Progress`) and the tab rail — progress of the whole module.

Per-view feedback is a tick, not a number. There is **no** view counter, no
percentage during acquisition, and no estimate of remaining time.

The percentage appears only for the offline solve (`Calibrating`, 1 %→~85 % over
1.73 s, measured at ≈1.2 %/frame early and ≈2 %/frame later), and for cleanup
(`Quit calibration`).

### 8.2 Completion conditions

* A **step** completes when all five boxes are ticked; the chevron advances 0.80 s
  later. There is no confirm button.
* The **acquisition** completes when step 5 reaches 5/5; the solve starts 0.50 s
  later, automatically.
* The **geometric calibration** completes when the solve returns; a modal states
  `Calibration succeeded`.
* The **module** completes after white balance and the `Quit calibration` step; the
  header turns into `Nov.14 - 00:13 ✓` and the app lands on the Scan tab.
* **No quality metric is ever shown.** The only success signal is the word
  "succeeded".

### 8.3 Error handling and recovery

The app's whole error vocabulary is: *point at the target*, *too far → move down*,
*too close → move up*. There is no error dialog, no retry counter, no "view
rejected" message, and no explanation of *why* a mark did not tick.

Recovery is implicit and works well: when alignment is lost the disc returns to
blue and the operator simply moves again; nothing is cancelled, nothing is lost,
and no mark already earned is taken away. The `Too Far` episode at 4.933 s is
recovered in 0.67 s. The longest unaligned stretch is 3.13 s (19.87–23.00 s, the
repositioning after a chevron advance).

The `Calibrating` dialog has **no cancel affordance** — once the 25th view is in,
the operator cannot abort the solve from the UI.

### 8.4 Final quality indicators — what the operator is told

**Observed.** At the end of the geometric stage the operator is told exactly one
thing: **`Calibration succeeded`**, on a dimmed backdrop, with a `Next` button and a
reminder to store the plate carefully. At the end of white balance:
**`White balance calibration successful`**. On the Scan tab the only persistent
record is **`Nov.14 - 00:13 ✓`** under the *Calibration* tab.

Nothing else is shown anywhere in the 1992 frames:

* no RMS, residual, reprojection error or epipolar number;
* no coverage map, no per-view score, no rejected-view count;
* no condition number, covariance or uncertainty;
* no statement of which parameters were solved and which were held;
* no "quality: good/fair/poor" indicator;
* no way to inspect the 25 captured views (no gallery, no thumbnails).

So the **final quality indicator is a boolean**. Whether the calibration was good,
marginal or bad is not communicated, and the operator has no basis to decide whether
to repeat it. This is the single sharpest difference from our wizard, which ends with
RMS 0.651 px / held-out 0.770 ± 0.019 px / epipolar 0.333 (vs 0.465 factory) and an
explicit readiness verdict.

---

## 9. Reconciliation with the on-disk session (as supplied)

The filesystem evidence (read-only Windows partition, supplied by the coordinator,
not re-measured here):

| time | event |
|---|---|
| 00:09:52.65 | EXStar rewrites `ParaCCM_0.txt` / `ParaDark_0.txt` — session/app start |
| 00:13:11.94 | `LeftCCF.txt`, `RightCCF.txt`, `TexCCF.txt` — **geometric calibration committed** |
| 00:13:12.50 | `calibrate_image_read_rapid/` (25 × 3 images) |
| 00:14:54.31 | `wbdata.txt`, `wbdata_noled.txt` — **white balance committed** |
| 00:14:55.00 | `calibrate_image_wb/` (2 × 3 images) |

### 9.1 What the video demonstrably covers

* **The complete 25-view geometric acquisition** (0.000–46.867 s) and its solve
  (46.867–48.600 s). The solve ends immediately before `Calibration succeeded`, and
  the header later records the calibration as **Nov.14 – 00:13**, matching
  00:13:11.94 at minute resolution.
* **The white-balance capture** (52.967–60.633 s), which must correspond to the
  00:14:54.31 commit because it is the only WB commit in the session.

### 9.2 What it does not cover

* **App startup, device connection, plate setup and the pre-capture idle time:
  ≈150 s.** The video opens with the wizard already at `Height(0/5)`, step 1/5. If
  the solve ended at 00:13:11.94, video t = 0 maps to ≈00:12:23 — so the first
  ≈2 min 31 s of the 00:09:52 session are absent.
* **≈85–90 s between the geometric and white-balance commits.** See below.
* Anything after 66.400 s (the recording stops 0.63 s after the Scan tab appears).

### 9.3 The arithmetic that proves the clip is not continuous

Anchor the solve at the CCF commit: **video 48.600 s ↔ 00:13:11.94**.
The WB commit is **00:14:54.31**, i.e. **102.4 s later**.
The video reaches its WB success modal at **60.633 s**, i.e. **12.03 s later**.

`102.4 − 12.03 = 90.4 s` of wall-clock time is not represented in the file.

The alternative anchoring (WB commit = the `Quit calibration` click at 64.40 s)
gives `102.4 − 15.80 = 86.6 s`. Either way **≈85–90 s is missing**, and it must lie
between the geometric success modal and the end of white balance. Because both
success modals are pixel-static, the cut cannot be localised more precisely from
the video: it could be hidden inside either modal, or inside the WB alignment
screen.

**Two readings are consistent with the evidence, and the video cannot separate
them:**

* **(a)** The white-balance stage really took ≈100 s (board flipping, positioning,
  the `Too close` correction), and ~90 s of it was cut from the clip.
* **(b)** The operator left the `White balance calibration successful` modal open
  for ~90 s before pressing `Next`, and that idle time was cut.

Reading (a) is more likely because the WB clip begins at 52.967 s with the white field
**already in view and the reticle already near the target at 53.03 s** — there is no
visible "flip the board" phase, which is exactly the part that would have taken the time.

### 9.4 What this does to the "one minute" claim

* **Geometric calibration: ≈49 s of interaction, confirmed by the video and
  consistent with the 00:12:23 → 00:13:11.94 window.** The claim holds here.
* **White balance: the geometric-commit → WB-commit interval is 102.4 s on disk
  against 12.0 s in the clip.** The claim fails here.
* **Whole session: 5 min 03 s on disk** (including startup and setup), of which the
  clip shows 66.4 s.

So EXStar's *calibration interaction* is best estimated at **≈49 s geometry +
≈100 s white balance ≈ 2.5 min**, plus ≈2.5 min of startup/setup. It is still an
order of magnitude faster than our 22.4 min, but it is **not** one minute.

---

## 10. Design lessons

### (a) Facts observed in the video

1. A finite, prescribed task: 5 orientations × 5 distances = 25 views, all visible
   as ticks.
2. Acceptance is automatic; there is no shutter, no Start, no Finish, and no click
   anywhere in the 47 s acquisition.
3. Success feedback is one glance: a green disc with a check plus a filled box.
4. Failure feedback is one imperative sentence naming a direction
   (`Too Far` → *move it downwards*).
5. The five marks can be filled **in any order** (five different orders observed).
6. 25 views in 46.867 s = **1.87 s per accepted view**; 45 % of that time is spent
   aligned, 55 % moving/realigning.
7. Each orientation costs 8.2–11.1 s: 2.3–3.1 s to re-align the new pose, five marks
   at 0.10–4.20 s intervals (median 1.35 s), then a fixed 0.80 s automatic step close.
8. The solve takes 1.73 s and is presented with a numeric percentage.
9. Success is communicated with a word, not a number: `Calibration succeeded`, no
   RMS, no residual, no coverage map, no per-view score.
10. Only two corrective distance messages occur in the entire session; the operator
    is otherwise left alone.
11. The live view is a normalised/rectified plate rendering, not the raw camera
    image (static 76 px pitch, correlation 1.000).
12. The clip is edited: ~85–90 s of the session is absent, and the audio track is
    pure digital silence.

### (b) Interpretations / inferences

1. The 25 views are a **designed, enumerable constellation**, not a search: the app
   knows exactly which 25 poses it wants, so it can tell the operator precisely
   where to go and can accept every view without a coverage computation.
2. The app most likely **only fits a small correction on top of a factory
   intrinsics prior**. 25 views over 5 orientations cannot constrain a full lens
   model, and nothing in the UI suggests it tries. *(Supported by our own finding
   that freeing distortion on 25–297 vendor views makes held-out error worse; but
   the video itself cannot show this.)*
3. Acceptance is probably **reticle overlap + distance band**, not a solved pose or
   an uncertainty test: the tick can occur in the very frame the green state
   appears, and no per-view pose/quality number is ever displayed.
4. The `Height` widget probably maps to **five discrete distance thresholds** that
   the scanner crosses while being raised; the app may simply record one group per
   threshold crossing while alignment is good. This explains why ticks can occur
   1.4 s apart while the disc stays green.
5. The order-independence and the "top box last" pattern suggest the operator found
   one end of the distance range awkward to reach and saved it for last — the app
   did not care.
6. The 0.80 s step-close is an app animation/timer, not operator behaviour.

### (c) What a screen recording cannot reveal

1. **The acceptance predicate.** What alignment tolerance, what distance tolerance,
   what blur/exposure limits, how many frames must be stable.
2. **What is actually solved**, with which parameters held fixed, and against what
   prior.
3. **Any quality number**: residuals, RMS, held-out error, covariance, condition
   number. The app shows none, and the recording cannot invent them.
4. **Whether any validation ran at all** beyond the word "succeeded".
5. **USB traffic**, trigger timing, exposure/gain changes, illuminator behaviour,
   how many frames per group, and whether the three cameras are captured
   synchronously.
6. **How many images were really stored** (the 25 × 3 correspondence comes from the
   filesystem, not the screen).
7. **Whether the removed 85–90 s contained a failure, a retry, or just operator
   fumbling.**
8. **Audio feedback** — the track is silent, so we cannot know whether EXStar
   normally beeps on acceptance.
9. **What the operator saw on the physical device** (LEDs, projector pattern) and
   whether the scanner was handheld or on a stand.

---

## 11. What our Linux wizard should adopt / improve

Our baseline (from `docs/STATUS.md` §0a and `docs/calibration/`): **22.4 min,
13 195 trigger groups, 297 live-accepted views, 7/8 coverage, not READY** (D4 image
corners and D7 dwell unsatisfied), at 9.76 groups/s. EXStar: **25 views, 46.9 s,
automatic, "succeeded"**.

### 11.1 The arithmetic of the gap — this is the point of the analysis

* Ours: 297 accepted views in 1344 s = **4.53 s per accepted view**.
* EXStar: 25 accepted views in 46.9 s = **1.87 s per accepted view**.
* **Per-view cost differs by 2.4×. View count differs by 11.9×.**
* Even at EXStar's per-view speed, 297 views would take **9.3 min**. The dominant
  term is therefore **how many views the system demands**, not how fast it accepts
  them.

Why we demanded 297: our finish rule is *eight independent coverage dimensions all
satisfied*, and every accepted view must be **non-redundant** — a view too similar
to an accepted one is rejected ("redundant view" 102×, "too similar" 32× in
session-03). So each acceptance requires a genuinely new pose in a 6-D space, and
as coverage fills, new poses get harder to find. The tail is long: the last few
dimensions (D4 right-hand image corners, D7 dwell) consumed most of the 22 minutes
and still failed.

Why EXStar needed 25: its coverage requirement is a **fixed list of 25 poses**. It
never has to search, it never rejects a view as redundant, and it can therefore
accept at the operator's motion rate. **Its speed comes from replacing a search
with a script.**

There is a second, subtler reason. Our redundancy rejection is what makes D7
(dwell/stationarity) unreachable: 138 stationary observations were rejected as
redundant, leaving only 2–3 distinct dwell poses. EXStar's marks tick *while the
scanner is moving through a distance band*, so motion and capture are not in
conflict at all.

### 11.2 Adopt (mechanisms worth copying, with the reason)

| mechanism | why it is fast | how to adopt without losing rigour |
|---|---|---|
| **Prescribed pose script** (5 orientations × 5 distances) | removes the search; every pose is known to be wanted | generate the script *from our coverage model*: pick the minimal set of poses that provably satisfies D1–D8, then drive the operator through it |
| **Per-pose distance marks that tick as you move** | capture happens during natural motion; no "hold still and wait" | tie acceptance to the *crossing of a planned distance band* with a good pose, instead of to a dwell |
| **Order-free marks** | operator never has to backtrack; five different orders all completed | keep: any order, show what remains |
| **Automatic acceptance, no Start/Finish** | removes two decisions and two modes | trigger acquisition when the plate is first detected; finish automatically at the script's end |
| **One-glance success** (green disc + check + filled box) | no reading, no numbers | keep our numbers *available*, but make the primary channel a shape/colour change |
| **Directional corrective banner** (`Too Far` → move down) | one imperative, immediately actionable | our NEXT MOVE already does this; keep the imperative form and add the *distance* variant |
| **Numeric progress for the offline solve** | sets expectations for a 1.7 s wait | give our finish solve a determinate bar with a percentage instead of an indefinite busy state |
| **A closing summary modal** | tells the operator the stage is done and what to do next | keep, but put the quality number in it |

### 11.3 Improve (where EXStar is weak and we must not clone it)

1. **No quality evidence at all.** `Calibration succeeded` is the entire result. A
   calibration that cannot state its RMS, held-out error or covariance is not
   auditable. Our wizard already produces RMS 0.651 px, held-out 0.770 ± 0.019 px,
   epipolar 0.333 vs 0.465 factory — that must stay, and it must be shown at the
   end, not buried in a report.
2. **25 views cannot validate a lens model**, and EXStar never says what it held
   fixed. Our distortion policy (release only on ≥10 % held-out improvement) is
   correct and should not be relaxed to match the vendor's speed.
3. **No coverage proof.** Five orientations are a *design assumption*, not a
   measurement. If the operator's 30° poses are actually 20°, nothing notices. We
   should keep measuring the achieved tilt/roll/position per view — but use it to
   *confirm the script* rather than to *search*.
4. **Acceptance appears to be geometric overlap only.** Our acceptance uses a full
   PnP with marker assignment, quality metrics and redundancy — strictly better.
   The fix is not to weaken it but to make the *pose script* such that every
   scripted pose passes it by construction.
5. **The order-free ticking hides what is left.** With box 1 still empty and four
   green, the operator cannot tell that box 1 means "nearest distance". A stronger
   UI would label the remaining marks in physical terms ("still need: nearest /
   farthest") — which is what our missing-dimension hints already do.
6. **No failure diagnosis.** If a mark never ticks, the app only says "point at the
   target". It never says "the plate is cropped", "the pose is ambiguous", or "this
   view is redundant". Our error vocabulary is richer; keep it.
7. **No cancel on the solve.** The `Calibrating` dialog has no abort. Our
   `FinishWorker` must remain cancellable.
8. **The instruction text is ≈12 px tall at 1080p** and sits in a low-contrast grey.
   It is barely legible in the recording. Our operator window should not copy this.
9. **The distance widget resets each step**, so the only global progress is the
   chevron strip; a first-time operator can read the reset as lost progress.
10. **White balance has no numeric feedback** — no grey level, no colour
    temperature, no convergence indicator, only a disc that turns green. Ours should
    report the measured WB gains and the residual colour error.
11. **(Unknown, not a criticism)** Whether EXStar provides audio feedback cannot be
    determined: the clip's audio track is digital silence.

### 11.4 Concrete recommendation

Replace the *adaptive search* with a *coverage-complete script*:

1. Precompute the minimal pose set that satisfies D1–D8 on our plate geometry — the
   vendor's 25 shows that such a set exists and is small.
2. Drive the operator through it with the EXStar pattern: one pose at a time, an
   orientation illustration, N distance marks per orientation, order-free ticking,
   automatic acceptance, automatic advance, automatic finish.
3. Keep our acceptance test (PnP + quality + assignment) *inside* each scripted
   pose, and keep our validation and reporting *after* the solve.
4. Add D7-style dwell by construction: make one or two of the scripted poses
   explicitly "stop here for 2 s" poses, so stationarity is collected by design
   instead of emerging from rejected frames.
5. Budget: **25–40 views**, target **≤90 s** of acquisition, and measure the
   achieved coverage from the accepted poses afterwards rather than gating on it
   during the run.

That is the mechanism of the 22 min → ~2.5 min difference: EXStar does not have a
better camera or a faster solver — it asks for 25 known poses instead of searching
for 297 unknown ones, and it accepts them apparently without a redundancy test. The
geometric half of that (49 s) is the part worth matching; the white balance is not
faster than ours in any way the recording can demonstrate.

---

## 12. Reproducing this analysis

```sh
# stream facts
ffprobe -v error -show_streams -show_format screencapture/ScreenCapture_EXStar_Calibration.mp4
# audio: prove silence
ffmpeg -i screencapture/ScreenCapture_EXStar_Calibration.mp4 -af ebur128=peak=true -f null -
ffmpeg -i screencapture/ScreenCapture_EXStar_Calibration.mp4 -af silencedetect=n=-45dB:d=0.3 -f null -
# frame-accurate timeline (all 1992 frames decoded once at 960x540 rgb24 and 480x270 gray)
ffmpeg -i screencapture/ScreenCapture_EXStar_Calibration.mp4 -vf scale=960:540,format=rgb24 -f rawvideo rgb960.raw
# exact frame extraction used for the committed frames
ffmpeg -i screencapture/ScreenCapture_EXStar_Calibration.mp4 \
  -vf "select=eq(n\,76),crop=1920:850:0:110,scale=960:-1" -fps_mode vfr -frames:v 1 out.jpg
```

Artifacts in `analysis/exstar_reference/`:

* `video_timeline.json` — phases, steps, all 25 ticks, all banners, every instruction
  with its region and first/last timestamp, acceptance mechanics, edit analysis and
  the filesystem reconciliation.
* `frames/00…13_*.jpg` — 14 full frames at 960 px wide (the workflow states above),
  `14_detail_calibration_pane.jpg` (5 boxes + `Height(N/5)`),
  `15_detail_wb_pane.jpg` (7 boxes, middle ticked, 90° WB illustration),
  `16_detail_distance_banners.jpg` (`Too Far` / `Too close`). Total **≈600 KB**; no video is copied.

The raw recording remains only at `screencapture/ScreenCapture_EXStar_Calibration.mp4`
and is deliberately not committed or copied.
