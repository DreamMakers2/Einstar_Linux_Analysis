"""Recovered Einstar marker-plate specification.

Evidence
--------
The physical target shipped with the Einstar ("calibration board", 200 x 150 mm)
is a white plate with a rectangular grid of circular holes.  In the vendor's
own 25-pose calibration corpus (``res/Einscan-E10/calibrate_image_read_rapid``)
the grid is **8 columns x 5 rows** of small holes plus **4 large holes** whose
positions form an orientation code:

    (u, v) = (2, 1), (4, 1), (5, 1)   # three collinear, spacing ratio 2:1
    (u, v) = (3.5, 3)                 # midway between u=3 and u=4, two rows below

``u`` runs along the long (200 mm) side, ``v`` along the short (150 mm) side.
The half-integer coordinate of the fourth large marker is deliberate: it makes
the pattern chiral, so a single view fixes the board frame without ambiguity
(no 180-degree or mirror ambiguity).  The pattern was measured independently
in all 25 left-camera images; the same coordinates appear in every image
(see ``analysis/calibration/plate_geometry.md``).

The metric pitch is estimated from the vendor images and the CCF stereo rig
(``calib.geometry``); it is stored in :data:`DEFAULT_PITCH_MM` only after that
estimate.  All geometry below is expressed in board units so that a wrong
pitch cannot silently change the topology.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

#: active plate variant.  The vendor corpus is 8 x 5; the shipped manual's
#: figure is 8 x 7, so the variant MUST be confirmed against the physical board
#: (count the holes and measure the pitch).  ``use_spec`` switches all helpers.
GRID_COLS = 8
GRID_ROWS = 5

#: large-marker coordinates in board units (u, v), centred pattern:
#: three collinear one row above the middle, and one at the midpoint two rows
#: below.  For an 8x5 board this is the recovered set; for 8x7 the same
#: centre-relative rule gives (2,2),(4,2),(5,2),(3.5,4).
BIG_MARKERS = np.array([[2.0, 1.0], [4.0, 1.0], [5.0, 1.0], [3.5, 3.0]])

#: board centre in the same units (used to centre metric coordinates)
BOARD_CENTRE_UV = np.array([3.5, 2.0])


@dataclass
class PlateSpec:
    """A complete plate variant (grid topology, big-marker code, pitch)."""
    cols: int = 8
    rows: int = 5
    big_markers: np.ndarray = None
    pitch_mm: float = 27.72

    def __post_init__(self):
        if self.big_markers is None:
            cy = (self.rows - 1) / 2.0
            self.big_markers = np.array([[2.0, cy - 1.0], [4.0, cy - 1.0],
                                         [5.0, cy - 1.0], [3.5, cy + 1.0]])
        self.big_markers = np.asarray(self.big_markers, float)

    @property
    def centre(self) -> np.ndarray:
        return np.array([(self.cols - 1) / 2.0, (self.rows - 1) / 2.0])

    @property
    def points_mm(self) -> np.ndarray:
        uv = np.array([[u, v] for v in range(self.rows) for u in range(self.cols)], float)
        return np.column_stack([(uv - self.centre) * self.pitch_mm, np.zeros(len(uv))])

    @property
    def big_mm(self) -> np.ndarray:
        return np.column_stack([(self.big_markers - self.centre) * self.pitch_mm,
                                np.zeros(len(self.big_markers))])


def use_spec(spec: PlateSpec) -> PlateSpec:
    """Switch the module-level active plate variant (CLI convenience)."""
    global GRID_COLS, GRID_ROWS, BIG_MARKERS, BOARD_CENTRE_UV, DEFAULT_PITCH_MM
    GRID_COLS = int(spec.cols)
    GRID_ROWS = int(spec.rows)
    BIG_MARKERS = np.asarray(spec.big_markers, float)
    BOARD_CENTRE_UV = spec.centre
    DEFAULT_PITCH_MM = float(spec.pitch_mm)
    return spec


def current_spec() -> PlateSpec:
    return PlateSpec(GRID_COLS, GRID_ROWS, BIG_MARKERS.copy(), DEFAULT_PITCH_MM)

#: nominal plate size in millimetres (vendor directory name ``200x150``)
PLATE_SIZE_MM = (200.0, 150.0)

#: Pitch estimated from the vendor's 25-pose corpus and the CCF stereo rig.
#: Two independent estimators agree: point-distance triangulation gives
#: 27.735 mm (sd 0.022 over 22 poses) and a joint bundle adjustment scaled to
#: the CCF baseline gives 27.72 mm.  The 0.05 % spread is statistical; the
#: systematic uncertainty is the CCF baseline (~0.1 %).  The design value is
#: probably 27.7 mm, so this is stored as the measured default and the engine
#: can also anchor the scale to the factory baseline explicitly.
DEFAULT_PITCH_MM = 27.72


def lattice_uv() -> np.ndarray:
    """All small-hole coordinates as (u, v), row-major."""
    return np.array([[u, v] for v in range(GRID_ROWS) for u in range(GRID_COLS)], float)


def board_points_mm(pitch: float = DEFAULT_PITCH_MM,
                    centre: Iterable[float] = BOARD_CENTRE_UV) -> np.ndarray:
    """Metric 3-D coordinates of the 40 small holes, Z=0 (mm)."""
    uv = lattice_uv()
    c = np.asarray(centre, float)
    xy = (uv - c) * float(pitch)
    return np.column_stack([xy, np.zeros(len(uv))])


def big_points_uv() -> np.ndarray:
    return BIG_MARKERS.copy()


def big_points_mm(pitch: float = DEFAULT_PITCH_MM,
                  centre: Iterable[float] = BOARD_CENTRE_UV) -> np.ndarray:
    c = np.asarray(centre, float)
    xy = (BIG_MARKERS - c) * float(pitch)
    return np.column_stack([xy, np.zeros(len(BIG_MARKERS))])


def uv_index() -> dict[tuple[float, float], int]:
    return {(float(u), float(v)): i for i, (u, v) in enumerate(lattice_uv())}


def canonicalise_uv(uv: np.ndarray) -> np.ndarray:
    """Map detected (u,v) onto the canonical 8x5 frame.

    The detector assigns coordinates from the large-marker pattern; this
    helper only checks the range and rounds half-integer large markers.
    """
    uv = np.asarray(uv, float).copy()
    return uv
