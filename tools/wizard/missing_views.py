#!/usr/bin/env python3
"""Offline verification and operator summary for the missing-view workflow.

Reads an existing session **read-only**, measures the corrected coverage
dimensions, prints exactly what is missing, and emits the minimal ordered plan
of board poses that would close it.  With ``--extension PATH`` it re-assesses
an extension session that already contains accepted holds.

The command never writes to the base session, never captures, never solves a
new calibration and never activates a profile; ``--json OUT`` is the only
thing it writes (and it refuses an ``OUT`` inside the evidence directories).

Examples::

    # What is missing in the untouched 52 GB session?
    .venv/bin/python tools/wizard/missing_views.py \
        --session-dir ~/einstar-calibration-03

    # The recovered session already adds the four raw corner views; only D7
    # holds remain (this is the operator's completion base).
    .venv/bin/python tools/wizard/missing_views.py \
        --session-dir analysis/calibration/recovery/session_recovered \
        --json /tmp/missing_views.json

    # Verify a captured extension offline before the operator is asked for
    # anything else.
    .venv/bin/python tools/wizard/missing_views.py \
        --session-dir analysis/calibration/recovery/session_recovered \
        --extension ~/einstar-extension-01 --json /tmp/after.json

Exit status: ``0`` when the assessed evidence is READY, ``1`` when it is not
(the normal case before the holds), ``2`` on an error.  Activation is always a
separate, explicit lifecycle step.
"""

from __future__ import annotations

# Running this file as a script puts ``tools/wizard`` on sys.path[0], where
# ``types.py`` would shadow the standard library ``types`` module (argparse and
# pathlib import it indirectly).  Remove that directory and put the repo root
# first -- using only ``sys``/``os``, before importing anything else.
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path[:] = [entry for entry in sys.path
               if entry not in ("", ".", _SCRIPT_DIR, "tools/wizard")
               and os.path.abspath(entry or ".") != _SCRIPT_DIR]
for _path in (os.path.join(_REPO, "tools"), _REPO):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

import argparse
import json
from pathlib import Path

from calib import missing_views as mv  # noqa: E402


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def _fmt(value, digits: int = 0) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _print_assessment(title: str, assessment) -> None:
    verdict = assessment.readiness
    if title:
        print(f"\n{title}")
    print(f"  coverage            : {assessment.satisfied}/{assessment.total} "
          f"satisfied"
          + ("" if assessment.ready else " — NOT READY"))
    if assessment.ready:
        print("  verdict             : READY")
        return
    for dim in verdict.get("unsatisfied_dimensions") or []:
        node = (assessment.measurement.get(dim) or {})
        print(f"    {dim} {node.get('label', '')}: unsatisfied")
        for name in node.get("checks") or []:
            check = (assessment.measurement.get("checks") or {}).get(name) or {}
            if check.get("satisfied", True):
                continue
            print(f"      - {name}: {check.get('hint', '')}")


def _print_plan(plan: dict) -> None:
    print(f"\nPlan: {plan['n_planned']} pose(s) "
          f"(cap {plan['cap']}, ~{plan['estimated_minutes']:.1f} min of "
          f"scanning)")
    if not plan["poses"]:
        print("  nothing to capture")
    for entry in plan["poses"]:
        target = entry.get("target") or {}
        print(f"  {entry['index']}. {entry['dimension']} — "
              f"{entry['description']}")
        print(f"     move    : {entry['instruction']}")
        hold = target.get("hold_s")
        print(f"     target  : {_fmt(target.get('distance_mm'))} mm "
              f"{target.get('distance_range_mm')}, tilt "
              f"{target.get('tilt_quadrant')} "
              f"({_fmt(target.get('tilt_x_deg'))}/"
              f"{_fmt(target.get('tilt_y_deg'))} deg), roll sector "
              f"{target.get('roll_sector')} "
              f"({_fmt(target.get('roll_deg'))} deg), image cell "
              f"{target.get('image_cell')} ({target.get('image_cell_name')})"
              + ("" if hold is None else f", hold {_fmt(hold, 1)} s"))
        print(f"     reason  : {entry['reason']}")
    for note in plan.get("notes") or []:
        print(f"  note: {note}")


