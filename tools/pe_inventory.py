#!/usr/bin/env python3
"""Static PE inventory for the SHINING 3D EXStar installation.

Read-only: walks the (read-only) Windows mount, never writes there.
Outputs analysis/gnu/pe_inventory.json
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import traceback
from datetime import datetime, timezone

import pefile

ROOT = "<EXStar-install>/Shining3d/EXStar"
OUT = "<repo>/analysis/gnu/pe_inventory.json"
MAX_SIZE = 200 * 1024 * 1024  # skip nothing except >200MB files

MACHINE = {
    0x0: "UNKNOWN",
    0x14C: "I386",
    0x8664: "AMD64",
    0x1C0: "ARM",
    0x1C4: "ARMNT",
    0xAA64: "ARM64",
    0x200: "IA64",
}
SUBSYSTEM = {
    0: "UNKNOWN",
    1: "NATIVE",
    2: "WINDOWS_GUI",
    3: "WINDOWS_CUI",
    5: "OS2_CUI",
    7: "POSIX_CUI",
    9: "WINDOWS_CE_GUI",
    10: "EFI_APPLICATION",
    11: "EFI_BOOT_SERVICE_DRIVER",
    12: "EFI_RUNTIME_DRIVER",
    13: "EFI_ROM",
    14: "XBOX",
    16: "WINDOWS_BOOT_APPLICATION",
}
DIR_IMPORT = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]
DIR_EXPORT = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]
DIR_DEBUG = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"]
DIR_DELAY = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]
DIR_COM = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_COM_DESCRIPTOR"]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_rich(data: bytes):
    """Decode the MSVC Rich header: returns (present, key, entries[(build, prodid, count)])."""
    idx = data.find(b"Rich", 0, 0x400)
    while idx != -1:
        if idx + 8 <= len(data):
            key = struct.unpack_from("<I", data, idx + 4)[0]
            dans = struct.pack("<I", 0x536E6144 ^ key)  # 'DanS' xor key
            start = data.rfind(dans, 0, idx)
            if start != -1:
                entries = []
                p = start + 16
                while p + 8 <= idx:
                    compid, count = struct.unpack_from("<II", data, p)
                    compid ^= key
                    count ^= key
                    # comp.id = (product_id << 16) | build_number
                    entries.append({"build": compid & 0xFFFF, "product_id": compid >> 16, "count": count})
                    p += 8
                return True, key, entries
        idx = data.find(b"Rich", idx + 4, 0x400)
    return False, None, []


def section_flags(ch: int):
    out = []
    if ch & 0x00000020:
        out.append("CODE")
    if ch & 0x00000040:
        out.append("INITIALIZED_DATA")
    if ch & 0x00000080:
        out.append("UNINITIALIZED_DATA")
    if ch & 0x20000000:
        out.append("EXECUTE")
    if ch & 0x40000000:
        out.append("READ")
    if ch & 0x80000000:
        out.append("WRITE")
    if ch & 0x02000000:
        out.append("DISCARDABLE")
    return out


def get_pdb(pe):
    """CodeView RSDS debug entry -> (pdb path, guid, age)."""
    try:
        if not hasattr(pe, "DIRECTORY_ENTRY_DEBUG"):
            return None, None, None
        for entry in pe.DIRECTORY_ENTRY_DEBUG:
            try:
                st = entry.struct
                if getattr(st, "Type", None) != 2:
                    continue
                data = entry.entry
                raw = pe.get_data(entry.struct.AddressOfRawData, entry.struct.SizeOfData)
                if raw[:4] == b"RSDS":
                    guid = raw[4:20].hex()
                    age = struct.unpack_from("<I", raw, 20)[0]
                    path = raw[24:].split(b"\x00", 1)[0].decode("utf-8", "replace")
                    return path, guid, age
                if raw[:4] == b"NB10":
                    path = raw[16:].split(b"\x00", 1)[0].decode("utf-8", "replace")
                    return path, None, None
            except Exception:
                continue
    except Exception:
        return None, None, None
    return None, None, None


def parse_one(path: str, rel: str):
    st = os.stat(path)
    rec = {
        "path": rel,
        "size": st.st_size,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "sha256": sha256_file(path),
        "parse_error": None,
        "machine": None,
        "machine_name": None,
        "subsystem": None,
        "subsystem_name": None,
        "characteristics": [],
        "linker_version": None,
        "timestamp": None,
        "timestamp_raw": None,
        "image_base": None,
        "size_of_image": None,
        "entry_point": None,
        "os_version": None,
        "has_rich_header": False,
        "rich_builds": [],
        "rich_entries": [],
        "pdb_path": None,
        "pdb_guid": None,
        "pdb_age": None,
        "is_dotnet": False,
        "clr_version": None,
        "is_dll": False,
        "is_driver": False,
        "sections": [],
        "imports": {},
        "delay_imports": {},
        "exports": [],
        "export_count": 0,
    }
    with open(path, "rb") as f:
        head = f.read(0x600)
    present, key, entries = parse_rich(head)
    rec["has_rich_header"] = present
    if present:
        rec["rich_entries"] = entries
        rec["rich_builds"] = sorted({e["build"] for e in entries if e["build"]})

    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(
            directories=[DIR_IMPORT, DIR_EXPORT, DIR_DELAY, DIR_DEBUG, DIR_COM]
        )
    except Exception as exc:  # noqa: BLE001
        rec["parse_error"] = f"{type(exc).__name__}: {exc}"
        return rec

    try:
        fh = pe.FILE_HEADER
        oh = pe.OPTIONAL_HEADER
        rec["machine"] = fh.Machine
        rec["machine_name"] = MACHINE.get(fh.Machine, f"0x{fh.Machine:x}")
        rec["subsystem"] = oh.Subsystem
        rec["subsystem_name"] = SUBSYSTEM.get(oh.Subsystem, f"0x{oh.Subsystem:x}")
        rec["linker_version"] = f"{oh.MajorLinkerVersion}.{oh.MinorLinkerVersion}"
        rec["timestamp_raw"] = fh.TimeDateStamp
        rec["timestamp"] = datetime.fromtimestamp(
            fh.TimeDateStamp, timezone.utc
        ).isoformat()
        rec["image_base"] = hex(oh.ImageBase)
        rec["size_of_image"] = oh.SizeOfImage
        rec["entry_point"] = hex(oh.AddressOfEntryPoint)
        rec["os_version"] = f"{oh.MajorOperatingSystemVersion}.{oh.MinorOperatingSystemVersion}"
        rec["is_dll"] = bool(fh.Characteristics & 0x2000)
        rec["is_driver"] = bool(oh.Subsystem == 1)
        for name, bit in (
            ("EXECUTABLE_IMAGE", 0x0002),
            ("LARGE_ADDRESS_AWARE", 0x0020),
            ("DLL", 0x2000),
            ("DEBUG_STRIPPED", 0x0200),
            ("32BIT_MACHINE", 0x0100),
        ):
            if fh.Characteristics & bit:
                rec["characteristics"].append(name)
        for sec in pe.sections:
            nm = sec.Name.rstrip(b"\x00").decode("latin-1", "replace")
            rec["sections"].append(
                {
                    "name": nm,
                    "va": hex(sec.VirtualAddress),
                    "vsize": sec.Misc_VirtualSize,
                    "raw_size": sec.SizeOfRawData,
                    "raw_ptr": hex(sec.PointerToRawData),
                    "flags": section_flags(sec.Characteristics),
                }
            )
        rec["pdb_path"], rec["pdb_guid"], rec["pdb_age"] = get_pdb(pe)

        if hasattr(pe, "DIRECTORY_ENTRY_COM_DESCRIPTOR"):
            rec["is_dotnet"] = True
            try:
                dv = pe.DIRECTORY_ENTRY_COM_DESCRIPTOR.struct
                rec["clr_version"] = (
                    f"{dv.MajorRuntimeVersion}.{dv.MinorRuntimeVersion}"
                )
            except Exception:
                pass

        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            for entry in pe.DIRECTORY_ENTRY_IMPORT:
                dll = (entry.dll or b"").decode("latin-1", "replace")
                funcs = []
                for imp in entry.imports:
                    if imp.name:
                        funcs.append(imp.name.decode("latin-1", "replace"))
                    elif imp.ordinal is not None:
                        funcs.append(f"ordinal_{imp.ordinal}")
                rec["imports"][dll] = funcs
        if hasattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in pe.DIRECTORY_ENTRY_DELAY_IMPORT:
                dll = (entry.dll or b"").decode("latin-1", "replace")
                funcs = []
                for imp in entry.imports:
                    if imp.name:
                        funcs.append(imp.name.decode("latin-1", "replace"))
                    elif imp.ordinal is not None:
                        funcs.append(f"ordinal_{imp.ordinal}")
                rec["delay_imports"][dll] = funcs
        if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
            for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
                rec["exports"].append(
                    {
                        "ordinal": exp.ordinal,
                        "name": (exp.name or b"").decode("latin-1", "replace")
                        if exp.name
                        else None,
                        "rva": hex(exp.address) if exp.address else None,
                        "forwarder": exp.forwarder.decode("latin-1", "replace")
                        if exp.forwarder
                        else None,
                    }
                )
            rec["export_count"] = len(rec["exports"])
    except Exception as exc:  # noqa: BLE001
        rec["parse_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            pe.close()
        except Exception:
            pass
    return rec


def main():
    files = []
    for dirpath, _dirnames, filenames in os.walk(ROOT):
        for fn in filenames:
            low = fn.lower()
            if low.endswith(".exe") or low.endswith(".dll"):
                full = os.path.join(dirpath, fn)
                files.append(full)
    files.sort()
    print(f"found {len(files)} PE candidates", flush=True)

    modules = {}
    skipped = []
    for i, full in enumerate(files, 1):
        rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
        try:
            size = os.stat(full).st_size
        except OSError as exc:
            skipped.append({"path": rel, "reason": f"stat: {exc}"})
            continue
        if size > MAX_SIZE:
            skipped.append({"path": rel, "reason": f"size {size} > 200MB"})
            continue
        try:
            modules[rel] = parse_one(full, rel)
        except Exception as exc:  # noqa: BLE001
            modules[rel] = {
                "path": rel,
                "size": size,
                "parse_error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=3),
            }
        if i % 50 == 0 or i == len(files):
            print(f"  [{i}/{len(files)}] {rel}", flush=True)

    doc = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "root": ROOT,
        "tool": {"pefile": pefile.__version__, "python": sys.version.split()[0]},
        "max_size_rule": "skip >200 MiB",
        "counts": {
            "pe_files": len(modules),
            "exe": sum(1 for m in modules.values() if m["path"].lower().endswith(".exe")),
            "dll": sum(1 for m in modules.values() if m["path"].lower().endswith(".dll")),
            "dotnet": sum(1 for m in modules.values() if m.get("is_dotnet")),
            "native": sum(1 for m in modules.values() if not m.get("is_dotnet")),
            "with_rich_header": sum(1 for m in modules.values() if m.get("has_rich_header")),
            "with_pdb": sum(1 for m in modules.values() if m.get("pdb_path")),
            "parse_errors": sum(1 for m in modules.values() if m.get("parse_error")),
            "machine": {},
        },
        "skipped": skipped,
        "modules": modules,
    }
    for m in modules.values():
        mn = m.get("machine_name") or "PARSE_ERROR"
        doc["counts"]["machine"][mn] = doc["counts"]["machine"].get(mn, 0) + 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=False, ensure_ascii=False)
    os.replace(tmp, OUT)
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
