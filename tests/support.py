"""Shared helpers for the published test suite.

The repository intentionally does **not** ship proprietary artefacts (vendor
DLLs, firmware images, factory calibration records).  Tests that need those
fixtures look them up under ``fixtures/`` (or ``$EINSTAR_FIXTURES``) and skip
cleanly when they are absent, so the public suite always passes.

A reviewer who has the vendor files can run the full evidence-backed suite:

    EINSTAR_FIXTURES=/path/to/fixtures python3 -m pytest -q tests/

Expected layout (all optional):

    fixtures/firmware/EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img
    fixtures/firmware/EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_CH_IAP.img
    fixtures/firmware/official_downloads/EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img
    fixtures/firmware/official_downloads/Fx3BootAppGcc.img
    fixtures/res_Einscan-E10_200x150/{Left,Right,Tex}CCF.txt
    fixtures/ccf_200x150/{Left,Right,Tex}CCF.txt
    fixtures/calibration.bin
    fixtures/EinScan10_EN_fpga.bin
    fixtures/session.log
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(os.environ.get("EINSTAR_FIXTURES", ROOT / "fixtures"))


class Skip(Exception):
    """Raised when an optional fixture is not available."""


def require(path: Path) -> Path:
    """Return *path* if it exists, otherwise skip the current test."""
    p = Path(path)
    if p.exists():
        return p
    message = f"fixture not available: {p} (set EINSTAR_FIXTURES)"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest  # noqa: PLC0415

        pytest.skip(message)
    raise Skip(message)


def run(namespace: dict) -> int:
    """Standalone runner: executes ``test_*`` callables, counting skips."""
    failures = 0
    ran = 0
    skipped = 0
    names = [n for n in sorted(namespace) if n.startswith("test_") and callable(namespace[n])]
    for name in names:
        fn = namespace[name]
        try:
            fn()
            print(f"PASS {name}")
            ran += 1
        except Skip as exc:
            print(f"SKIP {name}: {exc}")
            skipped += 1
        except BaseException as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failures += 1
    print(f"\n{ran} passed, {skipped} skipped, {failures} failed")
    return 1 if failures else 0
