"""Bridge between the new engine and the project's three-camera CCF model.

Two directions:

* :func:`to_ccf_records` turns a :class:`CalibrationResult` into the vendor's
  33-double CCF layout, preserving the factory IR trailer verbatim (its role
  is unresolved; storage_report.md O1).
* :func:`from_ccf_files` builds the same dictionaries the existing
  ``tools.calib.calib_model.Calibration`` consumes.

Nothing here writes to the scanner.  The output is a host-side profile.
"""

from __future__ import annotations

import numpy as np

from . import plate
from .calibrate import CAMERAS, CalibrationResult
from .ccf import CCFRecord, IR_KEY, COLOR_KEY


def _values(cam, R, T, error: float, alpha: float) -> np.ndarray:
    v = np.zeros(33)
    v[0] = cam.fx; v[1] = cam.fy
    v[4] = cam.cx; v[5] = cam.cy
    v[8] = alpha
    v[10:15] = cam.kc
    v[20:23] = T
    v[23:32] = np.asarray(R, float).ravel()
    v[32] = error
    return v


def to_ccf_records(result: CalibrationResult,
                   factory: dict[str, CCFRecord] | None = None,
                   world_view: int | None = 0) -> dict[str, CCFRecord]:
    if world_view is not None:
        result = result.rebased(world_view)
    out = {}
    for c in CAMERAS:
        R, T = result.extrinsics[c]
        alpha = result.cameras[c].skew
        # the IR obfuscation stores true+key; keep the same convention
        key = IR_KEY if c != "tex" else COLOR_KEY
        trailer = None
        if c != "tex":
            if factory is not None and c in factory and factory[c].ir:
                trailer = factory[c].raw[264:]
            else:
                trailer = bytes(256)
        raw = _values(result.cameras[c], R, T, result.residual_rms, alpha)
        # encode/decode round-trip through the same key path used on disk
        from .ccf import encode_record
        data = encode_record(raw, ir=(c != "tex"), trailer=trailer, key=key)
        out[c] = CCFRecord(raw=data, values=raw, key=key, name=f"{c}.CCF",
                           ir=(c != "tex"))
    return out


def from_ccf_files(paths: dict[str, str]) -> dict:
    """Load three CCF files into plain arrays for the legacy model."""
    from .ccf import load
    out = {}
    for c, p in paths.items():
        rec = load(p)
        out[c] = {"K": rec.K(), "dist": rec.dist(), "R": rec.R(), "T": rec.T()}
    return out


def result_summary(result: CalibrationResult) -> dict:
    return {
        "baseline_mm": float(np.linalg.norm(result.baseline())),
        "rig_mm": {
            "left_right": float(np.linalg.norm(result.baseline("left", "right"))),
            "left_tex": float(np.linalg.norm(result.baseline("left", "tex"))),
            "right_tex": float(np.linalg.norm(result.baseline("right", "tex"))),
        },
        "rms_px": result.residual_rms,
        "max_px": result.residual_max,
        "n_obs": result.n_observations,
        "n_params": result.n_parameters,
    }
