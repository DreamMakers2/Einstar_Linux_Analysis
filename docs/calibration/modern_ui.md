# Operator UI — the replacement calibration window

Status 2026-11-12. The engineering dashboard is still available as
`tools/wizard/gui/main_window.py` for regression tests, but the launcher opens
`tools/wizard/gui/modern_window.py`.

## Design

The screen has one purpose: move the plate, capture automatically, finish when
the geometry is covered.

```
┌──────────────────────────────────────────────────────────────────────┐
│ EINSTAR  CALIBRATION                    ● CAPTURING   9.9 fps  12 views │
├─────────────────────────── feeds ────────────────────┬────────────────┤
│  LEFT INFRARED     RIGHT INFRARED     COLOUR          │  MOVE THE PLATE │
│  (large live)      (large live)       (large live)    │  one action     │
│                                                       │  target numbers │
│                                                       ├────────────────┤
│  5 views captured   distance tilt rotation position  │  3-D plate      │
│  coverage chips                                      │                 │
│  image-region / depth / tilt map                      │                 │
├───────────────────────────────────────────────────────┴────────────────┤
│ Keep moving — distance, tilt             Stop & keep data  Advanced  Pause│
└──────────────────────────────────────────────────────────────────────┘
```

* Three large feeds, all from the newest complete group, updated before any
  decision runs. Preview is not tied to detection or optimisation.
* One short instruction (derived from the planner's full sentence) and a
  movement glyph. The exact target distance/tilt/roll/region is a second line;
  the full planner text is a tooltip.
* Accepted-view count, pose-family chips, image-region/depth/tilt coverage map,
  capture FPS and scanner state are visible without opening diagnostics.
* The primary button is **Start calibration** when idle and **Finish & build
  profile** only when coverage is complete. There is no way to force a
  not-ready candidate into activation.
* **Stop & keep data** is always available and never deletes frames.
* **Advanced** opens a separate dialog with observability, acquisition,
  events and the raw snapshot. It is not part of the operator flow.
* Finish runs on `FinishWorker`; the window keeps painting and shows elapsed
  time. The overlay never blocks the event loop.

## Behaviour

| state | what the operator sees |
|---|---|
| idle/live | feeds, "Place the plate…", one Start button |
| recording | instruction, automatic capture confirmations, progress, Pause |
| paused | Resume; acquisition holds |
| finishing | "Optimising and validating… N s", progress bar, no freeze |
| done | "Profile built and validated"; candidate is `validation: pending` |
| error | named error banner and the last event |

Keyboard: `Space` pause/resume, `F` finish, `Ctrl+Shift+A` Advanced.

## Performance path

The GUI only calls `controller.snapshot()` at 10 Hz and paints. All acquisition,
detection, solving, validation and hashing happen on other threads/processes.
Measured on the live unit: preview 9.75 Hz p95 121 ms while decisions ran at
4.55/s; process CPU 193% (1.93 cores). See
`docs/calibration/performance_overhaul_2026-11-12.md`.

## Safety

Closing the window stops the worker, closes the capture source and verifies the
final state. A GUI `SIGKILL` is handled by the capture process itself
(`docs/calibration/process_safety.md`); the UI does not claim optical darkness,
only the verified trigger/illuminator readback.

## Acceptance fixes (2026-11-12, later)

The first acceptance review found that the visual design was sound but the
state model was not. Fixed:

* **Finish truthfulness.** `candidate written` is no longer shown as
  "validated". The window reads `validation.passed` / `verdict.would_activate`;
  it says "Profile validated" only for a passing candidate, "Candidate not
  validated" with the first reason otherwise, and surfaces a quality advisory
  separately when the independent lifecycle gates pass.
* **Finish gating.** `_on_finish` refuses unless READY and a valid state; the
  hidden `F` shortcut cannot bypass readiness. Stop is disabled outside
  recording/paused; Start is hidden after done.
* **Terminal states.** Source exhaustion becomes SOURCE ENDED, freezes the
  capture labels and shows "Not enough coverage" when applicable. Pause,
  stop, finish and error states update the pill, headline, feed badges and
  footer together.
* **Truthful replay metrics.** Replay/synthetic FPS comes from the worker's
  preview rate, feed badges say REPLAY/SYNTHETIC, and the scanner line reports
  processed/accepted groups instead of a capture process's 0.0 fps.
* **Feedback.** An accepted confirmation is held for ~1.8 s and cannot be
  overwritten by the next 4–10 Hz rejection. The feedback timer is parented, so
  closing the window during a message cannot fire into a deleted label.
* **Layout.** At widths below 1320 px the side column is 320 px and feeds can
  shrink to 210 px; the 1180x760 minimum now has no overlap or clipped feed
  badge (minimumSizeHint 1091 px measured). The finish card has a 500 px
  minimum and a wrapped text row.
* **3-D plate.** Scanner mode uses a fixed 1280x1024 image-to-widget mapping;
  moving the plate laterally or changing distance now moves/scales the drawing
  instead of auto-fitting it to the same rectangle every frame.
* **Guidance glyphs.** Up/down arrows now point up/down; closer/farther uses a
  distinct inward/outward chevron glyph rather than a vertical arrow.

Open usability work: the coverage card still has unused width, the target
region is described but not highlighted in the feed, and Advanced does not yet
show the final validation reasons. These are recorded as remaining polish, not
as accepted correctness claims.
