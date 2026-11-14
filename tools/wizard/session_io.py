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

Production storage (the default for a new session, :class:`ViewStore`)::

    SESSION/
      storage.json              # atomic summary: mode, measured bytes/view, codec
      views.jsonl               # append-only CRC-framed journal (one record/view)
      views_manifest.json       # rebuildable digest of every stored view
      views/000123/cam0.png     # lossless PNG triplet (names match FILE_CAM)
      views/000123/cam1.png
      views/000123/cam2.png
      views/000123/observation.json  # refined marker observations + pose
      views/000123/view.json         # per-view record: hashes, sizes, timestamps
      diagnostics/...           # diagnostic mode only, explicitly bounded

Rules
-----
* Every JSON write is atomic (``types.atomic_write_json``: ``.tmp`` + rename).
* ``decisions.jsonl`` is append-only and is never rewritten, not even by
  :func:`resume` — history is the record of what the controller decided.
* :func:`load` is cheap (it reads the small JSON files and the recorded
  digests); :func:`verify` is the expensive one that re-hashes every
  materialised PGM **and** every stored production view.
* The image orientation has already been applied by the source
  (``types.FrameGroup``), so this module writes the arrays as given.
* Production views are written as: temp directory → ``fsync`` → atomic
  rename → append the CRC-framed journal record → ``fsync``.  A crash at any
  point leaves either no view, a complete view recoverable by scanning
  ``views/``, or a truncated journal tail that :meth:`ViewStore.load` drops
  while keeping the intact prefix.
* A lossless ``.e1r`` recording is **not** part of a production session.  It
  is a diagnostic/developer option selected on the capture side
  (``tools.wizard.capture``); ``storage.json`` records which mode was used.

Nothing here reads the device or the private recordings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
from typing import Any, Iterator, Mapping, Sequence
import zlib

import numpy as np

from .types import (CAMERA_KEYS, FILE_CAM, IMAGE_HEIGHT, IMAGE_WIDTH,
                    FrameGroup, SessionPaths, append_jsonl, atomic_write_json,
                    read_json, read_pgm, sha256_file, write_pgm)

SESSION_SCHEMA = "einstar.wizard.session/1"
MANIFEST_SCHEMA = "einstar.wizard.manifest/1"

#: one complete three-camera group of 1024x1280 8-bit frames, in bytes
GROUP_BYTES = 3 * IMAGE_WIDTH * IMAGE_HEIGHT

#: layout generation recorded in ``storage.json``.  ``legacy`` means the
#: per-view ``groups/NNNNNN/camN.pgm`` triplet; ``production`` means the
#: compressed ``views/NNNNNN`` record described in :class:`ViewStore`.
STORAGE_LEGACY = "legacy"
STORAGE_PRODUCTION = "production"
STORAGE_DIAGNOSTIC = "diagnostic"
#: The default storage mode for a new production session: compressed views,
#: no lossless ``.e1r`` recording.
DEFAULT_STORAGE_MODE = STORAGE_PRODUCTION

STORAGE_SCHEMA = "einstar.wizard.storage/1"
VIEW_SCHEMA = "einstar.wizard.view/1"
VIEWS_MANIFEST_SCHEMA = "einstar.wizard.views-manifest/1"

