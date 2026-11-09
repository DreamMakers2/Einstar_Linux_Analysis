"""Model of EXStar's rapid camera-calibration workflow.

Recovered from ``rapidCameraCalibrate.xml`` and the controller/capture DLLs
(see ``analysis/calibration/re/workflow_report.md``):

* 5 groups (board orientations) x 5 steps (working-distance bands) = 25 steps;
* 1 frame per step, 3 images per frame (left IR, right IR, texture) = 75 images;
* distance band [200, 600] mm split into five contiguous windows centred at
  240, 320, 400, 480, 560 mm, each +/- 40 mm;
* only group 0 (vertical, 0/0/180 deg) has an enforced angle gate (+/-8 deg);
  groups 1-4 are +-18 deg tilts but their angle thresholds are not evaluated;
* no per-step retry: a rejected pose is simply not committed, and "redo" is a
  full re-entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

#: distance band [min, max] in mm from the XML (distanceMid, distanceMax)
DISTANCE_BAND_MM = (200.0, 600.0)
#: number of steps per group
STEPS_PER_GROUP = 5
#: recovered step centres (mm) and half-width (thresDim)
STEP_CENTRES_MM = (240.0, 320.0, 400.0, 480.0, 560.0)
STEP_HALF_WIDTH_MM = 40.0
#: group orientations in degrees (x, y, z) as stored in the XML
GROUP_ANGLES_DEG = ((0.0, 0.0, 180.0), (-18.0, 0.0, 180.0), (18.0, 0.0, 180.0),
                    (0.0, 18.0, 180.0), (0.0, -18.0, 180.0))
#: angle gate active only for group 0, +/-8 deg
GROUP_ANGLE_ENFORCED = (True, False, False, False, False)
GROUP_ANGLE_THRESHOLD_DEG = ((8, 8, 8), (10, 8, 8), (10, 8, 8), (8, 10, 8), (8, 10, 8))


@dataclass(frozen=True)
class StepSpec:
    group: int
    step: int
    target_distance_mm: float
    distance_window_mm: tuple[float, float]
    target_angle_deg: tuple[float, float, float]
    angle_enforced: bool
    angle_threshold_deg: tuple[float, float, float]
    lateral_limit_mm: float = 40.0
    images: tuple[str, str, str] = ("left", "right", "tex")

    @property
    def label(self) -> str:
        return f"g{self.group}s{self.step}"


def exstar_sequence() -> list[StepSpec]:
    out = []
    for g in range(5):
        for s in range(STEPS_PER_GROUP):
            c = STEP_CENTRES_MM[s]
            out.append(StepSpec(g, s, c, (c - STEP_HALF_WIDTH_MM, c + STEP_HALF_WIDTH_MM),
                                GROUP_ANGLES_DEG[g], GROUP_ANGLE_ENFORCED[g],
                                GROUP_ANGLE_THRESHOLD_DEG[g]))
    return out


@dataclass
class GroupConfig:
    name: str
    index: int
    count: int
    distance_mid: float
    distance_max: float
    angle: tuple[float, float, float]
    angle_valid: bool
    thres: tuple[float, float, float]
    pt: tuple[float, float, float]
    thres_dim: float


@dataclass
class WorkflowConfig:
    groups: list[GroupConfig]
    exposure: dict = field(default_factory=dict)
    illumination: dict = field(default_factory=dict)
    source: str = ""

    @property
    def n_steps(self) -> int:
        return sum(g.count for g in self.groups)

    @property
    def n_images(self) -> int:
        return self.n_steps * 3

    def sequence(self) -> list[StepSpec]:
        return exstar_sequence()


def parse_rapid_config(path: str | Path) -> WorkflowConfig:
    root = ET.parse(path).getroot()
    groups = []
    groups_node = root.find("groups")
    children = list(groups_node) if groups_node is not None else []
    for g in children:
        if not g.tag.startswith("group"):
            continue
        idx = int(g.tag.replace("group", ""))
        pos = g.find("positionInfo")
        ang = pos.find("angle") if pos is not None else None
        pt = pos.find("pt") if pos is not None else None
        a = (float(ang.get("x", 0)), float(ang.get("y", 0)), float(ang.get("z", 0))) if ang is not None else (0, 0, 0)
        th = (float(ang.get("thresX", 0)), float(ang.get("thresY", 0)), float(ang.get("thresZ", 0))) if ang is not None else (0, 0, 0)
        groups.append(GroupConfig(
            name=g.get("name", str(idx)), index=idx, count=int(g.get("count", 0)),
            distance_mid=float(g.get("distanceMid", 0)), distance_max=float(g.get("distanceMax", 0)),
            angle=a, angle_valid=(ang is not None and ang.get("valid", "false") == "true"),
            thres=th,
            pt=(float(pt.get("x", 0)), float(pt.get("y", 0)), float(pt.get("z", 0))) if pt is not None else (0, 0, 0),
            thres_dim=float(pt.get("thresDim", 0)) if pt is not None else 0.0))
    exposure = {}
    gain = root.find(".//commands/command0/DevCommand/gainExposure")
    if gain is not None:
        for e in gain:
            exposure[e.get("id")] = {"gain": float(e.get("gain", 0)), "exp": float(e.get("exp", 0))}
    illum = {}
    start = root.find(".//command1[@type='start']/DevCommand")
    if start is not None:
        for e in start.iter():
            illum[e.tag] = dict(e.attrib)
    return WorkflowConfig(groups, exposure, illum, str(path))


def coverage_stats(poses: list[dict]) -> dict:
    """Summarise the geometric coverage of a pose set.

    Each pose must contain ``distance_mm`` and ``tilt_deg`` (a 2-tuple) or
    ``R`` (board->camera rotation; the tilt is derived from the normal).
    """
    d = np.array([p["distance_mm"] for p in poses], float)
    tilts = []
    for p in poses:
        if "tilt_deg" in p:
            tilts.append(p["tilt_deg"])
        else:
            R = np.asarray(p["R"], float)
            n = R @ np.array([0.0, 0.0, 1.0])
            tx = np.degrees(np.arctan2(n[0], -n[2]))
            ty = np.degrees(np.arctan2(n[1], -n[2]))
            tilts.append((tx, ty))
    tilts = np.array(tilts, float)
    return {"n": len(poses), "distance_min": float(d.min()), "distance_max": float(d.max()),
            "distance_levels": int(len(np.unique(np.round(d / 40.0)))),
            "tilt_x_range": (float(tilts[:, 0].min()), float(tilts[:, 0].max())),
            "tilt_y_range": (float(tilts[:, 1].min()), float(tilts[:, 1].max())),
            "tilt_magnitude_p50": float(np.median(np.linalg.norm(tilts, axis=1)))}
