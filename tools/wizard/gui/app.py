"""Entry point for the Einstar calibration wizard.

    tools/wizard/gui/app.py --source live --session-dir ~/calib-session
    tools/wizard/gui/app.py --source replay --frames DIR [--pace 10]
    tools/wizard/gui/app.py --source recording --recording RAW.e1r
    tools/wizard/gui/app.py --source synthetic --scene full --demo
    tools/wizard/gui/app.py --source replay --frames DIR --headless --duration 20 \
        --snapshot-out /tmp/wizard.png

``--demo`` uses the deterministic demo controller (no real calibration); it is
for layout work and automated snapshot tests only.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[2], _HERE.parents[3]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def parse(argv=None):
    ap = argparse.ArgumentParser(description="Einstar calibration wizard (Qt6)")
    ap.add_argument("--source", choices=("live", "replay", "recording", "synthetic", "vendor"),
                    default="replay")
    ap.add_argument("--frames", help="frame directory (replay)")
    ap.add_argument("--recording", help=".e1r raw recording (recording)")
    ap.add_argument("--scene", default="default", help="synthetic scene name")
    ap.add_argument("--views", type=int, default=0,
                    help="synthetic view count (0 = source default)")
    ap.add_argument("--noise", type=float, default=None,
                    help="synthetic image noise sigma (default: source)")
    ap.add_argument("--seed", type=int, default=0,
                    help="synthetic scene seed")
    ap.add_argument("--async-decisions", action="store_true",
                    help="replay/synthetic: split preview from decisions")
    ap.add_argument("--session-dir", default=str(Path.home() / "einstar-calibration"))
    ap.add_argument("--profile", default=str(Path.home() / "einstar-private" /
                                             "einstar-profiles" / "factory-e10.json"))
    ap.add_argument("--mode", default="standard", choices=("quick", "standard", "high"))
    ap.add_argument("--pace", type=float, default=0.0,
                    help="replay pacing in Hz (0 = as fast as possible)")
    ap.add_argument("--loop", action="store_true", help="loop a replay source")
    ap.add_argument("--max-groups", type=int, default=0)
    ap.add_argument("--demo", action="store_true",
                    help="use the deterministic demo controller (layout/snapshot tests)")
    ap.add_argument("--resume", action="store_true",
                    help="continue the session already in --session-dir")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--duration", type=float, default=8.0)
    ap.add_argument("--snapshot-out", default="")
    ap.add_argument("--autostart", action="store_true",
                    help="start recording immediately (default in headless mode)")
    ap.add_argument("--no-auto-exposure", action="store_true",
                    help="keep the configured IR/RGB exposure fixed")
    ap.add_argument("--finish", action="store_true",
                    help="headless: run the final solve and build the candidate "
                         "profile instead of cancelling")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse(argv)
    if args.headless:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6 import QtCore, QtWidgets

    from wizard.gui import controller_adapter
    from wizard.gui.modern_window import ModernMainWindow as MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

    scene = args.scene
    if args.source == "synthetic":
        from wizard.sources import SyntheticSpec
        kw = {"pattern": str(args.scene), "seed": int(args.seed)}
        if args.views:
            kw["n_views"] = int(args.views)
        if args.noise is not None:
            kw["noise_sigma"] = float(args.noise)
        scene = SyntheticSpec(**kw)
    source = controller_adapter.open_source(
        args.source, frames=args.frames, recording=args.recording,
        session_dir=args.session_dir, scene=scene, loop=args.loop)
    if args.demo or not controller_adapter.real_controller_available():
        controller = controller_adapter.DemoController(
            factory_profile=args.profile, session_dir=args.session_dir,
            mode=args.mode, source_kind=args.source)
    else:
        controller = controller_adapter.make_controller(
            factory_profile=args.profile, session_dir=args.session_dir,
            mode=args.mode, source_kind=args.source,
            source_info=getattr(source, "info", None),
            # the capture process belongs to the live source; without it the
            # controller cannot report fps and the wizard shows a false stall
            capture=getattr(source, "capture", None))

    if args.resume:
        info = controller.resume()
        print(f"wizard: resume {info}")
    win = MainWindow(controller, source, pace_hz=args.pace,
                     headless=args.headless, max_groups=args.max_groups,
                     autostart=(args.autostart or
                                (not args.headless and args.source == "live")),
                     auto_exposure=not args.no_auto_exposure,
                     async_decisions=(True if args.async_decisions else None))
    win.show()
    win.maybe_autostart()

    if args.headless or args.autostart:
        win._on_start()

    if args.headless:
        deadline = time.time() + max(0.5, args.duration)
        while time.time() < deadline:
            app.processEvents()
            if (args.finish and win.worker is not None
                    and not win.worker.isRunning()):
                # A finite source (replay/synthetic) has been fully consumed:
                # no need to idle for the rest of the safety duration before
                # starting the final solve.
                break
            time.sleep(0.02)
        path = args.snapshot_out or str(Path(args.session_dir) / "wizard_snapshot.png")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        win.snapshot_png(path)
        snap = controller.snapshot()
        cov = snap.get("coverage") or {}
        ready = snap.get("readiness") or {}
        print("wizard: "
              f"state={snap.get('state')} "
              f"accepted={(snap.get('counts') or {}).get('accepted', 0)} "
              f"rejected={(snap.get('counts') or {}).get('rejected', 0)} "
              f"coverage={(cov.get('satisfied', 0))}/{(cov.get('total', 0))} "
              f"ready={ready.get('ready')} "
              f"missing={','.join(str(m) for m in (ready.get('missing') or [])[:4]) or '-'} "
              f"elapsed_s={args.duration:.0f} snapshot={path}")
        try:
            if args.finish:
                win._on_finish()
                # the solve is deliberately off the GUI thread; the headless
                # driver must wait for it (the old immediate read raced it)
                win.wait_for_finish(600.0)
                app.processEvents()
            else:
                win._on_cancel()
            win.shutdown()
        finally:
            try:
                source.close()
            except Exception:
                pass
            win.close()
            win.deleteLater()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()
        return 0

    rc = app.exec()
    try:
        source.close()
    except Exception:
        pass
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
