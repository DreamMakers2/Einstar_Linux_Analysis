#!/usr/bin/env python3
"""Test fixture: publish a plausible ``einstar-live-status-v1`` file at ~2 Hz.

Used to exercise the viewer's LIVE -> STALE transition without a scanner:

    python tools/liveview/status_fixture.py --output /tmp/lv/status.json \
        --frames /tmp/lv/frames --duration 3

The last JSON is left behind (atomically written), so the viewer sees it go
stale exactly as it would when a capture process exits.
"""
from __future__ import annotations

import argparse
import json
import os
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="status JSON path")
    parser.add_argument("--frames", default="/tmp/einstar-frames",
                        help="value for output_directory")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--groups", type=int, default=350)
    parser.add_argument("--rate", type=float, default=2.0, help="writes per second")
    parser.add_argument("--throughput", type=float, default=39.4, help="MB/s")
    parser.add_argument("--dropped", type=int, default=2)
    parser.add_argument("--simulated", action="store_true")
    args = parser.parse_args()

    path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    start = time.time()
    written = 0
    while True:
        elapsed = time.time() - start
        if elapsed >= args.duration:
            break
        written = min(args.groups, max(1, int(args.groups * elapsed / args.duration)))
        status = {
            "schema": "einstar-live-status-v1",
            "elapsed_s": round(elapsed, 3),
            "groups_written": written,
            "groups_requested": args.groups,
            "latest_group": written - 1,
            "frames_completed": written * 3,
            "frames_dropped": args.dropped,
            "bytes_received": int(args.throughput * 1e6 * elapsed),
            "throughput_mbps": args.throughput,
            "transfer_timeouts": 0,
            "frames_invalid": 0,
            "output_directory": os.path.abspath(args.frames),
            "trigger_period_us": 100000,
            "exposure_us": [1200, 1200, 5000],
            "gain": [105, 105, 180],
            "laser_percent": 100,
            "simulated": bool(args.simulated),
            "live": True,
        }
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(status, handle)
            handle.write("\n")
        os.replace(temporary, path)
        time.sleep(1.0 / max(0.2, args.rate))
    print(f"status fixture: wrote {path} for {args.duration:.1f}s "
          f"({written}/{args.groups} groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
