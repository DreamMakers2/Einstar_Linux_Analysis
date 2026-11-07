#!/usr/bin/env python3
"""Sanity tests for the Einstar FPGA payload decoder.

Run with either:
    pytest -q tests/test_fpga.py
    python3 tests/test_fpga.py

The assertions are deliberately specific: they pin the vendor-mandated
constants of the Pango Logos configuration wire format and the identity of
the configured device, so a regression in the parser (or a swapped payload)
fails loudly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "tools", "fpga", "fpga_bits.py")
sys.path.insert(0, HERE)
from support import FIXTURES  # noqa: E402

PAYLOAD = str(FIXTURES / "EinScan10_EN_fpga.bin")

EXPECTED_SIZE = 1_005_676
EXPECTED_SHA256 = "cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4"
EXPECTED_IDCODE = 0x00511899          # Pango Table 3-8 -> PGL25G
EXPECTED_SYNC = 0x01332D94


def _load_tool():
    spec = importlib.util.spec_from_file_location("fpga_bits", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


@pytest.fixture(scope="module")
def payload():
    if not os.path.exists(PAYLOAD):
        pytest.skip(f"proprietary FPGA payload not available: {PAYLOAD}")
    with open(PAYLOAD, "rb") as fh:
        data = fh.read()
    return data


@pytest.fixture(scope="module")
def parsed(tool, payload):
    words = list(tool.struct.unpack(">%dI" % (len(payload) // 4), payload))
    info, packets = tool.analyse(payload, words, PAYLOAD)
    return info, packets, words


# ---------------------------------------------------------------------------
# artifact identity
# ---------------------------------------------------------------------------
def test_size_and_hash(payload):
    assert len(payload) == EXPECTED_SIZE
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_SHA256
    assert len(payload) % 4 == 0


# ---------------------------------------------------------------------------
# Pango wire-format constants (UG020005 V2.3 sections 3.1.2.1 / 3.1.2.2)
# ---------------------------------------------------------------------------
def test_bus_width_detection_sequence(payload):
    assert payload[0:4] == bytes.fromhex("000000aa")
    assert payload[4:8] == bytes.fromhex("08100020")   # 32-bit bus variant


def test_padding_words(payload):
    words = [int.from_bytes(payload[i:i+4], "big") for i in range(8, 0x30, 4)]
    assert words == [0xFFFFFFFF] * 10


def test_sync_word(payload):
    assert payload[0x30:0x34] == EXPECTED_SYNC.to_bytes(4, "big")


# ---------------------------------------------------------------------------
# packet stream
# ---------------------------------------------------------------------------
def test_parse_consumes_whole_file(parsed):
    info, packets, words = parsed
    assert info["end_offset"] == info["size"]
    assert "trailing_bytes" not in info
    assert info["packet_count"] == len(packets)


def test_packet_types(parsed):
    info, packets, words = parsed
    assert info["type1_packets"] == 187
    assert info["type2_packets"] == 2
    # every non-Type2 packet must be a Type1 with a valid register number
    for p in packets:
        if p.ptype != 2:
            assert p.ptype == 5
            assert 0 <= p.reg <= 27


def test_no_op_padding(parsed):
    info, packets, words = parsed
    # 258 words equal 0xA0000000: 160 are Type-1 no-op packet headers,
    # the other 98 are configuration-data words that happen to match.
    assert info["a0000000_words"] == 258
    assert info["a0000000_runs"] == 103
    assert info["a0000000_longest_run"] == 100
    assert info["a0000000_longest_run_offset"] == 0xF56DC


def test_config_data_packets(parsed):
    info, packets, words = parsed
    t2 = [p for p in packets if p.ptype == 2]
    assert [p.count for p in t2] == [35_360, 215_832]
    assert [p.offset for p in t2] == [0x108, 0x229F0]
    # the first block is literally all zero bytes
    assert t2[0].data_offset == 0x10C
    assert words[0x10C // 4:(0x10C // 4) + 16] == [0] * 16
    assert info["config_words"] == 251_192
    assert info["config_bytes"] == 1_004_768


def test_device_idcode(parsed):
    info, packets, words = parsed
    assert info["idcode"] == "0x00511899"
    assert info["device"] == "PGL25G"
    assert info["device_bitstream_kib"] == 982
    # payload size rounds to the vendor's uncompressed PGL25G bitstream size
    assert round(info["size"] / 1024) == 982


def test_register_init_block(parsed):
    info, packets, words = parsed
    regs = info["register_writes"]
    for name in ("RCRR", "SBPIR", "IRSTCTRLR", "IRSTADRR", "CMDR", "IDR",
                 "WATCHDOGR", "CMASKR", "CTRL0R", "OPTION0R", "OPTION1R",
                 "FALLBACKR", "ADRR"):
        assert name in regs, name
    # CMEMIR only ever appears as an empty address-select packet
    assert info["empty_packet_registers"].get("CMEMIR") == 2
    cmds = [c["name"] for c in info["commands"]]
    assert cmds[:3] == ["NOP", "RSTCRC", "SWITCH"]
    assert cmds[-3:] == ["GUP", "SWAKEUP", "DESYNC"]
    # first write targets DRM content type (ADRR[26:25] == 01)
    assert info["frame_address_writes"][0]["value"] == 0x02000000
    assert info["frame_address_writes"][1]["value"] == 0x00000000


def test_checksum_registers(parsed):
    info, packets, words = parsed
    crcs = [c["value"] for c in info["crc_writes"]]
    assert crcs == [0x0C10164C, 0x86393275]


# ---------------------------------------------------------------------------
# compression / entropy checks (no compressed section expected)
# ---------------------------------------------------------------------------
def test_uncompressed_sparse_data(tool, payload, parsed):
    info, packets, words = parsed
    # whole-file entropy is far below the ~7.9 bit/byte of a compressed or
    # encrypted stream; the low value comes from empty configuration frames
    assert 2.5 < info["entropy"] < 4.0
    data_region = payload[0x22000:]
    assert 3.0 < tool._entropy(data_region) < 4.0
    assert payload[0x10C:0x2298C] == b"\x00" * (0x2298C - 0x10C)
    assert info["zero_frac"] > 0.35


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------
def _run(*args):
    return subprocess.run([sys.executable, TOOL, *args],
                          capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("flag", ["--info", "--opcodes", "--hist", "--dump-words"])
def test_cli_modes(flag):
    if not os.path.exists(PAYLOAD):
        pytest.skip("proprietary FPGA payload not available")
    r = _run(flag)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()


def test_cli_json_out(tmp_path):
    if not os.path.exists(PAYLOAD):
        pytest.skip("proprietary FPGA payload not available")
    out = tmp_path / "fpga.json"
    r = _run("--info", "--json-out", str(out))
    assert r.returncode == 0, r.stderr
    rep = json.loads(out.read_text())
    assert rep["device"] == "PGL25G"
    assert rep["idcode"] == "0x00511899"
    assert rep["config_bytes"] == 1_004_768
    assert len(rep["packets"]) == 189


def test_cli_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\x00" * 64)
    r = _run(str(bad), "--info")
    assert r.returncode != 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
