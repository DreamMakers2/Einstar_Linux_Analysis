"""Bounded rolling buffer of recent synchronised camera triplets.

Why this exists
---------------
The live acquisition path offers one three-camera group every ~102 ms
(9.8 groups/s measured on session-03; 9.96 groups/s on a 120 s soak).  A group
is three 1280x1024 8-bit frames, i.e. **3.93 MB** uncompressed, so the stream
runs at ~38.6 MB/s and an unbounded history is exactly the 52 GB mistake this
work removes.

Short-horizon decisions - motion blur, exposure settling, stationarity/dwell,
"did the operator actually stop moving?" - need *recent* frames only.  This
module keeps the last N bytes of triplets in RAM, evicting oldest first, with
O(1) accounting and without copying pixels on eviction.  Accepted views are
persisted separately (``tools.wizard.session_io`` production storage); the
buffer is never the record.

Accounting contract
-------------------
* ``payload_bytes`` is the exact sum of ``ndarray.nbytes`` of the frames the
  buffer holds.  A non-contiguous frame is made contiguous on push so the
  number describes the bytes actually retained (a view over a larger array
  would otherwise be under-counted).
* ``nbytes`` is ``payload_bytes`` plus a fixed per-entry wrapper overhead
  (:data:`ENTRY_OVERHEAD_BYTES`); **that** is what the byte budget bounds.
* ``seconds`` converts the retained groups to wall-clock time at a measured
  cadence (default :data:`DEFAULT_CADENCE_HZ`, session-03's 9.807 fps rounded
  down to 9.8).  It is a diagnostic conversion, not a promise about the live
  stream.

The default budget is 256 MiB: 65 groups, 195 frames, 6.6 s at 9.8 Hz.  That
is enough for stationarity/dwell windows and motion history while staying two
orders of magnitude below the recording it replaces.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Any, Iterable, Iterator, Mapping

import numpy as np

from .types import CAMERA_KEYS, IMAGE_HEIGHT, IMAGE_WIDTH

#: One complete three-camera group of 1280x1024 8-bit frames, in bytes.
GROUP_BYTES = len(CAMERA_KEYS) * int(IMAGE_WIDTH) * int(IMAGE_HEIGHT)

#: Default RAM budget for recent triplets (256 MiB).
DEFAULT_CAPACITY_BYTES = 256 * 1024 * 1024

#: Measured live cadence (session-03: 13,195 groups / 1,345.59 s = 9.807 fps).
DEFAULT_CADENCE_HZ = 9.8

#: Fixed accounting overhead per retained entry (dataclass, dict, key strings).
ENTRY_OVERHEAD_BYTES = 256


@dataclass(frozen=True)
class RingEntry:
    """One retained camera triplet.

    ``images`` holds *references* to the source arrays.  The buffer never
    copies a frame on push (unless ``copy_on_push`` was requested) or on
    eviction; dropping the entry drops the last reference the buffer owns.
    """

    seq: int
    key: int | None
    timestamp_s: float | None
    images: Mapping[str, np.ndarray]
    payload_bytes: int

    @property
    def nbytes(self) -> int:
        """Accounted bytes: payload plus the per-entry wrapper overhead."""
        return int(self.payload_bytes) + ENTRY_OVERHEAD_BYTES

    @property
    def n_frames(self) -> int:
        return len(self.images)

    @property
    def cameras(self) -> tuple[str, ...]:
        return tuple(self.images)


def frames_bytes(images: Mapping[str, Any]) -> int:
    """Exact payload bytes of a (camera -> array) mapping."""
    total = 0
    for value in images.values():
        total += int(np.asarray(value).nbytes)
    return total


class BoundedRingBuffer:
    """A first-in-first-out buffer bounded by an exact byte budget.

    Thread-safe: the live reader thread pushes while the decision thread reads
    a snapshot.  ``snapshot()`` returns immutable entry references, so a
    consumer can walk it without holding the lock.
    """

    def __init__(self, capacity_bytes: int = DEFAULT_CAPACITY_BYTES, *,
                 copy_on_push: bool = False,
                 cadence_hz: float = DEFAULT_CADENCE_HZ,
                 name: str = "ring") -> None:
        capacity = int(capacity_bytes)
        if capacity <= 0:
            raise ValueError("capacity_bytes must be > 0")
        if float(cadence_hz) <= 0:
            raise ValueError("cadence_hz must be > 0")
        self._capacity = capacity
        self._copy = bool(copy_on_push)
        self._cadence = float(cadence_hz)
        self.name = str(name)
        self._entries: deque[RingEntry] = deque()
        self._seq = 0
        self._payload = 0
        self._nbytes = 0
        self._frames = 0
        self._lock = threading.RLock()
        self._evicted_groups = 0
        self._evicted_bytes = 0
        self._pushed_groups = 0
        self._pushed_bytes = 0
        self._oversize_dropped = 0
        self._peak_nbytes = 0

    # -- properties ------------------------------------------------------
    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @property
    def cadence_hz(self) -> float:
        return self._cadence

    @property
    def nbytes(self) -> int:
        """Accounted bytes currently held (payload + wrapper overhead)."""
        return self._nbytes

    @property
    def payload_bytes(self) -> int:
        return self._payload

    @property
    def n_frames(self) -> int:
        return self._frames

    @property
    def n_groups(self) -> int:
        return len(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __contains__(self, key: object) -> bool:
        return any(entry.key == key for entry in self._entries)

    def __iter__(self) -> Iterator[RingEntry]:
        return iter(self.snapshot())

    # -- writes ----------------------------------------------------------
    def push(self, images: Mapping[str, Any], *, key: int | None = None,
             timestamp_s: float | None = None) -> tuple[RingEntry, ...]:
        """Retain one triplet; return the entries evicted to make room.

        An entry larger than the whole budget is refused (it is never
        retained) and counted in :meth:`stats` as ``oversize_dropped``; the
        buffer keeps its previous contents.  This is the adversarial-size
        contract: the buffer can not be made to exceed its budget by one huge
        frame.
        """
        prepared: dict[str, np.ndarray] = {}
        payload = 0
        for cam, value in (images or {}).items():
            arr = np.asarray(value)
            if self._copy:
                arr = np.array(arr, copy=True, order="C")
            elif not arr.flags.c_contiguous:
                # a view over a bigger buffer would be under-counted; the
                # contiguous copy is what the budget then describes
                arr = np.ascontiguousarray(arr)
            prepared[str(cam)] = arr
            payload += int(arr.nbytes)
        if not prepared:
            raise ValueError("a ring entry needs at least one frame")
        entry_nbytes = payload + ENTRY_OVERHEAD_BYTES
        with self._lock:
            if entry_nbytes > self._capacity:
                self._oversize_dropped += 1
                return ()
            evicted: list[RingEntry] = []
            while self._entries and self._nbytes + entry_nbytes > self._capacity:
                evicted.append(self._entries.popleft())
                self._nbytes -= evicted[-1].nbytes
                self._payload -= evicted[-1].payload_bytes
                self._frames -= evicted[-1].n_frames
                self._evicted_groups += 1
                self._evicted_bytes += evicted[-1].nbytes
            entry = RingEntry(seq=self._seq, key=key,
                              timestamp_s=(None if timestamp_s is None
                                           else float(timestamp_s)),
                              images=prepared, payload_bytes=payload)
            self._seq += 1
            self._entries.append(entry)
            self._nbytes += entry_nbytes
            self._payload += payload
            self._frames += len(prepared)
            self._pushed_groups += 1
            self._pushed_bytes += payload
            if self._nbytes > self._peak_nbytes:
                self._peak_nbytes = self._nbytes
            return tuple(evicted)

    def push_group(self, group: Any) -> tuple[RingEntry, ...]:
        """Retain a ``types.FrameGroup``'s oriented images."""
        return self.push(getattr(group, "images", {}) or {},
                         key=getattr(group, "index", None),
                         timestamp_s=getattr(group, "timestamp_s", None))

    # -- reads -----------------------------------------------------------
    def snapshot(self) -> tuple[RingEntry, ...]:
        """Oldest-to-newest immutable view of the retained entries."""
        with self._lock:
            return tuple(self._entries)

    def newest(self, count: int = 1) -> tuple[RingEntry, ...]:
        """The ``count`` newest entries, oldest first."""
        n = int(count)
        if n <= 0:
            return ()
        with self._lock:
            total = len(self._entries)
            start = max(0, total - n)
            return tuple(self._entries[i] for i in range(start, total))

    def oldest(self, count: int = 1) -> tuple[RingEntry, ...]:
        """The ``count`` oldest entries, oldest first."""
        n = int(count)
        if n <= 0:
            return ()
        with self._lock:
            return tuple(self._entries[i] for i in range(min(n, len(self._entries))))

    def find(self, key: object) -> RingEntry | None:
        with self._lock:
            for entry in self._entries:
                if entry.key == key:
                    return entry
        return None

    def keys(self) -> tuple[int | None, ...]:
        with self._lock:
            return tuple(entry.key for entry in self._entries)

    def clear(self) -> int:
        """Drop every entry; return how many were discarded."""
        with self._lock:
            dropped = len(self._entries)
            self._entries.clear()
            self._payload = 0
            self._nbytes = 0
            self._frames = 0
            return dropped

    # -- derived ---------------------------------------------------------
    def seconds(self, cadence_hz: float | None = None) -> float:
        """Retained history in seconds at a measured (default 9.8 Hz) cadence."""
        hz = self._cadence if cadence_hz is None else float(cadence_hz)
        if hz <= 0:
            raise ValueError("cadence_hz must be > 0")
        return len(self._entries) / hz

    def capacity_seconds(self, cadence_hz: float | None = None,
                         group_bytes: int = GROUP_BYTES) -> float:
        """Upper bound on history for equal-sized *full* groups at that cadence.

        A budget smaller than one group honestly reports 0.0 s of full-group
        capacity; :meth:`seconds` reports what is actually retained.
        """
        hz = self._cadence if cadence_hz is None else float(cadence_hz)
        if hz <= 0:
            raise ValueError("cadence_hz must be > 0")
        group = int(group_bytes) + ENTRY_OVERHEAD_BYTES
        return (self._capacity // max(group, 1)) / hz

    def stats(self) -> dict:
        """Exact accounting plus eviction/oversize counters (JSON-safe)."""
        with self._lock:
            return {
                "name": self.name,
                "capacity_bytes": self._capacity,
                "capacity_groups": self._capacity // (GROUP_BYTES
                                                      + ENTRY_OVERHEAD_BYTES),
                "capacity_seconds": self.capacity_seconds(),
                "cadence_hz": self._cadence,
                "groups": len(self._entries),
                "frames": self._frames,
                "payload_bytes": self._payload,
                "nbytes": self._nbytes,
                "seconds": self.seconds(),
                "peak_nbytes": self._peak_nbytes,
                "utilization": (self._nbytes / self._capacity
                                if self._capacity else 0.0),
                "pushed_groups": self._pushed_groups,
                "pushed_bytes": self._pushed_bytes,
                "evicted_groups": self._evicted_groups,
                "evicted_bytes": self._evicted_bytes,
                "oversize_dropped": self._oversize_dropped,
                "consistent": self._nbytes == sum(e.nbytes for e in self._entries),
            }


def capacity_for_seconds(seconds: float,
                         cadence_hz: float = DEFAULT_CADENCE_HZ,
                         group_bytes: int = GROUP_BYTES) -> int:
    """Budget bytes for a requested history length (for callers that think in
    seconds rather than bytes).  Always at least one group."""
    if float(seconds) <= 0:
        raise ValueError("seconds must be > 0")
    groups = max(1, int(np.ceil(float(seconds) * float(cadence_hz))))
    return groups * (int(group_bytes) + ENTRY_OVERHEAD_BYTES)


def total_bytes(entries: Iterable[RingEntry]) -> int:
    """Exact accounted bytes of an entry sequence (e.g. a snapshot)."""
    return sum(entry.nbytes for entry in entries)
