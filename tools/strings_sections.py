#!/usr/bin/env python3
"""Section-aware string extraction: only .rdata/.data/.rsrc/.qtmetad payloads.

Produces per-module:
  <base>.sections.txt  - ASCII >=5 and UTF-16LE >=4 from data sections
  <base>.cjk.txt       - properly decoded CJK (Chinese/Japanese) UTF-16LE runs
Raw full-file dumps are produced separately by tools/extract_strings.sh.
"""
from __future__ import annotations

import os
import re
import sys

import pefile

SRC = "<repo>/sources/windows/EXStar"
OUT = "<repo>/analysis/strings"
MODULES = [
    "Sn3DDeviceEinStar.dll",
    "Sn3DEinStarController.dll",
    "FirmwareupdateUI.exe",
    "SiUSBXp.dll",
    "Sn3DCalibDevFile.dll",
    "Sn3DCalibrateLoader.dll",
    "Sn3DCameraCalibHelper.dll",
    "softwareUpgrade.exe",
]
DATA_SECS = (".rdata", ".data", ".rsrc", ".qtmetad", ".CRT", ".tls")
ASCII_RE = re.compile(rb"[\x20-\x7e]{5,}")
CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
ASCII_PRINT = set(range(0x20, 0x7F))


def ascii_runs(blob: bytes):
    return [m.group().decode("ascii", "replace") for m in ASCII_RE.finditer(blob)]


def utf16_runs(blob: bytes):
    """Decode 16-bit LE runs, rejecting ASCII bytes misread as UTF-16.

    Mangled C++ names are plain ASCII; pairing their bytes yields bogus CJK.
    A real UTF-16 string's underlying bytes are not printable ASCII.
    """
    out = []
    i, n = 0, len(blob) - 1
    start = 0
    cur = []
    while i < n:
        ch = blob[i] | (blob[i + 1] << 8)
        ok = (0x20 <= ch <= 0x7E) or (0x3000 <= ch <= 0x9FFF) or (0xFF00 <= ch <= 0xFFEF) or ch in (0x0A, 0x0D, 0x09)
        if ok:
            if not cur:
                start = i
            cur.append(chr(ch))
            i += 2
        else:
            if len(cur) >= 4:
                raw = blob[start:i]
                printable = sum(1 for b in raw if b in ASCII_PRINT)
                if printable < 0.85 * len(raw):  # not just an ASCII string seen as UTF-16
                    out.append("".join(cur))
            cur = []
            i += 2
    if len(cur) >= 4:
        raw = blob[start:i]
        printable = sum(1 for b in raw if b in ASCII_PRINT)
        if printable < 0.85 * len(raw):
            out.append("".join(cur))
    return out


def dedupe(seq):
    seen = set()
    out = []
    for s in seq:
        s = s.strip()
        if len(s) < 4 or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    for mod in MODULES:
        path = os.path.join(SRC, mod)
        base = os.path.splitext(mod)[0]
        pe = pefile.PE(path, fast_load=True)
        ascii_all, utf16_all = [], []
        for sec in pe.sections:
            nm = sec.Name.rstrip(b"\x00").decode("latin-1", "replace")
            if not any(nm.startswith(d) for d in DATA_SECS):
                continue
            blob = sec.get_data()
            ascii_all.extend(ascii_runs(blob))
            utf16_all.extend(utf16_runs(blob))
        pe.close()
        ascii_u = dedupe(ascii_all)
        utf16_u = dedupe(utf16_all)
        with open(os.path.join(OUT, f"{base}.sections.txt"), "w", encoding="utf-8") as f:
            f.write(f"# module {mod}\n# ASCII strings >=5 from data sections ({len(ascii_u)})\n")
            f.write("\n".join(ascii_u))
            f.write(f"\n\n# UTF-16LE strings >=4 ({len(utf16_u)})\n")
            f.write("\n".join(utf16_u))
            f.write("\n")
        cjk = sorted({s for s in utf16_u if CJK_RE.search(s)}, key=len, reverse=True)
        with open(os.path.join(OUT, f"{base}.cjk.txt"), "w", encoding="utf-8") as f:
            f.write(f"# module {mod}\n# strings containing CJK characters ({len(cjk)})\n")
            f.write("\n".join(cjk))
            f.write("\n")
        print(f"{mod}: ascii={len(ascii_u)} utf16={len(utf16_u)} cjk={len(cjk)}")


if __name__ == "__main__":
    sys.exit(main())
