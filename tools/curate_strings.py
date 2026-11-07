#!/usr/bin/env python3
"""Curate high-signal strings from the 8 key module dumps.

Inputs : analysis/strings/<base>.sections.txt  (ASCII + decoded UTF-16, data sections)
Output : analysis/strings/interesting_strings.md
"""
from __future__ import annotations

import os
import re

STR = "<repo>/analysis/strings"
OUT = os.path.join(STR, "interesting_strings.md")
MODULES = [
    "Sn3DDeviceEinStar",
    "Sn3DEinStarController",
    "FirmwareupdateUI",
    "SiUSBXp",
    "Sn3DCalibDevFile",
    "Sn3DCalibrateLoader",
    "Sn3DCameraCalibHelper",
    "softwareUpgrade",
]

CATS = [
    ("Firmware / FX3 / bootloader / IAP / FPGA", re.compile(
        r"firmware|bootload|fx3|\biap\b|fpga|\.img|updatefirmware|upgrade|flash", re.I), 45),
    ("Version identifiers", re.compile(r"\b\d+\.\d+\.\d+(\.\d+)?\b"), 30),
    ("Build provenance (PDB / source paths)", re.compile(
        r"jenkins|^[A-Z]:\\|/src/|\\.pdb", re.I), 30),
    ("Calibration / CCF / camera parameters", re.compile(
        r"ccf|\bccm\b|calib|whit(e)?balan|dark|exposure|gain|intrinsic|distort", re.I), 45),
    ("USB / HID / device transport", re.compile(
        r"\busb\b|\bhid\b|cyusb|siusbxp|setupdi|createfile|deviceiocontrol|pipe|endpoint|"
        r"serial|vid_|pid_|driver|\\\\.\\", re.I), 45),
    ("Crypto / licensing / anti-tamper", re.compile(
        r"hasp|dongle|licen|crypto|aes|3des|rsa|sha256|signature|tamper|machine|encrypt", re.I), 35),
    ("Error and status messages", re.compile(
        r"error|fail|invalid|timeout|exception|retry|disconnect|not found", re.I), 30),
]


def load(base):
    path = os.path.join(STR, f"{base}.sections.txt")
    if not os.path.exists(path):
        return [], []
    ascii_part, utf16_part = [], []
    mode = ascii_part
    for line in open(path, encoding="utf-8"):
        if line.startswith("# UTF-16LE"):
            mode = utf16_part
            continue
        if line.startswith("#"):
            continue
        line = line.rstrip("\n")
        if line.strip():
            mode.append(line)
    return ascii_part, utf16_part


def interesting(s):
    """Drop mangled symbols, opcode soup and boilerplate."""
    if s.startswith(("?", ".?AV", ".?AU", "??")):
        return False
    if re.fullmatch(r"[A-Za-z0-9_$.@?`^\\/|{}\[\]()=+,;:!<>%&*#~'\" -]{0,4}", s):
        return False
    letters = sum(c.isalpha() for c in s)
    if letters < 4:
        return False
    if len(s) > 6 and letters / len(s) < 0.45:
        return False
    return True


