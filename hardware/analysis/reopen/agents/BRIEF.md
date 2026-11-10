# Shared brief — reopened FX3 USB3 hardware investigation (2026-11-08)

Read-only photographic/static analysis. No hardware access, no power, no soldering.

Source images (unmodified, lossy VP8 WebP):
- hardware/photos/P1_top_side.webp   2341x1199  (component side: FX3, FPGA, DDR3, 40-pin connector)
- hardware/photos/P2_bottom_side.webp 2283x1203 (solder side)
- hardware/photos/P3_bottom_side_2.webp 2312x1180
- hardware/photos/P4_bottom_side_3.webp 2260x1207
- hardware/photos/P5_connector_J4.webp 1172x489

Coordinate convention: source-image pixels, origin top-left, x right, y down.
Package (from a first pass, to be independently re-derived): body approx x 1171-1542, y 621-992;
nominal 10.00 mm body, 0.8 mm ball pitch => approx 37 px/mm. Verify, do not assume.

Safety: analysis only. Do not modify hardware/photos. Write only under
hardware/analysis/reopen/agents/ (and figures under hardware/analysis/reopen/figs/).
