#!/usr/bin/env python3
"""Derived analysis over analysis/gnu/pe_inventory.json.

Produces:
  analysis/gnu/module_dependencies.json
  analysis/gnu/classification.json
  analysis/gnu/calibration_api.json
  analysis/gnu/flagged_exports.json
  analysis/gnu/toolchain.json
  analysis/gnu/usb_stack.json
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

import pefile

BASE = "<repo>/analysis/gnu"
INV = os.path.join(BASE, "pe_inventory.json")
ROOT = "<EXStar-install>/Shining3d/EXStar"
DLL_RE = re.compile(rb"[A-Za-z0-9_.\-+]{2,64}\.dll", re.IGNORECASE)

CALIB_MODULES = [
    "Sn3DCalibDevFile.dll",
    "Sn3DCalibrateData.dll",
    "Sn3DCalibrateLoader.dll",
    "Sn3DCalibrateAlgorithmWrapper.dll",
    "Sn3DCalibrateAlgorithmHelper.dll",
    "Sn3DCameraCalibHelper.dll",
    "Sn3DCalibCaptureImageProcess.dll",
    "Sn3DEACalibrateController.dll",
    "Sn3DCalibBigCirExtract.dll",
    "Sn3DCalibrateImageInfo.dll",
    "CalibrationPage_EA.dll",
    "Sn3DCalibrationJR.dll",
    "Sn3DTurntableController.dll",
    "Sn3DAutoExposureHelper.dll",
    "Sn3DTexCameraHelper.dll",
]

DEVICE_OWNERS = [
    "Sn3DDeviceEinStar.dll",
    "Sn3DEinStarController.dll",
    "sn3DHIDCommunication.dll",
    "SiUSBXp.dll",
    "UsbTreeModel.dll",
    "Sn3DScanController.dll",
    "Sn3DRealtimeScan.dll",
    "Sn3DTexCameraHelper.dll",
    "sn3DTurnTable.dll",
    "Sn3DDeveloperControlPlugin.dll",
    "Sn3DSoftwareUpdate.dll",
    "Sn3DSoftwareUpgradeController.dll",
]

# module basename -> category membership is derived from imports/names below.
QT_RE = re.compile(r"^Qt5[A-Za-z]+\.dll$", re.IGNORECASE)


def load_inv():
    with open(INV, encoding="utf-8") as f:
        return json.load(f)


def section_blobs(path, wanted=(".rdata", ".data")):
    try:
        pe = pefile.PE(path, fast_load=True)
    except Exception:
        return []
    out = []
    for sec in pe.sections:
        nm = sec.Name.rstrip(b"\x00").decode("latin-1", "replace")
        if nm.startswith(wanted):
            try:
                out.append(sec.get_data())
            except Exception:
                pass
    pe.close()
    return out


def readable_export(name: str) -> str:
    """Heuristic readability aid for MSVC-mangled names (not a demangler)."""
    if not name.startswith("?"):
        return name
    body = name[1:]
    head = body.split("@@")[0]
    toks = [t.replace("?$", "").replace("$", "") for t in head.split("@")]
    toks = [t for t in toks if t]
    if not toks:
        return name
    if len(toks) == 1:
        tok = toks[0]
        m = re.match(r"^(_[0-9A-Z]|[0-9])(?=[A-Za-z_])", tok)
        if m:
            code, rest = m.group(1), tok[m.end():]
            if code in ("0", "_F"):
                return f"{rest}::{rest}"
            if code == "1":
                return f"{rest}::~{rest}"
            if code == "4":
                return f"{rest}::operator="
            if code == "_7":
                return f"{rest}::vftable"
            return f"{rest}::{rest}"
        return tok
    method, cls = toks[0], toks[1]
    if cls.endswith("Controller") or cls[:1].isupper():
        return f"{cls}::{method}"
    return f"{method}::{cls}"


def main():
    inv = load_inv()
    mods = inv["modules"]
    by_base = {}
    for rel in mods:
        base = rel.rsplit("/", 1)[-1].lower()
        by_base.setdefault(base, []).append(rel)

    edges, dynamic, external = [], [], Counter()
    for rel, rec in mods.items():
        for dll, funcs in rec.get("imports", {}).items():
            base = dll.lower()
            if base in by_base:
                for target in by_base[base]:
                    if target != rel:
                        edges.append({"from": rel, "to": target, "kind": "import", "dll": dll,
                                      "api_count": len(funcs)})
            else:
                external[dll] += 1
        for dll, funcs in rec.get("delay_imports", {}).items():
            base = dll.lower()
            if base in by_base:
                for target in by_base[base]:
                    if target != rel:
                        edges.append({"from": rel, "to": target, "kind": "delay_import",
                                      "dll": dll, "api_count": len(funcs)})

    # dynamic-load string references (only for DLLs shipped in this tree)
    tracked = set(by_base)
    for rel in sorted(mods):
        path = os.path.join(ROOT, rel)
        try:
            if os.path.getsize(path) > 60 * 1024 * 1024:
                continue
        except OSError:
            continue
        found = set()
        for blob in section_blobs(path):
            for m in DLL_RE.finditer(blob):
                nm = m.group().decode("latin-1", "replace")
                if nm[0] in "+-." or nm.startswith("api-ms"):
                    continue
                if nm.lower() in tracked:
                    found.add(nm)
        imported = {d.lower() for d in rec.get("imports", {})} | {
            d.lower() for d in rec.get("delay_imports", {})}
        for nm in sorted(found):
            if nm.lower() in imported:
                continue
            for target in by_base[nm.lower()]:
                if target != rel:
                    dynamic.append({"from": rel, "to": target, "kind": "dynamic_ref",
                                    "string": nm})

    # compact dependency view
    deps = defaultdict(list)
    for e in edges + dynamic:
        deps[e["from"]].append({"to": e["to"], "kind": e["kind"]})
    dependents = defaultdict(list)
    for e in edges + dynamic:
        dependents[e["to"]].append({"from": e["from"], "kind": e["kind"]})

    json.dump({"edges": edges, "dynamic_refs": dynamic,
               "external_dlls": dict(external.most_common()),
               "dependencies": deps, "dependents": dependents},
              open(os.path.join(BASE, "module_dependencies.json"), "w"), indent=1)

    # ---------- classification ----------
    cls = defaultdict(lambda: {"modules": [], "evidence": {}})

    def add(cat, rel, ev):
        if rel not in cls[cat]["modules"]:
            cls[cat]["modules"].append(rel)
        e = cls[cat]["evidence"].setdefault(rel, [])
        if ev and ev not in e:
            e.append(ev)

    usb_api_pat = re.compile(r"^(SetupDi|HidD_|HidP_|WinUsb_|SI_|CM_|libusb_)", re.I)
    for rel, rec in mods.items():
        imps = rec.get("imports", {})
        lows = [d.lower() for d in imps]
        for dll in lows:
            if dll == "siusbxp.dll":
                add("usb_hid", rel, f"imports SiUSBXp.dll ({len(imps['SiUSBXp.dll'])} ordinals)")
            if dll == "hid.dll":
                add("usb_hid", rel, "imports HID.DLL")
            if dll == "setupapi.dll":
                add("usb_hid", rel, "imports SETUPAPI.dll")
            if dll in ("winusb.dll", "libusb-1.0.dll", "cyusb.dll", "cyapi.dll"):
                add("usb_hid", rel, f"imports {dll}")
            if dll == "sn3dhidcommunication.dll":
                add("usb_hid", rel, "imports sn3DHIDCommunication.dll")
            if dll == "usbtreemodel.dll":
                add("usb_hid", rel, "imports UsbTreeModel.dll")
        for dll, funcs in imps.items():
            hits = [f for f in funcs if usb_api_pat.match(f)]
            if hits:
                add("usb_hid", rel, f"{dll}: {', '.join(sorted(hits)[:6])}")
            if any("DeviceIoControl" == f for f in funcs):
                add("usb_hid", rel, f"{dll}: DeviceIoControl")
            if any(f in ("CreateFileW", "CreateFileA") for f in funcs) and "siusbxp" in dll.lower():
                pass

    for rel in mods:
        base = rel.rsplit("/", 1)[-1]
        if base in ("sn3DHIDCommunication.dll", "syncservice/sn3DHIDCommunication.dll",
                    "syncservice/sn3DHIDCommunicationM.dll", "syncservice/sn3DHIDCommunicationNew.dll",
                    "SiUSBXp.dll", "UsbTreeModel.dll", "syncservice/UsbTreeModel.dll",
                    "syncservice/CommunicateLayer.dll", "syncservice/sn3DCommunicateLayerM.dll"):
            add("usb_hid", rel, "USB/HID implementation module")

    # crypto / anti-tamper
    crypto_names = ("sn3dcrypto", "sn3dsecurity", "cryptopp", "hasp", "libcrypto", "libssl",
                    "sqlitecipher", "sn3dlock", "tunnel_encrypt", "libeay", "ssleay",
                    "vcamp", "sn3dpassportcontroller")
    crypt_imports = re.compile(r"^(BCrypt|Crypt|NCrypt|Cert)", re.I)
    for rel, rec in mods.items():
        b = rel.rsplit("/", 1)[-1].lower()
        if any(b.startswith(n) for n in crypto_names):
            add("crypto", rel, "crypto/security/licensing module")
        for dll, funcs in rec.get("imports", {}).items():
            if dll.lower() in ("sn3dcrypto.dll", "sn3dsecurity.dll", "cryptopp.dll",
                               "hasp_windows_x64_105562.dll", "libcrypto-1_1-x64.dll",
                               "sqlitecipher.dll", "sn3dlock.dll", "tunnel_encrypt.dll",
                               "libeay32.dll", "ssleay32.dll"):
                add("crypto", rel, f"imports {dll} ({len(funcs)})")
            hits = [f for f in funcs if crypt_imports.match(f)]
            if hits:
                add("crypto", rel, f"{dll}: {', '.join(sorted(hits)[:5])}")

    # GPU / compute
    gpu_dlls = {"cudart64_102.dll", "cusolver64_10.dll", "nppc64_10.dll", "nppig64_10.dll",
                "nvcuda.dll", "cublas64_10.dll", "cudnn64_7.dll", "nvml.dll", "opencl.dll",
                "nvinfer.dll", "d3d11.dll", "dxgi.dll", "d3d9.dll", "opengl32.dll",
                "d3dcompiler_47.dll", "libegl.dll", "libglesv2.dll", "opengl32sw.dll",
                "mkl_rt.dll", "mkl_core.dll", "mkl_def.dll", "mkl_sequential.dll",
                "mkl_tbb_thread.dll", "tbb.dll"}
    for rel, rec in mods.items():
        for dll, funcs in rec.get("imports", {}).items():
            if dll.lower() in gpu_dlls:
                add("gpu_compute", rel, f"imports {dll}")
        b = rel.rsplit("/", 1)[-1].lower()
        if b.startswith(("osg158-", "ot21-")) or b.startswith("cudart") or b.startswith("cusolver") \
                or b.startswith("npp") or b.startswith("mkl_") or b.startswith("opengl32sw"):
            add("gpu_compute", rel, "graphics/compute runtime module")
        if "librenderkit" in b and "cuda" in b:
            add("gpu_compute", rel, "CUDA render-kit plugin")

    # vision / reconstruction
    vis_prefix = ("opencv_", "ceres", "openmesh", "lib3mf", "vtk", "embree", "assimp",
                  "pcl_", "tbb", "libblas", "liblapack", "libgfortran", "libquadmath", "mpir",
                  "eigen")
    for rel, rec in mods.items():
        b = rel.rsplit("/", 1)[-1].lower()
        if b.startswith(vis_prefix):
            add("vision_recon", rel, "third-party vision/geometry/numeric library")
        for dll in rec.get("imports", {}):
            if dll.lower().startswith(("opencv_", "ceres", "openmesh", "lib3mf", "pcl_", "vtk")):
                add("vision_recon", rel, f"imports {dll}")
        if b.startswith("algorithm") or b.startswith("sn3d"):
            add("vision_recon", rel, "Shining3D algorithm/scanning module")

    # Qt
    for rel, rec in mods.items():
        b = rel.rsplit("/", 1)[-1]
        if QT_RE.match(b) or b.lower().startswith(("qttunnel", "libqt", "qt5")):
            add("qt", rel, "Qt module")
        else:
            for dll in rec.get("imports", {}):
                if QT_RE.match(dll) or dll.lower().startswith("qttunnel"):
                    add("qt", rel, f"imports {dll}")
                    break

    # calibration
    for rel in mods:
        if rel.rsplit("/", 1)[-1] in CALIB_MODULES:
            add("calibration", rel, "calibration API/data module")
    for rel, rec in mods.items():
        for dll in rec.get("imports", {}):
            if dll in CALIB_MODULES:
                add("calibration", rel, f"imports {dll}")
                break

    # firmware update
    for rel, rec in mods.items():
        b = rel.rsplit("/", 1)[-1].lower()
        if b.startswith(("firmwareupdate", "softwareupgrade", "sn3dsoftwareupdate")):
            add("firmware_update", rel, "firmware/software update module")

    # plug-ins / processes
    for rel, rec in mods.items():
        if rel.lower().endswith(".exe"):
            add("executable", rel, "PE executable")

    json.dump(cls, open(os.path.join(BASE, "classification.json"), "w"), indent=1)

    # ---------- calibration exports ----------
    calib = {}
    for rel in mods:
        b = rel.rsplit("/", 1)[-1]
        if b in CALIB_MODULES:
            ex = mods[rel]["exports"]
            calib[rel] = {
                "sha256": mods[rel]["sha256"],
                "size": mods[rel]["size"],
                "export_count": len(ex),
                "exports_verbatim": [e["name"] if e["name"] else f"ordinal_{e['ordinal']}"
                                     for e in ex],
            }
    json.dump(calib, open(os.path.join(BASE, "calibration_api.json"), "w"), indent=1)

    # ---------- flagged exports ----------
    kw = re.compile(r"firmware|update|upgrade|iap|fpga|usb|hid|camera|image|calib|ccf|"
                    r"version|flash|boot|device|sensor|frame|exposure|projector",
                    re.I)
    flagged = {}
    for rel in DEVICE_OWNERS + CALIB_MODULES:
        if rel not in mods:
            continue
        hits = []
        for e in mods[rel]["exports"]:
            nm = e["name"] or ""
            if kw.search(nm):
                hits.append({"ordinal": e["ordinal"], "name": nm,
                             "keywords": sorted({m.group().lower() for m in kw.finditer(nm)})})
        flagged[rel] = {"export_count": mods[rel]["export_count"], "flagged": hits}
    json.dump(flagged, open(os.path.join(BASE, "flagged_exports.json"), "w"), indent=1)

    # ---------- toolchain ----------
    link = Counter()
    rich_build = Counter()
    pdb_roots = Counter()
    runtime = Counter()
    rich_missing = []
    for rel, rec in mods.items():
        link[rec.get("linker_version")] += 1
        for b in rec.get("rich_builds", []):
            rich_build[b] += 1
        p = rec.get("pdb_path")
        if p:
            root = p.split("\\")[0] + "\\" + (p.split("\\")[1] if "\\" in p else "")
            pdb_roots[root] += 1
        imps = rec.get("imports", {})
        for d in ("MSVCP140.dll", "VCRUNTIME140.dll", "MSVCP120.dll", "MSVCR120.dll",
                  "MSVCP100.dll", "MSVCR100.dll", "api-ms-win-crt-runtime-l1-1-0.dll"):
            if d in imps:
                runtime[d] += 1
        if not rec.get("has_rich_header"):
            rich_missing.append(rel)
    toolchain = {
        "linker_versions": dict(link.most_common()),
        "rich_builds": dict(rich_build.most_common(25)),
        "pdb_roots": dict(pdb_roots.most_common(20)),
        "crt_imports": dict(runtime.most_common()),
        "no_rich_header": rich_missing,
        "compiler_spec_hint": "MSVC 19.16 (linker 14.16 = VS2017 15.9 toolset), x64, /MD (UCRT)",
    }
    json.dump(toolchain, open(os.path.join(BASE, "toolchain.json"), "w"), indent=1)

    # ---------- usb stack ----------
    usb = {}
    for rel in ["SiUSBXp.dll", "sn3DHIDCommunication.dll", "syncservice/sn3DHIDCommunication.dll",
                "syncservice/sn3DHIDCommunicationM.dll", "syncservice/sn3DHIDCommunicationNew.dll",
                "UsbTreeModel.dll", "syncservice/UsbTreeModel.dll", "Sn3DTexCameraHelper.dll",
                "sn3DTurnTable.dll", "syncservice/CommunicateLayer.dll",
                "syncservice/sn3DCommunicateLayerM.dll", "Sn3DDeviceEinStar.dll",
                "Sn3DEinStarController.dll"]:
        if rel in mods:
            r = mods[rel]
            usb[rel] = {
                "sha256": r["sha256"],
                "size": r["size"],
                "timestamp": r["timestamp"],
                "imports": list(r["imports"]),
                "exports": [e["name"] for e in r["exports"]],
                "soft_imported_by": sorted({d["from"] for d in dependents.get(rel, [])
                                            if d["kind"] != "dynamic_ref"}),
                "dynamic_ref_by": sorted({d["from"] for d in dependents.get(rel, [])
                                          if d["kind"] == "dynamic_ref"}),
            }
    json.dump(usb, open(os.path.join(BASE, "usb_stack.json"), "w"), indent=1)

    # ---------- console summary ----------
    print("edges:", len(edges), "dynamic refs:", len(dynamic))
    print("top external DLLs:", external.most_common(12))
    for cat in sorted(cls):
        print(f"{cat:16s} {len(cls[cat]['modules']):4d} modules")
    print("\nUSB-related importers:")
    for rel, rec in sorted(usb.items()):
        print(" ", rel, "| exports:", len(rec["exports"]), "| imported by:", rec["soft_imported_by"][:6])
    print("\ntoolchain:", toolchain["linker_versions"], toolchain["crt_imports"])
    print("rich builds:", list(toolchain["rich_builds"].items())[:10])
    print("pdb roots:", list(toolchain["pdb_roots"].items())[:8])


if __name__ == "__main__":
    main()
