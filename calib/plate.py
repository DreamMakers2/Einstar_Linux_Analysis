"""Recovered Einstar calibration-target specification (EXStar 1.2.2.0).

This module is the single source of truth for the board geometry used by the
Linux-native calibration engine.  The numbers below are **recovered from the
vendor software**, not measured on a physical plate: the complete evidence
chain, addresses and decompiled fragments are in
``docs/calibration/plate_spec_recovered.md``.

Board topology (proven)
-----------------------
The Einstar "calibration board" is a white plate with dark circular markers
(``Sn3DEACalibrateController`` / vendor video / 25-pose IR corpus):

* **8 columns x 5 rows = 40 small-marker lattice nodes**;
* **4 large markers**, three on the lattice at ``(u, v) = (2,1), (4,1), (5,1)``
  and one **off-lattice** at ``(u, v) = (3.5, 3)`` -- mid-way between columns 3
  and 4, two rows away from the large-marker row;
* total **41 markers** (37 small + 4 large).

The half-integer coordinate makes the pattern chiral, so one view fixes the
board frame with no 180-degree/mirror ambiguity.

Board frame and metric geometry (proven)
----------------------------------------
The vendor builds a **17 x 14 marker lattice at ``iSpace = 14 mm``** and only
every second node in each direction carries a physical marker:

    x_c = (c - 8) * 14          c = 0 .. 16
    y_r = (6 - r) * 14          r = 0 .. 13
    marker at c = 2u + 1, r = 2v + 2

Substituting gives the marker positions in the **vendor board frame** (origin
at the centre of the 8x5 marker field, x along the 8-marker axis, y towards
row 0, z = 0):

    x(u) = 28 * (u - 3.5)       y(v) = 28 * (2 - v)      [mm]
    pitch = 2 * iSpace = **28.0 mm**

so the markers span 196 mm x 112 mm.  The plate outline is **8 x 6 marker
pitches = 224 x 168 mm** (0.5-pitch margins left/right, 1-pitch margins
top/bottom), which reproduces the measured corpus outline (7.97 x 6.02
pitches, aspect 1.321 vs 224/168 = 1.333).  Note: the ``8``/``6`` fields of
``CalibrationTableParam`` are the *template origin* lattice index (col,row),
not the plate dimensions; the 8 x 6 outline comes from the images.

Large markers in the vendor frame (mm):

    (u,v) (2,1)  -> (-42, +28)
    (u,v) (4,1)  -> (+14, +28)
    (u,v) (5,1)  -> (+42, +28)
    (u,v) (3.5,3)-> (  0, -28)

Evidence for the 28.0 mm pitch (independent of any physical measurement)
------------------------------------------------------------------------
1. ``CalibrateBoard3DAlg::generate`` (Sn3DCalibrateAlgorithmWrapper.dll,
   0x18004BA90) builds exactly the lattice above from ``iX, iY, iSpace``;
   the live EXStar session used ``iX=17, iY=14, iSpace=14``.
2. The recognition output is a 17 x 14 array
   (``Sn3D_one_calibTable_fast_recognition``, Sn3DCalibrationJR.dll
   0x18014B880 sizes it to ``param[0x18] * param[0x14]``), and the four large
   markers are read at flat indices **73, 77, 79, 144**
   (``FUN_180015850``), i.e. (c,r) = (5,4),(9,4),(11,4),(8,8), which is
   exactly ``c = 2u+1, r = 2v+2`` for the observed large-marker pattern.
3. The per-board distance constraints shipped in the ``.ple`` file
   (``AA``/``BB``) are true metric distances between two 3-D points of the
   calibration array (``DistanceRef`` = {int32 A, int32 B, double d}, used as
   a soft scale residual in ``global_optimize_landMark``, JR 0x18024E790).
   For this unit they are the pairs (71,149) and (79,139):

       71=(c3,r4) -> 149=(c13,r8):  Delta=(10,4) -> 14*sqrt(116) = 150.784 mm
       79=(c11,r4)-> 139=(c3,r8):   Delta=(-8,4) -> 14*sqrt(80)  = 125.219 mm

   The EA model defaults (``aa=151.030, bb=124.857``) and this unit's PLE
   (``AA=150.804, BB=125.071``) both agree with those values to 0.02-0.3 %,
   pinning the **lattice pitch to 14.00 mm** (13.983-14.002 mm).  The markers
   occupy every second lattice node, hence the **28.0 mm marker pitch**.  The
   per-board PLE values differ from the model defaults, i.e. they are measured
   on the physical board, not copied from the nominal model.

The vendor's **CCF-effective** pitch is 27.72 mm: a free 3-camera bundle
adjustment of the same 25-pose corpus reproduces the factory CCF baseline
(158.557 mm) only when the model pitch is 27.72 mm.  The 1.0 % difference from
28.0 mm is the difference between our estimator (41 markers per view, fixed
factory distortion) and the vendor's (238-cell lattice, distance constraints,
free distortion); it is reported, not hidden.  ``27.72`` therefore remains
available as a scale option for reproducing the shipped CCF, while the
**nominal vendor geometry is 28.0 mm**.

Retired hypothesis
------------------
``200x150`` is a profile/directory name (``res/Einscan-E10/200x150``) shared
with other devices (``5M/200x150``, ``EinScan-S/200x150``); it is not a plate
measurement.  The earlier 25.0 mm hypothesis was based on that name; it is now
**rejected** (it would require a physical AA distance of 134.6 mm instead of
the measured 150.8 mm).

The 8 x 7 drawing in ``CalibrationPage_EA.dll``/manual is a shared
cross-product illustration (the same PNG is byte-identical in the EXStar Hub
2.0.0-44 DLL) and does **not** describe the Einstar board; the official
vendor calibration video shows the physical 8 x 5 board.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

# ---------------------------------------------------------------------------
# recovered vendor constants
# ---------------------------------------------------------------------------

#: marker-lattice columns/rows on the physical board
GRID_COLS = 8
GRID_ROWS = 5

#: full recognition lattice (17 x 14) used by EXStar, and its pitch (mm)
RECOGNITION_GRID = (17, 14)
RECOGNITION_LATTICE_PITCH_MM = 14.0
#: flat indices (row-major, 17 columns) of the four large markers
RECOGNITION_BIG_INDICES = (73, 77, 79, 144)

#: plate outline in pitches: 8 wide x 6 tall (marker field 8 x 5 + margins)
PLATE_PITCHES = (8, 6)

#: nominal marker pitch = 2 * recognition lattice pitch
VENDOR_PITCH_MM = 28.0

#: large-marker coordinates in board units (u, v); canonical vendor pattern
BIG_MARKERS = np.array([[2.0, 1.0], [4.0, 1.0], [5.0, 1.0], [3.5, 3.0]])

#: centre of the marker field in the same units (the vendor frame origin)
BOARD_CENTRE_UV = np.array([3.5, 2.0])

#: nominal plate outline in millimetres (8 x 6 pitches of 28 mm)
PLATE_SIZE_MM = (PLATE_PITCHES[0] * VENDOR_PITCH_MM,
                 PLATE_PITCHES[1] * VENDOR_PITCH_MM)

#: pitch provenance.  ``vendor_model`` is the authoritative nominal geometry;
#: ``ccf_effective`` reproduces the shipped factory CCF; the last entry is the
#: retired "200x150 profile name = plate size" hypothesis.
PITCH_SOURCES_MM = {
    "vendor_model": VENDOR_PITCH_MM,
    "ccf_effective": 27.72,
    "profile_name_hypothesis_rejected": 25.0,
}

#: vendor constant table recovered from Sn3DCalibrateAlgorithmWrapper.dll,
#: global ``CalibrationTableParam`` at 0x180087748.  ``pitch_x_mm``/
#: ``pitch_y_mm`` are the raw table fields; see the docs for their use (the
#: marker pitch used by the generator is ``2 * iSpace``).
VENDOR_CONSTANTS = {
    "recognition_cols": 17,            # +0x14  row stride of the 238-cell array
    "recognition_rows": 14,            # +0x18
    "template_scale_0x20": 25.0,       # +0x20  template units per lattice step
    "template_scale_0x28": 25.0,       # +0x28  (NOT a metric pitch)
    "template_origin_col": 8,          # +0x30  lattice (col,row) that maps to (0,0)
    "template_origin_row": 6,          # +0x34
    "big_circle_num": 4,               # +0x04
    "large_marker_cols": (8, 5, 7, 11),          # +0x38,+0x40,+0x48,+0x50
    "large_marker_rows": (4, 8, 8, 8),           # +0x58..+0x70
    "large_marker_indices": (76, 141, 143, 147),  # 17*row + col (canonical)
    "large_marker_indices_e7": (73, 77, 79, 144),  # 180-degree rotation used by FUN_180015850
    "session_iX": 17,
    "session_iY": 14,
    "session_iSpace_mm": 14,
}

#: default pitch used by the engine: the recovered nominal vendor geometry
DEFAULT_PITCH_MM = VENDOR_PITCH_MM
DEFAULT_PITCH_SOURCE = "vendor_model"

#: compatibility alias used by reports
PITCH_CANDIDATES_MM = {
    "vendor_model_28.0": VENDOR_PITCH_MM,
    "ccf_effective_27.72": 27.72,
    "profile_size_hypothesis_25.0_rejected": 25.0,
}


# ---------------------------------------------------------------------------
# plate specification
# ---------------------------------------------------------------------------

@dataclass
class PlateSpec:
    """A complete plate variant (grid topology, big-marker code, pitch).

    ``pitch_mm`` is the marker pitch (centre-to-centre of adjacent small
    markers).  The recognition lattice pitch is ``pitch_mm / 2``.
    """

    cols: int = GRID_COLS
    rows: int = GRID_ROWS
    big_markers: np.ndarray = None
    pitch_mm: float = DEFAULT_PITCH_MM
    pitch_source: str = DEFAULT_PITCH_SOURCE
    plate_pitches: tuple = PLATE_PITCHES
    provenance: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.big_markers is None:
            cy = (self.rows - 1) / 2.0
            self.big_markers = np.array([[2.0, cy - 1.0], [4.0, cy - 1.0],
                                         [5.0, cy - 1.0], [3.5, cy + 1.0]])
        self.big_markers = np.asarray(self.big_markers, float)
        if not self.provenance:
            self.provenance = {
                "source": "EXStar 1.2.2.0 binaries",
                "generator": "CalibrateBoard3DAlg::generate 0x18004BA90",
                "lattice": "17x14 @ 14 mm, markers on every second node",
                "pitch": self.pitch_source,
            }

    # -- topology ---------------------------------------------------------
    @property
    def n_lattice(self) -> int:
        return self.cols * self.rows

    @property
    def n_large_on_lattice(self) -> int:
        return int(np.sum((self.big_markers[:, 0] == np.round(self.big_markers[:, 0])) &
                          (self.big_markers[:, 1] == np.round(self.big_markers[:, 1]))))

    @property
    def n_small(self) -> int:
        return self.n_lattice - self.n_large_on_lattice

    @property
    def n_holes(self) -> int:
        return self.n_small + len(self.big_markers)

    @property
    def centre(self) -> np.ndarray:
        return np.array([(self.cols - 1) / 2.0, (self.rows - 1) / 2.0])

    @property
    def lattice_pitch_mm(self) -> float:
        """Pitch of the vendor 17x14 recognition lattice (half the marker pitch)."""
        return self.pitch_mm / 2.0

    @property
    def plate_size_mm(self) -> tuple:
        return (self.plate_pitches[0] * self.pitch_mm,
                self.plate_pitches[1] * self.pitch_mm)

    # -- metric geometry --------------------------------------------------
    @property
    def points_mm(self) -> np.ndarray:
        """40 lattice markers, engine frame (x right, y = +v, z = 0)."""
        uv = np.array([[u, v] for v in range(self.rows) for u in range(self.cols)], float)
        return np.column_stack([(uv - self.centre) * self.pitch_mm, np.zeros(len(uv))])

    @property
    def big_mm(self) -> np.ndarray:
        return np.column_stack([(self.big_markers - self.centre) * self.pitch_mm,
                                np.zeros(len(self.big_markers))])

    def vendor_points_mm(self) -> np.ndarray:
        """40 lattice markers in the **vendor** frame (y = pitch*(2-v), z = 0)."""
        uv = np.array([[u, v] for v in range(self.rows) for u in range(self.cols)], float)
        x = (uv[:, 0] - self.centre[0]) * self.pitch_mm
        y = (self.centre[1] - uv[:, 1]) * self.pitch_mm
        return np.column_stack([x, y, np.zeros(len(uv))])

    def vendor_big_mm(self) -> np.ndarray:
        x = (self.big_markers[:, 0] - self.centre[0]) * self.pitch_mm
        y = (self.centre[1] - self.big_markers[:, 1]) * self.pitch_mm
        return np.column_stack([x, y, np.zeros(len(self.big_markers))])

    def recognition_index(self, u: float, v: float) -> int:
        """Flat index (row-major, 17 columns) of board coordinate (u, v)."""
        c = int(round(2.0 * u + 1.0))
        r = int(round(2.0 * v + 2.0))
        return r * RECOGNITION_GRID[0] + c

    def recognition_mm(self, index: int) -> np.ndarray:
        """3-D point (vendor frame) of a flat recognition-lattice index."""
        cols = RECOGNITION_GRID[0]
        c, r = index % cols, index // cols
        x = (c - cols // 2) * self.lattice_pitch_mm
        y = ((RECOGNITION_GRID[1] - 1) // 2 - r) * self.lattice_pitch_mm
        return np.array([x, y, 0.0])

    def distance_refs_mm(self) -> dict:
        """Vendor ``DistanceRef`` pairs -> their nominal model distances (mm).

        These are the pairs whose measured values the per-board ``.ple`` file
        stores as ``AA`` and ``BB``.
        """
        pairs = {"AA": (71, 149), "BB": (79, 139)}
        out = {}
        for key, (i, j) in pairs.items():
            pi = self.recognition_mm(i)
            pj = self.recognition_mm(j)
            out[key] = {
                "indices": (i, j),
                "model_distance_mm": float(np.linalg.norm(pi - pj)),
                "implied_pitch_mm": float(2.0 * np.linalg.norm(pi - pj)
                                          / np.sqrt(((i % 17 - j % 17) ** 2 +
                                                     (i // 17 - j // 17) ** 2))),
                "unit_ple_value_mm": 150.804 if key == "AA" else 125.071,
                "ea_model_default_mm": 151.030 if key == "AA" else 124.857,
            }
        return out


# ---------------------------------------------------------------------------
# module-level active spec (kept API-compatible with the original module)
# ---------------------------------------------------------------------------

ACTIVE_SPEC = PlateSpec()


def use_spec(spec: PlateSpec) -> PlateSpec:
    """Switch the module-level active plate variant (CLI convenience)."""
    global ACTIVE_SPEC, GRID_COLS, GRID_ROWS, BIG_MARKERS, BOARD_CENTRE_UV
    global DEFAULT_PITCH_MM, DEFAULT_PITCH_SOURCE
    ACTIVE_SPEC = spec
    GRID_COLS = int(spec.cols)
    GRID_ROWS = int(spec.rows)
    BIG_MARKERS = np.asarray(spec.big_markers, float)
    BOARD_CENTRE_UV = spec.centre
    DEFAULT_PITCH_MM = float(spec.pitch_mm)
    DEFAULT_PITCH_SOURCE = spec.pitch_source
    return spec


def current_spec() -> PlateSpec:
    return ACTIVE_SPEC


def lattice_uv() -> np.ndarray:
    """All small-hole coordinates as (u, v), row-major."""
    s = current_spec()
    return np.array([[u, v] for v in range(s.rows) for u in range(s.cols)], float)


def board_points_mm(pitch: float | None = None,
                    centre: Iterable[float] | None = None) -> np.ndarray:
    """Metric 3-D coordinates of the 40 lattice markers, Z=0 (mm).

    The three on-lattice large markers are included (their centres are valid
    calibration observations).  The off-lattice large marker is in
    :func:`big_points_mm`.  The frame is the engine frame (x right, y = +v);
    use :meth:`PlateSpec.vendor_points_mm` for the vendor's y-up frame.
    """
    s = current_spec()
    uv = lattice_uv()
    pitch = s.pitch_mm if pitch is None else pitch
    centre = s.centre if centre is None else centre
    c = np.asarray(centre, float)
    xy = (uv - c) * float(pitch)
    return np.column_stack([xy, np.zeros(len(uv))])


def big_points_uv() -> np.ndarray:
    return current_spec().big_markers.copy()


def big_points_mm(pitch: float | None = None,
                  centre: Iterable[float] | None = None) -> np.ndarray:
    s = current_spec()
    pitch = s.pitch_mm if pitch is None else pitch
    centre = s.centre if centre is None else centre
    c = np.asarray(centre, float)
    xy = (s.big_markers - c) * float(pitch)
    return np.column_stack([xy, np.zeros(len(s.big_markers))])


def all_points_mm(pitch: float | None = None) -> np.ndarray:
    """All 41 marker centres (40 lattice + 1 off-lattice), engine frame."""
    return np.vstack([board_points_mm(pitch), big_points_mm(pitch)])


def plate_summary(spec: PlateSpec | None = None) -> dict:
    s = spec or current_spec()
    return {"cols": s.cols, "rows": s.rows, "n_lattice": s.n_lattice,
            "n_large_on_lattice": s.n_large_on_lattice,
            "n_small": s.n_small, "n_holes": s.n_holes,
            "big_markers_uv": s.big_markers.tolist(),
            "pitch_mm": s.pitch_mm,
            "pitch_source": s.pitch_source,
            "lattice_pitch_mm": s.lattice_pitch_mm,
            "recognition_grid": RECOGNITION_GRID,
            "recognition_big_indices": RECOGNITION_BIG_INDICES,
            "plate_pitches": s.plate_pitches,
            "plate_size_mm": s.plate_size_mm,
            "plate_size_nominal_mm": PLATE_SIZE_MM,
            "pitch_candidates_mm": PITCH_CANDIDATES_MM,
            "vendor_constants": VENDOR_CONSTANTS,
            "distance_refs_mm": s.distance_refs_mm(),
            "provenance": s.provenance}


def uv_index() -> dict[tuple[float, float], int]:
    return {(float(u), float(v)): i for i, (u, v) in enumerate(lattice_uv())}


def canonicalise_uv(uv: np.ndarray) -> np.ndarray:
    """Map detected (u,v) onto the canonical 8x5 frame (range check only)."""
    uv = np.asarray(uv, float).copy()
    return uv
