# Partial-board / roll-sector bench — scope

`tools/wizard/partial_bench.py` measures the detection stack on real recorded
pixels plus controlled transforms of them.  See `REPORT.md` for the results.

Run:

```
.venv/bin/python -m tools.wizard.partial_bench --help
.venv/bin/python -m tools.wizard.partial_bench --max-views 8 \
    --families base,clip_edge,clip_corner,rotate,tilt \
    --out analysis/calibration/partial_board/measurements_after.json
```

No scanner is opened and no session is written: the store is read through the
read-only `ViewStore` replay path and every transform happens in memory.
