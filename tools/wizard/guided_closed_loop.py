#!/usr/bin/env python3
"""Closed-loop guidance rehearsal (see analysis/calibration/guided_operator).

Renders the requested pose, detects the plate, verifies the measured centre is
in the requested 3x3 image cell, retries with a small variation on a refusal,
and ingests a short hold.  Used to verify the guidance end to end.
"""
from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv else ""
sys.path[:] = [entry for entry in sys.path
               if entry not in ("", ".", _SCRIPT_DIR, "tools/wizard")
               and os.path.abspath(entry or ".") != _SCRIPT_DIR]

import json, math, os, sys, tempfile
sys.path[:0] = ["<repo>", "<repo>/tools"]
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
from pathlib import Path
import numpy as np
from tools.wizard import controller, sources, types

SEED, PERT = 7, 1.5
REG = {"top-left": (0, 0), "top": (0, 1), "top-right": (0, 2), "left": (1, 0),
       "centre": (1, 1), "right": (1, 2), "bottom-left": (2, 0),
       "bottom": (2, 1), "bottom-right": (2, 2)}
W, H = types.IMAGE_WIDTH, types.IMAGE_HEIGHT


def main(n=45, finish=False):
    base = sources.SyntheticSource(sources.SyntheticSpec(
        n_views=1, seed=SEED, pattern="single", factory_perturb_px=PERT))
    cam = base.cameras["left"]
    clock, groups, log = 0.0, 0, []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        f = td / "factory.json"
        types.atomic_write_json(f, base.profile_dict())
        info = types.SourceInfo(kind="synthetic", path="synthetic:closed",
                                note="closed loop", continuous=True)
        ctl = controller.WizardController(factory_profile=f, session_dir=td / "s",
                                          mode="high", source=info, ring_keep=2,
                                          async_solve=True)
        ctl.begin()

        def render(pose):
            return next(iter(sources.SyntheticSource(sources.SyntheticSpec(
                n_views=1, seed=SEED, poses=[pose], frames_per_pose=1,
                factory_perturb_px=PERT))))

        def centre(group):
            grids, _ = ctl._detect({k: np.asarray(v) for k, v in group.images.items()})
            o = grids.get("left")
            if o is None or len(o.uv) < 8:
                return None, 0
            pts = np.asarray(list(o.uv.values()), float)
            return pts.mean(axis=0), len(o.uv)

        def emit(pose, label):
            nonlocal clock, groups
            g = render(pose)
            move = None
            for i in range(4):
                clock += 0.6 if i == 0 else 0.1
                fr = types.FrameGroup(
                    index=groups + i, device_group=None, timestamp_s=clock,
                    images={k: g.images[k] for k in ("left", "right", "tex")},
                    slot_ids={}, complete=True, source="synthetic",
                    label=f"{label}-{i}", colour_bgr=g.colour_bgr,
                    continuous=True)
                d = ctl.ingest(fr)
                if i == 0:
                    move = d
            groups += 4
            return move

        def target_pose(t, fb):
            d = float(t.get("distance_mm") or fb[0])
            tt = t.get("tilt_deg") or [None, None]
            tx = float(tt[0]) if tt and tt[0] is not None else fb[1]
            ty = float(tt[1]) if tt and len(tt) > 1 and tt[1] is not None else fb[2]
            roll = float(t["roll_deg"]) if t.get("roll_deg") is not None else fb[3]
            rc = REG.get(str(t.get("region") or ""))
            if rc is None:
                return d, tx, ty, roll, 0.0, 0.0
            u, v = (rc[1] + 0.5) * W / 3.0, (rc[0] + 0.5) * H / 3.0
            z = d
            lx = ly = 0.0
            for _ in range(8):
                lx = (u - cam.cx) * z / cam.fx
                ly = (v - cam.cy) * z / cam.fy
                z = math.sqrt(max(d * d - lx * lx - ly * ly, 1.0))
            return d, tx, ty, roll, lx, ly

        def acquire(t, fb, idx):
            pose = target_pose(t, fb)
            rc = REG.get(str(t.get("region") or ""))
            attempts = []
            for k in range(6):
                g = render(pose)
                c, n = centre(g)
                if c is None:
                    pose = (pose[0], pose[1], pose[2], pose[3],
                            pose[4] * .88, pose[5] * .88)
                    attempts.append({"attempt": k, "centre": None})
                    continue
                u, v = c
                cell = (min(2, max(0, int(v * 3 / H))), min(2, max(0, int(u * 3 / W))))
                attempts.append({"attempt": k, "centre": [round(float(u), 1), round(float(v), 1)],
                                 "cell": list(cell), "markers": n})
                if rc is None or cell == rc:
                    decision = emit(pose, f"v{idx}")
                    if decision and decision.accepted:
                        return pose, decision, attempts
                    # a competent operator varies the view slightly when the
                    # controller refuses it as redundant
                    for j, (dd, dtx, dty, droll) in enumerate((
                            (0.0, 0.0, 0.0, 10.0),
                            (12.0, 5.0, -5.0, -12.0),
                            (-12.0, -5.0, 5.0, 18.0))):
                        p2 = (pose[0] + dd, pose[1] + dtx, pose[2] + dty,
                              pose[3] + droll, pose[4], pose[5])
                        d2 = emit(p2, f"v{idx}r{j}")
                        attempts.append({"attempt": f"retry{j}",
                                         "pose": [round(float(x), 2) for x in p2]})
                        if d2 and d2.accepted:
                            return p2, d2, attempts
                    return pose, decision, attempts
                z = math.sqrt(max(pose[0] ** 2 - pose[4] ** 2 - pose[5] ** 2, 1.0))
                ut, vt = (rc[1] + .5) * W / 3, (rc[0] + .5) * H / 3
                pose = (pose[0], pose[1], pose[2], pose[3],
                        pose[4] + (ut - u) * z / cam.fx,
                        pose[5] + (vt - v) * z / cam.fy)
            return pose, emit(pose, f"v{idx}u"), attempts

        last = (420., 0., 0., 0., 0., 0.)
        d = emit(last, "boot")
        log.append({"view": 0, "pose": list(last), "accepted": bool(d.accepted),
                    "reasons": list(d.reasons), "instruction": "bootstrap",
                    "attempts": []})
        for i in range(1, n):
            snap = ctl.snapshot()
            if (snap.get("coverage") or {}).get("ready"):
                break
            g = snap.get("guidance") or {}
            t = g.get("target") or {}
            if g.get("priority") == "none" or t.get("mode") == "done":
                break
            pose, d, at = acquire(t, last, i)
            m = ctl._measurement or {}
            log.append({"view": i, "pose": [round(float(x), 2) for x in pose],
                        "accepted": bool(d and d.accepted),
                        "reasons": list(d.reasons) if d else [],
                        "instruction": str(g.get("instruction") or ""),
                        "target_tilt": t.get("tilt_deg"),
                        "y_sat": ((m.get("checks") or {}).get("tilt_axis_y") or {}).get("satisfied"),
                        "q_sat": ((m.get("checks") or {}).get("tilt_quadrants") or {}).get("satisfied"),
                        "attempts": at})
            last = pose
        m = ctl._measurement or {}
        r = ctl._readiness_cache or {}
        out = {"views": len(log), "accepted": len(ctl.session.accepted),
               "coverage": {f"D{k}": (m.get(f"D{k}") or {}).get("satisfied") for k in range(1, 9)},
               "ready": r.get("ready"), "missing": r.get("missing"), "log": log}
        if finish:
            fin = ctl.finish()
            v = fin.get("validation") or {}
            out["finish"] = {"ok": fin.get("ok"), "passed": v.get("passed"),
                             "would": (v.get("verdict") or {}).get("would_activate"),
                             "rms": v.get("rms_px"),
                             "reasons": (v.get("verdict") or {}).get("reasons")}
        else:
            ctl.cancel()
        Path("/tmp/closed_fix.json").write_text(json.dumps(out, indent=1))
        print(json.dumps({k: out[k] for k in ("views", "accepted", "ready", "missing", "coverage")}, indent=1))
        if "finish" in out:
            print("finish", out["finish"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 45,
                          "--finish" in sys.argv))
