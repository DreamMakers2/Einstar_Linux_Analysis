"""Volatile exposure/gain control through the real capture executable."""
from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

from tools.wizard.capture import CaptureConfig, LiveCaptureProcess

REPO = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("EINSTAR_CAPTURE_TEST_BINARY",
                             REPO / "build/driver/einstar-capture-frames"))


@pytest.mark.skipif(not BINARY.is_file(), reason="capture tool not built")
def test_partial_exposure_request_merges_current_cameras(tmp_path):
    config = CaptureConfig(
        session_dir=tmp_path, binary=BINARY, backend="simulated", groups=0,
        max_seconds=2.0, ring=0)
    cap = LiveCaptureProcess(config)
    cap.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and cap.status().get("state") != "running":
            time.sleep(0.05)
        cap.set_volatile(exposure_us={0: 1800}, source="test")
        deadline = time.time() + 5
        status = {}
        while time.time() < deadline:
            status = cap.status()
            if (status.get("exposure_us") or [None])[0] == 1800:
                break
            time.sleep(0.05)
        assert status.get("exposure_us") == [1800, 1200, 5000]
        # A partial gain request uses the current gain for the other cameras.
        cap.set_volatile(gain={2: 200}, source="test")
        deadline = time.time() + 5
        while time.time() < deadline:
            status = cap.status()
            if (status.get("gain") or [None, None, None])[2] == 200:
                break
            time.sleep(0.05)
        assert status.get("gain") == [105, 105, 200]
    finally:
        cap.stop()
        cap.close()
