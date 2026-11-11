"""Command-line scanning application: record → depth → track → fuse → mesh.

    .venv/bin/python -m tools.scan.cli --frames DIR --profile PROFILE \
        --out OUTDIR --count 12

The Qt window (`tools/scan/gui.py`) drives the same `ScanSession`, so the CLI is
the reproducible path for a scan and the GUI is the interactive one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.scan.session import ScanConfig, ScanSession  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", required=True, help="frames directory")
    ap.add_argument("--profile", required=True, help="recon profile JSON")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--recon", default="build/recon/recon_cli",
                    help="reconstruction binary")
    ap.add_argument("--count", type=int, default=8, help="frames to process")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--voxel-mm", type=float, default=5.0)
    ap.add_argument("--trunc-mm", type=float, default=None)
    ap.add_argument("--input-voxel-mm", type=float, default=8.0)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = ScanConfig(frames_dir=Path(args.frames), profile=Path(args.profile),
                     out_dir=Path(args.out), recon_binary=Path(args.recon),
                     voxel_mm=args.voxel_mm, trunc_mm=args.trunc_mm,
                     input_voxel_mm=args.input_voxel_mm, scale=args.scale)
    session = ScanSession(cfg)
    t0 = time.time()
    statuses = []
    try:
        for i in range(args.start, args.start + args.count):
            status = session.process(i)
            statuses.append(status.as_dict())
            if not status.ok:
                print(f"frame {i}: rejected - {status.reason}", file=sys.stderr)
    finally:
        report = session.finish()
        session.close()
    report["wall_s"] = round(time.time() - t0, 2)
    report["statuses"] = statuses
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "frames_status"},
                         indent=1))
    else:
        print(f"frames {report['frames']} rejected {report['rejected']} "
              f"vertices {report.get('vertices')} faces {report.get('faces')} "
              f"wall {report['wall_s']:.1f} s -> {report.get('obj')}")
        print(f"report: {report['report']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
