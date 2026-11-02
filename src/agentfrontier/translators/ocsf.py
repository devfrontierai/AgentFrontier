"""OCSF 1.3 API Activity events for audit events (spec docs/translators.md, "ocsf")."""

from __future__ import annotations

from .. import json_util as J
from ..model import Record
from . import TranslationContext, epoch_ms, upserts

_ACTIVITY = {"create": (1, "Create"), "read": (2, "Read"), "sample": (2, "Read"), "query": (2, "Read"),
             "write": (3, "Update"), "update": (3, "Update"), "delete": (4, "Delete")}
_UNMAPPED = ("decision", "outcome", "policyVersion", "purpose", "reason", "records", "sequence", "previousHash")


def translate(records: list[Record], ctx: TranslationContext) -> list[dict]:
    out = []
    for i in upserts(records, "audit_event"):
        v = i.v
        aid, aname = _ACTIVITY.get(J.jstr(v.get("action")), (99, "Other"))
        ok = v.get("outcome") == "success"
        user: dict = {"name": J.jstr(v.get("actor"))}
        if v.get("actorType") is not None:
            user["type"] = v["actorType"]
        resource: dict = {"name": J.jstr(v.get("resource"))}
        if v.get("resourceType") is not None:
            resource["type"] = v["resourceType"]
        meta: dict = {"product": {"name": "AgentFrontier", "vendor_name": "DevFrontierAI"}, "version": "1.3.0"}
        if v.get("correlationId") is not None:
            meta["correlation_uid"] = J.jstr(v["correlationId"])
        if v.get("hash") is not None:
            meta["uid"] = J.jstr(v["hash"])
        e = {"category_uid": 6, "category_name": "Application Activity", "class_uid": 6003,
             "class_name": "API Activity", "activity_id": aid, "activity_name": aname, "type_uid": 600300 + aid,
             "time": epoch_ms(v.get("occurredAt") or ctx.finished_at), "status_id": 1 if ok else 2,
             "status": "Success" if ok else "Failure", "severity_id": 3 if v.get("outcome") == "denied" else 1,
             "severity": "Medium" if v.get("outcome") == "denied" else "Informational",
             "actor": {"user": user}, "api": {"operation": J.jstr(v.get("action")), "service": {"name": "agentfrontier"}},
             "resources": [resource], "metadata": meta}
        unmapped = {k: v[k] for k in _UNMAPPED if v.get(k) is not None}
        if unmapped:
            e["unmapped"] = unmapped
        out.append(e)
    return out
