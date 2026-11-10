# Plate-scale validation against the vendor CCF (2026-11-10)

The captured installation's `res/Einscan-E10/200x150/*CCF.txt` are the vendor's own calibration
output on the same 25-image corpus: the device record was written atomically at **2026-02-09
21:34:58** and its three CCF slots are byte-identical to those files
(`docs/calibration/ccf_format.md` §7).  They are therefore a same-corpus reference for the
recovered plate geometry (`docs/calibration/plate_spec_recovered.md`).

| quantity | vendor CCF | engine @ 28.0 mm | engine @ 28.0 mm, free kc | engine @ 27.72 mm |
|---|---|---|---|---|
| IR baseline (L–R) | **158.557 mm** | 160.144 mm | 160.193 mm | **158.543 mm** |
| baseline vs CCF | — | +1.587 mm | +1.636 mm | **−0.014 mm** |
| IR relative rotation vs CCF | — | 0.202° | 0.429° | 0.202° |
| per-point RMS | — | 0.367 px | 0.358 px | 0.367 px |
| Δfx vs CCF (L/R/T) | — | −2.78 / −5.12 / −4.17 px | −3.06 / −6.07 / −4.70 px | same |

Reproduction:

```bash
OPENBLAS_NUM_THREADS=1 python3 -m calib.cli calibrate <corpus> \
  --factory sources/calibration/res_Einscan-E10_200x150 \
  --pitch 28.0 --no-anchor --out /tmp/calib_28.json
OPENBLAS_NUM_THREADS=1 python3 -m calib.cli calibrate <corpus> \
  --factory sources/calibration/res_Einscan-E10_200x150 \
  --pitch 27.72 --out /tmp/calib_2772.json
```

Raw numbers: `plate_scale_validation.json`.  Interpretation: the nominal vendor geometry is
28.0 mm; the vendor's realised CCF is 1.0 % below it (a solver difference, not a plate property),
so 27.72 mm is kept only as the CCF-reproduction scale.

**Colour camera.**  The same record's Tex CCF differs from the joint three-camera solve by
**17.66 mm** (relative rotation 0.14°), while the IR pair agrees to 0.76 mm.  The session reports
`hasTexture:false`, i.e. EXStar did not re-estimate the colour camera and kept the previous record;
this closes the colour-alignment discrepancy as a stale-record issue.
