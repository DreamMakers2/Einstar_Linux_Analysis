#!/usr/bin/env python3
"""Cross-check the protocol model against a real EXStar session log.

The log (`analysis/logs/session_2026-02-09.log`, 637,523 lines, 132 MB) was produced by the
vendor stack while a real Einstar was connected. It is not committed (see .gitignore); this
test skips when it is absent. When present it proves that:

  * every operation the vendor UI performed maps to a command name we model;
  * the device reported exactly the firmware string our docs claim;
  * the calibration blob is `ReadData(offset=0, size=6568)`;
  * the operational scan parameters match docs/protocol/einstar_usb_protocol.md.

Run standalone: python3 tests/test_log_replay.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "analysis" / "logs" / "session_2026-02-09.log"
GZ = ROOT / "analysis" / "logs" / "session_2026-02-09.log.gz"

# Operations observed in the log -> what they must be in our model.
KNOWN_OPS = {
    "openDevices", "closeDevices", "getProperty", "setProperty", "setFlagBitInterface",
    "execute", "ReadData", "WriteData", "UpdateFirmware",
}
KNOWN_PROPERTIES = {
    "CameraExposure", "CameraGain", "CameraExposureLevel", "CameraResolution", "CameraSerial",
    "SensorCount", "VendorName", "Vid", "ProductName", "Pid", "FirmwareVersion", "Serial",
    "ScanTriggerPeriod", "HeartBeatPeriod", "Temperature", "LDRGBLuminance", "StrobeLuminance",
    "TriggerMode", "ConvertParam", "Progress", "Indication", "ImageStream",
}

EXPECTED_FIRMWARE = "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN"


def _iter_lines():
    if LOG.exists():
        with LOG.open("rb") as f:
            for line in f:
                yield line.decode("utf-8", "replace")
    elif GZ.exists():
        import gzip

        with gzip.open(GZ, "rt", errors="replace") as f:
            yield from f
    else:
        return


def _available() -> bool:
    return LOG.exists() or GZ.exists()


def test_operations_are_modelled() -> None:
    if not _available():
        print("  (skipped: session log not present)")
        return
    seen_ops: set[str] = set()
    seen_props: set[str] = set()
    for line in _iter_lines():
        m = re.search(r'recvExecuteSignal begin, plugin: "\w+"  cmd: "([^"]+)"', line)
        if m:
            seen_ops.add(m.group(1))
        m = re.search(r'InternalE10Device::execute : execute start\. cmd:  "([^"]+)"', line)
        if m:
            seen_ops.add(m.group(1))
        for m in re.finditer(r'(?:get|set)Property start\. cmd:  "([^"]+)"', line):
            seen_props.add(m.group(1))
    unknown_ops = seen_ops - KNOWN_OPS
    unknown_props = seen_props - KNOWN_PROPERTIES
    assert not unknown_ops, f"unmodelled operations in real log: {sorted(unknown_ops)}"
    assert not unknown_props, f"unmodelled properties in real log: {sorted(unknown_props)}"
    assert {"openDevices", "getProperty", "setProperty"} <= seen_ops
    # the execute-style operations appear through the device plugin's own log line
    assert {"ReadData"} <= seen_ops, sorted(seen_ops)


def test_device_reports_claimed_firmware() -> None:
    if not _available():
        return
    found = False
    for line in _iter_lines():
        if EXPECTED_FIRMWARE in line and "FirmwareVersion" in line:
            found = True
            break
    assert found, "device never reported the documented firmware string"


def test_calibration_read_is_offset_zero_6568() -> None:
    if not _available():
        return
    seen = False
    for line in _iter_lines():
        if "readData offset:" in line:
            m = re.search(r"readData offset:\s*(\d+)\s*,\s*dataSize:\s*(\d+)", line)
            if m:
                assert (int(m.group(1)), int(m.group(2))) == (0, 6568), m.groups()
                seen = True
    assert seen, "no ReadData call found"


def test_scan_parameters() -> None:
    if not _available():
        return
    periods: set[int] = set()
    modes: set[tuple[int, int]] = set()
    for line in _iter_lines():
        m = re.search(r'"ScanTriggerPeriod" , param:  QVariant\(double, (\d+)\)', line)
        if m:
            periods.add(int(m.group(1)))
        m = re.search(
            r'"TriggerMode" , param:  QVariant\(QVariantMap, QMap\(\("MONOTRIGCOUNT", '
            r'QVariant\(double, (\d+)\)\)\("RGBTRIGCOUNT", QVariant\(double, (\d+)\)\)',
            line,
        )
        if m:
            modes.add((int(m.group(1)), int(m.group(2))))
    assert periods, "no ScanTriggerPeriod observed"
    assert 68000 in periods, f"expected the documented 68000 us scan period, saw {sorted(periods)}"
    assert (3, 1) in modes, f"expected TriggerMode (3,1), saw {sorted(modes)}"


def main() -> int:
    failures = 0
    names = [n for n in sorted(globals()) if n.startswith("test_") and callable(globals()[n])]
    for name in names:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except BaseException as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(names) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
