"""Provenance hashing must not trust an overwritten file's restored mtime."""
from __future__ import annotations

import os
from pathlib import Path

from tools.wizard.lifecycle import file_digest


def test_digest_cache_detects_equal_length_overwrite_with_restored_mtime(tmp_path):
    p = tmp_path / "raw.e1r"
    p.write_bytes(b"AAAA" * 1024)
    first = file_digest(p)
    st = p.stat()
    p.write_bytes(b"BBBB" * 1024)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert p.stat().st_size == st.st_size
    second = file_digest(p)
    assert second != first
