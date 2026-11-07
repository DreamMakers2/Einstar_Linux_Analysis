#!/usr/bin/env python3
"""Compare two Einstar firmware images (any mix of .img container or raw image).

Produces a deterministic report:
  * container-level identity (hashes, sizes, record checksums)
  * section-by-section matching and byte-diff ranges
  * FPGA payload comparison
  * attribution of changed ranges to functions of a reference image, using the
    Ghidra corpus headers (`// range: 0xSTART-0xEND`) as the symbol map.

Usage:
  python3 tools/firmware/compare_fw.py OLD NEW --json out.json --markdown out.md \
      [--functions analysis/ghidra/FX3_FW/clean]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fx3img  # noqa: E402

RANGE_RE = re.compile(r"^// range: (0x[0-9a-fA-F]+)-(0x[0-9a-fA-F]+) inclusive")


def load_func_map(corpus: Path) -> list[tuple[int, int, str]]:
    """Parse `// range:` headers of a decompiled corpus into (start, end, name)."""
    funcs: list[tuple[int, int, str]] = []
    if not corpus.exists():
        return funcs
    for f in corpus.glob("*.c"):
        try:
            head = f.read_text(errors="replace")[:1200].splitlines()
        except OSError:
            continue
        name = f.stem
        lo = hi = None
        for line in head:
            if line.startswith("// name:"):
                name = line.split(":", 1)[1].strip()
            m = RANGE_RE.match(line)
            if m:
                a, b = int(m.group(1), 16), int(m.group(2), 16)
                lo = a if lo is None else min(lo, a)
                hi = b if hi is None else max(hi, b)
        if lo is not None:
            funcs.append((lo, hi, name))
    funcs.sort()
    return funcs


def attribute(addr: int, funcs: list[tuple[int, int, str]]) -> str:
    lo, hi = 0, len(funcs) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b, n = funcs[mid]
        if addr < a:
            hi = mid - 1
        elif addr > b:
            lo = mid + 1
        else:
            return n
    return "?"


def diff_ranges(old: bytes, new: bytes, base: int, limit: int = 200_000) -> list[dict]:
    """Return merged differing ranges (address space = base + offset)."""
    n = min(len(old), len(new))
    out: list[dict] = []
    i = 0
    while i < n:
        if old[i] != new[i]:
            j = i + 1
            while j < n and old[j] != new[j] and out and False:
                j += 1
            # merge runs separated by <= 8 identical bytes
            k = j
            while k < n and (old[k] != new[k] or _close(old, new, k, n)):
                k += 1
            out.append({"address": base + i, "size": k - i})
            i = k
            if len(out) > limit:
                break
        else:
            i += 1
    return out


def _close(old: bytes, new: bytes, k: int, n: int, gap: int = 8) -> bool:
    """True if another difference occurs within `gap` bytes (to merge ranges)."""
    for t in range(k, min(k + gap, n)):
        if old[t] != new[t]:
            return True
    return False


def summarize(fw: fx3img.Firmware) -> dict:
    return {
        "path": str(fw.source),
        "sha256": fw.sha256,
        "container": fw.container,
        "image_sha256": fw.image_sha256,
        "image_size": len(fw.image),
        "entry": fw.entry,
        "checksum": fw.stored_checksum,
        "checksum_ok": fw.stored_checksum == fw.computed_checksum,
        "sections": [
            {"index": s.index, "words": s.n_words, "size": s.size, "address": s.address}
            for s in fw.sections
        ],
        "fpga_offset": None,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--markdown", type=Path)
    ap.add_argument("--functions", type=Path, help="decompiled corpus of the NEW image for attribution")
    args = ap.parse_args(argv)

    a = fx3img.load(args.old)
    b = fx3img.load(args.new)
    funcs = load_func_map(args.functions) if args.functions else []

    sa, sb = summarize(a), summarize(b)
    # match sections by load address
    by_addr_b = {s.address: s for s in b.sections}
    section_diffs = []
    total_changed = 0
    changed_funcs: Counter[str] = Counter()

    for sec in a.sections:
        other = by_addr_b.get(sec.address)
        if other is None:
            section_diffs.append({"address": sec.address, "status": "removed"})
            continue
        if other.size != sec.size:
            # Byte offsets no longer align after a resize, so a raw diff would be
            # meaningless; report the resize and stop (use a symbol-level diff instead).
            section_diffs.append(
                {
                    "address": sec.address,
                    "status": "resized",
                    "old_size": sec.size,
                    "new_size": other.size,
                    "delta": other.size - sec.size,
                }
            )
            continue
        n = min(len(sec.data), len(other.data))
        ranges = diff_ranges(sec.data[:n], other.data[:n], sec.address)
        total_changed += sum(r["size"] for r in ranges)
        for r in ranges:
            if funcs:
                changed_funcs[attribute(r["address"], funcs)] += r["size"]
        section_diffs.append(
            {
                "address": sec.address,
                "status": "same-size" if other.size == sec.size else "resized",
                "size": sec.size,
                "changed_bytes": sum(r["size"] for r in ranges),
                "ranges": ranges[:50],
                "range_count": len(ranges),
            }
        )

    # Robust functional signal: printable-string set difference over the FX3 image
    # region (layout-independent, unlike raw byte diffs after a recompile).
    def strset(img: bytes, end: int) -> set[str]:
        return {m.group(0).decode("latin-1") for m in re.finditer(rb"[ -~]{8,}", img[:end])}

    stra = strset(a.image, a.terminator_offset + 12)
    strb = strset(b.image, b.terminator_offset + 12)
    strings = {
        "old_count": len(stra),
        "new_count": len(strb),
        "removed": sorted(stra - strb)[:200],
        "added": sorted(strb - stra)[:200],
        "removed_count": len(stra - strb),
        "added_count": len(strb - stra),
    }

    fp_a, fl_a = fx3img.find_fpga_payload(a.image, a.terminator_offset + 12)
    fp_b, fl_b = fx3img.find_fpga_payload(b.image, b.terminator_offset + 12)
    fpga = {
        "old": {"offset": fp_a, "size": fl_a, "sha256": hashlib.sha256(a.image[fp_a : fp_a + fl_a]).hexdigest()},
        "new": {"offset": fp_b, "size": fl_b, "sha256": hashlib.sha256(b.image[fp_b : fp_b + fl_b]).hexdigest()},
    }
    fpga["identical"] = fpga["old"]["sha256"] == fpga["new"]["sha256"]

    report = {
        "old": sa,
        "new": sb,
        "sections": section_diffs,
        "total_changed_bytes": total_changed,
        "fpga": fpga,
        "changed_functions": [
            {"name": n, "changed_bytes": c} for n, c in changed_funcs.most_common(40)
        ],
        "strings": strings,
    }

    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")

    if not fpga["identical"]:
        fpga_diff = sum(1 for x, y in zip(
            a.image[fp_a:fp_a + min(fl_a, fl_b)], b.image[fp_b:fp_b + min(fl_a, fl_b)]
        ) if x != y)
    else:
        fpga_diff = 0

    lines = []
    lines.append(f"# Firmware comparison: {args.old.name} → {args.new.name}\n")
    lines.append("| | old | new |")
    lines.append("|---|---|---|")
    lines.append(f"| file SHA-256 | `{sa['sha256'][:16]}…` | `{sb['sha256'][:16]}…` |")
    lines.append(f"| container | {sa['container']} | {sb['container']} |")
    lines.append(f"| image size | {sa['image_size']} | {sb['image_size']} |")
    lines.append(f"| entry point | `0x{sa['entry']:08X}` | `0x{sb['entry']:08X}` |")
    lines.append(f"| image checksum | `0x{sa['checksum']:08X}` | `0x{sb['checksum']:08X}` |")
    lines.append(f"\n**Changed FX3 code/data bytes:** {total_changed}\n")
    lines.append("## Sections\n")
    lines.append("| load address | status | size | changed bytes | diff ranges |")
    lines.append("|---|---|---|---|---|")
    for s in section_diffs:
        if s["status"] == "removed":
            lines.append(f"| `0x{s['address']:08X}` | removed | – | – | – |")
            continue
        if s["status"] == "resized":
            lines.append(
                f"| `0x{s['address']:08X}` | resized | {s['old_size']} → {s['new_size']} "
                f"({s['delta']:+d}) | n/a (offsets shift) | n/a |"
            )
            continue
        lines.append(
            f"| `0x{s['address']:08X}` | {s['status']} | {s.get('size','')} | "
            f"{s.get('changed_bytes','')} | {s.get('range_count','')} |"
        )
    lines.append("\n## FPGA payload\n")
    lines.append(f"* old: offset `0x{fpga['old']['offset']:X}`, {fpga['old']['size']} B, `{fpga['old']['sha256'][:16]}…`")
    lines.append(f"* new: offset `0x{fpga['new']['offset']:X}`, {fpga['new']['size']} B, `{fpga['new']['sha256'][:16]}…`")
    lines.append(f"* identical: **{fpga['identical']}**" + (f", {fpga_diff} differing bytes" if not fpga["identical"] else ""))
    lines.append("\n## Added / removed strings (layout-independent)\n")
    lines.append(f"* old: {strings['old_count']} strings, new: {strings['new_count']} strings")
    lines.append(f"* **added {strings['added_count']}**, **removed {strings['removed_count']}**")
    if strings["added"]:
        lines.append("\n### Added (first 40)\n")
        for t in strings["added"][:40]:
            lines.append(f"* `{t}`")
    if strings["removed"]:
        lines.append("\n### Removed (first 40)\n")
        for t in strings["removed"][:40]:
            lines.append(f"* `{t}`")
    lines.append("")
    if report["changed_functions"]:
        lines.append("\n## Most-changed functions (attributed to the NEW image)\n")
        lines.append("| function | changed bytes |")
        lines.append("|---|---|")
        for f in report["changed_functions"][:25]:
            lines.append(f"| `{f['name']}` | {f['changed_bytes']} |")
        lines.append("\n_Attribution uses the decompiled corpus of the new image; ranges outside any "
                     "known function are reported as `?`._")
    md = "\n".join(lines) + "\n"
    if args.markdown:
        args.markdown.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
