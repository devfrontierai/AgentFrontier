"""Notebooks from typed cells: markdown, SQL (a query whose result becomes a DataFrame), chart (a plot of a
DataFrame) and Python. Each cell renders to plain Python that runs in any Jupyter, writes to an ``.ipynb``
(nbformat 4) with the cell's kind, author and hash in its metadata, and reads back. ``report_markdown`` turns an
executed notebook into a Markdown report.

SQL cells call ``query(sql)``. The prelude cell defines it: in a user's Jupyter it calls the product's read-only
SQL endpoint (credentials from the environment, never in the notebook); a product that executes the notebook itself
swaps the prelude for one that reads results it fetched. Standard library only; the notebooks need pandas and
matplotlib where they run.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Cell", "Output", "render", "to_ipynb", "from_ipynb", "prelude", "report_markdown", "cell_hash",
           "dumps", "preview", "KINDS", "CHART_TYPES"]

KINDS = ("markdown", "sql", "chart", "python")
CHART_TYPES = ("bar", "barh", "line", "area", "pie", "scatter")
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,40}")


@dataclass
class Cell:
    kind: str
    source: str = ""                  # markdown text, SQL or Python
    name: str | None = None           # sql: the DataFrame variable it fills
    chart: dict | None = None         # chart: {"data", "type", "x", "y": [...], "title"}
    authored: str = "template"        # template (written by the product), llm, user
    title: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError("cell kind must be one of " + ", ".join(KINDS))
        if self.kind == "sql" and not (self.name and _NAME.fullmatch(self.name)):
            raise ValueError("a sql cell needs a lower-case variable name")
        if self.kind == "chart":
            c = self.chart or {}
            if c.get("type") not in CHART_TYPES or not c.get("data") or not c.get("x") or not c.get("y"):
                raise ValueError("a chart cell needs data, type (" + ", ".join(CHART_TYPES) + "), x and y")

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @staticmethod
    def from_json(d: dict) -> "Cell":
        return Cell(**{k: v for k, v in d.items() if k in Cell.__dataclass_fields__})


@dataclass
class Output:
    status: str = "not_run"           # ok, error, skipped, not_run
    text: str = ""                    # stdout, or a summary of the result
    error: dict | None = None         # {"type", "message", "traceback"}
    table: dict | None = None         # {"columns", "rows" (first rows), "rowCount"}
    image_png: str | None = None      # base64
    ms: int = 0

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}

    @staticmethod
    def from_json(d: dict) -> "Output":
        return Output(**{k: v for k, v in d.items() if k in Output.__dataclass_fields__})


def _py_str(s: str) -> str:
    """A Python literal for SQL: triple-quoted when it is safe, else repr."""
    if '"""' not in s and not s.endswith('"') and "\\" not in s:
        return '"""\n' + s.strip() + '\n"""'
    return repr(s)


def render(cell: Cell) -> str:
    """The Python (or Markdown) a cell becomes."""
    if cell.kind in ("markdown", "python"):
        return cell.source
    if cell.kind == "sql":
        return cell.name + " = query(" + _py_str(cell.source) + ")\n" + cell.name + ".head(20)"
    c = cell.chart or {}
    data, x, y = c["data"], c["x"], list(c["y"])
    title = repr(c.get("title") or "")
    if c["type"] == "pie":
        body = (data + ".set_index(" + repr(x) + ")[" + repr(y[0]) + "].plot.pie(figsize=(6, 6), autopct='%1.0f%%', "
                "title=" + title + ", ylabel='')")
    elif c["type"] == "scatter":
        body = data + ".plot.scatter(x=" + repr(x) + ", y=" + repr(y[0]) + ", figsize=(8, 4), title=" + title + ")"
    else:
        body = (data + ".plot(kind=" + repr(c["type"]) + ", x=" + repr(x) + ", y=" + repr(y if len(y) > 1 else y[0])
                + ", figsize=(9, 4), title=" + title + ", legend=" + str(len(y) > 1) + ")")
    return "ax = " + body + "\nplt.tight_layout()\nplt.show()"


