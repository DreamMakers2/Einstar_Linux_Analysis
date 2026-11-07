# Firmware comparison: EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img → EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img

| | old | new |
|---|---|---|
| file SHA-256 | `067ce53da1cd00c2…` | `52346b33c7b03375…` |
| container | raw | records |
| image size | 1310720 | 1310720 |
| entry point | `0x40015488` | `0x40016F50` |
| image checksum | `0x91AEFEAA` | `0x0FE314BC` |

**Changed FX3 code/data bytes:** 130782

## Sections

| load address | status | size | changed bytes | diff ranges |
|---|---|---|---|---|
| `0x00000100` | resized | 8992 → 9512 (+520) | n/a (offsets shift) | n/a |
| `0x40003000` | same-size | 65536 | 65310 | 13 |
| `0x40013000` | same-size | 65536 | 65472 | 7 |
| `0x40023000` | resized | 17280 → 27956 (+10676) | n/a (offsets shift) | n/a |
| `0x40030000` | resized | 17032 → 21712 (+4680) | n/a (offsets shift) | n/a |

## FPGA payload

* old: offset `0x40190`, 1005676 B, `1b994d4d1c303050…`
* new: offset `0x40190`, 1005676 B, `cca7860b22ec025f…`
* identical: **False**, 526936 differing bytes

## Added / removed strings (layout-independent)

* old: 131 strings, new: 167 strings
* **added 55**, **removed 19**

### Added (first 40)

* `@M3XFMDNE``
* `@M3XFMDNEt`
* `After pstAppFlag->uiNewAppAddr: 0x%x, pstAppFlag->uiOldAppAddr: 0x%x`
* `Before pstAppFlag->uiNewAppAddr: 0x%x, pstAppFlag->uiOldAppAddr: 0x%x`
* `Bulk DMA Discard failed!`
* `Bulk DMA send failed!`
* `CLEAR_FEATURE Request `
* `Class Request Target Device`
* `Class Request Target Endpoint`
* `Class Request Target Interface`
* `Firmware size is %d, update starting...`
* `Firmware update error`
* `Firmware update over, firmware size %d, recive bytes %d`
* `Firmware update package size is 0x%x, TOO BIG`
* `Firmware update package ucCheckByte(0x%x) != ucCheckCal(0x%x)`
* `GET_CONFIGURATION Request `
* `GET_DESCRIPTOR Request `
* `GET_INTERFACE Request `
* `GET_STATUS Request `
* `Get Event! evtype=0x%x evdata=0x%x `
* `GetFpgaLoadAddr: 0x%x`
* `REQ 0x%x 0x%x 0x%x 0x%x 0x%x`
* `Reset endpoint1 IN`
* `Reset endpoint1 IN OK`
* `Reset endpoint1 OUT`
* `Reset endpoint1 OUT OK`
* `Reset endpoint2 IN`
* `Reset endpoint2 IN OK`
* `Reset endpoint2 OUT`
* `Reset endpoint2 OUT OK`
* `Reset endpoint3 IN`
* `Reset endpoint3 IN OK`
* `SET_ADDRESS Request `
* `SET_CONFIGURATION Request `
* `SET_DESCRIPTOR Request `
* `SET_FEATURE Request `
* `SET_INTERFACE Request `
* `SET_ISOCH_DELAY Request `
* `SET_SEL Request `
* `SYNCH_FRAME Request `

### Removed (first 40)

* `CallBack type = %x`
* `Commit Err: %x`
* `Count = %d `
* `DATA CHECK RIGHT `
* `DATA update over `
* `Data Check Error and Upgrade! Failed!`
* `FirmwareSize = %d`
* `IsSame!=0, err_count = %d. `
* `Not Handle USB event...%d`
* `Read back error check beyond 4 times and update firmware failed.`
* `Receive command ID=0x%x DataSize=%d`
* `RecvFirmwareSize = %d`
* `SpiWriteCount = %d`
* `THE FINAL CalcCheck = %x RxCheck_u=0x%x`
* `U_u USB enUSBSpeed: 0x%x, enPowerMode: 0x%x, uiConnectStatus: 0x%x, uiDisConnectCnt: 0x%x`
* `WriteI2C_FPGA Register=0x%x Val:0x%x 0x%x:0x%x:0x%x`
* `WriteI2C_seneor_register_for_sc130 err, write data`
* `bmReqType 0x%x bRequest 0x%x wValue 0x%x wIndex 0x%x wLength 0x%x`
* `read seneor_register from 3e08 failed`


## Most-changed functions (attributed to the NEW image)

| function | changed bytes |
|---|---|
| `FUN_40008d70` | 41368 |
| `FUN_400170c8` | 19526 |
| `FUN_4001d0e8` | 17109 |
| `FUN_40012ec0` | 16583 |
| `FUN_400051c8` | 15485 |
| `FUN_400035c0` | 5630 |
| `FUN_4001c3d8` | 4325 |
| `FUN_40021904` | 3707 |
| `FUN_4001bd10` | 2118 |
| `FUN_400225fc` | 2104 |
| `entry` | 1114 |
| `FUN_400033e0` | 730 |
| `FUN_40004bec` | 568 |
| `?` | 415 |

_Attribution uses the decompiled corpus of the new image; ranges outside any known function are reported as `?`._
