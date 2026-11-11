"""Business Context Graph v2: business terms and their relationships proposed by a model from the catalog, marked
``assertion="inferred"`` with a confidence and ``reviewStatus="pending"``. Nothing proposed here is a fact until a
person accepts it; products show the proposals for review and keep the accepted ones as asserted terms.
"""

from __future__ import annotations

import re
from typing import Any

from .llm import Budget, Client, structured_output
from .sqlcheck import SchemaCatalog

__all__ = ["propose_terms"]

_SCHEMA = {"type": "object", "required": ["terms"], "properties": {"terms": {"type": "array", "items": {
    "type": "object", "required": ["name", "definition", "columns"], "properties": {
        "name": {"type": "string"}, "definition": {"type": "string"},
        "columns": {"type": "array", "items": {"type": "string"}}, "expression": {"type": "string"},
        "related": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number"}}}}}}


def propose_terms(client: Client, catalog: SchemaCatalog, known: list[str] | None = None, max_terms: int = 12,
                  budget: Budget | None = None) -> list[dict[str, Any]]:
    """Proposed terms, each tied to catalog columns that exist; a proposal naming no real column is dropped."""
    lines = []
    for t, cols in list(catalog.tables.items())[:60]:
        lines.append(t + ": " + ", ".join(list(cols)[:40]))
    prompt = ("Propose up to " + str(max_terms) + " business terms (metrics and entities) that this schema "
              "supports. For each: a one-sentence definition, the schema.table.column values it is computed from, "
              "an SQL expression when it is a metric, related term names, and your confidence from 0 to 1."
              + ("\nAlready defined, do not repeat: " + ", ".join(known) if known else "")
              + "\n\nSchema:\n" + "\n".join(lines))
    out, _ = structured_output(client, prompt, _SCHEMA, budget=budget)
    terms = []
    seen = {k.lower() for k in known or []}
    for t in out.get("terms") or []:
        name = str(t.get("name") or "").strip()
        cols = [c.lower() for c in t.get("columns") or []
                if re.fullmatch(r"[\w$]+\.[\w$]+\.[\w$]+", str(c)) and
                str(c).lower().rsplit(".", 1)[0] in catalog.tables and
                str(c).lower().rsplit(".", 1)[1] in catalog.tables[str(c).lower().rsplit(".", 1)[0]]]
        if not name or name.lower() in seen or not cols:
            continue
        seen.add(name.lower())
        conf = t.get("confidence")
        terms.append({"name": name, "definition": str(t.get("definition") or ""), "columns": cols,
                      "expression": t.get("expression"), "related": list(t.get("related") or []),
                      "assertion": "inferred", "confidence": max(0.0, min(1.0, float(conf))) if isinstance(
                          conf, (int, float)) else 0.5, "reviewStatus": "pending"})
    return terms[:max_terms]