def prelude(source_id: str, url_env: str = "AVERO_URL", default_url: str = "http://127.0.0.1:8096") -> Cell:
    """The first code cell: imports and ``query()``, which runs read-only SQL through the product's API with the
    token from ``AVERO_TOKEN``. Policy and masking apply as for any other query."""
    code = f'''import json
import os
import urllib.request

import matplotlib.pyplot as plt
import pandas as pd

SOURCE = {source_id!r}
URL = os.environ.get({url_env!r}, {default_url!r})


def query(sql, max_rows=5000):
    """Read-only SQL through Avero (policy and masking apply). Needs AVERO_TOKEN in the environment."""
    body = json.dumps({{"source_id": SOURCE, "sql": sql, "max_rows": max_rows}}).encode()
    req = urllib.request.Request(URL + "/api/v1/tools/run_sql", data=body, method="POST", headers={{
        "Authorization": "Bearer " + os.environ["AVERO_TOKEN"], "Content-Type": "application/json"}})
    with urllib.request.urlopen(req) as r:
        out = json.load(r)
    return pd.DataFrame(out["rows"], columns=out["columns"])
'''
    return Cell("python", code, authored="template", title="Setup", id="prelude")


def cell_hash(cell: Cell) -> str:
    return "sha256:" + hashlib.sha256((cell.kind + "\n" + render(cell)).encode("utf-8")).hexdigest()[:16]


def _outputs(o: Output | None, count: int) -> list[dict]:
    if o is None or o.status == "not_run":
        return []
    out: list[dict] = []
    if o.text:
        out.append({"output_type": "stream", "name": "stdout", "text": o.text})
    if o.table:
        out.append({"output_type": "execute_result", "execution_count": count, "metadata": {},
                    "data": {"text/plain": _table_text(o.table)}})
    if o.image_png:
        out.append({"output_type": "display_data", "metadata": {}, "data": {"image/png": o.image_png}})
    if o.error:
        out.append({"output_type": "error", "ename": o.error.get("type", "Error"),
                    "evalue": o.error.get("message", ""), "traceback": o.error.get("traceback", [])})
    return out


def _table_text(t: dict) -> str:
    cols = [str(c) for c in t.get("columns", [])]
    rows = [[("" if v is None else str(v)) for v in r] for r in t.get("rows", [])[:20]]
    widths = [max([len(c)] + [len(r[i]) for r in rows if i < len(r)]) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths))  # noqa: E731
    return "\n".join([line(cols)] + [line(r) for r in rows]) + (
        "\n… " + format(t.get("rowCount", 0), ",") + " rows" if t.get("rowCount", 0) > len(rows) else "")


