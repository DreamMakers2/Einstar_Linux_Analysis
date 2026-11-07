# Windows module dependency and API map — SHINING 3D EXStar (Einstar)

*Generated from static analysis of the read-only installation at* `<EXStar-install>/Shining3d/EXStar`  
*Inventory source: `analysis/gnu/pe_inventory.json` (generated 2026-11-07T02:10:32.322221+00:00; pefile 2024.8.26, Python 3.14.9).*

**No binary was executed.** All facts below are read-only PE parsing plus byte/string inspection; role statements are hypotheses unless an evidence column says otherwise.

---

## 0. Executive summary

- **741 PE files** (36 EXE, 705 DLL) were enumerated across the whole tree, including subdirectories; **none exceeded the 200 MiB skip threshold**, so nothing was skipped. **0 are .NET** — every module is native x64 code (11 32-bit stragglers, AMD64: 730).
- **730 of 741** carry an MSVC Rich header and **385** still reference a PDB path — the build tree layout (`E:\Jenkins\workspace\…`, `D:\qt_build`, `F:\SyncServiceDev`) is recoverable.
- The product is a **Qt 5.15.10 / MSVC 19.16 (VS2017 15.9) x64** application with a static-import plug-in architecture (Qt plug-in metadata + a vendor matter/message bus), plus OpenCV 3.4.8, OpenSceneGraph 3.6.3, Ceres, OpenMesh, lib3MF, CUDA 10.2.89 and MKL.
- **USB is split across three transports**: a Cypress **FX3 / CyUSB3** driver package, a Shining3D **SH3DU3Driver** (USB VID `3267`) transport DLL pair (`CommunicateLayer`), and **HID** (`sn3DHIDCommunication`) for control-class devices; the legacy **SiUSBXp** (Silicon Labs USBXpress) path is used only by the turntable.
- Scanner I/O is **not** in the UI process: `Sn3DDeviceEinStar.dll` spawns `syncservice/SnSyncService.exe` and talks to it over the **QtTunnel** shared-memory/IPC bus; the service side owns the USB handles.
- **Calibration** is a self-contained plug-in family (7 requested modules, 387 exported symbols) with CCF/CCM/dark/white-balance serialisation and a JSON/XML-driven procedure loader; `ccf/200x150/{Left,Right,Tex}CCF.txt` are the matching per-unit data files.
- **Anti-tamper is layered**: Crypto++/AES-3DES data protection (`Sn3DCrypto`), HASP dongle + USB machine binding (`Sn3DSecurity` + `hasp_*`), SQLCipher config DBs and an encrypted tunnel (`tunnel_encrypt`).

## 1. Method and evidence discipline

| step | tool | output |
|---|---|---|
| enumerate + hash + PE headers/imports/exports | `tools/pe_inventory.py` (pefile 2024.8.26) | `analysis/gnu/pe_inventory.json` (66 MB) |
| dependency graph, classification, export flagging | `tools/analyze_pe.py` | `module_dependencies.json`, `classification.json`, `calibration_api.json`, `flagged_exports.json`, `usb_stack.json`, `toolchain.json` |
| strings (ASCII + UTF-16LE) | GNU strings 2.47 via `tools/extract_strings.sh` | `analysis/strings/<module>.strings.txt` |
| section-aware / CJK-aware strings | `tools/strings_sections.py` | `analysis/strings/<module>.sections.txt`, `<module>.cjk.txt` |
| curated findings | `tools/curate_strings.py` | `analysis/strings/interesting_strings.md` |
| report | `tools/build_report.py` | this file |

Evidence levels used in this document: **observed** (bytes/headers parsed directly), **derived** (computed from observed data, e.g. dependency edges), **hypothesis** (interpretation of names/behaviour, explicitly labelled).

## 2. Inventory

| metric | value |
|---|---|
| PE files | 741 |
| EXE / DLL | 36 / 705 |
| machine | AMD64: 730, I386: 11 |
| .NET (CLR directory) | 0 |
| native | 741 |
| with Rich header | 730 |
| with PDB path | 385 |
| parse errors | 0 |
| >200 MiB skipped | 0 |

Directory distribution (top):

| directory | PE files |
|---|---|
| `.` | 280 |
| `Release/osgPlugins-3.6.3` | 85 |
| `osgPlugins-3.6.3` | 85 |
| `syncservice` | 35 |
| `syncservice/device` | 26 |
| `passport/openweburl` | 21 |
| `imageformats` | 20 |
| `Release` | 14 |
| `Resource/runtime` | 13 |
| `platforms` | 10 |
| `passport/openweburl/imageformats` | 9 |
| `syncservice/imageformats` | 9 |

## 3. Toolchain and SDKs

| component | version | evidence (observed) |
|---|---|---|
| MSVC toolset | **19.16 / VS2017 15.9** (linker 14.16) | linker version in 587/741 modules; `MSVCP140`+`VCRUNTIME140`+UCRT `api-ms-win-crt-*` |
| MSVC (other) | 19.11 (54), 18.00/VS2013 (37), 17.00 (9), 16.00 (7), 19.29/19.32 (a few) | linker-version histogram in `toolchain.json` |
| Rich-header builds | 30729, 26706, 26213, 27040, 27412, 27027 … | decoded Rich entries; 26xxx–27xxx = VS2017-era toolsets, 30729 = VS2008 SP1 (legacy SDK DLLs) |
| Qt | **5.15.10** shared, x86_64, built by MSVC 2017 | literal `Qt 5.15.10 (x86_64-little_endian-llp64 shared (dynamic) release build; by MSVC 2017)` in `Qt5Core.dll`; PDB root `D:\qt_build` in 62 Qt plug-ins |
| Boost | **1.72**, `vc141`, x64 (one `-gd` debug lib) | filenames `boost_*-vc141-mt-x64-1_72.dll` |
| OpenCV | **3.4.8**, built with MSVC 19.14.26428 (VS2017 15.7 Enterprise) | `General configuration for OpenCV 3.4.8` banner incl. compiler path in `opencv_core348.dll` |
| OpenSceneGraph | **3.6.3** | PDB `E:\OSG\OpenSceneGraph-3.6.3\…\src\osg\Uniform.cpp`; `osgPlugins-3.6.3/`; `osg158-*` SOVERSION |
| CUDA | **10.2.89** | `NVIDIA CUDA 10.2.89 Runtime` in `cudart64_102.dll`; `cusolver64_10`, `nppc64_10`, `nppig64_10` |
| Intel MKL | present (`mkl_rt`, `mkl_core`, `mkl_def`, `mkl_sequential`, `mkl_tbb_thread`) | file names + exports |
| Ceres Solver / Eigen / OpenMesh / lib3MF / TBB | present (Eigen header-only) | `ceres.dll` symbols; `Eigen::Matrix` in mangled names of `algorithm*.dll`; `OpenMeshCore/Tools`; `lib3MF` `NMR::`; `tbb.dll` |
| OpenSSL | **1.1.1d** (`libcrypto-1_1-x64`/`libssl-1_1-x64`, 2019-09-10) **and** legacy **1.0.2q** (`LIBEAY32`/`SSLEAY32`, 2018-11-20) | version banners inside the DLLs (`OpenSSL 1.1.1d  10 Sep 2019`, `OpenSSL 1.0.2q  20 Nov 2018`) |
| Crypto++ | `cryptopp.dll` (4200 exports) | exports `CryptoPP::` symbols |
| HASP/SafeNet | `hasp_windows_x64_105562.dll`, `haspvlib_105562.dll` | Aladdin HASP strings + `hasp_rt.exe` reference |
| SQLCipher | `sqlitecipher.dll` | exported sqlite API, encrypted `config/app1.db` |
| 7-Zip | **21.07** (2021-12-26) (`7z.dll`, `7z.exe`) | `7-Zip 21.07 (x64) : Copyright (c) 1999-2021 Igor Pavlov` |

**Statically linked vs shared (derived from import tables):**

- **Shared, shipped as DLLs:** Qt5, OSG, OpenCV, Ceres, OpenMesh, lib3MF, TBB, MKL, CUDA runtime, Boost, OpenSSL, Crypto++, HASP, SQLCipher — each has a matching `*.dll` in the tree and appears in import tables.
- **Statically linked inside vendor DLLs:** Eigen (header-only — only symbol names such as `Eigen::Matrix<float,3,1,…>` inside `algorithm*.dll`/`common*.dll`, no Eigen DLL), glog (the `glogc.dll` custom build is shared but most vendor modules also embed the vendor logging wrapper), and the Shining3D geometry core (`common.dll` is shared).
- **Debug/release mixes:** `boost_serialization-…-gd` and `networktunneld.dll`/`qttunnel.3.0.9d.dll` are debug builds shipped alongside release ones — a packaging smell, not a functional finding.

## 4. Runtime architecture (hypothesis built from imports, PDB names and strings)

```
EXStar.exe ──► AppCommon_E7.dll / AppUi.dll ──► Sn3DProcessPlugin.dll
     │                                             (spawns worker processes)
     ├──► Sn3DEinStarController.dll  (Qt plug-in)
     │         └──► Sn3DDeviceEinStar.dll  (device abstraction, 187 exports)
     │                   ├── QProcess ──► syncservice/SnSyncService.exe
     │                   │        └── SnSyncCore.dll (DeviceController)
     │                   │               ├── syncservice/device/<model>.*.dll
     │                   │               ├── CommunicateLayer.dll ──► SH3DU3Driver.sys / cyusb3.sys
     │                   │               └── sn3DHIDCommunication.dll ──► hid.dll + SetupAPI
     │                   ├── Sn3DImageQueue.dll / Sn3DTexture.dll (frame hand-off)
     │                   └── QProcess ──► Fx3Bootloader.exe + *.img   (firmware update)
     ├──► Sn3DScanController.dll ──► Sn3DRealtimeScan.dll (SLAM/fusion)
     │         └── Sn3DRegistration.dll, Sn3DTextureSlam.dll, OpenCV, TBB
     └──► CalibrationPage_EA.dll ──► Sn3DCalibrate* / Sn3DCameraCalibHelper / Sn3DCalib*
```

The Qt plug-in metadata (`qt_plugin_instance` / `qt_plugin_query_metadata` exports) marks the loadable modules; `Sn3DBox.dll` + `qttunnel.3.0.9.dll` provide the vendor's topic/message bus used between processes (`SnSharedBlock.dll` supplies the shared memory).

## 5. USB / HID stack

### 5.1 Kernel drivers shipped with the product

| package | driver | hardware IDs | notes (from INF, observed) |
|---|---|---|---|
| `Resource/driver/FX3/` | `cyusb3.sys` (Cypress CyUSB3, `DriverVer=08/21/2014,1.2.3.10`) | Cypress VID `04B4` PIDs `00F0/00F1/00F3/4720/00B0/00BC/FFF1-3/0053/0082/0095/1004/8613/1003/6823/4617/4611…` | `cyusb3.inf` + `CYUSB3.cat` + `WdfCoinstaller01011.dll`; `test.bat` runs `pnputil /add-driver cyusb3.inf`. FX3 is the Einstar camera's USB3 controller. |
| `Resource/driver/UEC/` | `SH3DU3Driver.sys` (Shining3D, `DriverVer 03/02/2022,16.25.20.115`) | **VID `3267`** PIDs `0001-0009` (scanners: SHININGSCANNER / SHINING3D_UE / E10 / FACE), `0200-0205` (cameras) | custom device class `Shining3D` GUID `{6A0F6C25-2C5E-4F49-A338-556699A7BA8E}`; `SHCtl.exe` start/stop + `PsExec64.exe` used by `Install.bat` |

### 5.2 User-mode transports

| layer | module | API evidence | used by |
|---|---|---|---|
| HID | `sn3DHIDCommunication.dll` (2020-07, VS2013, `msvcr120`) | imports `HID.DLL` (`HidD_GetHidGuid`) + `SETUPAPI.dll` (`SetupDiGetClassDevsW/EnumDeviceInterfaces/GetDeviceInterfaceDetailW`); exports `sn3DInitHidDev` (6 overloads), `sn3DExitHidDev`, `sn3DReadData`, `sn3DReadDataOverlapped`, `sn3DSendData`, `sn3DGetReport`, `sn3DSetReport`, `sn3DClearBuffer`, `sn3DEnumSpecifiedUSBDevice` | `sn3DTurnTable.dll`, `syncservice/device/{einscan3, dlp3100projector, freescante25, ty1080projector, tylaser}.dll`; plus `sn3DHIDCommunicationM/New` variants for `einscan7`, `freescan`, `sesp` |
| Vendor USB3 | `syncservice/CommunicateLayer.dll` (29 exports) and `syncservice/sn3DCommunicateLayerM.dll` (30 exports) | `initializeSystem(const GUID&)`, `attachDevice(serial)`, `detachDevice`, `getDevicePipeAddressList`, `getMaxCommandSize`, `getImageDataUnitSize`, `driverStreamSwitch`, `receiveBulkReply`, `commandChannelLock`, `dataStreamlock`, `discardDriverFrameCaches`, `getOnlineDeviceSerial(Ex)` | sync-service device plug-ins (`Einscan3H2`, `SHNDetect`, `einscan10`, `fs3scan`, `sp3scan`, `uescanwl`, `uescan`) |
| USB topology | `UsbTreeModel.dll` | imports `SETUPAPI` + `CM_Get_Device_IDW`/`CM_Get_Parent` + `DeviceIoControl`; exports `USBTREE::EnumAllHCD`, `EraseList`, `GetSpecifyDeviceTreePath`, `ResetDevice` | `libSn3DNavigation.dll`, `syncservice/logic/3DDigitalSyncInterface.2.0.0.dll` |
| USBXpress (legacy) | `SiUSBXp.dll` — Silicon Labs **USBXpress 3.4.1.0**, 2011, VS2010 | exports `SI_Open/SI_Close/SI_Read/SI_Write/SI_FlushBuffers/SI_DeviceIOControl/SI_ResetDevice/SI_SetBaudRate/…` (24); imports `SETUPAPI` + `DeviceIoControl` | **only** `sn3DTurnTable.dll` (imports by ordinal 1,3,4,6,7,9,10,12) |

The GUID passed to `CommunicateLayer::initializeSystem` is not stored as a printable literal, so the mapping of that transport to `SH3DU3Driver.sys` vs `cyusb3.sys` is a **hypothesis** inferred from the shipped driver packages plus the pipe/serial API shape; what is observed is that these are the only two USB drivers shipped and that the vendor driver claims VID `3267`.

**Absent** (verified by import scan of all 741 modules): WinUSB (`WinUsb_*`), libusb (`libusb_init`), CyAPI/CyUSB user-mode DLLs, and any `\\.\` device-path literal. The only `libusb` string in the tree lives inside the Chromium blob `passport/openweburl/Qt5WebEngineCore.dll` and is unrelated. USB access therefore goes through the two vendor drivers above and HID.

### 5.3 Exact call chain (observed imports + strings)

```
turntable:  Sn3DTurntableController.dll -> sn3DTurnTable.dll -> SiUSBXp.dll -> SETUPAPI/DeviceIoControl -> usbser/USBXpress
                                       -> sn3DHIDCommunication.dll -> HID.DLL + SETUPAPI -> hidclass
scanner:    EXStar.exe -> Sn3DEinStarController.dll -> Sn3DDeviceEinStar.dll
                          -> QProcess -> syncservice/SnSyncService.exe -> SnSyncCore.dll
                             -> syncservice/device/<model>.dll -> CommunicateLayer.dll
                                -> DeviceIoControl(GUID) -> SH3DU3Driver.sys (VID_3267) | cyusb3.sys (FX3)
