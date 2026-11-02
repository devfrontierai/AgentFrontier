"""Incremental runs (spec docs/incremental.md): checkpoints between runs, change detection with tombstones, watermark
and delta-token operations.

A pipeline with an ``incremental`` block keeps one checkpoint per ``<tenant>/<source>/<pipeline>``. Each run compares
what it reads with the checkpoint, emits only new and changed objects, tombstones the objects that disappeared from
scopes it read completely, and commits the new checkpoint only when the run did not fail and the sinks finished.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from . import json_util as J
from .errors import AgentFrontierError
from .model import Record

CHECKPOINT_VERSION = 1
# record keys that identify an object (docs/incremental.md, "Change detection")
_IDENTITY_KEYS = ("dataset", "name", "field", "upstreamDataset", "upstreamField", "principal", "privilege", "queryId")


# -------------------------------------------------------------------------------------------------------- stores


class CheckpointStore:
    """Where checkpoints live between runs."""

    def load(self, key: str) -> dict | None:
        raise NotImplementedError

    def save(self, key: str, checkpoint: dict) -> None:
        raise NotImplementedError


def _encode_part(p: str) -> str:
    return urllib.parse.quote(p, safe="-_.~")


class FileCheckpointStore(CheckpointStore):
    """One JSON file per pipeline under a folder, written through a temporary file and a rename."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def path(self, key: str) -> Path:
        parts = [_encode_part(p) for p in key.split("/")]
        return self.folder.joinpath(*parts[:-1], parts[-1] + ".json")

    def load(self, key: str) -> dict | None:
        p = self.path(key)
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise AgentFrontierError.invalid_config("cannot read checkpoint " + str(p) + ": " + str(e)) from e
        return doc if isinstance(doc, dict) else None

    def save(self, key: str, checkpoint: dict) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(J.write(checkpoint))
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class MemoryCheckpointStore(CheckpointStore):
    """Checkpoints kept in the process, shared by name (tests, embedding)."""

    _stores: dict[str, dict[str, dict]] = {}
    _lock = threading.Lock()

    def __init__(self, name: str = "default"):
        self.name = name

    def load(self, key: str) -> dict | None:
        with MemoryCheckpointStore._lock:
            c = MemoryCheckpointStore._stores.get(self.name, {}).get(key)
            return None if c is None else J.deep_copy(c)

    def save(self, key: str, checkpoint: dict) -> None:
        with MemoryCheckpointStore._lock:
            MemoryCheckpointStore._stores.setdefault(self.name, {})[key] = J.deep_copy(checkpoint)

    @staticmethod
    def forget(name: str) -> None:
        with MemoryCheckpointStore._lock:
            MemoryCheckpointStore._stores.pop(name, None)


def store_from_config(c: Any) -> CheckpointStore:
    """``"folder"``, ``{"type": "file", "path": "folder"}`` or ``{"type": "memory", "name": "x"}``."""
    if isinstance(c, CheckpointStore):
        return c
    if isinstance(c, str) and not J.is_blank(c):
        return FileCheckpointStore(c)
    if isinstance(c, dict):
        t = J.text(c, "type", "file")
        if t == "memory":
            return MemoryCheckpointStore(J.text(c, "name", "default") or "default")
        if t == "file" and J.text(c, "path"):
            return FileCheckpointStore(J.text(c, "path"))  # type: ignore[arg-type]
    raise AgentFrontierError.invalid_config("incremental.checkpoints must be a folder or {\"type\": \"memory\"}")


# -------------------------------------------------------------------------------------------------------- run state


def scope_key(scope_map: dict) -> str:
    """``level=value`` joined with ``/`` in hierarchy order; the root scope is the empty string."""
    return "/".join(k + "=" + J.jstr(v) for k, v in scope_map.items())


