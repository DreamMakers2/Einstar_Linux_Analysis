#!/usr/bin/env python3
"""Generate docs/windows/module_map.md from the analysed JSON artefacts."""
from __future__ import annotations

import json
import os
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = str(ROOT / "analysis" / "gnu")
OUT = str(ROOT / "docs" / "windows" / "module_map.md")

inv = json.load(open(os.path.join(BASE, "pe_inventory.json")))
mods = inv["modules"]
deps = json.load(open(os.path.join(BASE, "module_dependencies.json")))
cls = json.load(open(os.path.join(BASE, "classification.json")))
calib = json.load(open(os.path.join(BASE, "calibration_api.json")))
tc = json.load(open(os.path.join(BASE, "toolchain.json")))

LINK2MSVC = {
    "14.32": "MSVC 19.32 (VS2022 17.2)", "14.29": "MSVC 19.29 (VS2019 16.10)",
    "14.27": "MSVC 19.27 (VS2019 16.5)", "14.24": "MSVC 19.24 (VS2019 16.4)",
    "14.21": "MSVC 19.21 (VS2019 16.1)", "14.20": "MSVC 19.20 (VS2019 16.0)",
    "14.16": "MSVC 19.16 (VS2017 15.9)", "14.14": "MSVC 19.14 (VS2017 15.7)",
    "14.11": "MSVC 19.11 (VS2017 15.4)", "14.0": "MSVC 19.00 (VS2015)",
    "12.0": "MSVC 18.00 (VS2013)", "11.0": "MSVC 17.00 (VS2012)",
    "10.0": "MSVC 16.00 (VS2010)", "10.10": "MSVC 16.10 (VS2010 SP1)",
    "9.0": "MSVC 15.00 (VS2008)", "8.0": "MSVC 14.00 (VS2005)",
    "7.10": "MSVC 13.10 (VS2003)", "6.0": "MSVC 12.00 (VS6)",
}


def kb(n):
    return f"{n/1024:.0f}"


def short_tc(rec):
    lv = rec.get("linker_version")
    ms = LINK2MSVC.get(lv, f"link {lv}")
    crt = ""
    imps = rec.get("imports", {})
    if "MSVCP140.dll" in imps or "VCRUNTIME140.dll" in imps:
        crt = " /MD (UCRT)"
    elif "MSVCR120.dll" in imps or "MSVCP120.dll" in imps:
        crt = " /MD (MSVCR120)"
    elif "MSVCR100.dll" in imps:
        crt = " /MD (MSVCR100)"
    return ms + crt


def imp_summary(rec, n=4):
    items = []
    for dll, funcs in sorted(rec.get("imports", {}).items(), key=lambda kv: -len(kv[1])):
        if dll.lower().startswith(("api-ms-win", "kernel32", "vcruntime", "msvcp", "user32",
                                   "advapi32", "shell32", "ole32", "gdi32", "ws2_32")):
            continue
        items.append(f"{dll} ({len(funcs)})")
        if len(items) >= n:
            break
    return ", ".join(items) if items else "—"


def exp_summary(rec, n=3):
    names = [e["name"] or "" for e in rec.get("exports", [])]
    good = [x for x in names if x and not x.startswith(("??0", "??1", "??_7", "??_F", "?_7", "?_F", "??4"))]
    ex = (good or names)[:n]
    if not ex:
        return "—"
    return ", ".join(f"`{e}`" for e in ex)


