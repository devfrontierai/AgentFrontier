"""OpenMetadata 1.x create-or-update requests (spec docs/translators.md, "openmetadata")."""

from __future__ import annotations

import re

from .. import json_util as J
from ..model import Record
from . import TranslationContext, level_values, upserts

SERVICE_TYPES = {"postgresql": "Postgres", "mysql": "Mysql", "oracle": "Oracle", "sqlserver": "Mssql",
                 "snowflake": "Snowflake", "bigquery": "BigQuery", "redshift": "Redshift", "databricks": "Databricks",
                 "unity_catalog": "Databricks", "sqlite": "SQLite"}
_TYPES: dict[str, str] = {}
for _t, _names in {
    "BIGINT": "bigint int8 long", "INT": "int integer int4 mediumint", "SMALLINT": "smallint int2",
    "TINYINT": "tinyint", "DECIMAL": "numeric decimal number money", "DOUBLE": "double|double precision|float8",
    "FLOAT": "real float float4", "BOOLEAN": "boolean bool bit",
    "VARCHAR": "varchar|character varying|nvarchar|varchar2|nvarchar2|string",
    "CHAR": "char character nchar bpchar", "TEXT": "text clob nclob longtext mediumtext", "DATE": "date",
    "TIMESTAMP": "timestamp|timestamptz|timestamp with time zone|timestamp without time zone|datetime|datetime2|"
                 "timestamp_ntz|timestamp_ltz|timestamp_tz",
    "TIME": "time|time with time zone|time without time zone", "JSON": "json jsonb variant object",
    "UUID": "uuid uniqueidentifier", "BLOB": "bytea blob binary varbinary bytes longblob",
    "STRUCT": "record struct", "ARRAY": "array", "GEOGRAPHY": "geography geometry",
}.items():
    for _n in (_names.split("|") if "|" in _names else _names.split()):
        _TYPES[_n] = _t
_SUFFIX = re.compile(r"\s*\(([^)]*)\)\s*")
_NUMBER = re.compile(r"\d+")


def data_type(source_type: str | None) -> tuple[str, str | None, str | None]:
    """(dataType, arrayDataType, the parenthesised suffix) of a source type."""
    t = J.jtrim(J.jstr(source_type or "")).lower()
    is_array = t.endswith("[]")
    if is_array:
        t = t[:-2]
    m = _SUFFIX.search(t)
    suffix = m.group(1) if m else None
    base = J.jtrim(_SUFFIX.sub(" ", t)).strip()
    mapped = _TYPES.get(base, "UNKNOWN")
    if is_array:
        return "ARRAY", mapped, suffix
    return mapped, None, suffix


def layout(v: dict, levels: list[str]) -> tuple[str, str]:
    """(database, schema) of a record (docs/translators.md, "openmetadata")."""
    vals = level_values(v, levels)
    if len(levels) >= 2:
        return vals[0] if vals else "default", ".".join(vals[1:]) or "default"
    if len(levels) == 1:
        x = vals[0] if vals else "default"
        return (x, x) if levels[0] == "database" else ("default", x)
    return "default", "default"


def _ref_layout(v: dict, levels: list[str], prefix: str) -> tuple[str, str]:
    own = layout(v, levels)
    p = {lv: v.get(prefix + lv[:1].upper() + lv[1:]) for lv in levels}
    if all(x is None for x in p.values()):
        return own
    return layout({lv: (p[lv] if p[lv] is not None else v.get(lv)) for lv in levels}, levels)


TABLE_TYPES = {"table": "Regular", "view": "View", "materialized_view": "MaterializedView",
               "external_table": "External", "partitioned_table": "Partitioned"}


