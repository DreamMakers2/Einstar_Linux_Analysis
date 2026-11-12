"""Measure the complete final-calibration path on a preserved session.

Creates a shadow directory with symlinks to the multi-GB frame/recording data,
so the original session is never modified.  Runs ``WizardController.resume()``
then ``finish()`` and reports wall time, solver time/passes, residuals and the
candidate verdict as JSON.

Usage:
    python tools/bench/bench_finalize.py \
        --session ~/einstar-calibration-02 --out analysis/.../finalization.json
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))


def shadow_session(source: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="einstar-bench-"))
    for name in ("session.json", "manifest.json", "decisions.jsonl",
                 "status.json", "control.txt", "report.md",
                 "capture.stdout.log", "capture.stderr.log"):
        p = source / name
        if p.is_file():
            shutil.copy2(p, tmp / name)
    for name in ("groups", "frames", "raw.e1r", "ring"):
        p = source / name
        if p.exists():
            (tmp / name).symlink_to(p.resolve())
    cand = source / "candidate"
    if cand.is_dir():
        shutil.copytree(cand, tmp / "candidate")
    return tmp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--mode", default="high")
    ap.add_argument("--factory", default=str(Path.home() / "einstar-private" /
                                             "einstar-profiles" / "factory-e10.json"))
    args = ap.parse_args()
    source = Path(os.path.expanduser(args.session)).resolve()
    shadow = shadow_session(source)
    try:
        from wizard import controller as C
        from wizard import types as T
        ctl = C.WizardController(factory_profile=args.factory,
                                 session_dir=shadow, mode=args.mode,
                                 source=T.SourceInfo(kind="live", path=str(source)))
        t0 = time.perf_counter()
        resumed = ctl.resume()
        resume_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        result = ctl.finish()
        finish_s = time.perf_counter() - t0
        validation = {}
        vp = ctl.paths.candidate_dir / "validation.json"
        if vp.is_file():
            validation = json.loads(vp.read_text())
        out = {
            "schema": "einstar-bench-finalize/1",
            "session": str(source),
            "resume": resumed,
            "resume_s": resume_s,
            "finish_s": finish_s,
            "accepted": len(ctl.session.accepted),
            "solves": int(getattr(ctl.session, "_solves", 0)),
            "solve_seconds": float(getattr(ctl.session, "solve_seconds", 0.0)),
            "ok": bool(result.get("ok")),
            "state": ctl.state,
            "error": ctl.error,
            "rms_px": (result.get("validation") or {}).get("rms_px"),
            "verdict": (validation.get("verdict") or {}).get("status"),
            "would_activate": (validation.get("verdict") or {}).get("would_activate"),
            "candidate": str(ctl.paths.candidate_dir / "profile.json"),
            "factory_unchanged": bool(result.get("factory_unchanged")),
        }
    finally:
        shutil.rmtree(shadow, ignore_errors=True)
    text = json.dumps(out, indent=1, sort_keys=True)
    print(text)
    if args.out:
        dest = Path(args.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
