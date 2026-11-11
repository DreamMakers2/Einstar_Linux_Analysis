"""Session directory: creation, atomic manifest, materialisation, resume.

Implements the layout of ``docs/calibration/wizard_architecture.md`` §9::

    SESSION/
      session.json              # atomic: state, config, hashes, counters
      manifest.json             # accepted views: index, device_group, pose, quality
      decisions.jsonl           # one line per ingest (accepted or rejected + reasons)
      groups/000123/cam0.pgm    # materialised accepted triplets (copied from the ring)
      groups/000123/cam1.pgm
      groups/000123/cam2.pgm
      raw.e1r                   # lossless USB recording (live) or source path (replay)
      status.json               # live capture status (live only)
      candidate/profile.json    # validation: pending
      candidate/validation.json # gates, held-out numbers, observability
      report.md                 # human-readable session report

Rules
-----
* Every JSON write is atomic (``types.atomic_write_json``: ``.tmp`` + rename).
* ``decisions.jsonl`` is append-only and is never rewritten, not even by
  :func:`resume` — history is the record of what the controller decided.
* :func:`load` is cheap (it reads the small JSON files and the recorded
  digests); :func:`verify` is the expensive one that re-hashes every
  materialised PGM.
* The image orientation has already been applied by the source
  (``types.FrameGroup``), so this module writes the arrays as given.

Nothing here reads the device or the private recordings.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

import numpy as np

from .types import (CAMERA_KEYS, IMAGE_HEIGHT, IMAGE_WIDTH, FrameGroup,
                    SessionPaths, append_jsonl, atomic_write_json, read_json,
                    sha256_file, write_pgm)

SESSION_SCHEMA = "einstar.wizard.session/1"
MANIFEST_SCHEMA = "einstar.wizard.manifest/1"

#: one complete three-camera group of 1024x1280 8-bit frames, in bytes
GROUP_BYTES = 3 * IMAGE_WIDTH * IMAGE_HEIGHT


class SessionError(RuntimeError):
    """A session operation was refused or the directory is inconsistent."""

    def __init__(self, message: str, code: str = "error", detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest_json(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     default=_json_default).encode()).hexdigest()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


def _paths(root: str | Path | SessionPaths) -> SessionPaths:
    if isinstance(root, SessionPaths):
        return root
    return SessionPaths(Path(root))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def create(root: str | Path, config: Mapping[str, Any], *,
           state: str = "created", spec: Mapping[str, Any] | None = None,
           exist_ok: bool = False) -> SessionPaths:
    """Create the session directory and its two atomic JSON files.

    Refuses to overwrite an existing session (``exist_ok=True`` allows
    re-writing ``session.json`` for a directory that already has the layout;
    the manifest and ``decisions.jsonl`` are never truncated).
    """
    paths = _paths(root)
    root = paths.root
    if paths.session_json.exists() and not exist_ok:
        raise FileExistsError(f"session already exists: {paths.session_json}")
    root.mkdir(parents=True, exist_ok=True)
    paths.groups_dir.mkdir(parents=True, exist_ok=True)
    paths.candidate_dir.mkdir(parents=True, exist_ok=True)
    if not paths.decisions_jsonl.exists():
        paths.decisions_jsonl.touch()

    cfg = dict(config or {})
    now = _now()
    session = {
        "schema": SESSION_SCHEMA,
        "created_utc": now,
        "updated_utc": now,
        "state": str(state),
        "root": str(root),
        "config": cfg,
        "config_sha256": _digest_json(cfg),
        "spec": dict(spec) if spec else None,
        "counters": {"decisions": 0, "accepted": 0, "rejected": 0,
                     "groups": 0, "next_group_index": 0, "next_decision_seq": 0},
        "hashes": {"session": None, "manifest": None, "decisions": None,
                   "raw": _file_digest_or_none(paths.raw_recording),
                   "status": _file_digest_or_none(paths.status_json)},
        "history": [{"utc": now, "event": "created"}],
    }
    if not paths.manifest_json.exists():
        manifest = {"schema": MANIFEST_SCHEMA, "created_utc": now,
                    "updated_utc": now, "accepted": [], "n_accepted": 0,
                    "accepted_digest": _digest_json([])}
        atomic_write_json(paths.manifest_json, manifest)
    session["hashes"]["manifest"] = _file_digest_or_none(paths.manifest_json)
    session["hashes"]["decisions"] = _file_digest_or_none(paths.decisions_jsonl)
    session["hashes"]["session"] = _digest_json(
        {k: v for k, v in session.items() if k != "hashes"})
    atomic_write_json(paths.session_json, session)
    return paths


def _file_digest_or_none(path: str | Path) -> str | None:
    p = Path(path)
    try:
        return sha256_file(p) if p.is_file() else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# session.json / manifest.json read-modify-write
# ---------------------------------------------------------------------------

def read_session(paths: str | Path | SessionPaths) -> dict:
    paths = _paths(paths)
    data = read_json(paths.session_json, default=None)
    if not isinstance(data, Mapping):
        raise SessionError(f"no readable session.json in {paths.root}", "missing")
    return dict(data)


def _update_session(paths: SessionPaths, mutate) -> dict:
    """Atomic read-modify-write of ``session.json``."""
    session = read_session(paths)
    mutate(session)
    session["updated_utc"] = _now()
    session["hashes"] = dict(session.get("hashes") or {})
    session["hashes"]["session"] = _digest_json(
        {k: v for k, v in session.items() if k != "hashes"})
    atomic_write_json(paths.session_json, session)
    return session


def append_decision(paths: str | Path | SessionPaths, decision: Mapping[str, Any]) -> dict:
    """Append one ingest decision to ``decisions.jsonl`` (atomic counters).

    Returns the record that was written, including its ``seq`` and ``utc``.
    The JSONL file is append-only: nothing is ever rewritten.
    """
    paths = _paths(paths)
    session = read_session(paths)
    counters = dict(session.get("counters") or {})
    seq = int(counters.get("decisions", 0))
    rec = dict(decision or {})
    rec.setdefault("utc", _now())
    rec["seq"] = seq
    accepted = rec.get("accepted")
    if accepted is None and "decision" in rec:
        accepted = str(rec["decision"]).lower() in ("accept", "accepted")
    append_jsonl(paths.decisions_jsonl, rec)

    def mutate(s: dict) -> None:
        c = dict(s.get("counters") or {})
        c["decisions"] = seq + 1
        c["next_decision_seq"] = seq + 1
        if accepted is True:
            c["accepted"] = int(c.get("accepted", 0)) + 1
        elif accepted is False:
            c["rejected"] = int(c.get("rejected", 0)) + 1
        s["counters"] = c
        s.setdefault("history", []).append(
            {"utc": rec["utc"], "event": "decision",
             "seq": seq, "accepted": bool(accepted)})
    _update_session(paths, mutate)
    return rec


# ---------------------------------------------------------------------------
# materialise + manifest
# ---------------------------------------------------------------------------

def materialise_group(paths: str | Path | SessionPaths, group: FrameGroup) -> dict:
    """Write the (already oriented) PGM triplet of one accepted group.

    Missing cameras are reported, not invented: an incomplete group materialises
    only the cameras it has, and ``complete`` is False.
    """
    paths = _paths(paths)
    index = int(group.index)
    d = paths.group_dir(index)
    d.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    missing: list[str] = []
    for cam in CAMERA_KEYS:
        img = group.images.get(cam)
        if img is None:
            missing.append(cam)
            continue
        p = paths.group_file(index, cam)
        write_pgm(p, np.asarray(img))
        files[cam] = {"path": str(p.relative_to(paths.root)),
                      "sha256": sha256_file(p), "bytes": int(p.stat().st_size),
                      "shape": [int(np.asarray(img).shape[0]), int(np.asarray(img).shape[1])]}
    rec = {"index": index, "device_group": group.device_group,
           "timestamp_s": float(group.timestamp_s), "source": group.source,
           "label": group.label, "complete": bool(group.complete and not missing),
           "missing": missing, "files": files,
           "sha256": _digest_json({"index": index, "files": files})}
    _update_session(paths, lambda s: s.setdefault("counters", {}).update(
        {"groups": int((s.get("counters") or {}).get("groups", 0)) + 1,
         "next_group_index": max(int((s.get("counters") or {}).get("next_group_index", 0)),
                                 index + 1)}))
    return rec


#: keys kept in one accepted-view manifest entry.  The raw marker pixels are
#: deliberately excluded: the manifest records what §9 asks for (index,
#: device_group, pose, quality) plus the derived numbers a resumed session
#: needs to re-measure coverage.  ``image_points`` is replaced by
#: ``plate_centre_px``, which is all D4 uses.
_VIEW_KEYS = ("index", "view_id", "device_group", "pose", "quality", "timestamp",
              "accepted", "n_markers", "shared_markers_ir", "assignment_ok",
              "stationary", "scale_artefact", "files", "sha256")


def _plate_centre(entry: Mapping[str, Any]) -> list[float] | None:
    pts = entry.get("image_points") or {}
    for cam in CAMERA_KEYS:
        arr = pts.get(cam) if isinstance(pts, Mapping) else None
        if arr is None:
            continue
        arr = np.asarray(arr, float).reshape(-1, 2)
        if len(arr):
            return arr.mean(axis=0).tolist()
    c = entry.get("plate_centre_px")
    if c is not None:
        try:
            return np.asarray(c, float).reshape(2).tolist()
        except (TypeError, ValueError):
            return None
    return None


def _normalise_view(entry: Mapping[str, Any]) -> dict:
    """Normalise one accepted view for the manifest (small, JSON-safe)."""
    out: dict[str, Any] = {}
    for key in _VIEW_KEYS:
        if key in entry:
            out[key] = entry[key]
    pose = out.get("pose")
    if pose is not None and not isinstance(pose, Mapping):
        try:
            R, t = pose
            out["pose"] = {"R": np.asarray(R, float).tolist(),
                           "t": np.asarray(t, float).tolist()}
        except (TypeError, ValueError):
            out["pose"] = None
    elif isinstance(pose, Mapping):
        out["pose"] = {
            "R": None if pose.get("R") is None else np.asarray(pose["R"], float).tolist(),
            "t": None if pose.get("t") is None else np.asarray(pose["t"], float).tolist()}
    for key in ("quality", "n_markers"):
        v = out.get(key)
        if isinstance(v, Mapping):
            out[key] = {str(k): (np.asarray(x).tolist() if isinstance(x, np.ndarray) else x)
                        for k, x in v.items()}
    centre = _plate_centre(entry)
    if centre is not None:
        out["plate_centre_px"] = centre
    out.setdefault("sha256", _digest_json(
        {k: out.get(k) for k in ("index", "view_id", "device_group", "pose",
                                 "quality", "timestamp", "plate_centre_px")}))
    return out


def save_manifest(paths: str | Path | SessionPaths,
                  accepted: Sequence[Mapping[str, Any]],
                  *, hash_files: bool = True) -> dict:
    """Atomically write ``manifest.json`` for the accepted views.

    Each entry keeps ``index``, ``device_group``, ``pose``, ``quality`` and the
    per-camera file digests.  File digests are reused from the entry when the
    controller passes them (``materialise_group`` already returns them);
    otherwise the files under ``groups/`` are hashed now.
    """
    paths = _paths(paths)
    prev = read_json(paths.manifest_json, default={}) or {}
    prev_by_index = {int(a.get("index", -1)): a for a in (prev.get("accepted") or [])
                     if isinstance(a, Mapping)}
    out: list[dict] = []
    for i, entry in enumerate(accepted):
        rec = _normalise_view(entry)
        rec.setdefault("index", int(entry.get("index", i)))
        files = rec.get("files")
        if not files and hash_files:
            idx = int(rec["index"])
            files = {}
            for cam in CAMERA_KEYS:
                p = paths.group_file(idx, cam)
                if p.is_file():
                    files[cam] = {"path": str(p.relative_to(paths.root)),
                                  "sha256": sha256_file(p),
                                  "bytes": int(p.stat().st_size)}
            if not files:
                old = prev_by_index.get(idx) or {}
                files = old.get("files") or {}
            rec["files"] = files
        rec["sha256"] = _digest_json(
            {k: rec.get(k) for k in ("index", "view_id", "device_group", "pose",
                                     "quality", "files", "plate_centre_px")})
        out.append(rec)
    now = _now()
    manifest = {"schema": MANIFEST_SCHEMA,
                "created_utc": (prev.get("created_utc") or now),
                "updated_utc": now,
                "accepted": out, "n_accepted": len(out),
                "accepted_digest": _digest_json(out)}
    atomic_write_json(paths.manifest_json, manifest)
    digest = _file_digest_or_none(paths.manifest_json)

    def mutate(s: dict) -> None:
        c = dict(s.get("counters") or {})
        c["accepted"] = len(out)
        c["next_group_index"] = max(
            [int(c.get("next_group_index", 0))]
            + [int(a.get("index", -1)) + 1 for a in out])
        s["counters"] = c
        h = dict(s.get("hashes") or {})
        h["manifest"] = digest
        h["decisions"] = _file_digest_or_none(paths.decisions_jsonl)
        s["hashes"] = h
        s.setdefault("history", []).append(
            {"utc": now, "event": "manifest", "n_accepted": len(out)})
    _update_session(paths, mutate)
    return {"n_accepted": len(out), "manifest_sha256": digest,
            "accepted_digest": manifest["accepted_digest"]}


# ---------------------------------------------------------------------------
# load / verify / resume
# ---------------------------------------------------------------------------

def load(root: str | Path | SessionPaths) -> dict:
    """Read config + accepted views + recorded hashes (no PGM re-hashing)."""
    paths = _paths(root)
    session = read_session(paths)
    manifest = read_json(paths.manifest_json, default=None)
    if not isinstance(manifest, Mapping):
        raise SessionError(f"no readable manifest.json in {paths.root}", "missing")
    accepted = list(manifest.get("accepted") or [])
    n_decisions = _count_lines(paths.decisions_jsonl)
    counters = dict(session.get("counters") or {})
    manifest_digest = _file_digest_or_none(paths.manifest_json)
    notes: list[str] = []
    if counters.get("decisions") not in (None, n_decisions):
        notes.append(f"counters.decisions={counters.get('decisions')} but "
                     f"decisions.jsonl has {n_decisions} lines")
    if counters.get("accepted") not in (None, len(accepted)):
        notes.append(f"counters.accepted={counters.get('accepted')} but manifest has "
                     f"{len(accepted)} entries")
    recorded_manifest = (session.get("hashes") or {}).get("manifest")
    if recorded_manifest and manifest_digest and recorded_manifest != manifest_digest:
        notes.append("manifest.json does not match the digest recorded in session.json")
    return {
        "paths": paths,
        "root": str(paths.root),
        "state": session.get("state"),
        "config": dict(session.get("config") or {}),
        "config_sha256": session.get("config_sha256"),
        "spec": session.get("spec"),
        "session": session,
        "manifest": dict(manifest),
        "accepted": accepted,
        "n_accepted": len(accepted),
        "counters": counters,
        "decisions": {"n_lines": n_decisions,
                      "sha256": _file_digest_or_none(paths.decisions_jsonl)},
        "hashes": {
            "session": (session.get("hashes") or {}).get("session"),
            "manifest": manifest_digest,
            "manifest_recorded": recorded_manifest,
            "decisions": (session.get("hashes") or {}).get("decisions"),
            "raw": _file_digest_or_none(paths.raw_recording),
            "status": _file_digest_or_none(paths.status_json),
            "accepted_files": {int(a.get("index", -1)): dict(a.get("files") or {})
                               for a in accepted},
        },
        "consistent": not notes,
        "notes": notes,
    }


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    n = 0
    with path.open("rb") as fh:
        for _ in fh:
            n += 1
    return n


def verify(root: str | Path | SessionPaths) -> dict:
    """Re-hash every materialised file and report mismatches.

    Checks, in order: the manifest's own digest against ``session.json``; every
    file listed in every accepted entry; files under ``groups/`` that no entry
    mentions; and the decision counter against the JSONL line count.
    """
    paths = _paths(root)
    data = load(paths)
    mismatches: list[dict] = []
    missing: list[dict] = []
    checked = 0
    listed: set[str] = set()
    for entry in data["accepted"]:
        idx = int(entry.get("index", -1))
        files = entry.get("files") or {}
        for cam, meta in files.items():
            rel = (meta or {}).get("path")
            if not rel:
                continue
            listed.add(str(rel))
            p = paths.root / rel
            checked += 1
            if not p.is_file():
                missing.append({"index": idx, "camera": cam, "path": str(rel)})
                continue
            actual = sha256_file(p)
            want = (meta or {}).get("sha256")
            if want and actual != want:
                mismatches.append({"index": idx, "camera": cam, "path": str(rel),
                                   "expected": want, "actual": actual})
    unexpected = []
    if paths.groups_dir.is_dir():
        for p in sorted(paths.groups_dir.rglob("*.pgm")):
            rel = str(p.relative_to(paths.root))
            if rel not in listed:
                unexpected.append(rel)
    counters = data["counters"]
    counters_ok = (counters.get("decisions") in (None, data["decisions"]["n_lines"])
                   and counters.get("accepted") in (None, data["n_accepted"]))
    manifest_ok = data["hashes"]["manifest_recorded"] in (None,
                                                          data["hashes"]["manifest"])
    ok = not mismatches and not missing and counters_ok and bool(manifest_ok)
    return {"ok": bool(ok), "checked": checked, "n_accepted": data["n_accepted"],
            "mismatches": mismatches, "missing": missing,
            "unexpected": unexpected,
            "manifest_digest_ok": bool(manifest_ok),
            "counters_ok": bool(counters_ok),
            "notes": list(data["notes"])}


def resume(root: str | Path | SessionPaths, *, verify_hashes: bool = True) -> dict:
    """Continue a session without rewriting its history.

    ``manifest.json`` and ``decisions.jsonl`` are left byte-identical; only
    ``session.json`` gains a ``resume`` history entry and the next indexes.  By
    default every materialised file is re-hashed first (§9): resuming onto
    damaged data would silently corrupt the calibration.
    """
    paths = _paths(root)
    data = load(paths)
    ver = verify(paths) if verify_hashes else None
    if ver is not None and not ver["ok"]:
        raise SessionError(
            "session failed its hash check; refusing to resume: "
            + json.dumps({k: ver[k] for k in ("mismatches", "missing",
                                              "manifest_digest_ok", "counters_ok")})[:400],
            "verify_failed", ver)
    counters = dict(data["counters"])
    next_group = int(counters.get("next_group_index", 0))
    if paths.groups_dir.is_dir():
        existing = [int(p.name) for p in paths.groups_dir.iterdir()
                    if p.is_dir() and p.name.isdigit()]
        next_group = max([next_group, data["n_accepted"]] + [i + 1 for i in existing])
    next_seq = int(counters.get("decisions", data["decisions"]["n_lines"]))
    now = _now()

    def mutate(s: dict) -> None:
        c = dict(s.get("counters") or {})
        c["next_group_index"] = next_group
        c["next_decision_seq"] = next_seq
        s["counters"] = c
        s["state"] = "resumed"
        s.setdefault("history", []).append(
            {"utc": now, "event": "resume", "next_group_index": next_group,
             "next_decision_seq": next_seq,
             "verified": bool(ver["ok"]) if ver else None})
    _update_session(paths, mutate)
    return {"paths": paths, "state": "resumed", "next_group_index": next_group,
            "next_decision_seq": next_seq, "n_accepted": data["n_accepted"],
            "config": data["config"], "verify": ver,
            "note": "manifest.json and decisions.jsonl were not modified"}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def disk_free_bytes(path: str | Path) -> int:
    """Free bytes on the filesystem holding ``path`` (or its nearest parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return int(shutil.disk_usage(p).free)


def tree_bytes(path: str | Path) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        return int(p.stat().st_size)
    return int(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()))


