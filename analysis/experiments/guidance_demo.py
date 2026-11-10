#!/usr/bin/env python3
"""Render a guided-session demo: closed-loop simulation + dashboard frames.

Produces ``outdir/dash_XX.png`` for several accepted views and an ``index.html``
contact sheet, plus a text log of the recommendations and rejections.  This is
the visual-feedback prototype required by the design; it runs headless.
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from calib import ui                                  # noqa: E402
from calib.session import CalibrationSession, FrameSet  # noqa: E402
from calib.simloop import VirtualOperator, render_frame  # noqa: E402
from calib.synth import make_scene, perturbed_init    # noqa: E402


def main(outdir: str, mode: str = "standard", max_views: int = 10, seed: int = 4):
    os.makedirs(outdir, exist_ok=True)
    scene = make_scene(n_views=25, noise_px=0.2, outlier_frac=0.01, seed=seed)
    rng = np.random.default_rng(seed)
    op = VirtualOperator(scene, noise_px=0.2, outlier_frac=0.01, render=True, rng=rng)
    R0, t0 = scene.views[0]
    op.pose = (np.asarray(R0), np.asarray(t0))
    sess = CalibrationSession(mode=mode, seed=seed)
    sess.cameras = perturbed_init(scene, rng)
    paths, log = [], []
    ts = 0.0
    for step in range(max_views):
        frames, grids = op.capture(timestamp=ts)
        dec = sess.ingest(frames, grids=grids)
        rec = sess.recommend()
        log.append({"step": step, "accepted": dec.accepted, "reasons": dec.reasons,
                    "text": rec.get("text", ""), "n": len(sess.accepted)})
        if dec.accepted and len(paths) < 8:
            p = os.path.join(outdir, f"dash_{len(paths):02d}.png")
            ui.dashboard(sess, p, frame=frames.images.get("left"), grids=grids,
                         recommendation=rec)
            paths.append(p)
        R, t = rec.get("target_pose", op.pose)
        if not dec.accepted and not sess.rejected:
            pass
        op.pose = (np.asarray(R, float), np.asarray(t, float))
        ts += 0.8
        if len(sess.accepted) >= max_views:
            break
    ui.contact_sheet(paths, os.path.join(outdir, "index.html"))
    with open(os.path.join(outdir, "log.txt"), "w", encoding="utf-8") as f:
        for row in log:
            f.write(f"{row}\n")
    print(f"{len(paths)} dashboards, accepted {len(sess.accepted)}, "
          f"stop={sess.stop_decision()['stop']}")
    return paths


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/einstar_cal2/out/demo")