def main():
    data = {m: load(m) for m in MODULES}
    lines = []
    lines.append("# Curated high-signal strings — 8 key EXStar modules\n")
    lines.append(
        "Static extraction only. ASCII runs >=5 and UTF-16LE runs >=4 taken from data "
        "sections (.rdata/.data/.rsrc/.qtmetad) by `tools/strings_sections.py`; "
        "raw whole-file dumps are `<module>.strings.txt` (GNU strings 2.47, `-a -n 5` and "
        "`-a -e l -n 5`). Mangled C++ symbols are excluded here.\n"
    )
    lines.append("## Module hashes\n")
    lines.append("| module | sha256 |\n|---|---|")
    import hashlib
    for m in MODULES:
        ext = ".exe" if m in ("FirmwareupdateUI", "softwareUpgrade") else ".dll"
        p = f"<repo>/sources/windows/EXStar/{m}{ext}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"| {m} | `{h}` |")
    lines.append("")

    for title, rx, cap in CATS:
        lines.append(f"## {title}\n")
        rows, total = [], 0
        for m in MODULES:
            seen = set()
            for s in data[m][0] + data[m][1]:
                if s in seen or not interesting(s) or not rx.search(s):
                    continue
                seen.add(s)
                total += 1
                if len(rows) < cap:
                    rows.append((m, s[:240]))
        lines.append(f"Matches (deduplicated, pre-cap): {total}\n")
        lines.append("| module | string |\n|---|---|")
        for m, s in rows:
            s = s.replace("|", "\\|")
            lines.append(f"| {m} | `{s}` |")
        lines.append("")

    # targeted facts
    lines.append("## Targeted observations\n")
    obs = []
    dev = data["Sn3DDeviceEinStar"][0] + data["Sn3DDeviceEinStar"][1]
    ctl = data["Sn3DEinStarController"][0] + data["Sn3DEinStarController"][1]
    fw = data["FirmwareupdateUI"][0] + data["FirmwareupdateUI"][1]
    cal = data["Sn3DCalibDevFile"][0] + data["Sn3DCalibDevFile"][1]
    for needle, where, note in [
        ("/Fx3Bootloader.exe", dev, "FX3 bootloader helper launched by the device layer"),
        ("*.img", dev, "firmware image filter"),
        ("_JAP.img", dev, "Japan-market firmware variant selector"),
        ("BOOTLOADERSUBDIR", dev, "static member naming the bootloader sub-directory"),
        ("FirmUpdate/writeBootLoader", dev, "firmware-update command/topic name"),
        ("/FirmUpdate/Configure", ctl, "firmware-update configuration command"),
        ("queryUpdateProgress", ctl, "update progress polling"),
        ("ISFORCEFWUPDATE", dev, "force-firmware-update flag accessor"),
        ("FirmwareLanguage", dev, "firmware language selector (explains _JAP variant)"),
        ("CameraSerialL", ctl, "left camera serial (stereo pair)"),
        ("getDeviceTemperature", ctl, "device temperature telemetry"),
        ("setDeviceBrightness", ctl, "illumination/brightness control"),
        ("setReadImgScanMode", ctl, "image read-out / scan mode control"),
        ("'UpdateFirmware'", fw, "QML command sent to the firmware service"),
        ("filePath", fw, "QML passes $device.firmwareFilePath to the update command"),
        ("2CCFF,", fw, "CCF-related constant/token in the firmware UI"),
        ("readDevDataToFileCmd", cal, "calibration device-data read-out command"),
        ("handleNotifyIsCablliratedCmd", cal, "calibration-state notification handler"),
    ]:
        hit = next((s for s in where if needle in s), None)
        if hit:
            mod = ("Sn3DDeviceEinStar" if where is dev else
                   "Sn3DEinStarController" if where is ctl else
                   "FirmwareupdateUI" if where is fw else "Sn3DCalibDevFile")
            obs.append((mod, needle, note))
    lines.append("| module | literal | meaning (inferred) |\n|---|---|---|")
    for m, needle, note in obs:
        lines.append(f"| {m} | `{needle}` | {note} |")
    lines.append("")

    lines.append("## Targeted pattern sweep\n")
    sweep = [
        ("firmware", re.compile(r"firmware", re.I)),
        ("FPGA", re.compile(r"\bfpga\b", re.I)),
        ("IAP", re.compile(r"\biap\b", re.I)),
        ("semver x.y.z", re.compile(r"\b\d+\.\d+\.\d+\b")),
        ("Cypress-style 'CY…' token", re.compile(r"\bCY[A-Za-z0-9_]{2,}\b")),
        ("hex literal 0x…", re.compile(r"0x[0-9a-fA-F]{4,8}")),
        (".img", re.compile(r"\.img\b", re.I)),
        ("ccf", re.compile(r"ccf", re.I)),
        ("calib", re.compile(r"calib", re.I)),
        ("einstar", re.compile(r"einstar", re.I)),
        ("FX3", re.compile(r"fx3", re.I)),
        ("USB", re.compile(r"usb", re.I)),
        ("endpoint / EPn / bulk / pipe", re.compile(r"\bep[0-9]{1,2}\b|endpoint|bulk|\bpipe\b", re.I)),
        ("error code", re.compile(r"error ?code|softwareError", re.I)),
    ]
    lines.append("| pattern | hits | examples (module: literal) |\n|---|---|---|")
    for label, rx in sweep:
        hits, ex = 0, []
        for m in MODULES:
            for s2 in data[m][0] + data[m][1]:
                if rx.search(s2) and not s2.startswith(("?", ".?AV", ".?AU", "??")):
                    hits += 1
                    if len(ex) < 4:
                        ex.append(f"{m}: `{s2[:90]}`")
        lines.append(f"| {label} | {hits} | {'; '.join(ex) if ex else '**none**'} |")
    lines.append("")
    lines.append("Interpretation: there is **no Cypress API literal, no FPGA/IAP token and no USB "
                 "endpoint/bulk/pipe name** in these eight modules. The FX3 connection is visible "
                 "only through `Fx3Bootloader.exe`, `*.img`/`_JAP.img` and `FirmwareLanguage`. "
                 "`0x00000013` sits next to `softwareError`/`errorCode` in Sn3DDeviceEinStar and is "
                 "the only hex constant. Semver matches are build/PDB paths or dependency "
                 "filenames, not product versions.\n")

    lines.append("## Chinese (CJK) strings — result\n")
    lines.append(
        "No genuine Chinese string literals were found in these eight binaries. "
        "`*.cjk.txt` candidates are all decoding artefacts: (a) MSVC 0xCC stack-fill "
        "patterns (`锰烫烫烫…`), (b) ASCII C++ mangled names read as UTF-16 code units "
        "(`䵑灡` = `QMap`), (c) embedded font glyph/name tables in FirmwareupdateUI.exe. "
        "Localisation is data-driven instead: 21 Qt translation catalogues under "
        "`language_qm/` (`lang_en_US.qm`, `Language_loka_*.qm`). A GBK sweep (lead byte "
        "0x81-0xFE, trail 0x40-0xFE, >=60% CJK) likewise returned only 0xCC fill noise.\n"
    )

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
