#!/usr/bin/env python3
"""Regression tests for the Einstar firmware toolkit.

Run standalone:   python3 tests/test_firmware.py
Also pytest-compatible (functions named test_*).

These tests assert the reverse-engineered container invariants against the four
official images in the repository.  They must keep passing: if a future
firmware changes the format, these tests are the tripwire.
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "firmware"))
sys.path.insert(0, str(ROOT / "tests"))

import fx3img  # noqa: E402
from support import FIXTURES, Skip, require  # noqa: E402

FW = FIXTURES / "firmware"
OD = FW / "official_downloads"

EN = FW / "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img"
CH = FW / "EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_CH_IAP.img"
V1 = OD / "EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img"
BOOT = OD / "Fx3BootAppGcc.img"

EXPECTED = {
    "en_sha256": "52346b33c7b0337566d7cfe8d453533c46f18898e4e7f82c1c00c885a8c68c3d",
    "ch_sha256": "0c5a41b5bfdc7f50fb6b55794c36fc4b6200ab28709d25b3e4b82d93fce6e7c5",
    "v1_sha256": "067ce53da1cd00c2bab9b61ba3bc1111b631dbb5faac189c1c4a9e8a3b0c9e45",
    "boot_sha256": "2d3f346c190147727a6ed00753ad4d047c975b9603be9ac5b46d450ebb33713e",
    "en_fpga_sha256": "cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4",
    "en_entry": 0x40016F50,
    "en_checksum": 0x0FE314BC,
    "ch_checksum": 0x0FE30EBA,
    "v1_entry": 0x40015488,
    "v1_checksum": 0x91AEFEAA,
}


def _require(p: Path) -> Path:
    return require(p)


def test_record_container_checksums() -> None:
    fw = fx3img.load(_require(EN))
    assert fw.container == "records"
    assert len(fw.records) == 320
    assert fw.checksum_errors == []
    assert len(fw.raw) == 1311040
    assert fw.sha256 == EXPECTED["en_sha256"]


def test_fx3_sections_and_checksum() -> None:
    fw = fx3img.load(_require(EN))
    assert fw.image_ctl == 0x1C and fw.image_type == 0xB0
    assert [s.words if hasattr(s, "words") else s.n_words for s in fw.sections] == [
        2378, 16384, 16384, 6989, 5428
    ]
    assert [s.address for s in fw.sections] == [
        0x100, 0x40003000, 0x40013000, 0x40023000, 0x40030000
    ]
    assert fw.entry == EXPECTED["en_entry"]
    assert fw.stored_checksum == fw.computed_checksum == EXPECTED["en_checksum"]
    assert fw.terminator_offset == 0x2E758


def test_deinterleave_is_exactly_record_stripping() -> None:
    raw = _require(EN).read_bytes()
    fw = fx3img.load(_require(EN))
    manual = b"".join(raw[i * 0x1001 : i * 0x1001 + 0x1000] for i in range(320))
    assert manual == fw.image
    assert len(fw.image) == 0x140000


def test_en_ch_differ_only_in_locale_and_checksums() -> None:
    en = fx3img.load(_require(EN))
    ch = fx3img.load(_require(CH))
    assert ch.sha256 == EXPECTED["ch_sha256"]
    diffs = [i for i, (a, b) in enumerate(zip(en.image, ch.image)) if a != b]
    assert diffs == [0xCD2C, 0xCD2D, 0x2E760, 0x2E761], [hex(d) for d in diffs]
    # locale word
    assert en.image[0xCD2C:0xCD2E] == b"EN"
    assert ch.image[0xCD2C:0xCD2E] == b"CH"
    # checksum drops by exactly the word difference
    assert en.stored_checksum - ch.stored_checksum == 0x602
    # affected 8-bit record checksums each drop by 8
    assert en.raw[0xD00C] - ch.raw[0xD00C] == 8
    assert en.raw[0x2F02E] - ch.raw[0x2F02E] == 8


def test_fpga_payload_identical_en_ch_and_offset() -> None:
    en = fx3img.load(_require(EN))
    ch = fx3img.load(_require(CH))
    off_e, len_e = fx3img.find_fpga_payload(en.image, en.terminator_offset + 12)
    off_c, len_c = fx3img.find_fpga_payload(ch.image, ch.terminator_offset + 12)
    assert off_e == off_c == 0x40190
    assert len_e == len_c == 1005676
    blob = en.image[off_e : off_e + len_e]
    assert hashlib.sha256(blob).hexdigest() == EXPECTED["en_fpga_sha256"]
    assert blob == ch.image[off_c : off_c + len_c]


def test_raw_recovery_image_parses() -> None:
    fw = fx3img.load(_require(V1))
    assert fw.container == "raw"
    assert fw.sha256 == EXPECTED["v1_sha256"]
    assert fw.entry == EXPECTED["v1_entry"]
    assert fw.stored_checksum == fw.computed_checksum == EXPECTED["v1_checksum"]
    assert [s.n_words for s in fw.sections] == [0x8C8, 0x4000, 0x4000, 0x10E0, 0x10A2]


def test_boot_app_image() -> None:
    fw = fx3img.load(_require(BOOT))
    assert fw.sha256 == EXPECTED["boot_sha256"]
    assert fw.entry == 0x40078000
    assert [s.address for s in fw.sections] == [0x40078000, 0x4007F000]
    assert fw.stored_checksum == fw.computed_checksum


def test_elf_emitter_places_sections_at_load_addresses() -> None:
    fw = fx3img.load(_require(EN))
    elf = fx3img.build_elf(fw)
    assert elf[:4] == b"\x7fELF"
    assert elf[4] == 1 and elf[5] == 1  # ELF32 LE
    e_entry = struct.unpack_from("<I", elf, 24)[0]
    assert e_entry == EXPECTED["en_entry"]
    phoff = struct.unpack_from("<I", elf, 28)[0]
    phentsize, phnum = struct.unpack_from("<HH", elf, 42)
    assert phentsize == 32 and phnum == 5
    vaddrs = []
    for i in range(phnum):
        p_type, p_off, p_vaddr, p_paddr, p_filesz = struct.unpack_from(
            "<IIIII", elf, phoff + i * phentsize
        )
        assert p_type == 1
        vaddrs.append(p_vaddr)
        # congruence requirement for mmap-able segments
        assert (p_off & 0xFFF) == (p_vaddr & 0xFFF)
        # segment content must equal the section data
        data = fw.sections[i].data
        assert elf[p_off : p_off + p_filesz] == data
    assert vaddrs == [0x100, 0x40003000, 0x40013000, 0x40023000, 0x40030000]


def test_corrupt_record_is_detected(tmp_path: Path) -> None:
    raw = bytearray(_require(EN).read_bytes())
    raw[0x10] ^= 0xFF  # flip a payload byte in record 0
    bad = tmp_path / "bad.img"
    bad.write_bytes(bytes(raw))
    fw = fx3img.load(bad)
    assert 0 in fw.checksum_errors


def test_truncated_section_fails(tmp_path: Path) -> None:
    raw = bytearray(_require(EN).read_bytes())
    # Corrupt section 2 length so the chain runs off the end.
    img = bytearray(b"".join(raw[i * 0x1001 : i * 0x1001 + 0x1000] for i in range(320)))
    struct.pack_into("<I", img, 0x2534, 0x7FFFFFFF)
    # Re-interleave so the loader sees a valid container with a bad FX3 chain.
    out = bytearray()
    for i in range(320):
        block = img[i * 0x1000 : (i + 1) * 0x1000]
        out += block + bytes([sum(block) & 0xFF])
    p = tmp_path / "trunc.img"
    p.write_bytes(bytes(out))
    try:
        fx3img.load(p)
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for a section that runs past the image")


def main() -> int:
    failures = 0
    skipped = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if fn.__code__.co_argcount == 1:
                import tempfile

                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Skip as exc:
            skipped += 1
            print(f"SKIP {name}: {exc}")
        except BaseException as exc:  # noqa: BLE001 - SystemExit from the tool counts too
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    total = len([n for n in globals() if n.startswith("test_")])
    print(f"\n{total - failures - skipped} passed, {skipped} skipped, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
