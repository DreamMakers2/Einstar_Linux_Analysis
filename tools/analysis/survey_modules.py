#!/usr/bin/env python3
"""Reproducible static survey of the SHINING 3D Einstar reconstruction modules.

This tool is read-only with respect to the vendor installation.  For every
requested PE it records, with file offsets wherever possible:

  * SHA-256, size, PE machine/timestamp/linker, PDB path
  * sections (RVA, raw offset/size, entropy) including the CUDA fatbin
  * imports (DLL -> function list) and delay imports
  * exports (raw MSVC mangled + demangled via llvm-undname when available)
  * embedded CUDA kernel names found in .nv_fatb / .text (section-name strings)
  * CUDA driver-API evidence (nvcuda.dll, cuLaunchKernel, ...)
  * keyword-classified ASCII and UTF-16LE strings with file offsets

Outputs (default):
  analysis/gnu/reconstruction_strings/<Module>.imports.txt
  analysis/gnu/reconstruction_strings/<Module>.exports.txt
  analysis/gnu/reconstruction_strings/<Module>.cuda.txt
  analysis/gnu/reconstruction_strings/<Module>.curated_strings.txt
  analysis/gnu/reconstruction_strings/<Module>.params.txt
  analysis/gnu/reconstruction_strings/App_EA.config.txt
  analysis/gnu/reconstruction_survey.json

No vendor binary is executed and no file in the vendor tree is written.

Usage:
  python3 tools/analysis/survey_modules.py \
      --root "<EXStar-install>/Shining3d/EXStar" \
      --out analysis/gnu/reconstruction_strings \
      --json analysis/gnu/reconstruction_survey.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from typing import Iterable, Iterator

try:
    import pefile
except ImportError as exc:  # pragma: no cover
    sys.exit("pefile is required: pip install pefile (%s)" % exc)


# ---------------------------------------------------------------------------
# Target set.  "role" is an analyst label, not a vendor claim.
# ---------------------------------------------------------------------------
MODULES: "OrderedDict[str, dict]" = OrderedDict(
    [
        ("Sn3DSpeckle.dll", {"role": "speckle/white-light depth reconstruction"}),
        ("Sn3DSpeckleFusion.dll", {"role": "speckle reconstruction + depth fusion variant"}),
        ("Sn3DPhaseBuild.dll", {"role": "phase-shift / structured-light reconstruction"}),
        ("Sn3DGratingRecons.dll", {"role": "grating (fringe) reconstruction"}),
        ("Sn3DRealtimeScan.dll", {"role": "realtime scan orchestration / SLAM"}),
        ("Sn3DRegistration.dll", {"role": "registration (Ceres, OpenMesh)"}),
        ("Sn3DGeometricTrackFusion.dll", {"role": "geometric tracking + fusion"}),
        ("Sn3DTextureBasedTrack.dll", {"role": "texture/feature-based tracking"}),
        ("Sn3DTextureSlam.dll", {"role": "texture SLAM backend"}),
        ("Sn3DTextureSFM.dll", {"role": "structure-from-motion / texture"}),
        ("Sn3DTextureUtil.dll", {"role": "texture utilities"}),
        ("Sn3DTextureMapper.dll", {"role": "texture mapping"}),
        ("Sn3DCloudProcessGPU.dll", {"role": "GPU point-cloud processing"}),
        ("Sn3DColorCorrect.dll", {"role": "colour correction / white balance"}),
        ("Sn3DAlgorithmWrapper.dll", {"role": "algorithm facade"}),
        ("Sn3DScanController.dll", {"role": "scan controller"}),
        ("commonGpu.dll", {"role": "GPU primitives (CUDA fatbins)"}),
        ("common.dll", {"role": "geometry core (Cloud/PointImage/RigidMatrix)"}),
        ("CommonFunc.dll", {"role": "shared helpers"}),
        ("Sn3DMagic.dll", {"role": "support library imported by tracking modules"}),
        ("GlobalRegistrationProcess.dll", {"role": "global registration worker"}),
        ("GenerateDataProcess.dll", {"role": "data generation worker"}),
        ("algorithm1.dll", {"role": "vendor algorithm variant 1"}),
        ("algorithm2.dll", {"role": "vendor algorithm variant 2"}),
        ("algorithmHlj.dll", {"role": "vendor algorithm variant Hlj"}),
        ("algorithmLy.dll", {"role": "vendor algorithm variant Ly"}),
        ("algorithmLzy.dll", {"role": "vendor algorithm variant Lzy"}),
        ("algorithmZbt.dll", {"role": "vendor algorithm variant Zbt"}),
        ("ceres.dll", {"role": "Ceres Solver (third party)"}),
    ]
)

# ---------------------------------------------------------------------------
# String classification.  Categories overlap on purpose; a hit is an index into
# evidence, not a conclusion.
# ---------------------------------------------------------------------------
CATEGORIES: "OrderedDict[str, re.Pattern[bytes]]" = OrderedDict(
    [
        (
            "cuda",
            re.compile(
                rb"(?i)(nvcuda|cudart|cublas|cudnn|nvrtc|tensorrt|nvinfer|nv_fatb|"
                rb"cuLaunch|cuCtx|cuMem|cuModule|cuDevice|__global__|kernel_|"
                rb"sm_[0-9]+|compute_[0-9]+|maxThreadsPerBlock|sharedMemPerBlock|"
                rb"CUDA|GPU)"
            ),
        ),
        (
            "stereo",
            re.compile(
                rb"(?i)(stereo|dispar|parallax|rectif|epipolar|sgbm|semi.?global|"
                rb"\bwta\b|sub.?pix|left.?right|right.?left|consisten|cost.?aggreg|"
                rb"pyramid|match|correlat|ZNCC|SAD|SSD|census|patch)"
            ),
        ),
        (
            "speckle",
            re.compile(rb"(?i)(speckle|infrar|infra.?red|white.?light|projector|laser|pattern|dot)"),
        ),
        (
            "phase_grating",
            re.compile(
                rb"(?i)(phase|grating|sinus|fringe|structured.?light|gray.?code|"
                rb"unwrap|lookup.?table|projector|intensit)"
            ),
        ),
        (
            "depth",
            re.compile(
                rb"(?i)(depth|point.?cloud|pointimage|range.?image|invalid|hole|"
                rb"confidence|noise|outlier|island|smooth|median|gauss|normal|"
                rb"tangent|curvature|flying)"
            ),
        ),
        (
            "calib",
            re.compile(
                rb"(?i)(calib|intrin|extrin|distort|undistort|\bk1\b|\bk2\b|\bk3\b|"
                rb"\bp1\b|\bp2\b|focal|\bcx\b|\bcy\b|baseline|\bccf\b|\bccm\b|"
                rb"white.?balance|vignette|gamma|exposure|\bhdr\b|radiometric)"
            ),
        ),
        (
            "registration",
            re.compile(
                rb"(?i)(regist|\bicp\b|align|rigid|pose|track|slam|odometry|"
                rb"feature|correspond|fitness|converg|iterat|threshold|inlier|"
                rb"ransac|\bfgr\b|gicp|\bndt\b|loop.?clos|pose.?graph|bundle|"
                rb"\bba\b|reproject|ceres|g2o|gtsam)"
            ),
        ),
        (
            "fusion_mesh",
            re.compile(
                rb"(?i)(fusion|tsdf|voxel|surfel|marching|poisson|mesh|triangul|"
                rb"decimat|simplif|hole.?fill|watertight|laplacian|bilateral|"
                rb"normal.?estimat|integrat|truncat|sdf|octree)"
            ),
        ),
        (
            "texture",
            re.compile(
                rb"(?i)(texture|\brgb\b|\buv\b|atlas|unwrap|blend|exposure|"
                rb"white.?balance|gamma|vignette|\bsfm\b|\bmvs\b|colour|color)"
            ),
        ),
        (
            "params",
            re.compile(
                rb"(?i)(param|threshold|iteration|\biter\b|window|\bsize\b|level|"
                rb"scale|\bmin\b|\bmax\b|step|radius|epsilon|tolerance|weight|"
                rb"lambda|sigma|voxel|resolution|\bfps\b|width|height|win|range|"
                rb"ratio|factor|limit|count|number|degree|angle|distance)"
            ),
        ),
        (
            "logging",
            re.compile(
                rb"(?i)(error|warn|fail|invalid|start|finish|begin|end|cost|"
                rb"time|elapsed|\bms\b|debug|info|trace|assert|except|null|"
                rb"success|done)"
            ),
        ),
        (
            "paths_files",
            re.compile(
                rb"(?i)(\.(bmp|txt|asc|ply|stl|obj|bin|dat|json|xml|ini|log|png|"
                rb"jpg|jpeg|tif|tiff|exr|pcd|3mf)\b|D:\\Jenkins|/res/|ScanSets|"
                rb"App_EA|config\.db)"
            ),
        ),
    ]
)

# A string is "parameter-like" when it carries a number AND a tuning word.
PARAM_NUM = re.compile(
    rb"(?i)(range|window|win|size|level|scale|depth|dispar|threshold|iter|"
    rb"step|radius|sigma|gauss|smooth|normal|min|max|ratio|factor|weight|"
    rb"resolution|fps|exposure|gain|gamma|angle|degree|baseline|voxel|"
    rb"trunc|dist|error|time|cost)"
)
NUM_RE = re.compile(rb"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

ASCII_RE = re.compile(rb"[\x20-\x7e\t]{4,}")
UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")

# Kernel / section name evidence inside CUDA fatbins.
KERNEL_SECTION_RE = re.compile(rb"\.(?:text|nv\.shared|nv\.info)\.(_Z[A-Za-z0-9_$.]{6,})")
KERNEL_NAME_RE = re.compile(rb"\b(kernel_[A-Za-z0-9_]{3,})")
CUDA_API_RE = re.compile(rb"\b(cu[A-Z][A-Za-z0-9_]{2,})\b")
CUDA_LIB_RE = re.compile(rb"(?i)\b(nvcuda\.dll|cudart64_[0-9]+\.dll|cublas[0-9_]*\.dll|cudnn[0-9_]*\.dll|nvrtc[0-9_]*\.dll|nvinfer[0-9_]*\.dll)\b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def entropy(buf: bytes) -> float:
    if not buf:
        return 0.0
    c = Counter(buf)
    n = len(buf)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def iter_ascii(data: bytes, minlen: int = 4) -> Iterator[tuple[int, str]]:
    for m in ASCII_RE.finditer(data):
        s = m.group()
        if len(s) >= minlen:
            yield m.start(), s.decode("latin-1")


def iter_utf16(data: bytes, minlen: int = 4) -> Iterator[tuple[int, str]]:
    for m in UTF16_RE.finditer(data):
        raw = m.group()
        if len(raw) // 2 < minlen:
            continue
        try:
            s = raw.decode("utf-16-le", "replace")
        except Exception:
            continue
        # Reject CJK/latin noise that is really a bitmap: require >=70% printable.
        if sum(32 <= ord(ch) < 127 for ch in s) / max(1, len(s)) >= 0.7:
            yield m.start(), s


def _demangle_msvc(names: list[str]) -> dict[str, str]:
    """Demangle MSVC names with llvm-undname (best effort, chunked).

    llvm-undname echoes each input line followed by the demangled line; for
    non-mangled input it prints only the input, so the mapping is built from
    the echoed/demangled pairs.
    """
    out: dict[str, str] = {}
    tool = shutil.which("llvm-undname")
    if not tool:
        return {n: n for n in names}
    CH = 400
    for i in range(0, len(names), CH):
        chunk = names[i : i + CH]
        try:
            p = subprocess.run(
                [tool], input="\n".join(chunk) + "\n", text=True,
                capture_output=True, timeout=120,
            )
        except Exception:
            out.update({n: n for n in chunk})
            continue
        lines = p.stdout.splitlines()
        j = 0
        while j < len(lines):
            raw = lines[j]
            if raw in chunk and j + 1 < len(lines) and lines[j + 1].strip():
                out[raw] = lines[j + 1].strip()
                j += 2
            else:
                if raw in chunk:
                    out.setdefault(raw, raw)
                j += 1
    return out


def _demangle_itanium(names: list[str]) -> dict[str, str]:
    """Demangle GCC/Clang Itanium names (CUDA kernel symbols) with c++filt."""
    out: dict[str, str] = {}
    tool = shutil.which("c++filt")
    if not tool:
        return {n: n for n in names}
    CH = 500
    for i in range(0, len(names), CH):
        chunk = names[i : i + CH]
        try:
            p = subprocess.run(
                [tool], input="\n".join(chunk) + "\n", text=True,
                capture_output=True, timeout=120,
            )
            lines = p.stdout.splitlines()
        except Exception:
            lines = []
        for raw, dem in zip(chunk, lines):
            out[raw] = dem if dem and not dem.startswith("_Z") else raw
    return out


def demangle(names: Iterable[str]) -> dict[str, str]:
    names = list(names)
    msvc = [n for n in names if n.startswith("?")]
    itan = [n for n in names if n.startswith("_Z")]
    out = _demangle_msvc(msvc)
    out.update(_demangle_itanium(itan))
    for n in names:
        out.setdefault(n, n)
    return out


def read_section(pe, name: str) -> tuple[bytes, int, int]:
    for s in pe.sections:
        if s.Name.rstrip(b"\x00").decode("latin-1") == name:
            return s.get_data(), s.PointerToRawData, s.SizeOfRawData
    return b"", 0, 0


def pe_pdb(pe) -> str | None:
    try:
        for dbg in getattr(pe, "DIRECTORY_ENTRY_DEBUG", []):
            if dbg.struct.Type == 2:  # IMAGE_DEBUG_TYPE_CODEVIEW
                d = dbg.entry
                off = d.PointerToRawData
                with open(pe.__path__, "rb") as f:
                    f.seek(off)
                    blob = f.read(d.SizeOfData)
                if blob[:4] == b"RSDS":
                    return blob[24:].split(b"\x00", 1)[0].decode("latin-1", "replace")
    except Exception:
        pass
    return None


def pe_meta(path: str) -> dict:
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(
        directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"],
        ]
    )
    pe.__path__ = path
    sections = []
    for s in pe.sections:
        nm = s.Name.rstrip(b"\x00").decode("latin-1", "replace")
        raw = s.get_data()
        sections.append(
            {
                "name": nm,
                "rva": int(s.VirtualAddress),
                "vsize": int(s.Misc_VirtualSize),
                "raw_offset": int(s.PointerToRawData),
                "raw_size": int(s.SizeOfRawData),
                "entropy": round(entropy(raw), 4),
            }
        )
    imports = OrderedDict()
    for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        dll = e.dll.decode("latin-1", "replace")
        imports[dll] = [
            (i.name.decode("latin-1", "replace") if i.name else f"ordinal:{i.ordinal}")
            for i in e.imports
        ]
    delay = OrderedDict()
    for e in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
        dll = e.dll.decode("latin-1", "replace")
        delay[dll] = [
            (i.name.decode("latin-1", "replace") if i.name else f"ordinal:{i.ordinal}")
            for i in e.imports
        ]
    exports = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for s in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            exports.append(
                {
                    "name": s.name.decode("latin-1", "replace") if s.name else f"ordinal:{s.ordinal}",
                    "ordinal": int(s.ordinal),
                    "rva": int(s.address),
                }
            )
    meta = {
        "machine": hex(pe.FILE_HEADER.Machine),
        "machine_name": pefile.MACHINE_TYPE.get(pe.FILE_HEADER.Machine, "unknown"),
        "timestamp": int(pe.FILE_HEADER.TimeDateStamp),
        "timestamp_utc": _dt.datetime.fromtimestamp(
            pe.FILE_HEADER.TimeDateStamp, tz=_dt.timezone.utc
        ).isoformat(),
        "linker": f"{pe.OPTIONAL_HEADER.MajorLinkerVersion}.{pe.OPTIONAL_HEADER.MinorLinkerVersion}",
        "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
        "entry_point": hex(pe.OPTIONAL_HEADER.AddressOfEntryPoint),
        "sections": sections,
        "imports": imports,
        "delay_imports": delay,
        "exports": exports,
        "pdb": pe_pdb(pe),
    }
    pe.close()
    return meta


def cuda_evidence(path: str, meta: dict, data: bytes) -> dict:
    fatb, fatb_off, fatb_size = read_section(pefile.PE(path, fast_load=True), ".nv_fatb")
    # Kernel names: section-name strings inside the fatbin and mangled symbols.
    kernels: "OrderedDict[str, dict]" = OrderedDict()
    for m in KERNEL_SECTION_RE.finditer(fatb):
        sym = m.group(1).decode("latin-1")
        rec = kernels.setdefault(sym, {"offset": fatb_off + m.start(), "sections": Counter()})
        rec["sections"][m.group()[: m.group().find(b".", 1)].decode("latin-1")] += 1
    for m in KERNEL_NAME_RE.finditer(data):
        name = m.group(1).decode("latin-1")
        kernels.setdefault(name, {"offset": m.start(), "sections": Counter()})
    # Some modules (e.g. Sn3DMagic) store only compressed PTX/cubins: there are
    # no ELF section-name strings, but the PTX still contains mangled entry
    # symbols.  Scan those only when the section-name pass found (almost)
    # nothing, to avoid pulling thousands of thrust internals into every dump.
    if len(kernels) < 5 and fatb:
        for m in re.finditer(rb"(_Z[0-9A-Za-z_]{8,})", fatb):
            sym = m.group(1).decode("latin-1")
            if sym.startswith(("_ZN6thrust", "_ZN3cub", "_ZN4cub", "_ZN5thrust")):
                continue
            kernels.setdefault(sym, {"offset": fatb_off + m.start(), "sections": Counter()})
            kernels[sym]["sections"]["ptx"] += 1
    dem = demangle(list(kernels))
    kernel_list = []
    for sym, rec in kernels.items():
        kernel_list.append(
            {
                "symbol": sym,
                "demangled": dem.get(sym, sym),
                "file_offset": rec["offset"],
                "occurrences": dict(rec["sections"]),
            }
        )
    api = sorted({m.group(1).decode("latin-1") for m in CUDA_API_RE.finditer(data)})
    libs = sorted({m.group(1).decode("latin-1") for m in CUDA_LIB_RE.finditer(data)})
    return {
        "fatbin": {
            "section": ".nv_fatb",
            "file_offset": fatb_off,
            "size": fatb_size,
            "elf_images": fatb.count(b"\x7fELF"),
        },
        "kernel_count": len(kernel_list),
        "kernels": sorted(kernel_list, key=lambda k: k["file_offset"]),
        "driver_api": {
            "names": api,
            "count": len(api),
            "has_cuLaunchKernel": "cuLaunchKernel" in api,
            "has_nvcuda_dll": "nvcuda.dll" in libs,
        },
        "libraries": libs,
    }


def scan_strings(data: bytes, minlen: int) -> tuple[list[tuple[int, str, str]], Counter]:
    """Return (offset, encoding, text) plus per-category counts."""
    cats: Counter = Counter()
    hits: list[tuple[int, str, str]] = []
    for off, s in iter_ascii(data, minlen):
        b = s.encode("latin-1", "ignore")
        matched = [name for name, rx in CATEGORIES.items() if rx.search(b)]
        if matched:
            for name in matched:
                cats[name] += 1
            hits.append((off, "ascii", s))
    for off, s in iter_utf16(data, minlen):
        b = s.encode("utf-16-le", "ignore")
        matched = [name for name, rx in CATEGORIES.items() if rx.search(b)]
        if matched:
            for name in matched:
                cats[name] += 1
            hits.append((off, "utf16", s))
    hits.sort(key=lambda t: t[0])
    return hits, cats


def fmt_offset(off: int) -> str:
    return f"0x{off:08X}"


def write_module_dumps(out_dir: str, name: str, meta: dict, cuda: dict,
                       hits: list[tuple[int, str, str]], cats: Counter,
                       dem_exports: dict[str, str], dem_imports: dict[str, str]) -> None:
    base = os.path.join(out_dir, name[:-4] if name.lower().endswith(".dll") else name)

    with open(base + ".imports.txt", "w", encoding="utf-8") as f:
        f.write(f"# module: {name}\n# imports (DLL -> count), demangled where MSVC C++\n")
        for dll, funcs in meta["imports"].items():
            f.write(f"\n[{dll}] {len(funcs)}\n")
            for fn in funcs:
                d = dem_imports.get(fn, fn)
                f.write(f"  {fn}\n" + (f"      -> {d}\n" if d != fn else ""))
        for dll, funcs in meta["delay_imports"].items():
            f.write(f"\n[delay:{dll}] {len(funcs)}\n")
            for fn in funcs:
                f.write(f"  {fn}\n")

    with open(base + ".exports.txt", "w", encoding="utf-8") as f:
        f.write(f"# module: {name}\n# exports: {len(meta['exports'])}\n")
        f.write("# RVA is the preferred image base address; file offset differs by section.\n")
        for e in meta["exports"]:
            d = dem_exports.get(e["name"], e["name"])
            f.write(f"\nRVA 0x{e['rva']:08X} ord {e['ordinal']}\n  {e['name']}\n")
            if d != e["name"]:
                f.write(f"  -> {d}\n")

    with open(base + ".cuda.txt", "w", encoding="utf-8") as f:
        fb = cuda["fatbin"]
        f.write(f"# module: {name}\n")
        f.write(f"# .nv_fatb file_offset={fmt_offset(fb['file_offset'])} size={fb['size']} "
                f"embedded_elf_images={fb['elf_images']}\n")
        f.write(f"# kernel/PTX entry symbols: {cuda['kernel_count']}\n")
        f.write(f"# driver API names: {cuda['driver_api']['count']} "
                f"cuLaunchKernel={cuda['driver_api']['has_cuLaunchKernel']} "
                f"nvcuda.dll={cuda['driver_api']['has_nvcuda_dll']}\n")
        f.write(f"# cuda libraries: {', '.join(cuda['libraries']) or '-'}\n")
        f.write("\n## kernels\n")
        for k in cuda["kernels"]:
            f.write(f"\nfile {fmt_offset(k['file_offset'])}  {k['symbol']}\n")
            if k["demangled"] != k["symbol"]:
                f.write(f"  -> {k['demangled']}\n")
        f.write("\n## driver API strings\n")
        for a in cuda["driver_api"]["names"]:
            f.write(f"  {a}\n")

    with open(base + ".curated_strings.txt", "w", encoding="utf-8") as f:
        f.write(f"# module: {name}\n")
        f.write("# offset\\tencoding\\tcategories\\tstring\n")
        f.write("# categories: " + ", ".join(CATEGORIES) + "\n")
        for off, enc, s in hits:
            b = s.encode("latin-1" if enc == "ascii" else "utf-16-le", "ignore")
            matched = ",".join(n for n, rx in CATEGORIES.items() if rx.search(b))
            s = s.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
            f.write(f"{fmt_offset(off)}\t{enc}\t{matched}\t{s[:500]}\n")

    with open(base + ".params.txt", "w", encoding="utf-8") as f:
        f.write(f"# module: {name}\n# parameter-like strings carrying at least one numeric token\n")
        f.write("# offset\\tencoding\\tnumbers\\tstring\n")
        for off, enc, s in hits:
            b = s.encode("latin-1" if enc == "ascii" else "utf-16-le", "ignore")
            if not (PARAM_NUM.search(b) and NUM_RE.search(b)):
                continue
            nums = ",".join(m.group().decode("latin-1") for m in NUM_RE.finditer(b))[:200]
            s = s.replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
            f.write(f"{fmt_offset(off)}\t{enc}\t{nums}\t{s[:400]}\n")


def parse_app_ea(xml_path: str, out_path: str) -> dict:
    import xml.etree.ElementTree as ET

    summary: dict = {"path": xml_path, "present": os.path.exists(xml_path)}
    if not summary["present"]:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# App_EA.xml not found\n")
        return summary
    tree = ET.parse(xml_path)
    root = tree.getroot()
    cfg = root.find("config")
    if cfg is not None:
        summary["config"] = {c.tag: (c.text or "").strip() for c in cfg}
    # Topic / controller inventory (the file is an app-framework wiring manifest,
    # not an algorithm parameter file).
    topics = []
    for t in root.iter("topic"):
        name = t.findtext("name")
        typ = t.findtext("type")
        pubs = [p.findtext("name") for p in t.iter("publisher")]
        subs = [s.findtext("name") for s in t.iter("subscriber")]
        topics.append({"name": name, "type": typ, "publishers": pubs, "subscribers": subs})
    summary["topic_count"] = len(topics)
    summary["topics"] = topics
    libs = sorted({e.text.strip() for e in root.iter("libpath") if e.text})
    summary["libpaths"] = libs
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# App_EA.xml — application wiring (read-only vendor install)\n")
        f.write("# This is a message-bus/topic manifest; it contains no reconstruction tuning values.\n")
        f.write(f"# config block: {json.dumps(summary.get('config', {}), ensure_ascii=False)}\n")
        f.write(f"# topics: {len(topics)}\n\n")
        for t in topics:
            f.write(f"[{t['type']}] {t['name']}\n")
            f.write(f"  publishers : {', '.join(x for x in t['publishers'] if x)}\n")
            f.write(f"  subscribers: {', '.join(x for x in t['subscribers'] if x)}\n")
        f.write("\n# libpaths\n")
        for l in libs:
            f.write(f"  {l}\n")
    return summary


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="<EXStar-install>/Shining3d/EXStar")
    ap.add_argument("--out", default="analysis/gnu/reconstruction_strings")
    ap.add_argument("--json", default="analysis/gnu/reconstruction_survey.json")
    ap.add_argument("--modules", default="", help="comma separated override list")
    ap.add_argument("--min-len", type=int, default=4)
    ap.add_argument("--skip-demangle", action="store_true")
    args = ap.parse_args(argv)

    targets = list(MODULES)
    if args.modules:
        targets = [m.strip() for m in args.modules.split(",") if m.strip()]
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)

    survey: dict = {
        "generated_utc": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "root": args.root,
        "tool": "tools/analysis/survey_modules.py",
        "python": sys.version.split()[0],
        "pefile": pefile.__version__,
        "modules": OrderedDict(),
    }

    for i, name in enumerate(targets, 1):
        path = os.path.join(args.root, name)
        if not os.path.exists(path):
            print(f"[{i}/{len(targets)}] MISSING {name}", file=sys.stderr)
            survey["modules"][name] = {"present": False, "role": MODULES.get(name, {}).get("role")}
            continue
        print(f"[{i}/{len(targets)}] {name}", file=sys.stderr)
        data = open(path, "rb").read()
        meta = pe_meta(path)
        cuda = cuda_evidence(path, meta, data)
        hits, cats = scan_strings(data, args.min_len)
        if args.skip_demangle:
            dem_e = {e["name"]: e["name"] for e in meta["exports"]}
            dem_i: dict[str, str] = {}
        else:
            dem_e = demangle([e["name"] for e in meta["exports"]])
            dem_i = demangle({fn for fl in meta["imports"].values() for fn in fl})
        write_module_dumps(args.out, name, meta, cuda, hits, cats, dem_e, dem_i)
        survey["modules"][name] = {
            "present": True,
            "role": MODULES.get(name, {}).get("role"),
            "sha256": sha256_file(path),
            "size": len(data),
            "pe": meta,
            "cuda": cuda,
            "string_counts": dict(cats),
            "curated_string_count": len(hits),
        }
        # Do not embed multi-megabyte string arrays in the JSON; the dumps carry them.

    app = parse_app_ea(
        os.path.join(args.root, "App_EA.xml"),
        os.path.join(args.out, "App_EA.config.txt"),
    )
    survey["app_ea"] = app
    cfg_db = os.path.join(args.root, "res", "config.db")
    survey["config_db"] = {
        "path": cfg_db,
        "present": os.path.exists(cfg_db),
        "size": os.path.getsize(cfg_db) if os.path.exists(cfg_db) else None,
        "sha256": sha256_file(cfg_db) if os.path.exists(cfg_db) else None,
        "header_hex": open(cfg_db, "rb").read(32).hex() if os.path.exists(cfg_db) else None,
        "status": "opaque/encrypted; no SQLite or SQLCipher page validated (see docs)",
    }
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(survey, f, indent=2, sort_keys=False)
    print(f"\nwrote {args.json}", file=sys.stderr)
    print(f"wrote dumps under {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
