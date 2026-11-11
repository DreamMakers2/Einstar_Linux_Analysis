"""Vendor 25-view corpus as a wizard frame source.

The corpus is a directory of ``imageLeftNN.bmp`` / ``imageRightNN.bmp`` /
``imageTexNN.bmp`` triplets, discovered and decoded by the existing recorder
(``tools/calibsession/calibrate_session.py``) so the wizard reuses the same
loader, orientation matrix and hashes instead of a second implementation.

The corpus is a **still collection**, not a video stream, so
``SourceInfo.continuous`` is False: consecutive groups are independent
viewpoints and a large pose jump is expected.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import numpy as np

_HERE = Path(__file__).resolve()
for _p in (_HERE.parents[1], _HERE.parents[2]):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from wizard import types  # noqa: E402


def _loader():
    from tools.calibsession import calibrate_session as cs  # noqa: WPS433
    return cs


class VendorCorpusSource:
    """Yields oriented ``FrameGroup`` objects from the vendor corpus."""

    deterministic = True

    def __init__(self, directory: str | Path):
        cs = _loader()
        self.root = Path(directory)
        self.sets, self.info_raw = cs.discover_vendor_corpus(self.root)
        self.info = types.SourceInfo(
            kind="vendor-corpus", path=str(self.root),
            note=f"{len(self.sets)} views, still collection",
            continuous=False)
        self._closed = False

    # -- source protocol -------------------------------------------------
    def __iter__(self) -> Iterator[types.FrameGroup]:
        cs = _loader()
        for index, item in enumerate(self.sets):
            images: dict[str, np.ndarray] = {}
            colour = None
            present = True
            for key, camera in (("left", "left"), ("right", "right"), ("tex", "tex")):
                path = item.paths.get(camera)
                loaded = cs.load_image(camera, path)
                if loaded.gray is None:
                    present = False
                    continue
                gray = loaded.gray
                # The vendor corpus is stored already in calibration
                # orientation (verified: no rotation gives a relative pose of
                # 164.6 mm / 22.8 deg against the factory 158.6 mm / 22 deg;
                # rotating the right frame gives 785 mm / 164 deg).  Never
                # apply the live-stream 180 deg here.
                images[key] = np.ascontiguousarray(gray.astype(np.uint8))
            if not images:
                continue
            yield types.FrameGroup(
                index=index, device_group=None,
                timestamp_s=float(index) * 0.1,
                images=images, slot_ids={}, complete=present,
                source="vendor-corpus", label=item.label,
                colour_bgr=colour, continuous=False)

    def close(self) -> None:
        self._closed = True
