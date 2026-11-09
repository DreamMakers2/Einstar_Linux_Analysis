"""Versioned, reversible host-side calibration profiles.

EXStar overwrites ``res/Einscan-E10/200x150/*CCF.txt`` in place and mirrors the
same bytes into the scanner's 6568-byte device record; it keeps **no backup**
(recovered in ``analysis/calibration/re/storage_report.md``).  This module
implements the safe alternative:

    <root>/<serial>/factory/          immutable snapshot of the factory files
    <root>/<serial>/revisions/0001/   one complete, self-describing revision
    <root>/<serial>/active            pointer (text file)

Every revision is a directory, never a partial overwrite.  Loading a revision
verifies its SHA-256 manifest.  Nothing here writes to the device: the
``ExportPlan`` describes the exact bytes and offset so a future writer can do
it explicitly, after the user has validated the profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Optional

import numpy as np

from .ccf import CCFRecord, decode_record, encode_record, load as load_ccf


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


@dataclass
class Revision:
    root: Path
    index: int
    files: dict[str, str]           # name -> sha256
    meta: dict = field(default_factory=dict)

    @property
    def ccf_dir(self) -> Path:
        return self.root / "ccf"

    def record(self, camera: str) -> CCFRecord:
        return load_ccf(self.ccf_dir / f"{camera}.CCF")

    def verify(self) -> bool:
        for name, digest in self.files.items():
            p = self.root / name
            if not p.exists() or sha256_file(p) != digest:
                return False
        return True


class ProfileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def device_dir(self, serial: str) -> Path:
        return self.root / serial

    # -- factory ---------------------------------------------------------
    def snapshot_factory(self, serial: str, ccf_files: dict[str, Path],
                         device_dump: Optional[Path] = None) -> Path:
        """Copy the factory files once; never overwrite an existing snapshot."""
        d = self.device_dir(serial) / "factory"
        d.mkdir(parents=True, exist_ok=True)
        man = d / "manifest.json"
        if man.exists():
            raise FileExistsError(f"factory snapshot already exists: {d}")
        files = {}
        for name, src in ccf_files.items():
            dst = d / f"{name}.CCF"
            shutil.copyfile(src, dst)
            files[f"{name}.CCF"] = sha256_file(dst)
        if device_dump is not None:
            dst = d / "device_calib.bin"
            shutil.copyfile(device_dump, dst)
            files["device_calib.bin"] = sha256_file(dst)
        man.write_text(json.dumps({"created_utc": _now(), "files": files},
                                  indent=2, sort_keys=True))
        return d

    # -- revisions -------------------------------------------------------
    def revisions(self, serial: str) -> list[Revision]:
        d = self.device_dir(serial) / "revisions"
        out = []
        if not d.exists():
            return out
        for sub in sorted(d.iterdir()):
            if not sub.is_dir():
                continue
            man = sub / "manifest.json"
            if not man.exists():
                continue
            data = json.loads(man.read_text())
            out.append(Revision(sub, int(data.get("index", -1)),
                                data.get("files", {}), data.get("meta", {})))
        return out

    def save_revision(self, serial: str, records: dict[str, CCFRecord],
                      meta: Optional[dict] = None,
                      covariance: Optional[np.ndarray] = None,
                      param_names: Optional[list[str]] = None) -> Revision:
        d = self.device_dir(serial) / "revisions"
        d.mkdir(parents=True, exist_ok=True)
        existing = [r.index for r in self.revisions(serial)]
        idx = (max(existing) + 1) if existing else 1
        rev = d / f"{idx:04d}"
        rev.mkdir()
        ccf = rev / "ccf"
        ccf.mkdir()
        files = {}
        for name, rec in records.items():
            raw = encode_record(rec.values, ir=rec.ir,
                                trailer=rec.raw[264:] if rec.ir else None,
                                key=rec.key)
            p = ccf / f"{name}.CCF"
            p.write_bytes(raw)
            files[f"ccf/{name}.CCF"] = sha256_bytes(raw)
        meta = dict(meta or {})
        meta.setdefault("created_utc", _now())
        meta["index"] = idx
        (rev / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True,
                                                  default=_json_default))
        if covariance is not None:
            np.savez_compressed(rev / "covariance.npz", cov=covariance,
                                names=np.array(param_names or [], dtype=object))
            files["covariance.npz"] = sha256_file(rev / "covariance.npz")
        (rev / "manifest.json").write_text(json.dumps(
            {"index": idx, "files": files, "meta": meta}, indent=2, sort_keys=True,
            default=_json_default))
        self.activate(serial, idx)
        return Revision(rev, idx, files, meta)

    def activate(self, serial: str, index: int) -> None:
        p = self.device_dir(serial) / "active"
        p.write_text(f"{index:04d}\n")

    def active(self, serial: str) -> Optional[Revision]:
        p = self.device_dir(serial) / "active"
        if not p.exists():
            return None
        idx = int(p.read_text().strip())
        for r in self.revisions(serial):
            if r.index == idx:
                return r
        return None

    def rollback(self, serial: str) -> Optional[Revision]:
        revs = self.revisions(serial)
        if len(revs) < 2:
            return None
        cur = self.active(serial)
        prev = [r for r in revs if cur is None or r.index < cur.index]
        if not prev:
            return None
        self.activate(serial, prev[-1].index)
        return prev[-1]

    # -- device export plan ---------------------------------------------
    def export_plan(self, rev: Revision, serial: str) -> dict:
        """Describe the exact device write EXStar would perform.

        The block layout was recovered byte-for-byte (storage_report.md §6.2):
        a 3873-byte ``0xF21`` block at ``tagDevData + 0x39B`` containing the
        three CCF records, their lengths, the block timestamp and the
        ``FQFQ\\0`` magic.
        """
        left = (rev.ccf_dir / "left.CCF").read_bytes()
        right = (rev.ccf_dir / "right.CCF").read_bytes()
        tex = (rev.ccf_dir / "tex.CCF").read_bytes()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M").encode()
        block = bytearray(0xF21)
        block[0:4] = (4).to_bytes(4, "little")
        block[0x108:0x10C] = len(left).to_bytes(4, "little")
        block[0x10C:0x10C + len(left)] = left
        block[0x610:0x614] = len(right).to_bytes(4, "little")
        block[0x614:0x614 + len(right)] = right
        block[0xA14:0xA14 + len(stamp)] = stamp
        block[0xB18:0xB1C] = len(tex).to_bytes(4, "little")
        block[0xB1C:0xB1C + len(tex)] = tex
        block[0xF1C:0xF21] = b"FQFQ\0"
        return {"serial": serial, "offset": 0x39B, "size": 0xF21,
                "block_sha256": sha256_bytes(bytes(block)),
                "records": {"left": sha256_bytes(left),
                            "right": sha256_bytes(right),
                            "tex": sha256_bytes(tex)},
                "note": "not written automatically; validate first"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))
