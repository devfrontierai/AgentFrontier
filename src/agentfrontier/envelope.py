"""Raw-stream envelope: wraps a canonical record in an event that says where it came from and what it is.

The rules (property order, ``nativeObjectId`` per entity, ``eventId``) are in the spec's ``docs/envelope.md`` and
are shared with every implementation; ``conformance/event-ids.json`` holds the test vectors.
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import json_util as J
from .errors import AgentFrontierError
from .model import Record
from .sinks import uids
from .spec_model import EntityModel, StreamCatalog

ENVELOPE_VERSION = "1"
EVENT_ID_PREFIX = "af-event/1"
DEFAULT_TENANT = "default"

# entities whose id is the graph uid of sinks/uids.py
_UID_ENTITIES = {"source", "container", "dataset", "field", "index", "constraint", "routine", "policy", "principal"}


class Event(Record):
    """A record wrapped in a raw-stream envelope. ``entity`` and ``values`` are the record's, so code that reads
    records (graph sinks, filters) works unchanged; ``to_json()`` returns the whole event with the record as
    ``body``."""

    def __init__(self, record: Record, envelope: dict):
        super().__init__(record.entity, record.values)
        self.record = record
        self.envelope = envelope

    def to_json(self) -> dict:
        out = dict(self.envelope)
        out["body"] = self.record.to_json()
        return out

    def __repr__(self) -> str:
        return "Event(" + J.jstr(self.envelope.get("eventId")) + ", " + self.entity + ")"


def _h16(v: Any) -> str:
    return hashlib.sha256(J.canonical(v).encode("utf-8")).hexdigest()[:16]


def native_object_id(r: Record, levels: list[str]) -> str:
    """Stable id of the record's object inside its source (docs/envelope.md, "nativeObjectId")."""
    e = r.entity
    if e in _UID_ENTITIES:
        return J.jstr(uids.row(r, levels)["uid"])
    source = r.string("source")
    path = uids.path_of(source, r, levels, "")
    dataset = r.string("dataset")
    dataset_uid = None if dataset is None else path + "/" + dataset
    field = r.string("field")
    if dataset_uid is not None and field is not None:
        target = dataset_uid + "/" + field
    else:
        target = dataset_uid if dataset_uid is not None else path
    if e == "lineage":
        down = J.jstr(dataset_uid) + ("" if field is None else "/" + field)
        up = uids.path_of(source, r, levels, "upstream") + "/" + J.jstr(r.string("upstreamDataset"))
        up_field = r.string("upstreamField")
        return down + "<-" + up + ("" if up_field is None else "/" + up_field)
    if e == "grant":
        return J.jstr(source) + "/principal/" + J.jstr(r.string("principal")) + "->" + target + "#grant/" \
            + J.jstr(r.string("privilege"))
    if e == "tag":
        return target + "#tag/" + J.jstr(r.string("name"))
    if e == "statistic":
        return target + "#statistic"
    if e == "row":
        return J.jstr(dataset_uid) + "#row/" + _h16(r.values.get("values"))
    if e == "usage":
        query_id = r.string("queryId")
        return J.jstr(source) + "/usage/" + (query_id if query_id is not None else _h16(r.to_json()))
    name = r.string("name")
    return path + "#" + e + "/" + (name if name is not None else _h16(r.to_json()))


def event_id(tenant_id: str, source_system_id: str, entity: str, object_id: str, op: str, body: Any) -> str:
    """``sha256:`` + hex SHA-256 of the canonical JSON of the identifying values (docs/envelope.md, "eventId")."""
    key = J.canonical([EVENT_ID_PREFIX, tenant_id, source_system_id, entity, object_id, op, body])
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


class EnvelopeBuilder:
    """Wraps the records of one run. Thread-safe (holds no mutable state)."""

    def __init__(self, entity_model: EntityModel, streams: StreamCatalog, tenant_id: str | None,
                 source_system_id: str, connector_id: str, run_id: str, levels: list[str]):
        self.entity_model = entity_model
        self.streams = streams
        self.tenant_id = DEFAULT_TENANT if tenant_id is None or J.is_blank(tenant_id) else tenant_id
        self.source_system_id = source_system_id
        self.connector_id = connector_id
        self.run_id = run_id
        self.levels = list(levels)

    def wrap(self, r: Record, observed_at: str, operation: dict | None = None, op: str = "upsert") -> Event:
        category = self.entity_model.category_of(r.entity)
        family = self.streams.family_of(category)
        if family is None:
            raise AgentFrontierError.invalid_definition("entity '" + r.entity + "' has no raw-stream category in "
                                                        "entities.json; it cannot be enveloped")
        assertion = J.text(operation, "assertion", "observed") if operation else "observed"
        confidence = J.get(operation, "confidence") if operation else None
        body = r.to_json()
        object_id = native_object_id(r, self.levels)
        env: dict[str, Any] = {
            "envelopeVersion": ENVELOPE_VERSION,
            "eventId": event_id(self.tenant_id, self.source_system_id, r.entity, object_id, op, body),
            "tenantId": self.tenant_id,
            "sourceSystemId": self.source_system_id,
            "connectorId": self.connector_id,
            "runId": self.run_id,
            "family": family,
            "category": category,
            "entity": r.entity,
            "op": op,
            "nativeObjectId": object_id,
            "observedAt": observed_at,
            "schemaVersion": self.entity_model.spec_version,
            "assertion": assertion,
            "confidence": float(confidence) if J.is_number(confidence) else 1.0,
        }
        return Event(r, env)


__all__ = ["Event", "EnvelopeBuilder", "native_object_id", "event_id", "ENVELOPE_VERSION", "DEFAULT_TENANT"]