def storage_guard(paths: str | Path | SessionPaths, limit_gb: float,
                  *, group_bytes: int = GROUP_BYTES) -> dict:
    """Refuse to keep recording when free space falls below ``limit_gb``.

    ``group_bytes`` is the size of one complete three-camera group (the PGMs on
    disk); ``groups_left`` is therefore an *estimate* derived from free space,
    while ``free_gb`` and ``used_gb`` are measured.
    """
    paths = _paths(paths)
    free = disk_free_bytes(paths.root)
    used = tree_bytes(paths.root)
    limit = float(limit_gb) * 1e9
    ok = free >= limit
    est = int(free // max(int(group_bytes), 1))
    return {"ok": bool(ok), "path": str(paths.root),
            "free_bytes": free, "free_gb": free / 1e9,
            "used_bytes": used, "used_gb": used / 1e9,
            "limit_gb": float(limit_gb),
            "group_bytes": int(group_bytes),
            "groups_left_estimate": est,
            "action": "continue" if ok else "stop",
            "message": (f"{free / 1e9:.1f} GB free (limit {limit_gb:.1f} GB), "
                        f"session uses {used / 1e9:.2f} GB, ~{est} more groups fit"
                        if ok else
                        f"only {free / 1e9:.2f} GB free: below the {limit_gb:.1f} GB "
                        "limit, stop the session")}
