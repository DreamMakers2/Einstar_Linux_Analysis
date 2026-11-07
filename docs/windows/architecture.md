# EXStar runtime architecture (Einstar / EinScan10_01)

This is the *runtime* view: which process owns which resource, and where the protocol boundaries
are. The exhaustive static inventory (741 PE files, imports/exports, toolchain, 4.7k call edges)
is in `docs/windows/module_map.md` and `analysis/gnu/*.json`; this document explains the parts
that matter for a Linux reimplementation.

## 1. Process and IPC chain (verified)

```
EXStar.exe  (Qt 5.15.10 QML UI)
 └─ Sn3DEinStarController.dll        Qt plugin; scan session controller
     └─ Sn3DDeviceEinStar.dll        device object (Sn3DDeviceBase/…EinStar), firmware file
         │                           selection, calibration blob handling, image queue
         └─ QProcess + QtTunnel ("qttunnel.3.0.9.dll", TCP 1883 hub)
             └─ syncservice/SnSyncService.exe
                 └─ SnSyncCore.dll                plugin/command manager
                     └─ logic/3DDigitalSyncInterface.2.0.0.dll
                         │   implements the documented E10 API
                         │   (openDevices/getProperty/setProperty/setFlagBitInterface/execute)
                         └─ device/einscan10.2.0.0.dll
                             │   InternalE10Device + CaptureThread + ImageStreamThread
                             └─ CommunicateLayer.dll
                                 └─ SH3DU3Driver.sys / cyusb3.sys  → USB 3267:0003
```

Key consequences:

* The scanner is **not** opened by the UI process. It is owned by `SnSyncService.exe`, and the
  UI speaks a JSON/QVariant RPC over QtTunnel. This is why `EXStar.exe` imports no USB API.
* The **device plugin** (`einscan10.2.0.0.dll`) is the only module that knows the byte-level
  command protocol and the image-stream framing. It is the authoritative source used in
  `docs/protocol/einstar_usb_protocol.md`.
* `CommunicateLayer.dll` is a thin, named-export wrapper around the kernel driver:
  `initializeSystem(GUID)`, `attachDevice(serial)`, `sendCommand`, `sendCommandAndWaitReply`,
  `sendDataAndWaitReply`, `receiveImageStream`, `setDriverAttemptPkgSize`,
  `setDriverPkgCountPerUnit`, `setDriverCacheUnitCount`, `setMaxGrabWaitTime`,
  `resetCommandPipe`, `resetBulkPipe`, `driverStreamSwitch`, `getDevicePipeAddressList`.
  On Linux the equivalent is libusb + our own asynchronous pipeline.

### Why the vendor uses a kernel driver

`CommunicateLayer.dll` calls `CreateFileW` + `DeviceIoControl` + overlapped `ReadFile`/`WriteFile`
against a device interface discovered via `SetupDi*`. The Windows package ships two drivers:

| Driver | VID/PID | Use |
|---|---|---|
| `SH3DU3Driver.sys` (`SH3DU3Driver.inf`) | `USB\VID_3267&PID_0003` → `SHINING3D_E10` | normal operation |
| `cyusb3.sys` (`Resource/driver/FX3/cyusb3.inf`) | Cypress `VID_04B4` family | FX3 boot/recovery mode |

A Linux port therefore has to replicate the USB semantics **and** the timing/queueing behaviour
the driver provides (the vendor sets 0xA400-byte transfers, 32 packets/unit, 100 cached units,
100 ms grab timeout).

## 2. Firmware update path

```
FirmwareupdateUI.exe  (QML UI; embedded QML, no firmware bytes inside the .rdata)
  └─ publish "UpdateFirmware" with filePath
      └─ Sn3DDeviceEinStar/Sn3DEinStarController  (executeResultFWUpd, inFirmUpdate)
          └─ syncservice einscan10 plugin  FUN_18000bc80
              ├─ validates 4097-byte record checksums of the .img
              ├─ command system op 0x06, announcement + 0x1001-byte chunks over bulk EP2
              └─ device writes de-interleaved image to SPI flash (A/B slots)
```

Recovery (`Sn3DDeviceBase::restoreBlock`) instead spawns
`Fx3Bootloader.exe -i <file> -t SPI` via `cmd.exe /k "cd /d <dir> && …"`. That tool is not
shipped in 1.2.2.0; the 1.0.6.0 installer contains the recovery payloads
(`Fx3BootAppGcc.img` + a V1.0/V2.6 raw image) in `fabu_UPDATE/writeBootLoader/`.

## 3. Calibration data path

```
device flash bytes 0..6567  ──ReadData(0,6568)──►  Sn3DEinStarDeviceData::getEinStarCaliData(tagDevData&)
                                                     ├─ Sn3DCalibDevFile.dll   (DevDataConfigInfo, file I/O)
                                                     ├─ Sn3DCalibrateData.dll  (deserializeCCF/CCMDark/WB,
                                                     │                          writeCCFToFile)
                                                     └─ res/Einscan-E10/200x150/{Left,Right,Tex}CCF.txt
```

`tagDevData` is the in-memory calibration record set; EXStar persists it both to the device flash
(page 0, 6568 bytes) and to `res/.../CCF.txt` files. The `ReadData` offset/size pair is confirmed
by the real session log, which is how the private 6568-byte dump was obtained.

## 4. Vendor API surface (from `3DDigitalSyncInterface-E10.pdf`)

The abstract device API (`openDevices`, `getProperty`, `setProperty`, `setFlagBitInterface`,
`execute`, image/button callbacks, error enum) is documented in the vendor PDF and reproduced in
`docs/protocol/einstar_usb_protocol.md` §7. That API is a *good* target for a Linux control
plane: it is the vendor's own abstraction, and it is stable across Einstar-family devices.

## 5. Toolchain facts (for ABI reimplementation)

MSVC 19.16 (VS2017 15.9) x64, `/MD` UCRT; Qt 5.15.10 (vendor build, PDB `D:\qt_build`),
Boost 1.72 vc141, OpenCV 3.4.8, OSG 3.6.3, CUDA 10.2.89, MKL, Ceres, Eigen, OpenMesh, lib3MF,
TBB, OpenSSL 1.1.1d/1.0.2q, Crypto++, HASP, SQLCipher. 385/741 modules keep PDB paths; no .NET.

## 6. Open questions

* Whether `einscan10.2.0.0.dll` performs any device-side authentication before accepting
  `UpdateFirmware` (no obvious crypto import; `Sn3DCrypto.dll`/`Sn3DSecurity.dll` live in the UI
  process, not in the sync service).
* The exact semantics of the `ReadData`/`WriteData` opcode pair on the device side (the plugin's
  `execute` implementation is in the decompiled corpus at
  `analysis/ghidra/Einscan10Device/clean/0x180009c20.c`, 1079 lines, not yet fully mapped).
* Whether `_JAP.img` (a string in `Sn3DDeviceEinStar.dll`) is a Japan regional image or a typo
  for `_IAP.img`. No such file ships in any installer.
