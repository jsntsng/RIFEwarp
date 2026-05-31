"""
Snapshot — a named, persisted curve variant within a project.

A project holds a SnapshotCollection of one or more snapshots; exactly one is
"active" at a time, and the active snapshot's curve and in/out points are what
the UI reads from and writes to (Model A — no separate working curve).

This module is Qt-free by design (see CLAUDE.md guardrails).
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from core.timewarp import TimewarpCurve


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Snapshot:
    """One named curve variant. `id` is identity; `name` is for display only and
    may duplicate other snapshots' names."""
    curve: TimewarpCurve
    in_point: int
    out_point: int
    name: str = "Snapshot 1"
    id: str = field(default_factory=_new_id)
    # Free-form one-liner shown in the panel below the snapshot list.
    notes: str = ""
    # Palette key (e.g. "red", "blue"); None = no swatch. Stored as a key, not
    # a raw hex, so palette retheming doesn't break existing project files.
    color: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "id":        self.id,
            "name":      self.name,
            "curve":     self.curve.to_dict(),
            "in_point":  int(self.in_point),
            "out_point": int(self.out_point),
        }
        # Only emit non-default optional fields so older files round-trip
        # byte-identical when these features aren't used.
        if self.notes:
            d["notes"] = self.notes
        if self.color:
            d["color"] = self.color
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        return cls(
            id=d.get("id") or _new_id(),
            name=d.get("name", "Snapshot"),
            curve=TimewarpCurve.from_dict(d["curve"]),
            in_point=int(d.get("in_point", 0)),
            out_point=int(d.get("out_point", 0)),
            notes=d.get("notes", ""),
            color=d.get("color"),
        )


class SnapshotCollection:
    """Ordered list of snapshots with one active. Always holds at least one."""

    def __init__(self, snapshots: Optional[List[Snapshot]] = None,
                 active_id: Optional[str] = None):
        self.snapshots: List[Snapshot] = list(snapshots) if snapshots else []
        self._active_id: Optional[str] = None
        if self.snapshots:
            ids = {s.id for s in self.snapshots}
            self._active_id = active_id if active_id in ids else self.snapshots[0].id

    # ── Invariants ────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.snapshots)

    def _index_of(self, snap_id: str) -> int:
        for i, s in enumerate(self.snapshots):
            if s.id == snap_id:
                return i
        raise KeyError(f"snapshot id not found: {snap_id}")

    def get(self, snap_id: str) -> Snapshot:
        return self.snapshots[self._index_of(snap_id)]

    def active(self) -> Snapshot:
        if not self.snapshots:
            raise RuntimeError("SnapshotCollection is empty")
        return self.get(self._active_id) if self._active_id else self.snapshots[0]

    @property
    def active_id(self) -> str:
        return self._active_id or (self.snapshots[0].id if self.snapshots else "")

    def set_active(self, snap_id: str) -> Snapshot:
        idx = self._index_of(snap_id)
        self._active_id = self.snapshots[idx].id
        return self.snapshots[idx]

    # ── Mutators ──────────────────────────────────────────────────────────────

    def add(self, snap: Snapshot, make_active: bool = True) -> Snapshot:
        self.snapshots.append(snap)
        if make_active or self._active_id is None:
            self._active_id = snap.id
        return snap

    def insert_after(self, snap_id: str, snap: Snapshot,
                     make_active: bool = True) -> Snapshot:
        idx = self._index_of(snap_id)
        self.snapshots.insert(idx + 1, snap)
        if make_active or self._active_id is None:
            self._active_id = snap.id
        return snap

    def remove(self, snap_id: str) -> None:
        if len(self.snapshots) <= 1:
            raise ValueError("Cannot remove the last snapshot")
        idx = self._index_of(snap_id)
        was_active = (self.snapshots[idx].id == self._active_id)
        self.snapshots.pop(idx)
        if was_active:
            # Pick next-in-order; if removed was the last, fall back to previous.
            new_idx = idx if idx < len(self.snapshots) else len(self.snapshots) - 1
            self._active_id = self.snapshots[new_idx].id

    def rename(self, snap_id: str, name: str) -> Snapshot:
        snap = self.get(snap_id)
        snap.name = name
        return snap

    def duplicate(self, snap_id: str, make_active: bool = True) -> Snapshot:
        """Clone the snapshot (deep copy of curve) with a new id and a `" copy"`
        suffix. Inserted directly after the source row. Notes and color carry
        over so a duplicate is a faithful clone, not a fresh blank."""
        src = self.get(snap_id)
        new = Snapshot(
            curve=copy.deepcopy(src.curve),
            in_point=src.in_point,
            out_point=src.out_point,
            name=self._dup_name(src.name),
            notes=src.notes,
            color=src.color,
        )
        return self.insert_after(snap_id, new, make_active=make_active)

    def move(self, snap_id: str, new_index: int) -> None:
        """Reorder snap_id to new_index (clamped). Active id is unchanged."""
        idx = self._index_of(snap_id)
        snap = self.snapshots.pop(idx)
        n = len(self.snapshots)
        new_index = max(0, min(n, int(new_index)))
        self.snapshots.insert(new_index, snap)

    def cycle(self, direction: int) -> Snapshot:
        """Advance active by +1 or -1 (wraps). Returns the new active snapshot."""
        if not self.snapshots:
            raise RuntimeError("SnapshotCollection is empty")
        if direction not in (-1, 1):
            raise ValueError("direction must be +1 or -1")
        idx = self._index_of(self._active_id) if self._active_id else 0
        new_idx = (idx + direction) % len(self.snapshots)
        self._active_id = self.snapshots[new_idx].id
        return self.snapshots[new_idx]

    # ── Naming helpers ────────────────────────────────────────────────────────

    def _names(self) -> set:
        return {s.name for s in self.snapshots}

    def default_new_name(self) -> str:
        """Smallest positive integer N such that "Snapshot N" is unused."""
        existing = self._names()
        n = 1
        while f"Snapshot {n}" in existing:
            n += 1
        return f"Snapshot {n}"

    def _dup_name(self, source_name: str) -> str:
        """`"<src> copy"`; if that exists, `"<src> copy 2"`, etc."""
        existing = self._names()
        base = f"{source_name} copy"
        if base not in existing:
            return base
        n = 2
        while f"{base} {n}" in existing:
            n += 1
        return f"{base} {n}"

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "snapshots":          [s.to_dict() for s in self.snapshots],
            "active_snapshot_id": self.active_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotCollection":
        snaps = [Snapshot.from_dict(sd) for sd in d.get("snapshots", [])]
        return cls(snapshots=snaps, active_id=d.get("active_snapshot_id"))
