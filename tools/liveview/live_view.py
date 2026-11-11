#!/usr/bin/env python3
"""Single-window OpenCV diagnostic viewer for Einstar capture frames.

Read-only: three camera previews with plate markers, plate pose and quality, live
capture counters and a periodically refreshed recon_cli disparity preview.
Ctrl-C or q/Esc quits; ``--headless`` writes a PNG snapshot.

    python tools/liveview/live_view.py --frames DIR [--status-file PATH]
    python tools/liveview/live_view.py --frames DIR --headless --duration 8 \
        --snapshot-out /tmp/dashboard.png
"""
from __future__ import annotations

import argparse
from collections import deque
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np

cv2.setNumThreads(1)          # one viewer core; workers already run in threads

HERE = Path(__file__).resolve().parent
for _path in (str(HERE), str(HERE.parents[1])):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import lv_analysis as analysis          # noqa: E402
import lv_depth                         # noqa: E402
import lv_frames                        # noqa: E402
import lv_status                        # noqa: E402

W, H, HEAD = 1600, 900, 56
TILE_W, TILE_H, LABEL_H = 480, 384, 22
TILE_AT = ((8, 64), (500, 64), (8, 478), (500, 478))     # left, right, colour, depth
PANEL, COL_A, COL_B = (1000, 64, 592, 820), (1012, 74, 276), (1304, 74, 276)
FONT = cv2.FONT_HERSHEY_SIMPLEX
BG, PANEL_BG, BAR_BG, EDGE = (24, 24, 28), (38, 38, 44), (48, 48, 56), (70, 70, 80)
FG, MUT, DIM = (232, 232, 236), (150, 150, 160), (100, 100, 110)
GREEN, AMBER, RED, CYAN = (90, 215, 90), (60, 190, 250), (70, 70, 240), (220, 200, 90)


def truncate(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:max(1, limit - 2)] + ".."


