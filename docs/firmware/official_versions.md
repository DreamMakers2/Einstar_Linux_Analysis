# Official EXStar / Einstar firmware versions (download archaeology)

Target: SHINING 3D **Einstar** (model `EinScan10_01`, USB `3267:0003`).
Research date: 2026-11-07. Method: official pages + official CDN only; no installers executed.
Companion files: `sources/web/DOWNLOAD_MANIFEST.tsv`, `sources/web/OFFICIAL_SOURCES.md`.

## Bottom line (verified)

| Question | Answer | Confidence |
|---|---|---|
| Newest **EXStar** for the original Einstar (Windows) | **1.2.2.0** | Verified (official KB "Latest Version" + official CDN file, FileVersion 1.2.2.0) |
| Newest **EXStar** macOS build | **1.2.2.5** (package build; the download is labelled "EXStar_v1.2.2.0 release candidate.pkg") | Verified from `PackageInfo version="1.2.2.5"` |
| Newest **firmware for the Einstar** | **FX3 V2.10 + FPGA V3.7** (`EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_{EN,CH}_IAP.img`) | Verified — byte-identical in 1.0.6.0 (2023), 1.2.2.0 (2024) and the 1.2.2.5 macOS pkg (2024) |
| Any FX3 **≥ V2.14** or FPGA **≥ V3.8**? | **Not found in any official artifact.** The V2.14 / V2.16 / V3.x figures could not be confirmed from any official source. | Verified negative within official sources searched |
| EXStar **1.3.x / 1.4.x / 2.x** for the original Einstar | **Does not exist.** Those numbers belong to **EXStar Hub 2.0.0-xx**, a different product for Einstar 2 / Einstar Rockit. | Verified |
| Official Linux support / Linux SDK | **None.** EXStar is Windows 10/11 x64 + macOS (Apple Silicon) only. The Einstar SDK repos are private. | Verified negative |

> **No third-party mirror was used.** Every byte reported here came from `shining3d.com` CDN
> hosts (`cdnimg.shining3d.com`, `cdnimgcn.shining3d.com`), which are the hosts the official
> support article links to.

## EXStar release chronology (evidence-based)

`docs.shining3d.com/exstar/<version>/en-us/` is the official manual tree; each version directory
that returns HTTP 200 is proof that the version shipped. Probed matrix:

| Version | docs path | Installer evidence |
|---|---|---|
| 1.0.0 | 200 | – |
| 1.0.1 | 200 | – |
| 1.0.2 | 404 | – |
| 1.0.3 | 404 | – |
| 1.0.4 | 200 | – |
| 1.0.5 | 200 | referenced by the official "how to update" article |
| 1.0.6 | 200 | **installer downloaded** (1.0.6.0, CDN Last-Modified 2023-04-18) |
| 1.0.7 | 404 | – |
| 1.1.0 / 1.1.1 | 404 | – |
| 1.2.0 | 200 | – |
| 1.2.1 | 404 | user manual is **V1.2.1.1** (2024-06-21), so a 1.2.1 build existed; no installer located |
| 1.2.2 | 200 | **installer downloaded** (1.2.2.0, CDN Last-Modified 2024-06-28) |
| 1.2.3 / 1.2.4 | 404 | – |
| 1.3.0 / 1.3.1 / 1.4.0 / 2.0.0 | 404 | – |

Timeline of the artifacts actually obtained:

| Date | Artifact | Version | Size | SHA-256 |
|---|---|---|---|---|
| 2023-04-18 (CDN LM) | `EXStar_v1.0.6.0.exe` (Win, NSIS) | 1.0.6.0 | 867,350,160 | `cab66b3488b46e65a31753ace5c1f2feb0d350934de09731d8c88f80da69ab5f` |
| 2024-06-21 | `SHINING 3D Einstar User Manual V1.2.1.1.pdf` | 1.2.1.1 | 14,388,012 | `7750996e471000efc2ed6144212d696f70cf0773280bd4bd6beaa73b554cd3d4` |
| 2024-06-28 (CDN LM) | `EXStar_v1.2.2.0.exe` (Win, NSIS) | 1.2.2.0 | 728,788,984 | `3720a6a341fada78b34ca76e27ca9b03c90feecf092bcb64cb876cc9b54d1a37` |
| 2024-07-29 (CDN LM) | `EXStar_v1.2.2.0 release candidate.pkg` (macOS, XAR) | 1.2.2.5 | 669,736,874 | `7cc5caf6f9f61554f59f6ae567be7b68db900fda35f4c6f43332f0577d827467` |
| 2026-10-28 (CDN LM) | `EXStar Hub_v2.0.0-44.exe` — **different product line** | 2.0.0-44 | 1,652,037,120 | `5d53a993cc37f2b997829c998f1bd53a0c0a65ae69cafc57328a2d530781a071` |
| 2026-11-03 (CDN LM) | `EXStar Hub_v2.0.0-45.exe` — **different product line, newest EXStar-branded build** | 2.0.0-45 | 1,652,038,736 | `08f3e6b6e5023d560c14e5b47216551d5da3ce41276a5e20e71acb15794a0db2` |

**Newest EXStar for the Einstar = 1.2.2.0 (Windows) / 1.2.2.5 (macOS build).**
The official KB article was last edited 2025-06-30 and still names 1.2.2.0 as "Latest Version";
the "Einstar Family Latest Software Download" routing article (edited 2026-02-24) still sends the
original Einstar to that same 1.2.2.0 article, so this was still current in Feb 2026.

## Firmware chronology (verified by hash)

Firmware ships **inside** the EXStar installers under `fabu_UPDATE/Configure/`. There is no
separate firmware download on any official page.