firmware:   Sn3DDeviceEinStar.dll -> QProcess -> Fx3Bootloader.exe (not shipped) + *.img / _JAP.img
```

## 6. Owners of scanner communication

Full export lists are in `analysis/gnu/usb_stack.json` and `flagged_exports.json`; the keyword-flagged subset (firmware/update/IAP/FPGA/USB/camera/image/calibration/CCF) is summarised here.

| module | sha256 | exports | what its exports say |
|---|---|---|---|
| `Sn3DDeviceEinStar.dll` | `d419882dd957` | 187 | `??0Sn3DConfig@@QEAA@AEBV0@@Z`, `??0Sn3DConfig@@QEAA@XZ`, `??0Sn3DConfigSQLite@@QEAA@XZ`, `??0Sn3DDeviceBase@@QEAA@PEAVQObject@@HAEBVQString@@@Z`, `??0Sn3DDeviceBaseData@@IEAA@AEAVSn3DDeviceBaseDataPrivate@@@Z`, `??0Sn3DDeviceBaseData@@QEAA@XZ` … |
| `Sn3DEinStarController.dll` | `eda4caa4b215` | 2 | `qt_plugin_instance`, `qt_plugin_query_metadata` |
| `sn3DHIDCommunication.dll` | `27b99f62d782` | 15 | `?sn3DClearBuffer@sn3DHIDCommunication@@YAXPEAX@Z`, `?sn3DEnumSpecifiedUSBDevice@sn3DHIDCommunication@@YA_NAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@0AEAH@Z`, `?sn3DExitHidDev@sn3DHIDCommunication@@YA_NPEAX@Z`, `?sn3DGetReport@sn3DHIDCommunication@@YA_NPEAXPEAEAEAH@Z`, `?sn3DInitHidDev@sn3DHIDCommunication@@YA_NAEAPEAX@Z`, `?sn3DInitHidDev@sn3DHIDCommunication@@YA_NAEAPEAXPEBD11@Z` … |
| `SiUSBXp.dll` | `33958f5ae581` | 24 | `?SI_ResetDevice@@YAHPEAX@Z`, `SI_CheckRXQueue`, `SI_Close`, `SI_DeviceIOControl`, `SI_FlushBuffers`, `SI_GetDLLVersion` … |
| `UsbTreeModel.dll` | `f1468c27c5ce` | 4 | `?EnumAllHCD@USBTREE@@YA_NXZ`, `?EraseList@USBTREE@@YAXXZ`, `?GetSpecifyDeviceTreePath@USBTREE@@YA_NKKAEAV?$vector@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@V?$allocator@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@2@@std@@@Z`, `?ResetDevice@USBTREE@@YA_NKK@Z` |

### 6.1 Device-layer exports that matter (Sn3DDeviceEinStar.dll, 187 total)

```
?BOOTLOADERSUBDIR@Sn3DDeviceBase@@0QEADEA
?ISBLOCK@Sn3DDeviceBaseData@@2QEADEA
?ISFORCEFWUPDATE@Sn3DDeviceBaseData@@2QEADEA
?ISFWUPDATE@Sn3DDeviceBase@@2QEADEA
?PARALIST@Sn3DDeviceBase@@2QEADEA
?captureImage@Sn3DDeviceEinStar@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?captureRGBImage@Sn3DDeviceBase@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?checkProcessExistence@Sn3DDeviceBase@@AEAA_NVQString@@@Z
?closeDevice@Sn3DDeviceBase@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?closeDevice@Sn3DDeviceEinStar@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?deviceTemperature@Sn3DDeviceBaseData@@QEAAXN@Z
?errorCode@Sn3DDeviceBase@@IEAAXAEBVQString@@0H@Z
?execute@Sn3DDeviceBase@@QEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?executeFunc@Sn3DDeviceBase@@IEAAXAEBVQString@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?executeResult@Sn3DDeviceBase@@QEAA?AU?$QPair@IVQVariant@@@@AEBVQString@@AEBV?$QList@VQVariant@@@@@Z
?executeResultFWUpd@Sn3DDeviceBase@@QEAA?AU?$QPair@IVQVariant@@@@AEBVQString@@AEBV?$QList@VQVariant@@@@@Z
?exexuteSnSync@Sn3DDeviceBase@@UEAA?AU?$QPair@IVQVariant@@@@AEBVQString@@AEBV?$QList@VQVariant@@@@@Z
?finalizeSharedBlockMemory@Sn3DDeviceBase@@AEAAXXZ
?firstConnect@Sn3DEinStarDeviceData@@IEBA_NXZ
?getBrightnessSetp@Sn3DEinStarDeviceData@@QEAAHXZ
?getCaliData@Sn3DDeviceEinStar@@IEAAXXZ
?getCameraCount@Sn3DDeviceBaseData@@QEAAHXZ
?getCameraSNList@Sn3DDeviceBaseData@@QEAA?AV?$QList@VQVariant@@@@XZ
?getCameraSeries@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getCamsHeight@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getCamsWidth@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getCurrentExp@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getCurrentGain@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getDeviceActiveData@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getDeviceCount@Sn3DDeviceEinStar@@IEAAXXZ
?getDeviceInnerData@Sn3DDeviceBaseData@@QEAA?AVQByteArray@@XZ
?getDeviceName@Sn3DDeviceBaseData@@QEAA?AVQString@@XZ
?getDeviceSerial@Sn3DDeviceBaseData@@QEAA?AVQString@@XZ
?getDeviceSeries@Sn3DDeviceBaseData@@UEAAHXZ
?getDeviceSubType@Sn3DDeviceBaseData@@UEAAHXZ
?getDeviceType@Sn3DDeviceBaseData@@QEAA?AVQString@@XZ
?getEinStarCaliData@Sn3DEinStarDeviceData@@QEAA_NAEAUtagDevData@@@Z
?getFiramVersion@Sn3DDeviceBaseData@@QEAA?AVQString@@XZ
?getFirmwareFilePath@Sn3DDeviceBaseData@@QEAA?AVQString@@XZ
?getFirmwareVersion@Sn3DDeviceEinStar@@IEAAXXZ
?getHasTexCam@Sn3DDeviceBaseData@@UEAA_NXZ
?getImageFinish@Sn3DDeviceBase@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?getIsOfflineEnter@Sn3DDeviceBaseData@@QEAA_NXZ
?getIsOfflineEnterReconnect@Sn3DDeviceBaseData@@QEAA_NXZ
?getLaserBrightness@Sn3DDeviceBaseData@@QEAAHXZ
?getLedBrightness@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getLeftCameraSN@Sn3DDeviceBaseData@@QEAA?AVQString@@XZ
?getParaCCMCloseLed@Sn3DEinStarDeviceData@@IEAA?AVQString@@XZ
?getParaCCMOpenLed@Sn3DEinStarDeviceData@@IEAA?AVQString@@XZ
?getParaDarkCloseLed@Sn3DEinStarDeviceData@@IEAA?AVQString@@XZ
?getParaDarkOpenLed@Sn3DEinStarDeviceData@@IEAA?AVQString@@XZ
?getProperty@Sn3DDeviceBase@@QEAAXAEBV?$QMap@VQString@@VQVariant@@@@AEAV2@@Z
?getProperty@Sn3DDeviceBase@@QEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?getPropertyRet@Sn3DDeviceBaseData@@MEAAXV?$QMap@VQString@@VQVariant@@@@@Z
?getPropertyRet@Sn3DEinStarDeviceData@@MEAAXV?$QMap@VQString@@VQVariant@@@@@Z
?getRecoverFile@Sn3DDeviceBase@@AEAA?AVQString@@XZ
?getResolution@Sn3DDeviceBaseData@@QEAA?AV?$QMap@VQString@@VQVariant@@@@XZ
?getSerial@Sn3DDeviceEinStar@@IEAAXXZ
?getWbDataCloseLed@Sn3DEinStarDeviceData@@IEAA?AVQString@@XZ
?getWbDataOpenLed@Sn3DEinStarDeviceData@@IEAA?AVQString@@XZ
?imageStreamcallback@Sn3DDeviceBase@@UEAAXVQString@@0V?$QList@VQVariant@@@@0@Z
?imageStreamcallback@Sn3DDeviceEinStar@@UEAAXVQString@@0V?$QList@VQVariant@@@@0@Z
?inFirmUpdate@Sn3DDeviceBaseData@@UEAA_NXZ
?isAutoReconnectClosed@Sn3DEinStarDeviceData@@IEAA_NXZ
?isBlock@Sn3DDeviceBaseData@@UEAA_NXZ
?isBlockDisableTrigger@Sn3DDeviceBase@@AEAA_NAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?isCalibrateInfoCorrect@Sn3DDeviceEinStar@@IEAAXXZ
?isForceFWUpdate@Sn3DDeviceBaseData@@UEAA_NXZ
?isOffLine@Sn3DDeviceBaseData@@QEAA_NXZ
?messageCallback@Sn3DDeviceBase@@UEAAXVQString@@0V?$QList@VQVariant@@@@0@Z
?messageCallback@Sn3DDeviceEinStar@@UEAAXVQString@@0V?$QList@VQVariant@@@@0@Z
?metaObject@Sn3DDeviceBase@@UEBAPEBUQMetaObject@@XZ
?metaObject@Sn3DDeviceBaseData@@UEBAPEBUQMetaObject@@XZ
?metaObject@Sn3DDeviceEinStar@@UEBAPEBUQMetaObject@@XZ
?onSubscribeData@Sn3DDeviceBase@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@Sn3DDeviceBaseData@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@Sn3DDeviceEinStar@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@Sn3DEinStarDeviceData@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?openDevice@Sn3DDeviceEinStar@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?parseActiveData@Sn3DEinStarDeviceData@@QEAAXXZ
?project@Sn3DDeviceBase@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?publishDeiviceStatus@Sn3DDeviceBaseData@@UEAAX_N0@Z
?publishIsOfflineEnter@Sn3DDeviceBaseData@@QEAAX_N@Z
?publishReconnectRet@Sn3DDeviceBaseData@@QEAAX_N@Z
?publishUpdateFirmWare@Sn3DDeviceBaseData@@QEAAXVQString@@@Z
?reconnectDevice@Sn3DDeviceEinStar@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?reconnectRet@Sn3DDeviceBase@@IEAAX_N@Z
?release@Sn3DDeviceBase@@AEAAXXZ
?resetCurCam@Sn3DDeviceBase@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?restoreBlock@Sn3DDeviceBase@@AEAA_NAEBVQString@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?setAutoReconnectClosed@Sn3DEinStarDeviceData@@IEAAX_N@Z
?setBrightnessSetp@Sn3DEinStarDeviceData@@QEAAXH@Z
?setButtonData@Sn3DDeviceBase@@MEAAXVQString@@0@Z
?setButtonData@Sn3DDeviceEinStar@@MEAAXVQString@@0@Z
?setCaliData@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?setCameraCount@Sn3DDeviceBaseData@@QEAAXH@Z
?setCameraSNList@Sn3DDeviceBaseData@@QEAAXV?$QList@VQVariant@@@@@Z
?setCamsHeight@Sn3DDeviceBaseData@@QEAAXVQString@@H@Z
?setCamsSeries@Sn3DDeviceBaseData@@QEAAXVQString@@0@Z
?setCamsWidth@Sn3DDeviceBaseData@@QEAAXVQString@@H@Z
?setCurrentExp@Sn3DDeviceBaseData@@QEAAXVQString@@N@Z
?setCurrentGain@Sn3DDeviceBaseData@@QEAAXVQString@@H@Z
?setDeviceActiveData@Sn3DDeviceBaseData@@QEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z
?setDeviceInnerData@Sn3DDeviceBaseData@@QEAAXAEBVQByteArray@@@Z
?setDeviceName@Sn3DDeviceBaseData@@QEAAXVQString@@@Z
?setDeviceSerial@Sn3DDeviceBaseData@@QEAAXVQString@@@Z
?setDeviceSeries@Sn3DDeviceBaseData@@UEAAXH@Z
?setDeviceStatus@Sn3DDeviceBase@@IEAAX_N00@Z
?setDeviceSubType@Sn3DDeviceBaseData@@UEAAXH@Z
?setDeviceType@Sn3DDeviceBaseData@@QEAAXAEBVQString@@@Z
?setFirmaVersion@Sn3DDeviceBaseData@@QEAAXAEBVQString@@@Z
?setFirstConnect@Sn3DEinStarDeviceData@@IEAAXXZ
?setFlagBitInterface@Sn3DDeviceBase@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?setFlagBitInterface@Sn3DDeviceEinStar@@UEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?setInFirmUpdate@Sn3DDeviceBaseData@@UEAAX_N@Z
?setIsOfflineEnter@Sn3DDeviceBase@@IEAAX_N@Z
?setIsOfflineEnter@Sn3DDeviceBaseData@@QEAAX_N@Z
?setIsOfflineEnterReconnect@Sn3DDeviceBaseData@@QEAAX_N@Z
?setIsRepalceDevice@Sn3DDeviceBaseData@@QEAAX_N@Z
?setLaserBrightness@Sn3DDeviceBaseData@@QEAAXH@Z
?setLedBrightness@Sn3DDeviceBaseData@@QEAAXHH@Z
?setOffLine@Sn3DDeviceBaseData@@QEAAX_N@Z
?setParaCCMCloseLed@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?setParaCCMOpenLed@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?setParaDarkCloseLed@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?setParaDarkOpenLed@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?setProperty@Sn3DDeviceBase@@QEAAXAEBVQString@@V?$QMap@VQString@@VQVariant@@@@@Z
?setWbDataCloseLed@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?setWbDataOpenLed@Sn3DEinStarDeviceData@@IEAAXAEBVQString@@@Z
?start@Sn3DDeviceBase@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@Sn3DDeviceBaseData@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@Sn3DDeviceEinStar@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?staticMetaObject@Sn3DDeviceBase@@2UQMetaObject@@B
?staticMetaObject@Sn3DDeviceBaseData@@2UQMetaObject@@B
?staticMetaObject@Sn3DDeviceEinStar@@2UQMetaObject@@B
?stop@Sn3DDeviceBase@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@Sn3DDeviceBaseData@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?tr@Sn3DDeviceBase@@SA?AVQString@@PEBD0H@Z
?tr@Sn3DDeviceBaseData@@SA?AVQString@@PEBD0H@Z
?tr@Sn3DDeviceEinStar@@SA?AVQString@@PEBD0H@Z
?trUtf8@Sn3DDeviceBase@@SA?AVQString@@PEBD0H@Z
?trUtf8@Sn3DDeviceBaseData@@SA?AVQString@@PEBD0H@Z
?trUtf8@Sn3DDeviceEinStar@@SA?AVQString@@PEBD0H@Z
?turntableRotate@Sn3DDeviceBase@@QEAA?AW4Sn3DErrorCode@CommonLib@@AEBVQString@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?writeImageData@Sn3DDeviceEinStar@@IEAAXV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@Z
```

Notable *data* symbols: `BOOTLOADERSUBDIR` (static member), `ISFWUPDATE`, `ISFORCEFWUPDATE`, `PARALIST`, `ISBLOCK` — firmware state is exposed as exported static/accessor symbols, so the update state machine is part of the DLL's public ABI.

### 6.2 Controller layer (Sn3DEinStarController.dll, 2 exports)

Only `qt_plugin_instance` and `qt_plugin_query_metadata` are exported: it is a Qt plug-in loaded by name. Its command surface is therefore not in the export table but in the topic strings it publishes/handles (`/FirmUpdate/Configure`, `FirmUpdate/writeBootLoader`, `queryUpdateProgress`, `firmUpdateResult`, `isFWUpdate`, `getCameraSerial`, `setDeviceBrightness`, `setReadImgScanMode`, `getDeviceTemperature`, `startEnterCalibration` / `startExitCalibration`).

### 6.3 SiUSBXp.dll and UsbTreeModel.dll

`SiUSBXp.dll` is a vendor SDK drop (Silicon Labs USBXpress 3.4.1.0, built 2011-05-04, VS2010) — it exposes the `SI_*` API and C++ mangled aliases, and does its own SetupAPI enumeration + `DeviceIoControl` on the USBXpress driver. `UsbTreeModel.dll` is a tiny (42 KB) helper that walks the USB hub tree via SetupAPI/Configuration Manager and can reset a device by port number (`ResetDevice`, `GetSpecifyDeviceTreePath`).

## 7. Calibration API surface (verbatim exports)

These are the seven requested modules plus the closely-related helpers found in the same family. Names are listed **verbatim** as they appear in the export table (MSVC mangling, no demangler available offline); the prose after each block is the readable summary.

### Sn3DCalibDevFile.dll

`c9258681f87260cc050db88cc29d1167daf8e4b6f99c94f2028463fdac396853` · 118776 B · 26 exports · linker 14.16

```
??0DevDataConfigInfo@@QEAA@AEBV0@@Z
??0DevDataConfigInfo@@QEAA@XZ
??0Sn3DCalibDevFileController@@QEAA@PEAVQObject@@@Z
??1DevDataConfigInfo@@QEAA@XZ
??1Sn3DCalibDevFileController@@UEAA@XZ
??4DevDataConfigInfo@@QEAAAEAV0@AEBV0@@Z
??_7Sn3DCalibDevFileController@@6BISn3DConfig@CommonLib@@@
??_7Sn3DCalibDevFileController@@6BQObject@@@
??_FSn3DCalibDevFileController@@QEAAXXZ
?dispatchCmd@Sn3DCalibDevFileController@@AEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@AEAV?$QMap@VQString@@VQVariant@@@@@Z
?getConfigInfo@Sn3DCalibDevFileController@@AEAA_NH@Z
?getInstance@DevDataConfigInfo@@SAAEAV1@XZ
?handleEndCalibComputeCmd@Sn3DCalibDevFileController@@AEAA?AW4Sn3DErrorCode@CommonLib@@AEAV?$QMap@VQString@@VQVariant@@@@@Z
?handleNotifyIsCablliratedCmd@Sn3DCalibDevFileController@@AEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?handleReadDevDataToFileCmd@Sn3DCalibDevFileController@@AEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?onSubscribeData@Sn3DCalibDevFileController@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?publishDevData@Sn3DCalibDevFileController@@AEAAXAEAV?$QMap@VQString@@VQVariant@@@@VQString@@@Z
?publishDevData@Sn3DCalibDevFileController@@AEAAXVQString@@@Z
?publishError@Sn3DCalibDevFileController@@AEAAXAEAV?$QMap@VQString@@VQVariant@@@@VQString@@@Z
?publishError@Sn3DCalibDevFileController@@AEAAXHVQString@@@Z
?readDevDataToFileCmd@Sn3DCalibDevFileController@@QEAA_NAEAUtagDevData@@AEAVQDateTime@@@Z
?setCalibrateTime@Sn3DCalibDevFileController@@AEAAXAEAVQDateTime@@@Z
?start@Sn3DCalibDevFileController@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@Sn3DCalibDevFileController@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
qt_plugin_instance
qt_plugin_query_metadata
```

### Sn3DCalibrateData.dll

`d0773e02cc35dc98905dacec9c7a6ebe26e004f9e3ff73d70df20b05da5a4bfe` · 138232 B · 29 exports · linker 14.16

```
??0ConfigInfo@@QEAA@AEBV0@@Z
??0ConfigInfo@@QEAA@XZ
??0Sn3DCalibData@@QEAA@AEBV0@@Z
??0Sn3DCalibData@@QEAA@XZ
??1ConfigInfo@@QEAA@XZ
??1Sn3DCalibData@@UEAA@XZ
??4ConfigInfo@@QEAAAEAV0@AEBV0@@Z
??4Sn3DCalibData@@QEAAAEAV0@AEBV0@@Z
??_7Sn3DCalibData@@6B@
?config@Sn3DCalibData@@UEAA_NXZ
?deserializeCCF@Sn3DCalibData@@UEAAXVQVariant@@@Z
?deserializeCCMDark@Sn3DCalibData@@UEAAXVQVariant@@@Z
?deserializeWb@Sn3DCalibData@@UEAAXVQVariant@@@Z
?existData@Sn3DCalibData@@UEAA_NXZ
?getCalibDateTime@Sn3DCalibData@@UEAA?AVQDateTime@@XZ
?getInstance@ConfigInfo@@SAAEAV1@XZ
?getLastCalibTime@Sn3DCalibData@@UEAA?AV?$QMap@VQString@@VQVariant@@@@_NAEBVQDateTime@@@Z
?readCCF@Sn3DCalibData@@UEAA_NAEAUtagQuickCCFData@@@Z
?readCCF@Sn3DCalibData@@UEAA_NXZ
?readCCM@Sn3DCalibData@@UEAA_NVQString@@@Z
?readDark@Sn3DCalibData@@UEAA_NVQString@@@Z
?readWb@Sn3DCalibData@@UEAA_NVQString@@@Z
?serializeCCF@Sn3DCalibData@@UEAA?AVQVariant@@XZ
?serializeCCMDark@Sn3DCalibData@@UEAA?AVQVariant@@XZ
?serializeWb@Sn3DCalibData@@UEAA?AVQVariant@@XZ
?setDataFlag@Sn3DCalibData@@UEAAX_N@Z
?writeCCFToFile@Sn3DCalibData@@UEAA_NV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@PEADH@Z
qt_plugin_instance
qt_plugin_query_metadata
```

### Sn3DCalibrateLoader.dll

`01d8d93b57b09e5815be14f730c6fc0fdebb08e7ad499c29074379ab5a077c11` · 133624 B · 58 exports · linker 14.16

```
??0BaseNode@Sn3DCalibrateLoader@@QEAA@AEBUPositionInfo@1@AEBUImgCaptureInfo@1@@Z
??0BaseNode@Sn3DCalibrateLoader@@QEAA@AEBV01@@Z
??0BaseNode@Sn3DCalibrateLoader@@QEAA@XZ
??0CaptureProcess@Sn3DCalibrateLoader@@QEAA@AEBV01@@Z
??0CaptureProcess@Sn3DCalibrateLoader@@QEAA@XZ
??0GroupNode@Sn3DCalibrateLoader@@QEAA@AEBUPositionInfo@1@AEBUImgCaptureInfo@1@@Z
??0GroupNode@Sn3DCalibrateLoader@@QEAA@AEBVCaptureProcess@1@@Z
??0GroupNode@Sn3DCalibrateLoader@@QEAA@XZ
??0StepNode@Sn3DCalibrateLoader@@QEAA@AEBUPositionInfo@1@AEBUImgCaptureInfo@1@@Z
??0StepNode@Sn3DCalibrateLoader@@QEAA@AEBV01@@Z
??0StepNode@Sn3DCalibrateLoader@@QEAA@AEBVGroupNode@1@@Z
??0StepNode@Sn3DCalibrateLoader@@QEAA@XZ
??1BaseNode@Sn3DCalibrateLoader@@UEAA@XZ
??1CaptureProcess@Sn3DCalibrateLoader@@UEAA@XZ
??1GroupNode@Sn3DCalibrateLoader@@UEAA@XZ
??1StepNode@Sn3DCalibrateLoader@@UEAA@XZ
??4BaseNode@Sn3DCalibrateLoader@@QEAAAEAV01@AEBV01@@Z
??4CaptureProcess@Sn3DCalibrateLoader@@QEAAAEAV01@AEBV01@@Z
??4GroupNode@Sn3DCalibrateLoader@@QEAAAEAV01@$$QEAV01@@Z
??4StepNode@Sn3DCalibrateLoader@@QEAAAEAV01@AEBV01@@Z
??ACaptureProcess@Sn3DCalibrateLoader@@QEAAAEAV?$shared_ptr@VGroupNode@Sn3DCalibrateLoader@@@std@@AEBH@Z
??AGroupNode@Sn3DCalibrateLoader@@QEAAAEAV?$shared_ptr@VStepNode@Sn3DCalibrateLoader@@@std@@AEBH@Z
??_7BaseNode@Sn3DCalibrateLoader@@6B@
??_7CaptureProcess@Sn3DCalibrateLoader@@6B@
??_7GroupNode@Sn3DCalibrateLoader@@6B@
??_7StepNode@Sn3DCalibrateLoader@@6B@
?calibCommands@BaseNode@Sn3DCalibrateLoader@@QEAAAEAUCalibCommands@2@XZ
?clear@CaptureProcess@Sn3DCalibrateLoader@@QEAAXXZ
?completeCapted@CaptureProcess@Sn3DCalibrateLoader@@UEAA_NXZ
?completeCapted@GroupNode@Sn3DCalibrateLoader@@UEAA_NXZ
?completeCapted@StepNode@Sn3DCalibrateLoader@@UEAA_NXZ
?distance@GroupNode@Sn3DCalibrateLoader@@QEBAAEBV?$tuple@_K_K_K@std@@XZ
?getCaptureProcess@Sn3DCalibrateLoader@@YAAEAVCaptureProcess@1@XZ
?get_lock@singleton_module@serialization@boost@@AEAAAEA_NXZ
?get_mutable_instance@?$singleton@VSn3DCalibrateXmlParser@Sn3DCalibrateLoader@@@serialization@boost@@SAAEAVSn3DCalibrateXmlParser@Sn3DCalibrateLoader@@XZ
?imgCaptureInfo@BaseNode@Sn3DCalibrateLoader@@QEBAAEAUImgCaptureInfo@2@XZ
?is_locked@singleton_module@serialization@boost@@QEAA_NXZ
?loadConfig@Sn3DCalibrateLoader@@YAXAEBVQString@@_N@Z
?lock@?1??get_lock@singleton_module@serialization@boost@@AEAAAEA_NXZ@4_NA
?lock@singleton_module@serialization@boost@@QEAAXXZ
?positionInfo@BaseNode@Sn3DCalibrateLoader@@QEBAAEBUPositionInfo@2@XZ
?pushStep@GroupNode@Sn3DCalibrateLoader@@QEAAXAEBV?$shared_ptr@VStepNode@Sn3DCalibrateLoader@@@std@@@Z
?push_back@CaptureProcess@Sn3DCalibrateLoader@@QEAAXAEBV?$shared_ptr@VGroupNode@Sn3DCalibrateLoader@@@std@@@Z
?push_back_wb@CaptureProcess@Sn3DCalibrateLoader@@QEAAXAEBV?$shared_ptr@VGroupNode@Sn3DCalibrateLoader@@@std@@@Z
?setCalibCommands@BaseNode@Sn3DCalibrateLoader@@UEAAXAEBUCalibCommands@2@@Z
?setDistance@GroupNode@Sn3DCalibrateLoader@@QEAAXAEBV?$tuple@_K_K_K@std@@@Z
?setImgCaptureInfo@BaseNode@Sn3DCalibrateLoader@@UEAAXAEBUImgCaptureInfo@2@@Z
?setImgCaptureUncomplete@BaseNode@Sn3DCalibrateLoader@@QEAAXXZ
?setPositionInfo@BaseNode@Sn3DCalibrateLoader@@UEAAXAEBUPositionInfo@2@@Z
?setStepPoseInfo@StepNode@Sn3DCalibrateLoader@@QEAAXAEBUPositionInfo@2@@Z
?size@CaptureProcess@Sn3DCalibrateLoader@@QEBAAEB_KXZ
?stepPoseInfo@StepNode@Sn3DCalibrateLoader@@QEAAAEAUStepPoseInfo@2@XZ
?stepsCount@GroupNode@Sn3DCalibrateLoader@@QEBA_KXZ
?switchCaptureProcess@Sn3DCalibrateLoader@@YAXAEAH@Z
?unCompleteCapted@CaptureProcess@Sn3DCalibrateLoader@@UEAAXXZ
?unCompleteCapted@GroupNode@Sn3DCalibrateLoader@@UEAAXXZ
?unCompleteCapted@StepNode@Sn3DCalibrateLoader@@UEAAXXZ
?unlock@singleton_module@serialization@boost@@QEAAXXZ
```

### Sn3DCalibrateAlgorithmWrapper.dll

`f1696b6e73f038341fb2dbd53d521837f5f95e9bdf0ca32da7173bb0bb00af03` · 583672 B · 129 exports · linker 14.16

```
??0CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0E7PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0EAPoseAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??0Sn3DConfig@@QEAA@AEBV0@@Z
??0Sn3DConfig@@QEAA@XZ
??0Sn3DConfigSQLite@@QEAA@XZ
??0WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@QEAA@PEAVQObject@@@Z
??1CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1E7PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??1Sn3DConfig@@UEAA@XZ
??1WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA@XZ
??4Sn3DConfig@@QEAAAEAV0@AEBV0@@Z
??_7CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7E7PoseAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7E7PoseAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7EAPoseAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7EAPoseAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7PoseAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7PoseAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_7Sn3DConfig@@6B@
??_7Sn3DConfigSQLite@@6B@
??_7WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@6BISn3DConfig@CommonLib@@@
??_7WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@6BQObject@@@
??_FCameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FE7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FE7PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FEACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FEAPoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FEAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FLaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FPoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
??_FWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@QEAAXXZ
?checkAccuracyTest@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAHAEBV?$QMap@VQString@@VQVariant@@@@AEAN@Z
?d_func@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVCameraCalibrateAlgManagerPrivate@@XZ
?d_func@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVCameraCalibrateAlgManagerPrivate@@XZ
?d_func@E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVE7CameraCalibAlgManagerPrivate@@XZ
?d_func@E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVE7CameraCalibAlgManagerPrivate@@XZ
?d_func@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVE7PoseAlgManagerPrivate@@XZ
?d_func@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVE7PoseAlgManagerPrivate@@XZ
?d_func@EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVEACameraCalibAlgManagerPrivate@@XZ
?d_func@EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVEACameraCalibAlgManagerPrivate@@XZ
?d_func@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVEAPoseAlgManagerPrivate@@XZ
?d_func@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVEAPoseAlgManagerPrivate@@XZ
?d_func@EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVEAWhiteBalanceAlgManagerPrivate@@XZ
?d_func@EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVEAWhiteBalanceAlgManagerPrivate@@XZ
?d_func@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVLaserCalibrateAlgManagerPrivate@@XZ
?d_func@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVLaserCalibrateAlgManagerPrivate@@XZ
?d_func@PoseAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVPoseAlgManagerPrivate@@XZ
?d_func@PoseAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVPoseAlgManagerPrivate@@XZ
?d_func@WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVWhiteBalanceAlgManagerPrivate@@XZ
?d_func@WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVWhiteBalanceAlgManagerPrivate@@XZ
?metaObject@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEBAPEBUQMetaObject@@XZ
?onSubscribeData@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?poseAlgCompute@PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAXAEBVQString@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@PoseAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?publishRet@WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@AEAAXAEBVQString@@HV?$QMap@VQString@@VQVariant@@@@@Z
?qt_metacall@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEAAHW4Call@QMetaObject@@HPEAPEAX@Z
?qt_metacast@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEAAPEAXPEBD@Z
?qt_static_metacall@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@CAXPEAVQObject@@W4Call@QMetaObject@@HPEAPEAX@Z
?releaseLaserRebHandle@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NXZ
?releaseTriViewRebHandle@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NXZ
?resetLaserRebHandle@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEBV?$QMap@VQString@@VQVariant@@@@@Z
?resetSn3DComputeE10FactoryPose@PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z
?resetTracker@PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z
?resetTriViewRebHandle@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEBV?$QMap@VQString@@VQVariant@@@@@Z
?runE7AccuracyCalibratePose@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@AEAA?AW4Sn3DErrorCode@CommonLib@@V?$QMap@VQString@@VQVariant@@@@@Z
?runLaserRebuildTy@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?runLaserRebuildXL@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?runSn3DUECalibration@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?runSn3DUEPExtrinsicCalibration@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?runTriViewRebuildXL@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@QEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?start@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?start@WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?staticMetaObject@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@2UQMetaObject@@B
?stop@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@E7CameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@EACameraCalibAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@EAWhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@LaserCalibrateAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@PoseAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@WhiteBalanceAlgManager@Sn3DCalibrateAlgorithm@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?tr@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@SA?AVQString@@PEBD0H@Z
?trUtf8@EAPoseAlgManager@Sn3DCalibrateAlgorithm@@SA?AVQString@@PEBD0H@Z
qt_plugin_instance
qt_plugin_query_metadata
```

### Sn3DCameraCalibHelper.dll

`c7bc073cd412b040f1552af30bf7d653a91e214dfd975b7e6f627f403878a69a` · 185848 B · 33 exports · linker 14.16

```
??0RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@QEAA@XZ
??0Sn3DCameraCalibAlg@0@QEAA@PEAVQObject@@@Z
??0Sn3DSespCalibAlgParam@Sn3DCameraCalibAlg@@QEAA@XZ
??0Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@QEAA@PEAVQObject@@@Z
??1RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@QEAA@XZ
??1Sn3DCameraCalibAlg@0@UEAA@XZ
??1Sn3DSespCalibAlgParam@Sn3DCameraCalibAlg@@QEAA@XZ
??1Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA@XZ
??4RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@QEAAAEAV01@AEBV01@@Z
??_7Sn3DCameraCalibAlg@0@6BISn3DConfig@CommonLib@@@
??_7Sn3DCameraCalibAlg@0@6BQObject@@@
??_7Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@6BISn3DConfig@CommonLib@@@
??_7Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@6BQObject@@@
??_FSn3DCameraCalibAlg@0@QEAAXXZ
??_FSn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@QEAAXXZ
?cameraCompute@Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA?AW4Sn3DCameraAlgErrorCode@2@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?comboWhiteBalance@RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@QEAAXPEAE000HHAEAN11@Z
?get2D@Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA_NAEBV?$QMap@VQString@@VQVariant@@@@@Z
?getAveImage@RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@QEAAXAEBV?$vector@PEAEV?$allocator@PEAE@std@@@std@@PEAEGG@Z
?mergeRGBBmp@RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@AEAAXHPEAE000@Z
?onSubscribeData@Sn3DCameraCalibAlg@1@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?progressBar@Sn3DCameraCalibAlg@1@IEAAXH_NHVQString@@@Z
?publishCalibData@Sn3DCameraCalibAlg@1@IEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z
?publishCalibRes@Sn3DCameraCalibAlg@1@IEAAXW4Sn3DCameraAlgErrorCode@1@N@Z
?readPicCameraCompute@Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA?AW4Sn3DCameraAlgErrorCode@2@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?start@Sn3DCameraCalibAlg@1@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@Sn3DCameraCalibAlg@1@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?subTexture@RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@AEAAXHPEAE0@Z
?whiteBalance@RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@AEAAXPEAEHAEAN11@Z
?whiteBanlance@Sn3DCameraCalibAlg@1@UEAA?AW4Sn3DCameraAlgErrorCode@1@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?whiteBanlance@Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA?AW4Sn3DCameraAlgErrorCode@2@AEBV?$QMap@VQString@@VQVariant@@@@@Z
qt_plugin_instance
qt_plugin_query_metadata
```

### Sn3DCalibCaptureImageProcess.dll

`b018f4a26925ee5062ddd3824b77b029ce84c582f6b4f107e9dc9800e25dd50b` · 459256 B · 103 exports · linker 14.16

```
??0CalibCaptureImageProcess@Sn3DCalibrateProcess@@QEAA@AEAVCalibCaptureImageProcessPrivate@1@PEAVQObject@@@Z
??0CalibCaptureImageProcess@Sn3DCalibrateProcess@@QEAA@PEAVQObject@@@Z
??0E7CalibCaptureImageController@Sn3DCalibrateProcess@@QEAA@PEAVQObject@@@Z
??0EACalibCaptureImageController@Sn3DCalibrateProcess@@QEAA@PEAVQObject@@@Z
??0SespCalibCaptureImageController@Sn3DCalibrateProcess@@QEAA@XZ
??0UECalibCaptureImageController@Sn3DCalibrateProcess@@QEAA@XZ
??1CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEAA@XZ
??1E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA@XZ
??1EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA@XZ
??1SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA@XZ
??1UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA@XZ
??_7CalibCaptureImageProcess@Sn3DCalibrateProcess@@6BISn3DConfig@CommonLib@@@
??_7CalibCaptureImageProcess@Sn3DCalibrateProcess@@6BQObject@@@
??_7E7CalibCaptureImageController@Sn3DCalibrateProcess@@6BISn3DConfig@CommonLib@@@
??_7E7CalibCaptureImageController@Sn3DCalibrateProcess@@6BQObject@@@
??_7EACalibCaptureImageController@Sn3DCalibrateProcess@@6BISn3DConfig@CommonLib@@@
??_7EACalibCaptureImageController@Sn3DCalibrateProcess@@6BQObject@@@
??_7SespCalibCaptureImageController@Sn3DCalibrateProcess@@6BISn3DConfig@CommonLib@@@
??_7SespCalibCaptureImageController@Sn3DCalibrateProcess@@6BQObject@@@
??_7UECalibCaptureImageController@Sn3DCalibrateProcess@@6BISn3DConfig@CommonLib@@@
??_7UECalibCaptureImageController@Sn3DCalibrateProcess@@6BQObject@@@
??_FCalibCaptureImageProcess@Sn3DCalibrateProcess@@QEAAXXZ
??_FE7CalibCaptureImageController@Sn3DCalibrateProcess@@QEAAXXZ
??_FEACalibCaptureImageController@Sn3DCalibrateProcess@@QEAAXXZ
?calibCaptureProcess@CalibCaptureImageProcess@Sn3DCalibrateProcess@@MEAAXXZ
?calibCaptureProcess@SespCalibCaptureImageController@Sn3DCalibrateProcess@@MEAAXXZ
?convertDownSampling@CalibCaptureImageProcess@Sn3DCalibrateProcess@@IEAA_N_NPEBEHHPEAEHH@Z
?d_func@CalibCaptureImageProcess@Sn3DCalibrateProcess@@IEAAPEAVCalibCaptureImageProcessPrivate@2@XZ
?d_func@CalibCaptureImageProcess@Sn3DCalibrateProcess@@IEBAPEBVCalibCaptureImageProcessPrivate@2@XZ
?d_func@E7CalibCaptureImageController@Sn3DCalibrateProcess@@AEAAPEAVE7CalibCaptureImageControllerPrivate@2@XZ
?d_func@E7CalibCaptureImageController@Sn3DCalibrateProcess@@AEBAPEBVE7CalibCaptureImageControllerPrivate@2@XZ
?d_func@EACalibCaptureImageController@Sn3DCalibrateProcess@@AEAAPEAVEACalibCaptureImageControllerPrivate@2@XZ
?d_func@EACalibCaptureImageController@Sn3DCalibrateProcess@@AEBAPEBVEACalibCaptureImageControllerPrivate@2@XZ
?getImagesData@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?BV?$vector@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@V?$allocator@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@2@@std@@AEAV?$QMap@VQString@@VQVariant@@@@@Z
?getImagesData@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?BV?$vector@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@V?$allocator@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@2@@std@@AEAV?$QMap@VQString@@VQVariant@@@@@Z
?getImagesData@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?BV?$vector@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@V?$allocator@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@2@@std@@AEAV?$QMap@VQString@@VQVariant@@@@@Z
?getImagesData@UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?BV?$vector@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@V?$allocator@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@2@@std@@AEAV?$QMap@VQString@@VQVariant@@@@@Z
?get_lock@singleton_module@serialization@boost@@AEAAAEA_NXZ
?get_mutable_instance@?$singleton@VCmdObjectFactory@@@serialization@boost@@SAAEAVCmdObjectFactory@@XZ
?handleEndCalibComputeCmd@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?handleEndCalibComputeCmd@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?handleEndCalibComputeCmd@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?handleEndCalibComputeCmd@UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?handleSaveAllImagesToDisk@E7CalibCaptureImageController@Sn3DCalibrateProcess@@QEAA?AW4Sn3DErrorCode@CommonLib@@_N@Z
?handleSaveAllImagesToDisk@EACalibCaptureImageController@Sn3DCalibrateProcess@@QEAA?AW4Sn3DErrorCode@CommonLib@@_N@Z
?handleSaveAllImagesToDiskCmd@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?handleSaveAllImagesToDiskCmd@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?handleSaveAllImagesToDiskCmd@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?handleSaveAllImagesToDiskCmd@UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$QMap@VQString@@VQVariant@@@@@Z
?isReadImagesMode@CalibCaptureImageProcess@Sn3DCalibrateProcess@@QEAA_NXZ
?is_locked@singleton_module@serialization@boost@@QEAA_NXZ
?lock@?1??get_lock@singleton_module@serialization@boost@@AEAAAEA_NXZ@4_NA
?lock@singleton_module@serialization@boost@@QEAAXXZ
?metaObject@CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEBAPEBUQMetaObject@@XZ
?metaObject@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEBAPEBUQMetaObject@@XZ
?metaObject@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEBAPEBUQMetaObject@@XZ
?onSubscribeData@CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?onSubscribeData@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z
?publishCalibResultCmd@E7CalibCaptureImageController@Sn3DCalibrateProcess@@QEAAX_NVQString@@N@Z
?publishCalibResultCmd@EACalibCaptureImageController@Sn3DCalibrateProcess@@QEAAX_NVQString@@N@Z
?qt_metacall@CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEAAHW4Call@QMetaObject@@HPEAPEAX@Z
?qt_metacall@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAAHW4Call@QMetaObject@@HPEAPEAX@Z
?qt_metacall@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAAHW4Call@QMetaObject@@HPEAPEAX@Z
?qt_metacast@CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEAAPEAXPEBD@Z
?qt_metacast@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAAPEAXPEBD@Z
?qt_metacast@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAAPEAXPEBD@Z
?qt_static_metacall@CalibCaptureImageProcess@Sn3DCalibrateProcess@@CAXPEAVQObject@@W4Call@QMetaObject@@HPEAPEAX@Z
?qt_static_metacall@E7CalibCaptureImageController@Sn3DCalibrateProcess@@CAXPEAVQObject@@W4Call@QMetaObject@@HPEAPEAX@Z
?qt_static_metacall@EACalibCaptureImageController@Sn3DCalibrateProcess@@CAXPEAVQObject@@W4Call@QMetaObject@@HPEAPEAX@Z
?readTotalImages@CalibCaptureImageProcess@Sn3DCalibrateProcess@@MEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?readTotalImages@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?readTotalImages@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?readTotalImages@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?readTotalImages@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?readTotalImages@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?readTotalImages@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?readTotalImages@UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z
?saveOneGroupImageData@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@AEBV?$QMap@VQString@@VQVariant@@@@AEBV?$vector@VQString@@V?$allocator@VQString@@@std@@@6@@Z
?saveOneGroupImageData@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@AEBV?$QMap@VQString@@VQVariant@@@@AEBV?$vector@VQString@@V?$allocator@VQString@@@std@@@6@@Z
?saveOneGroupImageData@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@AEBV?$QMap@VQString@@VQVariant@@@@AEBV?$vector@VQString@@V?$allocator@VQString@@@std@@@6@@Z
?saveOneGroupImageData@UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@AEBV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@AEBV?$QMap@VQString@@VQVariant@@@@AEBV?$vector@VQString@@V?$allocator@VQString@@@std@@@6@@Z
?saveOneImageToDisk@CalibCaptureImageProcess@Sn3DCalibrateProcess@@IEAAXPEBDPEAEHH@Z
?start@CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?staticMetaObject@CalibCaptureImageProcess@Sn3DCalibrateProcess@@2UQMetaObject@@B
?staticMetaObject@E7CalibCaptureImageController@Sn3DCalibrateProcess@@2UQMetaObject@@B
?staticMetaObject@EACalibCaptureImageController@Sn3DCalibrateProcess@@2UQMetaObject@@B
?stop@CalibCaptureImageProcess@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@E7CalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@EACalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@SespCalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?stop@UECalibCaptureImageController@Sn3DCalibrateProcess@@UEAA?AW4Sn3DErrorCode@CommonLib@@XZ
?tr@CalibCaptureImageProcess@Sn3DCalibrateProcess@@SA?AVQString@@PEBD0H@Z
?tr@E7CalibCaptureImageController@Sn3DCalibrateProcess@@SA?AVQString@@PEBD0H@Z
?tr@EACalibCaptureImageController@Sn3DCalibrateProcess@@SA?AVQString@@PEBD0H@Z
?trUtf8@CalibCaptureImageProcess@Sn3DCalibrateProcess@@SA?AVQString@@PEBD0H@Z
?trUtf8@E7CalibCaptureImageController@Sn3DCalibrateProcess@@SA?AVQString@@PEBD0H@Z
?trUtf8@EACalibCaptureImageController@Sn3DCalibrateProcess@@SA?AVQString@@PEBD0H@Z
?unlock@singleton_module@serialization@boost@@QEAAXXZ
?wbCaptureProcess@CalibCaptureImageProcess@Sn3DCalibrateProcess@@MEAAXXZ
?wbCaptureProcess@SespCalibCaptureImageController@Sn3DCalibrateProcess@@MEAAXXZ
qt_plugin_instance
qt_plugin_query_metadata
```

### Sn3DEACalibrateController.dll

`1e3d33af7faa56e21ba9d3dc602c45da4beced79ce9d6f488d3238c81d33abde` · 301048 B · 9 exports · linker 14.16

```
??0Sn3DConfig@@QEAA@AEBV0@@Z
??0Sn3DConfig@@QEAA@XZ
??0Sn3DConfigSQLite@@QEAA@XZ
??1Sn3DConfig@@UEAA@XZ
??4Sn3DConfig@@QEAAAEAV0@AEBV0@@Z
??_7Sn3DConfig@@6B@
??_7Sn3DConfigSQLite@@6B@
qt_plugin_instance
qt_plugin_query_metadata
```

### 7.1 API surface summary

| module | surface (readable) |
|---|---|
| Sn3DCalibDevFile | Qt plug-in controller `Sn3DCalibDevFileController` (start/stop/dispatchCmd, `readDevDataToFileCmd`, `handleReadDevDataToFileCmd`, `handleEndCalibComputeCmd`, `handleNotifyIsCablliratedCmd`, `publishDevData`, `publishError`, `setCalibrateTime`) + `DevDataConfigInfo` singleton. |
| Sn3DCalibrateData | `Sn3DCalibData` model: `readCCF` / `serializeCCF` / `deserializeCCF` / `writeCCFToFile`, `readCCM`/`deserializeCCMDark`, `readWb`/`deserializeWb`, `getCalibDateTime`, `getLastCalibTime`, `existData`, `setDataFlag`. |
| Sn3DCalibrateLoader | Procedure model `BaseNode`/`GroupNode`/`StepNode`/`CaptureProcess` with `loadConfig`, `calibCommands`, `imgCaptureInfo`, `completeCapted`, `distance`, plus a singleton `VSn3DCalibrateXmlParser` — i.e. the calibration *procedure* is XML-configured. |
| Sn3DCalibrateAlgorithmWrapper | Algorithm managers per product generation: `CameraCalibrateAlgManager`, `E7CameraCalibAlgManager`, `EACameraCalibAlgManager`, `PoseAlgManager`, `LaserCalibrateAlgManager`, `WhiteBalanceAlgManager` (+ `Sn3DConfig`/`Sn3DConfigSQLite`). |
| Sn3DCameraCalibHelper | `Sn3DCameraCalibAlg`, `Sn3DSespCameraCalibAlg`, `RGBComboWhiteBalanceAlg`: `cameraCompute`, `get2D`, `whiteBalance`, `comboWhiteBalance`, `mergeRGBBmp`, `getAveImage`, `readPicCameraCompute`, progress/publish handlers. |
| Sn3DCalibCaptureImageProcess | Capture controllers for four device families (`E7`, `EA`, `Sesp`, `UE`) + `CalibCaptureImageProcess`: `calibCaptureProcess`, `convertDownSampling`, `getImagesData`, `handleEndCalibComputeCmd`. |
| Sn3DEACalibrateController | Qt plug-in only (`qt_plugin_instance`) — the EA/Einstar calibration workflow wiring lives in its command handlers and QML page (`CalibrationPage_EA.dll`). |

Data files: `ccf/200x150/{LeftCCF,RightCCF,TexCCF}.txt` are binary arrays of little-endian doubles (camera intrinsics/extrinsics for left, right and texture cameras at 200×150), matching `Sn3DCalibData::readCCF`/`writeCCFToFile`. `config/app1.db` is a SQLCipher database; `config/einstarGFE…json` is the per-device config blob.

## 8. Crypto / anti-tamper

| layer | module | evidence |
|---|---|---|
| Data crypto | `Sn3DCrypto.dll` | Exports `Sn3DAlgorithm::Sn3DCryptoAES` and `Sn3DCrypto3DES` with `encrypto_text`, `decrypto_text`, `encrypto_file2file`, `encrypto_text2file`, and mesh-level `read_mesh_with_key`/`write_mesh_with_key`/`convert_binary_to_mesh` — scan data can be stored encrypted in a proprietary container. |
| Licensing / dongle | `Sn3DSecurity.dll` (6.0 MB), `hasp_windows_x64_105562.dll`, `haspvlib_105562.dll`, `sn3DLock.dll` | HASP strings (`Aladdin\Vid0529&Pid0001&HaspHL`, `hasp_rt.exe`, `checkDongle`, `Sn3DDongle`, `UE20Dongle`, `OptimScanStudioDongle`, cloud licensing `</cloud_licensing>`), clone detection and commuter-licence logic; imports `DeviceIoControl`. |
| Machine binding | `Sn3DSecurity.dll`, `Sn3DPassportController.dll` | USB enumeration strings `USB\VID_0424`, `USB\VID_0529`, `USB\VID_05E3`, `USB\VID_13FE`; `SetupDiGetClassDevsW` + `CM_Get_Device_IDW` in the passport controller. |
| Transport crypto | `tunnel_encrypt.dll`, `qttunnel.3.0.9.dll`, `libcrypto-1_1-x64.dll`/`libssl-1_1-x64.dll` | `tunnel_encrypt` imported by `qttunnel`; OpenSSL 1.1 for TLS; legacy `LIBEAY32`/`SSLEAY32` still shipped for older plug-ins. |
| Generic crypto | `cryptopp.dll` (4200 exports), `sqlitecipher.dll` | Crypto++ symbol table; SQLCipher for `config/app1.db`. |
| Integrity | `Sn3DSecurity.dll` | Qt plug-in that imports `Sn3DDeveloperControlPlugin`, `Sn3DErrorCode`; strings for certificate/licence directories (`certlicensedir`, `checkDongle`, `checkDeviceWithHaspdog`). |

## 9. GPU / compute

| stack | modules | evidence |
|---|---|---|
| CUDA 10.2 | `cudart64_102`, `cusolver64_10`, `nppc64_10`, `nppig64_10`, `nvjpeg64_10` (syncservice), `librenderkit.plugin.cuda.dll`, `Sn3DCloudProcessGPU.dll` | `NVIDIA CUDA 10.2.89 Runtime`; `NvOptimusEnablementCuda` export in `Sn3DCalibBigCirExtract.dll` |
| OpenCL | none shipped (no `opencl.dll` import found) | import scan |
| TensorRT / nvinfer | none | import scan |
| Intel MKL | `mkl_rt`, `mkl_core`, `mkl_def`, `mkl_sequential`, `mkl_tbb_thread`, `libblas`/`liblapack` (MinGW) | file names, exports |
| OpenGL / OSG | `osg158-*` (10 modules), `osgPlugins-3.6.3/` (85 plug-ins), `glew32`, `glfw3`, `opengl32sw`, `libEGL`/`libGLESV2` | directories + exports |
| DirectX | `D3Dcompiler_47.dll`, `dxgi.dll` (imported by `libSn3DNavigation.dll`), `SnDXGICapturePlugin.dll` | imports |

## 10. Vision / reconstruction libraries

| library | presence | evidence |
|---|---|---|
| OpenCV 3.4.8 | 10 DLLs (`core`, `imgproc`, `calib3d`, `features2d`, `flann`, `highgui`, `imgcodecs`, `videoio`, `cudawarping`) + `syncservice` copy at 3.4.11 | filenames + build banner |
| Ceres | `ceres.dll` | `ceres::` symbols |
| Eigen | **header-only** — no DLL | `Eigen::Matrix<…>` in mangled names of `algorithm*.dll`, `common.dll`, `Sn3DAlgorithmWrapper.dll` |
| OpenMesh | `OpenMeshCore.dll`, `OpenMeshTools.dll` | `OpenMesh::` symbols |
| lib3MF | `lib3MF.dll` | `NMR::` symbols |
| PCL | **absent** | no `pcl_*` DLL, no `pcl::` symbols |
| VTK | **absent** | no `vtk*` DLL; `vtk` substrings are inside mangled names only |
| Embree / Assimp | **absent** | no DLL, no imports |
| TBB | `tbb.dll` + `mkl_tbb_thread` | exports |

## 11. Qt and UI stack

Qt **5.15.10**, MSVC 2017 x64, shared. Core set: `Qt5Core`, `Gui`, `Widgets`, `Qml`, `Quick`, `QuickControls2`, `QuickTemplates2`, `QuickShapes`, `Network`, `Sql`, `Svg`, `Xml`, `Multimedia`, `MultimediaQuick`, `Concurrent`, `RemoteObjects`. Plug-in directories: `platforms/`, `imageformats/` (20), `iconengines/`, `sqldrivers/` (5), `bearer/`, `plugins/mediaservice/`, plus 40+ QML import trees (`QtQuick/`, `QtQml/`, `Qt/labs/`, `QtGraphicalEffects/`). `qttunnel.3.0.9.dll` is a vendor Qt module (QtTunnel + QMQTT) that implements the inter-process message bus; `Qt5RemoteObjects` supports its remoting mode.

## 12. Module dependency graph

Static import edges between shipped modules: **4696**; string-referenced (dynamic-load) edges: **4901**. The full adjacency lists are in `analysis/gnu/module_dependencies.json` (`edges`, `dynamic_refs`, `dependencies`, `dependents`).

Most-imported shipped modules (in-degree):

| module | dependents |
|---|---|
| `msvcp140.dll` | 433 |
| `Qt5Core.dll` | 287 |
| `passport/openweburl/Qt5Core.dll` | 287 |
| `syncservice/Qt5Core.dll` | 287 |
| `Release/osg158-osg.dll` | 195 |
| `osg158-osg.dll` | 195 |
| `Release/ot21-OpenThreads.dll` | 186 |
| `ot21-OpenThreads.dll` | 186 |
| `Release/osg158-osgDB.dll` | 184 |
| `osg158-osgDB.dll` | 184 |
| `Qt5Gui.dll` | 123 |
| `passport/openweburl/Qt5Gui.dll` | 123 |
| `syncservice/Qt5Gui.dll` | 123 |
| `Qt5Qml.dll` | 84 |
| `passport/openweburl/Qt5Qml.dll` | 84 |

Most-dependent modules (out-degree, shipped targets only):

| module | dependencies |
|---|---|
| `librenderkit.extensions.digit.dll` | 31 |
| `PreviewTool.exe` | 30 |
| `sn3d.digit.scenecontrol.dll` | 28 |
| `Sn3DAlgorithmWrapper.dll` | 27 |
| `librenderkit.dll` | 27 |
| `ScanAlignFusionProcess.dll` | 26 |
| `Sn3DDental.dll` | 26 |
| `Sn3DDigital.dll` | 25 |
| `PostProcess.dll` | 24 |
| `GenerateDataProcess.dll` | 22 |
| `Sn3DDeviceEinStar.dll` | 22 |
| `Sn3DTextureSFM.dll` | 22 |

Layering (derived): OS/runtime (`KERNEL32`, UCRT, `MSVCP140`) → Qt/OSG/OpenCV/third-party → `Sn3DBox`/`Sn3DConfig`/`Sn3DLog`/`Sn3DImageQueue` (vendor foundation) → `common`/`CommonFunc`/`Sn3DAlgorithmWrapper` (geometry) → device/calibration/scan controllers → `AppUi`/`AppCommon_E7` → `EXStar.exe`. No cycles were found among shipped modules at the DLL-import level (cycles are resolved at run time through the Qt plug-in and QtTunnel buses).

## 13. Firmware update path (evidence chain)

| # | evidence (observed) | module | interpretation (hypothesis) |
|---|---|---|---|
| 1 | `UpdateFirmware`, `confirmUpdateFirmWare`, `getFirmwareVersion`, `ISFWUPDATE`, `ISFORCEFWUPDATE` | `Sn3DDeviceEinStar.dll` | Device object exposes firmware version and an update state machine. |
| 2 | `FirmUpdate/writeBootLoader`, `FirmwareLanguage`, `*.img`, `_JAP.img`, `BOOTLOADERSUBDIR`, `/Fx3Bootloader.exe` | `Sn3DDeviceEinStar.dll` | Firmware images are `*.img` per language/market (`_JAP` = Japan), written through a bundled FX3 bootloader executable. |
| 3 | `/FirmUpdate/Configure`, `queryUpdateProgress`, `firmUpdateResult`, `isFWUpdate`, `updateFirmware filePath =`, `*.img` | `Sn3DEinStarController.dll` | Controller drives the update over the command bus, polls progress and reports a result. |
| 4 | QML: `param['cmd'] = 'UpdateFirmware'`, `param["filePath"] = $device.firmwareFilePath`, messages about cutting power | `FirmwareupdateUI.exe` | User-facing QML page sends the command and instructs a power cycle; UI is a separate process. |
| 5 | `Fx3Bootloader.exe` is **not present** in the installation, and no `*.img` files are shipped | tree scan | Firmware payloads/bootloader are provisioned separately (service tool or download), not by this installer. |

The FX3 driver package (`cyusb3.sys`) and `WdfCoinstaller01011.dll` are shipped, so the device can be re-enumerated in bootloader mode; the actual image bytes are not in scope.

## 14. High-signal strings (8 requested modules)

Full dumps: `analysis/strings/<module>.strings.txt` (whole file, ASCII + UTF-16LE, `strings -a -n 5` / `strings -a -e l -n 5`). Curated file: `analysis/strings/interesting_strings.md`. Section-aware dumps (`*.sections.txt`) and CJK attempts (`*.cjk.txt`) are alongside.

| finding | literal (verbatim) | module |
|---|---|---|
| FX3 bootloader helper | `/Fx3Bootloader.exe`, `Fx3Bootloader.exe` | Sn3DDeviceEinStar.dll |
| Firmware image selection | `*.img`, `_JAP.img`, `FirmwareLanguage` | Sn3DDeviceEinStar.dll |
| Firmware write command | `FirmUpdate/writeBootLoader` | Sn3DDeviceEinStar.dll |
| Firmware configure command | `/FirmUpdate/Configure` | Sn3DEinStarController.dll |
| Update progress | `queryUpdateProgress`, `firmUpdateResult`, `isFWUpdate` | Sn3DEinStarController.dll |
| Forced update flag | `ISFORCEFWUPDATE` (exported) | Sn3DDeviceEinStar.dll |
| QML update command | `param['cmd'] = 'UpdateFirmware'` | FirmwareupdateUI.exe |
| Power-cycle warning | “Upgrade successfully. Please cut off the electricity and reconnect…” | FirmwareupdateUI.exe |
| Device service | `[DP 2023] Exist SnSyncService`, `SnSyncService.exe Process state changed!` | Sn3DDeviceEinStar.dll |
| Stereo serials | `CameraSerialL`, `CameraSerialR`, `CameraSerial` | Sn3DEinStarController.dll |
| Telemetry | `getDeviceTemperature`, `setDeviceBrightness`, `setReadImgScanMode` | Sn3DEinStarController.dll |
| Calibration commands | `readDevDataToFileCmd`, `handleNotifyIsCablliratedCmd` | Sn3DCalibDevFile.dll |
| Turbo/CCF token | `2CCFF,` | FirmwareupdateUI.exe |
| Build provenance | `D:\Jenkins\workspace\Sn3DFirmwareUpdate\…` | FirmwareupdateUI.exe |

**Version strings** found in the eight modules are build/provenance paths rather than product version numbers; product versioning comes from the update/cloud modules (`Sn3DSoftwareUpdate::checkNewVersion`) and `config/SystemCfg.json`.

**Chinese strings:** none genuine. All CJK candidates are decoding artefacts — 0xCC stack fill (`锰烫烫烫…`), ASCII mangled names read as UTF-16 (`䵑灡` = `QMap`), or font tables in `FirmwareupdateUI.exe`. Localisation ships as 21 Qt catalogues in `language_qm/`. A GBK sweep also returned only fill noise.

## 15. Module table (curated)

`sha256` is truncated to 12 hex characters; full digests are in `pe_inventory.json`. “Toolchain” is the linker-version mapping plus CRT flavour. Roles are hypotheses unless the evidence column cites a literal/export.

| module | sha256 | type | size KB | toolchain | key imports | key exports | role hypothesis | evidence |
|---|---|---|---|---|---|---|---|---|
| `7z.dll` | `5b3ef4bbbc2c` | DLL / native / AMD64 | 1680 | MSVC 14.00 (VS2005) | msvcrt.dll (25), OLEAUT32.dll (7) | `CreateDecoder`, `CreateEncoder`, `CreateObject` | 7-Zip engine (13 exports). | 7-Zip 21.07 banner |
| `7z.exe` | `27d1d66bfd61` | EXE / native / AMD64 | 533 | MSVC 14.00 (VS2005) | msvcrt.dll (38), OLEAUT32.dll (7) | — | 7-Zip CLI shipped for archive handling. | 7-Zip 21.07 banner |
| `AppCommon_E7.dll` | `c09a53ea8a83` | DLL / native / AMD64 | 2493 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Widgets.dll (184), Qt5Core.dll (80), Qt5Gui.dll (9), Qt5Qml.dll (1) | `?closeEvent@Sn3DMessagebox@@MEAAXPEAVQCloseEvent@@@Z`, `?curLocaleId@Sn3DLanguageLoader@@QEBAHXZ`, `?curLocaleIdChanged@Sn3DLanguageLoader@@QEAAXXZ` | Shared application logic for the E7-family (EinScan) products. | 49 exports, Qt Widgets/Quick |
| `AppUi.dll` | `bf2f01b7715b` | DLL / native / AMD64 | 1193 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (265), Qt5Widgets.dll (186), Sn3DBox.dll (23), Qt5Gui.dll (17) | `qt_plugin_instance`, `qt_plugin_query_metadata` | UI shell / window composition for the EXStar product. | 2 exports (Qt plug-in), imports AppCommon_E7 |
| `CalibrationPage_EA.dll` | `5efe74fb2668` | DLL / native / AMD64 | 2655 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (83), Sn3DBox.dll (29), glogc.dll (5), Qt5Qml.dll (3) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Qt/QML calibration page for the EA (Einstar) product. | 2 exports; 2.7 MB QML resources |
| `CommonFunc.dll` | `9427a6fedbd3` | DLL / native / AMD64 | 5217 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (339), OpenMeshCore.dll (201), tbb.dll (27), VCOMP140.DLL (17) | `??6ClipperLib@@YAAEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEAV12@AEBUIntPoint@0@@Z`, `??6ClipperLib@@YAAEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEAV12@AEBV?$vector@UIntPoint@ClipperLib@@V?$allocator@UIntPoint@ClipperLib@@@std@@@2@@Z`, `??6ClipperLib@@YAAEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEAV12@AEBV?$vector@V?$vector@UIntPoint@ClipperLib@@V?$allocator@UIntPoint@ClipperLib@@@std@@@std@@V?$allocator@V?$vector@UIntPoint@ClipperLib@@V?$allocator@UIntPoint@ClipperLib@@@std@@@std@@@2@@2@@Z` | General utility library (files, strings, system). | 1487 exports |
| `D3Dcompiler_47.dll` | `dee3ddffb9cc` | DLL / native / AMD64 | 4071 | MSVC 17.00 (VS2012) | msvcrt.dll (117), RPCRT4.dll (1) | `D3DAssemble`, `D3DCompile`, `D3DCompile2` | Microsoft Direct3D shader compiler. | 29 exports |
| `EXStar.exe` | `2fb0092b14bf` | EXE / native / AMD64 | 340 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Widgets.dll (188), Qt5Core.dll (166), Qt5Gui.dll (13), Sn3DProcessPlugin.dll (5) | `NvOptimusEnablement` | Application entry point (Qt Widgets); boots the E7/EXStar app layer and loads process plug-ins. | imports AppCommon_E7/AppUi chain, Sn3DProcessPlugin; PDB 'EXStar' |
| `FirmwareupdateUI.exe` | `fefd7ce95514` | EXE / native / AMD64 | 2604 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (92), glogc.dll (5), Qt5Qml.dll (4), Qt5Gui.dll (3) | — | QML firmware-update UI: sends cmd='UpdateFirmware' with firmwareFilePath. | QML strings 'param[\'cmd\']=\'UpdateFirmware\'', PDB Sn3DFirmwareUpdate |
| `LIBEAY32.dll` | `a9dd2dc3f382` | DLL / native / AMD64 | 2239 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | CRYPT32.dll (7) | `ACCESS_DESCRIPTION_free`, `ACCESS_DESCRIPTION_it`, `ACCESS_DESCRIPTION_new` | Legacy OpenSSL 1.0.2q libcrypto (older plug-ins). | 3709 exports; banner OpenSSL 1.0.2q; D:\Projects PDB |
| `LaunchTool.exe` | `bfaa033f96b4` | EXE / native / AMD64 | 4130 | MSVC 19.16 (VS2017 15.9) | gdiplus.dll (22), OLEAUT32.dll (14), UxTheme.dll (12), SHLWAPI.dll (7) | — | Launcher/updater helper. | imports AppCommon_E7 |
| `OpenMeshCore.dll` | `737021682c7c` | DLL / native / AMD64 | 474 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | — | `?IOManager@IO@OpenMesh@@YAAEAV_IOManager_@12@XZ`, `?InvalidEdgeHandle@PolyConnectivity@OpenMesh@@2UEdgeHandle@2@B`, `?InvalidFaceHandle@PolyConnectivity@OpenMesh@@2UFaceHandle@2@B` | OpenMesh core mesh data structures. | 783 exports |
| `OpenMeshTools.dll` | `be9bb91a77ab` | DLL / native / AMD64 | 56 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | — | `??8Timer@Utils@OpenMesh@@QEBA_NAEBV012@@Z`, `??MTimer@Utils@OpenMesh@@QEBA_NAEBV012@@Z`, `??NTimer@Utils@OpenMesh@@QEBA_NAEBV012@@Z` | OpenMesh tools (decimater, subdivision…). | 116 exports |
| `PnPutil.exe` | `345c298340a9` | EXE / native / AMD64 | 304 | MSVC 19.20 (VS2019 16.0) | ntdll.dll (81), msvcrt.dll (29), DEVOBJ.dll (8), Cabinet.dll (5) | — | Microsoft PnP driver utility, used by driver install scripts. | Microsoft-signed |
| `PreviewTool.exe` | `3867e342b087` | EXE / native / AMD64 | 5344 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | osg158-osg.dll (573), Qt5Core.dll (259), osg158-osgGA.dll (111), Qt5Quick.dll (56) | — | Standalone preview tool. | GUI |
| `Qt5Core.dll` | `e6c1dc3f8890` | DLL / native / AMD64 | 8130 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | VERSION.dll (3), NETAPI32.dll (2), WINMM.dll (2), MPR.dll (1) | `??$qbswap@$01@@YAPEAXPEBX_JPEAX@Z`, `??$qbswap@$03@@YAPEAXPEBX_JPEAX@Z`, `??$qbswap@$07@@YAPEAXPEBX_JPEAX@Z` | Qt 5.15.10 core, built with MSVC 2017 (vendor build D:\qt_build). | 8368 exports; string 'Qt 5.15.10 (x86_64…by MSVC 2017)' |
| `Qt5Gui.dll` | `d9f35e2e1f40` | DLL / native / AMD64 | 7060 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (1477), d3d11.dll (1), dxgi.dll (1) | `??$handleApplicationStateChanged@UAsynchronousDelivery@QWindowSystemInterface@@@QWindowSystemInterface@@SAXW4ApplicationState@Qt@@_N@Z`, `??$handleApplicationStateChanged@UDefaultDelivery@QWindowSystemInterface@@@QWindowSystemInterface@@SAXW4ApplicationState@Qt@@_N@Z`, `??$handleApplicationStateChanged@USynchronousDelivery@QWindowSystemInterface@@@QWindowSystemInterface@@SAXW4ApplicationState@Qt@@_N@Z` | Qt 5.15.10 GUI. | 16563 exports |
| `Qt5Network.dll` | `c05967ffdba2` | DLL / native / AMD64 | 1331 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (881), libcrypto-1_1-x64.dll (164), libssl-1_1-x64.dll (78), CRYPT32.dll (9) | `??5@YAAEAVQDataStream@@AEAV0@AEAVQHostAddress@@@Z`, `??5@YAAEAVQDataStream@@AEAV0@AEAVQNetworkCacheMetaData@@@Z`, `??6@YA?AVQDebug@@V0@AEBVQHostAddress@@@Z` | Qt 5.15.10 networking (HTTP/TLS). | 1506 exports |
| `Qt5Qml.dll` | `12e4f15f8484` | DLL / native / AMD64 | 3735 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (1192), Qt5Network.dll (33) | `??$qmlRegisterTypeAndRevisions@VQQmlTypeNotAvailable@@X@QQmlPrivate@@YAXPEBDHPEBUQMetaObject@@@Z`, `??$singletonInstance@VQJSValue@@@QQmlEngine@@QEAA?AVQJSValue@@H@Z`, `??$singletonInstance@VQJSValue@@@QQmlEnginePrivate@@QEAA?AVQJSValue@@AEBVQQmlType@@@Z` | Qt 5.15.10 QML engine. | 5358 exports |
| `Qt5Quick.dll` | `3fa257ba4616` | DLL / native / AMD64 | 4187 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Gui.dll (1747), Qt5Core.dll (884), Qt5Qml.dll (479), Qt5QmlModels.dll (63) | `??6@YA?AVQDebug@@V0@AEBUVariable@ShaderInfo@QSGGuiThreadShaderEffectManager@@@Z`, `??6@YA?AVQDebug@@V0@AEBUVariableData@QSGShaderEffectNode@@@Z`, `??6@YA?AVQDebug@@V0@PEAVQQuickItem@@@Z` | Qt 5.15.10 QtQuick scene graph. | 6775 exports |
| `Qt5Widgets.dll` | `69b2b479102e` | DLL / native / AMD64 | 5500 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Gui.dll (1823), Qt5Core.dll (1436), UxTheme.dll (7), dwmapi.dll (3) | `??5@YAAEAVQDataStream@@AEAV0@AEAVQListWidgetItem@@@Z`, `??5@YAAEAVQDataStream@@AEAV0@AEAVQSizePolicy@@@Z`, `??5@YAAEAVQDataStream@@AEAV0@AEAVQTableWidgetItem@@@Z` | Qt 5.15.10 Widgets. | 9054 exports |
| `S3D_Teamviewer.exe` | `2be1d96ed040` | EXE / native / I386 | 24247 | MSVC 12.00 (VS6) | COMCTL32.dll (4) | — | Bundled TeamViewer remote-support client. | 24 MB GUI |
| `SSLEAY32.dll` | `9a2c6a64a732` | DLL / native / AMD64 | 364 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | LIBEAY32.dll (372) | `BIO_f_ssl`, `BIO_new_buffer_ssl_connect`, `BIO_new_ssl` | Legacy OpenSSL 1.0.2q libssl. | 321 exports; banner OpenSSL 1.0.2q |
| `SiUSBXp.dll` | `33958f5ae581` | DLL / native / AMD64 | 92 | MSVC 16.00 (VS2010) | SETUPAPI.dll (5), VERSION.dll (3) | `?SI_ResetDevice@@YAHPEAX@Z`, `SI_CheckRXQueue`, `SI_Close` | Silicon Laboratories USBXpress 3.4.1.0 API (SI_Open/SI_Read/SI_Write/SI_DeviceIOControl) for the turntable's USB-serial bridge. | 24 exports; imports SETUPAPI + DeviceIoControl; 2011 timestamp |
| `Sn3DAIOutUnity.dll` | `44d02d098671` | DLL / native / AMD64 | 92 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (16), opencv_core348.dll (1) | `?ai_out_status_list@AIOutStatusManager@Sn3DAlgorithm@@QEAAXAEBVBaseTriMesh@2@AEAV?$vector@_NV?$allocator@_N@std@@@std@@@Z`, `?dump@Sn3DAIOutBaseClass@Sn3DAlgorithm@@UEAA?AW4RetVal@2@AEAVBaseTriMesh@2@@Z`, `?get_Info@?$Sn3DAIInfo@V?$vector@V?$Matrix@M$02$00$0A@$02$00@Eigen@@V?$allocator@V?$Matrix@M$02$00$0A@$02$00@Eigen@@@std@@@std@@@Sn3DAIOutBaseClass@Sn3DAlgorithm@@QEAAAEAV?$vector@V?$Matrix@M$02$00$0A@$02$00@Eigen@@V?$allocator@V?$Matrix@M$02$00$0A@$02$00@Eigen@@@std@@@std@@XZ` | AI output bridge to Unity. | 33 exports |
| `Sn3DAVXImp.dll` | `19a1aa291901` | DLL / native / AMD64 | 162 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (26), tbb.dll (1) | `AlgorithmJYJavx_func`, `AlgorithmZBTavx_func`, `Sn3DRegistrationAVX_func` | AVX optimised import routines. | 3 exports |
| `Sn3DAlgorithmWrapper.dll` | `5ba900e27272` | DLL / native / AMD64 | 1730 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (166), common.dll (152), Sn3DRealtimeScan.dll (81), algorithmLzy.dll (37) | `?applyClip@ClipAlgManager@Sn3DAlgorithmWrapper@@QEAA_NV?$QMap@VQString@@VQVariant@@@@@Z`, `?autoMarkerPostProcess@SpeckleTrackAndFusionAlgManager@Sn3DAlgorithmWrapper@@QEAA?AW4Sn3DAlgorithmErrorCode@@AEBV?$QMap@VQString@@VQVariant@@@@_N@Z`, `?autoMarkerPostProcessV2@SpeckleTrackAndFusionAlgManager@Sn3DAlgorithmWrapper@@QEAA?AW4Sn3DAlgorithmErrorCode@@AEBV?$QMap@VQString@@VQVariant@@@@_N@Z` | Facade over the vendor algorithms; imported by scan controllers. | 147 exports |
| `Sn3DAutoExposureHelper.dll` | `0baacce4cf01` | DLL / native / AMD64 | 63 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (22), Sn3DBox.dll (20), glogc.dll (7) | `?clearBuffer@AutoExposureManager@AutoSetExposureHelper@@QEAAXXZ`, `?getCachedBrightnessLevelCount@AutoExposureManager@AutoSetExposureHelper@@QEAAHXZ`, `?getImageData@AutoExposureManager@AutoSetExposureHelper@@QEAAAEAV?$map@HV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@U?$less@H@2@V?$allocator@U?$pair@$$CBHV?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@std@@@2@@std@@XZ` | Auto-exposure manager for the scan cameras. | 19 exports |
| `Sn3DBox.dll` | `5d2e53988cbb` | DLL / native / AMD64 | 371 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (213), Qt5Xml.dll (17), qttunnel.3.0.9.dll (17), Qt5Qml.dll (7) | `??BSn3dSharedMemoryReader@Sn3DSharedMemory@CommonLib@@QEBA_NXZ`, `??BSn3dSharedMemoryWriter@Sn3DSharedMemory@CommonLib@@QEBA_NXZ`, `?SDKSettingMap@Sn3DApplication@@UEAA?AV?$QMap@VQString@@VQVariant@@@@XZ` | Qt-facing message box / IPC object base (Topic/Module wrappers). | 166 exports |
| `Sn3DCalibBigCirExtract.dll` | `ae024c5086ca` | DLL / native / AMD64 | 491 | MSVC 19.16 (VS2017 15.9) | opencv_core348.dll (10), opencv_imgproc348.dll (2) | `?ExtractBigCirclePoint@CBigCirExtractRealTime@Sn3DLaserScanner@@QEAA_NPEAU_IplImage@@00AEAV?$vector@UCvPoint3D64f@@V?$allocator@UCvPoint3D64f@@@std@@@std@@_N@Z`, `?RebPoint3D@CBigCirExtractRealTime@Sn3DLaserScanner@@QEAA_NAEAV?$vector@UCvPoint2D64f@@V?$allocator@UCvPoint2D64f@@@std@@@std@@0AEAV?$vector@UCvPoint3D64f@@V?$allocator@UCvPoint3D64f@@@std@@@4@@Z`, `?ResetCaliParamBigCircle@CBigCirExtractRealTime@Sn3DLaserScanner@@QEAAXPEAN00AEAN00000100@Z` | Big-circle extraction for laser/calibration targets. | 6 exports |
| `Sn3DCalibCaptureImageProcess.dll` | `b018f4a26925` | DLL / native / AMD64 | 448 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (156), Sn3DBox.dll (21), Sn3DCalibrateLoader.dll (11), glogc.dll (7) | `?calibCaptureProcess@CalibCaptureImageProcess@Sn3DCalibrateProcess@@MEAAXXZ`, `?calibCaptureProcess@SespCalibCaptureImageController@Sn3DCalibrateProcess@@MEAAXXZ`, `?convertDownSampling@CalibCaptureImageProcess@Sn3DCalibrateProcess@@IEAA_N_NPEBEHHPEAEHH@Z` | Calibration capture pipeline/controllers (E7/EA/Sesp/UE). | 103 exports |
| `Sn3DCalibDevFile.dll` | `c9258681f872` | DLL / native / AMD64 | 116 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (54), Sn3DBox.dll (20), glogc.dll (5) | `?dispatchCmd@Sn3DCalibDevFileController@@AEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@AEAV?$QMap@VQString@@VQVariant@@@@@Z`, `?getConfigInfo@Sn3DCalibDevFileController@@AEAA_NH@Z`, `?getInstance@DevDataConfigInfo@@SAAEAV1@XZ` | Device calibration data file controller (read DevData to file, calibration-state notifications). | 26 exports; readDevDataToFileCmd / handleNotifyIsCablliratedCmd |
| `Sn3DCalibrateAlgorithmHelper.dll` | `6e6a3b7bd74d` | DLL / native / AMD64 | 84 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (30), Sn3DCalibrationJR.dll (7), glogc.dll (5), common.dll (2) | `?clearMarkExtract@CalibrateMarkExtract@@AEAAXXZ`, `?g_instanceObj@CalibrateMarkExtract@@0PEAV1@EA`, `?getMarkExtract@CalibrateMarkExtract@@QEAA?BV?$shared_ptr@VSn3DMarkExtract@Sn3DAlgorithm@@@std@@H@Z` | Calibration marker extraction helper. | 10 exports |
| `Sn3DCalibrateAlgorithmWrapper.dll` | `f1696b6e73f0` | DLL / native / AMD64 | 570 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (114), common.dll (29), Sn3DBox.dll (21), Sn3DCalibrationJR.dll (20) | `?checkAccuracyTest@E7PoseAlgManager@Sn3DCalibrateAlgorithm@@QEAAHAEBV?$QMap@VQString@@VQVariant@@@@AEAN@Z`, `?d_func@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEAAPEAVCameraCalibrateAlgManagerPrivate@@XZ`, `?d_func@CameraCalibrateAlgManager@Sn3DCalibrateAlgorithm@@AEBAPEBVCameraCalibrateAlgManagerPrivate@@XZ` | Calibration algorithm managers (camera, pose, laser, white balance) for E7/EA/Sesp variants. | 129 exports |
| `Sn3DCalibrateData.dll` | `d0773e02cc35` | DLL / native / AMD64 | 135 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (109), Sn3DBox.dll (20), glogc.dll (7) | `?config@Sn3DCalibData@@UEAA_NXZ`, `?deserializeCCF@Sn3DCalibData@@UEAAXVQVariant@@@Z`, `?deserializeCCMDark@Sn3DCalibData@@UEAAXVQVariant@@@Z` | Calibration data model: (de)serialise CCF, CCM+dark, white balance; read/write CCF files. | 29 exports; readCCF/writeCCFToFile/serializeCCF |
| `Sn3DCalibrateImageInfo.dll` | `bafbee680299` | DLL / native / AMD64 | 67 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (51), Sn3DBox.dll (20), opencv_core348.dll (6), Sn3DImageQueue.dll (3) | `?clearCache@Sn3DCalibrateImageInfoController@@UEAAXXZ`, `?dispatchCmd@Sn3DCalibrateImageInfoController@@AEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@AEAV?$QMap@VQString@@VQVariant@@@@@Z`, `?getImagesData@Sn3DCalibrateImageInfoController@@UEAAAEAV?$vector@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@V?$allocator@V?$shared_ptr@UImageQueueItem@sn3d_imagequeue@@@std@@@2@@std@@XZ` | Calibration image store: save groups/images to disk, read totals. | 18 exports |
| `Sn3DCalibrateLoader.dll` | `01d8d93b57b0` | DLL / native / AMD64 | 130 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (70), Qt5Xml.dll (26), glogc.dll (4) | `??ACaptureProcess@Sn3DCalibrateLoader@@QEAAAEAV?$shared_ptr@VGroupNode@Sn3DCalibrateLoader@@@std@@AEBH@Z`, `??AGroupNode@Sn3DCalibrateLoader@@QEAAAEAV?$shared_ptr@VStepNode@Sn3DCalibrateLoader@@@std@@AEBH@Z`, `?calibCommands@BaseNode@Sn3DCalibrateLoader@@QEAAAEAUCalibCommands@2@XZ` | Calibration procedure loader: XML step/group tree (BaseNode/GroupNode/StepNode), capture workflow. | 58 exports |
| `Sn3DCalibrationJR.dll` | `2f14ad6f15a2` | DLL / native / AMD64 | 6030 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (104), opencv_core348.dll (48), opencv_imgproc348.dll (30), ceres.dll (29) | `??$importAsciiData@M@imu_tk@@YAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEAV?$vector@V?$ImuData@M@imu_tk@@V?$allocator@V?$ImuData@M@imu_tk@@@std@@@2@11W4TimestampUnit@0@W4DatasetType@0@@Z`, `??$importAsciiData@M@imu_tk@@YAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEAV?$vector@V?$ImuData@M@imu_tk@@V?$allocator@V?$ImuData@M@imu_tk@@@std@@@2@1W4TimestampUnit@0@W4DatasetType@0@@Z`, `??$importAsciiData@M@imu_tk@@YAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEAV?$vector@V?$ImuData@M@imu_tk@@V?$allocator@V?$ImuData@M@imu_tk@@@std@@@2@W4TimestampUnit@0@W4DatasetType@0@@Z` | Large calibration algorithm library (camera/IMU/light-plane, Ceres+imu_tk). | 790 exports |
| `Sn3DCameraCalibHelper.dll` | `c7bc073cd412` | DLL / native / AMD64 | 181 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (118), Sn3DBox.dll (21), common.dll (8), glogc.dll (7) | `?cameraCompute@Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA?AW4Sn3DCameraAlgErrorCode@2@AEBV?$QMap@VQString@@VQVariant@@@@@Z`, `?comboWhiteBalance@RGBComboWhiteBalanceAlg@Sn3DCameraCalibAlg@@QEAAXPEAE000HHAEAN11@Z`, `?get2D@Sn3DSespCameraCalibAlg@Sn3DCameraCalibAlg@@UEAA_NAEBV?$QMap@VQString@@VQVariant@@@@@Z` | Camera calibration + white balance algorithms (Sesp camera, RGB combo WB). | 33 exports |
| `Sn3DCloudDisk.dll` | `04305616fea1` | DLL / native / AMD64 | 247 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (207), Sn3DBox.dll (23), Sn3DQuickCloudHelper.dll (11), Sn3DConfig.dll (7) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Cloud-storage client (upload/download scans). | 9 exports |
| `Sn3DCloudProcessGPU.dll` | `02a17a13b0bd` | DLL / native / AMD64 | 650 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (37), tbb.dll (13), boost_log-vc141-mt-x64-1_72.dll (11), boost_filesystem-vc141-mt-x64-1_72.dll (8) | `?cloud_reproject_for_point_color_multi_times_GPU@Sn3DAlgorithm@@YA?AW4RetVal@1@AEAVCloud@1@AEBV?$vector@PEAVCompressedImage@Sn3DAlgorithm@@V?$allocator@PEAVCompressedImage@Sn3DAlgorithm@@@std@@@std@@AEBV?$vector@VRigidMatrix@Sn3DAlgorithm@@V?$allocator@VRigidMatrix@Sn3DAlgorithm@@@std@@@5@AEAV?$vector@MV?$allocator@M@std@@@5@AEBVCameraInParam@1@AEBV?$Matrix@H$03$00$0A@$03$00@Eigen@@@Z`, `?model_reproject_for_point_color_multi_times_GPU@Sn3DAlgorithm@@YA?AW4RetVal@1@AEAVBaseTriMesh@1@AEBV?$vector@PEAVCompressedImage@Sn3DAlgorithm@@V?$allocator@PEAVCompressedImage@Sn3DAlgorithm@@@std@@@std@@AEBV?$vector@VRigidMatrix@Sn3DAlgorithm@@V?$allocator@VRigidMatrix@Sn3DAlgorithm@@@std@@@5@AEAV?$vector@MV?$allocator@M@std@@@5@AEBVCameraInParam@1@AEBV?$Matrix@H$03$00$0A@$03$00@Eigen@@@Z`, `NvOptimusEnablementCuda` | GPU cloud-processing helper. | 3 exports |
| `Sn3DCodeProject.dll` | `fc116d4b795d` | DLL / native / AMD64 | 265 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (73), Sn3DBox.dll (20), glogc.dll (5) | `?Auto_ration_Quar@Csn3DCode@@QEAA_NAEBV?$vector@V?$vector@NV?$allocator@N@std@@@std@@V?$allocator@V?$vector@NV?$allocator@N@std@@@std@@@2@@std@@00000V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@3@11@Z`, `?Auto_ration_Quar@Csn3DCode@@QEAA_NAEBV?$vector@V?$vector@NV?$allocator@N@std@@@std@@V?$allocator@V?$vector@NV?$allocator@N@std@@@std@@@2@@std@@00000V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@3@1@Z`, `?checkAuthorization@Sn3DCodeProjectController@@AEAA_NVQString@@0AEAH1AEAV2@21@Z` | Coded structured-light projection. | 85 exports |
| `Sn3DColorCorrect.dll` | `1d50e0abf670` | DLL / native / AMD64 | 1333 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | commonGpu.dll (20), opencv_core348.dll (15), boost_log-vc141-mt-x64-1_72.dll (11), tbb.dll (10) | `?WriteCompensatingTable@SNColorAlgorithm@@YA_NAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@0@Z`, `?bilateral_filter@Denoise@SNColorAlgorithm@@QEAAXPEAMHMM@Z`, `?generate_bilateral_core@Denoise@SNColorAlgorithm@@AEAAXH@Z` | Colour correction / texture colour handling. | 64 exports |
| `Sn3DConfig.dll` | `9f7cc7791120` | DLL / native / AMD64 | 53 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (54), Qt5Sql.dll (30) | `?checkIsOpen@Sn3DConfigSQLite@@UEAA_NXZ`, `?execQuery@Sn3DConfigSQLite@@UEAA?AVQString@@AEBV2@@Z`, `?getConfig@Sn3DConfigSQLite@@UEAA?AVQVariant@@AEBVQString@@AEBV2@PEAVSn3DConfigStrategy@@@Z` | Configuration service (SQLite/JSON backed). | 15 exports |
| `Sn3DCork.dll` | `6f7fd29e8494` | DLL / native / AMD64 | 1869 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | OpenMeshCore.dll (66), common.dll (65), mpir.dll (21), tbb.dll (17) | `??8CmpVec@Sn3DAlgorithm@@QEBA_NAEBU01@@Z`, `??8Sn3DAlgorithm@@YA_NAEBV?$Matrix@M$02$00$0A@$02$00@Eigen@@0@Z`, `??8Sn3DAlgorithm@@YA_NAEBV?$Matrix@N$02$00$0A@$02$00@Eigen@@0@Z` | Feature/anchoring component (cork = registration seeds). | 244 exports |
| `Sn3DCrypto.dll` | `b0fb2568cbe5` | DLL / native / AMD64 | 312 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | cryptopp.dll (174), common.dll (39), boost_log-vc141-mt-x64-1_72.dll (11), boost_filesystem-vc141-mt-x64-1_72.dll (8) | `?convertKeyIV@Sn3DCrypto@Sn3DAlgorithm@@UEAA?AW4RetVal@2@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEAV?$vector@EV?$allocator@E@std@@@5@1@Z`, `?convert_binary_to_mesh@Sn3DCryptoBasetriMesh@Sn3DAlgorithm@@SA?AW4RetVal@2@AEAVBaseTriMesh@2@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z`, `?convert_mesh_to_binary@Sn3DCryptoBasetriMesh@Sn3DAlgorithm@@SA?AW4RetVal@2@AEBVBaseTriMesh@2@AEAV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z` | AES-128/3DES text/file crypto plus key-protected mesh read/write ('own format'). | 52 exports: Sn3DCryptoAES::encrypto_file2file, decrypto_text, read_mesh_with_key… |
| `Sn3DDental.dll` | `8f47fd23d0ea` | DLL / native / AMD64 | 6316 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (354), OpenMeshCore.dll (138), CommonFunc.dll (50), glew32.dll (43) | `?GenerateLines@Rod@Sn3DAlgorithm@@QEAAXXZ`, `?GetGrid@Sn3DDieToothSeg@Sn3DAlgorithm@@AEBA?AV?$Matrix@H$02$00$0A@$02$00@Eigen@@AEBH@Z`, `?GetHashIdx@Sn3DDieToothSeg@Sn3DAlgorithm@@AEBAHAEBV?$Matrix@H$02$00$0A@$02$00@Eigen@@@Z` | Dental scan application layer (vertical). | 893 exports |
| `Sn3DDentalAI.dll` | `6f7da84a89c1` | DLL / native / AMD64 | 2943 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | cryptopp.dll (159), common.dll (123), OpenMeshCore.dll (41), glew32.dll (39) | `??$check_trt_bin_exist@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@DentalDetectionThreadSafeManager@Sn3DAlgorithm@@SA_NAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z`, `??$check_trt_bin_exist@V?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@@DentalDetectionThreadSafeManager@Sn3DAlgorithm@@SA_NAEBV?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@@Z`, `??$initialize_auto_margin_line_extract@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@DentalDetectionThreadSafeManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBUAutoMarginLineExtractOpt@1@@Z` | Dental AI inference wrapper. | 357 exports |
| `Sn3DDeveloperControlPlugin.dll` | `fbf07ed4344e` | DLL / native / AMD64 | 68 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (61) | `?getDevCtrFile@devCtrPulgin@DevCtrSp@@UEAA_NAEAV?$QMap@VQString@@VQVariant@@@@@Z`, `?getDevCtrFile@devFileOperation_V1@DevCtrSp@@SA_NVQString@@AEAV3@AEAUdevFile_V1@2@@Z`, `?metaObject@devCtrPulgin@DevCtrSp@@UEBAPEBUQMetaObject@@XZ` | Developer control plug-in: reads/writes devCtr files (devFileOperation_V1). | 30 exports, getDevCtrFile/saveDevCtrFile |
| `Sn3DDeviceEinStar.dll` | `d419882dd957` | DLL / native / AMD64 | 353 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (225), Sn3DBox.dll (28), qttunnel.3.0.9.dll (17), Qt5Gui.dll (11) | `?BOOTLOADERSUBDIR@Sn3DDeviceBase@@0QEADEA`, `?ISBLOCK@Sn3DDeviceBaseData@@2QEADEA`, `?ISFORCEFWUPDATE@Sn3DDeviceBaseData@@2QEADEA` | Einstar device layer: enumeration/open/close, serials, temperature, brightness, calibration data read-out, firmware update orchestration (spawns SnSyncService.exe). | 187 exports: ISFWUPDATE, ISFORCEFWUPDATE, BOOTLOADERSUBDIR, openDevice, captureImage, getFirmwareVersion |
| `Sn3DDigitGlobal.dll` | `51823573300b` | DLL / native / AMD64 | 37 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (11), Qt5Qml.dll (7) | `?staticMetaObject@SnComDef_App@@3UQMetaObject@@B`, `?staticMetaObject@SnComDef_Device@@3UQMetaObject@@B`, `?staticMetaObject@SnComDef_Scan@@3UQMetaObject@@B` | Digital module globals. | 5 exports |
| `Sn3DDigital.dll` | `7e39a9444a11` | DLL / native / AMD64 | 5583 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (333), CommonFunc.dll (82), algorithm2.dll (68), algorithmLzy.dll (61) | `?SDFBlockToVirtualVoxelPos@HashData@@QEBA?AUint3@@AEBU2@@Z`, `?SDFBlockToWorld@HashData@@QEBA?AUfloat3@@AEBUint3@@@Z`, `?addVoxel@HashData@@QEAA_NAEATVoxelUtilHashHeaderInfo@@@Z` | Digital/scan data application layer. | 996 exports |
| `Sn3DEACalibrateController.dll` | `1e3d33af7faa` | DLL / native / AMD64 | 294 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (114), Sn3DBox.dll (23), Sn3DCalibrateData.dll (20), boost_thread-vc141-mt-x64-1_72.dll (7) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Qt plug-in that drives the EA calibration workflow for the Einstar-class device. | 9 exports; imports Sn3DDeviceEinStar |
| `Sn3DEinStarController.dll` | `eda4caa4b215` | DLL / native / AMD64 | 246 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (103), Sn3DBox.dll (22), Sn3DDeviceEinStar.dll (10), glogc.dll (5) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Qt plug-in that adapts Sn3DDeviceEinStar to the controller/command bus. | 2 exports (qt_plugin_instance); imports Sn3DDeviceEinStar; topics /FirmUpdate/Configure, FirmUpdate/writeBootLoader |
| `Sn3DErrorCode.dll` | `21639e1e5be3` | DLL / native / AMD64 | 52 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (15), glogc.dll (5) | `?buildErrCode@Sn3DErrorCodeTool@@QEAAHHH@Z`, `?getInstance@Sn3DErrorCodeTool@@SAPEAV1@XZ`, `?getLastError@Sn3DErrorCodeTool@@QEAAXAEAI@Z` | Central error-code enum/registry. | 18 exports, imports by Sn3DSecurity |
| `Sn3DFaceScan.dll` | `cc1734f3b2ee` | DLL / native / AMD64 | 5420 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | opencv_core348.dll (32), commonGpu.dll (27), common.dll (24), tbb.dll (11) | `?sn3D_Facescan_rebuild_ini@Sn3DAlgorithm@@YAPEAXAEBUFaceScanRebuildParam@1@@Z`, `?sn3D_get_Facescan_rebuild_camera_para@Sn3DAlgorithm@@YA?AW4RetVal@1@PEAXAEAVCameraInParam@1@@Z`, `?sn3D_get_Facescan_rebuild_rect_to_orig@Sn3DAlgorithm@@YA?AW4RetVal@1@PEAXAEAVRigidMatrix@1@@Z` | Face-scan pipeline. | 15 exports |
| `Sn3DFile.dll` | `4ec04204875a` | DLL / native / AMD64 | 1369 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (182), Qt5Core.dll (172), Sn3DBox.dll (22), boost_program_options-vc141-mt-x64-1_72.dll (10) | `?addData@PrjFile@@UEAA?AW4ErrorCode@Sn3DFile@@PEBUPrjInfo@@@Z`, `?addPrj@SlnFile@@QEAA?AW4ErrorCode@Sn3DFile@@AEBU_tagProjectInfo@@_N@Z`, `?appendPrj@PrjFile@@UEAA?AW4ErrorCode@Sn3DFile@@AEAV?$QMap@VQString@@VQVariant@@@@@Z` | File format I/O for projects and meshes. | 184 exports |
| `Sn3DFrameDataController.dll` | `e5a2e84bbed0` | DLL / native / AMD64 | 89 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (50), Qt5Core.dll (25), Sn3DConfig.dll (6), Sn3DMultiProjects.dll (3) | `?calclateLeft2RightRT@FrameDataHelper@@QEAAHAEBUCalibParam@Sn3DAlgorithm@@0AEAV?$Matrix@N$02$02$0A@$02$02@Eigen@@AEAV?$Matrix@N$02$00$0A@$02$00@5@@Z`, `?depthGenerateCloud@FrameDataHelper@@AEAAXAEAV?$vector@V?$shared_ptr@UFrame@ProjParser@@@std@@V?$allocator@V?$shared_ptr@UFrame@ProjParser@@@std@@@2@@std@@HV?$QMap@VQString@@VQVariant@@@@@Z`, `?depthMap2Data@FrameDataHelper@@QEAAHHAEBV?$vector@MV?$allocator@M@std@@@std@@V?$shared_ptr@VPointImage@Sn3DAlgorithm@@@3@V?$shared_ptr@VCloudWithWeight@Sn3DAlgorithm@@@3@@Z` | Frame-data controller object (serialise/publish frames). | 29 exports |
| `Sn3DFrameDataQueue.dll` | `be68e85737c3` | DLL / native / AMD64 | 46 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (28), Sn3DBox.dll (20) | `?deserializeData@FrameDataObj@Sn3DFrameData@@UEAA?AW4DeserializeErrorCode@Sn3DDeserialize@@PEADH@Z`, `?get@Sn3DFrameDataQueue@Sn3DFrameData@@QEAAXAEAV?$shared_ptr@VFrameDataObj@Sn3DFrameData@@@std@@H@Z`, `?getInstance@Sn3DFrameDataQueue@Sn3DFrameData@@SAPEAV12@AEBVQString@@@Z` | Frame data queue primitives. | 25 exports |
| `Sn3DGeometricTrackFusion.dll` | `fc3acc00355d` | DLL / native / AMD64 | 1782 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (219), tbb.dll (24), boost_log-vc141-mt-x64-1_72.dll (11), boost_filesystem-vc141-mt-x64-1_72.dll (8) | `?SDFBlockToVirtualVoxelPos@HashData@@QEBA?AUint3@@AEBU2@@Z`, `?SDFBlockToWorld@HashData@@QEBA?AUfloat3@@AEBUint3@@@Z`, `?_hilbert2@Hilbert2@@AEAAXAEAPEADAEAHPEAHHHH@Z` | Geometric tracking + fusion. | 302 exports |
| `Sn3DGratingRecons.dll` | `90ba58303ddd` | DLL / native / AMD64 | 2503 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (47), commonGpu.dll (27), opencv_core348.dll (18), tbb.dll (11) | `?GetRelativeRT@CameraParam@Sn3DGratingRecons@Sn3DAlgorithm@@QEAAXQEAN000@Z`, `?IntegratImages@ScanImages@Sn3DGratingRecons@Sn3DAlgorithm@@QEAAXPEAE@Z`, `?IntegratImages@ScanImages@Sn3DGratingRecons@Sn3DAlgorithm@@QEAAXPEAEH@Z` | Grating (structured-light) reconstruction. | 41 exports |
| `Sn3DHeadSeg.dll` | `6853e404437c` | DLL / native / AMD64 | 235 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | cryptopp.dll (159), common.dll (14), boost_log-vc141-mt-x64-1_72.dll (11), boost_filesystem-vc141-mt-x64-1_72.dll (8) | `??$initialize_head_segment@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Sn3DHeadSegManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBUHeadSegOpt@1@@Z`, `??$initialize_head_segment@V?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@@Sn3DHeadSegManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@AEBUHeadSegOpt@1@@Z`, `?onnx_to_encryp_onnx_file@Sn3DHeadSeg@Sn3DAlgorithm@@QEAA?AW4RetVal@2@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@00@Z` | Head segmentation (face scanning). | 15 exports |
| `Sn3DImageQueue.dll` | `306a164f5366` | DLL / native / AMD64 | 44 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (12) | `?clearQueue@Sn3DImageQueue@sn3d_imagequeue@@QEAA?AW4Sn3DImageQueueErrorCode@2@XZ`, `?containMax@Sn3DImageQueue@sn3d_imagequeue@@QEAA_NXZ`, `?d_func@Sn3DImageQueue@sn3d_imagequeue@@AEAAPEAVSn3DImageQueuePrivate@2@XZ` | Shared image frame queue between device, calibration and scan layers. | 25 exports |
| `Sn3DInfraredCariesDet.dll` | `22a57de89b17` | DLL / native / AMD64 | 569 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | cryptopp.dll (159), glew32.dll (38), common.dll (35), OpenMeshCore.dll (16) | `??$initialize_infrared_caries_det@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Sn3DInfraredCariesDetManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBUInfraredCariesDetOpt@1@@Z`, `??$initialize_infrared_caries_det@V?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@@Sn3DInfraredCariesDetManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@AEBUInfraredCariesDetOpt@1@@Z`, `??$initialize_infrared_detection@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Sn3DInfraredDetectionManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBUInfraredDetectionOpt@1@@Z` | Infrared caries detection (dental). | 29 exports |
| `Sn3DLandmarksDetForCli.dll` | `12b2f06c2182` | DLL / native / AMD64 | 975 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | cryptopp.dll (159), common.dll (46), glew32.dll (36), opencv_core348.dll (17) | `??$landmarks_detection_forCli_model_manager_initialize@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@LandmarksDetForCliModelManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBUSn3DLandmarksDetForCliOpt@1@@Z`, `??$landmarks_detection_forCli_model_manager_initialize@V?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@@LandmarksDetForCliModelManager@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@_WU?$char_traits@_W@std@@V?$allocator@_W@2@@std@@AEBUSn3DLandmarksDetForCliOpt@1@@Z`, `??$sn3d_landmarks_detection_forCli_initialize@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Sn3DLandmarksDetForCli@Sn3DAlgorithm@@QEAA?AW4RetVal@1@AEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBUSn3DLandmarksDetForCliOpt@1@@Z` | Landmark detection for clinical use. | 11 exports |
| `Sn3DLineCode.dll` | `870a83f72c5d` | DLL / native / AMD64 | 10874 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (74), opencv_core348.dll (43), commonGpu.dll (29), opencv_imgproc348.dll (19) | `?print@LineCodeRebuildParam@Sn3DAlgorithm@@QEBAXAEAV?$basic_ofstream@DU?$char_traits@D@std@@@std@@@Z`, `?read_archive@LineCodeCalibImageLists@Sn3DAlgorithm@@QEAAXAEAVReadAble@2@@Z`, `?read_archive@LineCodeRebuildParam@Sn3DAlgorithm@@QEAAXAEAVReadAble@2@@Z` | Line-code pattern projection/decoding. | 55 exports |
| `Sn3DLog.dll` | `3c838911267b` | DLL / native / AMD64 | 78 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (66), glogc.dll (24), Sn3DBox.dll (20) | `?d_func@Sn3DLog@@QEAAPEAVSn3DLogPrivate@@XZ`, `?d_func@Sn3DLog@@QEBAPEBVSn3DLogPrivate@@XZ`, `?enableEncrypt@Sn3DLog@@QEAA?AW4Sn3DLogErrorCode@@_N@Z` | Logging front-end (thin wrapper over glogc). | 19 exports |
| `Sn3DMagic.dll` | `7e7184ef0629` | DLL / native / AMD64 | 7513 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (202), OpenMeshCore.dll (33), tbb.dll (23), VCOMP140.DLL (12) | `?CompressedBufferToMem@HashData@@QEAAXPEBEIAEBUHashParams@@II@Z`, `?RigidMatrixDevice_convert_to_RigidMatrix@Sn3DAlgorithm@@YAXAEBURigidMatrixDevice@Sn3DGPUAlgorithm@@AEAVRigidMatrix@1@@Z`, `?RigidMatrix_convert_to_RigidMatrixDevice@Sn3DAlgorithm@@YAXAEBVRigidMatrix@1@AEAURigidMatrixDevice@Sn3DGPUAlgorithm@@@Z` | 'Magic' scan mode engine (auto-alignment/cleanup). | 658 exports |
| `Sn3DManualAlign.dll` | `8fd50d4b41d0` | DLL / native / AMD64 | 372 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (148), common.dll (54), Sn3DBox.dll (21), algorithmLzy.dll (7) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Manual alignment tool. | 2 exports |
| `Sn3DMultiProjects.dll` | `b2edf26f5318` | DLL / native / AMD64 | 1897 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (218), common.dll (123), Sn3DFile.dll (81), Sn3DBox.dll (23) | `?cancelScanProjData@Sn3DProjectsManager@@QEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z`, `?checkCurPrjDevice@Sn3DProjectsManager@@IEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z`, `?clearSceneAllData@Sn3DProjectsManager@@QEAAXAEBV?$QMap@VQString@@VQVariant@@@@@Z` | Multi-project session management. | 124 exports |
| `Sn3DPassportController.dll` | `7be366063e07` | DLL / native / AMD64 | 450 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (316), libpassport.dll (35), Sn3DBox.dll (21), Sn3DQuickCloudHelper.dll (11) | `?SwitchLanguage@AppGlobal@@SA?AW4AppLanguage@1@H@Z`, `?accountBindingDevice@DogPassportMgr@@QEAAXXZ`, `?accountBindingDeviceCore@DogPassportMgr@@QEAA?AV?$QFuture@X@@XZ` | Account/passport controller (login, entitlement). | 280 exports; imports SetupAPI/CM_* (USB machine binding) |
| `Sn3DPhaseBuild.dll` | `bb39f2ade980` | DLL / native / AMD64 | 3594 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (34), commonGpu.dll (29), tbb.dll (23), opencv_core348.dll (23) | `?AddDistor@CameraParam@Sn3DPhaseBuild@Sn3DAlgorithm@@QEAAXNNAEAN0@Z`, `?CalcalateGraydiff@Sn3DPhaseBuild@Sn3DAlgorithm@@YAMPEAVScanImages@12@0V?$shared_ptr@VCamCalibParam@Sn3DPhaseBuild@Sn3DAlgorithm@@@std@@V?$shared_ptr@VPhaseParam@Sn3DPhaseBuild@Sn3DAlgorithm@@@5@W4ScanType@12@@Z`, `?CheckCudaInfo@Sn3DPhaseBuild@Sn3DAlgorithm@@YA_NXZ` | Phase/structured-light build step. | 47 exports |
| `Sn3DProcessPlugin.dll` | `e4ad081d8bfd` | DLL / native / AMD64 | 132 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (95), qttunnel.3.0.9.dll (18), Qt5Xml.dll (16), Qt5Network.dll (2) | `?connectAndRegister@processMg@Sn3DProcessMgPluginSP@@SA_NAEAVQStringList@@0@Z`, `?decrypt@ModuleInfo@Sn3DProcessMgPluginSP@@SA?AV12@AEBV?$QMap@VQString@@VQVariant@@@@@Z`, `?decryptMouduleInfo@ProcessMgXml@Sn3DProcessMgPluginSP@@CA?AVModuleInfo@2@VQStringList@@AEAH@Z` | Process/plug-in bootstrap used by EXStar.exe, scanservice.exe, FirmwareupdateUI.exe. | 51 exports |
| `Sn3DProgressHelper.dll` | `064730abccb3` | DLL / native / AMD64 | 65 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (47), Sn3DBox.dll (20), glogc.dll (7) | `?addRespTimeStamp@ProgressManager@ProgressCtrl@@QEAAXAEB_KAEBH@Z`, `?finish@ProgressManager@ProgressCtrl@@QEAAXH@Z`, `?getFinish@ProgressFlag@@QEAA_NXZ` | Progress reporting helper. | 26 exports |
| `Sn3DProjDefaultPath.dll` | `2d1dbb774cfb` | DLL / native / AMD64 | 65 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (96), Sn3DBox.dll (22), Qt5Qml.dll (2) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Default project path policy. | 2 exports |
| `Sn3DQuickCloudHelper.dll` | `1defb64a0fa8` | DLL / native / AMD64 | 352 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (128), libquickcloud-1.4.23.dll (26), glogc.dll (7), Qt5Network.dll (4) | `?Sn3DCloudDiskRequest@Sn3DQuickCloudHelper@@QEAA?AVSn3DQuickCloudHelperRespWatcher@@W4Sn3DCloudDiskInterface@QuickCloudCommon@@V?$QMap@VQString@@VQVariant@@@@11VQString@@@Z`, `?Sn3DCloudDiskRequest@Sn3DQuickCloudHelper@@QEAAXW4Sn3DCloudDiskInterface@QuickCloudCommon@@AEAV?$QMap@VQString@@VQVariant@@@@V4@22VQString@@@Z`, `?Sn3DUserAccountRequest@Sn3DQuickCloudHelper@@QEAA?AVSn3DQuickCloudHelperRespWatcher@@W4Sn3DUserAccountInterface@QuickCloudCommon@@V?$QMap@VQString@@VQVariant@@@@11VQString@@2@Z` | Quick-cloud protocol helper (libquickcloud). | 54 exports; imports libquickcloud-1.4.23 |
| `Sn3DRealtimeScan.dll` | `456e4480ded1` | DLL / native / AMD64 | 8453 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (325), Sn3DGeometricTrackFusion.dll (59), tbb.dll (31), Sn3DTextureBasedTrack.dll (25) | `??RTeethSLAM@IntraoralScan@Sn3DAlgorithm@@QEAA_NAEBVPointImage@2@_NAEAH@Z`, `??RTeethSLAM@IntraoralScan@Sn3DAlgorithm@@QEAA_NAEBVPointImage@2@_NAEAHAEA_N2PEBV?$vector@EV?$allocator@E@std@@@std@@@Z`, `?Create_Calculate_motion_handle_and_init_mesh@Sn3DAlgorithm@@YAPEAXPEAVBaseTriMesh@1@AEBV?$vector@HV?$allocator@H@std@@@std@@M@Z` | Real-time SLAM/fusion engine (largest vendor algorithm surface). | 1003 exports, imports OpenCV/TBB |
| `Sn3DRegistration.dll` | `7cea821b870e` | DLL / native / AMD64 | 9071 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (392), OpenMeshCore.dll (24), VCOMP140.DLL (15), ceres.dll (12) | `??$point2SeglineLen@M@Sn3DAlgorithm@@YANAEBV?$Matrix@M$02$00$0A@$02$00@Eigen@@00AEAV12@@Z`, `?CloudPairRegistrationWithCheck_create@Sn3DAlgorithm@@YAPEAVCloudPairRegistrationWithCheck@1@UOption@21@@Z`, `?CreateRegistrationBufferImp@Sn3DAlgorithm@@YA?AV?$shared_ptr@VRegistrationBuffer@Sn3DAlgorithm@@@std@@AEAVBuffer@Sn3DRegistration@@@Z` | Point-cloud registration / ICP engine. | 1729 exports |
| `Sn3DRestoreData.dll` | `a788d523e8f0` | DLL / native / AMD64 | 59 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (72), Sn3DBox.dll (20), Sn3DConfig.dll (7), glogc.dll (5) | `?isRestoreOK@Sn3DRestoreDataController@@QEAA_NXZ`, `?onSubscribeData@Sn3DRestoreDataController@@UEAA?AW4Sn3DErrorCode@CommonLib@@VQString@@00V?$QMap@VQString@@VQVariant@@@@V?$shared_ptr@VAbstractSerializableObject@@@std@@@Z`, `?restoreData@Sn3DRestoreDataController@@AEAA_NXZ` | Crash/session data restore. | 19 exports |
| `Sn3DScanController.dll` | `f7ac7d0bca1d` | DLL / native / AMD64 | 841 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (113), common.dll (43), Sn3DBox.dll (22), Sn3DMultiProjects.dll (14) | `?d_func@Sn3DIRSpeckleScanController@@AEAAPEAVSn3DIRSpeckleScanControllerPrivate@@XZ`, `?d_func@Sn3DIRSpeckleScanController@@AEBAPEBVSn3DIRSpeckleScanControllerPrivate@@XZ`, `?deserializeData@FrameDataObj@Sn3DFrameData@@UEAA?AW4DeserializeErrorCode@Sn3DDeserialize@@PEADH@Z` | Scan-session controller: drives the scan pipeline and publishes frame data. | 48 exports; imports Sn3DTextureSlam, Sn3DMultiProjects, Sn3DCalibDevFile |
| `Sn3DScanSlam.dll` | `efc07e5b0cff` | DLL / native / AMD64 | 531 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | boost_log-vc141-mt-x64-1_72.dll (11), common.dll (9), VCOMP140.DLL (6) | `?clean_before_restock@ReturnDataFusionUniversalScan@Sn3DSlam@Sn3DAlgorithm@@UEAAXXZ`, `?create_pipeline_handle@Sn3DAlgorithm@@YA?AV?$shared_ptr@VSlamPipeline@Sn3DSlam@Sn3DAlgorithm@@@std@@XZ`, `?edit_data@FusionAlgoManager@Sn3DSlamAlgorithmManager@Sn3DAlgorithm@@UEAA_NPEAVEditData@Sn3DSlam@3@@Z` | Scan SLAM core. | 144 exports |
| `Sn3DSecurity.dll` | `26b09c201e9e` | DLL / native / AMD64 | 5905 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (164), Sn3DBox.dll (21), Qt5Xml.dll (17), glogc.dll (8) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Licensing / anti-tamper: HASP dongle access, USB machine binding, cloud licensing, Qt plug-in. | 6.0 MB; imports DeviceIoControl; strings 'Aladdin\Vid0529&Pid0001&HaspHL', 'USB\VID_0424', 'Sn3DDongle', 'Clone detection' |
| `Sn3DSoftwareUpdate.dll` | `11a5b3e01c67` | DLL / native / AMD64 | 144 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (188), qttunnel.3.0.9.dll (22), Sn3DBox.dll (20), glogc.dll (7) | `?checkNewVersion@Sn3DSoftwareUpdateController@@AEAAXXZ`, `?copyDirectory@Sn3DSoftwareUpdateController@@AEAA_NAEBVQString@@0_N@Z`, `?copyLaunchSetupTool@Sn3DSoftwareUpdateController@@AEAAXXZ` | Application self-update: version check, download, install; talks HTTP(S). | 31 exports checkNewVersion/downloadNewApp/installNewApp |
| `Sn3DSoftwareUpgradeController.dll` | `de3e4cb33dc3` | DLL / native / AMD64 | 76 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (76), Sn3DBox.dll (21), Sn3DQuickCloudHelper.dll (10), glogc.dll (5) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Qt plug-in wrapper that runs the upgrade helper with the IPC bus. | 2 exports (qt_plugin_instance) |
| `Sn3DSparseSolver.dll` | `f0540c3e1c9d` | DLL / native / AMD64 | 68 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | mkl_rt.dll (3), common.dll (1) | `sparse_solver_get_func` | Sparse linear solver wrapper. | 1 export |
| `Sn3DSpeckle.dll` | `b2abd309df6d` | DLL / native / AMD64 | 32192 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (62), opencv_core348.dll (55), commonGpu.dll (45), tbb.dll (11) | `?CostAggregateDownToUp@globalMatchTest@Sn3DSpeckleRebuild@Sn3DAlgorithm@@AEAAXAEBV?$Matrix@H$01$00$0A@$01$00@Eigen@@HMMAEBV?$vector@MV?$allocator@M@std@@@std@@AEAV67@H@Z`, `?CostAggregateLeftToRight@globalMatchTest@Sn3DSpeckleRebuild@Sn3DAlgorithm@@AEAAXAEBV?$Matrix@H$01$00$0A@$01$00@Eigen@@HMMAEBV?$vector@MV?$allocator@M@std@@@std@@AEAV67@H@Z`, `?CostAggregateRightToLeft@globalMatchTest@Sn3DSpeckleRebuild@Sn3DAlgorithm@@AEAAXAEBV?$Matrix@H$01$00$0A@$01$00@Eigen@@HMMAEBV?$vector@MV?$allocator@M@std@@@std@@AEAV67@H@Z` | Speckle pattern matching engine. | 32 MB, 69 exports |
| `Sn3DSpeckleFusion.dll` | `26c706e05281` | DLL / native / AMD64 | 26953 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (57), opencv_core348.dll (41), commonGpu.dll (37), tbb.dll (11) | `?debug_set@Sn3DSpeckleFusion@Sn3DAlgorithm@@YA?AW4RetVal@2@PEAX_N1H@Z`, `?depthImage_generate_pointImage@Sn3DSpeckleFusion@Sn3DAlgorithm@@YA?AW4RetVal@2@AEBVDepthImage@2@AEBVCameraInParam@2@AEBV?$Matrix@H$01$00$0A@$01$00@Eigen@@2AEBV?$Matrix@M$02$00$0A@$02$00@7@AEAVPointImage@2@@Z`, `?depthMap_generate_pointImage@Sn3DSpeckleFusion@Sn3DAlgorithm@@YA?AW4RetVal@2@AEBV?$vector@MV?$allocator@M@std@@@std@@AEAVPointImage@2@AEBVCameraInParam@2@AEBV?$Matrix@H$01$00$0A@$01$00@Eigen@@3AEBV?$Matrix@M$02$00$0A@$02$00@9@1@Z` | Speckle + fusion engine. | 27 MB |
| `Sn3DStatusCache.dll` | `2a00786205d2` | DLL / native / AMD64 | 28 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | — | `?getCalirateType@Sn3DFunctionStatusCache@@QEAAHXZ`, `?getInstance@Sn3DFunctionStatusCache@@SAAEAV1@XZ`, `?getInstance@Sn3DPageStatusCache@@SAAEAV1@XZ` | Status cache for UI. | 12 exports |
| `Sn3DTexCameraHelper.dll` | `8d1e02a72dbd` | DLL / native / AMD64 | 49 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | glogc.dll (7), Qt5Core.dll (6), Sn3DConfig.dll (6), SETUPAPI.dll (4) | `?get2XTexCameraGainMaxValue@Sn3DTexCameraConfigHelper@@QEAAHH@Z`, `?get2XTexCameraGainMinValue@Sn3DTexCameraConfigHelper@@QEAAHH@Z`, `?get2XTexCameraScanGainValue@Sn3DTexCameraConfigHelper@@QEAAHH@Z` | Texture-camera configuration helper (gain/exposure/CCM suffix), enumerates camera device interfaces via SetupAPI. | 28 exports; imports SETUPAPI |
| `Sn3DTexture.dll` | `dcdd32582482` | DLL / native / AMD64 | 164 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (59), common.dll (9), Sn3DColorCorrect.dll (9), Qt5Gui.dll (8) | `?ProgressCB@Sn3DMeshTextureAlg@@CAHH@Z`, `?colorCorrect@Sn3DTexture@@UEAA_NPEAEHH0H@Z`, `?colorCorrect@Sn3DTexture@@UEAA_NPEAE_NMM11@Z` | Texture data model shared by device/scan layers. | 66 exports |
| `Sn3DTextureMapper.dll` | `8cf55f418f3c` | DLL / native / AMD64 | 83 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (95), Sn3DBox.dll (23), glogc.dll (7), Qt5Qml.dll (2) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Texture mapping/UV projection. | 2 exports |
| `Sn3DTextureSFM.dll` | `97b053d6ba40` | DLL / native / AMD64 | 6052 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (128), ceres.dll (59), OPENGL32.dll (45), glew32.dll (42) | `?check@TextureSFMManualData@sn3d_sfm@Sn3DAlgorithm@@QEAA_NXZ`, `?check_mesh@TextureSFMModel@sn3d_sfm@Sn3DAlgorithm@@QEAAPEAVCloud@3@H@Z`, `?print_out@TextureSFMParam@sn3d_sfm@Sn3DAlgorithm@@QEAAXXZ` | Structure-from-motion texture pipeline. | 74 exports |
| `Sn3DTextureSlam.dll` | `99b68e6da343` | DLL / native / AMD64 | 2596 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (104), opencv_core348.dll (24), commonGpu.dll (15), tbb.dll (13) | `??8Result@DBoW2@@QEBA_NI@Z`, `??A?$map@INU?$less@I@std@@V?$allocator@U?$pair@$$CBIN@std@@@2@@std@@QEAAAEAN$$QEAI@Z`, `??A?$map@INU?$less@I@std@@V?$allocator@U?$pair@$$CBIN@std@@@2@@std@@QEAAAEANAEBI@Z` | Texture-assisted SLAM tracking. | 449 exports |
| `Sn3DTextureUtil.dll` | `fb0139dc6fb7` | DLL / native / AMD64 | 4875 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | VCOMP140.DLL (10) | `?util_add_voc@tex_util@Sn3DAlgorithm@@YA_NPEAXAEBVSiftDatas@12@@Z`, `?util_create@tex_util@Sn3DAlgorithm@@YAPEAXXZ`, `?util_create_voc@tex_util@Sn3DAlgorithm@@YA_NPEAXAEAVSiftVocOptions@12@@Z` | Texture utility routines. | 89 exports |
| `Sn3DTurntableController.dll` | `ea47cf1cbf48` | DLL / native / AMD64 | 62 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (80), Sn3DBox.dll (21), glogc.dll (7), sn3DTurnTable.dll (5) | `qt_plugin_instance`, `qt_plugin_query_metadata` | Qt plug-in for turntable control. | 2 exports; imports sn3DTurnTable |
| `Sn3DUserPassport.dll` | `e105d45e1997` | DLL / native / AMD64 | 742 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (230), Qt5Quick.dll (30), Qt5Gui.dll (25), Sn3DBox.dll (21) | `?metaObject@PassCommonDef@@UEBAPEBUQMetaObject@@XZ`, `?qt_metacall@PassCommonDef@@UEAAHW4Call@QMetaObject@@HPEAPEAX@Z`, `?qt_metacast@PassCommonDef@@UEAAPEAXPEBD@Z` | User passport data model. | 12 exports |
| `Sn3DprocessManager.exe` | `cc54acb60b12` | EXE / native / AMD64 | 2487 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Widgets.dll (189), Qt5Core.dll (161), Sn3DProcessPlugin.dll (13), Qt5Gui.dll (9) | — | Supervisor that launches/monitors scan worker processes; uses the QtTunnel IPC bus. | imports qttunnel.3.0.9.dll, Qt5Network |
| `TestOpenglHelper.exe` | `51c6f5f30d17` | EXE / native / AMD64 | 27 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Gui.dll (8), Qt5Quick.dll (5), Qt5Core.dll (5) | — | OpenGL self-test helper. | 28 KB |
| `UsbTreeModel.dll` | `f1468c27c5ce` | DLL / native / AMD64 | 42 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | SETUPAPI.dll (10) | `?EnumAllHCD@USBTREE@@YA_NXZ`, `?EraseList@USBTREE@@YAXXZ`, `?GetSpecifyDeviceTreePath@USBTREE@@YA_NKKAEAV?$vector@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@V?$allocator@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@2@@std@@@Z` | USB topology enumeration/reset (SetupAPI + Configuration Manager + DeviceIoControl). | 4 exports: EnumAllHCD, EraseList, GetSpecifyDeviceTreePath, ResetDevice |
| `algorithm1.dll` | `52074733992e` | DLL / native / AMD64 | 7653 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (246), OpenMeshCore.dll (88), CommonFunc.dll (44), VCOMP140.DLL (13) | `?BaseTriMesh_face_and_tooth_feature_registration@Sn3DAlgorithm@@YA?AW4RetVal@1@AEBVBaseTriMesh@1@0AEBV?$vector@V?$Matrix@M$02$00$0A@$02$00@Eigen@@V?$allocator@V?$Matrix@M$02$00$0A@$02$00@Eigen@@@std@@@std@@MMMMHAEAV?$vector@VRigidMatrix@Sn3DAlgorithm@@V?$allocator@VRigidMatrix@Sn3DAlgorithm@@@std@@@5@_N@Z`, `?BaseTriMesh_group_feature_registration_auto_param@Sn3DAlgorithm@@YA?AW4RetVal@1@AEBV?$vector@PEBVBaseTriMesh@Sn3DAlgorithm@@V?$allocator@PEBVBaseTriMesh@Sn3DAlgorithm@@@std@@@std@@0AEAVRigidMatrix@1@@Z`, `?BaseTriMesh_group_feature_registration_with_multi_result_auto_param@Sn3DAlgorithm@@YA?AW4RetVal@1@AEBV?$vector@PEBVBaseTriMesh@Sn3DAlgorithm@@V?$allocator@PEBVBaseTriMesh@Sn3DAlgorithm@@@std@@@std@@0HAEAV?$vector@VRigidMatrix@Sn3DAlgorithm@@V?$allocator@VRigidMatrix@Sn3DAlgorithm@@@std@@@4@@Z` | Customer/OEM algorithm build #1. | 289 exports; Ceres/Eigen/OpenMesh symbols |
| `algorithm2.dll` | `3af8d17a78c8` | DLL / native / AMD64 | 6675 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (390), CommonFunc.dll (158), OpenMeshCore.dll (152), commonGpu.dll (23) | `?$TSS0@?1??get_adjoin_points@Sn3DRemoveOverlapDense@Sn3DAlgorithm@@AEAAXHHAEAV?$vector@HV?$allocator@H@std@@@std@@@Z@4HA`, `?$TSS0@?1??get_adjoin_points@Sn3DRemoveOverlapEx@Sn3DAlgorithm@@AEAAXHHAEAV?$vector@HV?$allocator@H@std@@@std@@@Z@4HA`, `?$TSS0@?1??get_deleted_adjoin_points@Sn3DRemoveOverlapDense@Sn3DAlgorithm@@AEAAXHHAEAV?$vector@HV?$allocator@H@std@@@std@@@Z@4HA` | Customer/OEM algorithm build #2. | 1103 exports |
| `algorithmHlj.dll` | `a33b6cb3f40d` | DLL / native / AMD64 | 4780 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (103), opencv_core348.dll (63), commonGpu.dll (50), tbb.dll (20) | `?Gaussian_core_generate@ColorSpeckle@Rebuild@Sn3DAlgorithm@@AEAAXHAEAV?$vector@MV?$allocator@M@std@@@std@@M@Z`, `?Read_Camera_Para@CaliParReadIn@Rebuild@Sn3DAlgorithm@@AEAA_NAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@0@Z`, `?Read_Camera_Para@CaliParReadIn@Rebuild@Sn3DAlgorithm@@AEAA_NAEBV?$vector@MV?$allocator@M@std@@@std@@0@Z` | OEM algorithm variant 'Hlj'. | 427 exports |
| `algorithmLy.dll` | `bdeda50a6082` | DLL / native / AMD64 | 4673 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (76), commonGpu.dll (30), opencv_core348.dll (29), tbb.dll (23) | `?Depth_to_Cloud@LY3DAlgorithm@@YA_NAEAVCloudWithWeight@Sn3DAlgorithm@@AEAVPointImage@3@AEBV?$vector@MV?$allocator@M@std@@@std@@AEBV?$Matrix@N$02$02$0A@$02$02@Eigen@@AEBV?$Matrix@N$02$00$0A@$02$00@8@AEBVCameraInParam@3@HHH_N@Z`, `?Depth_to_Cloud@LY3DAlgorithm@@YA_NV?$shared_ptr@VCloudWithWeight@Sn3DAlgorithm@@@std@@V?$shared_ptr@VPointImage@Sn3DAlgorithm@@@3@AEBV?$vector@MV?$allocator@M@std@@@3@AEBV?$Matrix@N$02$02$0A@$02$02@Eigen@@AEBV?$Matrix@N$02$00$0A@$02$00@7@AEBVCameraInParam@Sn3DAlgorithm@@HHH_N@Z`, `?GauFilter1d@ScanImages@LY3DStereoBuild@@AEAAXPEAEPEAMHH@Z` | OEM algorithm variant 'Ly'. | 120 exports |
| `algorithmLzy.dll` | `5428be44674a` | DLL / native / AMD64 | 16043 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (123), opencv_core348.dll (61), commonGpu.dll (49), opencv_imgproc348.dll (33) | `?SetZero@CameraCalibParam@Sn3DAlgorithm@@QEAAXXZ`, `?TakeHoleVoteIni@Sn3DAlgorithm@@YA?AW4MK_TRACKER_RET@1@PEAX@Z`, `?add_dis@CameraCalibParam@Sn3DAlgorithm@@QEBAXAEBV?$Matrix@N$01$00$0A@$01$00@Eigen@@AEAV34@@Z` | OEM algorithm variant 'Lzy'. | 495 exports, 16 MB |
| `algorithmZbt.dll` | `2f44852063d8` | DLL / native / AMD64 | 5054 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | common.dll (274), OpenMeshCore.dll (78), ceres.dll (12), tbb.dll (12) | `?AddNode@BeizierCurveManager@Sn3DAlgorithm@@QEAAHAEBV?$Matrix@M$02$00$0A@$02$00@Eigen@@@Z`, `?AddNodes@BeizierCurveManager@Sn3DAlgorithm@@QEAAXAEBV?$vector@V?$Matrix@M$02$00$0A@$02$00@Eigen@@V?$allocator@V?$Matrix@M$02$00$0A@$02$00@Eigen@@@std@@@std@@@Z`, `?CalcCoefficient2@BeizierCurveManager@Sn3DAlgorithm@@CAXMAEAV?$Matrix@M$0?0$00$0A@$0?0$00@Eigen@@@Z` | OEM algorithm variant 'Zbt'. | 488 exports |
| `boost_filesystem-vc141-mt-x64-1_72.dll` | `729a677de954` | DLL / native / AMD64 | 127 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | — | `??_0path@filesystem@boost@@QEAAAEAV012@AEBV012@@Z`, `??_0path@filesystem@boost@@QEAAAEAV012@PEB_W@Z`, `?absolute@filesystem@boost@@YA?AVpath@12@AEBV312@0@Z` | Boost 1.72 filesystem (vc141 toolset). | 123 exports |
| `boost_log-vc141-mt-x64-1_72.dll` | `9cf20adf5a76` | DLL / native / AMD64 | 707 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | boost_filesystem-vc141-mt-x64-1_72.dll (25), boost_thread-vc141-mt-x64-1_72.dll (13), boost_date_time-vc141-mt-x64-1_72.dll (2), Secur32.dll (1) | `??$?5DU?$char_traits@D@std@@@trivial@v2_mt_nt6@log@boost@@YAAEAV?$basic_istream@DU?$char_traits@D@std@@@std@@AEAV45@AEAW4severity_level@0123@@Z`, `??$?5_WU?$char_traits@_W@std@@@trivial@v2_mt_nt6@log@boost@@YAAEAV?$basic_istream@_WU?$char_traits@_W@std@@@std@@AEAV45@AEAW4severity_level@0123@@Z`, `??$?6DU?$char_traits@D@std@@@aux@v2_mt_nt6@log@boost@@YAAEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEAV45@AEBV?$id@Uprocess@aux@v2_mt_nt6@log@boost@@@0123@@Z` | Boost 1.72 logging. | 473 exports |
| `boost_serialization-vc141-mt-gd-x64-1_72.dll` | `c4779a531b8c` | DLL / native / AMD64 | 705 | MSVC 19.16 (VS2017 15.9) | ucrtbased.dll (29) | `??8extended_type_info@serialization@boost@@QEBA_NAEBV012@@Z`, `??Mextended_type_info@serialization@boost@@QEBA_NAEBV012@@Z`, `?BOOST_ARCHIVE_SIGNATURE@archive@boost@@YAPEBDXZ` | Boost 1.72 serialization, debug runtime (-gd). | 277 exports |
| `ceres.dll` | `eff5555ccb1a` | DLL / native / AMD64 | 2168 | MSVC 18.00 (VS2013) /MD (MSVCR120) | MSVCR120.dll (69), glog.dll (15), VCOMP120.DLL (12), libblas.dll (11) | `?AddParameterBlock@DynamicCostFunction@ceres@@UEAAXH@Z`, `?AddParameterBlock@Problem@ceres@@QEAAXPEANH@Z`, `?AddParameterBlock@Problem@ceres@@QEAAXPEANHPEAVLocalParameterization@2@@Z` | Ceres Solver (non-linear least squares) used by calibration/registration. | 285 exports, ceres:: symbols |
| `common.dll` | `f72360f41363` | DLL / native / AMD64 | 5642 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | OpenMeshCore.dll (136), boost_log-vc141-mt-x64-1_72.dll (42), lib3MF.dll (40), tbb.dll (23) | `??8McEdge@Sn3DAlgorithm@@QEBA_NAEBU01@@Z`, `??ABitFlagVec@Sn3DAlgorithm@@QEBA_N_K@Z`, `??AVector3hf@half_float@Sn3DAlgorithm@@QEAAAEAVhalf@12@H@Z` | Geometry/mesh core used across the scan pipeline. | 2669 exports |
| `cryptopp.dll` | `e777bcaf846e` | DLL / native / AMD64 | 1698 | MSVC 19.16 (VS2017 15.9) | — | `??$IntToString@VInteger@CryptoPP@@@CryptoPP@@YA?AV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@VInteger@0@I@Z`, `??$IntToString@_K@CryptoPP@@YA?AV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@_KI@Z`, `??5CryptoPP@@YAAEAV?$basic_istream@DU?$char_traits@D@std@@@std@@AEAV12@AEAVInteger@0@@Z` | Crypto++ 5.x/6.x static-export DLL. | 4200 exports |
| `cudart64_102.dll` | `d01f98eccc63` | DLL / native / AMD64 | 412 | MSVC 17.00 (VS2012) | — | `NvOptimusEnablementCuda`, `__cudaInitModule`, `__cudaPopCallConfiguration` | NVIDIA CUDA 10.2.89 runtime. | 320 exports; string 'NVIDIA CUDA 10.2.89 Runtime' |
| `cusolver64_10.dll` | `71d6bce330f8` | DLL / native / AMD64 | 184415 | MSVC 17.00 (VS2012) | — | `NvOptimusEnablementCuda`, `cusolverDnCCgesv`, `cusolverDnCCgesv_bufferSize` | CUDA 10.2 cuSOLVER (dense/sparse solves). | 830 exports, 188 MB |
| `dynamixel.dll` | `ba6b7b405a30` | DLL / native / AMD64 | 95 | MSVC 14.00 (VS2005) | — | `dxl_get_highbyte`, `dxl_get_lowbyte`, `dxl_get_result` | ROBOTIS Dynamixel servo SDK (turntable motor). | 21 exports |
| `einscan_net_svr.exe` | `b596ac1472ec` | EXE / native / AMD64 | 41 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (51), glogc.dll (5), logiclayer.dll (1) | — | Legacy Einscan network server. | 42 KB |
| `freetype.dll` | `afd35aa5a582` | DLL / native / AMD64 | 606 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | libpng16.dll (23), zlib1.dll (4), libbz2.dll (3) | `FTC_CMapCache_Lookup`, `FTC_CMapCache_New`, `FTC_ImageCache_Lookup` | FreeType font rasteriser. | 299 exports |
| `glew32.dll` | `5f7663319b41` | DLL / native / AMD64 | 396 | MSVC 18.00 (VS2013) | OPENGL32.dll (4) | `__GLEW_3DFX_multisample`, `__GLEW_3DFX_tbuffer`, `__GLEW_3DFX_texture_compression_FXT1` | GLEW OpenGL loader. | 3199 exports |
| `glfw3.dll` | `e65beb338daf` | DLL / native / AMD64 | 93 | MSVC 18.00 (VS2013) /MD (MSVCR120) | MSVCR120.dll (30) | `glfwCreateCursor`, `glfwCreateStandardCursor`, `glfwCreateWindow` | GLFW window/context library. | 110 exports |
| `glogc.dll` | `c18b7d2ca4d2` | DLL / native / AMD64 | 157 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | dbghelp.dll (5) | `??$MakeCheckOpValueString@C@google@@YAXPEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEBC@Z`, `??$MakeCheckOpValueString@D@google@@YAXPEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEBD@Z`, `??$MakeCheckOpValueString@E@google@@YAXPEAV?$basic_ostream@DU?$char_traits@D@std@@@std@@AEBE@Z` | Google-glog logging runtime (custom build, C suffix). | 187 exports |
| `hasp_windows_x64_105562.dll` | `d0d99fd06c28` | DLL / native / AMD64 | 4861 | MSVC 14.00 (VS2005) | — | `API_HL_CSTACK`, `HLM_CHECKALLSLOTS`, `HLM_CHECKCOUNTER` | Aladdin/SafeNet HASP SRM runtime (dongle licensing). | 77 exports, version 105562 |
| `haspvlib_105562.dll` | `6371c4d36af8` | DLL / native / I386 | 1072 | MSVC 13.10 (VS2003) | — | `vli_get_version`, `vli_init`, `vli_shutdown` | HASP vendor library (dongle driver interface). | 3 exports |
| `informationCollect.exe` | `122e11f0d876` | EXE / native / AMD64 | 54 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (73), glogc.dll (5), Sn3DLog.dll (4), Sn3DProcessPlugin.dll (3) | — | Diagnostics collection helper. | information_api/informationFileCollect |
| `lib3MF.dll` | `c81a0154f276` | DLL / native / AMD64 | 630 | MSVC 18.00 (VS2013) /MD (MSVCR120) | MSVCR120.dll (53), OLEAUT32.dll (1), XmlLite.dll (1), SHLWAPI.dll (1) | `lib3mf_attachment_getpath`, `lib3mf_attachment_getpathutf8`, `lib3mf_attachment_getrelationshiptype` | lib3MF (3D Manufacturing Format) reader/writer. | 219 exports, NMR:: symbols |
| `libEGL.dll` | `06961b71a7c0` | DLL / native / AMD64 | 22 | MSVC 18.00 (VS2013) /MD (MSVCR120) | libGLESv2.dll (54), MSVCR120.dll (13) | `eglBindAPI`, `eglBindTexImage`, `eglChooseConfig` | ANGLE EGL. | 54 exports |
| `libGLESV2.dll` | `bb9eb97a2acf` | DLL / native / AMD64 | 2486 | MSVC 18.00 (VS2013) /MD (MSVCR120) | MSVCR120.dll (73), d3d9.dll (5) | `?ActiveTexture@gl@@YAXI@Z`, `?AttachShader@gl@@YAXII@Z`, `?BeginQuery@gl@@YAXII@Z` | ANGLE GLESv2. | 665 exports |
| `libcrypto-1_1-x64.dll` | `e8150325b863` | DLL / native / AMD64 | 2781 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | bcrypt.dll (1) | `ACCESS_DESCRIPTION_free`, `ACCESS_DESCRIPTION_it`, `ACCESS_DESCRIPTION_new` | OpenSSL 1.1.1d libcrypto (TLS + hashing for update/cloud). | 4388 exports; banner OpenSSL 1.1.1d |
| `libcurl.dll` | `384986486b24` | DLL / native / AMD64 | 346 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | WLDAP32.dll (16) | `curl_easy_cleanup`, `curl_easy_duphandle`, `curl_easy_escape` | libcurl HTTP(S) client (updates/cloud). | 74 exports |
| `libpassport.dll` | `80038debc336` | DLL / native / AMD64 | 441 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (253), qttunnel.3.0.9.dll (23), Qt5Network.dll (6) | `?base64_decode@EDData@Sn3DPassPort@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBV34@@Z`, `?base64_encode@EDData@Sn3DPassPort@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@PEBEI@Z`, `?communityConnect@InitialWorker@Sn3DPassPort@@QEAAXXZ` | Passport/auth client library. | 91 exports |
| `libpng16.dll` | `c70dd9c0468d` | DLL / native / AMD64 | 198 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | zlib1.dll (12) | `png_access_version_number`, `png_benign_error`, `png_build_grayscale_palette` | libpng 1.6. | 246 exports |
| `libpq.dll` | `1d473683b4df` | DLL / native / AMD64 | 193 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | SSLEAY32.dll (30), LIBEAY32.dll (28), Secur32.dll (5) | `IN6_IS_ADDR_UNSPECIFIED`, `PQbackendPID`, `PQbinaryTuples` | PostgreSQL client library. | 315 exports |
| `librenderkit.dll` | `e9648b641e7e` | DLL / native / AMD64 | 3108 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | osg158-osg.dll (1075), Qt5Gui.dll (224), Qt5Core.dll (208), osg158-osgGA.dll (121) | `??8DeviceAction@@QEBA_NAEBU0@@Z`, `??MDeviceAction@@QEBA_NAEBU0@@Z`, `??REnableCutSSBOOperation@RenderKit@Sn3D@@UEAAXPEAVObject@osg@@@Z` | Vendor render kit (3D viewport) built on OSG. | 1899 exports |
| `librenderkit.plugin.cuda.dll` | `0b0ea743d55b` | DLL / native / AMD64 | 448 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | — | `NvOptimusEnablementCuda`, `cudaDeviceSynchronizeWrapper`, `cudaFreeWrapper` | CUDA plug-in for the render kit. | 10 exports |
| `libssl-1_1-x64.dll` | `e864a08f4cc5` | DLL / native / AMD64 | 674 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | libcrypto-1_1-x64.dll (434) | `BIO_f_ssl`, `BIO_new_buffer_ssl_connect`, `BIO_new_ssl` | OpenSSL 1.1.1d libssl. | 495 exports |
| `mkl_rt.dll` | `fcf5a1282958` | DLL / native / AMD64 | 13395 | MSVC 18.00 (VS2013) | — | `CAXPBY`, `CAXPY`, `CAXPYI` | Intel MKL single dynamic library (BLAS/LAPACK/FFT). | 15435 exports |
| `networktunnel.dll` | `8118187fb68d` | DLL / native / AMD64 | 109 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (163), Qt5Network.dll (54) | `?connectRemoteService@TCPMemoryManager@NetworkTunnel@@QEAAXAEBVQHostAddress@@G@Z`, `?connectRemoted@TCPMemoryManager@NetworkTunnel@@QEAAXVQHostAddress@@GVQString@@@Z`, `?copyImageData@DataBlockImages@NetworkTunnel@@QEAA_NHIPEAEI@Z` | Socket transport for the tunnel layer (release build). | 50 exports |
| `networktunneld.dll` | `970abae7bcc0` | DLL / native / AMD64 | 292 | MSVC 19.16 (VS2017 15.9) | Qt5Cored.dll (187), Qt5Networkd.dll (54), ucrtbased.dll (25) | `?connectRemoteService@TCPMemoryManager@NetworkTunnel@@QEAAXAEBVQHostAddress@@G@Z`, `?connectRemoted@TCPMemoryManager@NetworkTunnel@@QEAAXVQHostAddress@@GVQString@@@Z`, `?copyImageData@DataBlockImages@NetworkTunnel@@QEAA_NHIPEAEI@Z` | Debug build of networktunnel. | 50 exports |
| `nppc64_10.dll` | `bcaa3c5aa2f2` | DLL / native / AMD64 | 239 | MSVC 17.00 (VS2012) | — | `Event`, `NvOptimusEnablementCuda`, `Stream` | CUDA NPP core image primitives. | 15 exports |
| `nppig64_10.dll` | `c33afc18ca50` | DLL / native / AMD64 | 25170 | MSVC 17.00 (VS2012) | nppc64_10.dll (1) | `NvOptimusEnablementCuda`, `nppiGetAffineBound`, `nppiGetAffineQuad` | CUDA NPP image geometry transforms. | 706 exports, 25 MB |
| `nvidia-smi.exe` | `5ad9ad17e06e` | EXE / native / AMD64 | 669 | MSVC 19.27 (VS2019 16.5) | — | — | NVIDIA driver management binary (CUDA stack). | NVIDIA |
| `opencv_calib3d348.dll` | `69cbebc42731` | DLL / native / AMD64 | 2292 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | opencv_core348.dll (268), opencv_features2d348.dll (52), opencv_imgproc348.dll (25), opencv_flann348.dll (16) | `??8MatSize@cv@@QEBA_NAEBU01@@Z`, `??8RNG@cv@@QEBA_NAEBV01@@Z`, `??9MatSize@cv@@QEBA_NAEBU01@@Z` | OpenCV 3.4.8 calibration. | 924 exports |
| `opencv_core348.dll` | `0bc36bf9ff0f` | DLL / native / AMD64 | 13939 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | cudart64_102.dll (43), CONCRT140.dll (23) | `??6cv@@YAAEAVFileStorage@0@AEAV10@AEBVString@0@@Z`, `??8MatSize@cv@@QEBA_NAEBU01@@Z`, `??8RNG@cv@@QEBA_NAEBV01@@Z` | OpenCV 3.4.8 core. | 2457 exports; build banner MSVC 19.14 |
| `opencv_cudawarping348.dll` | `c93974783186` | DLL / native / AMD64 | 62253 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | opencv_core348.dll (141), nppig64_10.dll (57), cudart64_102.dll (16), nppc64_10.dll (2) | `??8MatSize@cv@@QEBA_NAEBU01@@Z`, `??8RNG@cv@@QEBA_NAEBV01@@Z`, `??9MatSize@cv@@QEBA_NAEBU01@@Z` | OpenCV 3.4.8 CUDA warping. | 681 exports, 63 MB |
| `opencv_features2d348.dll` | `bfd2019ff5dc` | DLL / native / AMD64 | 879 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | opencv_core348.dll (184), opencv_flann348.dll (23), opencv_imgproc348.dll (17) | `??8MatSize@cv@@QEBA_NAEBU01@@Z`, `??8RNG@cv@@QEBA_NAEBV01@@Z`, `??9MatSize@cv@@QEBA_NAEBU01@@Z` | OpenCV 3.4.8 feature detection. | 897 exports |
| `opencv_imgproc348.dll` | `ca207d2716d1` | DLL / native / AMD64 | 5721 | MSVC 19.14 (VS2017 15.7) /MD (UCRT) | opencv_core348.dll (371) | `??8MatSize@cv@@QEBA_NAEBU01@@Z`, `??8RNG@cv@@QEBA_NAEBV01@@Z`, `??9MatSize@cv@@QEBA_NAEBU01@@Z` | OpenCV 3.4.8 image processing. | 1023 exports |
| `opengl32sw.dll` | `199546a0b51a` | DLL / native / AMD64 | 20443 | MSVC 19.00 (VS2015) | imagehlp.dll (8) | `DrvCopyContext`, `DrvCreateContext`, `DrvCreateLayerContext` | Mesa/llvmpipe software OpenGL fallback. | 1274 exports |
| `osg158-osg.dll` | `2fb9be451c89` | DLL / native / AMD64 | 2859 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | OPENGL32.dll (103), ot21-OpenThreads.dll (29) | `??8Matrixd@osg@@QEBA_NAEBV01@@Z`, `??8Matrixf@osg@@QEBA_NAEBV01@@Z`, `??8Plane@osg@@QEBA_NAEBV01@@Z` | OpenSceneGraph 3.6.3 core (SOVERSION 158). | 7545 exports; PDB E:\OSG\…-3.6.3 |
| `osg158-osgDB.dll` | `43cf92729808` | DLL / native / AMD64 | 1018 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | osg158-osg.dll (500), ot21-OpenThreads.dll (21), osg158-osgUtil.dll (13), zlib.dll (6) | `??5InputStream@osgDB@@QEAAAEAV01@AEAC@Z`, `??5InputStream@osgDB@@QEAAAEAV01@AEAD@Z`, `??5InputStream@osgDB@@QEAAAEAV01@AEAE@Z` | OpenSceneGraph scene I/O plug-in host. | 1328 exports |
| `osg158-osgUtil.dll` | `3a7a329b75fd` | DLL / native / AMD64 | 1431 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | osg158-osg.dll (1335), OPENGL32.dll (20), ot21-OpenThreads.dll (5) | `??8Edge@EdgeCollector@osgUtil@@QEBA_NAEBU012@@Z`, `??9Edge@EdgeCollector@osgUtil@@QEBA_NAEBU012@@Z`, `??MEdge@EdgeCollector@osgUtil@@QEBA_NAEBU012@@Z` | OpenSceneGraph utilities. | 1731 exports |
| `osg158-osgViewer.dll` | `350e3710b674` | DLL / native / AMD64 | 784 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | osg158-osg.dll (887), osg158-osgUtil.dll (102), osg158-osgGA.dll (81), osg158-osgDB.dll (44) | `??RRenderer@osgViewer@@UEAAXPEAVGraphicsContext@osg@@@Z`, `??RRenderer@osgViewer@@UEAAXPEAVObject@osg@@@Z`, `??RWriteToFile@ScreenCaptureHandler@osgViewer@@UEAAXAEBVImage@osg@@I@Z` | OpenSceneGraph viewer. | 879 exports |
| `polyWorkModeler.exe` | `efe4d0684bbd` | EXE / native / AMD64 | 1904 | MSVC 19.16 (VS2017 15.9) | — | — | Mesh modelling tool (polygon work). | CUI |
| `pskill.exe` | `309eefcf9681` | EXE / native / I386 | 272 | MSVC 18.00 (VS2013) | VERSION.dll (6), MPR.dll (2), COMDLG32.dll (1) | — | Sysinternals PsKill (process termination helper). | Sysinternals |
| `qttunnel.3.0.9.dll` | `2f41faf1c374` | DLL / native / AMD64 | 498 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (377), Qt5Network.dll (41), tunnel_encrypt.dll (7), IPHLPAPI.DLL (1) | `??8Frame@QMQTT@@QEBA_NAEBV01@@Z`, `??8Message@QMQTT@@QEBA_NAEBV01@@Z`, `??9Frame@QMQTT@@QEBA_NAEBV01@@Z` | QtTunnel + QMQTT IPC bus: shared-memory segments, routers, module/object remoting. | 406 exports, QtTunnel::Module/Object, QMQTT::Client |
| `scanhub.exe` | `87be3b2aa2fb` | EXE / native / AMD64 | 229 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | — | `mosquitto_broker_publish`, `mosquitto_broker_publish_copy`, `mosquitto_callback_register` | Legacy (2021) network hub for scan devices (WS2_32 sockets). | imports WS2_32, CreateFileA |
| `scanservice.exe` | `d8d90f9a516a` | EXE / native / AMD64 | 66 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (66), glogc.dll (5), Qt5Gui.dll (3), Sn3DLog.dll (3) | — | Per-scan worker process spawned by the process manager; hosts a process plug-in. | imports Sn3DProcessPlugin + Sn3DLog; 2024-06 build |
| `sn3DCommunity.exe` | `1541d705ac95` | EXE / native / AMD64 | 27744 | link 3.0 | — | — | Community/cloud client shell. | 28 MB |
| `sn3DHIDCommunication.dll` | `27b99f62d782` | DLL / native / AMD64 | 41 | MSVC 18.00 (VS2013) /MD (MSVCR120) | MSVCR120.dll (31), HID.DLL (7), SETUPAPI.dll (4) | `?sn3DClearBuffer@sn3DHIDCommunication@@YAXPEAX@Z`, `?sn3DEnumSpecifiedUSBDevice@sn3DHIDCommunication@@YA_NAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@0AEAH@Z`, `?sn3DExitHidDev@sn3DHIDCommunication@@YA_NPEAX@Z` | HID transport: enumerate/open HID devices, Get/SetReport, overlapped read/write. | 15 exports; imports HID.DLL, SETUPAPI |
| `sn3DLock.dll` | `c4d6fd5d43b9` | DLL / native / AMD64 | 334 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | hasp_windows_x64_105562.dll (10), WSOCK32.dll (9), freetype.dll (1), libpq.dll (1) | `?GetDongleKeyID@sn3DLock@@YA_NHHAEAHPEAD@Z`, `ApplyLicense`, `CheckTimeProduct` | File/process lock helper (DeviceIoControl on volume handles). | 13 exports |
| `sn3DTurnTable.dll` | `8ed7fe074f53` | DLL / native / AMD64 | 2666 | MSVC 19.16 (VS2017 15.9) | gdiplus.dll (22), UxTheme.dll (12), OLEAUT32.dll (12), SiUSBXp.dll (8) | `?CloseTurntable@sn3DTurnTable@@YAHXZ`, `?CloseTurntables@sn3DTurnTable@@YAHXZ`, `?CompeleteCurCmd@sn3DTurnTable@@YA_NE@Z` | Turntable driver: Silicon Labs USBXpress (SiUSBXp) and HID transports, Dynamixel servo library. | 24 exports GetPCBVersion; imports SiUSBXp.dll, sn3DHIDCommunication.dll, dynamixel.dll |
| `softwareUpgrade.exe` | `d3e79c57e24f` | EXE / native / AMD64 | 38 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (46), glogc.dll (5), Sn3DLog.dll (4), Sn3DProcessPlugin.dll (1) | — | Small helper executable that performs an upgrade step and reports through the tunnel topics. | strings 'Sn3DShareMemReqTopic', 'password', 'c5msnsync'; imports Sn3DProcessPlugin |
| `sqlitecipher.dll` | `464647f9270d` | DLL / native / AMD64 | 1432 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (116), Qt5Sql.dll (81) | `qt_plugin_instance`, `qt_plugin_query_metadata`, `sqlite3_carray_init` | SQLCipher build of SQLite (encrypted config/app DBs). | 7 exports |
| `tbb.dll` | `aaeff9bb9f0a` | DLL / native / AMD64 | 382 | MSVC 18.00 (VS2013) /MD (MSVCR120) | MSVCR120.dll (59) | `?NFS_Allocate@internal@tbb@@YAPEAX_K0PEAX@Z`, `?NFS_Free@internal@tbb@@YAXPEAX@Z`, `?NFS_GetLineSize@internal@tbb@@YA_KXZ` | Intel Threading Building Blocks runtime. | 229 exports |
| `tunnel_encrypt.dll` | `91edf7e6dd70` | DLL / native / AMD64 | 46 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (53) | `?decrypt@TunnelEncrypt@Encrypt@QtTunnel@@QEAA?AVQByteArray@@AEBV4@0@Z`, `?decrypt@TunnelEncrypt@Encrypt@QtTunnel@@QEAA?AVQByteArray@@AEBV4@@Z`, `?decrypt_json@TunnelEncrypt@Encrypt@QtTunnel@@QEAA?AVQJsonObject@@AEBVQByteArray@@0@Z` | Payload encryption for tunnel traffic. | 19 exports, imported by qttunnel |
| `uninst.exe` | `20ec8ea1b72c` | EXE / native / I386 | 514 | MSVC 16.00 (VS2010) | COMCTL32.dll (4), VERSION.dll (3) | — | Installer/uninstaller stub. | GUI |
| `vs_mesh_loader.exe` | `ce6ca757e4ac` | EXE / native / I386 | 60 | link 48.0 | mscoree.dll (1) | — | Mesh loading test harness. | 61 KB |
| `zlib1.dll` | `f2a2eae0d036` | DLL / native / AMD64 | 94 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | — | `adler32`, `adler32_combine`, `adler32_z` | zlib 1.2.x. | 84 exports |
| `syncservice/CommunicateLayer.dll` | `7554c8768321` | DLL / native / AMD64 | 65 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | SETUPAPI.dll (4) | `?attachDevice@CommunicateLayer@@YAKAEAPEAXV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z`, `?commandChannelLock@CommunicateLayer@@YAKPEAX_N@Z`, `?dataStreamlock@CommunicateLayer@@YAKPEAX_N@Z` | Vendor USB transport: initializeSystem(GUID), attachDevice(serial), pipe addresses, bulk reply, stream switch. | 29 exports; SETUPAPI + DeviceIoControl |
| `syncservice/SnSyncCore.dll` | `9319387a7e00` | DLL / native / AMD64 | 135 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (111), SnSyncTool.dll (4) | `?connectDeviceByIndex@DeviceController@SnSyncCore@@QEAAI_JH@Z`, `?connectDeviceBySerial@DeviceController@SnSyncCore@@QEAAI_JAEBVQString@@@Z`, `?createDevice@DeviceController@SnSyncCore@@QEAA?AU?$QPair@I_J@@AEBVQString@@AEBVQVersionNumber@@@Z` | Device/service core: DeviceController create/connect/discardFrames. | 71 exports |
| `syncservice/SnSyncService.exe` | `ae1c99f5c7af` | EXE / native / AMD64 | 504 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (215), qttunnel.3.0.9.dll (20), SnSyncTool.dll (11), SnSyncCore.dll (7) | — | Out-of-process scan-device service that owns the actual USB device handles. | spawned by Sn3DDeviceEinStar; imports SnSyncCore/SnSyncTool/SnSharedBlock |
| `syncservice/sn3DCommunicateLayerM.dll` | `9f79c4e92528` | DLL / native / AMD64 | 68 | MSVC 19.16 (VS2017 15.9) /MD (UCRT) | Qt5Core.dll (16), SETUPAPI.dll (4) | `?attachDevice@CommunicateLayer@@YAKAEAPEAXV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z`, `?commandChannelLock@CommunicateLayer@@YAKPEAX_N@Z`, `?dataStreamlock@CommunicateLayer@@YAKPEAX_N@Z` | Variant of CommunicateLayer with auto-reconnect. | 30 exports |

## 16. Limitations and open questions

1. **No execution, no debugging**: runtime behaviour (which plug-in is actually loaded for a given scanner model, which USB PID Einstar enumerates) is inferred, not observed.
2. **No MSVC demangler offline** (`c++filt`/`llvm-cxxfilt` are Itanium-only), so export lists are verbatim mangled names; the readable summaries were written by reading the name tokens, not by a tool.
3. **Firmware images and `Fx3Bootloader.exe` are absent** from this installation; the update payload path is therefore a hypothesis grounded in strings only.
4. **`Sn3DSecurity.dll` (6 MB) was not decompiled**: the HASP/machine-binding claims rest on imports, exports and strings.
5. **`algorithm*.dll` variants** (`Hlj`, `Ly`, `Lzy`, `Zbt`) are almost certainly customer/OEM builds; the mapping to customers is unknown.
6. **Dynamic edges** in `module_dependencies.json` are string references (a module naming a DLL), which is evidence of a possible `LoadLibrary`, not proof of loading.

## Appendix A — full inventory index

All 741 modules with digest prefix, type, size, linker version, import/export counts and the categories assigned in `classification.json`.

| module | sha256 | type | KB | link | imp DLLs | exp | categories |
|---|---|---|---|---|---|---|---|
| `7z.dll` | `5b3ef4bbbc` | DLL | 1680 | 8.0 | 5 | 13 |  |
| `7z.exe` | `27d1d66bfd` | EXE | 533 | 8.0 | 5 | 0 | executable,usb_hid |
| `AppCommon_E7.dll` | `c09a53ea8a` | DLL | 2493 | 14.16 | 9 | 49 | qt |
| `AppUi.dll` | `bf2f01b771` | DLL | 1193 | 14.16 | 16 | 9 | qt |
| `ApplicationTools.dll` | `373143151b` | DLL | 124 | 14.16 | 12 | 9 | qt |
| `BasiceComponents.dll` | `b7186b83c2` | DLL | 4414 | 14.16 | 6 | 2 | qt |
| `BizUiCommon.dll` | `44cc82f2e8` | DLL | 159 | 14.16 | 9 | 2 | qt |
| `CalibrationPage_EA.dll` | `5efe74fb26` | DLL | 2655 | 14.16 | 11 | 2 | calibration,qt |
| `ClientImpLayer.dll` | `fa16d1103a` | DLL | 169 | 14.16 | 13 | 2 | qt |
| `CommonCfg.dll` | `489f964e52` | DLL | 96 | 14.16 | 9 | 18 | qt |
| `CommonFunc.dll` | `9427a6fedb` | DLL | 5217 | 14.16 | 20 | 1487 | gpu_compute,vision_recon |
| `CompositeComponents.dll` | `ea03f4220e` | DLL | 4140 | 14.16 | 6 | 2 | qt |
| `D3Dcompiler_47.dll` | `dee3ddffb9` | DLL | 4071 | 11.0 | 4 | 29 | crypto,usb_hid |
| `Deserialize.dll` | `b2663812ce` | DLL | 160 | 14.16 | 10 | 87 | qt |
| `EXStar.exe` | `2fb0092b14` | EXE | 340 | 14.16 | 16 | 1 | executable,qt |
| `FirmwareupdateUI.exe` | `fefd7ce955` | EXE | 2604 | 14.16 | 16 | 0 | executable,firmware_update,qt |
| `FrameDataObj.dll` | `686c454c0e` | DLL | 125 | 14.16 | 12 | 20 | qt,vision_recon |
| `GenerateDataProcess.dll` | `f1416ab3bb` | DLL | 964 | 14.16 | 27 | 67 | qt,vision_recon |
| `GlobalRegistrationProcess.dll` | `65148338a2` | DLL | 195 | 14.16 | 13 | 30 | qt |
| `LIBEAY32.dll` | `a9dd2dc3f3` | DLL | 2239 | 14.16 | 18 | 3709 | crypto |
| `LaunchTool.exe` | `bfaa033f96` | EXE | 4130 | 14.16 | 17 | 0 | executable |
| `MarkdownConverter/MarkdownConverter.exe` | `2c39375757` | EXE | 2077 | 12.0 | 7 | 0 | executable |
| `Measurement.dll` | `4356ced3bc` | DLL | 590 | 14.16 | 25 | 17 | qt,vision_recon |
| `MeasurementUI.dll` | `f07590ab28` | DLL | 231 | 14.16 | 8 | 2 | qt |
| `ModeChoosePage.dll` | `61b3271aa5` | DLL | 189 | 14.16 | 10 | 9 | qt |
| `OpenMeshCore.dll` | `737021682c` | DLL | 474 | 14.14 | 10 | 783 | vision_recon |
| `OpenMeshTools.dll` | `be9bb91a77` | DLL | 56 | 14.14 | 8 | 116 | vision_recon |
| `OpenOrCreateController.dll` | `64beef518c` | DLL | 356 | 14.16 | 16 | 9 | qt,vision_recon |
| `PnPutil.exe` | `345c298340` | EXE | 304 | 14.20 | 43 | 0 | executable,usb_hid |
| `PostProcess.dll` | `22964a59a3` | DLL | 1337 | 14.16 | 32 | 9 | qt,vision_recon |
| `PostProcessUI.dll` | `e1144eb8ee` | DLL | 830 | 14.16 | 14 | 7 | qt |
| `PrestartCheck.dll` | `4c4ebba497` | DLL | 423 | 14.16 | 15 | 2 | qt |
| `PreviewTool.exe` | `3867e342b0` | EXE | 5344 | 14.16 | 24 | 0 | executable,qt |
| `Qt/labs/animation/labsanimationplugin.dll` | `cec3ecc168` | DLL | 43 | 14.16 | 7 | 2 | qt |
| `Qt/labs/animation/labsanimationplugind.dll` | `898758b1bc` | DLL | 148 | 14.16 | 5 | 2 | qt |
| `Qt/labs/calendar/qtlabscalendarplugin.dll` | `e76937946c` | DLL | 100 | 14.16 | 10 | 2 | qt |
| `Qt/labs/calendar/qtlabscalendarplugind.dll` | `6186bd8161` | DLL | 404 | 14.16 | 8 | 2 | qt |
| `Qt/labs/folderlistmodel/qmlfolderlistmodelplugin.dll` | `e3776ca8c1` | DLL | 66 | 14.16 | 7 | 2 | qt |
| `Qt/labs/folderlistmodel/qmlfolderlistmodelplugind.dll` | `b8d1a2e71d` | DLL | 193 | 14.16 | 5 | 2 | qt |
| `Qt/labs/location/locationlabsplugin.dll` | `e832a020d3` | DLL | 54 | 14.16 | 8 | 2 | qt |
| `Qt/labs/location/locationlabsplugind.dll` | `8289fd3a41` | DLL | 268 | 14.16 | 7 | 2 | qt |
| `Qt/labs/lottieqt/lottieqtplugin.dll` | `7dbe9299fb` | DLL | 88 | 14.16 | 9 | 2 | qt |
| `Qt/labs/lottieqt/lottieqtplugind.dll` | `426c9d170c` | DLL | 261 | 14.16 | 8 | 2 | qt |
| `Qt/labs/platform/qtlabsplatformplugin.dll` | `474342ecee` | DLL | 233 | 14.16 | 11 | 2 | qt |
| `Qt/labs/platform/qtlabsplatformplugind.dll` | `8430b585d5` | DLL | 796 | 14.16 | 9 | 2 | qt |
| `Qt/labs/qmlmodels/labsmodelsplugin.dll` | `bfa2dc4f23` | DLL | 125 | 14.16 | 8 | 2 | qt |
| `Qt/labs/qmlmodels/labsmodelsplugind.dll` | `e6620c966d` | DLL | 365 | 14.16 | 6 | 2 | qt |
| `Qt/labs/settings/qmlsettingsplugin.dll` | `cb88c081e9` | DLL | 45 | 14.16 | 7 | 2 | qt |
| `Qt/labs/settings/qmlsettingsplugind.dll` | `cbf7796e42` | DLL | 131 | 14.16 | 5 | 2 | qt |
| `Qt/labs/sharedimage/sharedimageplugin.dll` | `3d7afc6928` | DLL | 46 | 14.16 | 8 | 2 | qt |
| `Qt/labs/sharedimage/sharedimageplugind.dll` | `0f1b0c6821` | DLL | 138 | 14.16 | 7 | 2 | qt |
| `Qt/labs/wavefrontmesh/qmlwavefrontmeshplugin.dll` | `51539130d7` | DLL | 51 | 14.16 | 8 | 2 | qt |
| `Qt/labs/wavefrontmesh/qmlwavefrontmeshplugind.dll` | `26d8a946f5` | DLL | 161 | 14.16 | 7 | 2 | qt |
| `Qt5Concurrent.dll` | `be264d9929` | DLL | 36 | 14.16 | 5 | 33 | qt |
| `Qt5Core.dll` | `e6c1dc3f88` | DLL | 8130 | 14.16 | 24 | 8368 | qt,usb_hid |
| `Qt5Gui.dll` | `d9f35e2e1f` | DLL | 7060 | 14.16 | 17 | 16563 | gpu_compute,qt |
| `Qt5Multimedia.dll` | `1756a6c1f1` | DLL | 733 | 14.16 | 10 | 2268 | qt |
| `Qt5MultimediaQuick.dll` | `fbbbfb7647` | DLL | 133 | 14.16 | 9 | 86 | qt |
| `Qt5Network.dll` | `c05967ffdb` | DLL | 1331 | 14.16 | 17 | 1506 | crypto,qt |
| `Qt5Qml.dll` | `12e4f15f84` | DLL | 3735 | 14.16 | 12 | 5358 | qt |
| `Qt5QmlModels.dll` | `c78a552af5` | DLL | 440 | 14.16 | 9 | 369 | qt |
| `Qt5QmlWorkerScript.dll` | `fb21145b3f` | DLL | 64 | 14.16 | 9 | 4 | qt |
| `Qt5Quick.dll` | `3fa257ba46` | DLL | 4187 | 14.16 | 15 | 6775 | qt |
| `Qt5QuickControls2.dll` | `a0fa00fb2b` | DLL | 178 | 14.16 | 12 | 356 | qt |
| `Qt5QuickShapes.dll` | `f0c768a896` | DLL | 228 | 14.16 | 9 | 179 | qt |
| `Qt5QuickTemplates2.dll` | `6a4569cab4` | DLL | 1125 | 14.16 | 11 | 3426 | qt |
| `Qt5RemoteObjects.dll` | `b1d1a3e632` | DLL | 493 | 14.16 | 8 | 305 | qt |
| `Qt5Sql.dll` | `f644d603a7` | DLL | 212 | 14.16 | 5 | 506 | qt |
| `Qt5Svg.dll` | `46c97add86` | DLL | 337 | 14.16 | 9 | 612 | qt |
| `Qt5Widgets.dll` | `69b2b47910` | DLL | 5500 | 14.16 | 16 | 9054 | qt |
| `Qt5Xml.dll` | `50066bf78d` | DLL | 216 | 14.16 | 7 | 446 | qt |
| `QtGraphicalEffects/private/qtgraphicaleffectsprivate.dll` | `7c3fc8dce1` | DLL | 66 | 14.16 | 9 | 2 | qt |
| `QtGraphicalEffects/private/qtgraphicaleffectsprivated.dll` | `866f3618d3` | DLL | 200 | 14.16 | 7 | 2 | qt |
| `QtGraphicalEffects/qtgraphicaleffectsplugin.dll` | `89059a665b` | DLL | 68 | 14.16 | 6 | 2 | qt |
| `QtGraphicalEffects/qtgraphicaleffectsplugind.dll` | `019418ff1c` | DLL | 130 | 14.16 | 5 | 2 | qt |
| `QtMultimedia/declarative_multimedia.dll` | `7ec8f89549` | DLL | 272 | 14.16 | 12 | 2 | qt |
| `QtQml/Models.2/modelsplugin.dll` | `e5a1122cb0` | DLL | 27 | 14.16 | 7 | 2 | qt |
| `QtQml/Models.2/modelsplugind.dll` | `8e8b195c0e` | DLL | 78 | 14.16 | 6 | 2 | qt |
| `QtQml/RemoteObjects/qtqmlremoteobjects.dll` | `fc8e49bf21` | DLL | 35 | 14.16 | 7 | 2 | qt |
| `QtQml/RemoteObjects/qtqmlremoteobjectsd.dll` | `79dcecdb30` | DLL | 103 | 14.16 | 6 | 2 | qt |
| `QtQml/StateMachine/qtqmlstatemachine.dll` | `0af7f92c0e` | DLL | 77 | 14.16 | 7 | 2 | qt |
| `QtQml/StateMachine/qtqmlstatemachined.dll` | `a0279d2380` | DLL | 298 | 14.16 | 5 | 2 | qt |
| `QtQml/WorkerScript.2/workerscriptplugin.dll` | `7963e93225` | DLL | 27 | 14.16 | 7 | 2 | qt |
| `QtQml/WorkerScript.2/workerscriptplugind.dll` | `92b1e41b3e` | DLL | 78 | 14.16 | 6 | 2 | qt |
| `QtQml/qmlplugin.dll` | `714bb32aa1` | DLL | 27 | 14.16 | 7 | 2 | qt |
| `QtQml/qmlplugind.dll` | `6804bc08c2` | DLL | 81 | 14.16 | 6 | 2 | qt |
| `QtQuick.2/qtquick2plugin.dll` | `ae02f2b0c7` | DLL | 28 | 14.16 | 9 | 2 | qt |
| `QtQuick.2/qtquick2plugind.dll` | `eca468d87f` | DLL | 94 | 14.16 | 8 | 2 | qt |
| `QtQuick/Controls.2/Fusion/qtquickcontrols2fusionstyleplugin.dll` | `808f2e7d70` | DLL | 183 | 14.16 | 10 | 2 | qt |
| `QtQuick/Controls.2/Fusion/qtquickcontrols2fusionstyleplugind.dll` | `480377f469` | DLL | 341 | 14.16 | 9 | 2 | qt |
| `QtQuick/Controls.2/Imagine/qtquickcontrols2imaginestyleplugin.dll` | `12d5292f9b` | DLL | 1124 | 14.16 | 11 | 2 | qt |
| `QtQuick/Controls.2/Imagine/qtquickcontrols2imaginestyleplugind.dll` | `31ba345fc4` | DLL | 1551 | 14.16 | 9 | 2 | qt |
| `QtQuick/Controls.2/Material/qqc2materialstyleplugin.dll` | `35b08de45d` | DLL | 248 | 14.16 | 11 | 2 | qt |
| `QtQuick/Controls.2/Material/qqc2materialstyleplugind.dll` | `e2ef942be4` | DLL | 506 | 14.16 | 9 | 2 | qt |
| `QtQuick/Controls.2/Universal/qtquickcontrols2universalstyleplugin.dll` | `de3ef054ae` | DLL | 195 | 14.16 | 10 | 2 | qt |
| `QtQuick/Controls.2/Universal/qtquickcontrols2universalstyleplugind.dll` | `0a0c58275e` | DLL | 444 | 14.16 | 9 | 2 | qt |
| `QtQuick/Controls.2/qtquickcontrols2plugin.dll` | `b6cc48ac96` | DLL | 257 | 14.16 | 11 | 2 | qt |
| `QtQuick/Controls.2/qtquickcontrols2plugind.dll` | `efe601b468` | DLL | 595 | 14.16 | 9 | 2 | qt |
| `QtQuick/Controls/Styles/Flat/qtquickextrasflatplugin.dll` | `4f559e98af` | DLL | 788 | 14.16 | 8 | 4 | qt |
| `QtQuick/Controls/Styles/Flat/qtquickextrasflatplugind.dll` | `66d7be366e` | DLL | 1018 | 14.16 | 7 | 4 | qt |
| `QtQuick/Controls/qtquickcontrolsplugin.dll` | `d0b0493ecd` | DLL | 339 | 14.16 | 10 | 2 | qt |
| `QtQuick/Controls/qtquickcontrolsplugind.dll` | `d5949d0a83` | DLL | 1105 | 14.16 | 8 | 2 | qt |
| `QtQuick/Dialogs/Private/dialogsprivateplugin.dll` | `4e673f4e0c` | DLL | 55 | 14.16 | 8 | 2 | qt |
| `QtQuick/Dialogs/Private/dialogsprivateplugind.dll` | `b3eb9776c4` | DLL | 158 | 14.16 | 6 | 2 | qt |
| `QtQuick/Dialogs/dialogplugin.dll` | `b82f9deeb5` | DLL | 141 | 14.16 | 8 | 2 | qt |
| `QtQuick/Dialogs/dialogplugind.dll` | `fc02a906b0` | DLL | 627 | 14.16 | 7 | 2 | qt |
| `QtQuick/Extras/qtquickextrasplugin.dll` | `19797d57b8` | DLL | 80 | 14.16 | 9 | 2 | qt |
| `QtQuick/Extras/qtquickextrasplugind.dll` | `d0918bf2ea` | DLL | 250 | 14.16 | 7 | 2 | qt |
| `QtQuick/Layouts/qquicklayoutsplugin.dll` | `fde83ef232` | DLL | 114 | 14.16 | 10 | 2 | qt |
| `QtQuick/Layouts/qquicklayoutsplugind.dll` | `9d6cf322d8` | DLL | 482 | 14.16 | 7 | 2 | qt |
| `QtQuick/LocalStorage/qmllocalstorageplugin.dll` | `245a7bf33f` | DLL | 60 | 14.16 | 7 | 2 | qt |
| `QtQuick/LocalStorage/qmllocalstorageplugind.dll` | `8eeddb8bed` | DLL | 195 | 14.16 | 6 | 2 | qt |
| `QtQuick/Particles.2/particlesplugin.dll` | `9817c66275` | DLL | 28 | 14.16 | 7 | 2 | qt |
| `QtQuick/Particles.2/particlesplugind.dll` | `9e88382b9c` | DLL | 79 | 14.16 | 6 | 2 | qt |
| `QtQuick/Pdf/pdfplugin.dll` | `acfb29d1fb` | DLL | 130 | 14.16 | 10 | 2 | qt |
| `QtQuick/Pdf/pdfplugind.dll` | `bfb09e28d9` | DLL | 391 | 14.16 | 8 | 2 | qt |
| `QtQuick/PrivateWidgets/widgetsplugin.dll` | `b5a3e7dd2d` | DLL | 130 | 14.16 | 9 | 2 | qt |
| `QtQuick/PrivateWidgets/widgetsplugind.dll` | `d65a93cf51` | DLL | 478 | 14.16 | 8 | 2 | qt |
| `QtQuick/Scene2D/qtquickscene2dplugin.dll` | `059eecacf3` | DLL | 32 | 14.16 | 9 | 2 | qt |
| `QtQuick/Scene2D/qtquickscene2dplugind.dll` | `a8fd80a9d2` | DLL | 113 | 14.16 | 8 | 2 | qt |
| `QtQuick/Scene3D/qtquickscene3dplugin.dll` | `97c708b234` | DLL | 100 | 14.16 | 13 | 2 | qt |
| `QtQuick/Scene3D/qtquickscene3dplugind.dll` | `561f778f65` | DLL | 338 | 14.16 | 12 | 2 | qt |
| `QtQuick/Shapes/qmlshapesplugin.dll` | `213a294fd8` | DLL | 28 | 14.16 | 7 | 2 | qt |
| `QtQuick/Shapes/qmlshapesplugind.dll` | `bd0d6c1e53` | DLL | 95 | 14.16 | 6 | 2 | qt |
| `QtQuick/Templates.2/qtquicktemplates2plugin.dll` | `64c1d86aa5` | DLL | 328 | 14.16 | 9 | 2 | qt |
| `QtQuick/Templates.2/qtquicktemplates2plugind.dll` | `e947cae31c` | DLL | 835 | 14.16 | 8 | 2 | qt |
| `QtQuick/Timeline/qtquicktimelineplugin.dll` | `285e6f1a0c` | DLL | 66 | 14.16 | 8 | 2 | qt |
| `QtQuick/Timeline/qtquicktimelineplugind.dll` | `88a3fe5620` | DLL | 346 | 14.16 | 6 | 2 | qt |
| `QtQuick/VirtualKeyboard/Settings/qtquickvirtualkeyboardsettingsplugin.dll` | `2de36b54df` | DLL | 33 | 14.16 | 7 | 2 | qt |
| `QtQuick/VirtualKeyboard/Settings/qtquickvirtualkeyboardsettingsplugind.dll` | `30f659e6be` | DLL | 99 | 14.16 | 6 | 2 | qt |
| `QtQuick/VirtualKeyboard/Styles/qtquickvirtualkeyboardstylesplugin.dll` | `b4aea09d9b` | DLL | 59 | 14.16 | 9 | 2 | qt |
| `QtQuick/VirtualKeyboard/Styles/qtquickvirtualkeyboardstylesplugind.dll` | `2b86d66fef` | DLL | 134 | 14.16 | 8 | 2 | qt |
| `QtQuick/VirtualKeyboard/qtquickvirtualkeyboardplugin.dll` | `d46d74609a` | DLL | 61 | 14.16 | 7 | 2 | qt |
| `QtQuick/VirtualKeyboard/qtquickvirtualkeyboardplugind.dll` | `2fa002434a` | DLL | 174 | 14.16 | 6 | 2 | qt |
| `QtQuick/Window.2/windowplugin.dll` | `5d7a75e7a6` | DLL | 55 | 14.16 | 8 | 2 | qt |
| `QtQuick/Window.2/windowplugind.dll` | `4b7e212daf` | DLL | 203 | 14.16 | 7 | 2 | qt |
| `QtQuick/XmlListModel/qmlxmllistmodelplugin.dll` | `987da6407f` | DLL | 85 | 14.16 | 9 | 2 | qt |
| `QtQuick/XmlListModel/qmlxmllistmodelplugind.dll` | `a0fbeacbb1` | DLL | 246 | 14.16 | 7 | 2 | qt |
| `QuickCloudCfg/plugins/autoUpgrade.dll` | `83ad81763c` | DLL | 283 | 14.16 | 7 | 2 | qt |
| `QuickCloudCfg/plugins/qcServer.dll` | `2062bfbe76` | DLL | 418 | 14.16 | 11 | 2 | qt |
| `QuickCloudCfg/plugins/startDownload.dll` | `e4f44d058d` | DLL | 241 | 14.16 | 7 | 2 | qt |
| `QuickCloudCfg/plugins/startUpload.dll` | `8e56c98453` | DLL | 299 | 14.16 | 11 | 2 | qt |
| `RebuildProcess.dll` | `f8f701a602` | DLL | 625 | 14.16 | 20 | 9 | qt,vision_recon |
| `Release/libcurl.dll` | `664a40490c` | DLL | 346 | 14.16 | 15 | 74 | crypto |
| `Release/libpng.dll` | `6f69372a10` | DLL | 199 | 14.16 | 10 | 246 |  |
| `Release/osg158-osg.dll` | `a270b6f149` | DLL | 2859 | 14.16 | 13 | 7545 | gpu_compute |
| `Release/osg158-osgDB.dll` | `2246ddf041` | DLL | 1018 | 14.16 | 14 | 1328 | gpu_compute |
| `Release/osg158-osgFX.dll` | `4034f4c5ca` | DLL | 220 | 14.16 | 12 | 229 | gpu_compute |
| `Release/osg158-osgGA.dll` | `2962e4cabc` | DLL | 343 | 14.16 | 12 | 929 | gpu_compute |
| `Release/osg158-osgManipulator.dll` | `3d5ff75cf8` | DLL | 293 | 14.16 | 9 | 546 | gpu_compute |
| `Release/osg158-osgParticle.dll` | `e0ac481880` | DLL | 325 | 14.16 | 12 | 666 | gpu_compute |
| `Release/osg158-osgShadow.dll` | `bca632d207` | DLL | 522 | 14.16 | 14 | 580 | gpu_compute |
| `Release/osg158-osgText.dll` | `53b2780993` | DLL | 359 | 14.16 | 13 | 392 | gpu_compute |
| `Release/osg158-osgUtil.dll` | `fa54e156f6` | DLL | 1431 | 14.16 | 13 | 1731 | gpu_compute |
| `Release/osg158-osgViewer.dll` | `2067040453` | DLL | 784 | 14.16 | 17 | 879 | gpu_compute |
| `Release/osgPlugins-3.6.3/osgdb_3dc.dll` | `4f10779df9` | DLL | 75 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_3ds.dll` | `e004e089c4` | DLL | 296 | 14.16 | 12 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_ac.dll` | `d4ec4cec36` | DLL | 187 | 14.16 | 12 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_bmp.dll` | `586ff20ac1` | DLL | 50 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_bsp.dll` | `bb89d1b320` | DLL | 218 | 14.16 | 13 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_bvh.dll` | `380e59bd6b` | DLL | 121 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_cfg.dll` | `1b29e9cb00` | DLL | 126 | 14.16 | 14 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_curl.dll` | `4ece5b4745` | DLL | 94 | 14.16 | 14 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_dds.dll` | `eed446ec74` | DLL | 72 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osg.dll` | `2c933067ca` | DLL | 604 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osganimation.dll` | `c8a1f925e7` | DLL | 176 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgfx.dll` | `4af06efbec` | DLL | 61 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgparticle.dll` | `908eb8d175` | DLL | 167 | 14.16 | 11 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgshadow.dll` | `632858d38d` | DLL | 39 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgsim.dll` | `e3506d6670` | DLL | 98 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgterrain.dll` | `dbe87e6a97` | DLL | 85 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgtext.dll` | `e63c2b57fb` | DLL | 59 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgviewer.dll` | `4809f7381b` | DLL | 52 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgvolume.dll` | `8104e1e10b` | DLL | 89 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_deprecated_osgwidget.dll` | `5d99940ee0` | DLL | 73 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_dot.dll` | `b2c878dd6d` | DLL | 71 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_dxf.dll` | `7be49df5ff` | DLL | 232 | 14.16 | 12 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_freetype.dll` | `dfcb3315af` | DLL | 631 | 14.16 | 14 | 122 |  |
| `Release/osgPlugins-3.6.3/osgdb_gif.dll` | `cbe473a56e` | DLL | 60 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_gles.dll` | `5274958b5d` | DLL | 632 | 14.16 | 12 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_glsl.dll` | `f70fb50ce2` | DLL | 62 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_gz.dll` | `502fc72e22` | DLL | 62 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_hdr.dll` | `9a93849111` | DLL | 64 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_ive.dll` | `54373e3acb` | DLL | 728 | 14.16 | 16 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_jpeg.dll` | `9436aa94d5` | DLL | 306 | 14.16 | 11 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_ktx.dll` | `3a86034dcc` | DLL | 61 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_logo.dll` | `7520534e87` | DLL | 80 | 14.16 | 12 | 0 | gpu_compute |
| `Release/osgPlugins-3.6.3/osgdb_lua.dll` | `ab749438f8` | DLL | 453 | 14.16 | 17 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_lwo.dll` | `6a7fbd3de3` | DLL | 314 | 14.16 | 13 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_lws.dll` | `9ac4fdadb0` | DLL | 105 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_md2.dll` | `460f427055` | DLL | 84 | 14.16 | 11 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_mdl.dll` | `da7c7907fb` | DLL | 129 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_normals.dll` | `442b472f44` | DLL | 91 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_obj.dll` | `38617153b1` | DLL | 202 | 14.16 | 13 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_openflight.dll` | `827fb80cfd` | DLL | 458 | 14.16 | 12 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osc.dll` | `f5fa51aacd` | DLL | 202 | 14.16 | 14 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osg.dll` | `e4f9c4331b` | DLL | 164 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osga.dll` | `87a77de69c` | DLL | 92 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osgjs.dll` | `b9f3b51cfc` | DLL | 396 | 14.16 | 16 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osgshadow.dll` | `ed37b025a4` | DLL | 56 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osgterrain.dll` | `cf9b6e3ee6` | DLL | 54 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osgtgz.dll` | `31cd71ae2c` | DLL | 48 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_osgviewer.dll` | `20c20b85c4` | DLL | 56 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_p3d.dll` | `8c3a1a9c44` | DLL | 266 | 14.16 | 13 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_pic.dll` | `d1159e4dff` | DLL | 41 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_ply.dll` | `67c7c40cfb` | DLL | 102 | 14.16 | 12 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_png.dll` | `bdff6492fa` | DLL | 60 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_pnm.dll` | `0b9812e0f1` | DLL | 66 | 14.16 | 11 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_pov.dll` | `44d1dbfadb` | DLL | 91 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_pvr.dll` | `f2aa83de10` | DLL | 55 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_revisions.dll` | `4206707035` | DLL | 64 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_rgb.dll` | `bc3450ff53` | DLL | 53 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_rot.dll` | `ca30755506` | DLL | 49 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_scale.dll` | `23811dfb3a` | DLL | 49 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osg.dll` | `cb6d484d4a` | DLL | 1264 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osganimation.dll` | `d7d1510001` | DLL | 405 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgfx.dll` | `19dfde7931` | DLL | 76 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgga.dll` | `e039d66e4a` | DLL | 121 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgmanipulator.dll` | `797b59746e` | DLL | 115 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgparticle.dll` | `e8f902e0a1` | DLL | 251 | 14.16 | 11 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgshadow.dll` | `cef66c9202` | DLL | 131 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgsim.dll` | `79ac37728d` | DLL | 181 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgterrain.dll` | `8541be6465` | DLL | 144 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgtext.dll` | `b0ffaca5ee` | DLL | 112 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgui.dll` | `20c83bb233` | DLL | 186 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgutil.dll` | `ffa02c002d` | DLL | 32 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgviewer.dll` | `f716978939` | DLL | 74 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_serializers_osgvolume.dll` | `7a0354e361` | DLL | 181 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_shp.dll` | `154ae4fb59` | DLL | 153 | 14.16 | 13 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_stl.dll` | `3cd40954ee` | DLL | 109 | 14.16 | 13 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_tf.dll` | `72a05fdbec` | DLL | 61 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_tga.dll` | `4affcf3592` | DLL | 45 | 14.16 | 8 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_tgz.dll` | `1a30b48f05` | DLL | 51 | 14.16 | 11 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_trans.dll` | `6a60c3fd7e` | DLL | 49 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_trk.dll` | `1942c9c022` | DLL | 78 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_txf.dll` | `d2d068d2b3` | DLL | 58 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_txp.dll` | `ac4233166d` | DLL | 527 | 14.16 | 16 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_vtf.dll` | `1eac198971` | DLL | 51 | 14.16 | 9 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_x.dll` | `d6dad2bcda` | DLL | 122 | 14.16 | 10 | 0 |  |
| `Release/osgPlugins-3.6.3/osgdb_zip.dll` | `be9939bc46` | DLL | 122 | 14.16 | 12 | 0 |  |
| `Release/ot21-OpenThreads.dll` | `3dd4e1eea9` | DLL | 44 | 14.16 | 7 | 85 | gpu_compute |
| `Release/zlib.dll` | `2da9e484ac` | DLL | 93 | 14.16 | 6 | 84 |  |
| `Resource/driver/FX3/WdfCoinstaller01011.dll` | `f6fb39a857` | DLL | 1754 | 10.10 | 7 | 6 | usb_hid |
| `Resource/driver/UEC/PsExec64.exe` | `2c5817a56e` | EXE | 1053 | 14.16 | 9 | 0 | crypto,executable |
| `Resource/driver/UEC/SHCtl.exe` | `45f5720482` | EXE | 19 | 14.16 | 11 | 0 | executable,usb_hid |
| `Resource/driver/UEC/WdfCoinstaller01011.dll` | `a97e8eaa74` | DLL | 1762 | 10.10 | 7 | 6 | usb_hid |
| `Resource/driver/UEC/xdevcon.exe` | `a69f5577b5` | EXE | 82 | 9.0 | 7 | 0 | executable,usb_hid |
| `Resource/runtime/mfc120.dll` | `556442cbd5` | DLL | 5477 | 12.0 | 10 | 9297 |  |
| `Resource/runtime/mfc120u.dll` | `38437948c1` | DLL | 5503 | 12.0 | 10 | 9302 |  |
| `Resource/runtime/msvcp120.dll` | `2abf0aab5a` | DLL | 645 | 12.0 | 2 | 1569 |  |
| `Resource/runtime/msvcp140_1.dll` | `18aef42187` | DLL | 23 | 14.29 | 5 | 7 |  |
| `Resource/runtime/msvcr100.dll` | `ae3cb6c6af` | DLL | 810 | 10.0 | 1 | 1591 |  |
| `Resource/runtime/msvcr120.dll` | `ad32240bb1` | DLL | 941 | 12.0 | 1 | 1925 |  |
| `Resource/runtime/vcamp120.dll` | `ef3f1a0fa0` | DLL | 471 | 12.0 | 7 | 165 | crypto,gpu_compute |
| `Resource/runtime/vcomp120.dll` | `ae710d6670` | DLL | 134 | 12.0 | 2 | 113 |  |
| `Resource/runtime/vcomp140.dll` | `54fe6b0875` | DLL | 177 | 14.32 | 1 | 113 |  |
| `Resource/runtime/vcredist2013EN_x64.exe` | `e554425243` | EXE | 7026 | 10.0 | 14 | 0 | crypto,executable |
| `Resource/runtime/vcredist2015.exe` | `5eea714e1f` | EXE | 14230 | 10.0 | 15 | 0 | crypto,executable |
| `Resource/runtime/vcredist_x64.exe` | `cc7ec04421` | EXE | 10033 | 7.10 | 7 | 0 | crypto,executable,usb_hid |
| `Resource/runtime/vcredist_x64_VS2017.exe` | `5b0cbb977f` | EXE | 14953 | 14.0 | 8 | 0 | crypto,executable |
| `S3D_Teamviewer.exe` | `2be1d96ed0` | EXE | 24247 | 6.0 | 7 | 0 | executable |
| `SSLEAY32.dll` | `9a2c6a64a7` | DLL | 364 | 14.16 | 7 | 321 | crypto |
| `ScanAlignFusionProcess.dll` | `a31c489a10` | DLL | 1724 | 14.16 | 29 | 9 | qt,vision_recon |
| `ScanEditProcess.dll` | `c6ca46922e` | DLL | 832 | 14.16 | 25 | 9 | qt,vision_recon |
| `ScanPage_E7.dll` | `5c83dd51a0` | DLL | 2649 | 14.16 | 14 | 9 | qt |
| `SendDataToClientController.dll` | `783d031682` | DLL | 341 | 14.16 | 13 | 2 | qt |
| `Serialize.dll` | `1e35762fbb` | DLL | 176 | 14.16 | 12 | 86 | qt |
| `SiUSBXp.dll` | `33958f5ae5` | DLL | 92 | 10.0 | 4 | 24 | usb_hid |
| `Sn3D/Digit/Global/Sn3DDigitGlobal.dll` | `a12d39440d` | DLL | 37 | 14.16 | 6 | 5 | qt,vision_recon |
| `Sn3D/Digit/SceneMgr/pluginsn3d.digit.scenemgr.dll` | `92ea5401bf` | DLL | 39 | 14.16 | 8 | 2 | qt |
| `Sn3D/Digit/Ui/Sn3DDigitUi.dll` | `2fa8bb330a` | DLL | 41458 | 14.16 | 14 | 2 | qt,vision_recon |
| `Sn3D/Navigation/pluginSn3DNavigation.dll` | `6c8cab29fa` | DLL | 792 | 14.16 | 6 | 2 | qt |
| `Sn3D/RenderKit/Extensions/Digit/pluginrenderkit.extensions.digit.dll` | `d3fd0c780e` | DLL | 58 | 14.16 | 11 | 2 | qt |
| `Sn3D/RenderKit/pluginrenderkit.dll` | `09d20a8d27` | DLL | 61 | 14.16 | 9 | 2 | qt |
| `Sn3D/UI/Sn3DFileDialog/Sn3DFileDialog.dll` | `6429723eaa` | DLL | 218 | 14.16 | 11 | 10 | qt,vision_recon |
| `Sn3DAIOutUnity.dll` | `44d02d0986` | DLL | 92 | 14.16 | 8 | 33 | vision_recon |
| `Sn3DAVXImp.dll` | `19a1aa2919` | DLL | 162 | 14.16 | 8 | 3 | gpu_compute,vision_recon |
| `Sn3DAlgorithmWrapper.dll` | `5ba900e272` | DLL | 1730 | 14.16 | 31 | 147 | qt,vision_recon |
| `Sn3DAutoExposureHelper.dll` | `0baacce4cf` | DLL | 63 | 14.16 | 9 | 19 | calibration,qt,vision_recon |
| `Sn3DBox.dll` | `5d2e53988c` | DLL | 371 | 14.16 | 17 | 166 | qt,vision_recon |
| `Sn3DCalibBigCirExtract.dll` | `ae024c5086` | DLL | 491 | 14.16 | 3 | 6 | calibration,vision_recon |
| `Sn3DCalibCaptureImageProcess.dll` | `b018f4a269` | DLL | 448 | 14.16 | 16 | 103 | calibration,qt,vision_recon |
| `Sn3DCalibDevFile.dll` | `c9258681f8` | DLL | 116 | 14.16 | 12 | 26 | calibration,qt,vision_recon |
| `Sn3DCalibrateAlgorithmHelper.dll` | `6e6a3b7bd7` | DLL | 84 | 14.16 | 9 | 10 | calibration,qt,vision_recon |
| `Sn3DCalibrateAlgorithmWrapper.dll` | `f1696b6e73` | DLL | 570 | 14.16 | 27 | 129 | calibration,qt,vision_recon |
| `Sn3DCalibrateData.dll` | `d0773e02cc` | DLL | 135 | 14.16 | 12 | 29 | calibration,qt,vision_recon |
| `Sn3DCalibrateImageInfo.dll` | `bafbee6802` | DLL | 67 | 14.16 | 12 | 18 | calibration,qt,vision_recon |
| `Sn3DCalibrateLoader.dll` | `01d8d93b57` | DLL | 130 | 14.16 | 8 | 58 | calibration,qt,vision_recon |
| `Sn3DCalibrationJR.dll` | `2f14ad6f15` | DLL | 6030 | 14.16 | 23 | 790 | calibration,gpu_compute,vision_recon |
| `Sn3DCameraCalibHelper.dll` | `c7bc073cd4` | DLL | 181 | 14.16 | 17 | 33 | calibration,qt,vision_recon |
| `Sn3DCloudDisk.dll` | `04305616fe` | DLL | 247 | 14.16 | 13 | 9 | qt,vision_recon |
| `Sn3DCloudProcessGPU.dll` | `02a17a13b0` | DLL | 650 | 14.16 | 15 | 3 | gpu_compute,vision_recon |
| `Sn3DCodeProject.dll` | `fc116d4b79` | DLL | 265 | 14.16 | 16 | 85 | qt,vision_recon |
| `Sn3DColorCorrect.dll` | `1d50e0abf6` | DLL | 1333 | 14.16 | 20 | 64 | gpu_compute,vision_recon |
| `Sn3DConfig.dll` | `9f7cc77911` | DLL | 53 | 14.16 | 7 | 15 | qt,vision_recon |
| `Sn3DCork.dll` | `6f7fd29e84` | DLL | 1869 | 14.16 | 17 | 244 | gpu_compute,vision_recon |
| `Sn3DCrypto.dll` | `b0fb2568cb` | DLL | 312 | 14.16 | 14 | 52 | crypto,vision_recon |
| `Sn3DDental.dll` | `8f47fd23d0` | DLL | 6316 | 14.16 | 38 | 893 | calibration,gpu_compute,vision_recon |
| `Sn3DDentalAI.dll` | `6f7da84a89` | DLL | 2943 | 14.16 | 28 | 357 | crypto,gpu_compute,vision_recon |
| `Sn3DDentalBoxDet.dll` | `d8cd8668b0` | DLL | 166 | 14.16 | 12 | 9 | vision_recon |
| `Sn3DDentalMeshParameterize.dll` | `0401f8897f` | DLL | 27 | 14.16 | 6 | 4 | vision_recon |
| `Sn3DDentalOralCls.dll` | `b22a84a7af` | DLL | 292 | 14.16 | 16 | 22 | crypto,vision_recon |
| `Sn3DDeveloperControlPlugin.dll` | `fbf07ed434` | DLL | 68 | 14.16 | 9 | 30 | qt,vision_recon |
| `Sn3DDeviceEinStar.dll` | `d419882dd9` | DLL | 353 | 14.16 | 20 | 187 | calibration,qt,vision_recon |
| `Sn3DDigitGlobal.dll` | `5182357330` | DLL | 37 | 14.16 | 6 | 5 | qt,vision_recon |
| `Sn3DDigital.dll` | `7e39a9444a` | DLL | 5583 | 14.16 | 38 | 996 | calibration,gpu_compute,vision_recon |
| `Sn3DEACalibrateController.dll` | `1e3d33af7f` | DLL | 294 | 14.16 | 22 | 9 | calibration,qt,vision_recon |
| `Sn3DEinStarController.dll` | `eda4caa4b2` | DLL | 246 | 14.16 | 11 | 2 | qt,vision_recon |
| `Sn3DErrorCode.dll` | `21639e1e5b` | DLL | 52 | 14.16 | 7 | 18 | qt,vision_recon |
| `Sn3DFaceScan.dll` | `cc1734f3b2` | DLL | 5420 | 14.16 | 17 | 15 | gpu_compute,vision_recon |
| `Sn3DFaceUnity.dll` | `980327633f` | DLL | 981 | 14.16 | 21 | 60 | gpu_compute,vision_recon |
| `Sn3DFile.dll` | `4ec0420487` | DLL | 1369 | 14.16 | 21 | 184 | qt,vision_recon |
| `Sn3DFrameDataController.dll` | `e5a2e84bbe` | DLL | 89 | 14.16 | 13 | 29 | qt,vision_recon |
| `Sn3DFrameDataQueue.dll` | `be68e85737` | DLL | 46 | 14.16 | 8 | 25 | qt,vision_recon |
| `Sn3DFrameDeletor.dll` | `485b846b33` | DLL | 114 | 14.16 | 12 | 21 | qt,vision_recon |
| `Sn3DGeometricTrackFusion.dll` | `fc3acc0035` | DLL | 1782 | 14.16 | 19 | 302 | gpu_compute,vision_recon |
| `Sn3DGratingRecons.dll` | `90ba58303d` | DLL | 2503 | 14.16 | 19 | 41 | gpu_compute,vision_recon |
| `Sn3DHeadSeg.dll` | `6853e40443` | DLL | 235 | 14.16 | 14 | 15 | crypto,vision_recon |
| `Sn3DImageQueue.dll` | `306a164f53` | DLL | 44 | 14.16 | 6 | 25 | qt,vision_recon |
| `Sn3DInfraredCariesDet.dll` | `22a57de89b` | DLL | 569 | 14.16 | 21 | 29 | crypto,gpu_compute,vision_recon |
| `Sn3DLandmarksDetForCli.dll` | `12b2f06c21` | DLL | 975 | 14.16 | 22 | 11 | crypto,gpu_compute,vision_recon |
| `Sn3DLineCode.dll` | `870a83f72c` | DLL | 10874 | 14.16 | 21 | 55 | gpu_compute,vision_recon |
| `Sn3DLog.dll` | `3c83891126` | DLL | 78 | 14.16 | 14 | 19 | qt,vision_recon |
| `Sn3DMagic.dll` | `7e7184ef06` | DLL | 7513 | 14.16 | 23 | 658 | calibration,gpu_compute,vision_recon |
| `Sn3DManualAlign.dll` | `8fd50d4b41` | DLL | 372 | 14.16 | 17 | 2 | qt,vision_recon |
| `Sn3DMultiProjects.dll` | `b2edf26f53` | DLL | 1897 | 14.16 | 26 | 124 | qt,vision_recon |
| `Sn3DPassportController.dll` | `7be366063e` | DLL | 450 | 14.16 | 19 | 280 | crypto,qt,usb_hid,vision_recon |
| `Sn3DPhaseBuild.dll` | `bb39f2ade9` | DLL | 3594 | 14.16 | 19 | 47 | gpu_compute,vision_recon |
| `Sn3DProcessPlugin.dll` | `e4ad081d8b` | DLL | 132 | 14.16 | 9 | 51 | qt,vision_recon |
| `Sn3DProgressHelper.dll` | `064730abcc` | DLL | 65 | 14.16 | 9 | 26 | qt,vision_recon |
| `Sn3DProjDefaultPath.dll` | `2d1dbb774c` | DLL | 65 | 14.16 | 9 | 2 | qt,vision_recon |
| `Sn3DQuickCloudHelper.dll` | `1defb64a0f` | DLL | 352 | 14.16 | 12 | 54 | qt,vision_recon |
| `Sn3DRealtimeScan.dll` | `456e4480de` | DLL | 8453 | 14.16 | 24 | 1003 | gpu_compute,vision_recon |
| `Sn3DRegistration.dll` | `7cea821b87` | DLL | 9071 | 14.16 | 20 | 1729 | gpu_compute,vision_recon |
| `Sn3DRestoreData.dll` | `a788d523e8` | DLL | 59 | 14.16 | 11 | 19 | qt,vision_recon |
| `Sn3DScanController.dll` | `f7ac7d0bca` | DLL | 841 | 14.16 | 21 | 48 | calibration,qt,vision_recon |
| `Sn3DScanSlam.dll` | `efc07e5b0c` | DLL | 531 | 14.16 | 13 | 144 | vision_recon |
| `Sn3DSecurity.dll` | `26b09c201e` | DLL | 5905 | 14.16 | 12 | 3 | crypto,qt,usb_hid,vision_recon |
| `Sn3DSoftwareUpdate.dll` | `11a5b3e01c` | DLL | 144 | 14.16 | 13 | 31 | firmware_update,qt,vision_recon |
| `Sn3DSoftwareUpgradeController.dll` | `de3e4cb33d` | DLL | 76 | 14.16 | 10 | 2 | qt,vision_recon |
| `Sn3DSparseSolver.dll` | `f0540c3e1c` | DLL | 68 | 14.16 | 8 | 1 | gpu_compute,vision_recon |
| `Sn3DSpeckle.dll` | `b2abd309df` | DLL | 32192 | 14.16 | 21 | 69 | gpu_compute,vision_recon |
| `Sn3DSpeckleFusion.dll` | `26c706e052` | DLL | 26953 | 14.16 | 21 | 26 | gpu_compute,vision_recon |
| `Sn3DStatusCache.dll` | `2a00786205` | DLL | 28 | 14.16 | 4 | 12 | vision_recon |
| `Sn3DTexCameraHelper.dll` | `8d1e02a72d` | DLL | 49 | 14.16 | 9 | 28 | calibration,qt,usb_hid,vision_recon |
| `Sn3DTexture.dll` | `dcdd325824` | DLL | 164 | 14.16 | 21 | 66 | qt,vision_recon |
| `Sn3DTextureBasedTrack.dll` | `83196c6cb9` | DLL | 667 | 14.16 | 15 | 35 | gpu_compute,vision_recon |
| `Sn3DTextureMapper.dll` | `8cf55f418f` | DLL | 83 | 14.16 | 10 | 2 | qt,vision_recon |
| `Sn3DTextureSFM.dll` | `97b053d6ba` | DLL | 6052 | 14.16 | 36 | 74 | gpu_compute,vision_recon |
| `Sn3DTextureSlam.dll` | `99b68e6da3` | DLL | 2596 | 14.16 | 23 | 449 | gpu_compute,vision_recon |
| `Sn3DTextureUtil.dll` | `fb0139dc6f` | DLL | 4875 | 14.16 | 12 | 89 | vision_recon |
| `Sn3DTurntableController.dll` | `ea47cf1cbf` | DLL | 62 | 14.16 | 10 | 2 | calibration,qt,vision_recon |
| `Sn3DUserPassport.dll` | `e105d45e19` | DLL | 742 | 14.16 | 16 | 12 | qt,vision_recon |
| `Sn3DprocessManager.exe` | `cc54acb60b` | EXE | 2487 | 14.16 | 18 | 0 | executable,qt,vision_recon |
| `SnCommon.dll` | `2baa8223b4` | DLL | 230 | 14.16 | 11 | 118 | crypto,qt |
| `SnDXGICapturePlugin.dll` | `4bce63a372` | DLL | 37 | 14.16 | 8 | 2 | gpu_compute,qt |
| `SnScreenRecorder.dll` | `98d4e206a6` | DLL | 113 | 14.16 | 13 | 56 | qt |
| `SnSharedBlock.dll` | `e15b61cad7` | DLL | 83 | 14.16 | 8 | 286 | qt |
| `SoundPlayerProcessor.dll` | `f4b5d637d8` | DLL | 60 | 14.16 | 9 | 15 | qt |
| `TestOpenglHelper.exe` | `51c6f5f30d` | EXE | 27 | 14.16 | 10 | 0 | executable,qt |
| `Ty3dTrackCalibrationSolution.dll` | `226131b1aa` | DLL | 745 | 14.16 | 15 | 28 | vision_recon |
| `UsbTreeModel.dll` | `f1468c27c5` | DLL | 42 | 14.16 | 8 | 4 | usb_hid |
| `Utils.dll` | `a383e01dca` | DLL | 45 | 14.16 | 8 | 3 |  |
| `algorithm1.dll` | `5207473399` | DLL | 7653 | 14.16 | 21 | 289 | gpu_compute,vision_recon |
| `algorithm2.dll` | `3af8d17a78` | DLL | 6675 | 14.16 | 24 | 1103 | gpu_compute,vision_recon |
| `algorithmHlj.dll` | `a33b6cb3f4` | DLL | 4780 | 14.16 | 21 | 427 | gpu_compute,vision_recon |
| `algorithmLy.dll` | `bdeda50a60` | DLL | 4673 | 14.16 | 21 | 120 | gpu_compute,vision_recon |
| `algorithmLzy.dll` | `5428be4467` | DLL | 16043 | 14.16 | 25 | 495 | gpu_compute,vision_recon |
| `algorithmZbt.dll` | `2f44852063` | DLL | 5054 | 14.16 | 23 | 488 | gpu_compute,vision_recon |
| `bearer/qgenericbearer.dll` | `ee30d47308` | DLL | 56 | 14.16 | 7 | 2 | qt |
| `bearer/qgenericbearerd.dll` | `b6c21bfbce` | DLL | 189 | 14.16 | 7 | 2 | qt |
| `boost_chrono-vc141-mt-x64-1_72.dll` | `819e5ca633` | DLL | 48 | 14.16 | 8 | 37 |  |
| `boost_date_time-vc141-mt-x64-1_72.dll` | `fe8ec12861` | DLL | 63 | 14.16 | 5 | 47 |  |
| `boost_filesystem-vc141-mt-x64-1_72.dll` | `729a677de9` | DLL | 127 | 14.16 | 8 | 123 | crypto,usb_hid |
| `boost_log-vc141-mt-x64-1_72.dll` | `9cf20adf5a` | DLL | 707 | 14.16 | 16 | 473 |  |
| `boost_log_setup-vc141-mt-x64-1_72.dll` | `a2d240dd16` | DLL | 618 | 14.16 | 14 | 45 |  |
| `boost_program_options-vc141-mt-x64-1_72.dll` | `2ee0cb4a43` | DLL | 443 | 14.16 | 9 | 445 |  |
| `boost_regex-vc141-mt-x64-1_72.dll` | `80a0d31e5b` | DLL | 845 | 14.16 | 9 | 1116 |  |
| `boost_serialization-vc141-mt-gd-x64-1_72.dll` | `c4779a531b` | DLL | 705 | 14.16 | 4 | 277 |  |
| `boost_thread-vc141-mt-x64-1_72.dll` | `7a114378d1` | DLL | 103 | 14.16 | 6 | 68 |  |
| `boost_timer-vc141-mt-x64-1_72.dll` | `2954409221` | DLL | 39 | 14.16 | 6 | 26 |  |
| `ceres.dll` | `eff5555ccb` | DLL | 2168 | 12.0 | 7 | 285 | vision_recon |
| `common.dll` | `f72360f413` | DLL | 5642 | 14.16 | 33 | 2669 | crypto,gpu_compute,vision_recon |
| `commonGpu.dll` | `e7294ef313` | DLL | 1161 | 14.16 | 12 | 372 | vision_recon |
| `concrt140.dll` | `6440ead4a3` | DLL | 320 | 14.29 | 9 | 291 |  |
| `cryptopp.dll` | `e777bcaf84` | DLL | 1698 | 14.16 | 2 | 4200 | crypto |
| `cudart64_102.dll` | `d01f98eccc` | DLL | 412 | 11.0 | 1 | 320 | gpu_compute |
| `cusolver64_10.dll` | `71d6bce330` | DLL | 184415 | 11.0 | 1 | 830 | gpu_compute |
| `dynamixel.dll` | `ba6b7b405a` | DLL | 95 | 8.0 | 1 | 21 |  |
| `einscan_net_svr.exe` | `b596ac1472` | EXE | 41 | 14.16 | 12 | 0 | executable,qt |
| `freetype.dll` | `afd35aa5a5` | DLL | 606 | 14.16 | 12 | 299 |  |
| `glew32.dll` | `5f7663319b` | DLL | 396 | 12.0 | 1 | 3199 | gpu_compute |
| `glfw3.dll` | `e65beb338d` | DLL | 93 | 12.0 | 5 | 110 |  |
| `glog.dll` | `0059d3be22` | DLL | 156 | 14.16 | 13 | 187 |  |
| `glogc.dll` | `c18b7d2ca4` | DLL | 157 | 14.16 | 13 | 187 |  |
| `hasp_windows_x64_105562.dll` | `d0d99fd06c` | DLL | 4861 | 8.0 | 1 | 77 | crypto,usb_hid |
| `haspvlib_105562.dll` | `6371c4d36a` | DLL | 1072 | 7.10 | 2 | 3 | crypto |
| `iconengines/qsvgicon.dll` | `abb5c78533` | DLL | 45 | 14.16 | 7 | 2 | qt |
| `iconengines/qsvgicond.dll` | `c0d306548a` | DLL | 123 | 14.16 | 6 | 2 | qt |
| `imageformats/qgif.dll` | `55b4c43eac` | DLL | 41 | 14.16 | 7 | 2 | qt |
| `imageformats/qgifd.dll` | `2b63c2a5af` | DLL | 113 | 14.16 | 5 | 2 | qt |
| `imageformats/qicns.dll` | `fb75c1f2a2` | DLL | 49 | 14.16 | 7 | 2 | qt |
| `imageformats/qicnsd.dll` | `b0f6bb1ec9` | DLL | 137 | 14.16 | 5 | 2 | qt |
| `imageformats/qico.dll` | `158805d9ba` | DLL | 41 | 14.16 | 6 | 2 | qt |
| `imageformats/qicod.dll` | `2e56f2bfb0` | DLL | 112 | 14.16 | 5 | 2 | qt |
| `imageformats/qjpeg.dll` | `0e8353832c` | DLL | 449 | 14.16 | 10 | 2 | qt |
| `imageformats/qjpegd.dll` | `479aa853ed` | DLL | 940 | 14.16 | 5 | 2 | qt |
| `imageformats/qpdf.dll` | `aba1e1fab8` | DLL | 36 | 14.16 | 8 | 2 | qt |
| `imageformats/qpdfd.dll` | `df7bcce530` | DLL | 94 | 14.16 | 6 | 2 | qt |
| `imageformats/qsvg.dll` | `857ed0af3c` | DLL | 35 | 14.16 | 7 | 2 | qt |
| `imageformats/qsvgd.dll` | `57e966ccf3` | DLL | 94 | 14.16 | 6 | 2 | qt |
| `imageformats/qtga.dll` | `149b0da0cf` | DLL | 35 | 14.16 | 7 | 2 | qt |
| `imageformats/qtgad.dll` | `57fcdaeb1e` | DLL | 94 | 14.16 | 5 | 2 | qt |
| `imageformats/qtiff.dll` | `15e993b8ce` | DLL | 390 | 14.16 | 12 | 2 | qt |
| `imageformats/qtiffd.dll` | `e69f5fcf6e` | DLL | 857 | 14.16 | 5 | 2 | qt |
| `imageformats/qwbmp.dll` | `61cf334dd2` | DLL | 33 | 14.16 | 6 | 2 | qt |
| `imageformats/qwbmpd.dll` | `dac606ef3b` | DLL | 90 | 14.16 | 5 | 2 | qt |
| `imageformats/qwebp.dll` | `18854e26e5` | DLL | 535 | 14.16 | 8 | 2 | qt |
| `imageformats/qwebpd.dll` | `979e890e55` | DLL | 1122 | 14.16 | 5 | 2 | qt |
| `informationCollect.exe` | `122e11f0d8` | EXE | 54 | 14.16 | 13 | 0 | executable,qt |
| `informationFileCollect.dll` | `cdda6cb501` | DLL | 147 | 14.16 | 11 | 2 | qt |
| `information_api.dll` | `c058698021` | DLL | 283 | 14.16 | 18 | 49 | gpu_compute,qt |
| `lib3MF.dll` | `c81a0154f2` | DLL | 630 | 12.0 | 7 | 219 | vision_recon |
| `libEGL.dll` | `06961b71a7` | DLL | 22 | 12.0 | 3 | 54 | gpu_compute |
| `libGLESV2.dll` | `bb9eb97a2a` | DLL | 2486 | 12.0 | 5 | 665 | gpu_compute |
| `libSn3DNavigation.dll` | `17102a3c24` | DLL | 435 | 14.16 | 22 | 133 | gpu_compute,qt,usb_hid |
| `libblas.dll` | `047e087f73` | DLL | 825 | 2.22 | 3 | 151 | vision_recon |
| `libbz2.dll` | `fcf656148b` | DLL | 80 | 14.16 | 7 | 24 |  |
| `libcrypto-1_1-x64.dll` | `e8150325b8` | DLL | 2781 | 14.16 | 15 | 4388 | crypto |
| `libcrypto-1_1.dll` | `7bce22788a` | DLL | 3341 | 14.24 | 15 | 4393 | crypto |
| `libcurl.dll` | `384986486b` | DLL | 346 | 14.16 | 15 | 74 | crypto |
| `libgcc_s_seh-1.dll` | `3fb8d62ab7` | DLL | 84 | 2.25 | 2 | 121 |  |
| `libgcc_s_sjlj-1.dll` | `382404b934` | DLL | 90 | 2.23 | 2 | 132 |  |
| `libgfortran-3.dll` | `eac734b59f` | DLL | 1258 | 2.25 | 6 | 1606 | vision_recon |
| `liblapack.dll` | `69302956af` | DLL | 8090 | 2.22 | 5 | 1547 | vision_recon |
| `libpassport.dll` | `80038debc3` | DLL | 441 | 14.16 | 11 | 91 | qt |
| `libpng.dll` | `b8f93e462b` | DLL | 199 | 14.16 | 10 | 246 |  |
| `libpng16.dll` | `c70dd9c046` | DLL | 198 | 14.16 | 10 | 246 |  |
| `libpq.dll` | `1d473683b4` | DLL | 193 | 14.16 | 18 | 315 | crypto,usb_hid |
| `libquadmath-0.dll` | `7bfb216560` | DLL | 328 | 2.25 | 3 | 119 | vision_recon |
| `libquickcloud-1.4.23.dll` | `0ec58486d9` | DLL | 547 | 14.16 | 10 | 62 | qt |
| `librenderkit.dll` | `e9648b641e` | DLL | 3108 | 14.16 | 25 | 1899 | gpu_compute,qt |
| `librenderkit.extensions.digit.dll` | `8ccfead32e` | DLL | 3244 | 14.16 | 23 | 1413 | qt |
| `librenderkit.plugin.cuda.dll` | `0b0ea743d5` | DLL | 448 | 14.16 | 6 | 10 | gpu_compute |
| `libsn3d.digit.scenemgr.dll` | `32c5801bed` | DLL | 76 | 14.16 | 8 | 91 | qt |
| `libssl-1_1-x64.dll` | `e864a08f4c` | DLL | 674 | 14.16 | 9 | 495 | crypto |
| `libssl-1_1.dll` | `5d60c000f5` | DLL | 682 | 14.24 | 9 | 495 | crypto |
| `logiclayer.dll` | `e6b6c1628c` | DLL | 420 | 14.16 | 14 | 123 | qt |
| `mfc120.dll` | `556442cbd5` | DLL | 5477 | 12.0 | 10 | 9297 |  |
| `mfc120u.dll` | `38437948c1` | DLL | 5503 | 12.0 | 10 | 9302 |  |
| `mfc140u.dll` | `d52171cd19` | DLL | 5517 | 14.32 | 21 | 9311 |  |
| `mkl_core.dll` | `01976e5b91` | DLL | 65739 | 12.0 | 1 | 11150 | gpu_compute |
| `mkl_def.dll` | `7004eb2ecb` | DLL | 31614 | 12.0 | 2 | 5537 | gpu_compute |
| `mkl_rt.dll` | `fcf5a12829` | DLL | 13395 | 12.0 | 1 | 15435 | gpu_compute |
| `mkl_sequential.dll` | `fdc11135e4` | DLL | 15378 | 12.0 | 1 | 5631 | gpu_compute |
| `mkl_tbb_thread.dll` | `6e4024a7bf` | DLL | 18047 | 12.0 | 1 | 5631 | gpu_compute |
| `mpir.dll` | `4bbf003dec` | DLL | 593 | 14.16 | 9 | 596 | vision_recon |
| `msvcp120.dll` | `2abf0aab5a` | DLL | 645 | 12.0 | 2 | 1569 |  |
| `msvcp140.dll` | `f6e72abc42` | DLL | 557 | 14.32 | 14 | 1515 |  |
| `msvcp140_1.dll` | `18aef42187` | DLL | 23 | 14.29 | 5 | 7 |  |
| `msvcr100 .dll` | `0531a373ef` | DLL | 814 | 10.0 | 1 | 1591 |  |
| `msvcr100.dll` | `ae3cb6c6af` | DLL | 810 | 10.0 | 1 | 1591 |  |
| `msvcr120.dll` | `ad32240bb1` | DLL | 941 | 12.0 | 1 | 1925 |  |
| `networktunnel.dll` | `8118187fb6` | DLL | 109 | 14.16 | 7 | 50 | qt |
| `networktunneld.dll` | `970abae7bc` | DLL | 292 | 14.16 | 6 | 50 | qt |
| `nppc64_10.dll` | `bcaa3c5aa2` | DLL | 239 | 11.0 | 1 | 15 | gpu_compute |
| `nppig64_10.dll` | `c33afc18ca` | DLL | 25170 | 11.0 | 2 | 706 | gpu_compute |
| `nvidia-smi.exe` | `5ad9ad17e0` | EXE | 669 | 14.27 | 2 | 0 | executable |
| `opencv_calib3d348.dll` | `69cbebc427` | DLL | 2292 | 14.14 | 13 | 924 | vision_recon |
| `opencv_core348.dll` | `0bc36bf9ff` | DLL | 13939 | 14.14 | 13 | 2457 | gpu_compute,vision_recon |
| `opencv_cudawarping348.dll` | `c939747831` | DLL | 62253 | 14.14 | 12 | 681 | gpu_compute,vision_recon |
| `opencv_features2d348.dll` | `bfd2019ff5` | DLL | 879 | 14.14 | 10 | 897 | vision_recon |
| `opencv_flann348.dll` | `3a502c2a4d` | DLL | 547 | 14.14 | 9 | 579 | vision_recon |
| `opencv_highgui348.dll` | `0e7f5f824f` | DLL | 213 | 14.14 | 17 | 705 | vision_recon |
| `opencv_imgcodecs348.dll` | `8c1b00fd73` | DLL | 3474 | 14.14 | 15 | 593 | vision_recon |
| `opencv_imgproc348.dll` | `ca207d2716` | DLL | 5721 | 14.14 | 9 | 1023 | vision_recon |
| `opencv_videoio348.dll` | `55b34b3a01` | DLL | 510 | 14.14 | 21 | 695 | gpu_compute,vision_recon |
| `opengl32sw.dll` | `199546a0b5` | DLL | 20443 | 14.0 | 6 | 1274 | crypto,gpu_compute |
| `osg158-osg.dll` | `2fb9be451c` | DLL | 2859 | 14.16 | 13 | 7545 | gpu_compute |
| `osg158-osgDB.dll` | `43cf927298` | DLL | 1018 | 14.16 | 14 | 1328 | gpu_compute |
| `osg158-osgFX.dll` | `2bca6aec17` | DLL | 220 | 14.16 | 12 | 229 | gpu_compute |
| `osg158-osgGA.dll` | `43ac8cef5c` | DLL | 343 | 14.16 | 12 | 929 | gpu_compute |
| `osg158-osgManipulator.dll` | `6048c61314` | DLL | 293 | 14.16 | 9 | 546 | gpu_compute |
| `osg158-osgParticle.dll` | `0a9e2143ff` | DLL | 325 | 14.16 | 12 | 666 | gpu_compute |
| `osg158-osgShadow.dll` | `664b0a273b` | DLL | 522 | 14.16 | 14 | 580 | gpu_compute |
| `osg158-osgText.dll` | `0245df7173` | DLL | 359 | 14.16 | 13 | 392 | gpu_compute |
| `osg158-osgUtil.dll` | `3a7a329b75` | DLL | 1431 | 14.16 | 13 | 1731 | gpu_compute |
| `osg158-osgViewer.dll` | `350e3710b6` | DLL | 784 | 14.16 | 17 | 879 | gpu_compute |
| `osgPlugins-3.6.3/osgdb_3dc.dll` | `c2f2008130` | DLL | 75 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_3ds.dll` | `37c0727dda` | DLL | 296 | 14.16 | 12 | 0 |  |
| `osgPlugins-3.6.3/osgdb_ac.dll` | `880aa5c6f5` | DLL | 187 | 14.16 | 12 | 0 |  |
| `osgPlugins-3.6.3/osgdb_bmp.dll` | `6491e19b48` | DLL | 50 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_bsp.dll` | `bfb254fc91` | DLL | 218 | 14.16 | 13 | 0 |  |
| `osgPlugins-3.6.3/osgdb_bvh.dll` | `ff6b1356c0` | DLL | 121 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_cfg.dll` | `1bac0ac472` | DLL | 126 | 14.16 | 14 | 0 |  |
| `osgPlugins-3.6.3/osgdb_curl.dll` | `fecbf2f5cc` | DLL | 94 | 14.16 | 14 | 0 |  |
| `osgPlugins-3.6.3/osgdb_dds.dll` | `96a358e8e8` | DLL | 72 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osg.dll` | `0de2b0d968` | DLL | 604 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osganimation.dll` | `7443be8fcf` | DLL | 176 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgfx.dll` | `07a924ccd8` | DLL | 61 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgparticle.dll` | `6d0b551363` | DLL | 167 | 14.16 | 11 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgshadow.dll` | `ff9c1574a1` | DLL | 39 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgsim.dll` | `cf5cbf28fc` | DLL | 98 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgterrain.dll` | `f8b32e904c` | DLL | 85 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgtext.dll` | `90a4131101` | DLL | 59 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgviewer.dll` | `ef778dbb71` | DLL | 52 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgvolume.dll` | `e604406030` | DLL | 89 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_deprecated_osgwidget.dll` | `2524b025c1` | DLL | 73 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_dot.dll` | `c52993d3bb` | DLL | 71 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_dxf.dll` | `b1cec8d67d` | DLL | 232 | 14.16 | 12 | 0 |  |
| `osgPlugins-3.6.3/osgdb_freetype.dll` | `292ecdbb5a` | DLL | 631 | 14.16 | 14 | 122 |  |
| `osgPlugins-3.6.3/osgdb_gif.dll` | `782aec9a52` | DLL | 60 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_gles.dll` | `3dc20beb3e` | DLL | 632 | 14.16 | 12 | 0 |  |
| `osgPlugins-3.6.3/osgdb_glsl.dll` | `9f38527b5b` | DLL | 62 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_gz.dll` | `c6c7a8ca9a` | DLL | 62 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_hdr.dll` | `61ab44e200` | DLL | 64 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_ive.dll` | `610d7d31f1` | DLL | 728 | 14.16 | 16 | 0 |  |
| `osgPlugins-3.6.3/osgdb_jpeg.dll` | `3b7c4dad37` | DLL | 306 | 14.16 | 11 | 0 |  |
| `osgPlugins-3.6.3/osgdb_ktx.dll` | `3e71797b11` | DLL | 61 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_logo.dll` | `d16f99b082` | DLL | 80 | 14.16 | 12 | 0 | gpu_compute |
| `osgPlugins-3.6.3/osgdb_lua.dll` | `6de13387a2` | DLL | 453 | 14.16 | 17 | 0 |  |
| `osgPlugins-3.6.3/osgdb_lwo.dll` | `f15e7af418` | DLL | 314 | 14.16 | 13 | 0 |  |
| `osgPlugins-3.6.3/osgdb_lws.dll` | `d1c09a1ac2` | DLL | 105 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_md2.dll` | `03474e4823` | DLL | 84 | 14.16 | 11 | 0 |  |
| `osgPlugins-3.6.3/osgdb_mdl.dll` | `942f03ccbb` | DLL | 129 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_normals.dll` | `c24a4e456b` | DLL | 91 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_obj.dll` | `395bafdf2a` | DLL | 202 | 14.16 | 13 | 0 |  |
| `osgPlugins-3.6.3/osgdb_openflight.dll` | `9e4587e3a0` | DLL | 458 | 14.16 | 12 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osc.dll` | `cd51206420` | DLL | 202 | 14.16 | 14 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osg.dll` | `2c07e1e8f1` | DLL | 164 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osga.dll` | `15492754dd` | DLL | 92 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osgjs.dll` | `402cf7eb20` | DLL | 396 | 14.16 | 16 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osgshadow.dll` | `c46b458965` | DLL | 56 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osgterrain.dll` | `7d82827d8a` | DLL | 54 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osgtgz.dll` | `6330e0d720` | DLL | 48 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_osgviewer.dll` | `90f863afa1` | DLL | 56 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_p3d.dll` | `a22d504ced` | DLL | 266 | 14.16 | 13 | 0 |  |
| `osgPlugins-3.6.3/osgdb_pic.dll` | `a9f65f8f6c` | DLL | 41 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_ply.dll` | `479f52f57c` | DLL | 102 | 14.16 | 12 | 0 |  |
| `osgPlugins-3.6.3/osgdb_png.dll` | `6b1dcb489b` | DLL | 60 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_pnm.dll` | `f333bebae6` | DLL | 66 | 14.16 | 11 | 0 |  |
| `osgPlugins-3.6.3/osgdb_pov.dll` | `b0bf64eb72` | DLL | 91 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_pvr.dll` | `f370f704bc` | DLL | 55 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_revisions.dll` | `c7ecdcaf90` | DLL | 64 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_rgb.dll` | `3330de4878` | DLL | 53 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_rot.dll` | `1beb3528ef` | DLL | 49 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_scale.dll` | `c338c6f30a` | DLL | 49 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osg.dll` | `f66d14ed8f` | DLL | 1264 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osganimation.dll` | `3ffe5f9668` | DLL | 405 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgfx.dll` | `cad4e76068` | DLL | 76 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgga.dll` | `66ae4f0dc8` | DLL | 121 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgmanipulator.dll` | `3de2cca862` | DLL | 115 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgparticle.dll` | `ef5309a20a` | DLL | 251 | 14.16 | 11 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgshadow.dll` | `3670f3ae7d` | DLL | 131 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgsim.dll` | `6e0fad57ba` | DLL | 181 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgterrain.dll` | `2f6d272e46` | DLL | 144 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgtext.dll` | `fe1669169a` | DLL | 112 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgui.dll` | `55d24cb9f5` | DLL | 186 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgutil.dll` | `34d8ba1dc0` | DLL | 32 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgviewer.dll` | `ab8f72ae02` | DLL | 74 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_serializers_osgvolume.dll` | `32b761f8b0` | DLL | 181 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_shp.dll` | `4bef5ead30` | DLL | 153 | 14.16 | 13 | 0 |  |
| `osgPlugins-3.6.3/osgdb_stl.dll` | `a083c528f1` | DLL | 109 | 14.16 | 13 | 0 |  |
| `osgPlugins-3.6.3/osgdb_tf.dll` | `4214663584` | DLL | 61 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_tga.dll` | `49787cee50` | DLL | 45 | 14.16 | 8 | 0 |  |
| `osgPlugins-3.6.3/osgdb_tgz.dll` | `6e17da6558` | DLL | 51 | 14.16 | 11 | 0 |  |
| `osgPlugins-3.6.3/osgdb_trans.dll` | `99eadd6bef` | DLL | 49 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_trk.dll` | `9d1e66be5c` | DLL | 78 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_txf.dll` | `79016b3c05` | DLL | 58 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_txp.dll` | `1a9b533c25` | DLL | 527 | 14.16 | 16 | 0 |  |
| `osgPlugins-3.6.3/osgdb_vtf.dll` | `d49b1ac9c2` | DLL | 51 | 14.16 | 9 | 0 |  |
| `osgPlugins-3.6.3/osgdb_x.dll` | `76d38a6190` | DLL | 122 | 14.16 | 10 | 0 |  |
| `osgPlugins-3.6.3/osgdb_zip.dll` | `e2ac8a14ee` | DLL | 122 | 14.16 | 12 | 0 |  |
| `ot21-OpenThreads.dll` | `e8bb993a05` | DLL | 44 | 14.16 | 7 | 85 | gpu_compute |
| `passport/SnCommunity/sn3DCommunity.exe` | `f8b1882b02` | EXE | 19650 | 3.0 | 1 | 0 | executable,vision_recon |
| `passport/hub/passportHub.exe` | `846bbc6658` | EXE | 4412 | 14.21 | 11 | 0 | crypto,executable,usb_hid |
| `passport/hub/plugin.dll` | `1a2efac957` | DLL | 128 | 12.0 | 7 | 3 | crypto,qt |
| `passport/hub/plugind.dll` | `b08fb9c177` | DLL | 292 | 12.0 | 7 | 3 | qt |
| `passport/openweburl/D3Dcompiler_47.dll` | `cb5bd75798` | DLL | 4071 | 11.0 | 4 | 29 | crypto,usb_hid |
| `passport/openweburl/Qt5Core.dll` | `5526e720bf` | DLL | 6025 | 14.11 | 23 | 7920 | qt,usb_hid |
| `passport/openweburl/Qt5Gui.dll` | `cc4f6beff5` | DLL | 6368 | 14.11 | 15 | 16174 | qt |
| `passport/openweburl/Qt5Network.dll` | `68a2c68ea7` | DLL | 1301 | 14.11 | 14 | 1446 | crypto,qt |
| `passport/openweburl/Qt5Positioning.dll` | `bc946963c9` | DLL | 320 | 14.11 | 8 | 643 | qt |
| `passport/openweburl/Qt5PrintSupport.dll` | `9e47e287b1` | DLL | 321 | 14.11 | 13 | 456 | qt |
| `passport/openweburl/Qt5Qml.dll` | `7db3586907` | DLL | 3937 | 14.11 | 12 | 5274 | qt |
| `passport/openweburl/Qt5Quick.dll` | `f68cc9336c` | DLL | 3713 | 14.11 | 14 | 5748 | qt |
| `passport/openweburl/Qt5QuickWidgets.dll` | `9aa08c4793` | DLL | 82 | 14.11 | 9 | 59 | qt |
| `passport/openweburl/Qt5SerialPort.dll` | `32435f8f21` | DLL | 89 | 14.11 | 8 | 95 | qt,usb_hid |
| `passport/openweburl/Qt5Svg.dll` | `deee68ff23` | DLL | 335 | 14.11 | 9 | 608 | qt |
| `passport/openweburl/Qt5WebChannel.dll` | `331e6b56cf` | DLL | 125 | 14.11 | 7 | 118 | qt |
| `passport/openweburl/Qt5WebEngineCore.dll` | `850ddd6c50` | DLL | 78156 | 14.11 | 52 | 634 | crypto,gpu_compute,qt,usb_hid |
| `passport/openweburl/Qt5WebEngineWidgets.dll` | `36396aa307` | DLL | 231 | 14.11 | 13 | 379 | qt |
| `passport/openweburl/Qt5Widgets.dll` | `ddef2c4b28` | DLL | 5467 | 14.11 | 13 | 9011 | qt |
| `passport/openweburl/QtWebEngineProcess.exe` | `748cea9e83` | EXE | 29 | 14.11 | 10 | 0 | executable,qt |
| `passport/openweburl/bearer/qgenericbearer.dll` | `48c6486246` | DLL | 57 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/iconengines/qsvgicon.dll` | `8a24940f9a` | DLL | 45 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/imageformats/qgif.dll` | `7c63008a5a` | DLL | 41 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/imageformats/qicns.dll` | `04cdc1ce31` | DLL | 50 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/imageformats/qico.dll` | `454bdbea81` | DLL | 42 | 14.11 | 6 | 2 | qt |
| `passport/openweburl/imageformats/qjpeg.dll` | `3b6bc6bd59` | DLL | 398 | 14.11 | 9 | 2 | qt |
| `passport/openweburl/imageformats/qsvg.dll` | `4e6eec2716` | DLL | 35 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/imageformats/qtga.dll` | `7edd86840d` | DLL | 35 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/imageformats/qtiff.dll` | `83e7b9a723` | DLL | 381 | 14.11 | 12 | 2 | qt |
| `passport/openweburl/imageformats/qwbmp.dll` | `50a1f382a5` | DLL | 33 | 14.11 | 6 | 2 | qt |
| `passport/openweburl/imageformats/qwebp.dll` | `d9a74b7a29` | DLL | 491 | 14.11 | 8 | 2 | qt |
| `passport/openweburl/killer.exe` | `aa4cf16708` | EXE | 43 | 14.16 | 10 | 0 | executable,qt |
| `passport/openweburl/libEGL.dll` | `fc7bf68a8e` | DLL | 28 | 14.11 | 4 | 71 | gpu_compute |
| `passport/openweburl/libGLESV2.dll` | `13e6808083` | DLL | 3500 | 14.11 | 12 | 633 | gpu_compute |
| `passport/openweburl/opengl32sw.dll` | `9e959ea7e0` | DLL | 20443 | 14.0 | 6 | 1274 | crypto,gpu_compute |
| `passport/openweburl/openweburl.exe` | `0e4f9e06b7` | EXE | 288 | 14.16 | 16 | 0 | executable,qt |
| `passport/openweburl/platforms/qwindows.dll` | `ceba5ddf86` | DLL | 1443 | 14.11 | 22 | 129 | qt |
| `passport/openweburl/position/qtposition_positionpoll.dll` | `0c468458ee` | DLL | 53 | 14.11 | 7 | 2 | qt |
| `passport/openweburl/position/qtposition_serialnmea.dll` | `8167f6067d` | DLL | 37 | 14.11 | 8 | 2 | qt |
| `passport/openweburl/position/qtposition_winrt.dll` | `b9989f3ebf` | DLL | 60 | 14.11 | 12 | 2 | qt |
| `passport/openweburl/printsupport/windowsprintersupport.dll` | `3bd3087f54` | DLL | 60 | 14.11 | 9 | 2 | qt |
| `passport/openweburl/styles/qwindowsvistastyle.dll` | `ce19b2da47` | DLL | 144 | 14.11 | 10 | 2 | qt |
| `platforms/qdirect2d.dll` | `eaf13c1dec` | DLL | 1535 | 14.16 | 26 | 2 | gpu_compute,qt |
| `platforms/qdirect2dd.dll` | `34f9b9f67f` | DLL | 4714 | 14.16 | 19 | 2 | gpu_compute,qt |
| `platforms/qminimal.dll` | `f23bdedd9a` | DLL | 836 | 14.16 | 15 | 2 | qt |
| `platforms/qminimald.dll` | `4b4b06a581` | DLL | 1923 | 14.16 | 8 | 2 | qt |
| `platforms/qoffscreen.dll` | `1e64c210e2` | DLL | 748 | 14.16 | 12 | 2 | qt |
| `platforms/qoffscreend.dll` | `4416f77516` | DLL | 1688 | 14.16 | 5 | 2 | qt |
| `platforms/qwebgl.dll` | `deb0630880` | DLL | 554 | 14.16 | 16 | 2 | qt |
| `platforms/qwebgld.dll` | `07e0252d56` | DLL | 1389 | 14.16 | 12 | 2 | qt |
| `platforms/qwindows.dll` | `c94f8da099` | DLL | 1474 | 14.16 | 22 | 2 | qt |
| `platforms/qwindowsd.dll` | `5945dcc329` | DLL | 4501 | 14.16 | 15 | 2 | qt |
| `plugin.dll` | `47b2121de1` | DLL | 104 | 14.16 | 9 | 3 | crypto,qt |
| `plugins/mediaservice/dsengine.dll` | `6954d430ae` | DLL | 75 | 12.0 | 7 | 2 | qt |
| `plugins/mediaservice/qtmedia_audioengine.dll` | `d3037622a6` | DLL | 65 | 12.0 | 5 | 2 | qt |
| `plugins/mediaservice/wmfengine.dll` | `642f5ca7c8` | DLL | 210 | 12.0 | 17 | 2 | gpu_compute,qt |
| `polyWorkModeler.exe` | `efe4d0684b` | EXE | 1904 | 14.16 | 3 | 0 | executable |
| `print3DCommon.dll` | `41ea039899` | DLL | 1159 | 14.16 | 22 | 656 | gpu_compute,vision_recon |
| `print3DSlicing.dll` | `d661c9ae77` | DLL | 5427 | 14.16 | 25 | 623 | gpu_compute,vision_recon |
| `pskill.exe` | `309eefcf96` | EXE | 272 | 12.0 | 6 | 0 | executable |
| `qttunnel.3.0.9.dll` | `2f41faf1c3` | DLL | 498 | 14.16 | 13 | 406 | crypto,qt |
| `qttunnel.3.0.9d.dll` | `6cf295979a` | DLL | 1444 | 14.16 | 9 | 406 | qt |
| `scanComponent.dll` | `db4bfb7d1d` | DLL | 42 | 14.16 | 6 | 4 |  |
| `scanhub.exe` | `87be3b2aa2` | EXE | 229 | 14.16 | 14 | 55 | crypto,executable |
| `scanservice.exe` | `d8d90f9a51` | EXE | 66 | 14.16 | 14 | 0 | executable,qt |
| `sketchfab_api.dll` | `6857b4d813` | DLL | 153 | 14.16 | 10 | 12 | qt |
| `sn3DCommunity.exe` | `1541d705ac` | EXE | 27744 | 3.0 | 1 | 0 | executable,vision_recon |
| `sn3DHIDCommunication.dll` | `27b99f62d7` | DLL | 41 | 12.0 | 5 | 15 | usb_hid,vision_recon |
| `sn3DLock.dll` | `c4d6fd5d43` | DLL | 334 | 14.16 | 15 | 13 | crypto,usb_hid,vision_recon |
| `sn3DTurnTable.dll` | `8ed7fe074f` | DLL | 2666 | 14.16 | 18 | 24 | usb_hid,vision_recon |
| `sn3d.digit.scenecontrol.dll` | `ec978b8046` | DLL | 1717 | 14.16 | 22 | 2 | qt,vision_recon |
| `sn3d_user_account.dll` | `c8d77171e2` | DLL | 303 | 14.16 | 10 | 35 | qt,vision_recon |
| `softwareUpgrade.exe` | `d3e79c57e2` | EXE | 38 | 14.16 | 13 | 0 | executable,firmware_update,qt |
| `solid_edge/api.exe` | `c3edfb48fa` | EXE | 20 | 11.0 | 1 | 0 | executable |
| `sqldrivers/qsqlite.dll` | `7e687b1d28` | DLL | 1243 | 14.16 | 10 | 2 | qt |
| `sqldrivers/qsqlited.dll` | `98ecf94159` | DLL | 1946 | 14.16 | 5 | 2 | qt |
| `sqldrivers/qsqlodbc.dll` | `6c65e5c883` | DLL | 95 | 14.16 | 8 | 2 | qt |
| `sqldrivers/qsqlodbcd.dll` | `f189c55d8a` | DLL | 220 | 14.16 | 6 | 2 | qt |
| `sqldrivers/sqlitecipher.dll` | `4db99cc038` | DLL | 1432 | 14.16 | 13 | 7 | crypto,qt |
| `sqlitecipher.dll` | `464647f927` | DLL | 1432 | 14.16 | 13 | 7 | crypto,qt |
| `syncservice/CommunicateLayer.dll` | `7554c87683` | DLL | 65 | 14.16 | 10 | 29 | usb_hid |
| `syncservice/DeviceHelper.exe` | `b47e993b5e` | EXE | 41 | 14.16 | 9 | 0 | executable,qt |
| `syncservice/Qt5Core.dll` | `b48a161997` | DLL | 6025 | 14.11 | 23 | 7920 | qt,usb_hid |
| `syncservice/Qt5Gui.dll` | `567f21049d` | DLL | 6368 | 14.11 | 15 | 16174 | qt |
| `syncservice/Qt5Network.dll` | `5b44cbd2d3` | DLL | 1301 | 14.11 | 14 | 1446 | crypto,qt |
| `syncservice/Qt5Svg.dll` | `6e9b0944a5` | DLL | 335 | 14.11 | 9 | 608 | qt |
| `syncservice/Qt5Widgets.dll` | `3ad6be6758` | DLL | 5467 | 14.11 | 13 | 9011 | qt |
| `syncservice/SnCommon.dll` | `464d22852e` | DLL | 230 | 14.16 | 11 | 118 | crypto,qt |
| `syncservice/SnDXGICapturePlugin.dll` | `cee04453d5` | DLL | 37 | 14.16 | 8 | 2 | gpu_compute,qt |
| `syncservice/SnScreenRecorder.dll` | `4055a57447` | DLL | 113 | 14.16 | 13 | 56 | qt |
| `syncservice/SnSharedBlock.dll` | `00fb03b665` | DLL | 83 | 14.16 | 8 | 286 | qt |
| `syncservice/SnSyncCore.dll` | `9319387a7e` | DLL | 135 | 14.16 | 6 | 71 | qt |
| `syncservice/SnSyncService.exe` | `ae1c99f5c7` | EXE | 504 | 14.16 | 16 | 0 | executable,qt |
| `syncservice/SnSyncTool.dll` | `07f3278839` | DLL | 93 | 14.16 | 23 | 39 | qt |
| `syncservice/UsbTreeModel.dll` | `7c6a722aa0` | DLL | 42 | 14.16 | 8 | 4 | usb_hid |
| `syncservice/bearer/qgenericbearer.dll` | `22e57ab000` | DLL | 57 | 14.11 | 7 | 2 | qt |
| `syncservice/cryptopp.dll` | `0c5a4b8cb5` | DLL | 1698 | 14.16 | 2 | 4200 | crypto |
| `syncservice/cudart64_102.dll` | `2074fb613f` | DLL | 412 | 11.0 | 1 | 320 | gpu_compute |
| `syncservice/device/Einscan3H2.2.0.0.dll` | `3fc0f35283` | DLL | 211 | 14.16 | 14 | 2 | gpu_compute,qt,vision_recon |
| `syncservice/device/MECScan.2.0.0.dll` | `91ac09cb26` | DLL | 155 | 14.16 | 8 | 2 | qt |
| `syncservice/device/SHNDetect.2.0.0.dll` | `747626d296` | DLL | 43 | 14.16 | 9 | 2 | qt |
| `syncservice/device/dhgxicamera.2.0.6.dll` | `dd1813bdcb` | DLL | 96 | 14.16 | 8 | 2 | qt |
| `syncservice/device/dlp3100projector.2.0.0.dll` | `27bb18119e` | DLL | 73 | 14.16 | 8 | 2 | qt,usb_hid |
| `syncservice/device/einscan10.2.0.0.dll` | `d02e794b16` | DLL | 115 | 14.16 | 9 | 2 | qt |
| `syncservice/device/einscan3.2.0.0.dll` | `7f4853c1d2` | DLL | 96 | 14.16 | 8 | 2 | qt,usb_hid |
| `syncservice/device/einscan7.2.0.3.dll` | `8e3593758c` | DLL | 68 | 14.16 | 8 | 2 | qt |
| `syncservice/device/einscan7wl.2.0.0.dll` | `a70a96d4fa` | DLL | 80 | 14.16 | 8 | 2 | qt |
| `syncservice/device/freescan.2.1.0.dll` | `6632304774` | DLL | 89 | 14.16 | 8 | 2 | qt |
| `syncservice/device/freescante25.2.0.2.dll` | `accdef093c` | DLL | 69 | 14.16 | 8 | 2 | qt,usb_hid |
| `syncservice/device/freetrak.2.1.0.dll` | `3ca40ea207` | DLL | 229 | 14.16 | 15 | 2 | crypto,qt |
| `syncservice/device/fs3scan.2.0.0.dll` | `0dc853cda9` | DLL | 128 | 12.0 | 5 | 2 | qt |
| `syncservice/device/fsprobe.2.0.2.dll` | `9d50a45000` | DLL | 114 | 14.16 | 8 | 2 | qt |
| `syncservice/device/hkcamera.2.0.0.dll` | `836a078cf0` | DLL | 116 | 14.16 | 12 | 2 | qt |
| `syncservice/device/jaicamera.2.0.0.dll` | `5acce5f618` | DLL | 79 | 14.16 | 8 | 2 | qt |
| `syncservice/device/sesp.2.0.0.dll` | `f9fba5f700` | DLL | 95 | 14.16 | 9 | 2 | qt |
| `syncservice/device/shncamera.2.0.0.dll` | `be7486ee00` | DLL | 104 | 14.16 | 9 | 2 | qt |
| `syncservice/device/sp3scan.2.0.0.dll` | `27ca047c51` | DLL | 108 | 14.16 | 9 | 2 | qt |
| `syncservice/device/spncamera.2.0.0.dll` | `a7ae87e97f` | DLL | 116 | 14.16 | 9 | 2 | qt |
| `syncservice/device/spncamera.2.2.9.dll` | `0e05a9061a` | DLL | 121 | 14.16 | 8 | 2 | qt |
| `syncservice/device/ty1080projector.2.0.0.dll` | `f38acb2bf6` | DLL | 64 | 14.16 | 9 | 2 | qt,usb_hid |
| `syncservice/device/ty1080projector_wl.2.0.0.dll` | `c0c9337e1d` | DLL | 95 | 14.16 | 9 | 2 | qt |
| `syncservice/device/tylaser.2.0.0.dll` | `15b14abfb0` | DLL | 43 | 14.16 | 8 | 2 | qt,usb_hid |
| `syncservice/device/uescan.2.0.0.dll` | `f321ec8a47` | DLL | 200 | 14.16 | 11 | 2 | qt |
| `syncservice/device/uescanwl.2.0.0.dll` | `bb456fbfe5` | DLL | 172 | 14.16 | 11 | 2 | qt |
| `syncservice/iconengines/qsvgicon.dll` | `ac729f9813` | DLL | 45 | 14.11 | 7 | 2 | qt |
| `syncservice/imageformats/qgif.dll` | `9f574bd265` | DLL | 41 | 14.11 | 7 | 2 | qt |
| `syncservice/imageformats/qicns.dll` | `77e435fc17` | DLL | 50 | 14.11 | 7 | 2 | qt |
| `syncservice/imageformats/qico.dll` | `8fd32bde1a` | DLL | 42 | 14.11 | 6 | 2 | qt |
| `syncservice/imageformats/qjpeg.dll` | `3916c51325` | DLL | 398 | 14.11 | 9 | 2 | qt |
| `syncservice/imageformats/qsvg.dll` | `c538964367` | DLL | 35 | 14.11 | 7 | 2 | qt |
| `syncservice/imageformats/qtga.dll` | `366e099587` | DLL | 35 | 14.11 | 7 | 2 | qt |
| `syncservice/imageformats/qtiff.dll` | `5446e9ab34` | DLL | 381 | 14.11 | 12 | 2 | qt |
| `syncservice/imageformats/qwbmp.dll` | `d7f2187d8a` | DLL | 33 | 14.11 | 6 | 2 | qt |
| `syncservice/imageformats/qwebp.dll` | `0b358bd08c` | DLL | 491 | 14.11 | 8 | 2 | qt |
| `syncservice/libEGL.dll` | `553c5b6c1b` | DLL | 28 | 14.11 | 4 | 71 | gpu_compute |
| `syncservice/libGLESV2.dll` | `3651514875` | DLL | 3500 | 14.11 | 12 | 633 | gpu_compute |
| `syncservice/logic/3DDigitalSyncInterface.2.0.0.dll` | `88d8542d93` | DLL | 2512 | 14.16 | 18 | 2 | qt,usb_hid |
| `syncservice/networktunnel.dll` | `9e2017e7bb` | DLL | 109 | 14.16 | 7 | 50 | qt |
| `syncservice/networktunneld.dll` | `a0c6336611` | DLL | 292 | 14.16 | 6 | 50 | qt |
| `syncservice/nvjpeg64_10.dll` | `bfc130f2c0` | DLL | 3050 | 11.0 | 1 | 63 |  |
| `syncservice/opencv_core3411.dll` | `c679ce22a0` | DLL | 3943 | 12.0 | 3 | 2411 | vision_recon |
| `syncservice/opencv_imgcodecs3411.dll` | `f018b2aace` | DLL | 3873 | 12.0 | 5 | 540 | vision_recon |
| `syncservice/opencv_imgproc3411.dll` | `29cf8ed398` | DLL | 5219 | 12.0 | 4 | 968 | vision_recon |
| `syncservice/opengl32sw.dll` | `04b39a7c6b` | DLL | 20443 | 14.0 | 6 | 1274 | crypto,gpu_compute |
| `syncservice/platforms/qwindows.dll` | `548298a9bb` | DLL | 1443 | 14.11 | 22 | 129 | qt |
| `syncservice/qttunnel.3.0.9.dll` | `b8696966e7` | DLL | 498 | 14.16 | 13 | 406 | crypto,qt |
| `syncservice/qttunnel.3.0.9d.dll` | `77359a374b` | DLL | 1444 | 14.16 | 9 | 406 | qt |
| `syncservice/sn3DCommunicateLayerM.dll` | `9f79c4e925` | DLL | 68 | 14.16 | 11 | 30 | qt,usb_hid,vision_recon |
| `syncservice/sn3DHIDCommunication.dll` | `b156a4d560` | DLL | 39 | 12.0 | 5 | 16 | usb_hid,vision_recon |
| `syncservice/sn3DHIDCommunicationM.dll` | `ea016d82d1` | DLL | 47 | 14.16 | 8 | 17 | qt,usb_hid,vision_recon |
| `syncservice/sn3DHIDCommunicationNew.dll` | `215cc60347` | DLL | 41 | 14.16 | 8 | 16 | usb_hid,vision_recon |
| `syncservice/snCameraControl.dll` | `2d4306c267` | DLL | 87 | 14.16 | 9 | 26 |  |
| `syncservice/styles/qwindowsvistastyle.dll` | `5940837719` | DLL | 144 | 14.11 | 10 | 2 | qt |
| `syncservice/tunnel_encrypt.dll` | `548ed8e327` | DLL | 46 | 14.16 | 6 | 19 | crypto,qt |
| `syncservice/tunnel_encryptd.dll` | `8dd777802e` | DLL | 139 | 14.16 | 5 | 19 | crypto,qt |
| `tbb.dll` | `aaeff9bb9f` | DLL | 382 | 12.0 | 3 | 229 | vision_recon |
| `tunnel_encrypt.dll` | `91edf7e6dd` | DLL | 46 | 14.16 | 6 | 19 | crypto,qt |
| `tunnel_encryptd.dll` | `798864758e` | DLL | 139 | 14.16 | 5 | 19 | crypto,qt |
| `uninst.exe` | `20ec8ea1b7` | EXE | 514 | 10.0 | 8 | 0 | executable |
| `vcamp120.dll` | `ef3f1a0fa0` | DLL | 471 | 12.0 | 7 | 165 | crypto,gpu_compute |
| `vcomp120.dll` | `ae710d6670` | DLL | 134 | 12.0 | 2 | 113 |  |
| `vcomp140.dll` | `54fe6b0875` | DLL | 177 | 14.32 | 1 | 113 |  |
| `vcruntime140_1.dll` | `80e62e9e45` | DLL | 37 | 14.32 | 5 | 3 |  |
| `vs_mesh_loader.exe` | `ce6ca757e4` | EXE | 60 | 48.0 | 1 | 0 | executable |
| `zipHelper.dll` | `c782fc4260` | DLL | 62 | 14.16 | 11 | 44 | qt |
| `zlib.dll` | `967b828de5` | DLL | 93 | 14.16 | 6 | 84 |  |
| `zlib1.dll` | `f2a2eae0d0` | DLL | 94 | 14.16 | 6 | 84 |  |