#: Measured on the session-03 corpus (297 accepted views): see
#: ``docs/calibration/production_capture.md``.  Used only for a-priori size
#: estimates, never for accounting; a real session reports its own measurement.
PRODUCTION_BYTES_PER_VIEW = 1_870_000
#: 3.93 MB/group x 9.807 groups/s = 38.6 MB/s measured on session-03, i.e. the
#: lossless ``.e1r`` stream cost the old default paid for every second.
LOSSLESS_STREAM_BYTES_PER_SECOND = 38_600_000
LOSSLESS_BYTES_PER_VIEW_SESSION03 = 175_008_113


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
           exist_ok: bool = False, storage: Any = None,
           storage_options: Mapping[str, Any] | None = None) -> SessionPaths:
    """Create the session directory and its two atomic JSON files.

    Refuses to overwrite an existing session (``exist_ok=True`` allows
    re-writing ``session.json`` for a directory that already has the layout;
    the manifest and ``decisions.jsonl`` are never truncated).

    ``storage`` selects the accepted-view layout.  ``None`` (the historical
    default) creates only the legacy structure and is what the current
    controller calls; ``"production"``/``"diagnostic"`` additionally create
    the compact :class:`ViewStore` layout (``views/``, ``views.jsonl``,
    ``storage.json``).  A production session never expects a ``.e1r``.
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
    if storage is not None:
        open_store(paths, mode=storage, **(dict(storage_options or {})))
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

def _production_state(paths: SessionPaths) -> dict | None:
    """Cheap production-layout view for :func:`load` (``None`` when absent)."""
    if not _storage_path(paths).exists() and not (paths.root / "views").is_dir():
        return None
    try:
        store = ViewStore(paths)
        store.reload()
        summary = store.summary()
        rows = [{"index": int(record["index"]),
                 "view_id": int(record.get("view_id", record["index"])),
                 "stored_bytes": int(record.get("stored_bytes") or 0),
                 "observation_sha256": record.get("observation_sha256"),
                 "files": {cam: {"path": meta.get("path"),
                                 "sha256": meta.get("sha256"),
                                 "bytes": int(meta.get("bytes") or 0)}
                           for cam, meta in (record.get("files") or {}).items()}}
                for record in store.records()]
    except Exception as exc:  # never make load() fail on a damaged store
        return {"mode": STORAGE_LEGACY, "summary": None, "rows": [],
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"mode": summary["mode"], "summary": summary, "rows": rows,
            "ok": not summary["recovery"].get("journal_corrupt")}


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
    production = _production_state(paths)
    if production is not None:
        if production.get("error"):
            notes.append("production storage is unreadable: "
                         + str(production["error"]))
        else:
            recovery = (production.get("summary") or {}).get("recovery") or {}
            if recovery.get("journal_truncated"):
                notes.append(
                    "views.jsonl has a truncated tail; the intact prefix was "
                    f"recovered ({recovery.get('tail_bytes')} bytes dropped)")
            if recovery.get("journal_corrupt"):
                notes.append("views.jsonl has a corrupt record; the prefix "
                             "before it was recovered")
            if recovery.get("recovered_orphans"):
                notes.append(
                    f"{recovery.get('recovered_orphans')} complete view(s) were "
                    "recovered from views/ because the journal did not name them")
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
        "storage": (None if production is None
                    else dict(production.get("summary") or
                              {"mode": production.get("mode")})),
        "views": ([] if production is None else list(production.get("rows") or [])),
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
    notes = list(data["notes"])
    storage: dict | None = None
    if data.get("storage") is not None:
        try:
            store = ViewStore(paths)
            store.reload()
            storage = store.verify()
        except Exception as exc:  # a damaged store must never hide behind ok
            storage = {"ok": False, "checked": 0, "n_views": 0,
                       "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(ok) and bool(storage.get("ok"))
        checked += int(storage.get("checked") or 0)
        recovery = (storage.get("journal") or {})
        if recovery.get("recovered_orphans"):
            notes.append(f"{recovery['recovered_orphans']} production view(s) "
                         "recovered from views/ (journal record was lost)")
        if recovery.get("journal_truncated"):
            notes.append("views.jsonl has a truncated tail; the intact prefix "
                         "was verified")
    return {"ok": bool(ok), "checked": checked, "n_accepted": data["n_accepted"],
            "mismatches": mismatches, "missing": missing,
            "unexpected": unexpected,
            "manifest_digest_ok": bool(manifest_ok),
            "counters_ok": bool(counters_ok),
            "storage": storage,
            "notes": notes}


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
    # production views live outside groups/, so they must extend the index too
    view_indexes = [int(view.get("index", -1)) for view in (data.get("views") or [])
                    if int(view.get("index", -1)) >= 0]
    if view_indexes:
        next_group = max([next_group] + [i + 1 for i in view_indexes])
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


# ---------------------------------------------------------------------------
# production storage: compact, crash-safe accepted views
# ---------------------------------------------------------------------------

#: Journal framing: magic + payload length + CRC-32 of the payload.  A record
#: that is incomplete or fails its CRC ends the readable prefix; the loader
#: never guesses at bytes it cannot verify.
JOURNAL_MAGIC = b"E1V1"
_JOURNAL_HEADER = struct.Struct("<4sII")
#: Defensive cap so a damaged length field cannot allocate gigabytes.
JOURNAL_MAX_RECORD = 4 * 1024 * 1024

#: Default PNG deflate level.  Measured on the real corpus: level 3 gives
#: 1.870 MB/view (2.10x smaller than PGM) at 56 ms/view; level 9 is 10x slower
#: for 1 % more.  See docs/calibration/production_capture.md.
DEFAULT_PNG_LEVEL = 3
DEFAULT_CODEC = "png"

#: Diagnostic frames are an explicit, bounded developer aid, never the record.
DEFAULT_DIAGNOSTIC_GROUPS = 200
DEFAULT_DIAGNOSTIC_BYTES = 256 * 1024 * 1024
#: Measured live cadence used for the seconds conversion (session-03).
DEFAULT_CADENCE_HZ = 9.807


def resolve_storage_mode(value: Any) -> str:
    """Normalise a caller's storage mode name.

    ``production``/``compact``/``default`` → compressed views, no recording.
    ``diagnostic``/``developer``/``lossless`` → compressed views **and** the
    lossless capture recording (selected on the capture side).
    ``legacy``/``pgm`` → the historical uncompressed ``groups/`` layout.
    """
    text = str(value or DEFAULT_STORAGE_MODE).strip().lower()
    if text in ("production", "compact", "default", "normal"):
        return STORAGE_PRODUCTION
    if text in ("diagnostic", "developer", "lossless", "debug"):
        return STORAGE_DIAGNOSTIC
    if text in ("legacy", "pgm", "groups"):
        return STORAGE_LEGACY
    raise SessionError(f"unknown storage mode: {value!r}", "bad_storage_mode")


@dataclass(frozen=True)
class StorageConfig:
    """How a production session stores accepted views.

    ``mode`` is :data:`STORAGE_PRODUCTION` (default; no ``.e1r``) or
    :data:`STORAGE_DIAGNOSTIC` (also allows a bounded set of diagnostic
    frames and expects the capture side to keep the lossless recording).
    """

    mode: str = DEFAULT_STORAGE_MODE
    codec: str = DEFAULT_CODEC
    png_level: int = DEFAULT_PNG_LEVEL
    #: fsync every written file and every journal record.  Costs milliseconds
    #: per accepted view (a few per minute) and is what makes the crash story
    #: real rather than aspirational.
    fsync: bool = True
    diagnostic_max_groups: int = DEFAULT_DIAGNOSTIC_GROUPS
    diagnostic_max_bytes: int = DEFAULT_DIAGNOSTIC_BYTES
    cadence_hz: float = DEFAULT_CADENCE_HZ

    def __post_init__(self) -> None:
        mode = resolve_storage_mode(self.mode)
        object.__setattr__(self, "mode", mode)
        if str(self.codec).lower() != "png":
            raise SessionError(f"unsupported codec: {self.codec!r}",
                               "bad_codec")
        object.__setattr__(self, "codec", "png")
        level = int(self.png_level)
        if not 0 <= level <= 9:
            raise SessionError("png_level must be 0..9", "bad_codec")
        object.__setattr__(self, "png_level", level)
        if int(self.diagnostic_max_groups) < 0:
            raise SessionError("diagnostic_max_groups must be >= 0",
                               "bad_storage_mode")
        if float(self.cadence_hz) <= 0:
            raise SessionError("cadence_hz must be > 0", "bad_storage_mode")

    @property
    def lossless_recording(self) -> bool:
        """Whether the capture side is expected to keep a raw ``.e1r``."""
        return self.mode == STORAGE_DIAGNOSTIC

    def as_dict(self) -> dict:
        return {"mode": self.mode, "codec": self.codec,
                "png_level": int(self.png_level), "fsync": bool(self.fsync),
                "lossless_recording": self.lossless_recording,
                "diagnostic_max_groups": int(self.diagnostic_max_groups),
                "diagnostic_max_bytes": int(self.diagnostic_max_bytes),
                "cadence_hz": float(self.cadence_hz)}


def storage_config(mode: Any = None, **kwargs: Any) -> StorageConfig:
    """Build a :class:`StorageConfig` from a mode name and overrides."""
    if isinstance(mode, StorageConfig):
        return mode
    values = dict(kwargs)
    if mode is not None:
        values["mode"] = mode
    return StorageConfig(**values)


# -- codec helpers ----------------------------------------------------------

def _cv2() -> Any:
    try:
        import cv2
    except Exception as exc:  # pragma: no cover - cv2 is a hard dependency
        raise SessionError(f"PNG codec unavailable: {exc}", "codec_missing")
    return cv2


def encode_frame_png(image: np.ndarray, level: int = DEFAULT_PNG_LEVEL) -> bytes:
    """Losslessly encode one 8-bit grey frame as PNG bytes."""
    cv2 = _cv2()
    arr = np.ascontiguousarray(np.asarray(image, dtype=np.uint8))
    if arr.ndim != 2:
        raise SessionError(f"expected a 2-D grey frame, got shape {arr.shape}",
                           "bad_frame")
    ok, buf = cv2.imencode(".png", arr,
                           [int(cv2.IMWRITE_PNG_COMPRESSION), int(level)])
    if not ok:
        raise SessionError("PNG encoding failed", "encode_failed")
    return bytes(buf.tobytes())


def decode_frame_png(data: bytes) -> np.ndarray:
    """Decode PNG bytes back to the exact stored pixel array."""
    cv2 = _cv2()
    out = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if isinstance(out, tuple):  # OpenCV 4 returns (retval, image)
        out = out[1]
    if out is None:
        raise SessionError("PNG decoding failed", "decode_failed")
    return np.ascontiguousarray(out)


def images_sha256(images: Mapping[str, Any]) -> str:
    """Digest of a whole triplet's pixel bytes (camera order fixed)."""
    h = hashlib.sha256()
    for cam in CAMERA_KEYS:
        value = images.get(cam)
        if value is None:
            h.update(b"<missing>")
            continue
        h.update(str(cam).encode())
        h.update(np.ascontiguousarray(np.asarray(value, dtype=np.uint8)).tobytes())
    return h.hexdigest()