| Firmware | File | SHA-256 | Present in |
|---|---|---|---|
| FX3 **V2.10** + FPGA **V3.7**, EN | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img` (1,311,040 B) | `52346b33c7b0337566d7cfe8d453533c46f18898e4e7f82c1c00c885a8c68c3d` | 1.0.6.0 (2023-04), 1.2.2.0 (2024-06), 1.2.2.5 macOS (2024-07) |
| FX3 **V2.10** + FPGA **V3.7**, CH | `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_CH_IAP.img` (1,311,040 B) | `0c5a41b5bfdc7f50fb6b55794c36fc4b6200ab28709d25b3e4b82d93fce6e7c5` | same three installers |
| FX3 **V1.0** + FPGA **V2.6**, CH (factory/bootloader recovery image) | `EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img` (1,310,720 B) | `067ce53da1cd00c2bab9b61ba3bc1111b631dbb5faac189c1c4a9e8a3b0c9e45` | 1.0.6.0 only, `fabu_UPDATE/writeBootLoader/` |
| Cypress FX3 boot application | `Fx3BootAppGcc.img` (29,876 B) | `2d3f346c190147727a6ed00753ad4d047c975b9603be9ac5b46d450ebb33713e` | 1.0.6.0 only, `fabu_UPDATE/writeBootLoader/` |

`cmp` confirms the V2.10/V3.7 EN and CH images in the downloaded 1.2.2.0 installer are **byte-identical**
to the V2.10 files already held in `sources/firmware/`. The macOS 1.2.2.5 payload carries the same bytes.

So the Einstar firmware did not change between EXStar **1.0.6.0 (Apr 2023)** and EXStar **1.2.2.5 (Jul 2024)**,
and no official artifact newer than V2.10/V3.7 was found in Nov 2026.

### Firmware for *other* Einstar-family devices (not usable on EinScan10_01)

EXStar Hub **2.0.0-44** and **2.0.0-45** (Einstar 2 / Einstar Rockit only) both contain the same set:

* `fabu_UPDATE/Configure/tx2_1.63_K0001_20261023_223247_update_s.img` — 184,262,656 B, built 2026-10-23 (TX2 platform).
* `fabu_UPDATE/Configure/tx5_FWPKG_H2_V1.0.1/` — `tx5_H1_MV15.8.0.tar.gz`, `tx5_H2_AV1.0.1.tar.gz`,
  `tx5_H2_FSV0.0.5.tar.gz`, `tx5_H2_FV2.0.4.tar.gz`, `tx5_H2_KV0.0.2.tar.gz`, `tx5_H2_UV0.0.1.tar.gz` (TX5 platform).

These are a **different hardware generation**; no `EinScan10_01` image exists in either package.
Do not attempt to flash them to an Einstar.

### Lead (worth a follow-up, does not change the firmware conclusion)

EXStar Hub 2.x still carries the Einstar/E10 device module: `app_info.ini` says
`modelcode=einstar`, `Sn3DDeviceEinStar.dll` lists the device names `Einstar`, `Einstar 2`,
`Einstar EZ`, `Einstar Rockit` and `einscan10`, and `AppUi.dll`, `PrestartCheck.dll` and
`ScanPage_E7.dll` contain the literal string `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN`
(a default/expected `firmwareVersion` value, next to `firmwareVersion` and `device_info_manager_e7`).

Interpretation: Hub 2.x **recognises** an original Einstar and expects firmware
`V2.10 / FPGA V3.7`, but it ships **no** `EinScan10_01` firmware. So even the newest
EXStar-branded software (2026-11-03) treats V2.10/V3.7 as the current Einstar firmware.
Whether Hub 2.x can actually drive an original Einstar is **not documented** and was not tested
(the vendor routes Einstar → EXStar 1.2.2.0 in the family article).

## What is verified vs unverified

**Verified (hash + official page + official CDN):**
* 1.2.2.0 is the current Windows EXStar for the Einstar; 1.2.2.5 is the macOS build.
* The firmware bundled in every obtainable EXStar 1.x installer is FX3 V2.10 / FPGA V3.7.
* EXStar ships the Cypress FX3 driver (`cyusb3.{inf,sys,cat}`) and `SH3DU3Driver`, extracted to
  `sources/firmware/official_downloads/drivers_fx3_usb/`.
* The official `SH3DU3DRIVER.inf` maps **`USB\VID_3267&PID_0003` → `SHINING3D_E10 = "Einstar"`**,
  confirming the target device identity (E10 ↔ `EinScan10_01`).
* No Linux build, no Linux SDK, no public Einstar SDK repository.

**Unverified / not found:**
* Any FX3 firmware ≥ V2.14, ≥ V2.16 or any FPGA ≥ V3.8 — no official artifact, KB article, release
  note, doc page or forum post shows such a version for `EinScan10_01`.
* EXStar 1.2.1 (only its user manual V1.2.1.1 was found), 1.2.3, 1.3.x, 1.4.x, 2.x for the Einstar.
* A separate, standalone "Einstar firmware" package — none exists; firmware is always bundled and
  applied through the in-app prompt (documented in the manual and the `upgrade` doc page).
* The redirect target `https://www.einstar.com/zh-cn/pages/download-center-einstar` returned HTTP 429
  (rate-limited) during testing; the zh-CN download page could not be inspected.

## Implications for the Linux project

* There is no newer official firmware to obtain — V2.10/V3.7 is terminal for this model as of 2026-11.
* Because firmware ships only inside a 700 MB Windows/macOS installer, any independent firmware
  packaging effort must extract `fabu_UPDATE/Configure/*.img` from the installer (paths are stable
  across 1.0.6.0 → 1.2.2.5).
* `Fx3BootAppGcc.img` + `EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img` are the recovery/bootloader
  path and are only present in 1.0.6.0.
