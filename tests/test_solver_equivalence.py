"""The fast analytic calibration backend must reproduce the SciPy reference.

The production path switched from SciPy TRF finite differences to the
analytic-Jacobian LM in ``calib.solvers``.  This test pins the numerical
equivalence on a synthetic scene and on the reduced-distortion configuration
used by the final profile (the configuration with the largest opportunity for
a silent divergence).
"""

from __future__ import annotations

import numpy as np
import pytest

from calib import synth
from calib.calibrate import CAMERAS, CameraIntrinsics, calibrate


@pytest.fixture(scope="module")
def scene():
    return synth.make_scene(n_views=7, pitch_mm=28.0, noise_px=0.2,
                            outlier_frac=0.0, seed=3)


@pytest.fixture(scope="module")
def init():
    return {c: CameraIntrinsics(1000.0, 1000.0, 640.0, 512.0, np.zeros(5))
            for c in CAMERAS}


@pytest.mark.parametrize("free_kc", [False, True])
def test_fast_backend_matches_scipy(scene, init, free_kc):
    ref = calibrate(scene.observations, scene.pitch_mm, init,
                    free_kc=free_kc, backend="scipy", max_nfev=400)
    fast = calibrate(scene.observations, scene.pitch_mm, init,
                     free_kc=free_kc, backend="fast", max_nfev=400)

    assert fast.info.get("solver", {}).get("backend") == "lm"
    # Same optimum (frame-invariant parameterisation).
    assert abs(fast.residual_rms - ref.residual_rms) < 2e-4
    d = np.abs(fast.vector() - ref.vector())
    assert d.max() < 2e-3
    assert abs(np.linalg.norm(fast.baseline()) -
               np.linalg.norm(ref.baseline())) < 2e-3
    # Covariance is computed from an analytic Jacobian instead of finite
    # differences; the diagonal must agree to a few percent.
    sd_ref = np.sqrt(np.maximum(np.diag(ref.covariance), 0.0))
    sd_fast = np.sqrt(np.maximum(np.diag(fast.covariance), 0.0))
    keep = sd_ref > 0
    ratio = sd_fast[keep] / sd_ref[keep]
    assert np.median(np.abs(ratio - 1.0)) < 0.05
    assert np.percentile(np.abs(ratio - 1.0), 95) < 0.15


def test_fast_backend_is_faster(scene, init):
    import time

    t0 = time.perf_counter()
    calibrate(scene.observations, scene.pitch_mm, init, free_kc=False,
              backend="scipy", max_nfev=400)
    slow = time.perf_counter() - t0
    t0 = time.perf_counter()
    calibrate(scene.observations, scene.pitch_mm, init, free_kc=False,
              backend="fast", max_nfev=400)
    fast = time.perf_counter() - t0
    # The measured ratio is ~7x on real sessions and ~4-8x here.  A 2x floor
    # keeps the test meaningful without being flaky on a loaded CI host.
    assert fast < slow / 2.0, (fast, slow)