def _print_evidence(title: str, evidence, assessment) -> None:
    print(f"\n{title}")
    print(f"  session             : {evidence.session_dir}")
    print(f"  state               : {evidence.state}")
    print(f"  accepted views      : {len(evidence.accepted)} "
          f"({len(evidence.views)} coverage views)")
    print(f"  stationarity stream : {len(evidence.stationarity_observations)} "
          f"row(s) from {evidence.source}")
    factory = evidence.factory_sha256 or "-"
    print(f"  factory profile     : {factory[:16]}…")
    present = {name: digest for name, digest in evidence.digests.items()
               if digest}
    print("  digests             : "
          + (", ".join(f"{name} {digest[:12]}…"
                       for name, digest in present.items()) or "none"))
    for note in evidence.notes:
        print(f"  note                : {note}")
    _print_assessment("", assessment)


def _guard_output(path: Path, *forbidden: Path) -> Path:
    target = path.expanduser().resolve()
    for root in forbidden:
        try:
            root = root.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if root == target or root in target.parents:
            raise mv.MissingViewsError(
                f"refusing to write {target}: it is inside the read-only "
                f"evidence directory {root}")
    return target


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assess the missing views of an existing calibration "
                    "session and print the minimal completion plan.  The base "
                    "session is never written to and no profile is activated.")
    parser.add_argument("--session-dir", required=True, type=Path,
                        help="existing session directory (read-only)")
    parser.add_argument("--recovered", type=Path, default=None,
                        help="optional recovered-session directory from the "
                             "same recording (e.g. the four raw corner views); "
                             "its accepted views are used as the completion "
                             "base, the original session is still untouched")
    parser.add_argument("--extension", type=Path, default=None,
                        help="extension directory to combine and re-assess")
    parser.add_argument("--json", dest="json_out", type=Path, default=None,
                        help="write the machine-readable assessment + plan")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="default and only mode: nothing is written to the "
                             "evidence directories")
    parser.add_argument("--no-plan", action="store_true",
                        help="only assess; do not compute a plan")
    return parser