def pixel_sha256(image: np.ndarray) -> str:
    """Digest of the exact pixel bytes (independent of the container)."""
    arr = np.ascontiguousarray(np.asarray(image, dtype=np.uint8))
    return hashlib.sha256(arr.tobytes()).hexdigest()


# -- journal ----------------------------------------------------------------

def _journal_path(root: str | Path | SessionPaths) -> Path:
    return _paths(root).root / "views.jsonl"


def _storage_path(root: str | Path | SessionPaths) -> Path:
    return _paths(root).root / "storage.json"


def _views_manifest_path(root: str | Path | SessionPaths) -> Path:
    return _paths(root).root / "views_manifest.json"


def _diagnostics_journal_path(root: str | Path | SessionPaths) -> Path:
    return _paths(root).root / "diagnostics.jsonl"


def read_journal(path: str | Path) -> tuple[list[dict], dict]:
    """Read every intact record from a framed journal.

    Returns ``(records, info)``.  Reading stops at the first record that is
    incomplete or fails its CRC; ``info`` reports how many bytes are intact,
    how many belong to the damaged tail, and which failure ended the scan.
    Nothing past the damage is interpreted, so a torn tail can not inject a
    half-written view.
    """
    p = Path(path)
    info = {"path": str(p), "records": 0, "intact_bytes": 0, "tail_bytes": 0,
            "truncated": False, "corrupt": False, "reason": None}
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return [], info
    except OSError as exc:
        info["reason"] = f"unreadable: {type(exc).__name__}: {exc}"
        info["truncated"] = True
        return [], info
    records: list[dict] = []
    offset = 0
    total = len(data)
    while offset < total:
        header_end = offset + _JOURNAL_HEADER.size
        if header_end > total:
            info["truncated"] = True
            info["reason"] = "partial record header"
            break
        magic, length, crc = _JOURNAL_HEADER.unpack_from(data, offset)
        if magic != JOURNAL_MAGIC or length > JOURNAL_MAX_RECORD:
            info["corrupt"] = True
            info["reason"] = "bad record header"
            break
        end = header_end + int(length)
        if end > total:
            info["truncated"] = True
            info["reason"] = "partial record payload"
            break
        payload = data[header_end:end]
        if (zlib.crc32(payload) & 0xFFFFFFFF) != int(crc):
            info["corrupt"] = True
            info["reason"] = "record CRC mismatch"
            break
        try:
            record = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            info["corrupt"] = True
            info["reason"] = "record is not valid JSON"
            break
        if not isinstance(record, Mapping):
            info["corrupt"] = True
            info["reason"] = "record is not an object"
            break
        records.append(dict(record))
        offset = end
    info["records"] = len(records)
    info["intact_bytes"] = int(offset)
    info["tail_bytes"] = int(total - offset)
    return records, info


def append_journal(path: str | Path, record: Mapping[str, Any], *,
                   fsync: bool = True) -> int:
    """Append one CRC-framed record; return the bytes written.

    The write is one bounded ``write(2)`` per record (loop-free for the small
    records this module writes) followed by ``fsync`` when requested, so a
    crash leaves at most one torn tail record.
    """
    payload = json.dumps(dict(record), sort_keys=True,
                         default=_json_default).encode("utf-8")
    if len(payload) > JOURNAL_MAX_RECORD:
        raise SessionError("journal record is too large", "record_too_large")
    blob = _JOURNAL_HEADER.pack(JOURNAL_MAGIC, len(payload),
                                zlib.crc32(payload) & 0xFFFFFFFF) + payload
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = 0
        while written < len(blob):
            written += os.write(fd, blob[written:])
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    return len(blob)


# -- per-view files ---------------------------------------------------------

def _write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _canonical_observation(observation: Mapping[str, Any] | None,
                           extra: Mapping[str, Any] | None = None) -> dict:
    """The JSON observation stored next to a view (pixels are NOT here).

    The manifest keeps a compact projection (``_normalise_view`` drops raw
    marker pixels); the production store keeps the **refined observations**
    because replaying a session must reproduce the same input to the solver
    without re-detecting the frames.
    """
    out: dict[str, Any] = {}
    if isinstance(observation, Mapping):
        out.update({str(k): _jsonable(v) for k, v in observation.items()})
    if isinstance(extra, Mapping):
        out.update({str(k): _jsonable(v) for k, v in extra.items()})
    return out


