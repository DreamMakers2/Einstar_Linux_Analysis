"""Live-sweep reconstruction measurement harness (Phase 5).

This package drives the existing C++ reconstruction CLI (``build/recon/recon_cli``)
over a real, live-captured three-camera sweep and quantifies the quality of the
resulting point clouds against the known calibration-plate geometry.

Nothing here re-implements the production pipeline: the sweep itself is
produced by ``recon_cli`` and this package only *reads* its outputs (JSON report,
disparity/depth/confidence maps) plus the raw IR frames for plate detection.
The single exception is :mod:`live_recon.matcher_py`, a line-by-line Python
replica of the C++ matcher used **only** for the one knob the CLI does not
expose (the left/right consistency tolerance); it is validated against the real
CLI on the same frames before any of its numbers are reported
(``experiments.json -> replica_validation``).

Modules
-------
common          file/JSON/percentile helpers, profile parsing
geometry_py     rectification model + depth-map -> 3-D points inversion
plate_eval      plate detection, PnP pose, plate-frame surface statistics
recon_runner    subprocess driver for recon_cli (and frame-directory rewriting)
matcher_py      Python replica of the C++ SGBM + L/R matcher (validated)
sweep           per-group processing and aggregation
repeatability   pose-space pairing and plate-frame repeatability
experiments     controlled one-variable-at-a-time experiments
figures         matplotlib (Agg) figures
report          recon_report.json + SUMMARY.md
"""

__version__ = "1.0.0"
