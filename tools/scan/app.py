"""Entry point for the Qt scanning application.

    .venv/bin/python tools/scan/app.py --frames DIR --profile PROFILE --count 12

Headless (for tests and CI):

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/scan/app.py \
        --frames DIR --profile PROFILE --headless --count 3 --snapshot-out s.png
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))
sys.path.insert(0, str(_HERE.parents[1]))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Einstar scanning application")
    ap.add_argument("--frames", required=True, help="frames directory")
    ap.add_argument("--profile", required=True, help="recon profile JSON")
    ap.add_argument("--out", default="scan-out", help="output directory")
    ap.add_argument("--recon", default=str(_HERE.parents[2] / "build" / "recon" / "recon_cli"))
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--voxel-mm", type=float, default=5.0)
    ap.add_argument("--input-voxel-mm", type=float, default=8.0)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--live", action="store_true",
                    help="start the capture tool and scan live frames")
    ap.add_argument("--capture-binary",
                    default=str(_HERE.parents[2] / "build" / "driver" / "einstar-capture-frames"))
    ap.add_argument("--backend", default="libusb", choices=("libusb", "simulated"))
    ap.add_argument("--ring", type=int, default=120)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--snapshot-out", default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from tools.scan.gui import ScanWindow
    from tools.scan.session import ScanConfig
    from PySide6 import QtWidgets

    source = None
    frames = Path(args.frames)
    if args.live:
        from tools.scan.live import CaptureSource
        capture_dir = Path(args.out) / "capture"
        source = CaptureSource(session_dir=capture_dir,
                               capture_binary=Path(args.capture_binary),
                               backend=args.backend, ring=args.ring)
        frames = capture_dir / "frames"
    config = ScanConfig(frames_dir=frames, profile=Path(args.profile),
                        out_dir=Path(args.out), recon_binary=Path(args.recon),
                        voxel_mm=args.voxel_mm,
                        input_voxel_mm=args.input_voxel_mm, scale=args.scale,
                        min_confidence=args.min_confidence)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = ScanWindow(config, count=args.count, source=source)
    window.show()

    if not args.headless:
        return int(app.exec())

    # headless: run to completion, snapshot, exit
    window.start_scan()
    deadline = 600.0
    import time
    t0 = time.time()
    while window.worker is not None and window.worker.isRunning():
        app.processEvents()
        if time.time() - t0 > deadline:
            window.stop_scan()
            break
        time.sleep(0.05)
    app.processEvents()
    report = getattr(window.worker, "session", None)
    snap = window.last_snapshot
    if args.snapshot_out:
        window.snapshot_png(args.snapshot_out)
    window.shutdown()
    frames = snap.get("frames_integrated", 0)
    rejected = snap.get("frames_rejected", 0)
    print(f"scan: frames={frames} rejected={rejected} "
          f"snapshot={args.snapshot_out or '-'} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
