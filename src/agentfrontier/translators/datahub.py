"""DataHub metadata change proposals (spec docs/translators.md, "datahub")."""

from __future__ import annotations

from .. import json_util as J
from ..model import Record
from . import TranslationContext, items, qualified
from .openmetadata import data_type

PLATFORMS = {"postgresql": "postgres", "sqlserver": "mssql", "unity_catalog": "databricks", "adls": "adlsGen2",
             "local_files": "file", "opensearch": "elasticsearch"}
SUB_TYPES = {"view": "View", "materialized_view": "Materialized View", "external_table": "External Table",
             "file": "File"}
FIELD_TYPES = {"BIGINT": "NumberType", "INT": "NumberType", "SMALLINT": "NumberType", "TINYINT": "NumberType",
               "DECIMAL": "NumberType", "DOUBLE": "NumberType", "FLOAT": "NumberType", "VARCHAR": "StringType",
               "CHAR": "StringType", "TEXT": "StringType", "UUID": "StringType", "BOOLEAN": "BooleanType",
               "DATE": "DateType", "TIMESTAMP": "TimeType", "TIME": "TimeType", "BLOB": "BytesType",
               "ARRAY": "ArrayType", "JSON": "RecordType", "STRUCT": "RecordType"}
_NOT_CUSTOM = {"entity", "source", "name", "comment", "definition", "properties"}


def _scalar(v: object) -> bool:
    return isinstance(v, (str, int, float, bool))


