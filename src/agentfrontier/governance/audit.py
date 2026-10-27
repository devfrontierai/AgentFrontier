"""Audit events and the per-run hash chain (spec docs/audit.md).

::

    log = AuditLog(run_id="r1", source="retail", key=b"secret")
    log.record("read", actor="alice", resource="retail/operation=fields", resource_type="operation",
               decision="allow", outcome="success", records=12)
    log.manifest()                       # -> {"chain": "af-audit/1", "events": 1, ...}
    verify_directory("out")              # after a pipeline with auditing on
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import json_util as J
from ..model import Record

CHAIN = "af-audit/1"
MANIFEST = "af-audit-manifest/1"


def _now() -> str:
    t = _dt.datetime.now(_dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def chain_hash(event: dict) -> str:
    """``hash`` of an audit event: over its JSON form without ``hash`` and without null values."""
    body = {k: v for k, v in event.items() if k != "hash" and v is not None}
    return "sha256:" + hashlib.sha256(J.canonical([CHAIN, body]).encode("utf-8")).hexdigest()


def sign(key: bytes, run_id: str, events: int, last_hash: str | None) -> str:
    msg = J.canonical([MANIFEST, run_id, events, last_hash]).encode("utf-8")
    return "hmac-sha256:" + hmac.new(key, msg, hashlib.sha256).hexdigest()


class AuditLog:
    """Hash-chained ``audit_event`` records for one run (or one session). Thread-safe. ``sink``: called with each
    new record as it is created (a pipeline passes its emitter)."""

    def __init__(self, run_id: str, source: str | None = None, key: bytes | str | None = None,
                 sink: Any = None):
        self.run_id = run_id
        self.source = source
        self.key = key.encode("utf-8") if isinstance(key, str) else key
        self.sink = sink
        self._events: list[Record] = []
        self._last: str | None = None
        self._first: str | None = None
        self._lock = threading.Lock()

    def record(self, action: str, actor: str | None = None, actor_type: str | None = None,
               resource: str | None = None, resource_type: str | None = None, purpose: str | None = None,
               decision: str | None = None, policy_version: str | None = None, outcome: str | None = None,
               records: int | None = None, reason: str | None = None, occurred_at: str | None = None) -> Record:
        with self._lock:
            seq = len(self._events) + 1
            v = {"source": self.source, "actor": actor, "actorType": actor_type, "action": action,
                 "resource": resource, "resourceType": resource_type, "purpose": purpose, "decision": decision,
                 "policyVersion": policy_version, "outcome": outcome, "correlationId": self.run_id,
                 "occurredAt": occurred_at or _now(), "records": records, "reason": reason, "sequence": seq,
                 "previousHash": self._last}
            v = {k: x for k, x in v.items() if x is not None}
            r = Record("audit_event", v)
            h = chain_hash(r.to_json())
            r.values["hash"] = h
            self._events.append(r)
            self._last = h
            if self._first is None:
                self._first = h
            sink = self.sink
        if sink is not None:
            sink([r])
        return r

    @property
    def events(self) -> list[Record]:
        with self._lock:
            return list(self._events)

    def manifest(self) -> dict:
        with self._lock:
            m: dict = {"chain": CHAIN, "events": len(self._events), "firstHash": self._first,
                       "lastHash": self._last}
        if self.key is not None:
            m["signature"] = sign(self.key, self.run_id, m["events"], m["lastHash"])
        return m


@dataclass
class Verification:
    ok: bool
    events: int
    problems: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"ok": self.ok, "events": self.events, "problems": list(self.problems)}


def verify(events: Iterable[dict | Record], manifest: dict | None = None, key: bytes | str | None = None,
           run_id: str | None = None) -> Verification:
    """Checks a run's audit events (plain records or raw-stream events) against each other and the manifest."""
    items = []
    for e in events:
        v = e.to_json() if isinstance(e, Record) else dict(e)
        if isinstance(v.get("body"), dict) and "envelopeVersion" in v:
            v = dict(v["body"])
        items.append(v)
    problems: list[str] = []
    items.sort(key=lambda x: x.get("sequence") if isinstance(x.get("sequence"), int) else -1)
    prev = None
    for i, v in enumerate(items, 1):
        if v.get("sequence") != i:
            problems.append("expected sequence " + str(i) + ", found " + J.jstr(v.get("sequence")))
            break
        if v.get("previousHash") != prev:
            problems.append("event " + str(i) + ": previousHash does not match event " + str(i - 1))
        h = chain_hash(v)
        if v.get("hash") != h:
            problems.append("event " + str(i) + ": hash does not match its content")
        prev = v.get("hash")
    if manifest is not None:
        if manifest.get("chain") != CHAIN:
            problems.append("unknown chain '" + J.jstr(manifest.get("chain")) + "'")
        if manifest.get("events") != len(items):
            problems.append("manifest lists " + J.jstr(manifest.get("events")) + " events, found "
                            + str(len(items)))
        if items and manifest.get("firstHash") != items[0].get("hash"):
            problems.append("firstHash does not match the first event")
        if manifest.get("lastHash") != (items[-1].get("hash") if items else None):
            problems.append("lastHash does not match the last event")
        sig = manifest.get("signature")
        if key is not None:
            k = key.encode("utf-8") if isinstance(key, str) else key
            rid = run_id if run_id is not None else (items[0].get("correlationId") if items else None)
            if sig is None:
                problems.append("manifest is not signed")
            elif not hmac.compare_digest(J.jstr(sig), sign(k, J.jstr(rid), len(items), manifest.get("lastHash"))):
                problems.append("manifest signature does not match")
    return Verification(not problems, len(items), problems)


def verify_directory(directory: str | Path, key: bytes | str | None = None) -> Verification:
    """Verifies the ``audit_event.jsonl`` and ``run.json`` a jsonl sink wrote."""
    d = Path(directory)
    events = []
    p = d / "audit_event.jsonl"
    if p.is_file():
        events = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    run = d / "run.json"
    manifest = None
    run_id = None
    if run.is_file():
        doc = json.loads(run.read_text(encoding="utf-8"))
        report = doc.get("report") or {}
        manifest = report.get("audit")
        run_id = doc.get("runId")
    v = verify(events, manifest, key, run_id)
    if manifest is None:
        v.problems.append("run.json has no audit manifest (report.audit)")
        v.ok = False
    return v
