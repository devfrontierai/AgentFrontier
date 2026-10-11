"""Small helpers shared by the examples.

``load_connection`` falls back to the bundled SQLite sample, so every example runs with no setup. In your own code
this is just ``ConnectionConfig.from_file("my-source.json")``.
"""

from __future__ import annotations

from agentfrontier import AgentFrontier, ConnectionConfig, Record

import sample_db


def load_connection(path: str | None = None) -> ConnectionConfig:
    """A connection file, or the bundled sample shop database when no path is given."""
    if path:
        return ConnectionConfig.from_file(path)
    return ConnectionConfig.from_json({
        "id": "shop", "name": "Sample shop (SQLite)", "type": "sqlite",
        "properties": {"path": str(sample_db.ensure())},
    })


def levels(af: AgentFrontier, connection: ConnectionConfig) -> list[str]:
    """The hierarchy levels of a source, for example ``["database", "schema"]`` for PostgreSQL, ``[]`` for SQLite."""
    definition = af.definition(connection.type)
    return [lv.level for lv in definition.hierarchy] if definition else []


def qualified(record: Record, level_names: list[str], name_key: str = "dataset") -> str:
    """``database.schema.table``-style name of the dataset a record belongs to (``name_key="name"`` for datasets)."""
    parts = [record.string(lv) for lv in level_names if record.has(lv)]
    return ".".join(parts + [record.string(name_key)])


def ref_target(constraint: Record, level_names: list[str]) -> str:
    """Qualified name of the dataset a foreign key points to. Sources that can reference another container say so
    with ``refDatabase`` / ``refSchema`` / ...; otherwise the target lives next to the constraint's dataset."""
    parts = []
    for lv in level_names:
        ref = constraint.get_or_property("ref" + lv[0].upper() + lv[1:])
        parts.append(str(ref) if ref else constraint.string(lv))
    return ".".join([p for p in parts if p] + [constraint.string("refDataset")])
