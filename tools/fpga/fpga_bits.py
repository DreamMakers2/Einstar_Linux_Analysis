#!/usr/bin/env python3
"""fpga_bits.py -- structural decoder for the SHINING 3D Einstar FPGA payload.

Target
------
``analysis/firmware/EinScan10_EN_fpga.bin`` (1,005,676 bytes, SHA-256
``cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4``),
carved from image offset 0x40190 of the de-interleaved FX3 IAP image.

Identification
--------------
The payload is a **Pango Micro "Logos" family configuration bitstream** in
Slave-Parallel (x32) wire format.  Evidence is exact and three-fold:

* bytes 0x00..0x07  ``00 00 00 AA 08 10 00 20`` = the vendor's mandatory
  "bus bit width auto detection sequence" (``000000AA 08100020``) documented
  in Pango UG020005 V2.3, section 3.1.2.1 (32-bit bus variant);
* bytes 0x08..0x2F  ten ``FFFFFFFF`` "padding words" (same section);
* bytes 0x30..0x33  ``01332D94`` = the documented synchronisation word
  (section 3.1.2.2), required before packet deframing starts.

Everything from 0x34 on is a sequence of big-endian 32-bit Pango CCS
packets (UG020005 section 3.3.1):

Type 1 (register access)::

    [31:29] 101      packet type
    [28:27] op       00 NOP, 01 Write, 10 Read, 11 reserved
    [26:22] reg      5-bit configuration-register address (Table 3-7)
    [21:0]  count    number of 32-bit data words that follow

Type 2 (configuration-memory data)::

    [31:29] 010      packet type
    [28:27] op       01 Write (10 Read)
    [26:0]  count    number of 32-bit words that follow

A Type 2 packet must be preceded by a Type 1 *empty* packet (count == 0)
that selects the target register (in practice CMEMIR = 5).  This is why the
raw stream looks like "opcode byte + 24-bit operand": the 24-bit operand is
really ``{reg[1:0], count[21:0]}`` and the low 5 bits of the opcode byte are
``{type[2:0], op[1:0]}``.

The register writes at 0x8C program IDR (register 1) with ``0x00511899``,
which UG020005 Table 3-8 lists as the IDCODE of the **PGL25G** (X0511899).

Usage
-----
    python3 tools/fpga/fpga_bits.py --info
    python3 tools/fpga/fpga_bits.py --opcodes
    python3 tools/fpga/fpga_bits.py --hist
    python3 tools/fpga/fpga_bits.py --dump-words [--start 0x34 --count 0x100]
    python3 tools/fpga/fpga_bits.py --info --opcodes --hist --json-out out.json
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import struct
import sys

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "analysis", "firmware", "EinScan10_EN_fpga.bin",
)

EXPECTED_SIZE = 1_005_676
EXPECTED_SHA256 = "cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4"

# --- fixed wire-format constants (Pango UG020005 V2.3) ---------------------
BUS_WIDTH_DETECT = 0x000000AA          # followed by 0x08100020 for a 32-bit bus
BUS_WIDTH_MAGIC_32 = 0x08100020
PADDING_WORD = 0xFFFFFFFF
SYNC_WORD = 0x01332D94

PKT_TYPE1 = 0b101
PKT_TYPE2 = 0b010
PKT_TYPE3 = 0b001                      # unused here

# Table 3-7: configuration registers
REGISTERS = {
    0: "CRCR", 1: "IDR", 2: "CMDR", 3: "CTRL0R", 4: "CTRL1R", 5: "CMEMIR",
    6: "MFWRITER", 7: "CMEMOR", 8: "IVR", 9: "STATUSR", 10: "CHAINR",
    11: "ADRR", 12: "SBPIR", 13: "reg13", 14: "reg14", 15: "IRSTCTRLR",
    16: "IRSTADRR", 17: "WATCHDOGR", 18: "HSTATUSR", 19: "FADR0R",
    20: "FADR1R", 21: "FADR2R", 22: "FADR3R", 23: "CMASKR", 24: "FALLBACKR",
    25: "OPTION0R", 26: "OPTION1R", 27: "RCRR",
}

# Table 3-9: CCS commands written to CMDR (register 2)
COMMANDS = {
    0x00: "NOP", 0x01: "RSTCRC", 0x02: "SWITCH", 0x03: "SWITCHCLK",
    0x04: "WCMEM", 0x05: "MFWRITE", 0x06: "RCMEM", 0x07: "SWAKEUP",
    0x08: "SWAKEDOWN", 0x09: "GUP", 0x0A: "GDOWN", 0x0B: "DESYNC",
    0x0C: "RWD", 0x0D: "RRBCRC", 0x0E: "RBCRC", 0x0F: "IRST",
}

# Table 3-8: device IDCODEs (28-bit meaningful value)
# One IDCODE can name more than one part: 0x00303899 is documented as both
# PGL22G and PGL22GS (docs/firmware/fpga_bitstream.md).  A plain dict silently
# dropped one of the names; the value is the documented label and ``aliases``
# keeps the alternative.
DEVICE_IDS = {
    0x00501899: "PGL12G",
    0x00303899: "PGL22G",
    0x005A1899: "PGL50G",
    0x00521899: "PGL50H",
    0x00522899: "PGL100H",
}

#: alternative part names for the same IDCODE
DEVICE_ALIASES = {
    0x00303899: ("PGL22GS",),
}

# Table 3-3: uncompressed bitstream sizes, KiB (as printed in the vendor doc)
BITSTREAM_KIB = {
    "PGL12G": 518, "PGL22G": 745, "PGL22GS": 745, "PGL25G": 982,
    "PGL50G": 2052, "PGL50H": 2052, "PGL100H": 4059,
}

REGION_NAMES = {
    "header": (0x00, 0x34),
    "register_init": (0x34, 0x10C),
}


class Packet:
    __slots__ = ("offset", "word", "ptype", "op", "reg", "count", "data_offset")

    def __init__(self, offset, word, ptype, op, reg, count):
        self.offset = offset
        self.word = word
        self.ptype = ptype
        self.op = op
        self.reg = reg
        self.count = count
        self.data_offset = offset + 4

    @property
    def total_words(self):
        return 1 + (self.count if self.ptype in (PKT_TYPE1, PKT_TYPE2) else 0)

    def to_dict(self):
        d = {
            "offset": self.offset,
            "header": f"0x{self.word:08x}",
            "type": self.ptype,
            "op": self.op,
            "count": self.count,
            "data_offset": self.data_offset,
            "data_bytes": self.count * 4,
        }
        if self.ptype == PKT_TYPE1:
            d["register"] = self.reg
            d["register_name"] = REGISTERS.get(self.reg, f"reg{self.reg}")
        return d


def load(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) % 4:
        raise SystemExit(f"{path}: size {len(data)} is not a multiple of 4")
    words = list(struct.unpack(">%dI" % (len(data) // 4), data))
    return data, words


def parse(words):
    """Parse the payload as header + Pango CCS packet stream.

    Returns a dict with header facts and the packet list.  Raises ValueError
    if the stream does not decode cleanly to the last word.
    """
    if len(words) < 13:
        raise ValueError("file too small")
    header = {
        "bus_width_detect": words[0],
        "bus_width_magic": words[1],
        "bus_width_bits": None,
        "padding_words": 0,
        "sync_offset": None,
        "sync_word": None,
    }
    if words[0] != BUS_WIDTH_DETECT:
        raise ValueError(f"bad bus-width detect word: 0x{words[0]:08x}")
    if words[1] == BUS_WIDTH_MAGIC_32:
        header["bus_width_bits"] = 32
    elif words[1] == 0x08100010:
        header["bus_width_bits"] = 16
    elif words[1] == 0x08100008:
        header["bus_width_bits"] = 8
    i = 2
    while i < len(words) and words[i] == PADDING_WORD:
        header["padding_words"] += 1
        i += 1
    if i >= len(words) or words[i] != SYNC_WORD:
        raise ValueError(f"sync word not found, got 0x{words[i]:08x} at 0x{i*4:x}")
    header["sync_offset"] = i * 4
    header["sync_word"] = words[i]
    i += 1

    packets = []
    while i < len(words):
        w = words[i]
        ptype = (w >> 29) & 0x7
        if ptype == PKT_TYPE1:
            op = (w >> 27) & 0x3
            reg = (w >> 22) & 0x1F
            count = w & 0x3FFFFF
        elif ptype == PKT_TYPE2:
            op = (w >> 27) & 0x3
            reg = None
            count = w & 0x1FFFFFF
        else:
            raise ValueError(
                f"undecodable word 0x{w:08x} at 0x{i*4:x} (type {ptype})"
            )
        if i + 1 + count > len(words):
            raise ValueError(
                f"packet at 0x{i*4:x} runs past EOF (count={count})"
            )
        packets.append(Packet(i * 4, w, ptype, op, reg, count))
        i += 1 + count
    return header, packets


def _entropy(buf):
    if not buf:
        return 0.0
    counts = collections.Counter(buf)
    n = len(buf)
    return -sum((v / n) * math.log2(v / n) for v in counts.values())


def _runs(words, value):
    """Return (count, longest, longest_offset) of runs of `value`."""
    count = longest = 0
    longest_off = None
    i = 0
    n = len(words)
    while i < n:
        if words[i] == value:
            j = i
            while j < n and words[j] == value:
                j += 1
            count += 1
            if j - i > longest:
                longest = j - i
                longest_off = i * 4
            i = j
        else:
            i += 1
    return count, longest, longest_off


def analyse(data, words, path=None):
    header, packets = parse(words)

    t1 = [p for p in packets if p.ptype == PKT_TYPE1]
    t2 = [p for p in packets if p.ptype == PKT_TYPE2]

    registers = collections.Counter(
        REGISTERS.get(p.reg, f"reg{p.reg}") for p in t1 if p.count > 0)
    empty_regs = collections.Counter(
        REGISTERS.get(p.reg, f"reg{p.reg}") for p in t1 if p.count == 0)
    cmds = []
    idcode = None
    addr_writes = []
    crc_values = []
    for p in t1:
        if p.count >= 1 and p.reg is not None:
            val = words[p.data_offset // 4]
            if p.reg == 2:
                cmds.append({"offset": p.offset, "value": val,
                             "name": COMMANDS.get(val, f"0x{val:x}")})
            elif p.reg == 1:
                idcode = val
            elif p.reg == 11:
                addr_writes.append({"offset": p.offset, "value": val})
            elif p.reg == 0:
                crc_values.append({"offset": p.offset, "value": val})

    config_words = sum(p.count for p in t2)
    t2_list = []
    for p in t2:
        d = p.to_dict()
        # cheap content fingerprint of the data block
        chunk = data[p.data_offset:p.data_offset + p.count * 4]
        d["nonzero_bytes"] = sum(1 for b in chunk if b)
        d["entropy"] = round(max(0.0, _entropy(chunk)), 3)
        t2_list.append(d)

    a0_runs, a0_longest, a0_off = _runs(words, 0xA0000000)
    a0_words = sum(1 for w in words if w == 0xA0000000)
    zero_words = sum(1 for w in words if w == 0)

    digest = hashlib.sha256(data).hexdigest()
    info = {
        "path": path,
        "size": len(data),
        "size_kib": round(len(data) / 1024, 2),
        "sha256": digest,
        "sha256_matches_reference": digest == EXPECTED_SHA256,
        "words": len(words),
        "header": header,
        "packet_count": len(packets),
        "type1_packets": len(t1),
        "type2_packets": len(t2),
        "type2": t2_list,
        "config_words": config_words,
        "config_bytes": config_words * 4,
        "config_bits": config_words * 32,
        "register_writes": dict(registers),
        "empty_packets": sum(1 for p in t1 if p.count == 0),
        "empty_packet_registers": dict(empty_regs),
        "commands": cmds,
        "idcode": f"0x{idcode:08x}" if idcode is not None else None,
        "device": DEVICE_IDS.get(idcode) if idcode is not None else None,
        "device_aliases": list(DEVICE_ALIASES.get(idcode, ()))
        if idcode is not None else [],
        "device_bitstream_kib": BITSTREAM_KIB.get(DEVICE_IDS.get(idcode))
        if idcode in DEVICE_IDS else None,
        "frame_address_writes": addr_writes,
        "crc_writes": crc_values,
        "a0000000_words": a0_words,
        "a0000000_runs": a0_runs,
        "a0000000_longest_run": a0_longest,
        "a0000000_longest_run_offset": a0_off,
        "zero_words": zero_words,
        "zero_frac": round(zero_words / len(words), 4),
        "entropy": round(_entropy(data), 3),
        "first_packet_offset": packets[0].offset if packets else None,
        "last_packet_offset": packets[-1].offset if packets else None,
        "end_offset": (packets[-1].offset + packets[-1].total_words * 4)
        if packets else None,
    }
    if info["end_offset"] != len(data):
        info["trailing_bytes"] = len(data) - info["end_offset"]
    return info, packets


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def show_info(info):
    print("=" * 74)
    print("SHINING 3D Einstar FPGA payload -- Pango Logos configuration bitstream")
    print("=" * 74)
    print(f"file            : {info['path']}")
    print(f"size            : {info['size']:,} bytes ({info['size_kib']} KiB)")
    print(f"sha256          : {info['sha256']}")
    print(f"reference match : {info['sha256_matches_reference']}")
    print(f"32-bit words    : {info['words']:,}")
    h = info["header"]
    print("\n-- wire header ------------------------------------------------")
    print(f"0x0000 bus-width detect : 0x{h['bus_width_detect']:08x}")
    print(f"0x0004 width magic      : 0x{h['bus_width_magic']:08x} "
          f"-> {h['bus_width_bits']}-bit bus")
    print(f"0x0008 padding          : {h['padding_words']} x 0xFFFFFFFF "
          f"({h['padding_words']*4} bytes)")
    print(f"0x{h['sync_offset']:04x} sync word        : 0x{h['sync_word']:08x}")
    print("\n-- packet stream ----------------------------------------------")
    print(f"packets          : {info['packet_count']} "
          f"(type1={info['type1_packets']}, type2={info['type2_packets']})")
    print(f"packets span     : 0x{info['first_packet_offset']:x} .. "
          f"0x{info['end_offset']:x} (file end 0x{info['size']:x})")
    print(f"config data      : {info['config_words']:,} words = "
          f"{info['config_bytes']:,} bytes = {info['config_bits']:,} bits")
    for i, p in enumerate(info["type2"]):
        print(f"  T2 #{i}: 0x{p['offset']:06x} count={p['count']:,} words "
              f"({p['data_bytes']:,} B) data@0x{p['data_offset']:06x} "
              f"entropy={p['entropy']} nonzero={p['nonzero_bytes']:,}")
    print("\n-- identity ---------------------------------------------------")
    print(f"IDCODE written   : {info['idcode']}")
    print(f"device           : {info['device']}")
    print(f"vendor doc size  : {info['device_bitstream_kib']} KB "
          f"(uncompressed, UG020005 Table 3-3)")
    print(f"payload / doc    : {info['size']/1024:.1f} KiB vs "
          f"{info['device_bitstream_kib']} KB")
    print("\n-- register writes --------------------------------------------")
    for k, v in sorted(info["register_writes"].items()):
        print(f"  {k:10s} x{v}")
    print("\n-- command sequence (CMDR writes) ------------------------------")
    for c in info["commands"]:
        print(f"  0x{c['offset']:06x}  {c['name']}")
    print("\n-- trailer registers ------------------------------------------")
    for a in info["frame_address_writes"]:
        print(f"  0x{a['offset']:06x}  ADRR  = 0x{a['value']:08x}")
    for c in info["crc_writes"]:
        print(f"  0x{c['offset']:06x}  CRCR  = 0x{c['value']:08x}")
    print("\n-- padding ----------------------------------------------------")
    print(f"0xA0000000 no-op packets: {info['a0000000_words']:,} words "
          f"in {info['a0000000_runs']} runs; longest run {info['a0000000_longest_run']} words "
          f"at 0x{info['a0000000_longest_run_offset'] or 0:x}")
    print(f"0x00000000 words        : {info['zero_words']:,} "
          f"({100*info['zero_frac']:.1f}%)")
    print(f"whole-file entropy      : {info['entropy']} bits/byte")


def show_opcodes(words, packets):
    n = len(words)
    print("A. raw top-byte histogram of all %d words (naive 'opcode' view)" % n)
    print("   -- only header words matter; data words dominate this histogram")
    print("-" * 74)
    by_top = collections.Counter(w >> 24 for w in words)
    for top, cnt in sorted(by_top.items(), key=lambda kv: -kv[1])[:12]:
        print(f"   0x{top:02x} {cnt:8d} {100*cnt/n:6.2f}%")
    print("   (top byte 0x00 = 0x00000000 words; 0xA0 = the 0xA0000000 no-op/pad packet)")

    print("\nB. decoded Type-1 packet-header bytes (the real opcode table)")
    print("-" * 74)
    print("byte  count  type op  registers addressed (occurrences)")
    agg = collections.defaultdict(lambda: collections.Counter())
    for p in packets:
        if p.ptype == PKT_TYPE1:
            key = (p.word >> 24) & 0xFF
            reg = REGISTERS.get(p.reg, f"reg{p.reg}")
            agg[key][(reg, p.count)] += 1
    for key in sorted(agg):
        items = agg[key]
        regs = ", ".join(f"{r}(cnt={c})x{v}" for (r, c), v in
                         sorted(items.items(), key=lambda kv: -kv[1]))
        ptype = (key >> 5) & 7
        op = (key >> 3) & 3
        total = sum(items.values())
        tname = "T1" if ptype == PKT_TYPE1 else ("T2" if ptype == PKT_TYPE2 else f"?{ptype}")
        print(f"0x{key:02x} {total:6d}   {tname}  {op}   {regs}")

    print("\nC. 24-bit operand distribution for Type-1 header bytes")
    print("-" * 74)
    print("   operand = {reg[1:0], count[21:0]}; count is the number of 32-bit")
    print("   data words that follow the header.  The register's low two bits")
    print("   live in operand bits 23:22, which is why 0x800001/0x400001/")
    print("   0xC00001/0x000001 select registers with the same low bits.")
    operands = collections.defaultdict(collections.Counter)
    for p in packets:
        if p.ptype == PKT_TYPE1:
            operands[(p.word >> 24) & 0xFF][p.word & 0xFFFFFF] += 1
    for key in sorted(operands, key=lambda k: -sum(operands[k].values()))[:10]:
        ops = ", ".join(f"0x{o:06x}x{v}" for o, v in
                        operands[key].most_common(4))
        print(f"   0x{key:02x}: {ops}")

    print("\nD. Type-2 data packets")
    print("-" * 74)
    for p in packets:
        if p.ptype == PKT_TYPE2:
            print(f"   0x{p.offset:06x}  0x{p.word:08x}  write {p.count:,} words "
                  f"({p.count*4:,} bytes)")


def show_hist(data, words):
    print("byte histogram (data region 0x22000..end vs whole file)")
    print("-" * 60)
    for label, buf in (("whole", data), ("0x22000..", data[0x22000:])):
        counts = collections.Counter(buf)
        print(f"{label:9s} len={len(buf):,} entropy={_entropy(buf):.3f}")
        top = ", ".join(f"{k:02x}:{100*v/len(buf):.1f}%" for k, v in counts.most_common(8))
        print(f"          top bytes: {top}")
        print(f"          zero bytes: {100*counts[0]/len(buf):.1f}%")
    counts = collections.Counter(words)
    print(f"\n32-bit word multiplicity: {len(counts):,} distinct of {len(words):,}")
    print("top words: " + ", ".join(
        f"0x{k:08x}x{v}" for k, v in counts.most_common(6)))

    a0c, a0l, a0o = _runs(words, 0xA0000000)
    print(f"\n0xA0000000 runs: {a0c:,}, longest {a0l} words at 0x{a0o or 0:x}")
    print(f"0xA0000000 words total: {counts[0xA0000000]:,} "
          f"({100*counts[0xA0000000]/len(words):.1f}%)")
    ec, el, eo = _runs(words, 0)
    print(f"0x00000000 runs: {ec:,}, longest {el} words at 0x{eo or 0:x}")
    print(f"0xFFFFFFFF words: {counts[0xFFFFFFFF]:,}")

    print("\nentropy by 4 KiB block (non-empty blocks only)")
    print("  offset    entropy  zero%   A0-words")
    print("  [0x001000..0x021fff] is 138,992 bytes of 0x00 (empty frames)")
    for off in range(0, len(data), 4096):
        blk = data[off:off + 4096]
        z = 100 * blk.count(0) / len(blk)
        a0 = sum(1 for i in range(0, len(blk) - 3, 4)
                 if blk[i:i+4] == b"\xa0\x00\x00\x00")
        if z < 99.9:
            print(f"  0x{off:07x}  {_entropy(blk):5.2f}  {z:5.1f}  {a0:5d}")

    # periodicity check via FFT autocorrelation of the "this byte is non-zero"
    # indicator over the configuration-data region
    print("\nperiodicity of the data region (autocorrelation of non-zero bytes)")
    try:
        import numpy as np
        seg = np.frombuffer(data[0x22000:], dtype=np.uint8)
        x = (seg != 0).astype(np.float32)
        x -= x.mean()
        size = 1 << int(np.ceil(np.log2(2 * len(x))))
        spec = np.fft.rfft(x, size)
        ac = np.fft.irfft(spec * np.conj(spec), size)[:8192]
        ac /= ac[0]
        order = np.argsort(ac[1:])[::-1][:6] + 1
        for lag in order:
            print(f"   lag {int(lag):5d} bytes: autocorrelation {float(ac[lag]):.3f}")
    except Exception as exc:  # numpy optional
        print(f"   (numpy unavailable, skipped: {exc})")


def show_dump(data, words, start, count):
    end = min(len(words), start // 4 + count)
    first = start // 4
    print(f"structural dump 0x{first*4:06x}..0x{end*4:06x}")
    print("offset    word      type op reg  count  note")
    print("-" * 78)
    i = first
    while i < end:
        w = words[i]
        ptype = (w >> 29) & 7
        if ptype == PKT_TYPE1:
            op = (w >> 27) & 3
            reg = (w >> 22) & 0x1F
            cnt = w & 0x3FFFFF
            note = REGISTERS.get(reg, f"reg{reg}")
            if cnt:
                val = words[i+1] if i + 1 < len(words) else 0
                if reg == 2:
                    note += " = " + COMMANDS.get(val, f"0x{val:08x}")
                else:
                    note += f" = 0x{val:08x}"
            print(f"0x{i*4:06x}  {w:08x}  T1  {op:2d} {reg:3d} {cnt:6d}  {note}")
            i += 1 + cnt
        elif ptype == PKT_TYPE2:
            op = (w >> 27) & 3
            cnt = w & 0x1FFFFFF
            print(f"0x{i*4:06x}  {w:08x}  T2  {op:2d}   -  {cnt:6d}  "
                  f"config data, {cnt*4} bytes")
            i += 1 + cnt
        else:
            print(f"0x{i*4:06x}  {w:08x}  ??  {ptype}")
            i += 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Pango Logos bitstream structural decoder (Einstar FPGA payload)")
    ap.add_argument("file", nargs="?", default=DEFAULT_PATH,
                    help=f"payload (default: {DEFAULT_PATH})")
    ap.add_argument("--info", action="store_true", help="identity/summary report")
    ap.add_argument("--opcodes", action="store_true",
                    help="opcode-byte table and operand distribution")
    ap.add_argument("--hist", action="store_true",
                    help="byte/word histogram, entropy, runs, periodicity")
    ap.add_argument("--dump-words", action="store_true",
                    help="hexdump-style structural view")
    ap.add_argument("--start", default="0x0", help="dump start offset (default 0x0)")
    ap.add_argument("--count", default="0x200",
                    help="dump length in bytes (default 0x200)")
    ap.add_argument("--json-out", metavar="PATH", help="write JSON report")
    args = ap.parse_args(argv)

    if not os.path.exists(args.file):
        sys.exit(f"no such file: {args.file}")
    data, words = load(args.file)

    want_any = args.info or args.opcodes or args.hist or args.dump_words
    if not want_any:
        args.info = True

    info, packets = analyse(data, words, args.file)

    if args.info:
        show_info(info)
    if args.opcodes:
        if args.info:
            print()
        show_opcodes(words, packets)
    if args.hist:
        if args.info or args.opcodes:
            print()
        show_hist(data, words)
    if args.dump_words:
        if args.info or args.opcodes or args.hist:
            print()
        show_dump(data, words, int(args.start, 0), int(args.count, 0))

    if args.json_out:
        report = dict(info)
        report["packets"] = [p.to_dict() for p in packets]
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nJSON report written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
