# Derived analysis metadata (and what is not published)

This directory contains **derived, non-proprietary metadata** produced by the analysis tools:
PE classifications, the FX3 symbol/role map, call-graph edges, firmware summaries and the
V1.0→V2.10 comparison.

It intentionally does **not** contain:

| Excluded input | Why | SHA-256 (so reviewers can verify they hold the same file) |
|---|---|---|
| `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img` | vendor firmware image | `52346b33c7b0337566d7cfe8d453533c46f18898e4e7f82c1c00c885a8c68c3d` |
| `EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_CH_IAP.img` | vendor firmware image | `0c5a41b5bfdc7f50fb6b55794c36fc4b6200ab28709d25b3e4b82d93fce6e7c5` |
| `EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img` | vendor recovery image | `067ce53da1cd00c2bab9b61ba3bc1111b631dbb5faac189c1c4a9e8a3b0c9e45` |
| `Fx3BootAppGcc.img` | vendor boot application | `2d3f346c190147727a6ed00753ad4d047c975b9603be9ac5b46d450ebb33713e` |
| `LeftCCF.txt` / `RightCCF.txt` / `TexCCF.txt` | factory calibration per unit | (obtain from your own licensed install) |
| device calibration dump (6568 B) | per-unit private data | not published |
| vendor session log (132 MB) | contains a device serial | not published |
| vendor DLLs/EXEs, decompiled listings, string dumps | proprietary | not published |

The **FPGA payload** is also not published, but its SHA-256 is noted throughout the reports
(`cca7860b22ec025ff5d7657257b1d3c342867045bd89b99c22ce03c189d057e4`) because it is a
deterministic function of the public firmware image.

## Fixture layout for the evidence-backed test suites

Create `fixtures/` (or set `EINSTAR_FIXTURES=/path/to/fixtures`):

```
fixtures/
  firmware/
    EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_EN_IAP.img
    EinScan10_01_SC130_FX3_V2.10_FPGA_V3.7_CH_IAP.img
    official_downloads/
      EinScan10_01_SC130_FX3_V1.0_FPGA_V2.6_CH.img
      Fx3BootAppGcc.img
  res_Einscan-E10_200x150/{Left,Right,Tex}CCF.txt
  ccf_200x150/{Left,Right,Tex}CCF.txt
  calibration.bin          # optional: your own device dump
  EinScan10_EN_fpga.bin    # optional: tools/firmware/fx3img.py --extract-fpga
  session.log              # optional: vendor sync-service log
```

Then:

```bash
EINSTAR_FIXTURES=$PWD/fixtures python3 -m pytest -q tests/
```

The fixture-backed tests validate the published parsers against real vendor data (320/320 record
checksums, the CCF uniqueness proof, the Pango IDCODE, and the log-derived protocol parameters).
Without fixtures they skip with an explanatory message; the synthetic suite still exercises every
parser.
