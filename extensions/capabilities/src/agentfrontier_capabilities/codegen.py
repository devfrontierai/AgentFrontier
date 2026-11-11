"""Code generation from templates, with static validation only (execution is an ``Executor`` the product owns):
dbt models with their ``schema.yml`` (table references rewritten to ``ref()`` and ``source()``), and the reverse,
dbt SQL turned back into plain SQL so it can be validated against the catalog. Python snippets are checked by
compiling them.
"""

from __future__ import annotations

import re
from typing import Protocol

from .dq import dump_yaml
from .sqlcheck import referenced_tables

__all__ = ["dbt_model_files", "dejinja", "check_python", "Executor", "to_dbt_refs"]


class Executor(Protocol):
    """Runs generated code in a sandbox the product provides (Avero, the gateway). Not implemented here."""

    def run(self, kind: str, code: str, limits: dict) -> dict: ...


def to_dbt_refs(sql: str, dialect: str, model_of: dict[str, str], source_of: dict[str, tuple[str, str]]) -> str:
    """Table references as ``{{ ref('model') }}`` (tables a dbt model builds) or ``{{ source('src', 'table') }}``."""
    out = sql
    for schema, name in referenced_tables(sql, dialect):
        key = ((schema + ".") if schema else "") + name
        k = key.lower()
        repl = None
        if k in model_of:
            repl = "{{ ref('" + model_of[k] + "') }}"
        elif k in source_of:
            s, t = source_of[k]
            repl = "{{ source('" + s + "', '" + t + "') }}"
        if repl is None:
            continue
        out = re.sub(r"(?<![\w.\"])" + re.escape(key) + r"(?![\w\"])", repl, out, flags=re.I)
    return out


def dejinja(sql: str, schema_of_model: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """dbt SQL as plain SQL for validation: ``ref('m')`` becomes the table that model builds, ``source('s',
    't')`` becomes ``s.t``, ``config()`` and other blocks are dropped. Returns the SQL and what was dropped."""
    schema_of_model = schema_of_model or {}
    notes: list[str] = []

    def ref(m: re.Match) -> str:
        name = m.group(2) or m.group(1)
        return schema_of_model.get(name, name)
    s = re.sub(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?\s*\)\s*\}\}", ref, sql)
    s = re.sub(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}",
               lambda m: m.group(1) + "." + m.group(2), s)
    if re.search(r"\{\{\s*config\(", s):
        notes.append("config() dropped")
    s = re.sub(r"\{\{\s*config\([^}]*\)\s*\}\}", "", s)
    if re.search(r"\{%.*?%\}", s, re.S):
        notes.append("Jinja blocks dropped; the check covers the SQL outside them")
    s = re.sub(r"\{%.*?%\}", "", s, flags=re.S)
    s = re.sub(r"\{#.*?#\}", "", s, flags=re.S)
    if "{{" in s:
        notes.append("unresolved Jinja expressions replaced with NULL")
        s = re.sub(r"\{\{.*?\}\}", "null", s, flags=re.S)
    return s.strip(), notes


def dbt_model_files(name: str, sql: str, folder: str = "models", description: str | None = None,
                    materialized: str | None = None, schema_doc: dict | None = None) -> dict[str, str]:
    """The files of a new dbt model: ``<folder>/<name>.sql`` and ``<folder>/<name>.yml``. ``schema_doc`` is the
    document :func:`agentfrontier_capabilities.dq.to_dbt` returns; its model entry is renamed to ``name``."""
    body = sql.strip().rstrip(";") + "\n"
    if materialized:
        body = "{{ config(materialized='" + materialized + "') }}\n\n" + body
    doc = {"version": 2, "models": [{"name": name, **({"description": description} if description else {})}]}
    if schema_doc and schema_doc.get("models"):
        m = dict(schema_doc["models"][0])
        m["name"] = name
        if description:
            m = {"name": name, "description": description, **{k: v for k, v in m.items() if k != "name"}}
        doc["models"] = [m]
    path = folder.rstrip("/") + "/" + name
    return {path + ".sql": body, path + ".yml": dump_yaml(doc)}


def check_python(code: str) -> tuple[bool, str]:
    try:
        compile(code, "<generated>", "exec")
        return True, "compiles"
    except SyntaxError as e:
        return False, "line " + str(e.lineno) + ": " + str(e.msg)