def to_ipynb(cells: list[Cell], title: str, outputs: dict[str, Output] | None = None,
             metadata: dict | None = None) -> dict:
    """An nbformat 4 notebook. Code cells carry ``metadata.avero`` (kind, name, chart, author, hash)."""
    nb_cells = []
    n = 0
    for c in cells:
        meta = {"avero": {"kind": c.kind, "id": c.id, "authored": c.authored, "hash": cell_hash(c),
                          **({"name": c.name} if c.name else {}), **({"chart": c.chart} if c.chart else {}),
                          **({"sql": c.source} if c.kind == "sql" else {}),
                          **({"title": c.title} if c.title else {})}}
        if c.kind == "markdown":
            nb_cells.append({"cell_type": "markdown", "id": c.id, "metadata": meta,
                             "source": c.source.splitlines(keepends=True)})
            continue
        n += 1
        o = (outputs or {}).get(c.id)
        nb_cells.append({"cell_type": "code", "id": c.id, "metadata": meta,
                         "execution_count": n if o is not None and o.status != "not_run" else None,
                         "outputs": _outputs(o, n), "source": render(c).splitlines(keepends=True)})
    return {"nbformat": 4, "nbformat_minor": 5, "cells": nb_cells,
            "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                         "language_info": {"name": "python"}, "title": title, "avero": metadata or {}}}


def from_ipynb(doc: dict) -> list[Cell]:
    """Cells back from a notebook. Cells without Avero metadata, or whose source no longer matches their hash,
    are user Python (or markdown)."""
    out = []
    for c in doc.get("cells") or []:
        src = "".join(c.get("source") or [])
        meta = (c.get("metadata") or {}).get("avero") or {}
        cid = c.get("id") or uuid.uuid4().hex[:8]
        if c.get("cell_type") == "markdown":
            out.append(Cell("markdown", src, authored=meta.get("authored", "user"), id=cid))
            continue
        kind = meta.get("kind")
        if kind == "sql" and meta.get("sql") is not None and meta.get("name"):
            cell = Cell("sql", meta["sql"], meta["name"], authored=meta.get("authored", "template"),
                        title=meta.get("title"), id=cid)
        elif kind == "chart" and meta.get("chart"):
            cell = Cell("chart", "", chart=meta["chart"], authored=meta.get("authored", "template"),
                        title=meta.get("title"), id=cid)
        else:
            cell = Cell("python", src, authored=meta.get("authored", "user"), title=meta.get("title"), id=cid)
        if render(cell).strip() != src.strip():  # edited since it was written: it is the user's code now
            cell = Cell("python", src, authored="user", title=meta.get("title"), id=cid)
        out.append(cell)
    return out


def report_markdown(title: str, cells: list[Cell], outputs: dict[str, Output], definitions: list[dict],
                    images: dict[str, str] | None = None, notes: list[str] | None = None) -> str:
    """A report from an executed notebook: the definitions used, each step's text, result preview and chart (as an
    image file from ``images``: cell id -> relative path), and what failed."""
    lines = ["# " + title, ""]
    if definitions:
        lines += ["## Definitions", ""]
        for d in definitions:
            lines.append("* **" + str(d.get("name")) + "**: " + str(d.get("definition") or "")
                         + ((" (`" + str(d["expression"]) + "`)") if d.get("expression") else "")
                         + ((" — from " + str(d["source"])) if d.get("source") else ""))
        lines.append("")
    for c in cells:
        o = outputs.get(c.id)
        if c.kind == "markdown":
            lines += [c.source.strip(), ""]
            continue
        if c.id == "prelude":
            continue
        if c.title:
            lines += ["### " + c.title, ""]
        if c.kind == "sql":
            lines += ["```sql", c.source.strip(), "```", ""]
        if o is None or o.status == "not_run":
            lines += ["_Not run._", ""]
            continue
        if o.status == "error":
            err = o.error or {}
            lines += ["> **Failed:** " + str(err.get("type", "Error")) + ": " + str(err.get("message", "")), ""]
            continue
        if o.table:
            t = o.table
            cols = [str(x) for x in t.get("columns", [])]
            lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            for r in t.get("rows", [])[:10]:
                lines.append("| " + " | ".join("" if v is None else str(v).replace("|", "\\|") for v in r) + " |")
            if t.get("rowCount", 0) > 10:
                lines.append("")
                lines.append("_" + format(t["rowCount"], ",") + " rows; the first 10 are shown._")
            lines.append("")
        if images and c.id in images:
            lines += ["![" + (c.title or "chart") + "](" + images[c.id] + ")", ""]
        if o.text and c.kind == "python":
            lines += ["```text", o.text.strip()[:2000], "```", ""]
    if notes:
        lines += ["## Notes", ""] + ["* " + n for n in notes] + [""]
    return "\n".join(lines).rstrip() + "\n"


def dumps(nb: dict) -> str:
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"



def preview(rows: list[list[Any]], columns: list[str], n: int = 20) -> dict:
    return {"columns": columns, "rows": rows[:n], "rowCount": len(rows)}

