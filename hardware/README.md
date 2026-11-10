# Hardware investigation — USB 3.0 SuperSpeed on the Einstar mainboard

Static, read-only investigation of the original SHINING 3D Einstar (`EinScan10_01`, USB `3267:0003`)
mainboard: **can this board carry a USB 3.0 SuperSpeed link?**

**Answer: not as originally concluded.** The board is **not** a USB-2.0-only design. The FX3's lower
edge carries **four conductors in two coupled pairs** that map in order to **A6, A5, A4 and A3**; the
TX pair passes a **two-pad footprint across the pair** (not in series); and all four route toward the
component-side 40-pin connector. No on-board series AC coupling is proved. An operator continuity
report adds **six connector-side points in three pairs**, corroborating the connector destination. The stock **7-pin external cable** still cannot carry SuperSpeed (one spare conductor
against four), so a **passive adapter remains NO-GO**. A **custom daughterboard** on the 40-pin
connector is **CONDITIONAL GO** — practical without BGA rework if two safe continuity measurements
identify the connector pins and a 200 Ω `R_USB3` reference is found.

Full reasoning: [`../docs/hardware/usb3_mainboard_feasibility.md`](../docs/hardware/usb3_mainboard_feasibility.md).
**Amended by** [`../docs/hardware/usb3_feasibility_addendum_1.md`](../docs/hardware/usb3_feasibility_addendum_1.md)
(a first pair found in the same strip) and **superseded by**
[`../docs/hardware/usb3_feasibility_addendum_2.md`](../docs/hardware/usb3_feasibility_addendum_2.md)
(four conductors, two pairs, a two-pad footprint across TX, conditional GO).

---

## Contents

| Path | What it is |
|---|---|
| `photos/` | the five supplied photographs, unmodified (`P1` top side, `P2–P4` back side, `P5` J4 close-up) |
| `annotated/A1_top_side_overview.png` | component-side architecture map |
| `annotated/A2_fx3_ball_field.png` | FX3 orientation proof + reconstructed 121-ball field + SS balls |
| `annotated/A3_bottom_side_overview.png` | back-side features, `DP`/`DN` and `CLK`/`PCLK` test points |
| `annotated/A4_signal_path_audit.png` | signal-path audit and the five missing SS requirements |
| `annotated/A5_ball_map.png` | documented ball map, colour-coded |
| `annotated/A6_row_a_escape.png` | 8× zoom of the row-A escape region, with the projected A3–A6 columns |
| `annotated/A7_multimeter_procedure.png` | where to probe, how to probe, and what each reading decides |
| `annotated/B1_escape_pair_measured.png` | the 45° coupled pair below the FX3, fitted and extrapolated to the package edge |
| `annotated/B2_routing_channel.png` | the six through-routes in the strip below the FX3 |
| `analysis/fx3_ballout.md` · `.json` | all 121 balls with citations; machine-readable |
| `analysis/reopen/` | 2026-11-08 reopen: net maps (`figs/N1`, `N2`), scripts, and the four independent agent reports |
| `analysis/fx3_ss_layout_requirements.md` | BOM, impedance/skew rules, BGA escape, SI of a wire tap |
| `analysis/product_teardown_corroboration.md` | public teardown/connector/vendor-software evidence |
| `analysis/fx3_docs/` | vendor PDFs used (local only, not redistributed) |
| `crops/` | the measurement crops quoted in the report |
| `annotate.py`, `make_annotations.py` | reproducible figure generation |

## The short version

* **FX3 = CYUSB3014-BZXI**, 121-ball, 10 × 10 mm, 0.8 mm pitch. SuperSpeed balls are **A3/A4 (SSRX−/+)**
  and **A5/A6 (SSTX+/−)** — all on the outermost row, 2.6–5.0 mm from one edge.
* Orientation is proved three ways: the A1 laser dot, two silkscreen direction arrows that both start at
  that corner, and the upside-down chip marking. In the photos A1 is the **bottom-right** corner.
* **Four conductors**, not two, cross the lower package edge. Their Hough lines extrapolate to
  **A6/A5** and **A4/A3** with the symmetric inward convergence of a differential-pair fan-out
  (`analysis/reopen/figs/N1_edge_netmap.png`). The earlier "no bundle of four" was wrong.
* The **TX pair** passes a two-pad footprint at image (376–470, 988–1084); both traces are continuous
  on both sides, so it is **across** the pair, not in series — an unpopulated footprint or a
  differential shunt, not the AC-coupling pair. **No on-board series AC coupling is proved**; the
  daughterboard must carry it.
* The four route left and up into the via field beside the **component-side 40-pin connector**. The
  final pin connection is on inner layers and is **not** optically proved.
* The four capacitors next to the solder-side connector are a **shunt** decoupling bank, not AC
  coupling — verified by zooming (`crops/P2_acbank_zoom.png`). They are not the parts in question.
* `DP`/`DN`/`CLK`/`PCLK` are the only labelled test points; no SS test pad was found.
* Firmware is not the blocker: the FX3 image already advertises SuperSpeed and calls
  `CyU3PConnectState(1,1)` at every connect site.

## What a photograph cannot settle

The balls are under the package and the board has inner layers. An **unseen** connection is not an
**absent** one, and this investigation does not claim otherwise. The two facts that remain open —
whether A3–A6 are routed at all, and whether the 200 Ω B3 resistor is fitted — need either an X-ray or
a multimeter (see §10 of the report for the exact procedure; a 20-minute resistor sweep is decisive).

## Regenerate the figures

```bash
python3 hardware/make_annotations.py
```

The script reads only `photos/*.webp`, applies a global grey-world white balance plus unsharp masking,
and draws every annotation as an overlay. No pixel of the original photographs is painted over.
