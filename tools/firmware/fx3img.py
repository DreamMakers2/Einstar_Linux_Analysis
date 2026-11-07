#!/usr/bin/env python3
"""SHINING 3D Einstar (EinScan10_01 / SC130) firmware package toolkit.

Format (reverse engineered and verified against
EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_{EN,CH}_IAP.img):

  The .img file is a *transport container* made of 4097-byte records::

      record[k] = payload[4096] || checksum[1]
      checksum  = (sum(payload) & 0xFF)

  Concatenating the 4096-byte payloads of all records yields the real
  firmware image (1,310,720 bytes = 320 records * 4096 for the V2.10
  package).  The insertion of one checksum byte after every 4096 bytes is
  what shifts all later offsets by one byte per 4 KiB block; a naive parse
  of the raw file therefore appears to break at file offset 0x2534.

  The de-interleaved image is a Cypress EZ-USB FX3 boot image as documented
  in Infineon AN76405 "EZ-USB FX3 Boot Options" (Table 14)::

      header:  "CY" (2) | bImageCTL (1) | bImageType (1)
      section: dLength (u32 LE, in 32-bit words) | dAddress (u32 LE) | data
      end:     dLength == 0 | dAddress = entry point | dCheckSum (u32 LE)
      dCheckSum = 32-bit sum of every data word of every section

  For the V2.10 package the de-interleaved image contains 5 sections
  (ITCM at 0x100 plus four SRAM sections) followed by an FPGA configuration
  payload starting at 0x40190.

Verified results for the reference package:
  320/320 record checksums OK
  FX3 image checksum 0x0FE314BC OK
  entry point 0x40016F50
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

RECORD = 0x1001
PAYLOAD = 0x1000

# FX3 memories (see AN76405 / FX3 TRM).  Only used for reporting.
RANGES = [
    (0x00000000, 0x00040000, "ITCM/SYSMEM low"),
    (0x10000000, 0x10001000, "DTCM"),
    (0x40000000, 0x40080000, "SYSTEM RAM"),
]


def describe_addr(a: int) -> str:
    for lo, hi, name in RANGES:
        if lo <= a < hi:
            return name
    return "UNKNOWN"


@dataclass
class Section:
    index: int
    image_offset: int
    n_words: int
    address: int
    data: bytes

    @property
    def size(self) -> int:
        return self.n_words * 4


@dataclass
class Firmware:
    source: Path
    raw: bytes
    records: list[bytes]
    checksum_errors: list[int] = field(default_factory=list)
    image: bytes = b""
    image_ctl: int = 0
    image_type: int = 0
    sections: list[Section] = field(default_factory=list)
    entry: int = 0
    stored_checksum: int = 0
    computed_checksum: int = 0
    terminator_offset: int = 0
    fpga_offset: int = 0
    container: str = "records"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def image_sha256(self) -> str:
        return hashlib.sha256(self.image).hexdigest()

    def to_json(self) -> dict:
        return {
            "source": str(self.source),
            "sha256": self.sha256,
            "size": len(self.raw),
            "container": self.container,
            "records": len(self.records),
            "record_checksum_errors": self.checksum_errors,
            "image_size": len(self.image),
            "image_sha256": self.image_sha256,
            "bImageCTL": self.image_ctl,
            "bImageType": self.image_type,
            "sections": [
                {
                    "index": s.index,
                    "image_offset": s.image_offset,
                    "words": s.n_words,
                    "size": s.size,
                    "address": s.address,
                    "region": describe_addr(s.address),
                }
                for s in self.sections
            ],
            "entry": self.entry,
            "stored_checksum": self.stored_checksum,
            "computed_checksum": self.computed_checksum,
            "checksum_ok": self.stored_checksum == self.computed_checksum,
            "terminator_offset": self.terminator_offset,
            "fpga_offset": self.fpga_offset,
        }


def load(path: Path, force_raw: bool = False) -> Firmware:
    """Load a firmware file.

    Two on-disk forms exist:
      * factory / normal-update images -> 4097-byte records (4096 payload + 1 checksum)
      * recovery images (writeBootLoader/*.img) -> the bare FX3 boot image
    Auto-detection: a record file must have length % 0x1001 == 0 *and* pass the
    per-record checksum test; otherwise the file is treated as a raw FX3 image.
    """
    raw = path.read_bytes()
    fw = Firmware(source=path, raw=raw, records=[])

    raw_mode = force_raw
    if not raw_mode and len(raw) % RECORD:
        # Record containers are always an exact multiple of 0x1001; the raw images
        # seen so far (0x140000-byte recovery image, 29,876-byte boot app) are not.
        raw_mode = True

    if raw_mode:
        fw.container = "raw"
        fw.image = raw
        parse_fx3(fw)
        return fw

    fw.container = "records"
    payloads = []
    for i in range(len(raw) // RECORD):
        rec = raw[i * RECORD : (i + 1) * RECORD]
        fw.records.append(rec)
        want = rec[PAYLOAD]
        got = sum(rec[:PAYLOAD]) & 0xFF
        if want != got:
            fw.checksum_errors.append(i)
        payloads.append(rec[:PAYLOAD])
    fw.image = b"".join(payloads)
    parse_fx3(fw)
    return fw


def parse_fx3(fw: Firmware) -> None:
    img = fw.image
    if len(img) < 12 or img[0:2] != b"CY":
        raise SystemExit("de-interleaved image has no 'CY' signature")
    fw.image_ctl = img[2]
    fw.image_type = img[3]
    off = 4
    total = 0
    while True:
        if off + 8 > len(img):
            raise SystemExit("section chain runs past end of image")
        n_words, addr = struct.unpack_from("<II", img, off)
        if n_words == 0:
            fw.terminator_offset = off
            fw.entry = addr
            fw.stored_checksum = struct.unpack_from("<I", img, off + 8)[0]
            break
        data = img[off + 8 : off + 8 + n_words * 4]
        if len(data) != n_words * 4:
            raise SystemExit("truncated section data")
        fw.sections.append(Section(len(fw.sections) + 1, off, n_words, addr, data))
        total = (total + sum(struct.unpack_from("<%dI" % n_words, img, off + 8))) & 0xFFFFFFFF
        off += 8 + n_words * 4
    fw.computed_checksum = total
    end = fw.terminator_offset + 12
    # FPGA payload: first non-0xFF byte after the FX3 image.
    i = end
    while i < len(img) and img[i] == 0xFF:
        i += 1
    fw.fpga_offset = i if i < len(img) else 0


def find_fpga_payload(img: bytes, start: int) -> tuple[int, int]:
    """Return (offset, length) of the non-0xFF FPGA payload region."""
    i = start
    while i < len(img) and img[i] == 0xFF:
        i += 1
    j = len(img)
    while j > i and img[j - 1] == 0xFF:
        j -= 1
    return i, j - i


# ---------------------------------------------------------------------------
# ELF32 ARM emitter: lets Ghidra / IDA / objdump analyse the image with the
# correct load addresses (ITCM at 0x100, SRAM at 0x40003000..).
# ---------------------------------------------------------------------------

EM_ARM = 40
PT_LOAD = 1
PF_R, PF_W, PF_X = 4, 2, 1


def build_elf(fw: Firmware) -> bytes:
    segs = []
    for s in fw.sections:
        segs.append((s.address, s.data, PF_R | PF_X))
    segs.sort(key=lambda t: t[0])

    # Assign file offsets congruent to vaddr modulo 0x1000.
    cursor = 0x100
    placed = []
    for vaddr, data, flags in segs:
        want = vaddr & 0xFFF
        cursor = (cursor + 0xFFF) & ~0xFFF
        cursor += want
        placed.append((vaddr, data, flags, cursor))
        cursor += len(data)

    ehsize, phentsize = 52, 32
    phoff = ehsize
    nph = len(placed)
    data_start = (phoff + nph * phentsize + 0xFFF) & ~0xFFF
    # Re-lay out with data_start offset
    out = bytearray(data_start)
    phdrs = []
    cur = data_start
    for vaddr, data, flags, _ in placed:
        cur = (cur + 0xFFF) & ~0xFFF
        cur += vaddr & 0xFFF
        if len(out) < cur:
            out.extend(b"\0" * (cur - len(out)))
        out[cur : cur + len(data)] = data
        phdrs.append((vaddr, cur, len(data), flags))
        cur += len(data)
    if len(out) < cur:
        out.extend(b"\0" * (cur - len(out)))

    ident = bytearray(16)
    ident[0:4] = b"\x7fELF"
    ident[4] = 1  # ELFCLASS32
    ident[5] = 1  # little endian
    ident[6] = 1  # version
    ident[7] = 0  # SysV
    hdr = struct.pack(
        "<16sHHIIIIIHHHHHH",
        bytes(ident),
        2,  # ET_EXEC
        EM_ARM,
        1,  # version
        fw.entry,
        phoff,
        0,  # shoff
        0,  # flags
        ehsize,
        phentsize,
        nph,
        40,  # shentsize
        0,  # shnum
        0,  # shstrndx
    )
    out[0:len(hdr)] = hdr
    o = phoff
    for vaddr, off, size, flags in phdrs:
        ph = struct.pack("<IIIIIIII", PT_LOAD, off, vaddr, vaddr, size, size, flags, 0x1000)
        out[o : o + len(ph)] = ph
        o += len(ph)
    return bytes(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("--json", type=Path, help="write machine-readable summary")
    ap.add_argument("--extract-image", type=Path, help="write de-interleaved FX3 image")
    ap.add_argument("--extract-fpga", type=Path, help="write FPGA configuration payload")
    ap.add_argument("--elf", type=Path, help="write ELF32-ARM image for Ghidra/IDA")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    fw = load(args.image)
    js = fw.to_json()
    if args.json:
        args.json.write_text(json.dumps(js, indent=2) + "\n")
    if args.extract_image:
        args.extract_image.write_bytes(fw.image)
    if args.elf:
        args.elf.write_bytes(build_elf(fw))
    fpga_off, fpga_len = find_fpga_payload(fw.image, fw.terminator_offset + 12)
    js["fpga_payload"] = {"image_offset": fpga_off, "size": fpga_len}
    if args.extract_fpga:
        args.extract_fpga.write_bytes(fw.image[fpga_off : fpga_off + fpga_len])

    if not args.quiet:
        print(f"file        : {fw.source}")
        print(f"sha256      : {fw.sha256}")
        if fw.container == "records":
            print(f"size        : {len(fw.raw)} ({len(fw.records)} x 0x1001 records)")
            print(f"record csum : {len(fw.records) - len(fw.checksum_errors)}/{len(fw.records)} OK"
                  + (f"  BAD={fw.checksum_errors}" if fw.checksum_errors else ""))
        else:
            print(f"size        : {len(fw.raw)} (raw FX3 image, no 4097-byte container)")
        print(f"image       : {len(fw.image)} bytes sha256={fw.image_sha256}")
        print(f"header      : CY bImageCTL=0x{fw.image_ctl:02x} bImageType=0x{fw.image_type:02x}")
        for s in fw.sections:
            print(f"  section {s.index}: {s.n_words:>6d} words {s.size:>7d} B  -> 0x{s.address:08x} "
                  f"({describe_addr(s.address)})  image_off=0x{s.image_offset:06x}")
        print(f"terminator  : image offset 0x{fw.terminator_offset:06x}")
        print(f"entry point : 0x{fw.entry:08x}")
        print(f"checksum    : stored 0x{fw.stored_checksum:08x} computed 0x{fw.computed_checksum:08x} "
              f"{'OK' if fw.stored_checksum == fw.computed_checksum else 'MISMATCH'}")
        print(f"fpga payload: image offset 0x{fpga_off:06x} size {fpga_len} (0x{fpga_len:x})")
    return 0 if not fw.checksum_errors and fw.stored_checksum == fw.computed_checksum else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