def identity(body: dict, levels: list[str]) -> dict:
    """What a tombstone needs to name an object: entity, source, level values and the identifying keys."""
    out: dict = {"entity": body.get("entity"), "source": body.get("source")}
    for lv in levels:
        if body.get(lv) is not None:
            out[lv] = body[lv]
    for lv in levels:
        up = "upstream" + lv[:1].upper() + lv[1:]
        if body.get(up) is not None:
            out[up] = body[up]
    for k in _IDENTITY_KEYS:
        if body.get(k) is not None:
            out[k] = body[k]
    return out


def _greater(a: Any, b: Any) -> bool:
    """``a > b`` as numbers when both are numbers, else as text."""
    if J.is_number(a) and J.is_number(b) and not isinstance(a, bool) and not isinstance(b, bool):
        return a > b
    return J.jstr(a) > J.jstr(b)


class IncrementalRun:
    """The incremental state of one run. Thread-safe: leaf scopes may run in parallel."""

    def __init__(self, checkpoint: dict | None, full: bool, reconcile_every: int, event_entities: set[str],
                 levels: list[str]):
        cp = checkpoint if isinstance(checkpoint, dict) else {}
        self.previous_runs = J.integer(cp, "runs", 0)
        self.reconcile = checkpoint is None or full or (reconcile_every > 0 and
                                                         self.previous_runs % reconcile_every == 0)
        self.old_objects: dict[str, dict] = dict(cp.get("objects") or {}) if isinstance(cp.get("objects"), dict) else {}
        self.old_watermarks: dict[str, dict] = dict(cp.get("watermarks") or {}) \
            if isinstance(cp.get("watermarks"), dict) else {}
        self.last_reconciled = J.text(cp, "lastReconciledRunId")
        self.event_entities = set(event_entities) | {"row"}
        self.levels = list(levels)
        self._lock = threading.Lock()
        self.objects: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.deleted: set[str] = set()
        self.covered: set[tuple[str, str]] = set()
        self.watermarks: dict[str, dict] = {}
        self._wm_running: dict[str, tuple[Any, list[str]]] = {}
        self.changes = {"upserts": 0, "unchanged": 0, "deletes": 0, "tombstones": 0}

    # ---------------------------------------------------------------------------------------------- positions

    def since(self, op_name: str, skey: str) -> Any:
        """The saved position of an incremental operation, or None (reconciliation run, first run)."""
        if self.reconcile:
            return None
        w = self.old_watermarks.get(op_name + "@" + skey)
        if not isinstance(w, dict):
            return None
        return w.get("value") if "value" in w else w.get("token")

    def watermark_observed(self, key: str, field_value: Any, event_id: str) -> None:
        if field_value is None:
            return
        with self._lock:
            cur = self._wm_running.get(key)
            if cur is None or _greater(field_value, cur[0]):
                self._wm_running[key] = (field_value, [event_id])
            elif J.jstr(field_value) == J.jstr(cur[0]) and event_id not in cur[1]:
                cur[1].append(event_id)

    def in_boundary(self, key: str, event_id: str) -> bool:
        if self.reconcile:
            return False
        w = self.old_watermarks.get(key)
        return isinstance(w, dict) and event_id in (w.get("boundary") or [])

    def finish_watermark(self, key: str, completed: bool) -> None:
        """Stores the new watermark of an operation that ran; a failed one keeps the old one."""
        with self._lock:
            old = self.old_watermarks.get(key)
            if not completed:
                if isinstance(old, dict):
                    self.watermarks[key] = old
                return
            run = self._wm_running.get(key)
            old_value = old.get("value") if isinstance(old, dict) else None
            if run is None or (old_value is not None and not self.reconcile and _greater(old_value, run[0])):
                if isinstance(old, dict):
                    self.watermarks[key] = old
                return
            if old_value is not None and not self.reconcile and J.jstr(old_value) == J.jstr(run[0]):
                ids = list(dict.fromkeys(list(old.get("boundary") or []) + run[1]))  # type: ignore[union-attr]
            else:
                ids = list(run[1])
            self.watermarks[key] = {"value": run[0], "boundary": sorted(ids)}

    def finish_token(self, key: str, token: Any, completed: bool) -> None:
        with self._lock:
            old = self.old_watermarks.get(key)
            if completed and token is not None and not J.is_blank(J.jstr(token)):
                self.watermarks[key] = {"token": J.jstr(token)}
            elif isinstance(old, dict):
                self.watermarks[key] = old

    # ---------------------------------------------------------------------------------------------- objects

    def accept(self, event: Any, producer: str | None, skey: str, change: str) -> bool:
        """Whether an event goes to the sinks. ``change`` is the event's op (upsert or delete)."""
        entity = event.entity
        if entity in self.event_entities:
            return True
        key = entity + " " + J.jstr(event.envelope.get("nativeObjectId"))
        with self._lock:
            if change == "delete":
                self.seen.add(key)
                self.deleted.add(key)
                self.objects.pop(key, None)
                self.changes["deletes"] += 1
                return True
            self.seen.add(key)
            event_id = event.envelope.get("eventId")
            prev = self.objects.get(key) or self.old_objects.get(key)
            self.objects[key] = {"eventId": event_id, "producer": producer or "", "scope": skey,
                                 "identity": identity(event.record.to_json(), self.levels)}
            if prev is not None and prev.get("eventId") == event_id:
                self.changes["unchanged"] += 1
                return False
            self.changes["upserts"] += 1
            return True

    def cover(self, producer: str, skey: str) -> None:
        with self._lock:
            self.covered.add((producer, skey))

    def tombstones(self) -> list[tuple[str, dict]]:
        """(nativeObjectId, identity) of every object to tombstone, in key order; they leave the state. An object
        goes when its producer completed in its scope, or when its scope lies inside a container that goes."""
        out = []
        with self._lock:
            unseen = [k for k in sorted(self.old_objects) if k not in self.seen]
            gone_scopes = set()
            for key in unseen:
                o = self.old_objects[key]
                if key.startswith("container ") and self._covered(o):
                    ident = o.get("identity") or {}
                    gone_scopes.add(scope_key({lv: ident[lv] for lv in self.levels if lv in ident}))
            for key in unseen:
                o = self.old_objects[key]
                if self._covered(o) or _inside(J.jstr(o.get("scope")), gone_scopes):
                    out.append((key.split(" ", 1)[1], dict(o.get("identity") or {})))
                    self.changes["tombstones"] += 1
                else:
                    self.objects[key] = o  # carried over: its scope or producer did not complete
        return out

    def _covered(self, o: dict) -> bool:
        return (J.jstr(o.get("producer")), J.jstr(o.get("scope"))) in self.covered

    def checkpoint(self, run_id: str) -> dict:
        with self._lock:
            watermarks = dict(self.watermarks)
            for k, v in self.old_watermarks.items():
                watermarks.setdefault(k, v)
            return {"checkpointVersion": CHECKPOINT_VERSION, "runs": self.previous_runs + 1, "lastRunId": run_id,
                    "lastReconciledRunId": run_id if self.reconcile else self.last_reconciled,
                    "watermarks": {k: watermarks[k] for k in sorted(watermarks)},
                    "objects": {k: self.objects[k] for k in sorted(self.objects)}}


def _inside(skey: str, containers: set[str]) -> bool:
    """Whether a scope key is a container's scope or lies under it."""
    return any(skey == c or skey.startswith(c + "/") for c in containers if c)


def tombstone_record(ident: dict) -> Record:
    values = {k: v for k, v in ident.items() if k != "entity"}
    return Record(J.jstr(ident.get("entity")), values)


__all__ = ["CheckpointStore", "FileCheckpointStore", "MemoryCheckpointStore", "store_from_config", "IncrementalRun",
           "scope_key", "identity", "tombstone_record", "CHECKPOINT_VERSION"]
