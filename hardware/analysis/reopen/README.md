# Reopen — FX3 lower-edge re-measurement (2026-11-08)

This directory contains the evidence package behind
[`docs/hardware/usb3_feasibility_addendum_2.md`](../../../docs/hardware/usb3_feasibility_addendum_2.md).

It was produced read-only from the five supplied photographs. No scanner was powered, probed,
soldered or modified. The photographs themselves are unchanged in `hardware/photos/`.

## Contents

| Path | What it is |
|---|---|
| `scripts/enhance.py` | raw / high-pass / green / edge views of the FX3 lower strip |
| `scripts/ridge_scan.py` | row-by-row ridge peaks, used to find the four diagonals |
| `scripts/hough_diag.py` | Hough transform over the silk gap (first line search) |
| `scripts/four_tracks.py` | the four fitted lines and the row-A extrapolation |
| `scripts/netmap_edge.py` | `figs/N1_edge_netmap.png` (measured lines over the photo) |
| `scripts/netmap.py` | `figs/N1` and `figs/N2` overlays |
| `scripts/netmap_schematic.py` | `figs/N3_netmap_schematic.png` (not to scale) |
| `figs/N1_edge_netmap.png` | **key figure** — four conductors, balls, edge crossings |
| `figs/N3_netmap_schematic.png` | net-by-net block map (not to scale) |
| `figs/comp_deconv.png` | Wiener deconvolution of the two-pad footprint |
| `figs/B_trace_map.png` | independent track map, T01–T16, pads, vias |
| `figs/A_geometry_overlay.png` | independent body-edge / ball-grid overlay |
| `figs/C_forensics_*.png` | block artefacts, re-encode stability, multi-image noise |
| `evidence/user_continuity_overlay.png` | operator's annotated **six-point continuity** report (external, unverified) |
| `agents/A_geometry.md` | independent ball-grid calibration |
| `agents/B_traces.md` | independent trace-topology pass |
| `agents/C_forensics.md` | independent compression/artefact audit |
| `agents/D_electronics.md` | independent FX3 SS front-end / measurement memo |

The `agents/*_work/` and `agents/*_geom/` directories are scratch (large `.npy` arrays, re-encoded
images) and are not published.

## Reproduce

```bash
python3 hardware/analysis/reopen/scripts/enhance.py
python3 hardware/analysis/reopen/scripts/ridge_scan.py
python3 hardware/analysis/reopen/scripts/four_tracks.py
python3 hardware/analysis/reopen/scripts/netmap_edge.py
python3 hardware/analysis/reopen/scripts/netmap_schematic.py
```

`four_tracks.py` prints the four line fits and the row-A extrapolation. The expected output is in
Addendum 2 §A2.2.

## Limits

* The only component-side photograph is a 0.50 bit/pixel lossy VP8 WebP. Feature positions are good
  to ±1–2 px, not the 0.1 px quoted in the earlier reports.
* The last ~1 mm under the package is invisible; the ball assignment is geometric inference.
* The final connection from the left-corridor via field to specific connector pins is on inner
  layers and is **not** optically established. Addendum 2 §A2.7 M1/M2 is the electrical test.
