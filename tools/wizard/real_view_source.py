"""Replay real recorded calibration observations as a live-cadence stream.

The production view store already contains the accepted triplets of a physical
session.  This source turns a *curated* subset of them back into a
``~10 Hz`` stream so the complete wizard (controller, coverage, guidance, GUI,
final solve) can be rehearsed end-to-end on real images rather than only on
rendered scenes.

What is real and what is synthesized is explicit:

* every pixel is the recorded lossless PNG of that view (no re-render, no
  re-encoding, no Bayer round-trip);
* the *timing* is synthesized: distinct poses are 0.6 s apart (a hand move) and
  a chosen hold repeats the same real view at 0.1 s (a stationary hold).  The
  dwell is therefore a genuine repeated observation of the same recorded pose,
  not a fabricated measurement;
* the pose/distance/tilt/roll used for selection are the stored observations,
  and the wizard re-detects and re-solves them from the pixels anyway.

Optional ``extra_frames`` pull raw triplets from a capture frame ring; this is
how a genuine high-roll observation that the failed session never evaluated can
be included without pretending the old session was valid.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from . import sources, types

__all__ = ["RealViewSource"]


def _tokens(metrics: dict, spec: Any,
            region: tuple[int, int] | None = None) -> set[tuple]:
    """Coverage tokens one view contributes, for greedy selection."""
    out: set[tuple] = set()
    if not metrics:
        return out
    d = metrics.get("distance_mm")
    if d is not None:
        band = spec.band_index(float(d))
        if band is not None:
            out.add(("band", int(band)))
    tx = float(metrics.get("tilt_x_deg") or 0.0)
    ty = float(metrics.get("tilt_y_deg") or 0.0)
    if abs(tx) >= 8.0 or abs(ty) >= 8.0:
        out.add(("tilt", tx >= 0.0, ty >= 0.0))
    out.add(("roll", spec.roll_bin(float(metrics.get("roll_deg") or 0.0))))
    if region is not None:
        row, col = region
        out.add(("region", int(row), int(col)))
        if row in (0, 2) and col in (0, 2):
            out.add(("corner", int(row), int(col)))
    return out


def _rotate_images(images: dict, degrees: float,
                   principal_points: Mapping[str, Any] | None = None) -> dict:
    """Rotate a real triplet in-plane *about its optical axis*.

    The canonical centre of rotation is the principal point, not the image
    centre: the real cameras' principal points are tens of pixels off-centre
    (left 624/535, right 636/495).  Rotating about the image centre changes the
    effective principal point and produced 60-86 px residuals when the rotated
    view entered the solver.  Rotating about ``(cx, cy)`` leaves K exactly
    unchanged, which is what a camera rotated about its optical axis does.
    """
    import cv2
    out: dict[str, Any] = {}
    points = dict(principal_points or {})
    for key, image in images.items():
        h, w = image.shape[:2]
        cx, cy = points.get(key, (w / 2.0, h / 2.0))
        matrix = cv2.getRotationMatrix2D((float(cx), float(cy)), degrees, 1.0)
        out[key] = cv2.warpAffine(
            np.ascontiguousarray(image), matrix, (w, h),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
            borderValue=0)
    return out


def _pose_separation(a: dict, b: dict) -> tuple[float, float]:
    """(angular deg, translation mm) between two stored poses."""
    try:
        Ra = np.asarray(a["observation"]["pose"]["R"], float).reshape(3, 3)
        Rb = np.asarray(b["observation"]["pose"]["R"], float).reshape(3, 3)
        ta = np.asarray(a["observation"]["pose"]["t"], float).reshape(3)
        tb = np.asarray(b["observation"]["pose"]["t"], float).reshape(3)
    except Exception:
        return (180.0, 1e6)
    ang = math.degrees(math.acos(float(np.clip(
        (np.trace(Ra @ Rb.T) - 1.0) / 2.0, -1.0, 1.0))))
    return ang, float(np.linalg.norm(ta - tb))


def _min_separation(item: dict, chosen: list[dict]) -> tuple[float, float]:
    if not chosen:
        return (180.0, 1e6)
    sep = [_pose_separation(item, other) for other in chosen]
    return (min(s[0] for s in sep), min(s[1] for s in sep))


def _image_region(image_points: dict) -> tuple[int, int] | None:
    for cam in ("left", "right", "tex"):
        rows = image_points.get(cam) or []
        if not rows:
            continue
        pts = np.asarray(rows, float).reshape(-1, 2)
        if not len(pts):
            continue
        cx, cy = pts.mean(axis=0)
        col = int(np.clip(cx / types.IMAGE_WIDTH * 3.0, 0, 2))
        row = int(np.clip(cy / types.IMAGE_HEIGHT * 3.0, 0, 2))
        return row, col
    return None


def _load_raw_group(frames_dir: Path, gid: int):
    """Decode one raw capture triplet (cam0=right, cam1=left, cam2=Bayer)."""
    files = {0: frames_dir / f"frame_{gid:06d}_cam0.pgm",
             1: frames_dir / f"frame_{gid:06d}_cam1.pgm",
             2: frames_dir / f"frame_{gid:06d}_cam2.pgm"}
    if not all(p.is_file() for p in files.values()):
        return None
    raw0 = types.read_pgm(files[0])
    raw1 = types.read_pgm(files[1])
    raw2 = types.read_pgm(files[2])
    bgr = types.debayer_bgr(raw2)
    return {"left": types.orient_left(raw1),
            "right": types.orient_right(raw0),
            "tex": types.grey_from_bgr(bgr)}


class RealViewSource:
    """Yield selected real views and real holds at a live-like cadence."""

    kind = "views"
    deterministic = True

    def __init__(self, session_dir: str | Path, *,
                 select: int = 0, holds: int = 7,
                 hold_repeats: int = 5,
                 extra_frames: Sequence[int] = (),
                 frames_dir: str | Path | None = None,
                 rotated_views: Sequence[float] = (),
                 principal_points: Mapping[str, Any] | None = None,
                 vendor_dir: str | Path | None = None,
                 vendor_indices: Sequence[int] = (),
                 move_dt_s: float = 0.6,
                 hold_dt_s: float = 0.1) -> None:
        from . import coverage, session_io

        self.root = Path(session_dir).expanduser()
        self.spec = coverage.CoverageSpec()
        self.select = max(0, int(select))
        self.holds = max(0, int(holds))
        self.hold_repeats = max(0, int(hold_repeats))
        self.move_dt_s = float(move_dt_s)
        self.hold_dt_s = float(hold_dt_s)
        self.rotated_views = tuple(float(x) for x in rotated_views)
        self.principal_points = dict(principal_points or {})
        self.vendor_dir = (Path(vendor_dir).expanduser()
                           if vendor_dir else None)
        self.vendor_indices = tuple(int(x) for x in vendor_indices)
        self.skipped: list[dict] = []
        self._closed = False

        store = session_io.ViewStore(self.root)
        store.reload()
        candidates: list[dict] = []
        for view in store.iter_views():
            images = view.get("images") or {}
            if not all(k in images for k in ("left", "right", "tex")):
                continue
            observation = view.get("observation") or {}
            metrics = coverage.pose_metrics(observation) or {}
            quality = observation.get("quality") or {}
            qmetrics = quality.get("metrics") or {}
            # A view that the stored session accepted can still be a poor
            # rehearsal sample if it was motion-blurred at the time (the two
            # high-roll tail frames are exactly that).  Keep sharp views when
            # the metric exists; never invent one.
            focus = qmetrics.get("left.focus")
            if quality and quality.get("passed") is False:
                self.skipped.append({"label": view.get("record", {}).get("label"),
                                     "reason": "stored quality failed"})
                continue
            if focus is not None and float(focus) < 200.0:
                self.skipped.append({"label": view.get("record", {}).get("label"),
                                     "reason": f"stored focus {float(focus):.0f}"})
                continue
            candidates.append({
                "images": {k: np.ascontiguousarray(images[k])
                           for k in ("left", "right", "tex")},
                "observation": observation,
                "metrics": metrics,
                "region": _image_region(observation.get("image_points") or {}),
                "label": str((view.get("record") or {}).get("label")
                             or f"view {observation.get('view_id')}"),
                "source": "recorded-view",
            })
        if frames_dir is not None:
            frames_dir = Path(frames_dir).expanduser()
            for gid in extra_frames:
                images = _load_raw_group(frames_dir, int(gid))
                if images is None:
                    self.skipped.append({"group": int(gid),
                                         "reason": "raw triplet missing"})
                    continue
                candidates.append({
                    "images": images, "observation": {}, "metrics": {},
                    "region": None, "label": f"raw frame {int(gid)}",
                    "source": "recorded-frame",
                })
        if not candidates:
            raise sources.SourceError("no usable recorded views found")
        self._candidates = candidates
        selected = self._select(candidates) if self.select else list(candidates)
        # Extras are included verbatim: they are the real high-roll evidence the
        # failed session never evaluated, so they must not be dropped by a
        # greedy set-cover that has no stored pose for them.
        for item in candidates:
            if item["source"] == "recorded-frame" and all(
                    item is not chosen for chosen in selected):
                selected.append(item)
        # Controlled geometric surrogate for a sector the physical session
        # never captured sharply: rotate a *real* centred view by 90 deg.  The
        # pixels remain recorded data; only the in-plane orientation is
        # resampled.  This is labelled as rotated and is never counted as a
        # physical capture in the report.
        # Real views from another recording of the same plate and scanner.
        # Used only when the current recording never captured a sharp view of
        # a mandatory sector; they are labelled by source and are not synthetic.
        if self.vendor_dir is not None and self.vendor_indices:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from tools.calibsession import calibrate_session as cs
                sets, _ = cs.discover_vendor_corpus(self.vendor_dir)
                for index in self.vendor_indices:
                    if not 0 <= index < len(sets):
                        self.skipped.append({"index": index,
                                             "reason": "vendor index out of range"})
                        continue
                    item = sets[index]
                    images = {}
                    for cam in ("left", "right", "tex"):
                        loaded = cs.load_image(cam, item.paths[cam])
                        if loaded.gray is None:
                            images = {}
                            break
                        images[cam] = np.ascontiguousarray(
                            loaded.gray.astype(np.uint8))
                    if not images:
                        self.skipped.append({"index": index,
                                             "reason": "vendor image unreadable"})
                        continue
                    selected.append({
                        "images": images, "observation": {}, "metrics": {},
                        "region": None,
                        "label": f"vendor {item.label}", "source": "vendor-view"})
            except Exception as exc:
                self.skipped.append({"vendor": str(exc)})
        if self.rotated_views:
            import cv2
            centre = next((item for item in selected
                           if item.get("region") == (1, 1)
                           and item["source"] == "recorded-view"), None)
            if centre is None:
                centre = next((item for item in selected
                               if item["source"] == "recorded-view"), None)
            if centre is not None:
                for degrees in self.rotated_views:
                    rotated = dict(centre)
                    rotated["images"] = _rotate_images(
                        centre["images"], float(degrees),
                        self.principal_points)
                    rotated["source"] = "rotated-view"
                    rotated["metrics"] = {}
                    rotated["region"] = centre.get("region")
                    rotated["label"] = (f"{centre['label']} "
                                        f"({degrees:+.0f} deg rotated)")
                    selected.append(rotated)
        selected = self._coverage_refine(selected, candidates)
        self.selected = selected
        self._groups = list(self._build(selected))
        self.info = types.SourceInfo(
            kind="views", path=str(self.root),
            note=(f"{len(selected)} recorded views, {self.holds} real holds, "
                  f"{len(self._groups)} groups"),
            width=types.IMAGE_WIDTH, height=types.IMAGE_HEIGHT,
            continuous=True)

    # -- selection -------------------------------------------------------
    def _select(self, candidates: list[dict]) -> list[dict]:
        """Greedy set cover of distance bands, tilt quadrants and roll bins."""
        wanted: set[tuple] = set()
        for item in candidates:
            wanted |= _tokens(item["metrics"], self.spec, item.get("region"))
        chosen: list[dict] = []
        covered: set[tuple] = set()
        remaining = list(candidates)
        while remaining and len(chosen) < self.select:
            def score(item):
                gain = _tokens(item["metrics"], self.spec,
                               item.get("region")) - covered
                sep_deg, sep_mm = _min_separation(item, chosen)
                # Diverse, non-redundant poses first; the acceptance path
                # rejects a near-duplicate, so selecting one only wastes a
                # group.  Tokens still dominate (a new band/region is worth
                # more than a large separation).
                return (len(gain), sep_deg >= 6.0 and sep_mm >= 15.0,
                        sep_deg, sep_mm)

            best = max(remaining, key=score)
            chosen.append(best)
            covered |= _tokens(best["metrics"], self.spec, best.get("region"))
            remaining = [item for item in remaining if item is not best]
            # Never include a near-duplicate: the acceptance path rejects it,
            # so it would only spend a group and distort the rehearsal.
            remaining = [
                item for item in remaining
                if (_min_separation(item, chosen)[0] >= 4.0
                    or _min_separation(item, chosen)[1] >= 10.0
                    # a view that still supplies an uncovered dimension is
                    # never dropped by the duplicate filter: coverage of the
                    # mandatory tilt/roll/region cells wins
                    or (_tokens(item["metrics"], self.spec,
                                item.get("region")) - covered))]
            if not remaining:
                break
        # keep the recorded order so the path through the poses is natural
        order = {id(item): i for i, item in enumerate(candidates)}
        chosen.sort(key=lambda item: order.get(id(item), 0))
        return chosen

    def _coverage_refine(self, selected: list[dict],
                         candidates: list[dict]) -> list[dict]:
        """Add real views until the *measured* coverage gate is satisfied.

        Selection tokens are a cheap proxy; the gate itself centres the tilt
        excursion on its midrange and requires two views in each sign
        quadrant.  This post-pass scores candidates by the number of satisfied
        checks and then by the remaining deficit, so a view that moves a
        quadrant from 0/3 to 1/3 is progress even before the check flips.  It
        can only add recorded views, never change a measurement.
        """
        from dataclasses import replace

        from . import coverage as _cov

        refine_spec = replace(self.spec,
                              min_views_per_distance_band=4,
                              min_views_per_tilt_quadrant=3)
        observations = [item["observation"] for item in selected
                        if item["observation"]]

        def _deficit(value) -> int:
            total = 0
            if isinstance(value, dict):
                for item in value.values():
                    total += _deficit(item)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                total += max(0, int(round(float(value))))
            return total

        def evaluate(items) -> tuple[int, int, int]:
            rows = [item["observation"] for item in items
                    if item["observation"]]
            try:
                measurement = _cov.measure(rows, refine_spec,
                                           stationarity_observations=[])
            except Exception:
                return (-1, -10 ** 9, 0)
            checks = measurement.get("checks") or {}
            satisfied = sum(1 for check in checks.values()
                            if check.get("satisfied"))
            deficit = sum(_deficit(check.get("deficit"))
                          for check in checks.values())
            return (satisfied, -deficit, len(checks))

        best, _, total = evaluate(selected)
        for _ in range(16):
            if total and best >= total:
                break
            pool = [item for item in candidates
                    if all(item is not chosen for chosen in selected)
                    and item["observation"]]
            if not pool:
                break
            scored = []
            for item in pool[:100]:
                satisfied, minus_deficit, _ = evaluate(selected + [item])
                sep_deg, sep_mm = _min_separation(item, selected)
                scored.append(((satisfied, minus_deficit),
                               sep_deg, sep_mm, item))
            scored.sort(key=lambda row: (row[0], row[1], row[2]),
                        reverse=True)
            if not scored or scored[0][0] <= (best, -(10 ** 9)):
                break
            (satisfied, minus_deficit), _, _, item = scored[0]
            current = evaluate(selected)
            if (satisfied, minus_deficit) <= current[:2]:
                break
            best = satisfied
            selected.append(item)
        return selected

    # -- stream ----------------------------------------------------------
    def _build(self, selected: list[dict]) -> Iterator[types.FrameGroup]:
        n = len(selected)
        if not n:
            return
        holds = self.holds
        hold_at = set()
        if holds and n:
            picks = np.linspace(0, n - 1, min(holds, n)).round().astype(int)
            hold_at = {int(i) for i in picks}
        now = 0.0
        index = 0
        for i, item in enumerate(selected):
            now += self.move_dt_s
            yield types.FrameGroup(
                index=index, device_group=None, timestamp_s=now,
                images={k: np.ascontiguousarray(v)
                        for k, v in item["images"].items()},
                slot_ids={}, complete=True, source="views",
                label=f"{item['label']} (move)", colour_bgr=None,
                continuous=True)
            index += 1
            if i in hold_at and self.hold_repeats:
                for r in range(self.hold_repeats):
                    now += self.hold_dt_s
                    yield types.FrameGroup(
                        index=index, device_group=None, timestamp_s=now,
                        images={k: np.ascontiguousarray(v)
                                for k, v in item["images"].items()},
                        slot_ids={}, complete=True, source="views",
                        label=f"{item['label']} (hold {r + 1})",
                        colour_bgr=None, continuous=True)
                    index += 1

    def __iter__(self) -> Iterator[types.FrameGroup]:
        yield from self._groups

    def close(self) -> None:
        self._closed = True
