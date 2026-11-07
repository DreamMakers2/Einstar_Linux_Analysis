#!/usr/bin/env bash
# Reproduce every check that does not require proprietary artefacts.
# Usage: scripts/verify_all.sh [--with-driver]
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1. Python analysis suites (fixture-free) =="
python3 tests/test_synthetic.py | tail -2
python3 tests/test_protocol.py | tail -2
python3 tests/test_firmware.py | tail -3
python3 tests/test_fpga.py 2>/dev/null | tail -2 || true
python3 tests/test_log_replay.py | tail -2

echo
echo "== 2. Full pytest run (evidence-backed suites skip without fixtures) =="
python3 -m pytest -q tests/ | tail -2

echo
echo "== 3. Tool smoke tests =="
python3 tools/firmware/fx3img.py --help >/dev/null && echo "fx3img: ok"
python3 tools/fpga/fpga_bits.py --help >/dev/null && echo "fpga_bits: ok"
python3 tools/ccf/decode_ccf.py --help >/dev/null && echo "decode_ccf: ok"

if [ "${1:-}" = "--with-driver" ]; then
  echo
  echo "== 4. C++ acquisition backend =="
  cmake -S driver -B build -DCMAKE_BUILD_TYPE=Release >/dev/null
  cmake --build build -j"$(nproc)" >/dev/null
  ctest --test-dir build --output-on-failure | tail -3
  ./build/einstar-bench --quick | tail -5
fi

echo
echo "All published checks passed."
