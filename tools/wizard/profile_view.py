"""Read-only view of a calibration profile store, for the application UI.

The lifecycle module owns the rules (activation, rollback, the factory guard);
this module only *describes* the store so a window can show it, and is kept
separate so it can be tested without Qt.
"""

from __future__ import annotations

from pathlib import Path

from calib.profiles import ProfileStore
from tools.wizard import lifecycle

__all__ = ["list_serials", "store_summary"]


def list_serials(store_root: str | Path) -> list[str]:
    """Device serials that have a factory snapshot in this store."""
    root = Path(store_root)
    if not root.is_dir():
        return []
    out = []
    for sub in sorted(root.iterdir()):
        if (sub / "factory" / "manifest.json").is_file():
            out.append(sub.name)
    return out


def store_summary(store_root: str | Path, serial: str) -> dict:
    """Active state, revisions and factory presence for one serial.

    Every revision is verified against its own manifest here, so the caller can
    show *verified* rather than "a directory exists"; the lifecycle's
    ``active_profile`` is the authority for what is actually active.
    """
    root = Path(store_root)
    store = ProfileStore(root)
    revisions = []
    for rev in store.revisions(serial):
        try:
            verified = bool(rev.verify())
        except OSError:
            verified = False
        meta = dict(rev.meta or {})
        revisions.append({
            "index": int(rev.index),
            "verified": verified,
            "created": str(meta.get("created") or meta.get("timestamp") or ""),
            "state": str(meta.get("state") or ("validated" if verified else "broken")),
            "source": str(meta.get("candidate") or meta.get("source") or ""),
        })
    active = lifecycle.active_profile(root, serial)
    return {
        "store": str(root),
        "serial": serial,
        "factory_present": (root / serial / "factory" / "manifest.json").is_file(),
        "active": {
            "state": str(active.get("state") or "unknown"),
            "revision": active.get("revision"),
            "integrity_ok": bool(active.get("integrity_ok", True)),
            "reason": str(active.get("reason") or ""),
        },
        "revisions": revisions,
    }