def _observation_sha(observation: Mapping[str, Any]) -> str:
    """Stable digest of an observation record (independent of file layout)."""
    return hashlib.sha256(
        json.dumps(observation, sort_keys=True,
                   default=_json_default).encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


class ViewStore:
    """Compact, crash-safe storage for accepted calibration views.

    One instance owns one session directory's production layout.  It is used
    by the controller at accept time (``add_view``), by the offline migration
    (:func:`compact_session`) and by verification/replay
    (:meth:`iter_views`, :func:`verify_views`).

    The store is deliberately independent of the controller's decision logic:
    *which* frames are accepted is the existing acceptance/coverage machinery;
    this class only makes retaining them cheap, durable and verifiable.
    """

    def __init__(self, root: str | Path | SessionPaths,
                 config: StorageConfig | None = None) -> None:
        self.paths = _paths(root)
        self.config = config or StorageConfig()
        self.views_dir = self.paths.root / "views"
        self.diagnostics_dir = self.paths.root / "diagnostics"
        self.journal_path = _journal_path(self.paths)
        self.diagnostics_journal_path = _diagnostics_journal_path(self.paths)
        self.storage_path = _storage_path(self.paths)
        self.manifest_path = _views_manifest_path(self.paths)
        self._views: dict[int, dict] = {}
        self._diagnostics: list[dict] = []
        self._counters = {"views": 0, "view_bytes": 0, "frame_bytes": 0,
                          "observation_bytes": 0, "journal_bytes": 0,
                          "diagnostic_frames": 0, "diagnostic_bytes": 0}
        self._sizes: list[int] = []
        self._recovery = {"journal_truncated": False,
                          "journal_corrupt": False,
                          "recovered_orphans": 0,
                          "reason": None}
        self._created_utc: str | None = None
        self._loaded = False
        self._mode_explicit = False
        self._pgm_bytes_before: int | None = None

    # -- opening ---------------------------------------------------------
    @classmethod
    def open(cls, root: str | Path | SessionPaths, *,
             mode: Any = None, **kwargs: Any) -> "ViewStore":
        """Open (creating if needed) the production layout for a session."""
        config = storage_config(mode, **kwargs)
        store = cls(root, config)
        # An explicit mode wins; a read-only open adopts storage.json's mode.
        store._mode_explicit = mode is not None
        store._ensure_layout()
        store.reload()
        store._repair_journal()
        return store

    def _ensure_layout(self) -> None:
        self.views_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.journal_path.exists():
            self.journal_path.touch()
        if self.config.mode == STORAGE_DIAGNOSTIC:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        existing = read_json(self.storage_path, default=None)
        if isinstance(existing, Mapping) and existing.get("created_utc"):
            self._created_utc = str(existing["created_utc"])
        else:
            self._created_utc = _now()
        previous = read_json(self.storage_path, default=None)
        if isinstance(previous, Mapping):
            before = previous.get("migrated_from")
            if isinstance(before, Mapping) and before.get("pgm_bytes_before"):
                self._pgm_bytes_before = int(before["pgm_bytes_before"])
        if not isinstance(previous, Mapping) or self._mode_explicit:
            # make the chosen mode durable immediately: a later read-only open
            # (load/verify/resume) must know whether lossless recording was
            # requested even when the session stored no view yet
            self._write_summary()

    def _adopt_recorded_mode(self) -> None:
        """Adopt the configuration recorded in ``storage.json`` (read-only open)."""
        if self._mode_explicit:
            return
        existing = read_json(self.storage_path, default=None)
        if not isinstance(existing, Mapping):
            return
        values: dict[str, Any] = {}
        try:
            values["mode"] = resolve_storage_mode(existing.get("mode"))
        except SessionError:
            pass
        if existing.get("codec") is not None:
            values["codec"] = str(existing["codec"])
        if existing.get("png_level") is not None:
            values["png_level"] = int(existing["png_level"])
        if existing.get("fsync") is not None:
            values["fsync"] = bool(existing["fsync"])
        if existing.get("cadence_hz") is not None:
            values["cadence_hz"] = float(existing["cadence_hz"])
        diagnostics = existing.get("diagnostics")
        if isinstance(diagnostics, Mapping):
            if diagnostics.get("max_groups") is not None:
                values["diagnostic_max_groups"] = int(diagnostics["max_groups"])
            if diagnostics.get("max_bytes") is not None:
                values["diagnostic_max_bytes"] = int(diagnostics["max_bytes"])
        changes = {key: value for key, value in values.items()
                   if getattr(self.config, key, None) != value}
        if not changes:
            return
        try:
            self.config = replace(self.config, **changes)
        except (SessionError, ValueError):
            pass

    def reload(self) -> dict:
        """Read the journal and recover any complete view the journal missed."""
        self._adopt_recorded_mode()
        records, info = read_journal(self.journal_path)
        self._views = {}
        superseded: list[int] = []
        for record in records:
            index = int(record.get("index", -1))
            if index < 0:
                continue
            if index in self._views:
                # append-only log: the later record replaces an earlier one for
                # the same view (a re-delivered or corrected view)
                superseded.append(index)
            self._views[index] = dict(record)
        self._recovery["journal_truncated"] = bool(info.get("truncated"))
        self._recovery["journal_corrupt"] = bool(info.get("corrupt"))
        self._recovery["reason"] = info.get("reason")
        self._recovery["journal_records"] = int(info.get("records", 0))
        self._recovery["intact_bytes"] = int(info.get("intact_bytes", 0))
        self._recovery["journal_bytes"] = int(info.get("intact_bytes", 0))
        self._recovery["tail_bytes"] = int(info.get("tail_bytes", 0))
        self._recovery["superseded"] = sorted(set(superseded))
        self._recovery["recovered_orphans"] = 0
        recovered: list[int] = []
        for record in self._orphan_records():
            index = int(record.get("index", -1))
            if index < 0 or index in self._views:
                continue
            self._views[index] = dict(record)
            recovered.append(index)
        self._recovery["recovered_orphans"] = len(recovered)
        self._recovery["recovered_indexes"] = sorted(recovered)
        self._views = dict(sorted(self._views.items()))
        self._diagnostics = self._diagnostic_records()
        self._refresh_counters()
        self._loaded = True
        return self._counters

    def _diagnostic_records(self) -> list[dict]:
        records, _info = read_journal(self.diagnostics_journal_path)
        by_index = {int(r.get("index", -1)): dict(r) for r in records
                    if int(r.get("index", -1)) >= 0}
        # a diagnostic record that lost its journal tail is still on disk
        try:
            entries = sorted(self.diagnostics_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            index = int(entry.name)
            if index in by_index:
                continue
            record = read_json(entry / "view.json", default=None)
            if isinstance(record, Mapping) and record.get("schema") == VIEW_SCHEMA:
                by_index[index] = dict(record)
        return [by_index[key] for key in sorted(by_index)]

    def _repair_journal(self) -> bool:
        """Drop an unreadable journal tail so new appends stay readable.

        A crash can leave a partially written record at the end of
        ``views.jsonl``.  Appending after it would bury the torn bytes in the
        middle of the file, where every later record becomes unreachable, so a
        writable open truncates the file to the last intact record first.  The
        views themselves are already on disk (the journal append is the last
        step) and are recovered by :meth:`reload`.
        """
        recovery = self._recovery
        if not (recovery.get("journal_truncated")
                or recovery.get("journal_corrupt")):
            return False
        intact = int(recovery.get("intact_bytes") or 0)
        try:
            with self.journal_path.open("r+b") as fh:
                fh.truncate(intact)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            return False
        recovery["repaired"] = True
        recovery["repaired_bytes"] = int(recovery.get("tail_bytes") or 0)
        recovery["journal_truncated"] = False
        recovery["journal_corrupt"] = False
        recovery["tail_bytes"] = 0
        return True

    def _orphan_records(self) -> list[dict]:
        """View records on disk that the journal does not name (crash recovery).

        A view directory is only renamed into place after every frame and the
        observation file are fsynced, so its presence means the view is
        complete; the journal append is the last step and may have been lost.
        """
        out: list[dict] = []
        try:
            entries = sorted(self.views_dir.iterdir())
        except OSError:
            return out
        for entry in entries:
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            record = read_json(entry / "view.json", default=None)
            if isinstance(record, Mapping) and record.get("schema") == VIEW_SCHEMA:
                out.append(dict(record))
        return out

    def _refresh_counters(self) -> None:
        frames = view_bytes = frame_bytes = obs_bytes = 0
        sizes: list[int] = []
        for record in self._views.values():
            stored = int(record.get("stored_bytes") or 0)
            view_bytes += stored
            frame_bytes += int(record.get("frame_bytes") or 0)
            obs_bytes += int(record.get("observation_bytes") or 0)
            sizes.append(stored)
            frames += 1
        self._sizes = sizes
        diag_frames = sum(len(r.get("files") or {}) for r in self._diagnostics)
        diag_bytes = sum(int(r.get("stored_bytes") or 0)
                         for r in self._diagnostics)
        self._counters.update({"views": frames, "view_bytes": view_bytes,
                               "frame_bytes": frame_bytes,
                               "observation_bytes": obs_bytes,
                               "diagnostic_frames": diag_frames,
                               "diagnostic_bytes": diag_bytes,
                               "journal_bytes": _file_size(self.journal_path)})

    # -- writing ---------------------------------------------------------
    def add_view(self, group: FrameGroup, observation: Mapping[str, Any] | None,
                 *, view_id: int | None = None,
                 extra: Mapping[str, Any] | None = None) -> dict:
        """Store one accepted view (triplet + refined observations + hashes)."""
        index = int(view_id if view_id is not None else group.index)
        return self.add_images(
            index, dict(group.images or {}), observation,
            device_group=group.device_group,
            timestamp_s=float(group.timestamp_s),
            source=str(group.source), label=str(group.label or ""),
            complete=bool(group.complete), extra=extra, group_index=int(group.index))

    def add_images(self, index: int, images: Mapping[str, Any],
                   observation: Mapping[str, Any] | None, *,
                   view_id: int | None = None,
                   group_index: int | None = None,
                   device_group: int | None = None,
                   timestamp_s: float | None = None,
                   source: str = "", label: str = "",
                   complete: bool = True,
                   extra: Mapping[str, Any] | None = None) -> dict:
        """Store one view from explicit images (used by migration too)."""
        index = int(index)
        if index < 0:
            raise SessionError("view index must be >= 0", "bad_index")
        obs = _canonical_observation(observation, extra)
        obs_sha = _observation_sha(obs)
        frames_sha = images_sha256(images)
        target = self.views_dir / f"{index:06d}"
        existing = read_json(target / "view.json", default=None)
        if (isinstance(existing, Mapping)
                and existing.get("schema") == VIEW_SCHEMA
                and existing.get("observation_sha256") == obs_sha
                and existing.get("images_sha256") == frames_sha):
            # idempotent re-add (resume/replay): keep the stored record
            record = dict(existing)
            self._views[index] = record
            self._refresh_counters()
            self._write_summary()
            return record
        record = self._write_view(
            index, images, obs, obs_sha, base_dir=self.views_dir,
            rel_prefix="views", frames_sha=frames_sha,
            view_id=view_id, group_index=group_index,
            device_group=device_group, timestamp_s=timestamp_s,
            source=source, label=label, complete=complete)
        append_journal(self.journal_path, record, fsync=self.config.fsync)
        self._views[index] = record
        self._views = dict(sorted(self._views.items()))
        self._refresh_counters()
        self._write_summary()
        return record

    def _write_view(self, index: int, images: Mapping[str, Any], obs: dict,
                    obs_sha: str, *, base_dir: Path, rel_prefix: str,
                    frames_sha: str | None = None,
                    view_id: int | None = None,
                    group_index: int | None = None,
                    device_group: int | None = None,
                    timestamp_s: float | None = None,
                    source: str = "", label: str = "",
                    complete: bool = True,
                    extra_record: Mapping[str, Any] | None = None) -> dict:
        """Write one view directory atomically (temp dir -> fsync -> rename)."""
        index = int(index)
        images_sha = frames_sha if frames_sha is not None else images_sha256(images)
        base_dir.mkdir(parents=True, exist_ok=True)
        tmp = base_dir / f".{index:06d}.tmp"
        _remove_tree(tmp)
        tmp.mkdir(parents=True)
        files: dict[str, dict] = {}
        missing: list[str] = []
        frame_bytes = 0
        stored_bytes = 0
        try:
            for cam in CAMERA_KEYS:
                value = images.get(cam)
                if value is None:
                    missing.append(cam)
                    continue
                arr = np.ascontiguousarray(np.asarray(value, dtype=np.uint8))
                data = encode_frame_png(arr, self.config.png_level)
                name = f"cam{FILE_CAM[cam]}.png"
                _write_bytes(tmp / name, data, fsync=self.config.fsync)
                frame_bytes += int(arr.nbytes)
                stored_bytes += len(data)
                files[cam] = {
                    "name": name,
                    "path": f"{rel_prefix}/{index:06d}/{name}",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "image_sha256": pixel_sha256(arr),
                    "width": int(arr.shape[1]), "height": int(arr.shape[0]),
                    "dtype": str(arr.dtype),
                }
            if not files:
                raise SessionError("a view needs at least one frame",
                                   "empty_view")
            obs_bytes = json.dumps(obs, sort_keys=True, indent=1,
                                   default=_json_default).encode() + b"\n"
            _write_bytes(tmp / "observation.json", obs_bytes,
                         fsync=self.config.fsync)
            record = {
                "schema": VIEW_SCHEMA,
                "index": index,
                "view_id": int(view_id if view_id is not None else index),
                "group_index": (int(group_index) if group_index is not None
                                else index),
                "device_group": device_group,
                "timestamp_s": (None if timestamp_s is None
                                else float(timestamp_s)),
                "source": str(source), "label": str(label),
                "complete": bool(complete and not missing),
                "missing": missing,
                "codec": self.config.codec,
                "png_level": int(self.config.png_level),
                "files": files,
                "frame_bytes": int(frame_bytes),
                "stored_bytes": int(stored_bytes),
                "observation_sha256": obs_sha,
                "observation_bytes": int(len(obs_bytes)),
                "images_sha256": (images_sha if frames_sha is None
                                  else frames_sha),
                "created_utc": _now(),
            }
            if isinstance(extra_record, Mapping):
                record.update({str(k): _jsonable(v)
                               for k, v in extra_record.items()})
            _write_bytes(tmp / "view.json",
                         json.dumps(record, sort_keys=True, indent=1,
                                    default=_json_default).encode() + b"\n",
                         fsync=self.config.fsync)
            target = base_dir / f"{index:06d}"
            _remove_tree(target)
            os.replace(tmp, target)
        finally:
            _remove_tree(tmp)
        return record

    def _next_diagnostic_index(self) -> int:
        return max([int(r.get("index", -1)) for r in self._diagnostics] + [-1]) + 1

    def add_diagnostic(self, group: FrameGroup, reason: str, *,
                       meta: Mapping[str, Any] | None = None) -> dict | None:
        """Keep one explicitly-requested diagnostic frame set (bounded).

        Production mode stores none: ``None`` is returned and nothing is
        written.  Diagnostic mode keeps the frames losslessly under
        ``diagnostics/`` until ``diagnostic_max_groups`` / ``-bytes`` is hit;
        the reason is recorded with them.  Diagnostic frames are *evidence*,
        never the calibration record.
        """
        if self.config.mode != STORAGE_DIAGNOSTIC:
            return None
        if (len(self._diagnostics) >= int(self.config.diagnostic_max_groups)
                or self._counters["diagnostic_bytes"]
                >= int(self.config.diagnostic_max_bytes)):
            return None
        index = self._next_diagnostic_index()
        payload: dict[str, Any] = {"reason": str(reason)}
        if isinstance(meta, Mapping):
            payload.update({str(k): _jsonable(v) for k, v in meta.items()})
        payload.update({"group_index": int(group.index),
                        "device_group": group.device_group,
                        "timestamp_s": float(group.timestamp_s)})
        obs_sha = _observation_sha(payload)
        record = self._write_view(
            index, dict(group.images or {}), payload, obs_sha,
            base_dir=self.diagnostics_dir, rel_prefix="diagnostics",
            frames_sha=images_sha256(dict(group.images or {})),
            view_id=index, group_index=int(group.index),
            device_group=group.device_group, timestamp_s=float(group.timestamp_s),
            source=str(group.source), complete=bool(group.complete),
            extra_record={"diagnostic": True, "reason": str(reason)})
        append_journal(self.diagnostics_journal_path, record,
                       fsync=self.config.fsync)
        self._diagnostics.append(record)
        self._refresh_counters()
        self._write_summary()
        return record

    # -- reading ---------------------------------------------------------
    def records(self) -> list[dict]:
        if not self._loaded:
            self.reload()
        return [dict(record) for record in self._views.values()]

    def get(self, index: int) -> dict | None:
        if not self._loaded:
            self.reload()
        record = self._views.get(int(index))
        return None if record is None else dict(record)

    def observation(self, index: int) -> dict:
        record = self.get(index)
        if record is None:
            raise SessionError(f"view {index} is not stored", "missing_view")
        files = record.get("files") or {}
        first = next(iter(files.values()), None)
        if isinstance(first, Mapping) and first.get("path"):
            folder = (self.paths.root / str(first["path"])).parent
        else:
            folder = self.paths.root / f"views/{int(index):06d}"
        data = read_json(folder / "observation.json", default=None)
        if not isinstance(data, Mapping):
            raise SessionError(f"view {index} has no readable observation",
                               "missing_observation")
        return dict(data)

    def iter_views(self, *, verify: bool = False) -> Iterator[dict]:
        """Yield ``{record, observation, images}`` with decoded frames.

        This is the replay path: callers get exactly the pixels and the
        refined observations that were stored, without the raw stream.
        """
        for record in self.records():
            images: dict[str, np.ndarray] = {}
            files = record.get("files") or {}
            for cam, meta in files.items():
                path = self.paths.root / str(meta.get("path"))
                data = path.read_bytes()
                if verify and hashlib.sha256(data).hexdigest() != meta.get("sha256"):
                    raise SessionError(f"{path}: stored digest mismatch",
                                       "verify_failed")
                arr = decode_frame_png(data)
                if verify and pixel_sha256(arr) != meta.get("image_sha256"):
                    raise SessionError(f"{path}: decoded pixels differ from "
                                       "the recorded observation", "verify_failed")
                images[str(cam)] = arr
            yield {"record": record, "observation": self.observation(int(record["index"])),
                   "images": images}

    # -- summary / manifest ---------------------------------------------
    def summary(self) -> dict:
        if not self._loaded:
            self.reload()
        n = len(self._views)
        sizes = self._sizes or [int(r.get("stored_bytes") or 0)
                                for r in self._views.values()]
        view_bytes = int(sum(sizes))
        mean = float(view_bytes) / n if n else 0.0
        frames = sum(len(r.get("files") or {}) for r in self._views.values())
        journal_bytes = _file_size(self.journal_path)
        summary = {
            "schema": STORAGE_SCHEMA,
            "mode": self.config.mode,
            "lossless_recording": self.config.lossless_recording,
            "codec": self.config.codec,
            "png_level": int(self.config.png_level),
            "fsync": bool(self.config.fsync),
            "created_utc": self._created_utc,
            "updated_utc": _now(),
            "views": n,
            "frames": frames,
            "bytes": {
                "view_files": view_bytes,
                "journal": journal_bytes,
                "observations": int(self._counters["observation_bytes"]),
                "diagnostics": int(self._counters["diagnostic_bytes"]),
                "total": view_bytes + journal_bytes
                + int(self._counters["observation_bytes"])
                + int(self._counters["diagnostic_bytes"]),
            },
            "bytes_per_view": {
                "mean": mean,
                "min": int(min(sizes)) if sizes else 0,
                "max": int(max(sizes)) if sizes else 0,
                "frames_per_view": (float(frames) / n if n else 0.0),
            },
            "compression": {
                "pgm_bytes_per_view": GROUP_BYTES,
                "ratio": (GROUP_BYTES / mean) if mean else 0.0,
            },
            "cadence_hz": float(self.config.cadence_hz),
            "seconds_of_stream_at_cadence": (n / float(self.config.cadence_hz)
                                             if n else 0.0),
            "diagnostics": {
                "mode_enabled": self.config.mode == STORAGE_DIAGNOSTIC,
                "groups": len(self._diagnostics),
                "frames": int(self._counters["diagnostic_frames"]),
                "bytes": int(self._counters["diagnostic_bytes"]),
                "max_groups": int(self.config.diagnostic_max_groups),
                "max_bytes": int(self.config.diagnostic_max_bytes),
            },
            "recovery": dict(self._recovery),
            "estimate": estimate_session_bytes(n, bytes_per_view=mean or None),
        }
        if self._pgm_bytes_before is not None:
            summary["migrated_from"] = {"layout": STORAGE_LEGACY,
                                        "pgm_bytes_before": self._pgm_bytes_before}
        return summary

    def _write_summary(self) -> dict:
        summary = self.summary()
        atomic_write_json(self.storage_path, summary)
        return summary

    def write_summary(self) -> dict:
        """Persist ``storage.json`` (public: call after a batch/migration)."""
        return self._write_summary()

    def _manifest_views(self) -> list[dict]:
        views = []
        for record in self.records():
            files = {cam: {"path": meta.get("path"), "sha256": meta.get("sha256"),
                           "image_sha256": meta.get("image_sha256"),
                           "bytes": int(meta.get("bytes") or 0)}
                     for cam, meta in (record.get("files") or {}).items()}
            views.append({"index": int(record["index"]),
                          "view_id": int(record.get("view_id", record["index"])),
                          "observation_sha256": record.get("observation_sha256"),
                          "stored_bytes": int(record.get("stored_bytes") or 0),
                          "files": files})
        return views

    def rebuild_manifest(self) -> dict:
        """Rebuild ``views_manifest.json`` from what is actually on disk."""
        views = self._manifest_views()
        payload = {"schema": VIEWS_MANIFEST_SCHEMA, "generated_utc": _now(),
                   "mode": self.config.mode, "n_views": len(views),
                   "views": views}
        payload["digest"] = _digest_json(views)
        atomic_write_json(self.manifest_path, payload)
        return payload

    def manifest_status(self) -> dict:
        """Compare ``views_manifest.json`` with the views actually stored."""
        recorded = read_json(self.manifest_path, default=None)
        expected_views = self._manifest_views()
        expected = _digest_json(expected_views)
        if not isinstance(recorded, Mapping):
            return {"present": False, "ok": False, "expected_digest": expected,
                    "recorded_digest": None}
        actual = recorded.get("digest")
        recorded_views = recorded.get("views")
        content_ok = (_digest_json(_jsonable(list(recorded_views or [])))
                      == _digest_json(_jsonable(expected_views)))
        return {"present": True,
                "ok": bool(actual == expected and content_ok),
                "digest_ok": bool(actual == expected),
                "content_ok": bool(content_ok),
                "expected_digest": expected, "recorded_digest": actual,
                "n_views": int(recorded.get("n_views") or 0)}

    def estimate(self, n_views: int) -> dict:
        """Estimate a session of ``n_views`` accepted views.

        Uses this store's own measured maximum when it has views (a bound,
        not a projection); otherwise the documented corpus measurement.
        """
        summary = self.summary()
        per = summary["bytes_per_view"]["max"] or None
        return estimate_session_bytes(n_views, bytes_per_view=per)

    # -- verification ----------------------------------------------------
    def verify(self, *, full: bool = True) -> dict:
        """Re-hash and re-decode every stored view; report exact damage."""
        if not self._loaded:
            self.reload()
        mismatches: list[dict] = []
        missing: list[dict] = []
        checked = 0
        checked_bytes = 0
        for index, record in self._views.items():
            for cam, meta in (record.get("files") or {}).items():
                rel = meta.get("path")
                path = self.paths.root / str(rel)
                checked += 1
                if not path.is_file():
                    missing.append({"index": index, "camera": cam,
                                    "path": str(rel)})
                    continue
                data = path.read_bytes()
                checked_bytes += len(data)
                actual = hashlib.sha256(data).hexdigest()
                if actual != meta.get("sha256"):
                    mismatches.append({"index": index, "camera": cam,
                                       "path": str(rel), "kind": "file",
                                       "expected": meta.get("sha256"),
                                       "actual": actual})
                    continue
                if full:
                    try:
                        arr = decode_frame_png(data)
                    except SessionError as exc:
                        mismatches.append({"index": index, "camera": cam,
                                           "path": str(rel), "kind": "decode",
                                           "error": str(exc)})
                        continue
                    if pixel_sha256(arr) != meta.get("image_sha256"):
                        mismatches.append({"index": index, "camera": cam,
                                           "path": str(rel), "kind": "pixels",
                                           "expected": meta.get("image_sha256"),
                                           "actual": pixel_sha256(arr)})
            obs_path = self.paths.root / f"views/{index:06d}/observation.json"
            if not obs_path.is_file():
                missing.append({"index": index, "camera": "observation",
                                "path": str(obs_path.relative_to(self.paths.root))})
            else:
                obs = read_json(obs_path, default=None)
                digest = hashlib.sha256(
                    json.dumps(obs, sort_keys=True,
                               default=_json_default).encode()).hexdigest()
                if digest != record.get("observation_sha256"):
                    mismatches.append({"index": index, "camera": "observation",
                                       "path": str(obs_path.relative_to(
                                           self.paths.root)),
                                       "kind": "observation"})
        expected = {str(meta.get("path"))
                    for record in self._views.values()
                    for meta in (record.get("files") or {}).values()}
        unexpected: list[str] = []
        if self.views_dir.is_dir():
            for path in sorted(self.views_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(self.paths.root))
                if rel.endswith("view.json") or rel.endswith("observation.json"):
                    continue
                if rel not in expected:
                    unexpected.append(rel)
        diagnostic_mismatches: list[dict] = []
        for record in self._diagnostics:
            index = int(record.get("index", -1))
            if record.get("images_sha256"):
                try:
                    current = images_sha256(
                        {cam: decode_frame_png(
                            (self.paths.root / str(meta.get("path"))).read_bytes())
                         for cam, meta in (record.get("files") or {}).items()})
                except (OSError, SessionError):
                    diagnostic_mismatches.append(
                        {"index": index, "kind": "diagnostic_unreadable"})
                else:
                    if current != record.get("images_sha256"):
                        diagnostic_mismatches.append(
                            {"index": index, "kind": "diagnostic_pixels"})
        journal = dict(self._recovery)
        journal_ok = not journal.get("journal_corrupt")
        # The journal is an index, not the data: a complete view directory is
        # recovered from views/ with its own hashes, so a torn or corrupt
        # journal record does not lose a view that reached the disk.  ``ok``
        # therefore depends on the files, and ``journal_ok`` reports that the
        # index itself needs rebuilding (rebuild_manifest / compact_session).
        mismatches.extend(diagnostic_mismatches)
        manifest = self.manifest_status()
        ok = (not mismatches and not missing)
        return {"ok": bool(ok), "checked": checked,
                "checked_bytes": int(checked_bytes),
                "n_views": len(self._views),
                "n_diagnostics": len(self._diagnostics),
                "mismatches": mismatches, "missing": missing,
                "unexpected": unexpected,
                "journal": journal, "journal_ok": bool(journal_ok),
                "views_manifest": manifest,
                "views_manifest_ok": (bool(manifest.get("ok"))
                                      if manifest.get("present") else None),
                "recovered_orphans": int(journal.get("recovered_orphans") or 0),
                "summary": self.summary()}


def _file_size(path: str | Path) -> int:
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


# -- module-level API -------------------------------------------------------

def open_store(root: str | Path | SessionPaths, *,
               mode: Any = None, **kwargs: Any) -> ViewStore:
    """Open (or create) the production store for a session directory."""
    return ViewStore.open(root, mode=mode, **kwargs)


def load_views(root: str | Path | SessionPaths, *,
               recover: bool = True) -> dict:
    """Read the production layout, recovering a truncated journal tail.

    Returns the intact journals records, the recovered orphan views, and an
    explicit statement of what had to be recovered.  A truncated tail is not
    an error: the intact prefix is the session, and the tail is reported.
    """
    store = ViewStore(_paths(root))
    if recover:
        store.reload()
    else:
        records, info = read_journal(_journal_path(root))
        store._views = {int(r.get("index", -1)): dict(r) for r in records}
        store._recovery.update({"journal_truncated": bool(info.get("truncated")),
                                "journal_corrupt": bool(info.get("corrupt")),
                                "reason": info.get("reason"),
                                "recovered_orphans": 0})
        store._refresh_counters()
        store._loaded = True
    records = store.records()
    return {"schema": STORAGE_SCHEMA, "mode": store.config.mode,
            "views": records, "n_views": len(records),
            "journal": dict(store._recovery),
            "recovered_orphans": list(
                (store._recovery or {}).get("recovered_indexes") or []),
            "complete": not store._recovery.get("journal_corrupt"),
            "storage_path": str(store.storage_path)}


def verify_views(root: str | Path | SessionPaths, *, full: bool = True) -> dict:
    """Re-hash and re-decode a session's production views."""
    store = ViewStore(_paths(root))
    store.reload()
    return store.verify(full=full)


def storage_summary(root: str | Path | SessionPaths) -> dict:
    """The live :meth:`ViewStore.summary` for a session directory."""
    store = ViewStore(_paths(root))
    store.reload()
    return store.summary()


def estimate_session_bytes(n_views: int, *,
                           bytes_per_view: int | None = None) -> dict:
    """A-priori size estimate for a production session.

    ``bytes_per_view`` should be a measured value from the session's own
    corpus when available (see :meth:`ViewStore.estimate`); otherwise the
    documented session-03 measurement is used.  The returned ``low``/``high``
    bracket is ±25 % and the ``bound`` is ``n * measured_max``; the 1 GB
    regression limit is checked against ``total``.
    """
    n = max(0, int(n_views))
    per = int(bytes_per_view if bytes_per_view is not None
              else PRODUCTION_BYTES_PER_VIEW)
    journal_per_view = 4096
    fixed = 64 * 1024
    total = n * per + n * journal_per_view + fixed
    lossless = n * LOSSLESS_BYTES_PER_VIEW_SESSION03
    return {
        "views": n,
        "bytes_per_view": per,
        "journal_bytes_per_view": journal_per_view,
        "total_bytes": int(total),
        "low_bytes": int(total * 0.75),
        "high_bytes": int(total * 1.25),
        "bound_bytes": int(n * per + fixed),
        "documented_bound_bytes": 1_000_000_000,
        "within_bound": bool(total < 1_000_000_000),
        "lossless_reference_bytes": int(lossless),
        "lossless_stream_bytes_per_second": LOSSLESS_STREAM_BYTES_PER_SECOND,
        "note": ("production: 3 lossless PNG frames + observation per accepted "
                 "view, no raw stream; lossless reference = session-03 "
                 "measurement (51.98 GB / 297 accepted views)"),
    }


def compact_session(root: str | Path | SessionPaths, *,
                    delete_pgm: bool = False,
                    mode: Any = None,
                    png_level: int = DEFAULT_PNG_LEVEL,
                    dry_run: bool = False,
                    fsync: bool = True) -> dict:
    """Migrate a legacy PGM ``groups/`` session to production storage.

    Reads ``groups/NNNNNN/camN.pgm`` (plus the manifest's accepted entries as
    the observation source), writes the compressed views, rebuilds
    ``views_manifest.json`` and ``storage.json``.  ``delete_pgm`` removes the
    PGM triplets only after every view has been written and its journal record
    fsynced.  The manifest is rewritten to point at the compressed files so
    :func:`load`/:func:`verify`/:func:`resume` stay valid afterwards.
    """
    paths = _paths(root)
    manifest = read_json(paths.manifest_json, default=None)
    accepted = list((manifest or {}).get("accepted") or [])
    if not accepted and paths.groups_dir.is_dir():
        for entry in sorted(paths.groups_dir.iterdir()):
            if entry.is_dir() and entry.name.isdigit():
                accepted.append({"index": int(entry.name),
                                 "view_id": int(entry.name)})
    store = open_store(paths, mode=mode or STORAGE_PRODUCTION,
                       png_level=png_level, fsync=fsync)
    pgm_bytes_before = 0
    written: list[dict] = []
    updated_entries: list[dict] = []
    for order, entry in enumerate(accepted):
        index = int(entry.get("index", entry.get("view_id", order)))
        view_id = int(entry.get("view_id", index))
        images: dict[str, np.ndarray] = {}
        for cam in CAMERA_KEYS:
            name = f"cam{FILE_CAM[cam]}.pgm"
            candidates = [paths.group_file(index, cam),
                          paths.groups_dir / f"{index:06d}" / name]
            found = next((p for p in candidates if p.is_file()), None)
            if found is not None:
                images[cam] = read_pgm(found)
                pgm_bytes_before += int(found.stat().st_size)
        if not images:
            continue
        observation = {k: v for k, v in entry.items()
                       if k not in ("files", "sha256")}
        if dry_run:
            stored = sum(len(encode_frame_png(img, png_level))
                         for img in images.values())
            written.append({"index": index, "stored_bytes": int(stored)})
            continue
        record = store.add_images(
            index, images, observation, view_id=view_id,
            group_index=int(entry.get("group", index)),
            device_group=entry.get("device_group"),
            timestamp_s=entry.get("timestamp"),
            source=str(entry.get("source") or ""),
            complete=bool(entry.get("complete", True)),
            extra={"plate_centre_px": entry.get("plate_centre_px")})
        written.append({"index": index, "stored_bytes": record["stored_bytes"]})
        new_entry = dict(entry)
        new_entry["files"] = {cam: {"path": meta["path"],
                                    "sha256": meta["sha256"],
                                    "bytes": meta["bytes"]}
                              for cam, meta in record["files"].items()}
        if new_entry.get("view_id") is None:
            new_entry["view_id"] = view_id
        new_entry["index"] = index
        updated_entries.append(new_entry)
    stored_bytes = int(sum(item["stored_bytes"] for item in written))
    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": store.config.mode,
        "n_views": len(written),
        "pgm_bytes_before": int(pgm_bytes_before),
        "stored_bytes": stored_bytes,
        "ratio": (pgm_bytes_before / stored_bytes) if stored_bytes else 0.0,
        "deleted_pgm": False,
    }
    if dry_run:
        return result
    store._pgm_bytes_before = int(pgm_bytes_before)
    summary = store.write_summary()
    store.rebuild_manifest()
    result["storage"] = summary
    if updated_entries:
        save_manifest(paths, updated_entries, hash_files=False)
    if delete_pgm and paths.groups_dir.is_dir():
        removed = 0
        errors: list[dict] = []
        for item in written:
            target = paths.group_dir(int(item["index"]))
            if not target.is_dir():
                continue
            try:
                shutil.rmtree(target)
                removed += 1
            except OSError as exc:
                # read-only source files are common (the recovered session is
                # deliberately 0444); report instead of pretending it worked
                errors.append({"path": str(target),
                               "error": f"{type(exc).__name__}: {exc}"})
        result["deleted_pgm"] = bool(removed)
        result["removed_group_dirs"] = removed
        result["delete_errors"] = errors
        result["delete_complete"] = not errors
    return result


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover
    """Tiny operator CLI: ``python -m tools.wizard.session_io <command> DIR``."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m tools.wizard.session_io")
    parser.add_argument("command", choices=("verify", "summary", "compact",
                                            "estimate", "manifest"))
    parser.add_argument("path", nargs="?", default="")
    parser.add_argument("--delete-pgm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--views", type=int, default=60)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "estimate":
        out = estimate_session_bytes(args.views)
        json.dump(out, sys.stdout, indent=1, default=_json_default)
        sys.stdout.write("\n")
        return 0
    if not args.path:
        parser.error(f"{args.command} needs a session directory")
    if args.command == "verify":
        out = verify_views(args.path)
    elif args.command == "summary":
        out = storage_summary(args.path)
    elif args.command == "manifest":
        store = ViewStore(_paths(args.path))
        store.reload()
        if not store.records():
            parser.error(f"{args.path}: no production views to index")
        out = store.rebuild_manifest()
    else:
        out = compact_session(args.path, delete_pgm=args.delete_pgm,
                              dry_run=args.dry_run)
    json.dump(out, sys.stdout, indent=1, default=_json_default)
    sys.stdout.write("\n")
    if args.command == "verify":
        return 0 if out.get("ok") else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