class Canvas:
    def __init__(self):
        self.img = np.full((H, W, 3), BG, np.uint8)

    def text(self, value, x, y, scale=0.45, color=FG, thick=1, anchor="lt") -> None:
        value = str(value)
        (tw, th), _ = cv2.getTextSize(value, FONT, scale, thick)
        ox = x - (tw if anchor[0] == "r" else tw // 2 if anchor[0] == "c" else 0)
        oy = y + (th if anchor[1] == "t" else th // 2 if anchor[1] == "m" else 0)
        cv2.putText(self.img, value, (int(ox), int(oy)), FONT, scale, color, thick,
                    cv2.LINE_AA)

    def panel(self, x, y, w, h) -> None:
        cv2.rectangle(self.img, (x, y), (x + w - 1, y + h - 1), PANEL_BG, -1)
        cv2.rectangle(self.img, (x, y), (x + w - 1, y + h - 1), EDGE, 1)

    def kv(self, x, y, w, key, value, color=FG) -> None:
        self.text(key, x, y, 0.40, MUT)
        self.text(value, x + w, y + 1, 0.44, color, 1, "rt")

    def bar(self, x, y, w, fraction, color, mark=None) -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        cv2.rectangle(self.img, (x, y), (x + w, y + 12), BAR_BG, -1)
        cv2.rectangle(self.img, (x, y), (x + int(w * fraction), y + 12), color, -1)
        if mark is not None and 0 < mark < 1:
            mx = x + int(w * mark)
            cv2.line(self.img, (mx, y - 2), (mx, y + 14), FG, 1)

    def pill(self, x, y, w, h, label, fill) -> None:
        cv2.rectangle(self.img, (x, y), (x + w, y + h), fill, -1)
        cv2.rectangle(self.img, (x, y), (x + w, y + h), EDGE, 1)
        self.text(label, x + w // 2, y + h // 2, 0.48, BG, 1, "cm")


def draw_items(c: Canvas, x, y, w, items) -> None:
    """Render a compact panel description: h, kv, big, bar, gap, note."""
    for item in items:
        kind = item[0]
        if kind == "h":
            c.text(str(item[1]).upper(), x, y, 0.40, CYAN)
            y += 21
        elif kind == "kv":
            c.kv(x, y, w, item[1], item[2], item[3] if len(item) > 3 else FG)
            y += 17
        elif kind == "big":
            c.text(item[1], x, y, item[3] if len(item) > 3 else 0.62,
                   item[2] if len(item) > 2 else FG, 2 if len(item) > 3 else 1)
            y += 26
        elif kind == "bar":
            c.bar(x, y, w, item[1], item[2], item[3] if len(item) > 3 else None)
            y += 20
        elif kind == "note":
            c.text(truncate(item[1], 48), x, y, 0.36, item[2] if len(item) > 2 else DIM)
            y += 16
        elif kind == "gap":
            y += item[1]


def draw_tile(c: Canvas, x, y, image, points, title, sub, border) -> None:
    height = TILE_H + LABEL_H
    if image is None:
        cv2.rectangle(c.img, (x, y), (x + TILE_W - 1, y + TILE_H - 1), (18, 18, 22), -1)
        c.text("NO FRAME", x + TILE_W // 2, y + TILE_H // 2, 0.9, RED, 2, "cm")
    else:
        c.img[y:y + TILE_H, x:x + TILE_W] = image
        for point in (lv_frames.map_points(points, (TILE_W, TILE_H))
                      if points is not None else ()):
            px, py = int(round(point[0])) + x, int(round(point[1])) + y
            if 0 <= px < x + TILE_W and y <= py < y + TILE_H:
                cv2.circle(c.img, (px, py), 3, GREEN, 1, cv2.LINE_AA)
    cv2.rectangle(c.img, (x, y), (x + TILE_W - 1, y + height - 1), border, 2)
    cv2.rectangle(c.img, (x, y + TILE_H), (x + TILE_W - 1, y + height - 1), PANEL_BG, -1)
    c.text(title, x + 8, y + TILE_H + 4, 0.42, FG)
    c.text(sub, x + TILE_W - 8, y + TILE_H + 5, 0.38, MUT, 1, "rt")


class Viewer:
    def __init__(self, args):
        self.args, self.frames = args, lv_frames.FrameDir(args.frames)
        self.scan = self.frames.refresh(force=True)
        self.status = lv_status.StatusFile(args.status_file)
        self.status.read()
        self._manifest_cache, self._disk, self._selected = {}, None, None
        self.manifest = lv_status.read_manifest(self.frames.directory, self._manifest_cache)
        self.profile, self.profile_error = None, None
        try:
            self.profile = analysis.load_profile(args.profile)
        except (OSError, ValueError, KeyError) as exc:
            self.profile_error = f"{type(exc).__name__}: {exc}"
        self.left = analysis.AnalysisWorker(self.frames, 1, self.profile)
        self.right = analysis.AnalysisWorker(self.frames, 0, self.profile)
        self.depth, self.depth_error = None, None
        recon = lv_depth.find_recon_cli(args.recon_cli)
        if args.no_depth:
            self.depth_error = "disabled by --no-depth"
        elif self.profile is None:
            self.depth_error = f"no profile ({self.profile_error})"
        elif recon is None:
            self.depth_error = "recon_cli not found (--recon-cli)"
        else:
            self.depth = lv_depth.DepthWorker(
                self.frames, recon, args.profile, args.depth_scale, args.depth_interval,
                max(5.0, args.depth_interval * 5))
        self.record_window = max(0.5, args.record_window)
        self._scan_stamp = self._meta_at = self._disk_at = 0.0
        self._req, self._right_at = {0: None, 1: None}, 0.0
        self._drop_samples, self._gid_samples = deque(), deque()
        self._last_gid = None
        self.t0 = time.monotonic()

    def update(self, now: float) -> None:
        self.scan = self.frames.refresh()
        if self.scan.scanned_at != self._scan_stamp:
            self._scan_stamp, self._selected = self.scan.scanned_at, self.scan.newest_left
            if self._selected is not None and self._selected != self._last_gid:
                self._gid_samples.append((now, self._selected))
                self._last_gid = self._selected
                while len(self._gid_samples) > 2 and now - self._gid_samples[0][0] > 3.0:
                    self._gid_samples.popleft()
            self.manifest = lv_status.read_manifest(self.frames.directory, self._manifest_cache)
        if now - self._meta_at >= 0.5:
            self._meta_at = now
            self.status.read()
            dropped = self.status.get("frames_dropped")
            if isinstance(dropped, (int, float)):
                self._drop_samples.append((now, float(dropped)))
                while len(self._drop_samples) > 2 and \
                        now - self._drop_samples[0][0] > self.record_window:
                    self._drop_samples.popleft()
        if now - self._disk_at >= 2.0:
            self._disk_at = now
            directory = self.status.get("output_directory")
            if not isinstance(directory, str) or not os.path.isdir(directory):
                directory = self.frames.directory
            self._disk = lv_status.disk_usage(directory)
        self._request_analysis(now)

    def _request_analysis(self, now: float) -> None:
        selected = self._selected
        if selected is None:
            return
        for cam, worker in ((1, self.left), (0, self.right)):
            result = worker.latest()
            if result is not None and result.gid == selected and result.error \
                    and time.time() - result.ts > 1.5:
                self._req[cam] = None                    # partially written: retry
        if self._req[1] != selected:
            self.left.request(selected)
            self._req[1] = selected
        if self._req[0] != selected and now - self._right_at >= 1.0:
            self.right.request(selected)
            self._req[0], self._right_at = selected, now
        if self.depth is not None and all(self.frames.has(selected, cam) for cam in (0, 1, 2)):
            result = self.depth.latest()
            if result.gid != selected or time.time() - result.ts > self.depth.interval:
                self.depth.request(selected)

    def live_fps(self) -> float | None:
        if len(self._gid_samples) < 2:
            return None
        (t0, g0), (t1, g1) = self._gid_samples[0], self._gid_samples[-1]
        return (g1 - g0) / (t1 - t0) if t1 - t0 >= 0.8 and g1 > g0 else None

    def drop_rate(self) -> float:
        if len(self._drop_samples) < 2:
            return 0.0
        (t0, d0), (t1, d1) = self._drop_samples[0], self._drop_samples[-1]
        return max(0.0, (d1 - d0) / (t1 - t0)) if t1 > t0 else 0.0

    def stats(self) -> dict:
        status, manifest = self.status.data or {}, self.manifest or {}
        fresh, reassembly = self.status.fresh, manifest.get("reassembly", {})
        device = manifest.get("device", {}).get("volatile_settings", {})

        def pick(key):
            if fresh and status.get(key) is not None:
                return status[key]
            return manifest.get(key, status.get(key))

        def setting(key):
            return status[key] if status.get(key) is not None else device.get(key)

        return {
            "written": pick("groups_written"),
            "requested": pick("groups_requested") or manifest.get("requested_groups"),
            "frames": status.get("frames_completed") if fresh else
            reassembly.get("frames_completed", status.get("frames_completed")),
            "dropped": status.get("frames_dropped") if fresh else
            reassembly.get("frames_dropped", status.get("frames_dropped")),
            "timeouts": pick("transfer_timeouts") or reassembly.get("partial_timeouts"),
            "invalid": pick("frames_invalid"), "throughput": pick("throughput_mbps"),
            "elapsed": status.get("elapsed_s") if fresh else
            manifest.get("duration_s", status.get("elapsed_s")),
            "trigger": setting("trigger_period_us"), "exposure": setting("exposure_us"),
            "gain": setting("gain"),
            "laser": status.get("laser_percent") if status.get("laser_percent") is not None
            else device.get("illuminator", {}).get("laser_percent"),
            "schema": status.get("schema"),
            "simulated": status.get("simulated", manifest.get("simulated")),
        }

    def health(self, now: float) -> tuple[str, list[str]]:
        selected = self._selected
        if selected is None:
            return "BAD", ["no frames in directory"]
        reasons, cams = [], self.scan.cams(selected)
        if len(cams) < 3:
            reasons.append(f"sync {len(cams)}/3")
        result = self.left.latest()
        if result is None or result.gid != selected:
            return "WAIT", [f"analysing g{selected}"]
        if result.error:
            reasons.append("left IR unreadable")
        if result.quality is not None:
            if result.quality.sat_pct > 5.0:
                reasons.append(f"saturation {result.quality.sat_pct:.1f}%")
            if result.quality.blur < 100.0:
                reasons.append(f"blur {result.quality.blur:.0f}")
        if result.markers is not None and result.markers.n < analysis.MIN_MARKERS:
            reasons.append(f"markers {result.markers.n}/40")
        if self.drop_rate() > 0.0:
            reasons.append(f"drops {self.drop_rate():.1f}/s")
        return ("BAD" if reasons else "OK"), reasons

    # -- panel descriptions -------------------------------------------------
    def _header(self, c: Canvas, selected, health, reasons) -> None:
        c.text("EINSTAR LIVE VIEW", 16, HEAD // 2, 0.78, FG, 2, "lm")
        c.text(truncate(self.frames.directory, 70), 300, HEAD // 2, 0.40, MUT, 1, "lm")
        left = self.left.latest()
        markers = left.markers.n if left and left.markers else "--"
        if health == "OK":
            label, fill = f"OK · {len(self.scan.cams(selected))}/3 sync · {markers} markers", GREEN
        elif health == "WAIT":
            label, fill = f"WAIT · {reasons[0]}", AMBER
        else:
            label, fill = "BAD · " + " · ".join(reasons[:3]), RED
        c.pill(W - 560, 13, 396, 30, truncate(label, 46), fill)
        state, age = self.status.state(), self.status.age()
        color = GREEN if state == "LIVE" else AMBER if state == "STALE" else DIM
        label = state if state in ("NO STATUS", "NO FILE") or age is None else \
            f"{state} {lv_status.fmt_age(age)}"
        c.pill(W - 152, 13, 144, 30, truncate(label, 16), color)

    def _tile_specs(self, now: float, selected):
        specs = [("LEFT IR  cam1", 1, self.left.latest()),
                 ("RIGHT IR  cam0", 0, self.right.latest()),
                 ("COLOUR  cam2", 2, None), ("DEPTH DISPARITY · recon_cli", None, None)]
        result = []
        for i, (title, cam, analysed) in enumerate(specs):
            image = points = None
            border, sub = RED, "missing"
            if cam is None:
                preview = self.depth.latest() if self.depth else None
                if self.depth is None:
                    sub, border = truncate(self.depth_error or "disabled", 40), DIM
                elif preview.gid is None:
                    sub, border = "waiting for first run", AMBER
                else:
                    age = preview.age()
                    stale = preview.gid != selected or age is None or \
                        age > 2 * self.depth.interval + 2.0
                    border = RED if not preview.ok else AMBER if stale else GREEN
                    if preview.ok:
                        image = lv_frames.fit(preview.image, (TILE_W, TILE_H))
                        sub = f"g{preview.gid} · " + ("STALE " + lv_status.fmt_age(age)
                                                      if stale else lv_status.fmt_age(age))
                    else:
                        sub = truncate(preview.error, 40)
            elif selected is not None and self.frames.has(selected, cam):
                image = self.frames.preview(selected, cam, (TILE_W, TILE_H))
                if image is not None:
                    border, sub = GREEN, "loaded"
                if cam == 2:
                    sub = "BayerRG · display gain · no overlay"
                elif analysed is not None and analysed.gid == selected:
                    if analysed.error:
                        sub, border = truncate(analysed.error, 36), RED
                    elif analysed.markers is not None:
                        markers, points = analysed.markers, analysed.markers.points
                        sub = f"{markers.n}/40 mk · {markers.ms:.0f} ms"
                        if analysed.pose is not None:
                            sub += f" · {analysed.pose.rms_px:.2f} px"
                        if markers.n < analysis.MIN_MARKERS or (
                                cam == 1 and analysed.quality is not None and
                                (analysed.quality.sat_pct > 5.0 or analysed.quality.blur < 100)):
                            border = RED
                    else:
                        sub, border = "no plate detected", RED
                else:
                    sub, border = "measuring ...", AMBER
            result.append((TILE_AT[i], image, points, title, sub, border))
        return result

    def _status_items(self, stats) -> list:
        state = self.status.state()
        color = GREEN if state == "LIVE" else AMBER if state == "STALE" else DIM
        h = lambda t: ("h", t)                                       # noqa: E731
        kv = lambda k, v, c=FG: ("kv", k, v, c)                      # noqa: E731
        items = [h("Status file"), ("big", state, color, 0.62),
                 kv("age", lv_status.fmt_age(self.status.age()))]
        if self.status.path is None:
            items.append(("note", "no --status-file given"))
        elif state == "NO FILE":
            items.append(("note", "status file not found (yet)", RED))
        elif self.status.error:
            items.append(("note", self.status.error, RED))
        elif state == "STALE":
            items.append(("note", "stopped updating (>2 s)", AMBER))
        items += [kv("schema", truncate(stats["schema"] or "--", 28),
                     DIM if stats["schema"] is None else
                     GREEN if stats["schema"] == lv_status.SCHEMA else AMBER),
                  kv("elapsed · groups", f"{stats['elapsed']:.1f} s · "
                     f"{self._show(stats['written'])}/{self._show(stats['requested'])}"
                     if isinstance(stats["elapsed"], (int, float)) else "--"),
                  kv("throughput · drops", f"{stats['throughput']:.1f} MB/s · "
                     f"{self._show(stats['dropped'])}"
                     if isinstance(stats["throughput"], (int, float)) else "--"),
                  kv("timeouts · invalid",
                     f"{self._show(stats['timeouts'])} · {self._show(stats['invalid'])}",
                     RED if stats["invalid"] else
                     AMBER if stats["timeouts"] else FG),
                  kv("trigger us · laser %",
                     f"{self._show(stats['trigger'])} · {self._show(stats['laser'])}"),
                  kv("exposure us", self._join(stats["exposure"])),
                  kv("gain", self._join(stats["gain"])), ("gap", 14)]
        return items

    def _capture_items(self, stats) -> list:
        written, requested = stats["written"], stats["requested"]
        fraction = float(written) / requested if written is not None and requested else 0.0
        fps = self.live_fps() or (self.manifest or {}).get("cadence", {}).get("fps")
        gids = list(self.scan.gids)
        duplicate = (self.manifest or {}).get("duplicate_frames")
        h = lambda t: ("h", t)                                       # noqa: E731
        kv = lambda k, v, c=FG: ("kv", k, v, c)                      # noqa: E731
        return [h("Capture"), ("bar", fraction, GREEN if fraction >= 1 else CYAN),
                kv("groups", f"{written if written is not None else '--'} / "
                   f"{requested if requested is not None else '--'}"),
                kv("frames · dropped",
                   f"{self._show(stats['frames'])} · {self._show(stats['dropped'])}",
                   RED if self.drop_rate() > 0 else FG),
                kv("group rate · drop rate",
                   f"{fps:.2f} fps · {self.drop_rate():.1f} /s" if fps else
                   f"-- · {self.drop_rate():.1f} /s", GREEN if fps and fps >= 8 else AMBER),
                kv("disk free", lv_status.fmt_bytes(self._disk[0]) if self._disk else "--",
                   RED if self._disk and self._disk[0] < (1 << 30) else
                   AMBER if self._disk and self._disk[0] < (5 << 30) else FG),
                ("gap", 12), h("Frame scan (2 Hz)"),
                kv("gid range", f"{min(gids)}..{max(gids)}" if gids else "--"),
                kv("accepted · rejected", f"{self.scan.complete} · {self.scan.incomplete}",
                   RED if self.scan.incomplete else FG),
                kv("duplicate · late", f"{duplicate if duplicate is not None else '--'} · "
                   f"{(self.manifest or {}).get('late_frames') if self.manifest else '--'}"),
                kv("frames · scan age", f"{self.scan.frames} · "
                   f"{lv_status.fmt_age(self.scan.age(time.monotonic()))}"),
                kv("selected", f"g{self._selected}" if self._selected is not None else "--"),
                ("gap", 14)]

    def _manifest_items(self) -> list:
        h = lambda t: ("h", t)                                       # noqa: E731
        kv = lambda k, v, c=FG: ("kv", k, v, c)                      # noqa: E731
        manifest = self.manifest
        if manifest is None:
            return [h("Manifest"), kv("state", "not written yet", MUT), kv("groups", "--")]
        complete = bool(manifest.get("complete"))
        cadence = manifest.get("cadence", {})
        recording = manifest.get("recording", {}).get("path")
        return [h("Manifest"),
                kv("state", "complete" if complete else
                   f"partial ({manifest.get('groups_incomplete', 0)} incomplete)",
                   GREEN if complete else AMBER),
                kv("groups", f"{manifest.get('groups_written', '--')} / "
                   f"{manifest.get('requested_groups', '--')}"),
                kv("cadence", f"{cadence.get('fps', 0):.2f} fps · "
                   f"{cadence.get('mean_ms', 0):.1f} ms"),
                kv("duration", f"{manifest.get('duration_s', 0):.1f} s · "
                   f"{lv_status.fmt_bytes(manifest.get('bytes_written'))}"),
                kv("recording", truncate(os.path.basename(recording), 24)
                   if recording else "none"),
                ("gap", 12), h("Viewer"),
                kv("refresh", f"{self.args.max_fps:g} fps · scan 2 Hz"),
                kv("depth refresh", f"{self.args.depth_interval:g} s · "
                   f"scale {self.args.depth_scale:g}"),
                kv("quit", "q / Esc / Ctrl-C")]

    def _plate_items(self, selected) -> list:
        h = lambda t: ("h", t)                                       # noqa: E731
        kv = lambda k, v, c=FG: ("kv", k, v, c)                      # noqa: E731
        cams = self.scan.cams(selected) if selected is not None else frozenset()
        items = [h("Cameras & sync")]
        for cam in range(3):
            last = self.scan.seen[cam]
            items.append(kv(f"CAM{cam} {lv_frames.CAM_NAMES[cam]}",
                            f"{'OK' if cam in cams else 'MISSING'} · "
                            f"{'g' + str(last) if last is not None else 'never'}",
                            GREEN if cam in cams else RED))
        items += [kv("sync", f"{len(cams)}/3 " + ("OK" if len(cams) == 3 else "INCOMPLETE"),
                     GREEN if len(cams) == 3 else RED), ("gap", 8), h("Plate pose · left IR")]
        result = self.left.latest()
        result = result if result is not None and result.gid == selected else None
        if result is None or result.markers is None:
            return items + [("big", "NO PLATE" if selected is not None else "NO FRAMES",
                             AMBER if selected is not None else RED, 0.58)]
        markers, pose = result.markers, result.pose
        items.append(("big", f"{markers.n} / 40 markers",
                      GREEN if markers.n >= analysis.MIN_MARKERS else RED, 0.62))
        if self.profile is None:
            items.append(("note", "no profile: pose unavailable", AMBER))
        elif pose is None:
            items.append(("note", "PnP failed", RED))
        else:
            items += [kv("x · y · z", f"{pose.x:+.1f} · {pose.y:+.1f} · {pose.z:.1f} mm",
                         GREEN if 120 <= pose.z <= 900 else AMBER),
                      kv("tilt · in-plane", f"{pose.tilt_deg:.1f} · "
                         f"{pose.inplane_deg:+.1f} deg"),
                      kv("reprojection RMS", f"{pose.rms_px:.2f} px",
                         GREEN if pose.rms_px < 1.5 else AMBER if pose.rms_px < 3 else RED),
                      kv("detection RMS · points", f"{markers.rms:.2f} px · {pose.n}")]
        live = self.status.fresh or bool(self._gid_samples and
                                         time.monotonic() - self._gid_samples[-1][0] < 2.0)
        items += [kv("analysis", lv_status.fmt_age(time.time() - result.ts) +
                     (" ago" if live else " ago · static"), MUT),
                  ("gap", 8), h("Quality · left IR")]
        quality = result.quality
        if quality is None:
            return items + [("note", "measuring ...", AMBER)]
        items += [kv("mean", f"{quality.mean:.1f}"),
                  kv("saturation", f"{quality.sat_pct:.2f} %",
                     RED if quality.sat_pct > 5.0 else GREEN),
                  ("bar", quality.sat_pct / 10.0,
                   RED if quality.sat_pct > 5.0 else GREEN, 0.5),
                  kv("blur (Laplacian var)", f"{quality.blur:.0f}",
                     RED if quality.blur < 100 else GREEN)]
        return items

    def _depth_items(self, selected) -> list:
        h = lambda t: ("h", t)                                       # noqa: E731
        kv = lambda k, v, c=FG: ("kv", k, v, c)                      # noqa: E731
        items = [h("Depth preview")]
        if self.depth is None:
            return items + [("note", self.depth_error or "disabled")]
        p = self.depth.latest()
        if p.gid is None:
            return items + [("note", "waiting for first run", AMBER)]
        if not p.ok:
            return items + [("note", p.error, RED)]
        age = p.age()
        stale = p.gid != selected or age is None or age > 2 * self.depth.interval + 2.0
        return items + [
            kv("state", f"{'STALE' if stale else 'OK'} · g{p.gid} · {lv_status.fmt_age(age)}",
               AMBER if stale else GREEN),
            kv("valid · points", f"{p.valid:.1f} % · {p.points}"),
            kv("z p50 · p95", f"{p.p50_mm:.0f} · {p.p95_mm:.0f} mm"),
            kv("recon · scale", f"{p.run_ms:.0f} ms · {p.scale:g}")]

    def _device_items(self, stats, health, reasons) -> list:
        h = lambda t: ("h", t)                                       # noqa: E731
        kv = lambda k, v, c=FG: ("kv", k, v, c)                      # noqa: E731
        backend = (self.manifest or {}).get("backend")
        items = [h("Device / health"),
                 kv("mode", "SIMULATED" if stats.get("simulated") else "LIVE DEVICE",
                    AMBER if stats.get("simulated") else FG),
                 kv("backend", backend or ("status" if self.status.fresh else "--")),
                 kv("health", health, GREEN if health == "OK" else
                    AMBER if health == "WAIT" else RED)]
        if reasons:
            items.append(("note", " · ".join(reasons), RED))
        return items

    def render(self, now: float) -> np.ndarray:
        c, selected, stats = Canvas(), self._selected, self.stats()
        health, reasons = self.health(now)
        self._header(c, selected, health, reasons)
        for (x, y), image, points, title, sub, border in self._tile_specs(now, selected):
            draw_tile(c, x, y, image, points, title, sub, border)
        c.panel(*PANEL)
        draw_items(c, *COL_A, self._status_items(stats) + self._capture_items(stats) +
                   self._manifest_items())
        items_b = self._plate_items(selected) + [("gap", 10)] + \
            self._depth_items(selected) + [("gap", 10)] + \
            self._device_items(stats, health, reasons) + \
            [("gap", 12), ("note", "red = bad frame · amber = stale/partial", MUT)]
        draw_items(c, *COL_B, items_b)
        return c.img

    @staticmethod
    def _show(value) -> str:
        return "--" if value is None else str(value)

    @staticmethod
    def _join(value) -> str:
        if value is None:
            return "--"
        value = list(value.values()) if isinstance(value, dict) else value
        return "/".join(str(v) for v in value) if isinstance(value, (list, tuple)) \
            else str(value)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="OpenCV diagnostic viewer for Einstar capture frames",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--frames", required=True, help="frame directory")
    parser.add_argument("--status-file", help="live status JSON from the capture tool")
    parser.add_argument("--profile", default=os.path.expanduser(
        "~/einstar-private/einstar-profiles/factory-e10.json"),
        help="camera profile JSON (intrinsics, plate pose)")
    parser.add_argument("--max-fps", type=float, default=10.0, help="UI refresh rate")
    parser.add_argument("--no-depth", action="store_true", help="disable disparity preview")
    parser.add_argument("--record-window", type=float, default=1.0,
                        help="rolling window (s) for drop-rate health")
    parser.add_argument("--headless", action="store_true", help="never open a window")
    parser.add_argument("--duration", type=float, help="stop after N seconds")
    parser.add_argument("--snapshot-out", help="write the composed dashboard PNG here")
    parser.add_argument("--recon-cli", help="path to build/recon/recon_cli")
    parser.add_argument("--depth-interval", type=float, default=3.0,
                        help="seconds between disparity refreshes")
    parser.add_argument("--depth-scale", type=float, default=0.5,
                        help="stereo scale passed to recon_cli")
    return parser.parse_args(argv)


def gui_available() -> bool:
    try:
        return next(("NONE" not in line for line in cv2.getBuildInformation().splitlines()
                     if line.strip().startswith("GUI:")), True)
    except cv2.error:
        return True


def main(argv=None) -> int:
    args = parse_args(argv)
    viewer = Viewer(args)
    if viewer.profile_error:
        print(f"liveview: profile unavailable ({viewer.profile_error}); pose disabled",
              file=sys.stderr)
    if viewer.depth is None:
        print(f"liveview: depth preview off ({viewer.depth_error})", file=sys.stderr)
    display = not args.headless and gui_available() and bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not args.headless and not display:
        reason = "OpenCV built without GUI support" if not gui_available() \
            else "no DISPLAY/WAYLAND_DISPLAY"
        print(f"liveview: {reason}; running headless", file=sys.stderr)
    if display:
        try:
            cv2.namedWindow("Einstar Live View", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("Einstar Live View", W, H)
        except cv2.error as exc:
            print(f"liveview: cannot open window ({exc}); running headless", file=sys.stderr)
            display = False
    duration = args.duration
    if duration is None and not display:
        duration = 5.0
        print("liveview: headless without --duration; defaulting to 5 s", file=sys.stderr)
    period, next_tick, canvas = 1.0 / max(0.5, args.max_fps), time.monotonic(), None
    try:
        while True:
            now = time.monotonic()
            if duration is not None and canvas is not None and now - viewer.t0 >= duration:
                break
            viewer.update(now)
            canvas = viewer.render(now)
            if display:
                cv2.imshow("Einstar Live View", canvas)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
            next_tick += period
            remaining = next_tick - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            else:
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        if display:
            cv2.destroyAllWindows()
        for worker in (viewer.left, viewer.right, viewer.depth):
            if worker is not None:
                worker.close()
    canvas = canvas if canvas is not None else viewer.render(time.monotonic())
    runtime = time.monotonic() - viewer.t0
    snapshot = None
    if args.snapshot_out:
        snapshot = os.path.abspath(args.snapshot_out)
        os.makedirs(os.path.dirname(snapshot) or ".", exist_ok=True)
        if not cv2.imwrite(snapshot, canvas):
            print(f"liveview: cannot write snapshot {snapshot}", file=sys.stderr)
            return 1
    stats, left = viewer.stats(), viewer.left.latest()
    pose = left.pose if left and left.pose else None
    health, _ = viewer.health(time.monotonic())
    depth = viewer.depth.latest() if viewer.depth else None
    cadence = (viewer.manifest or {}).get("cadence", {}).get("fps")
    selected = f"g{viewer._selected}" if viewer._selected is not None else "--"
    print("liveview: "
          f"groups={viewer._show(stats['written'])}/{viewer._show(stats['requested'])} "
          f"complete_groups={viewer.scan.complete} incomplete={viewer.scan.incomplete} "
          f"frames={viewer._show(stats['frames'])} cadence_fps={cadence if cadence else 0:.2f} "
          f"dropped={viewer._show(stats['dropped'])} selected={selected} "
          f"markers={left.markers.n if left and left.markers else 0}/40 "
          f"pose={f'{pose.x:.1f},{pose.y:.1f},{pose.z:.1f}mm' if pose else 'na'} "
          f"status={viewer.status.state()} status_age_s={viewer.status.age()} "
          f"depth={'ok' if depth and depth.ok else 'na'} health={health} "
          f"elapsed_s={runtime:.2f} snapshot={snapshot or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