def translate(records: list[Record], ctx: TranslationContext) -> list[dict]:
    service = J.jstr(ctx.option("service", ctx.source))
    levels = ctx.levels
    fqn = lambda db, sc, t=None: ".".join([service, db, sc] + ([t] if t is not None else []))  # noqa: E731
    databases, schemas, tables = set(), set(), {}
    fields: dict[str, list[dict]] = {}
    constraints: dict[str, list[dict]] = {}
    for i in upserts(records, "dataset", "field", "constraint", "container"):
        v = i.v
        db, sc = layout(v, levels)
        if i.entity == "container":
            if levels and v.get("level") == levels[-1]:
                databases.add(db)
                schemas.add((db, sc))
            elif levels and v.get("level") == levels[0]:
                databases.add(db)
            continue
        databases.add(db)
        schemas.add((db, sc))
        if i.entity == "dataset":
            tables[fqn(db, sc, J.jstr(v.get("name")))] = (db, sc, v)
        elif i.entity == "field":
            fields.setdefault(fqn(db, sc, J.jstr(v.get("dataset"))), []).append(v)
        else:
            constraints.setdefault(fqn(db, sc, J.jstr(v.get("dataset"))), []).append(v)
    out = [{"method": "PUT", "path": "/v1/services/databaseServices",
            "body": {"name": service, "serviceType": SERVICE_TYPES.get(J.jstr(ctx.definition), "CustomDatabase")}}]
    for db in sorted(databases):
        out.append({"method": "PUT", "path": "/v1/databases", "body": {"name": db, "service": service}})
    for db, sc in sorted(schemas, key=lambda x: fqn(x[0], x[1])):
        out.append({"method": "PUT", "path": "/v1/databaseSchemas",
                    "body": {"name": sc, "database": service + "." + db}})
    for t in sorted(tables):
        db, sc, v = tables[t]
        cons = sorted(constraints.get(t, []), key=lambda c: J.jstr(c.get("name")))
        pk = next((c for c in cons if c.get("type") == "primary_key"), None)
        pk_cols = [J.jstr(x) for x in (pk.get("columns") or [])] if pk else []
        uniques = [c for c in cons if c.get("type") == "unique"]
        single_unique = {J.jstr(c["columns"][0]) for c in uniques if len(c.get("columns") or []) == 1}
        cols = []
        for f in sorted(fields.get(t, []), key=lambda f: (f.get("ordinal") is None, f.get("ordinal") or 0,
                                                          J.jstr(f.get("name")))):
            dt, arr, suffix = data_type(f.get("dataType"))
            c: dict = {"name": J.jstr(f.get("name")), "dataType": dt,
                       "dataTypeDisplay": J.jstr(f.get("dataType") or "unknown")}
            if f.get("ordinal") is not None:
                c["ordinalPosition"] = f["ordinal"]
            if f.get("comment") is not None:
                c["description"] = f["comment"]
            if dt in ("VARCHAR", "CHAR"):
                n = f.get("maxLength")
                if n is None and suffix is not None:
                    m = _NUMBER.search(suffix)
                    n = int(m.group()) if m else None
                c["dataLength"] = int(n) if n is not None else 1
            if arr is not None:
                c["arrayDataType"] = arr
            name = c["name"]
            if len(pk_cols) == 1 and pk_cols[0] == name:
                c["constraint"] = "PRIMARY_KEY"
            elif name in single_unique:
                c["constraint"] = "UNIQUE"
            elif f.get("nullable") is False:
                c["constraint"] = "NOT_NULL"
            cols.append(c)
        body: dict = {"name": J.jstr(v.get("name")), "databaseSchema": fqn(db, sc),
                      "tableType": TABLE_TYPES.get(J.jstr(v.get("type")), "Regular"), "columns": cols}
        if v.get("comment") is not None:
            body["description"] = v["comment"]
        if v.get("definition") is not None:
            body["schemaDefinition"] = J.jstr(v["definition"])
        tc = []
        if len(pk_cols) > 1:
            tc.append({"constraintType": "PRIMARY_KEY", "columns": pk_cols})
        for u in uniques:
            if len(u.get("columns") or []) > 1:
                tc.append({"constraintType": "UNIQUE", "columns": [J.jstr(x) for x in u["columns"]]})
        for c in cons:
            if c.get("type") == "foreign_key" and c.get("refDataset") is not None:
                rdb, rsc = _ref_layout(c, levels, "ref")
                ref = fqn(rdb, rsc, J.jstr(c["refDataset"]))
                tc.append({"constraintType": "FOREIGN_KEY", "columns": [J.jstr(x) for x in c.get("columns") or []],
                           "referredColumns": [ref + "." + J.jstr(x) for x in c.get("refColumns") or []]})
        if tc:
            body["tableConstraints"] = tc
        out.append({"method": "PUT", "path": "/v1/tables", "body": body})
    edges: dict[tuple[str, str], dict] = {}
    for i in upserts(records, "lineage"):
        v = i.v
        if v.get("dataset") is None or v.get("upstreamDataset") is None:
            continue
        db, sc = layout(v, levels)
        udb, usc = _ref_layout(v, levels, "upstream")
        to, frm = fqn(db, sc, J.jstr(v["dataset"])), fqn(udb, usc, J.jstr(v["upstreamDataset"]))
        e = edges.setdefault((to, frm), {"columns": {}})
        if v.get("field") is not None and v.get("upstreamField") is not None:
            e["columns"].setdefault(to + "." + J.jstr(v["field"]), set()).add(frm + "." + J.jstr(v["upstreamField"]))
    for (to, frm) in sorted(edges):
        details: dict = {"source": "ViewLineage"}
        cl = edges[(to, frm)]["columns"]
        if cl:
            details["columnsLineage"] = [{"fromColumns": sorted(cl[k]), "toColumn": k} for k in sorted(cl)]
        view = tables.get(to)
        if view is not None and view[2].get("definition") is not None:
            details["sqlQuery"] = J.jstr(view[2]["definition"])
        out.append({"method": "PUT", "path": "/v1/lineage",
                    "body": {"edge": {"fromEntity": {"type": "table", "fqn": frm},
                                      "toEntity": {"type": "table", "fqn": to}, "lineageDetails": details}}})
    return out
