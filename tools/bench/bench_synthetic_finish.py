"""Full controller finish on a synthetic 60-view session (no hardware)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from tools.wizard import controller as C
from tools.wizard import sources, types


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--views", type=int, default=60)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    spec = sources.SyntheticSpec(n_views=args.views, pattern="diverse", seed=7)
    src = sources.SyntheticSource(spec)
    tmp = Path(tempfile.mkdtemp(prefix="einstar-synth-finish-"))
    ctl = C.WizardController(factory_profile=src.profile, session_dir=tmp,
                             mode="standard",
                             source=types.SourceInfo(kind="synthetic",
                                                     path="synthetic:diverse"))
    t0 = time.perf_counter()
    ctl.begin()
    ingested = accepted = 0
    for group in src:
        ingested += 1
        d = ctl.ingest(group)
        accepted += int(bool(getattr(d, "accepted", False)))
    ingest_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    result = ctl.finish()
    finish_s = time.perf_counter() - t1
    validation = {}
    vp = ctl.paths.candidate_dir / "validation.json"
    if vp.is_file():
        validation = json.loads(vp.read_text())
    out = {
        "schema": "einstar-bench-synthetic-finish/1",
        "views_requested": args.views,
        "groups_ingested": ingested,
        "accepted": len(ctl.session.accepted),
        "ingest_s": ingest_s,
        "finish_s": finish_s,
        "total_s": ingest_s + finish_s,
        "solves": int(getattr(ctl.session, "_solves", 0)),
        "solve_seconds": float(getattr(ctl.session, "solve_seconds", 0.0)),
        "ok": bool(result.get("ok")),
        "state": ctl.state,
        "rms_px": (result.get("validation") or {}).get("rms_px"),
        "verdict": (validation.get("verdict") or {}).get("status"),
        "would_activate": (validation.get("verdict") or {}).get("would_activate"),
        "validation_gates": len(validation.get("gates") or []),
    }
    ctl.close()
    shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(out, indent=1, sort_keys=True)
    print(text)
    if args.out:
        p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
