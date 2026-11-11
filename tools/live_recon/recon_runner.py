"""Driver for the existing C++ reconstruction CLI.

The sweep is *produced* by ``build/recon/recon_cli``; this module only invokes it,
reads its outputs and (for the controlled experiments) writes modified copies of
the raw frame directory.  No stereo code lives here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import cv2
import numpy as np

from . import common

OUTPUT_SUFFIXES = {
    "depth": "_depth_mm.tiff",
    "confidence": "_confidence.tiff",
    "disparity": "_disparity.tiff",
    "valid_left": "_valid_left.png",
    "valid_right": "_valid_right.png",
    "rectified_left": "_rectified_left.png",
    "rectified_right": "_rectified_right.png",
    "report": "_report.json",
    "cloud": "_cloud.ply",
    "preview": "_preview.png",
}


def frame_path(directory: str, group: int, cam: int) -> str:
    return os.path.join(directory, f"frame_{group:06d}_cam{cam}.pgm")


def load_frame(directory: str, group: int, cam: int) -> np.ndarray | None:
    p = frame_path(directory, group, cam)
    if not os.path.exists(p):
        return None
    img = cv2.imread(p, cv2.IMREAD_UNCHANGED)
    return img


def calibration_view(directory: str, group: int, cam: int) -> np.ndarray | None:
    """Frame in calibration orientation (cam0 rotated 180, cam1 as-is)."""
    img = load_frame(directory, group, cam)
    if img is None:
        return None
    if cam == 0:
        return cv2.rotate(img, cv2.ROTATE_180)
    return img


def image_stats(img: np.ndarray) -> dict:
    if img is None:
        return {"present": False}
    a = img.astype(np.float64)
    return {
        "present": True,
        "shape": [int(img.shape[1]), int(img.shape[0])],
        "mean_dn": float(a.mean()),
        "p01_dn": float(np.percentile(a, 1)),
        "p50_dn": float(np.percentile(a, 50)),
        "p99_dn": float(np.percentile(a, 99)),
        "max_dn": float(a.max()),
        "saturated_fraction": float((img >= 255).mean()),
        "dark_fraction": float((img <= 2).mean()),
    }


class ReconCli:
    """Thin wrapper around ``recon_cli`` with provenance recording."""

    def __init__(self, path: str, timeout_s: float = 300.0):
        self.path = os.path.abspath(path)
        self.timeout_s = timeout_s
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"recon_cli not found: {self.path}")
        self.fingerprint = common.file_fingerprint(self.path)

    def run(self, frames: str, group: int, profile: str, outdir: str,
            scale: float = 0.5, prefix: str = "recon",
            extra_args: list | None = None) -> dict:
        os.makedirs(outdir, exist_ok=True)
        cmd = [self.path, "reconstruct", "--frames", frames, "--id", str(group),
               "--profile", profile, "--out", outdir, "--scale", repr(float(scale)),
               "--prefix", prefix]
        if extra_args:
            cmd.extend(extra_args)
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout_s)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            rc, stdout, stderr = -9, e.stdout or "", f"timeout after {self.timeout_s}s"
        wall = time.time() - t0
        report = None
        rpath = os.path.join(outdir, prefix + OUTPUT_SUFFIXES["report"])
        if os.path.exists(rpath):
            try:
                report = common.read_json(rpath)
            except Exception:
                report = None
        return {
            "command": cmd,
            "returncode": int(rc),
            "wall_seconds": float(wall),
            "stdout": stdout,
            "stderr": stderr,
            "report": report,
            "outdir": outdir,
            "prefix": prefix,
            "ok": bool(rc == 0 and report is not None and report.get("ok")),
        }

    # -- output readers ---------------------------------------------------
    @staticmethod
    def read_maps(outdir: str, prefix: str = "recon") -> dict:
        out = {}
        for key in ("depth", "confidence", "disparity"):
            p = os.path.join(outdir, prefix + OUTPUT_SUFFIXES[key])
            out[key] = cv2.imread(p, cv2.IMREAD_UNCHANGED) if os.path.exists(p) else None
        return out


# ---------------------------------------------------------------------------
# controlled frame-directory rewriting (exposure / saturation / preprocessing)
# ---------------------------------------------------------------------------

def write_modified_frames(src: str, dst: str, groups, transform=None,
                          cams=(0, 1), overwrite: bool = True) -> dict:
    """Copy ``groups`` of ``src`` into ``dst`` with a per-image transform.

    ``transform(cam, image, group) -> image``.  Unmodified cameras are symlinked
    to the original file, so the on-disk cost is only the two IR frames.  The
    value of this helper is that every experiment runs through the *real* CLI:
    an intensity change is applied to the PGM bytes, not to the matcher.
    """
    if overwrite and os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst, exist_ok=True)
    written = {"groups": 0, "images": 0, "links": 0}
    for g in groups:
        ok = True
        for cam in range(3):
            srcp = frame_path(src, g, cam)
            if not os.path.exists(srcp):
                ok = False
                break
            dstp = frame_path(dst, g, cam)
            if cam in cams and transform is not None:
                img = cv2.imread(srcp, cv2.IMREAD_UNCHANGED)
                if img is None:
                    ok = False
                    break
                new = transform(cam, img, g)
                cv2.imwrite(dstp, np.ascontiguousarray(new))
                written["images"] += 1
            else:
                if os.path.lexists(dstp):
                    os.remove(dstp)
                os.symlink(os.path.abspath(srcp), dstp)
                written["links"] += 1
        if ok:
            written["groups"] += 1
    return written


# ---------------------------------------------------------------------------
# CLI self-test: depth-map -> point-cloud inversion
# ---------------------------------------------------------------------------

def selftest_inversion(cli: ReconCli, model, profile_path: str, frames: str,
                       group: int, tmpdir: str, scale: float = 0.5) -> dict:
    """Prove that ``model.points_from_depth`` reproduces the CLI's own PLY."""
    os.makedirs(tmpdir, exist_ok=True)
    run = cli.run(frames, group, profile_path, tmpdir, scale=scale)
    if not run["ok"]:
        return {"ok": False, "reason": run.get("stderr") or "cli failed"}
    maps = cli.read_maps(tmpdir)
    v, u, X = model.points_from_depth(maps["depth"], scale)
    ply = os.path.join(tmpdir, "recon" + OUTPUT_SUFFIXES["cloud"])
    P, meta = read_ply(ply)
    if P is None or len(P) != len(X):
        return {"ok": False, "reason": "point count mismatch",
                "ply_points": None if P is None else int(len(P)),
                "depth_points": int(len(X))}
    d = np.abs(P[:, :3].astype(np.float64) - X)
    result = {
        "ok": True,
        "group": int(group),
        "n_points": int(len(X)),
        "max_abs_coord_diff_mm": float(d.max()),
        "p99_abs_coord_diff_mm": float(np.percentile(d, 99)),
        "ply_comments": meta,
        "cli": cli.fingerprint,
    }
    return result


def read_ply(path: str):
    """Read the binary PLY written by ``recon/src/pointcloud.cpp``."""
    if not os.path.exists(path):
        return None, {}
    with open(path, "rb") as f:
        data = f.read()
    marker = b"end_header\n"
    if marker not in data:
        return None, {}
    hend = data.index(marker) + len(marker)
    header = data[:hend].decode("ascii", "replace")
    n = None
    props = []
    comments = {}
    for line in header.splitlines():
        if line.startswith("element vertex"):
            n = int(line.split()[-1])
        elif line.startswith("property float"):
            props.append(line.split()[-1])
        elif line.startswith("comment "):
            body = line[len("comment "):]
            if "=" in body:
                k, v = body.split("=", 1)
                comments[k] = v
    if n is None or not props:
        return None, comments
    lines0 = [l.strip() for l in header.splitlines()[:2]]
    if "format binary_little_endian 1.0" not in lines0:
        return None, comments
    width = len(props) * 4
    arr = np.frombuffer(data[hend:hend + n * width], dtype="<f4").reshape(n, len(props))
    return arr, comments