def _resolved_evidence(session_dir: Path, recovered: Path | None):
    base = mv.load_base_evidence(session_dir)
    if recovered is None:
        return base, None
    recovered_evidence = mv.load_base_evidence(recovered)
    if (base.factory_sha256 and recovered_evidence.factory_sha256
            and base.factory_sha256 != recovered_evidence.factory_sha256):
        raise SystemExit(
            "refusing the recovered overlay: factory profile differs "
            f"({base.factory_sha256[:16]}… vs "
            f"{recovered_evidence.factory_sha256[:16]}…)")
    base_ids = {int(entry["view_id"]) for entry in base.accepted
                if entry.get("view_id") is not None}
    recovered_ids = {int(entry["view_id"]) for entry in recovered_evidence.accepted
                     if entry.get("view_id") is not None}
    missing = sorted(base_ids - recovered_ids)
    if missing:
        raise SystemExit(
            "refusing the recovered overlay: it does not contain the base "
            f"session's accepted views (missing view ids {missing[:8]}…)")
    return recovered_evidence, base


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session_dir = args.session_dir.expanduser().resolve()
    recovered = args.recovered.expanduser().resolve() if args.recovered else None
    extension = args.extension.expanduser().resolve() if args.extension else None
    try:
        evidence, original = _resolved_evidence(session_dir, recovered)
    except mv.MissingViewsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    base_assessment = mv.assess(evidence.views,
                                evidence.stationarity_observations,
                                evidence.spec)
    if recovered is not None and original is not None:
        original_assessment = mv.assess(original.views,
                                        original.stationarity_observations,
                                        original.spec)
        _print_evidence("Original session (untouched)", original,
                        original_assessment)
        print("\nRecovered overlay: accepted views from the same recording "
              "are included below; the original directory is not modified.")
    _print_evidence("Base evidence", evidence, base_assessment)
    if evidence.source == "decisions_jsonl" and recovered is None:
        print("\nwarning: this is the decision-rate stream (one row per live "
              "decision).\n"
              "  The complete raw 10 Hz stream of the same recording "
              "qualifies fewer intervals;\n"
              "  use --recovered <recovered-session> (or point --session-dir "
              "at it) for the\n"
              "  authoritative completion base (docs/calibration/"
              "missing_views_workflow.md).")

    extension_info = None
    final_assessment = base_assessment
    if extension is not None:
        try:
            store = mv.ExtensionStore.open(extension)
            holds = store.load_holds()
        except (mv.MissingViewsError, OSError) as exc:
            print(f"error: cannot open extension {extension}: {exc}",
                  file=sys.stderr)
            return 2
        expected = store.base_session_dir
        if expected != evidence.session_dir:
            print(f"refusing extension {extension}: it was created for "
                  f"{expected}, not {evidence.session_dir}", file=sys.stderr)
            return 2
        combined = mv.combine(evidence, holds)
        final_assessment = mv.assess(combined.views,
                                     combined.stationarity_observations,
                                     evidence.spec)
        extension_info = {
            "dir": str(extension),
            "manifest": store.manifest(),
            "n_holds": len(holds),
            "warnings": store.warnings,
            "holds": [
                {"hold_id": hold.get("hold_id"),
                 "duration_s": hold.get("duration_s"),
                 "mean_speed_mm_s": hold.get("mean_speed_mm_s"),
                 "mean_rate_deg_s": hold.get("mean_rate_deg_s"),
                 "n_samples": hold.get("n_samples"),
                 "target": hold.get("target")}
                for hold in holds],
        }
        _print_evidence("Combined evidence (base + extension dwell holds)",
                        evidence, final_assessment)
        print(f"\nExtension: {extension}")
        print(f"  accepted holds      : {len(holds)}")
        for hold in holds:
            print(f"    hold {hold.get('hold_id')}: "
                  f"{_fmt(hold.get('duration_s'), 2)} s, "
                  f"speed {_fmt(hold.get('mean_speed_mm_s'), 2)} mm/s, "
                  f"rotation {_fmt(hold.get('mean_rate_deg_s'), 2)} deg/s, "
                  f"{hold.get('n_samples')} samples")
        for warning in store.warnings:
            print(f"  warning             : {warning}")
        print("  solver              : not run here; the final rig solve is "
              "the controller's job\n"
              "                        (WizardController.finish() on the "
              "extension session)")

    plan = None if args.no_plan else mv.plan(final_assessment, evidence,
                                             evidence.spec)
    if plan is not None:
        _print_plan(plan)

    print("\nActivation: never by this command.  Readiness is a measurement; "
          "activation stays an explicit lifecycle call after a validated "
          "candidate.")
    ready = bool(final_assessment.ready)

    if args.json_out is not None:
        forbidden = [session_dir]
        if recovered is not None:
            forbidden.append(recovered)
        if extension is not None:
            forbidden.append(extension)
        try:
            out = _guard_output(args.json_out, *forbidden)
        except mv.MissingViewsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        payload = {
            "schema": f"{mv.SCHEMA}/cli",
            "session_dir": str(session_dir),
            "recovered": None if recovered is None else str(recovered),
            "extension": extension_info,
            "base": evidence.as_json(),
            "assessments": {
                "base": mv.assessment_json(base_assessment),
                "final": mv.assessment_json(final_assessment),
            },
            "plan": plan,
            "readiness": {"ready": ready},
            "activation": mv.activation_gate(),
        }
        if recovered is not None and original is not None:
            payload["original"] = original.as_json()
            payload["assessments"]["original"] = mv.assessment_json(
                original_assessment)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
        print(f"\nwrote {out}")

    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