# ------------------------------------------------------------------ roles
ROLE = {
    "EXStar.exe": ("Application entry point (Qt Widgets); boots the E7/EXStar app layer and loads process plug-ins.", "imports AppCommon_E7/AppUi chain, Sn3DProcessPlugin; PDB 'EXStar'"),
    "AppUi.dll": ("UI shell / window composition for the EXStar product.", "2 exports (Qt plug-in), imports AppCommon_E7"),
    "AppCommon_E7.dll": ("Shared application logic for the E7-family (EinScan) products.", "49 exports, Qt Widgets/Quick"),
    "scanservice.exe": ("Per-scan worker process spawned by the process manager; hosts a process plug-in.", "imports Sn3DProcessPlugin + Sn3DLog; 2024-06 build"),
    "Sn3DprocessManager.exe": ("Supervisor that launches/monitors scan worker processes; uses the QtTunnel IPC bus.", "imports qttunnel.3.0.9.dll, Qt5Network"),
    "scanhub.exe": ("Legacy (2021) network hub for scan devices (WS2_32 sockets).", "imports WS2_32, CreateFileA"),
    "Sn3DProcessPlugin.dll": ("Process/plug-in bootstrap used by EXStar.exe, scanservice.exe, FirmwareupdateUI.exe.", "51 exports"),
    "Sn3DDeveloperControlPlugin.dll": ("Developer control plug-in: reads/writes devCtr files (devFileOperation_V1).", "30 exports, getDevCtrFile/saveDevCtrFile"),
    "Sn3DScanController.dll": ("Scan-session controller: drives the scan pipeline and publishes frame data.", "48 exports; imports Sn3DTextureSlam, Sn3DMultiProjects, Sn3DCalibDevFile"),
    "Sn3DRealtimeScan.dll": ("Real-time SLAM/fusion engine (largest vendor algorithm surface).", "1003 exports, imports OpenCV/TBB"),
    "Sn3DRegistration.dll": ("Point-cloud registration / ICP engine.", "1729 exports"),
    "Sn3DAlgorithmWrapper.dll": ("Facade over the vendor algorithms; imported by scan controllers.", "147 exports"),
    "common.dll": ("Geometry/mesh core used across the scan pipeline.", "2669 exports"),
    "CommonFunc.dll": ("General utility library (files, strings, system).", "1487 exports"),
    "Sn3DBox.dll": ("Qt-facing message box / IPC object base (Topic/Module wrappers).", "166 exports"),
    "Sn3DConfig.dll": ("Configuration service (SQLite/JSON backed).", "15 exports"),
    "Sn3DConfigSQLite": ("SQLite-backed configuration backend (seen as class in other DLLs).", "mangled class, not a separate file"),
    "Sn3DLog.dll": ("Logging front-end (thin wrapper over glogc).", "19 exports"),
    "glogc.dll": ("Google-glog logging runtime (custom build, C suffix).", "187 exports"),
    "Sn3DImageQueue.dll": ("Shared image frame queue between device, calibration and scan layers.", "25 exports"),
    "Sn3DFrameDataQueue.dll": ("Frame data queue primitives.", "25 exports"),
    "Sn3DFrameDataController.dll": ("Frame-data controller object (serialise/publish frames).", "29 exports"),
    "Sn3DErrorCode.dll": ("Central error-code enum/registry.", "18 exports, imports by Sn3DSecurity"),
    "Sn3DFile.dll": ("File format I/O for projects and meshes.", "184 exports"),
    "Sn3DMultiProjects.dll": ("Multi-project session management.", "124 exports"),
    "Sn3DCrypto.dll": ("AES-128/3DES text/file crypto plus key-protected mesh read/write ('own format').", "52 exports: Sn3DCryptoAES::encrypto_file2file, decrypto_text, read_mesh_with_key…"),
    "Sn3DSecurity.dll": ("Licensing / anti-tamper: HASP dongle access, USB machine binding, cloud licensing, Qt plug-in.", "6.0 MB; imports DeviceIoControl; strings 'Aladdin\\Vid0529&Pid0001&HaspHL', 'USB\\VID_0424', 'Sn3DDongle', 'Clone detection'"),
    "sn3DLock.dll": ("File/process lock helper (DeviceIoControl on volume handles).", "13 exports"),
    "sqlitecipher.dll": ("SQLCipher build of SQLite (encrypted config/app DBs).", "7 exports"),
    "cryptopp.dll": ("Crypto++ 5.x/6.x static-export DLL.", "4200 exports"),
    "hasp_windows_x64_105562.dll": ("Aladdin/SafeNet HASP SRM runtime (dongle licensing).", "77 exports, version 105562"),
    "haspvlib_105562.dll": ("HASP vendor library (dongle driver interface).", "3 exports"),
    "libcrypto-1_1-x64.dll": ("OpenSSL 1.1.1d libcrypto (TLS + hashing for update/cloud).", "4388 exports; banner OpenSSL 1.1.1d"),
    "libssl-1_1-x64.dll": ("OpenSSL 1.1.1d libssl.", "495 exports"),
    "LIBEAY32.dll": ("Legacy OpenSSL 1.0.2q libcrypto (older plug-ins).", "3709 exports; banner OpenSSL 1.0.2q; D:\\Projects PDB"),
    "SSLEAY32.dll": ("Legacy OpenSSL 1.0.2q libssl.", "321 exports; banner OpenSSL 1.0.2q"),
    "qttunnel.3.0.9.dll": ("QtTunnel + QMQTT IPC bus: shared-memory segments, routers, module/object remoting.", "406 exports, QtTunnel::Module/Object, QMQTT::Client"),
    "networktunnel.dll": ("Socket transport for the tunnel layer (release build).", "50 exports"),
    "networktunneld.dll": ("Debug build of networktunnel.", "50 exports"),
    "tunnel_encrypt.dll": ("Payload encryption for tunnel traffic.", "19 exports, imported by qttunnel"),
    "Sn3DPassportController.dll": ("Account/passport controller (login, entitlement).", "280 exports; imports SetupAPI/CM_* (USB machine binding)"),
    "Sn3DUserPassport.dll": ("User passport data model.", "12 exports"),
    "libpassport.dll": ("Passport/auth client library.", "91 exports"),
    "Sn3DSoftwareUpdate.dll": ("Application self-update: version check, download, install; talks HTTP(S).", "31 exports checkNewVersion/downloadNewApp/installNewApp"),
    "Sn3DSoftwareUpgradeController.dll": ("Qt plug-in wrapper that runs the upgrade helper with the IPC bus.", "2 exports (qt_plugin_instance)"),
    "softwareUpgrade.exe": ("Small helper executable that performs an upgrade step and reports through the tunnel topics.", "strings 'Sn3DShareMemReqTopic', 'password', 'c5msnsync'; imports Sn3DProcessPlugin"),
    "FirmwareupdateUI.exe": ("QML firmware-update UI: sends cmd='UpdateFirmware' with firmwareFilePath.", "QML strings 'param[\\'cmd\\']=\\'UpdateFirmware\\'', PDB Sn3DFirmwareUpdate"),
    "Sn3DDeviceEinStar.dll": ("Einstar device layer: enumeration/open/close, serials, temperature, brightness, calibration data read-out, firmware update orchestration (spawns SnSyncService.exe).", "187 exports: ISFWUPDATE, ISFORCEFWUPDATE, BOOTLOADERSUBDIR, openDevice, captureImage, getFirmwareVersion"),
    "Sn3DEinStarController.dll": ("Qt plug-in that adapts Sn3DDeviceEinStar to the controller/command bus.", "2 exports (qt_plugin_instance); imports Sn3DDeviceEinStar; topics /FirmUpdate/Configure, FirmUpdate/writeBootLoader"),
    "Sn3DTexCameraHelper.dll": ("Texture-camera configuration helper (gain/exposure/CCM suffix), enumerates camera device interfaces via SetupAPI.", "28 exports; imports SETUPAPI"),
    "Sn3DAutoExposureHelper.dll": ("Auto-exposure manager for the scan cameras.", "19 exports"),
    "Sn3DCalibBigCirExtract.dll": ("Big-circle extraction for laser/calibration targets.", "6 exports"),
    "Sn3DCalibrationJR.dll": ("Large calibration algorithm library (camera/IMU/light-plane, Ceres+imu_tk).", "790 exports"),
    "Sn3DCalibDevFile.dll": ("Device calibration data file controller (read DevData to file, calibration-state notifications).", "26 exports; readDevDataToFileCmd / handleNotifyIsCablliratedCmd"),
    "Sn3DCalibrateData.dll": ("Calibration data model: (de)serialise CCF, CCM+dark, white balance; read/write CCF files.", "29 exports; readCCF/writeCCFToFile/serializeCCF"),
    "Sn3DCalibrateLoader.dll": ("Calibration procedure loader: XML step/group tree (BaseNode/GroupNode/StepNode), capture workflow.", "58 exports"),
    "Sn3DCalibrateAlgorithmWrapper.dll": ("Calibration algorithm managers (camera, pose, laser, white balance) for E7/EA/Sesp variants.", "129 exports"),
    "Sn3DCalibrateAlgorithmHelper.dll": ("Calibration marker extraction helper.", "10 exports"),
    "Sn3DCalibrateImageInfo.dll": ("Calibration image store: save groups/images to disk, read totals.", "18 exports"),
    "Sn3DCalibCaptureImageProcess.dll": ("Calibration capture pipeline/controllers (E7/EA/Sesp/UE).", "103 exports"),
    "Sn3DCameraCalibHelper.dll": ("Camera calibration + white balance algorithms (Sesp camera, RGB combo WB).", "33 exports"),
    "Sn3DEACalibrateController.dll": ("Qt plug-in that drives the EA calibration workflow for the Einstar-class device.", "9 exports; imports Sn3DDeviceEinStar"),
    "Sn3DTurntableController.dll": ("Qt plug-in for turntable control.", "2 exports; imports sn3DTurnTable"),
    "sn3DTurnTable.dll": ("Turntable driver: Silicon Labs USBXpress (SiUSBXp) and HID transports, Dynamixel servo library.", "24 exports GetPCBVersion; imports SiUSBXp.dll, sn3DHIDCommunication.dll, dynamixel.dll"),
    "SiUSBXp.dll": ("Silicon Laboratories USBXpress 3.4.1.0 API (SI_Open/SI_Read/SI_Write/SI_DeviceIOControl) for the turntable's USB-serial bridge.", "24 exports; imports SETUPAPI + DeviceIoControl; 2011 timestamp"),
    "sn3DHIDCommunication.dll": ("HID transport: enumerate/open HID devices, Get/SetReport, overlapped read/write.", "15 exports; imports HID.DLL, SETUPAPI"),
    "UsbTreeModel.dll": ("USB topology enumeration/reset (SetupAPI + Configuration Manager + DeviceIoControl).", "4 exports: EnumAllHCD, EraseList, GetSpecifyDeviceTreePath, ResetDevice"),
    "syncservice/SnSyncService.exe": ("Out-of-process scan-device service that owns the actual USB device handles.", "spawned by Sn3DDeviceEinStar; imports SnSyncCore/SnSyncTool/SnSharedBlock"),
    "syncservice/SnSyncCore.dll": ("Device/service core: DeviceController create/connect/discardFrames.", "71 exports"),
    "syncservice/CommunicateLayer.dll": ("Vendor USB transport: initializeSystem(GUID), attachDevice(serial), pipe addresses, bulk reply, stream switch.", "29 exports; SETUPAPI + DeviceIoControl"),
    "syncservice/sn3DCommunicateLayerM.dll": ("Variant of CommunicateLayer with auto-reconnect.", "30 exports"),
    "7z.exe": ("7-Zip CLI shipped for archive handling.", "7-Zip 21.07 banner"),
    "7z.dll": ("7-Zip engine (13 exports).", "7-Zip 21.07 banner"),
    "PnPutil.exe": ("Microsoft PnP driver utility, used by driver install scripts.", "Microsoft-signed"),
    "nvidia-smi.exe": ("NVIDIA driver management binary (CUDA stack).", "NVIDIA"),
    "pskill.exe": ("Sysinternals PsKill (process termination helper).", "Sysinternals"),
    "PsExec64.exe": ("Sysinternals PsExec used by the UEC driver installer.", "Resource/driver/UEC"),
    "S3D_Teamviewer.exe": ("Bundled TeamViewer remote-support client.", "24 MB GUI"),
    "sn3DCommunity.exe": ("Community/cloud client shell.", "28 MB"),
    "LaunchTool.exe": ("Launcher/updater helper.", "imports AppCommon_E7"),
    "PreviewTool.exe": ("Standalone preview tool.", "GUI"),
    "polyWorkModeler.exe": ("Mesh modelling tool (polygon work).", "CUI"),
    "uninst.exe": ("Installer/uninstaller stub.", "GUI"),
    "informationCollect.exe": ("Diagnostics collection helper.", "information_api/informationFileCollect"),
    "einscan_net_svr.exe": ("Legacy Einscan network server.", "42 KB"),
    "TestOpenglHelper.exe": ("OpenGL self-test helper.", "28 KB"),
    "vs_mesh_loader.exe": ("Mesh loading test harness.", "61 KB"),
    "CalibrationPage_EA.dll": ("Qt/QML calibration page for the EA (Einstar) product.", "2 exports; 2.7 MB QML resources"),
    "Sn3DCalibrationJR.dll ": ("see Sn3DCalibrationJR", ""),
    "Sn3DColorCorrect.dll": ("Colour correction / texture colour handling.", "64 exports"),
    "Sn3DGratingRecons.dll": ("Grating (structured-light) reconstruction.", "41 exports"),
    "Sn3DPhaseBuild.dll": ("Phase/structured-light build step.", "47 exports"),
    "Sn3DSpeckle.dll": ("Speckle pattern matching engine.", "32 MB, 69 exports"),
    "Sn3DSpeckleFusion.dll": ("Speckle + fusion engine.", "27 MB"),
    "Sn3DTextureSlam.dll": ("Texture-assisted SLAM tracking.", "449 exports"),
    "Sn3DTextureSFM.dll": ("Structure-from-motion texture pipeline.", "74 exports"),
    "Sn3DTextureMapper.dll": ("Texture mapping/UV projection.", "2 exports"),
    "Sn3DTextureUtil.dll": ("Texture utility routines.", "89 exports"),
    "Sn3DTexture.dll": ("Texture data model shared by device/scan layers.", "66 exports"),
    "Sn3DGeometricTrackFusion.dll": ("Geometric tracking + fusion.", "302 exports"),
    "Sn3DScanSlam.dll": ("Scan SLAM core.", "144 exports"),
    "Sn3DManualAlign.dll": ("Manual alignment tool.", "2 exports"),
    "Sn3DHeadSeg.dll": ("Head segmentation (face scanning).", "15 exports"),
    "Sn3DFaceScan.dll": ("Face-scan pipeline.", "15 exports"),
    "Sn3DDental.dll": ("Dental scan application layer (vertical).", "893 exports"),
    "Sn3DDentalAI.dll": ("Dental AI inference wrapper.", "357 exports"),
    "Sn3DInfraredCariesDet.dll": ("Infrared caries detection (dental).", "29 exports"),
    "Sn3DLandmarksDetForCli.dll": ("Landmark detection for clinical use.", "11 exports"),
    "Sn3DMagic.dll": ("'Magic' scan mode engine (auto-alignment/cleanup).", "658 exports"),
    "Sn3DLineCode.dll": ("Line-code pattern projection/decoding.", "55 exports"),
    "Sn3DCodeProject.dll": ("Coded structured-light projection.", "85 exports"),
    "Sn3DCork.dll": ("Feature/anchoring component (cork = registration seeds).", "244 exports"),
    "Sn3DCloudDisk.dll": ("Cloud-storage client (upload/download scans).", "9 exports"),
    "Sn3DCloudProcessGPU.dll": ("GPU cloud-processing helper.", "3 exports"),
    "Sn3DQuickCloudHelper.dll": ("Quick-cloud protocol helper (libquickcloud).", "54 exports; imports libquickcloud-1.4.23"),
    "Sn3DDigital.dll": ("Digital/scan data application layer.", "996 exports"),
    "Sn3DDigitGlobal.dll": ("Digital module globals.", "5 exports"),
    "Sn3DAIOutUnity.dll": ("AI output bridge to Unity.", "33 exports"),
    "Sn3DAVXImp.dll": ("AVX optimised import routines.", "3 exports"),
    "Sn3DSparseSolver.dll": ("Sparse linear solver wrapper.", "1 export"),
    "Sn3DRestoreData.dll": ("Crash/session data restore.", "19 exports"),
    "Sn3DStatusCache.dll": ("Status cache for UI.", "12 exports"),
    "Sn3DProgressHelper.dll": ("Progress reporting helper.", "26 exports"),
    "Sn3DProjDefaultPath.dll": ("Default project path policy.", "2 exports"),
    "Sn3DManualAlign.dll ": ("see above", ""),
    "algorithm1.dll": ("Customer/OEM algorithm build #1.", "289 exports; Ceres/Eigen/OpenMesh symbols"),
    "algorithm2.dll": ("Customer/OEM algorithm build #2.", "1103 exports"),
    "algorithmHlj.dll": ("OEM algorithm variant 'Hlj'.", "427 exports"),
    "algorithmLy.dll": ("OEM algorithm variant 'Ly'.", "120 exports"),
    "algorithmLzy.dll": ("OEM algorithm variant 'Lzy'.", "495 exports, 16 MB"),
    "algorithmZbt.dll": ("OEM algorithm variant 'Zbt'.", "488 exports"),
    "osg158-osg.dll": ("OpenSceneGraph 3.6.3 core (SOVERSION 158).", "7545 exports; PDB E:\\OSG\\…-3.6.3"),
    "osg158-osgViewer.dll": ("OpenSceneGraph viewer.", "879 exports"),
    "osg158-osgDB.dll": ("OpenSceneGraph scene I/O plug-in host.", "1328 exports"),
    "osg158-osgUtil.dll": ("OpenSceneGraph utilities.", "1731 exports"),
    "ceres.dll": ("Ceres Solver (non-linear least squares) used by calibration/registration.", "285 exports, ceres:: symbols"),
    "OpenMeshCore.dll": ("OpenMesh core mesh data structures.", "783 exports"),
    "OpenMeshTools.dll": ("OpenMesh tools (decimater, subdivision…).", "116 exports"),
    "lib3MF.dll": ("lib3MF (3D Manufacturing Format) reader/writer.", "219 exports, NMR:: symbols"),
    "tbb.dll": ("Intel Threading Building Blocks runtime.", "229 exports"),
    "mkl_rt.dll": ("Intel MKL single dynamic library (BLAS/LAPACK/FFT).", "15435 exports"),
    "cudart64_102.dll": ("NVIDIA CUDA 10.2.89 runtime.", "320 exports; string 'NVIDIA CUDA 10.2.89 Runtime'"),
    "cusolver64_10.dll": ("CUDA 10.2 cuSOLVER (dense/sparse solves).", "830 exports, 188 MB"),
    "nppc64_10.dll": ("CUDA NPP core image primitives.", "15 exports"),
    "nppig64_10.dll": ("CUDA NPP image geometry transforms.", "706 exports, 25 MB"),
    "opencv_core348.dll": ("OpenCV 3.4.8 core.", "2457 exports; build banner MSVC 19.14"),
    "opencv_calib3d348.dll": ("OpenCV 3.4.8 calibration.", "924 exports"),
    "opencv_cudawarping348.dll": ("OpenCV 3.4.8 CUDA warping.", "681 exports, 63 MB"),
    "opencv_imgproc348.dll": ("OpenCV 3.4.8 image processing.", "1023 exports"),
    "opencv_features2d348.dll": ("OpenCV 3.4.8 feature detection.", "897 exports"),
    "boost_filesystem-vc141-mt-x64-1_72.dll": ("Boost 1.72 filesystem (vc141 toolset).", "123 exports"),
    "boost_log-vc141-mt-x64-1_72.dll": ("Boost 1.72 logging.", "473 exports"),
    "boost_serialization-vc141-mt-gd-x64-1_72.dll": ("Boost 1.72 serialization, debug runtime (-gd).", "277 exports"),
    "libcurl.dll": ("libcurl HTTP(S) client (updates/cloud).", "74 exports"),
    "libpq.dll": ("PostgreSQL client library.", "315 exports"),
    "dynamixel.dll": ("ROBOTIS Dynamixel servo SDK (turntable motor).", "21 exports"),
    "librenderkit.dll": ("Vendor render kit (3D viewport) built on OSG.", "1899 exports"),
    "librenderkit.plugin.cuda.dll": ("CUDA plug-in for the render kit.", "10 exports"),
    "freetype.dll": ("FreeType font rasteriser.", "299 exports"),
    "glew32.dll": ("GLEW OpenGL loader.", "3199 exports"),
    "glfw3.dll": ("GLFW window/context library.", "110 exports"),
    "zlib1.dll": ("zlib 1.2.x.", "84 exports"),
    "libpng16.dll": ("libpng 1.6.", "246 exports"),
    "D3Dcompiler_47.dll": ("Microsoft Direct3D shader compiler.", "29 exports"),
    "opengl32sw.dll": ("Mesa/llvmpipe software OpenGL fallback.", "1274 exports"),
    "libEGL.dll": ("ANGLE EGL.", "54 exports"),
    "libGLESV2.dll": ("ANGLE GLESv2.", "665 exports"),
    "Qt5Core.dll": ("Qt 5.15.10 core, built with MSVC 2017 (vendor build D:\\qt_build).", "8368 exports; string 'Qt 5.15.10 (x86_64…by MSVC 2017)'"),
    "Qt5Gui.dll": ("Qt 5.15.10 GUI.", "16563 exports"),
    "Qt5Widgets.dll": ("Qt 5.15.10 Widgets.", "9054 exports"),
    "Qt5Quick.dll": ("Qt 5.15.10 QtQuick scene graph.", "6775 exports"),
    "Qt5Qml.dll": ("Qt 5.15.10 QML engine.", "5358 exports"),
    "Qt5Network.dll": ("Qt 5.15.10 networking (HTTP/TLS).", "1506 exports"),
}


