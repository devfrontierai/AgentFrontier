"""OTLP/JSON logs for quality results, activities and audit events (spec docs/translators.md, "otel")."""

from __future__ import annotations

from typing import Any

from .. import json_util as J
from ..model import Record
from . import TranslationContext, epoch_ms, upserts

_TIME = {"quality_result": "measuredAt", "activity": "startedAt", "audit_event": "occurredAt"}
INFO, WARN, ERROR = (9, "INFO"), (13, "WARN"), (17, "ERROR")


def _severity(entity: str, v: dict) -> tuple[int, str]:
    if entity == "quality_result":
        return {"pass": INFO, "warn": WARN, "fail": ERROR, "error": ERROR}.get(J.jstr(v.get("status")), INFO)
    if entity == "activity":
        return ERROR if v.get("status") == "failed" else INFO
    return {"denied": WARN, "failure": ERROR}.get(J.jstr(v.get("outcome")), INFO)


def _body(entity: str, v: dict) -> str:
    if v.get("message") is not None:
        return J.jstr(v["message"])
    if entity == "quality_result":
        return J.jstr(v.get("check")) + " " + J.jstr(v.get("status"))
    if entity == "activity":
        return J.jstr(v.get("name")) + " " + J.jstr(v.get("status"))
    return J.jstr(v.get("action")) + " " + J.jstr(v.get("outcome"))


def any_value(x: Any) -> dict:
    if isinstance(x, bool):
        return {"boolValue": x}
    if isinstance(x, int):
        return {"intValue": str(x)}
    if isinstance(x, float):
        return {"doubleValue": x}
    if isinstance(x, (list, dict)):
        return {"stringValue": J.canonical(x)}
    return {"stringValue": J.jstr(x)}


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def translate(records: list[Record], ctx: TranslationContext) -> dict:
    logs = []
    for i in upserts(records, "quality_result", "activity", "audit_event"):
        v = i.v
        t = epoch_ms(v.get(_TIME[i.entity]) or ctx.finished_at) or 0
        num, text = _severity(i.entity, v)
        attrs = {"agentfrontier.entity": {"stringValue": i.entity}}
        for k, x in v.items():
            if x is not None and k != "entity":
                attrs["agentfrontier." + k] = any_value(x)
        logs.append({"timeUnixNano": str(t * 1_000_000), "severityNumber": num, "severityText": text,
                     "body": {"stringValue": _body(i.entity, v)},
                     "attributes": [{"key": k, "value": attrs[k]} for k in sorted(attrs)]})
    resource = [_attr("service.name", "agentfrontier"), _attr("agentfrontier.source", ctx.source),
                _attr("agentfrontier.tenant", ctx.tenant), _attr("agentfrontier.run_id", ctx.run_id)]
    return {"resourceLogs": [{"resource": {"attributes": resource},
                              "scopeLogs": [{"scope": {"name": "agentfrontier"}, "logRecords": logs}]}]}
