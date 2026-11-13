"""Test-process defaults.

The solver-equivalence test compares two optimisers on dense least-squares
problems.  Multi-threaded BLAS reductions can change the last few bits between
runs; pinning the test process to one BLAS thread keeps the numerical contract
deterministic.  This is a test setting, not a production performance setting.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