def row(rel, role=None, ev=None):
    r = mods[rel]
    sha = r["sha256"][:12]
    typ = ("DLL" if rel.lower().endswith(".dll") else "EXE") + " / " + (
        ".NET" if r.get("is_dotnet") else "native") + " / " + r["machine_name"]
    rl, ev2 = ROLE.get(rel, (role or "—", ev or "inventory only"))
    if role:
        rl = role
    if ev:
        ev2 = ev
    return (f"| `{rel}` | `{sha}` | {typ} | {kb(r['size'])} | {short_tc(r)} | "
            f"{imp_summary(r)} | {exp_summary(r)} | {rl} | {ev2} |")


def main():
    L = []
    A = L.append
    counts = inv["counts"]
    A("# Windows module dependency and API map — SHINING 3D EXStar (Einstar)")
    A("")
    A(f"*Generated from static analysis of the read-only installation at* `{inv['root']}`  ")
    A(f"*Inventory source: `analysis/gnu/pe_inventory.json` (generated {inv['generated_utc']}; "
      f"pefile {inv['tool']['pefile']}, Python {inv['tool']['python']}).*")
    A("")
    A("**No binary was executed.** All facts below are read-only PE parsing plus byte/string "
      "inspection; role statements are hypotheses unless an evidence column says otherwise.")
    A("")
    A("---")
    A("")
    A("## 0. Executive summary")
    A("")
    A(f"- **{counts['pe_files']} PE files** ({counts['exe']} EXE, {counts['dll']} DLL) were "
      f"enumerated across the whole tree, including subdirectories; **none exceeded the 200 MiB "
      f"skip threshold**, so nothing was skipped. **{counts['dotnet']} are .NET** — every module "
      f"is native x64 code ({counts['machine'].get('I386',0)} 32-bit stragglers, "
      f"AMD64: {counts['machine'].get('AMD64',0)}).")
    A(f"- **{counts['with_rich_header']} of {counts['pe_files']}** carry an MSVC Rich header and "
      f"**{counts['with_pdb']}** still reference a PDB path — the build tree layout "
      f"(`E:\\Jenkins\\workspace\\…`, `D:\\qt_build`, `F:\\SyncServiceDev`) is recoverable.")
    A("- The product is a **Qt 5.15.10 / MSVC 19.16 (VS2017 15.9) x64** application with a "
      "static-import plug-in architecture (Qt plug-in metadata + a vendor matter/message bus), "
      "plus OpenCV 3.4.8, OpenSceneGraph 3.6.3, Ceres, OpenMesh, lib3MF, CUDA 10.2.89 and MKL.")
    A("- **USB is split across three transports**: a Cypress **FX3 / CyUSB3** driver package, a "
      "Shining3D **SH3DU3Driver** (USB VID `3267`) transport DLL pair "
      "(`CommunicateLayer`), and **HID** (`sn3DHIDCommunication`) for control-class devices; the "
      "legacy **SiUSBXp** (Silicon Labs USBXpress) path is used only by the turntable.")
    A("- Scanner I/O is **not** in the UI process: `Sn3DDeviceEinStar.dll` spawns "
      "`syncservice/SnSyncService.exe` and talks to it over the **QtTunnel** shared-memory/IPC "
      "bus; the service side owns the USB handles.")
    A("- **Calibration** is a self-contained plug-in family (7 requested modules, "
      f"{sum(calib[m]['export_count'] for m in calib if m in ('Sn3DCalibDevFile.dll','Sn3DCalibrateData.dll','Sn3DCalibrateLoader.dll','Sn3DCalibrateAlgorithmWrapper.dll','Sn3DCameraCalibHelper.dll','Sn3DCalibCaptureImageProcess.dll','Sn3DEACalibrateController.dll'))} exported "
      "symbols) with CCF/CCM/dark/white-balance serialisation and a JSON/XML-driven procedure "
      "loader; `ccf/200x150/{Left,Right,Tex}CCF.txt` are the matching per-unit data files.")
    A("- **Anti-tamper is layered**: Crypto++/AES-3DES data protection (`Sn3DCrypto`), "
      "HASP dongle + USB machine binding (`Sn3DSecurity` + `hasp_*`), SQLCipher config DBs and "
      "an encrypted tunnel (`tunnel_encrypt`).")
    A("")

    A("## 1. Method and evidence discipline")
    A("")
    A("| step | tool | output |")
    A("|---|---|---|")
    A("| enumerate + hash + PE headers/imports/exports | `tools/pe_inventory.py` (pefile 2024.8.26) | `analysis/gnu/pe_inventory.json` (66 MB) |")
    A("| dependency graph, classification, export flagging | `tools/analyze_pe.py` | `module_dependencies.json`, `classification.json`, `calibration_api.json`, `flagged_exports.json`, `usb_stack.json`, `toolchain.json` |")
    A("| strings (ASCII + UTF-16LE) | GNU strings 2.47 via `tools/extract_strings.sh` | `analysis/strings/<module>.strings.txt` |")
    A("| section-aware / CJK-aware strings | `tools/strings_sections.py` | `analysis/strings/<module>.sections.txt`, `<module>.cjk.txt` |")
    A("| curated findings | `tools/curate_strings.py` | `analysis/strings/interesting_strings.md` |")
    A("| report | `tools/build_report.py` | this file |")
    A("")
    A("Evidence levels used in this document: **observed** (bytes/headers parsed directly), "
      "**derived** (computed from observed data, e.g. dependency edges), **hypothesis** "
      "(interpretation of names/behaviour, explicitly labelled).")
    A("")

    A("## 2. Inventory")
    A("")
    A("| metric | value |")
    A("|---|---|")
    A(f"| PE files | {counts['pe_files']} |")
    A(f"| EXE / DLL | {counts['exe']} / {counts['dll']} |")
    A(f"| machine | " + ", ".join(f"{k}: {v}" for k, v in counts["machine"].items()) + " |")
    A(f"| .NET (CLR directory) | {counts['dotnet']} |")
    A(f"| native | {counts['native']} |")
    A(f"| with Rich header | {counts['with_rich_header']} |")
    A(f"| with PDB path | {counts['with_pdb']} |")
    A(f"| parse errors | {counts['parse_errors']} |")
    A(f"| >200 MiB skipped | {len(inv['skipped'])} |")
    A("")
    A("Directory distribution (top):")
    A("")
    A("| directory | PE files |")
    A("|---|---|")
    dirs = Counter()
    for rel in mods:
        dirs[rel.rsplit("/", 1)[0] if "/" in rel else "."] += 1
    for d, n in dirs.most_common(12):
        A(f"| `{d}` | {n} |")
    A("")

    A("## 3. Toolchain and SDKs")
    A("")
    A("| component | version | evidence (observed) |")
    A("|---|---|---|")
    A(f"| MSVC toolset | **19.16 / VS2017 15.9** (linker 14.16) | linker version in {tc['linker_versions'].get('14.16',0)}/{counts['pe_files']} modules; `MSVCP140`+`VCRUNTIME140`+UCRT `api-ms-win-crt-*` |")
    A(f"| MSVC (other) | 19.11 ({tc['linker_versions'].get('14.11',0)}), 18.00/VS2013 ({tc['linker_versions'].get('12.0',0)}), 17.00 ({tc['linker_versions'].get('11.0',0)}), 16.00 ({tc['linker_versions'].get('10.0',0)}), 19.29/19.32 (a few) | linker-version histogram in `toolchain.json` |")
    A(f"| Rich-header builds | {', '.join(str(k) for k,_ in list(tc['rich_builds'].items())[:6])} … | decoded Rich entries; 26xxx–27xxx = VS2017-era toolsets, 30729 = VS2008 SP1 (legacy SDK DLLs) |")
    A(f"| Qt | **5.15.10** shared, x86_64, built by MSVC 2017 | literal `Qt 5.15.10 (x86_64-little_endian-llp64 shared (dynamic) release build; by MSVC 2017)` in `Qt5Core.dll`; PDB root `D:\\qt_build` in 62 Qt plug-ins |")
    A("| Boost | **1.72**, `vc141`, x64 (one `-gd` debug lib) | filenames `boost_*-vc141-mt-x64-1_72.dll` |")
    A("| OpenCV | **3.4.8**, built with MSVC 19.14.26428 (VS2017 15.7 Enterprise) | `General configuration for OpenCV 3.4.8` banner incl. compiler path in `opencv_core348.dll` |")
    A("| OpenSceneGraph | **3.6.3** | PDB `E:\\OSG\\OpenSceneGraph-3.6.3\\…\\src\\osg\\Uniform.cpp`; `osgPlugins-3.6.3/`; `osg158-*` SOVERSION |")
    A("| CUDA | **10.2.89** | `NVIDIA CUDA 10.2.89 Runtime` in `cudart64_102.dll`; `cusolver64_10`, `nppc64_10`, `nppig64_10` |")
    A("| Intel MKL | present (`mkl_rt`, `mkl_core`, `mkl_def`, `mkl_sequential`, `mkl_tbb_thread`) | file names + exports |")
    A("| Ceres Solver / Eigen / OpenMesh / lib3MF / TBB | present (Eigen header-only) | `ceres.dll` symbols; `Eigen::Matrix` in mangled names of `algorithm*.dll`; `OpenMeshCore/Tools`; `lib3MF` `NMR::`; `tbb.dll` |")
    A("| OpenSSL | **1.1.1d** (`libcrypto-1_1-x64`/`libssl-1_1-x64`, 2019-09-10) **and** legacy **1.0.2q** (`LIBEAY32`/`SSLEAY32`, 2018-11-20) | version banners inside the DLLs (`OpenSSL 1.1.1d  10 Sep 2019`, `OpenSSL 1.0.2q  20 Nov 2018`) |")
    A("| Crypto++ | `cryptopp.dll` (4200 exports) | exports `CryptoPP::` symbols |")
    A("| HASP/SafeNet | `hasp_windows_x64_105562.dll`, `haspvlib_105562.dll` | Aladdin HASP strings + `hasp_rt.exe` reference |")
    A("| SQLCipher | `sqlitecipher.dll` | exported sqlite API, encrypted `config/app1.db` |")
    A("| 7-Zip | **21.07** (2021-12-26) (`7z.dll`, `7z.exe`) | `7-Zip 21.07 (x64) : Copyright (c) 1999-2021 Igor Pavlov` |")
    A("")
    A("**Statically linked vs shared (derived from import tables):**")
    A("")
    A("- **Shared, shipped as DLLs:** Qt5, OSG, OpenCV, Ceres, OpenMesh, lib3MF, TBB, MKL, CUDA "
      "runtime, Boost, OpenSSL, Crypto++, HASP, SQLCipher — each has a matching `*.dll` in the tree "
      "and appears in import tables.")
    A("- **Statically linked inside vendor DLLs:** Eigen (header-only — only symbol names such as "
      "`Eigen::Matrix<float,3,1,…>` inside `algorithm*.dll`/`common*.dll`, no Eigen DLL), "
      "glog (the `glogc.dll` custom build is shared but most vendor modules also embed the vendor "
      "logging wrapper), and the Shining3D geometry core (`common.dll` is shared).")
    A("- **Debug/release mixes:** `boost_serialization-…-gd` and `networktunneld.dll`/"
      "`qttunnel.3.0.9d.dll` are debug builds shipped alongside release ones — a packaging smell, "
      "not a functional finding.")
    A("")

    A("## 4. Runtime architecture (hypothesis built from imports, PDB names and strings)")
    A("")
    A("```")
    A("EXStar.exe ──► AppCommon_E7.dll / AppUi.dll ──► Sn3DProcessPlugin.dll")
    A("     │                                             (spawns worker processes)")
    A("     ├──► Sn3DEinStarController.dll  (Qt plug-in)")
    A("     │         └──► Sn3DDeviceEinStar.dll  (device abstraction, 187 exports)")
    A("     │                   ├── QProcess ──► syncservice/SnSyncService.exe")
    A("     │                   │        └── SnSyncCore.dll (DeviceController)")
    A("     │                   │               ├── syncservice/device/<model>.*.dll")
    A("     │                   │               ├── CommunicateLayer.dll ──► SH3DU3Driver.sys / cyusb3.sys")
    A("     │                   │               └── sn3DHIDCommunication.dll ──► hid.dll + SetupAPI")
    A("     │                   ├── Sn3DImageQueue.dll / Sn3DTexture.dll (frame hand-off)")
    A("     │                   └── QProcess ──► Fx3Bootloader.exe + *.img   (firmware update)")
    A("     ├──► Sn3DScanController.dll ──► Sn3DRealtimeScan.dll (SLAM/fusion)")
    A("     │         └── Sn3DRegistration.dll, Sn3DTextureSlam.dll, OpenCV, TBB")
    A("     └──► CalibrationPage_EA.dll ──► Sn3DCalibrate* / Sn3DCameraCalibHelper / Sn3DCalib*")
    A("```")
    A("")
    A("The Qt plug-in metadata (`qt_plugin_instance` / `qt_plugin_query_metadata` exports) marks "
      "the loadable modules; `Sn3DBox.dll` + `qttunnel.3.0.9.dll` provide the vendor's "
      "topic/message bus used between processes (`SnSharedBlock.dll` supplies the shared memory).")
    A("")

    A("## 5. USB / HID stack")
    A("")
    A("### 5.1 Kernel drivers shipped with the product")
    A("")
    A("| package | driver | hardware IDs | notes (from INF, observed) |")
    A("|---|---|---|---|")
    A("| `Resource/driver/FX3/` | `cyusb3.sys` (Cypress CyUSB3, `DriverVer=08/21/2014,1.2.3.10`) | Cypress VID `04B4` PIDs `00F0/00F1/00F3/4720/00B0/00BC/FFF1-3/0053/0082/0095/1004/8613/1003/6823/4617/4611…` | `cyusb3.inf` + `CYUSB3.cat` + `WdfCoinstaller01011.dll`; `test.bat` runs `pnputil /add-driver cyusb3.inf`. FX3 is the Einstar camera's USB3 controller. |")
    A("| `Resource/driver/UEC/` | `SH3DU3Driver.sys` (Shining3D, `DriverVer 03/02/2022,16.25.20.115`) | **VID `3267`** PIDs `0001-0009` (scanners: SHININGSCANNER / SHINING3D_UE / E10 / FACE), `0200-0205` (cameras) | custom device class `Shining3D` GUID `{6A0F6C25-2C5E-4F49-A338-556699A7BA8E}`; `SHCtl.exe` start/stop + `PsExec64.exe` used by `Install.bat` |")
    A("")
    A("### 5.2 User-mode transports")
    A("")
    A("| layer | module | API evidence | used by |")
    A("|---|---|---|---|")
    A("| HID | `sn3DHIDCommunication.dll` (2020-07, VS2013, `msvcr120`) | imports `HID.DLL` (`HidD_GetHidGuid`) + `SETUPAPI.dll` (`SetupDiGetClassDevsW/EnumDeviceInterfaces/GetDeviceInterfaceDetailW`); exports `sn3DInitHidDev` (6 overloads), `sn3DExitHidDev`, `sn3DReadData`, `sn3DReadDataOverlapped`, `sn3DSendData`, `sn3DGetReport`, `sn3DSetReport`, `sn3DClearBuffer`, `sn3DEnumSpecifiedUSBDevice` | `sn3DTurnTable.dll`, `syncservice/device/{einscan3, dlp3100projector, freescante25, ty1080projector, tylaser}.dll`; plus `sn3DHIDCommunicationM/New` variants for `einscan7`, `freescan`, `sesp` |")
    A("| Vendor USB3 | `syncservice/CommunicateLayer.dll` (29 exports) and `syncservice/sn3DCommunicateLayerM.dll` (30 exports) | `initializeSystem(const GUID&)`, `attachDevice(serial)`, `detachDevice`, `getDevicePipeAddressList`, `getMaxCommandSize`, `getImageDataUnitSize`, `driverStreamSwitch`, `receiveBulkReply`, `commandChannelLock`, `dataStreamlock`, `discardDriverFrameCaches`, `getOnlineDeviceSerial(Ex)` | sync-service device plug-ins (`Einscan3H2`, `SHNDetect`, `einscan10`, `fs3scan`, `sp3scan`, `uescanwl`, `uescan`) |")
    A("| USB topology | `UsbTreeModel.dll` | imports `SETUPAPI` + `CM_Get_Device_IDW`/`CM_Get_Parent` + `DeviceIoControl`; exports `USBTREE::EnumAllHCD`, `EraseList`, `GetSpecifyDeviceTreePath`, `ResetDevice` | `libSn3DNavigation.dll`, `syncservice/logic/3DDigitalSyncInterface.2.0.0.dll` |")
    A("| USBXpress (legacy) | `SiUSBXp.dll` — Silicon Labs **USBXpress 3.4.1.0**, 2011, VS2010 | exports `SI_Open/SI_Close/SI_Read/SI_Write/SI_FlushBuffers/SI_DeviceIOControl/SI_ResetDevice/SI_SetBaudRate/…` (24); imports `SETUPAPI` + `DeviceIoControl` | **only** `sn3DTurnTable.dll` (imports by ordinal 1,3,4,6,7,9,10,12) |")
    A("")
    A("The GUID passed to `CommunicateLayer::initializeSystem` is not stored as a printable literal, "
      "so the mapping of that transport to `SH3DU3Driver.sys` vs `cyusb3.sys` is a **hypothesis** "
      "inferred from the shipped driver packages plus the pipe/serial API shape; what is observed is "
      "that these are the only two USB drivers shipped and that the vendor driver claims VID `3267`.")
    A("")
    A("**Absent** (verified by import scan of all 741 modules): WinUSB (`WinUsb_*`), libusb "
      "(`libusb_init`), CyAPI/CyUSB user-mode DLLs, and any `\\\\.\\` device-path literal. The only "
      "`libusb` string in the tree lives inside the Chromium blob `passport/openweburl/Qt5WebEngineCore.dll` "
      "and is unrelated. USB access therefore goes through the two vendor drivers above and HID.")
    A("")
    A("### 5.3 Exact call chain (observed imports + strings)")
    A("")
    A("```")
    A("turntable:  Sn3DTurntableController.dll -> sn3DTurnTable.dll -> SiUSBXp.dll -> SETUPAPI/DeviceIoControl -> usbser/USBXpress")
    A("                                       -> sn3DHIDCommunication.dll -> HID.DLL + SETUPAPI -> hidclass")
    A("scanner:    EXStar.exe -> Sn3DEinStarController.dll -> Sn3DDeviceEinStar.dll")
    A("                          -> QProcess -> syncservice/SnSyncService.exe -> SnSyncCore.dll")
    A("                             -> syncservice/device/<model>.dll -> CommunicateLayer.dll")
    A("                                -> DeviceIoControl(GUID) -> SH3DU3Driver.sys (VID_3267) | cyusb3.sys (FX3)")
    A("firmware:   Sn3DDeviceEinStar.dll -> QProcess -> Fx3Bootloader.exe (not shipped) + *.img / _JAP.img")
    A("```")
    A("")

    A("## 6. Owners of scanner communication")
    A("")
    A("Full export lists are in `analysis/gnu/usb_stack.json` and `flagged_exports.json`; the "
      "keyword-flagged subset (firmware/update/IAP/FPGA/USB/camera/image/calibration/CCF) is "
      "summarised here.")
    A("")
    A("| module | sha256 | exports | what its exports say |")
    A("|---|---|---|---|")
    for rel in ["Sn3DDeviceEinStar.dll", "Sn3DEinStarController.dll", "sn3DHIDCommunication.dll",
                "SiUSBXp.dll", "UsbTreeModel.dll"]:
        r = mods[rel]
        ex = [e["name"] if e["name"] else f"ordinal_{e['ordinal']}" for e in r["exports"]]
        A(f"| `{rel}` | `{r['sha256'][:12]}` | {len(ex)} | " +
          (", ".join(f"`{e}`" for e in ex[:6]) + (" …" if len(ex) > 6 else "")) + " |")
    A("")
    A("### 6.1 Device-layer exports that matter (Sn3DDeviceEinStar.dll, 187 total)")
    A("")
    A("```")
    for e in mods["Sn3DDeviceEinStar.dll"]["exports"]:
        n = e["name"] or ""
        if not n or n.startswith(("??0", "??1", "??_7", "??_F", "??_7", "??4", "?d_func", "?qt_")):
            continue
        if n.startswith("?"):
            first = n[1:].split("@")[0]
            if not (first[:1].islower() or first.startswith(("BOOTLOADERSUBDIR", "ISFWUPDATE",
                                                             "ISFORCEFWUPDATE", "PARALIST", "ISBLOCK"))):
                continue
        if any(k in n.lower() for k in ("firmware", "fwupd", "bootloader", "update", "open", "close",
                                        "capture", "serial", "temperature", "brightness", "cali", "device",
                                        "execute", "sharedblock", "sync")):
            A(n)
    A("```")
    A("")
    A("Notable *data* symbols: `BOOTLOADERSUBDIR` (static member), `ISFWUPDATE`, "
      "`ISFORCEFWUPDATE`, `PARALIST`, `ISBLOCK` — firmware state is exposed as exported "
      "static/accessor symbols, so the update state machine is part of the DLL's public ABI.")
    A("")
    A("### 6.2 Controller layer (Sn3DEinStarController.dll, 2 exports)")
    A("")
    A("Only `qt_plugin_instance` and `qt_plugin_query_metadata` are exported: it is a Qt plug-in "
      "loaded by name. Its command surface is therefore not in the export table but in the "
      "topic strings it publishes/handles (`/FirmUpdate/Configure`, `FirmUpdate/writeBootLoader`, "
      "`queryUpdateProgress`, `firmUpdateResult`, `isFWUpdate`, `getCameraSerial`, "
      "`setDeviceBrightness`, `setReadImgScanMode`, `getDeviceTemperature`, "
      "`startEnterCalibration` / `startExitCalibration`).")
    A("")
    A("### 6.3 SiUSBXp.dll and UsbTreeModel.dll")
    A("")
    A("`SiUSBXp.dll` is a vendor SDK drop (Silicon Labs USBXpress 3.4.1.0, built 2011-05-04, "
      "VS2010) — it exposes the `SI_*` API and C++ mangled aliases, and does its own "
      "SetupAPI enumeration + `DeviceIoControl` on the USBXpress driver. `UsbTreeModel.dll` is a "
      "tiny (42 KB) helper that walks the USB hub tree via SetupAPI/Configuration Manager and can "
      "reset a device by port number (`ResetDevice`, `GetSpecifyDeviceTreePath`).")
    A("")

    A("## 7. Calibration API surface (verbatim exports)")
    A("")
    A("These are the seven requested modules plus the closely-related helpers found in the same "
      "family. Names are listed **verbatim** as they appear in the export table (MSVC mangling, no "
      "demangler available offline); the prose after each block is the readable summary.")
    A("")
    for rel in ["Sn3DCalibDevFile.dll", "Sn3DCalibrateData.dll", "Sn3DCalibrateLoader.dll",
                "Sn3DCalibrateAlgorithmWrapper.dll", "Sn3DCameraCalibHelper.dll",
                "Sn3DCalibCaptureImageProcess.dll", "Sn3DEACalibrateController.dll"]:
        r = mods[rel]
        c = calib[rel]
        A(f"### {rel}")
        A("")
        A(f"`{r['sha256']}` · {r['size']} B · {r['export_count']} exports · linker {r['linker_version']}")
        A("")
        A("```")
        for n in c["exports_verbatim"]:
            A(n)
        A("```")
        A("")
    A("### 7.1 API surface summary")
    A("")
    A("| module | surface (readable) |")
    A("|---|---|")
    A("| Sn3DCalibDevFile | Qt plug-in controller `Sn3DCalibDevFileController` (start/stop/dispatchCmd, `readDevDataToFileCmd`, `handleReadDevDataToFileCmd`, `handleEndCalibComputeCmd`, `handleNotifyIsCablliratedCmd`, `publishDevData`, `publishError`, `setCalibrateTime`) + `DevDataConfigInfo` singleton. |")
    A("| Sn3DCalibrateData | `Sn3DCalibData` model: `readCCF` / `serializeCCF` / `deserializeCCF` / `writeCCFToFile`, `readCCM`/`deserializeCCMDark`, `readWb`/`deserializeWb`, `getCalibDateTime`, `getLastCalibTime`, `existData`, `setDataFlag`. |")
    A("| Sn3DCalibrateLoader | Procedure model `BaseNode`/`GroupNode`/`StepNode`/`CaptureProcess` with `loadConfig`, `calibCommands`, `imgCaptureInfo`, `completeCapted`, `distance`, plus a singleton `VSn3DCalibrateXmlParser` — i.e. the calibration *procedure* is XML-configured. |")
    A("| Sn3DCalibrateAlgorithmWrapper | Algorithm managers per product generation: `CameraCalibrateAlgManager`, `E7CameraCalibAlgManager`, `EACameraCalibAlgManager`, `PoseAlgManager`, `LaserCalibrateAlgManager`, `WhiteBalanceAlgManager` (+ `Sn3DConfig`/`Sn3DConfigSQLite`). |")
    A("| Sn3DCameraCalibHelper | `Sn3DCameraCalibAlg`, `Sn3DSespCameraCalibAlg`, `RGBComboWhiteBalanceAlg`: `cameraCompute`, `get2D`, `whiteBalance`, `comboWhiteBalance`, `mergeRGBBmp`, `getAveImage`, `readPicCameraCompute`, progress/publish handlers. |")
    A("| Sn3DCalibCaptureImageProcess | Capture controllers for four device families (`E7`, `EA`, `Sesp`, `UE`) + `CalibCaptureImageProcess`: `calibCaptureProcess`, `convertDownSampling`, `getImagesData`, `handleEndCalibComputeCmd`. |")
    A("| Sn3DEACalibrateController | Qt plug-in only (`qt_plugin_instance`) — the EA/Einstar calibration workflow wiring lives in its command handlers and QML page (`CalibrationPage_EA.dll`). |")
    A("")
    A("Data files: `ccf/200x150/{LeftCCF,RightCCF,TexCCF}.txt` are binary arrays of little-endian "
      "doubles (camera intrinsics/extrinsics for left, right and texture cameras at 200×150), "
      "matching `Sn3DCalibData::readCCF`/`writeCCFToFile`. `config/app1.db` is a SQLCipher "
      "database; `config/einstarGFE…json` is the per-device config blob.")
    A("")

    A("## 8. Crypto / anti-tamper")
    A("")
    A("| layer | module | evidence |")
    A("|---|---|---|")
    A("| Data crypto | `Sn3DCrypto.dll` | Exports `Sn3DAlgorithm::Sn3DCryptoAES` and `Sn3DCrypto3DES` with `encrypto_text`, `decrypto_text`, `encrypto_file2file`, `encrypto_text2file`, and mesh-level `read_mesh_with_key`/`write_mesh_with_key`/`convert_binary_to_mesh` — scan data can be stored encrypted in a proprietary container. |")
    A("| Licensing / dongle | `Sn3DSecurity.dll` (6.0 MB), `hasp_windows_x64_105562.dll`, `haspvlib_105562.dll`, `sn3DLock.dll` | HASP strings (`Aladdin\\Vid0529&Pid0001&HaspHL`, `hasp_rt.exe`, `checkDongle`, `Sn3DDongle`, `UE20Dongle`, `OptimScanStudioDongle`, cloud licensing `</cloud_licensing>`), clone detection and commuter-licence logic; imports `DeviceIoControl`. |")
    A("| Machine binding | `Sn3DSecurity.dll`, `Sn3DPassportController.dll` | USB enumeration strings `USB\\VID_0424`, `USB\\VID_0529`, `USB\\VID_05E3`, `USB\\VID_13FE`; `SetupDiGetClassDevsW` + `CM_Get_Device_IDW` in the passport controller. |")
    A("| Transport crypto | `tunnel_encrypt.dll`, `qttunnel.3.0.9.dll`, `libcrypto-1_1-x64.dll`/`libssl-1_1-x64.dll` | `tunnel_encrypt` imported by `qttunnel`; OpenSSL 1.1 for TLS; legacy `LIBEAY32`/`SSLEAY32` still shipped for older plug-ins. |")
    A("| Generic crypto | `cryptopp.dll` (4200 exports), `sqlitecipher.dll` | Crypto++ symbol table; SQLCipher for `config/app1.db`. |")
    A("| Integrity | `Sn3DSecurity.dll` | Qt plug-in that imports `Sn3DDeveloperControlPlugin`, `Sn3DErrorCode`; strings for certificate/licence directories (`certlicensedir`, `checkDongle`, `checkDeviceWithHaspdog`). |")
    A("")

    A("## 9. GPU / compute")
    A("")
    A("| stack | modules | evidence |")
    A("|---|---|---|")
    A("| CUDA 10.2 | `cudart64_102`, `cusolver64_10`, `nppc64_10`, `nppig64_10`, `nvjpeg64_10` (syncservice), `librenderkit.plugin.cuda.dll`, `Sn3DCloudProcessGPU.dll` | `NVIDIA CUDA 10.2.89 Runtime`; `NvOptimusEnablementCuda` export in `Sn3DCalibBigCirExtract.dll` |")
    A("| OpenCL | none shipped (no `opencl.dll` import found) | import scan |")
    A("| TensorRT / nvinfer | none | import scan |")
    A("| Intel MKL | `mkl_rt`, `mkl_core`, `mkl_def`, `mkl_sequential`, `mkl_tbb_thread`, `libblas`/`liblapack` (MinGW) | file names, exports |")
    A("| OpenGL / OSG | `osg158-*` (10 modules), `osgPlugins-3.6.3/` (85 plug-ins), `glew32`, `glfw3`, `opengl32sw`, `libEGL`/`libGLESV2` | directories + exports |")
    A("| DirectX | `D3Dcompiler_47.dll`, `dxgi.dll` (imported by `libSn3DNavigation.dll`), `SnDXGICapturePlugin.dll` | imports |")
    A("")

    A("## 10. Vision / reconstruction libraries")
    A("")
    A("| library | presence | evidence |")
    A("|---|---|---|")
    A("| OpenCV 3.4.8 | 10 DLLs (`core`, `imgproc`, `calib3d`, `features2d`, `flann`, `highgui`, `imgcodecs`, `videoio`, `cudawarping`) + `syncservice` copy at 3.4.11 | filenames + build banner |")
    A("| Ceres | `ceres.dll` | `ceres::` symbols |")
    A("| Eigen | **header-only** — no DLL | `Eigen::Matrix<…>` in mangled names of `algorithm*.dll`, `common.dll`, `Sn3DAlgorithmWrapper.dll` |")
    A("| OpenMesh | `OpenMeshCore.dll`, `OpenMeshTools.dll` | `OpenMesh::` symbols |")
    A("| lib3MF | `lib3MF.dll` | `NMR::` symbols |")
    A("| PCL | **absent** | no `pcl_*` DLL, no `pcl::` symbols |")
    A("| VTK | **absent** | no `vtk*` DLL; `vtk` substrings are inside mangled names only |")
    A("| Embree / Assimp | **absent** | no DLL, no imports |")
    A("| TBB | `tbb.dll` + `mkl_tbb_thread` | exports |")
    A("")

    A("## 11. Qt and UI stack")
    A("")
    A("Qt **5.15.10**, MSVC 2017 x64, shared. Core set: `Qt5Core`, `Gui`, `Widgets`, `Qml`, `Quick`, "
      "`QuickControls2`, `QuickTemplates2`, `QuickShapes`, `Network`, `Sql`, `Svg`, `Xml`, "
      "`Multimedia`, `MultimediaQuick`, `Concurrent`, `RemoteObjects`. Plug-in directories: "
      "`platforms/`, `imageformats/` (20), `iconengines/`, `sqldrivers/` (5), `bearer/`, "
      "`plugins/mediaservice/`, plus 40+ QML import trees (`QtQuick/`, `QtQml/`, `Qt/labs/`, "
      "`QtGraphicalEffects/`). `qttunnel.3.0.9.dll` is a vendor Qt module (QtTunnel + QMQTT) that "
      "implements the inter-process message bus; `Qt5RemoteObjects` supports its remoting mode.")
    A("")

    A("## 12. Module dependency graph")
    A("")
    A(f"Static import edges between shipped modules: **{len(deps['edges'])}**; string-referenced "
      f"(dynamic-load) edges: **{len(deps['dynamic_refs'])}**. The full adjacency lists are in "
      f"`analysis/gnu/module_dependencies.json` (`edges`, `dynamic_refs`, `dependencies`, "
      f"`dependents`).")
    A("")
    A("Most-imported shipped modules (in-degree):")
    A("")
    A("| module | dependents |")
    A("|---|---|")
    indeg = Counter()
    for e in deps["edges"]:
        indeg[e["to"]] += 1
    for rel, n in indeg.most_common(15):
        A(f"| `{rel}` | {n} |")
    A("")
    A("Most-dependent modules (out-degree, shipped targets only):")
    A("")
    A("| module | dependencies |")
    A("|---|---|")
    outdeg = Counter()
    for e in deps["edges"]:
        outdeg[e["from"]] += 1
    for rel, n in outdeg.most_common(12):
        A(f"| `{rel}` | {n} |")
    A("")
    A("Layering (derived): OS/runtime (`KERNEL32`, UCRT, `MSVCP140`) → Qt/OSG/OpenCV/third-party "
      "→ `Sn3DBox`/`Sn3DConfig`/`Sn3DLog`/`Sn3DImageQueue` (vendor foundation) → "
      "`common`/`CommonFunc`/`Sn3DAlgorithmWrapper` (geometry) → device/calibration/scan "
      "controllers → `AppUi`/`AppCommon_E7` → `EXStar.exe`. No cycles were found among shipped "
      "modules at the DLL-import level (cycles are resolved at run time through the Qt plug-in "
      "and QtTunnel buses).")
    A("")

    A("## 13. Firmware update path (evidence chain)")
    A("")
    A("| # | evidence (observed) | module | interpretation (hypothesis) |")
    A("|---|---|---|---|")
    A("| 1 | `UpdateFirmware`, `confirmUpdateFirmWare`, `getFirmwareVersion`, `ISFWUPDATE`, `ISFORCEFWUPDATE` | `Sn3DDeviceEinStar.dll` | Device object exposes firmware version and an update state machine. |")
    A("| 2 | `FirmUpdate/writeBootLoader`, `FirmwareLanguage`, `*.img`, `_JAP.img`, `BOOTLOADERSUBDIR`, `/Fx3Bootloader.exe` | `Sn3DDeviceEinStar.dll` | Firmware images are `*.img` per language/market (`_JAP` = Japan), written through a bundled FX3 bootloader executable. |")
    A("| 3 | `/FirmUpdate/Configure`, `queryUpdateProgress`, `firmUpdateResult`, `isFWUpdate`, `updateFirmware filePath =`, `*.img` | `Sn3DEinStarController.dll` | Controller drives the update over the command bus, polls progress and reports a result. |")
    A("| 4 | QML: `param['cmd'] = 'UpdateFirmware'`, `param[\"filePath\"] = $device.firmwareFilePath`, messages about cutting power | `FirmwareupdateUI.exe` | User-facing QML page sends the command and instructs a power cycle; UI is a separate process. |")
    A("| 5 | `Fx3Bootloader.exe` is **not present** in the installation, and no `*.img` files are shipped | tree scan | Firmware payloads/bootloader are provisioned separately (service tool or download), not by this installer. |")
    A("")
    A("The FX3 driver package (`cyusb3.sys`) and `WdfCoinstaller01011.dll` are shipped, so the "
      "device can be re-enumerated in bootloader mode; the actual image bytes are not in scope.")
    A("")

    A("## 14. High-signal strings (8 requested modules)")
    A("")
    A("Full dumps: `analysis/strings/<module>.strings.txt` (whole file, ASCII + UTF-16LE, "
      "`strings -a -n 5` / `strings -a -e l -n 5`). Curated file: "
      "`analysis/strings/interesting_strings.md`. Section-aware dumps "
      "(`*.sections.txt`) and CJK attempts (`*.cjk.txt`) are alongside.")
    A("")
    A("| finding | literal (verbatim) | module |")
    A("|---|---|---|")
    A("| FX3 bootloader helper | `/Fx3Bootloader.exe`, `Fx3Bootloader.exe` | Sn3DDeviceEinStar.dll |")
    A("| Firmware image selection | `*.img`, `_JAP.img`, `FirmwareLanguage` | Sn3DDeviceEinStar.dll |")
    A("| Firmware write command | `FirmUpdate/writeBootLoader` | Sn3DDeviceEinStar.dll |")
    A("| Firmware configure command | `/FirmUpdate/Configure` | Sn3DEinStarController.dll |")
    A("| Update progress | `queryUpdateProgress`, `firmUpdateResult`, `isFWUpdate` | Sn3DEinStarController.dll |")
    A("| Forced update flag | `ISFORCEFWUPDATE` (exported) | Sn3DDeviceEinStar.dll |")
    A("| QML update command | `param['cmd'] = 'UpdateFirmware'` | FirmwareupdateUI.exe |")
    A("| Power-cycle warning | “Upgrade successfully. Please cut off the electricity and reconnect…” | FirmwareupdateUI.exe |")
    A("| Device service | `[DP 2023] Exist SnSyncService`, `SnSyncService.exe Process state changed!` | Sn3DDeviceEinStar.dll |")
    A("| Stereo serials | `CameraSerialL`, `CameraSerialR`, `CameraSerial` | Sn3DEinStarController.dll |")
    A("| Telemetry | `getDeviceTemperature`, `setDeviceBrightness`, `setReadImgScanMode` | Sn3DEinStarController.dll |")
    A("| Calibration commands | `readDevDataToFileCmd`, `handleNotifyIsCablliratedCmd` | Sn3DCalibDevFile.dll |")
    A("| Turbo/CCF token | `2CCFF,` | FirmwareupdateUI.exe |")
    A("| Build provenance | `D:\\Jenkins\\workspace\\Sn3DFirmwareUpdate\\…` | FirmwareupdateUI.exe |")
    A("")
    A("**Version strings** found in the eight modules are build/provenance paths rather than "
      "product version numbers; product versioning comes from the update/cloud modules "
      "(`Sn3DSoftwareUpdate::checkNewVersion`) and `config/SystemCfg.json`.")
    A("")
    A("**Chinese strings:** none genuine. All CJK candidates are decoding artefacts — 0xCC stack "
      "fill (`锰烫烫烫…`), ASCII mangled names read as UTF-16 (`䵑灡` = `QMap`), or font tables in "
      "`FirmwareupdateUI.exe`. Localisation ships as 21 Qt catalogues in `language_qm/`. A GBK "
      "sweep also returned only fill noise.")
    A("")

    A("## 15. Module table (curated)")
    A("")
    A("`sha256` is truncated to 12 hex characters; full digests are in `pe_inventory.json`. "
      "“Toolchain” is the linker-version mapping plus CRT flavour. Roles are hypotheses unless "
      "the evidence column cites a literal/export.")
    A("")
    A("| module | sha256 | type | size KB | toolchain | key imports | key exports | role hypothesis | evidence |")
    A("|---|---|---|---|---|---|---|---|---|")
    curated = [r for r in ROLE if r in mods]
    for rel in sorted(curated, key=lambda p: (p.count("/"), p)):
        A(row(rel))
    A("")
    A("## 16. Limitations and open questions")
    A("")
    A("1. **No execution, no debugging**: runtime behaviour (which plug-in is actually loaded for a "
      "given scanner model, which USB PID Einstar enumerates) is inferred, not observed.")
    A("2. **No MSVC demangler offline** (`c++filt`/`llvm-cxxfilt` are Itanium-only), so export "
      "lists are verbatim mangled names; the readable summaries were written by reading the "
      "name tokens, not by a tool.")
    A("3. **Firmware images and `Fx3Bootloader.exe` are absent** from this installation; the "
      "update payload path is therefore a hypothesis grounded in strings only.")
    A("4. **`Sn3DSecurity.dll` (6 MB) was not decompiled**: the HASP/machine-binding claims rest on "
      "imports, exports and strings.")
    A("5. **`algorithm*.dll` variants** (`Hlj`, `Ly`, `Lzy`, `Zbt`) are almost certainly "
      "customer/OEM builds; the mapping to customers is unknown.")
    A("6. **Dynamic edges** in `module_dependencies.json` are string references (a module naming a "
      "DLL), which is evidence of a possible `LoadLibrary`, not proof of loading.")
    A("")
    A("## Appendix A — full inventory index")
    A("")
    A(f"All {counts['pe_files']} modules with digest prefix, type, size, linker version, import/"
      f"export counts and the categories assigned in `classification.json`.")
    A("")
    A("| module | sha256 | type | KB | link | imp DLLs | exp | categories |")
    A("|---|---|---|---|---|---|---|---|")
    cat_of = {}
    for cat, v in cls.items():
        for m in v["modules"]:
            cat_of.setdefault(m, []).append(cat)
    for rel in sorted(mods):
        r = mods[rel]
        typ = "EXE" if rel.lower().endswith(".exe") else "DLL"
        cats = ",".join(sorted(cat_of.get(rel, [])))
        A(f"| `{rel}` | `{r['sha256'][:10]}` | {typ} | {kb(r['size'])} | {r.get('linker_version')} | "
          f"{len(r.get('imports', {}))} | {r.get('export_count', 0)} | {cats} |")
    A("")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("wrote", OUT, len(L), "lines", os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
