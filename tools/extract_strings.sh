#!/bin/bash
# Full string dumps for the 8 key modules (ASCII + UTF-16LE), read-only on sources.
set -u
SRC="<repo>/sources/windows/EXStar"
OUT="<repo>/analysis/strings"
mkdir -p "$OUT"
MODS=(Sn3DDeviceEinStar.dll Sn3DEinStarController.dll FirmwareupdateUI.exe SiUSBXp.dll Sn3DCalibDevFile.dll Sn3DCalibrateLoader.dll Sn3DCameraCalibHelper.dll softwareUpgrade.exe)
for m in "${MODS[@]}"; do
  base="${m%.*}"
  {
    echo "# module: $m"
    echo "# sha256: $(sha256sum "$SRC/$m" | cut -d' ' -f1)"
    echo "# tool: GNU strings 2.47 ; ASCII: -a -n 5 ; UTF-16LE: -a -e l -n 5"
    echo "# --- ASCII (printable >=5) ---"
    strings -a -n 5 "$SRC/$m"
    echo "# --- UTF-16LE (>=5 chars) ---"
    strings -a -e l -n 5 "$SRC/$m"
  } > "$OUT/${base}.strings.txt"
  echo "$m -> $OUT/${base}.strings.txt $(wc -l < "$OUT/${base}.strings.txt") lines"
done
