"""Capture-state sanity: never claim RECORDING while nothing is arriving.

The reported failure had the window showing a recording state while the capture
was effectively idle, so the operator could not tell a slow scan from a dead
one.  These helpers are pure functions of the numbers the window already has, so
the rule is testable without a GUI.

Thresholds are deliberately generous compared to the frame interval: the live
stream produces one group every 100 ms, and a solver pause can legitimately take
a second or two, so a stall is only declared after several missed frames.
"""

from __future__ import annotations

__all__ = ["STALL_SECONDS", "FPS_STALL_SECONDS", "stall_reason", "display_state"]

#: no decision for this long while recording means the pipeline has stalled
STALL_SECONDS = 3.0
#: capture reports zero fps for this long while recording
FPS_STALL_SECONDS = 2.0


def stall_reason(state: str, seconds_since_decision: float | None,
                 fps: float | None, *, seconds_at_zero: float = 0.0,
                 stall_seconds: float = STALL_SECONDS,
                 fps_stall_seconds: float = FPS_STALL_SECONDS) -> str | None:
    """Why the UI should not call this state "recording", or ``None``."""
    if str(state).lower() != "recording":
        return None
    if seconds_since_decision is not None and seconds_since_decision > stall_seconds:
        return (f"no frames processed for {seconds_since_decision:.1f} s")
    if fps is not None and fps <= 0.05 and seconds_at_zero > fps_stall_seconds:
        return f"capture reports {fps:.2f} fps for {seconds_at_zero:.1f} s"
    return None


def display_state(state: str, reason: str | None) -> str:
    """The label the header should show, given a stall reason."""
    label = str(state).upper()
    if reason is None:
        return label
    return f"{label} · STALLED"


def fps_from_decisions(count: int, seconds: float) -> float:
    """Measured processing rate; 0 while the window has no measurements yet."""
    if seconds <= 0:
        return 0.0
    return float(count) / float(seconds)
