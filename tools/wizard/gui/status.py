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

__all__ = ["STALL_SECONDS", "FPS_STALL_SECONDS", "stall_reason", "display_state",
           "operator_guidance", "acquisition_label"]

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


# ---------------------------------------------------------------------------
# operator guidance: what is actually missing, and when to stop
# ---------------------------------------------------------------------------

_CORNER_WORDS = {
    "top_left": "top-left", "top_right": "top-right",
    "bottom_left": "bottom-left", "bottom_right": "bottom-right",
}
_REGION_WORDS = {
    "top-left": "top-left", "top": "top", "top-right": "top-right",
    "left": "left", "centre": "centre", "right": "right",
    "bottom-left": "bottom-left", "bottom": "bottom",
    "bottom-right": "bottom-right",
}


def acquisition_label(capture: dict | None) -> str:
    """Human label for the authoritative acquisition state."""
    acq = (capture or {}).get("acquisition") or {}
    state = str(acq.get("state") or "unknown")
    labels = {
        "idle": "scanner idle",
        "starting": "scanner starting",
        "running": "scanner RUNNING",
        "pausing": "scanner pausing",
        "paused": "scanner paused",
        "stopping": "scanner stopping",
        "stopping_unverified": "scanner stopped — UNVERIFIED",
        "stopped_verified": "scanner stopped — verified",
        "failed": "scanner FAILED",
        "not_applicable": "",
    }
    return labels.get(state, f"scanner {state}")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def operator_guidance(snapshot: dict) -> dict:
    """Turn a controller snapshot into the operator's next safe action.

    This is a pure function so the exact wording can be regression-tested with
    a recorded snapshot (the 52 GB session's D1-satisfied / D4-corners-missing
    state included).  Rules:

    * the coverage engine's target is authoritative; the old window shortened
      *any* differing target distance to "move farther away" even when D1 was
      already satisfied, which is why the operator was told to move farther
      while the actually missing evidence was the right-hand image corners;
    * a distance instruction may only be shown when the distance dimension is
      unsatisfied and the guidance priority is distance;
    * no-progress / unreachable / stop recommendations from the progress
      advisor take precedence over normal pose guidance;
    * an acquisition reader that ended never suppresses a still-running or
      unverified scanner warning.
    """
    snapshot = snapshot or {}
    state = str(snapshot.get("state") or "idle")
    capture = snapshot.get("capture") or {}
    acq = capture.get("acquisition") or {}
    acquisition_state = str(acq.get("state") or "unknown")
    verified = acq.get("verified")
    terminal = bool(acq.get("terminal"))
    guidance = snapshot.get("guidance") or {}
    readiness = snapshot.get("readiness") or {}
    progress = (guidance.get("progress") or readiness.get("progress") or {})
    coverage = snapshot.get("coverage") or {}
    dims = coverage.get("dimensions") or {}
    target = guidance.get("target") or {}
    missing = [str(x) for x in (readiness.get("missing") or [])]
    missing_corners = [str(x) for x in
                       (progress.get("missing_corners") or [])]
    missing_keys = [str(k) for k, v in dims.items()
                    if isinstance(v, dict) and not v.get("satisfied")]

    result = {
        "state": state,
        "instruction": "Press Start to begin" if state in ("idle", "cancelled")
                       else "Hold position",
        "detail": "",
        "footer": "",
        "banner": None,
        "tone": "normal",
        "stop_recommended": bool(progress.get("stop_recommended")),
        "unreachable": bool(progress.get("unreachable")),
        "missing": missing,
        "guidance_age_s": _num(guidance.get("age_s")),
        "acquisition_state": acquisition_state,
        "acquisition_label": acquisition_label(capture),
        "capture_still_running": bool(acq.get("child", {}).get("running")),
        "capture_unverified": bool(terminal and verified is False),
        "verified": verified,
    }

    # -- acquisition truth first: never imply a stopped scanner -------------
    running = bool(acq.get("child", {}).get("running"))
    if acquisition_state in ("failed", "stopping_unverified") or (
            terminal and verified is False):
        reasons = [str(r) for r in (acq.get("reasons") or [])]
        result["banner"] = (
            "Scanner state UNVERIFIED — " +
            ("; ".join(reasons[:2]) if reasons else "no verified final state"))
        result["tone"] = "error"
        result["footer"] = ("The session data is preserved; check Advanced "
                            "diagnostics before treating the scanner as off.")
    elif running and state not in ("recording", "paused"):
        result["banner"] = (
            "CAPTURE IS STILL RUNNING — the reader stopped, the scanner did "
            "not. Stopping it now…")
        result["tone"] = "error"
    elif acquisition_state == "stopping":
        result["banner"] = "Stopping the scanner and finalising the recording…"
        result["tone"] = "warn"

    if state not in ("recording", "paused"):
        if result["banner"] is None and terminal and verified is True:
            result["footer"] = ("Scanner stopped and recording finalized "
                                "(verified).")
        return result

    if readiness.get("ready"):
        result["instruction"] = "Coverage complete — finish when you are ready"
        result["footer"] = "All required views are present."
        return result

    # -- no-progress / unreachable / stop recommendation -------------------
    if progress.get("stop_recommended"):
        message = str(progress.get("stop_message") or "").strip()
        result["instruction"] = "Stop recording — the remaining gaps are not improving"
        result["footer"] = (
            "Press “Stop & keep data”. " + (message or
            "No new coverage is arriving; analyse the views offline."))
        result["banner"] = message or (
            "No meaningful progress: stop and analyse the session offline. "
            "The raw recording and status are preserved.")
        result["tone"] = "warn"
        return result
    if progress.get("unreachable"):
        message = str(progress.get("unreachable_message") or "").strip()
        result["instruction"] = message or (
            "The requested viewpoint appears unreachable — reposition the "
            "scanner or stop and analyse offline")
        result["footer"] = (
            "If the pose cannot be reached, stop recording: every recorded "
            "group stays on disk so you can analyse the views offline.")
        result["banner"] = message or result["instruction"]
        result["tone"] = "warn"
        return result

    # -- actual missing information ---------------------------------------
    corners_text = ", ".join(_CORNER_WORDS.get(c, c.replace("_", " "))
                             for c in missing_corners)
    region = target.get("region")
    region_text = _REGION_WORDS.get(str(region), str(region)) if region else ""
    priority = str(guidance.get("priority") or "pose")
    current_dist = _num((snapshot.get("last_pose") or {}).get("distance_mm"))
    target_dist = _num(target.get("distance_mm"))
    distance_missing = "distance_bands" in missing_keys

    if missing_corners:
        result["instruction"] = (
            "Aim the plate at the image " +
            (" and ".join(corners_text.split(", ")[:2])))
        result["detail"] = "Missing image corners: " + corners_text
    elif priority == "region" and region_text:
        result["instruction"] = (
            f"Move the plate toward the image {region_text}")
        result["detail"] = str(target.get("label") or "image-region coverage")
    elif priority == "distance" and distance_missing and target_dist is not None \
            and current_dist is not None:
        if target_dist < current_dist - 25.0:
            result["instruction"] = "Move the plate closer"
        elif target_dist > current_dist + 25.0:
            result["instruction"] = "Move the plate farther away"
        else:
            result["instruction"] = "Hold this distance"
        result["detail"] = f"Target {target_dist:.0f} mm from {current_dist:.0f} mm"
    else:
        instruction = str(guidance.get("instruction") or "").strip()
        result["instruction"] = instruction or (
            "Keep moving the plate to a new pose")
        bits = []
        if region_text and priority == "region":
            bits.append(f"region {region_text}")
        if target_dist is not None:
            bits.append(f"target {target_dist:.0f} mm")
        if target.get("tilt_deg"):
            tilt = target["tilt_deg"]
            if isinstance(tilt, (list, tuple)) and len(tilt) == 2:
                tx, ty = _num(tilt[0]), _num(tilt[1])
                if tx is not None and ty is not None:
                    bits.append(f"tilt {tx:+.0f}° / {ty:+.0f}°")
        result["detail"] = "  ·  ".join(bits)

    if result["banner"] is None:
        if missing_corners:
            result["footer"] = f"Missing image corners: {corners_text}"
        else:
            result["footer"] = ("Keep moving — " + ", ".join(
                str(m).replace("_", " ") for m in missing[:2])
                if missing else
                "Keep the plate in view; the scanner captures automatically.")
    age = result["guidance_age_s"]
    if age is not None and age > 3.0:
        result["footer"] = (
            f"Guidance is {age:.1f} s old (measurement is catching up). "
            + result["footer"])
    return result
