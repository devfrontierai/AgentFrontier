"""Capability registry: what a connector can do for a source, derived from its definition (spec
docs/capabilities.md). One ``capability`` record per operation, plus ``query`` for SQL definitions."""

from __future__ import annotations

from typing import Any

from . import json_util as J
from .definitions import SourceDefinition
from .model import Record
from .spec_model import EntityModel


def _entity_of(name: str, op: dict, level_lists: set[str]) -> str:
    if name in level_lists:
        return "container"
    if name == "datasets":
        return "dataset"
    if name == "sample":
        return "row"
    if name == "source":
        return "source"
    return J.text(op, "entity", "record")


def _modes(op: Any, entity: str, declared: dict, entity_model: EntityModel) -> list[str]:
    """Incremental modes (spec docs/capabilities.md): the operation's own ``incremental`` mode, ``full_diff`` for a
    stateful entity, then what the capabilities block declares."""
    out: list[str] = []
    mode = J.text(J.get(op, "incremental"), "mode")
    if mode:
        out.append(mode)
    if entity not in entity_model.event_entities and entity != "record":
        out.append("full_diff")
    for m in J.strings(declared.get("incremental")):
        if m not in out:
            out.append(m)
    return out


def capabilities(d: SourceDefinition, entity_model: EntityModel) -> list[Record]:
    """Capability records of a definition as written (overlays for particular versions are not applied)."""
    block = d.raw.get("capabilities") if isinstance(d.raw.get("capabilities"), dict) else {}
    declared_ops = block.get("operations") if isinstance(block.get("operations"), dict) else {}
    limits: dict[str, Any] = dict(block.get("limits") or {})
    rps = J.get(J.get(d.connection("http"), "rateLimit"), "requestsPerSecond")
    if rps is not None and "requestsPerSecond" not in limits:
        limits["requestsPerSecond"] = rps
    level_lists = {lv.list for lv in d.hierarchy if lv.list is not None}
    out: list[Record] = []

    def add(name: str, entity: str, aspect: str | None) -> None:
        declared = declared_ops.get(name) if isinstance(declared_ops.get(name), dict) else {}
        op_limits = dict(limits)
        op_limits.update(declared.get("limits") or {})
        out.append(Record("capability", {
            "source": d.id,
            "name": J.jstr(d.id) + "." + name,
            "operation": name,
            "producesEntity": entity,
            "aspect": aspect if aspect is not None else entity_model.aspect_of(entity),
            "category": entity_model.category_of(entity),
            "access": "read",
            "incremental": _modes(d.operation(name), entity, declared, entity_model),
            "apiVersion": J.text(block, "apiVersion"),
            "authMethods": list(d.auth),
            "limits": op_limits or None,
            "comment": J.text(declared, "comment", J.text(d.operation(name), "description")),
        }))

    for name, op in d.operations.items():
        if name == "test":
            continue
        add(name, _entity_of(name, op, level_lists), J.text(op, "aspect"))
    if d.protocol == "sql":
        add("query", "row", "data")
    return out


__all__ = ["capabilities"]