def translate(records: list[Record], ctx: TranslationContext) -> list[dict]:
    env = J.jstr(ctx.option("env", "PROD"))
    platform = J.jstr(ctx.option("platform", PLATFORMS.get(J.jstr(ctx.definition), J.jstr(ctx.definition))))
    levels = ctx.levels
    purn = "urn:li:dataPlatform:" + platform

    def urn(qn: str) -> str:
        return "urn:li:dataset:(" + purn + "," + qn + "," + env + ")"

    def furn(qn: str, field: str) -> str:
        return "urn:li:schemaField:(" + urn(qn) + "," + field + ")"

    datasets: dict[str, dict] = {}
    removed: set[str] = set()
    fields: dict[str, list[dict]] = {}
    cons: dict[str, list[dict]] = {}
    tags: dict[str, set[str]] = {}
    lineage: dict[str, dict] = {}
    for i in items(records):
        v = i.v
        if i.entity == "dataset":
            qn = qualified(v, levels, "name")
            if i.op == "upsert":
                datasets[qn] = v
                removed.discard(qn)
            else:
                removed.add(qn)
                datasets.pop(qn, None)
            continue
        if i.op != "upsert":
            continue
        if i.entity == "field" and v.get("dataset") is not None:
            fields.setdefault(qualified(v, levels), []).append(v)
        elif i.entity == "constraint" and v.get("dataset") is not None:
            cons.setdefault(qualified(v, levels), []).append(v)
        elif i.entity == "tag" and v.get("dataset") is not None and v.get("field") is None:
            name = J.jstr(v.get("name")) + ("" if v.get("value") is None else "=" + J.jstr(v["value"]))
            tags.setdefault(qualified(v, levels), set()).add("urn:li:tag:" + name)
        elif i.entity == "lineage" and v.get("dataset") is not None and v.get("upstreamDataset") is not None:
            down, up = qualified(v, levels), qualified(v, levels, "upstreamDataset", "upstream")
            e = lineage.setdefault(down, {"ups": {}, "fine": {}})
            e["ups"][up] = "VIEW" if v.get("kind") == "view" else e["ups"].get(up, "TRANSFORMED")
            if v.get("field") is not None and v.get("upstreamField") is not None:
                key = (J.jstr(v["field"]), "IDENTITY" if v.get("transformation") == "identity" else "TRANSFORMATION")
                conf = i.envelope.get("confidence", 1.0)
                f = e["fine"].setdefault(key, {"ups": set(), "conf": float(conf)})
                f["ups"].add(furn(up, J.jstr(v["upstreamField"])))
                f["conf"] = min(f["conf"], float(conf))

    out: list[dict] = []

    def mcp(qn: str, aspect: str, value: dict) -> None:
        out.append({"entityType": "dataset", "entityUrn": urn(qn), "changeType": "UPSERT", "aspectName": aspect,
                    "aspect": value})

    for qn in sorted(set(datasets) | removed | set(lineage), key=urn):
        if qn in removed:
            mcp(qn, "status", {"removed": True})
            continue
        v = datasets.get(qn)
        if v is not None:
            props = {k: J.jstr(x) if not isinstance(x, bool) else ("true" if x else "false")
                     for k, x in v.items() if k not in _NOT_CUSTOM and k not in levels and _scalar(x)}
            for k, x in (v.get("properties") or {}).items():
                if _scalar(x):
                    props[k] = J.jstr(x) if not isinstance(x, bool) else ("true" if x else "false")
            dp: dict = {"name": J.jstr(v.get("name")), "qualifiedName": qn}
            if v.get("comment") is not None:
                dp["description"] = J.jstr(v["comment"])
            dp["customProperties"] = {k: props[k] for k in sorted(props)}
            mcp(qn, "datasetProperties", dp)
            mcp(qn, "subTypes", {"typeNames": [SUB_TYPES.get(J.jstr(v.get("type")), "Table")]})
        fs = fields.get(qn)
        cs = sorted(cons.get(qn, []), key=lambda c: J.jstr(c.get("name")))
        pk = next((c for c in cs if c.get("type") == "primary_key"), None)
        pk_cols = [J.jstr(x) for x in (pk.get("columns") or [])] if pk else []
        if fs:
            sf = []
            for f in sorted(fs, key=lambda f: (f.get("ordinal") is None, f.get("ordinal") or 0, J.jstr(f.get("name")))):
                t = FIELD_TYPES.get(data_type(f.get("dataType"))[0], "NullType")
                x: dict = {"fieldPath": J.jstr(f.get("name")), "nativeDataType": J.jstr(f.get("dataType") or ""),
                           "type": {"type": {"com.linkedin.schema." + t: {}}}, "nullable": f.get("nullable") is not False}
                if f.get("comment") is not None:
                    x["description"] = J.jstr(f["comment"])
                x["isPartOfKey"] = J.jstr(f.get("name")) in pk_cols
                sf.append(x)
            sm: dict = {"schemaName": qn, "platform": purn, "version": 0, "hash": "",
                        "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": ""}}, "fields": sf}
            if pk_cols:
                sm["primaryKeys"] = pk_cols
            fks = []
            for c in cs:
                if c.get("type") == "foreign_key" and c.get("refDataset") is not None:
                    ref = qualified(c, levels, "refDataset", "ref")
                    fks.append({"name": J.jstr(c.get("name")), "foreignDataset": urn(ref),
                                "sourceFields": [furn(qn, J.jstr(x)) for x in c.get("columns") or []],
                                "foreignFields": [furn(ref, J.jstr(x)) for x in c.get("refColumns") or []]})
            if fks:
                sm["foreignKeys"] = fks
            mcp(qn, "schemaMetadata", sm)
        e = lineage.get(qn)
        if e is not None:
            ul: dict = {"upstreams": [{"dataset": urn(u), "type": e["ups"][u]}
                                      for u in sorted(e["ups"], key=urn)]}
            if e["fine"]:
                ul["fineGrainedLineages"] = [
                    {"upstreamType": "FIELD_SET", "upstreams": sorted(f["ups"]), "downstreamType": "FIELD",
                     "downstreams": [furn(qn, col)], "transformOperation": op, "confidenceScore": f["conf"]}
                    for (col, op), f in sorted(e["fine"].items(), key=lambda kv: (furn(qn, kv[0][0]),
                                                                                  sorted(kv[1]["ups"])))]
            mcp(qn, "upstreamLineage", ul)
        if tags.get(qn):
            mcp(qn, "globalTags", {"tags": [{"tag": t} for t in sorted(tags[qn])]})
        if v is not None:
            mcp(qn, "status", {"removed": False})
    return out
