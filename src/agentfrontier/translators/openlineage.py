"""OpenLineage 2-0-2 RunEvents for a run's lineage (spec docs/translators.md, "openlineage")."""

from __future__ import annotations

from typing import Any

from .. import json_util as J
from ..model import Record
from . import PRODUCER, TranslationContext, det_uuid, qualified, upserts

RUN_EVENT = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
JOB_TYPE = "https://openlineage.io/spec/facets/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet"
SQL = "https://openlineage.io/spec/facets/1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet"
SCHEMA = "https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
COLUMN_LINEAGE = "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet"


def _facet(url: str, **values: Any) -> dict:
    return dict({"_producer": PRODUCER, "_schemaURL": url}, **values)


def translate(records: list[Record], ctx: TranslationContext) -> list[dict]:
    ns = ctx.option("namespace", "agentfrontier://" + ctx.source)
    levels = ctx.levels
    datasets: dict[str, dict] = {}
    fields: dict[str, list[dict]] = {}
    for i in upserts(records, "dataset", "field"):
        if i.entity == "dataset":
            datasets[qualified(i.v, levels, "name")] = i.v
        else:
            fields.setdefault(qualified(i.v, levels), []).append(i.v)
    edges: dict[str, dict] = {}  # downstream -> {"inputs": {...}, "columns": {...}}
    for i in upserts(records, "lineage"):
        v = i.v
        if v.get("dataset") is None or v.get("upstreamDataset") is None:
            continue
        down = qualified(v, levels)
        up = qualified(v, levels, "upstreamDataset", "upstream")
        e = edges.setdefault(down, {"inputs": set(), "columns": {}})
        e["inputs"].add(up)
        if v.get("field") is not None and v.get("upstreamField") is not None:
            sub = "IDENTITY" if v.get("transformation") == "identity" else "TRANSFORMATION"
            e["columns"].setdefault(J.jstr(v["field"]), set()).add((up, J.jstr(v["upstreamField"]), sub))

    def schema(name: str) -> dict | None:
        fs = fields.get(name)
        if not fs:
            return None
        ordered = sorted(fs, key=lambda f: (f.get("ordinal") is None, f.get("ordinal") or 0, J.jstr(f.get("name"))))
        out = []
        for f in ordered:
            x = {"name": J.jstr(f.get("name")), "type": f.get("dataType")}
            if f.get("comment") is not None:
                x["description"] = f["comment"]
            out.append({k: y for k, y in x.items() if y is not None})
        return _facet(SCHEMA, fields=out)

    events = []
    for name in sorted(edges):
        e = edges[name]
        d = datasets.get(name) or {}
        kind = "VIEW" if J.jstr(d.get("type")) in ("view", "materialized_view") else "QUERY"
        job_facets: dict = {"jobType": _facet(JOB_TYPE, processingType="BATCH", integration="AGENTFRONTIER",
                                              jobType=kind)}
        if d.get("definition") is not None:
            job_facets["sql"] = _facet(SQL, query=J.jstr(d["definition"]))
        inputs = []
        for up in sorted(e["inputs"]):
            facets: dict = {}
            s = schema(up)
            if s is not None:
                facets["schema"] = s
            inputs.append({"namespace": ns, "name": up, "facets": facets})
        out_facets: dict = {}
        s = schema(name)
        if s is not None:
            out_facets["schema"] = s
        if e["columns"]:
            cols = {}
            for col in sorted(e["columns"]):
                cols[col] = {"inputFields": [
                    {"namespace": ns, "name": up, "field": uf,
                     "transformations": [{"type": "DIRECT", "subtype": sub, "description": "", "masking": False}]}
                    for up, uf, sub in sorted(e["columns"][col])]}
            out_facets["columnLineage"] = _facet(COLUMN_LINEAGE, fields=cols)
        events.append({"eventType": "COMPLETE", "eventTime": ctx.finished_at, "producer": PRODUCER,
                       "schemaURL": RUN_EVENT, "run": {"runId": det_uuid(ctx.run_id, name)},
                       "job": {"namespace": ns, "name": name, "facets": job_facets}, "inputs": inputs,
                       "outputs": [{"namespace": ns, "name": name, "facets": out_facets}]})
    return events
