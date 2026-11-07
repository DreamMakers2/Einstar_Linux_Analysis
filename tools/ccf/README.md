# CCF calibration tools

Decoder / validator for the SHINING 3D Einstar (EinScan10_01) CCF calibration
records.  Full format description: [`docs/calibration/ccf_format.md`](../../docs/calibration/ccf_format.md).

```bash
python3 tools/ccf/decode_ccf.py --txt <LeftCCF.txt>            # single record
python3 tools/ccf/decode_ccf.py --bin <calibration.bin> --json-out out.json
python3 tools/ccf/decode_ccf.py --print-key                    # IR key table
python3 tools/ccf/validate_ccf.py                              # 40 invariants
python3 tests/test_ccf.py                                      # 18 tests
```

## Requirements

* Python >= 3.10
* **numpy** (already installed here, 2.5.4) — the only hard dependency
* *optional*: **pytest** for `pytest tests/test_ccf.py -q`
  (`python3 -m pip install --user --break-system-packages pytest`);
  `tests/test_ccf.py` also runs standalone with no pytest at all
* scipy is **not** used

Nothing here touches the network, the USB device, or writes to the inputs.
