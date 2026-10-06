"""Mirrors the Java EngineUnitTest: the building blocks every implementation must agree on."""

from __future__ import annotations

import json

import pytest

from agentfrontier import AgentFrontier, AgentFrontierError, Secrets
from agentfrontier import json_util as J
from agentfrontier import template as T
from agentfrontier.definitions import VersionRange
from agentfrontier.engine import decode_cursor, encode_cursor
from agentfrontier.mapping import JsonPath, RecordMapper
from agentfrontier.protocols.inference import PathGlob
from agentfrontier.protocols.sqlguard import SqlGuard, convert_markers


def vars_(**kv):
    return T.Vars.of(kv)


@pytest.fixture(scope="module")
def af():
    return AgentFrontier()


# ------------------------------------------------------------------------------------------------- templates


def test_whole_placeholder_keeps_its_type():
    assert T.render("{{port}}", vars_(port=5432), T.STRICT) == 5432
    assert T.render("{{l}}", vars_(l=["a", "b"]), T.STRICT) == ["a", "b"]
    assert T.render("{{host}}:{{port}}", vars_(host="h", port=5432), T.STRICT) == "h:5432"


def test_strict_drops_and_lenient_blanks():
    assert T.render("Bearer {{auth.token}}", vars_(), T.STRICT) is None
    assert T.render("Bearer {{auth.token}}", vars_(), T.LENIENT) == "Bearer "
    assert T.render("Bearer {{auth.token}}", vars_(auth={"token": "t"}), T.STRICT) == "Bearer t"


def test_filters():
    assert T.render("{{sslmode|default:require}}", vars_(), T.STRICT) == "require"
    assert T.render("{{host|default:{{account}}.snowflakecomputing.com}}", vars_(account="acct"),
                    T.STRICT) == "acct.snowflakecomputing.com"
    assert T.render("{{t|mul:1000}}", vars_(t=10), T.STRICT) == 10000
    assert T.render("{{p|add:1}}", vars_(p=0), T.STRICT) == 1
    assert T.render("{{s|removePrefix:jdbc:}}", vars_(s="jdbc:postgresql://h/db"), T.STRICT) == "postgresql://h/db"
    assert T.render("{{x|url}}", vars_(x="a b/c"), T.STRICT) == "a%20b%2Fc"
    assert T.render("{{x|join:;}}", vars_(x=["a", "b"]), T.STRICT) == "a;b"
    assert T.render("{{database|default:}}", vars_(), T.STRICT) == ""


def test_more_filters_and_text_forms():
    assert T.render("{{t|epochMs}}", vars_(t=1704067200123), T.STRICT) == "2024-01-01T00:00:00Z"
    assert T.render("{{t|mul:0.5}}", vars_(t=3), T.STRICT) == 1.5
    assert T.render("{{x|upper}}-{{x|lower}}", vars_(x="aB"), T.STRICT) == "AB-ab"
    assert T.render("{{x|json}}", vars_(x={"a": [1, True, None]}), T.STRICT) == '{"a":[1,true,null]}'
    assert T.render("{{x|base64}}", vars_(x="u:p"), T.STRICT) == "dTpw"
    assert T.render("n={{x}}", vars_(x=5.0), T.STRICT) == "n=5"
    assert T.render("n={{x}}", vars_(x=True), T.STRICT) == "n=true"
    assert T.render("{{x|url}}", vars_(x="~*.-_é"), T.STRICT) == "%7E*.-_%C3%A9"
    with pytest.raises(AgentFrontierError) as e:
        T.render("{{x|nope}}", vars_(x=1), T.STRICT)
    assert e.value.code == "INVALID_DEFINITION"


def test_file_filter_stages_content():
    files = T.TempFiles()
    path = T.render("{{k|file}}", vars_(k="secret-pem"), T.STRICT, files)
    with open(path, encoding="utf-8") as f:
        assert f.read() == "secret-pem"
    files.close()
    import os
    assert not os.path.exists(path)


def test_sql_binds_lists_identifiers_and_optional_blocks():
    r = T.render_sql(
        "select * from {{schema|ident}}.t where a = {{a}} and b in ({{bs}}) [[and c > {{after}}]] limit {{limit|int}}",
        vars_(schema='we"ird', a="x", bs=[1, 2], limit=50), '"')
    assert r.sql == 'select * from "we""ird".t where a = ? and b in (?, ?)  limit 50'
    assert r.binds == ["x", 1, 2]
    page2 = T.render_sql("x [[and c > {{after}}]]", vars_(after="m"), '"')
    assert page2.sql == "x and c > ?"
    assert T.render_sql("where a in ({{xs}})", vars_(xs=[]), '"').sql == "where a in (null)"
    pct = T.render_sql("like 'a%' and x = {{x}}", vars_(x=1), '"', lambda i: "%s", True, None)
    assert pct.sql == "like 'a%%' and x = %s"
    named = T.render_sql("a = {{a}} and b = {{b}}", vars_(a=1, b=2), '"', lambda i: ":p" + str(i), False, None)
    assert named.sql == "a = :p1 and b = :p2"
    with pytest.raises(AgentFrontierError):
        T.render_sql("{{n|int}}", vars_(n="1; drop"), '"')


# ------------------------------------------------------------------------------------------------- versions


def test_version_ranges():
    assert VersionRange.parse("<12").matches("11.22")
    assert not VersionRange.parse("<12").matches("12.0")
    assert VersionRange.parse(">=10 <12").matches("PostgreSQL 10.4 on x86")
    assert VersionRange.parse("<10 || >=16").matches("16.4")
    assert not VersionRange.parse("<10 || >=16").matches("14.1")
    assert VersionRange.parse("9.6").matches("9.6.24")
    assert not VersionRange.parse("9.6").matches("9.5")
    assert VersionRange.parse("9.x").matches("9.2")
    assert VersionRange.parse(">=8.0.13").matches("8.4.3")
    assert not VersionRange.parse(">=8.0.13").matches("8.0.12")
    assert not VersionRange.parse("<12").matches(None)


# ------------------------------------------------------------------------------------------------- mapping


def test_columns_map_to_canonical_fields_and_coerce(af):
    op = {"entity": "field"}
    row = {"DATASET": "T", "ORDINAL": "3", "NAME": "ID", "NULLABLE": "N", "DEFAULT_VALUE": None, "IS_GENERATED": "Y",
           "CHAR_SET": "AL32UTF8"}
    m = RecordMapper.map(op, [row], af.entity_model.entity("field"), ["schema"])[0]
    assert m["dataset"] == "T"
    assert m["ordinal"] == 3
    assert m["nullable"] is False
    assert m["isGenerated"] is True
    assert "defaultValue" not in m
    assert m["properties"] == {"char_set": "AL32UTF8"}


def test_explicit_fields_values_and_enums(af):
    op = json.loads('{"entity": "dataset", "fields": {"name": "table_name", "type": {"from": "kind", "values": '
                    '{"r": "table"}, "else": "other"}, "health": "health"}, "set": {"format": "heap"}, '
                    '"extras": "drop"}')
    row = {"table_name": "orders", "kind": "BASE TABLE", "health": "green", "junk": 1}
    m = RecordMapper.map(op, [row], af.entity_model.entity("dataset"), [])[0]
    assert m["name"] == "orders"
    assert m["type"] == "other"
    assert m["format"] == "heap"
    assert m["properties"] == {"health": "green"}, "explicit extras stay, unmapped ones drop"
    m2 = RecordMapper.map({"entity": "dataset"}, [{"name": "v", "type": "BASE TABLE"}],
                          af.entity_model.entity("dataset"), [])[0]
    assert m2["type"] == "table", "enum synonyms normalise BASE TABLE"


def test_collect_gathers_key_columns_in_order(af):
    op = json.loads('{"entity": "constraint", "collect": {"by": ["dataset", "name"], '
                    '"lists": ["columns", "refColumns"], "orderBy": "seq"}}')
    rows = [{"dataset": "t", "name": "fk", "type": "f", "columns": "b", "ref_columns": "y", "seq": 2},
            {"dataset": "t", "name": "fk", "type": "f", "columns": "a", "ref_columns": "x", "seq": 1},
            {"dataset": "t", "name": "pk", "type": "p", "columns": "id", "seq": 1}]
    mapped = RecordMapper.map(op, rows, af.entity_model.entity("constraint"), [])
    out = RecordMapper.collect(op["collect"], mapped)
    assert len(out) == 2
    assert out[0]["columns"] == ["a", "b"]
    assert out[0]["refColumns"] == ["x", "y"]
    assert out[0]["type"] == "foreign_key"
    assert "properties" not in out[0], "orderBy field is removed after collecting"


def test_json_path_and_tree_records():
    body = json.loads('{"idx": {"mappings": {"properties": {"a": {"type": "text"}, "b": {"properties": '
                      '{"c": {"type": "long"}}}}}}, "list": [{"k": 1}, {"k": 2}], "docs.count": "7"}')
    assert JsonPath.read(body, "$.list[*].k") == [1, 2]
    assert JsonPath.first(body, "$['docs.count']") == "7"
    recs = RecordMapper.extract(json.loads('{"path": "$.*.mappings.properties", "tree": {"children": "properties"}}'),
                                body)
    assert [r["name"] for r in recs] == ["a", "b", "b.c"]
    assert recs[2]["parent"] == "b"


def test_secrets_resolve_from_env_and_leave_unknown_schemes():
    s = Secrets([], {"PW": "s3cret"}.get)
    assert s.resolve("${env:PW}") == "s3cret"
    assert s.resolve("pre-${env:PW}-post") == "pre-s3cret-post"
    assert s.resolve("${vault:x}") == "${vault:x}"
    with pytest.raises(AgentFrontierError) as e:
        s.resolve("${env:MISSING}")
    assert "MISSING" in e.value.message


def test_path_globs():
    g = PathGlob(["**/*.csv"], ["**/tmp/**"])
    assert g.accepts("a.csv")
    assert g.accepts("x/y/a.CSV")
    assert not g.accepts("tmp/a.csv")
    assert not g.accepts("a.json")


# ------------------------------------------------------------------------------------------------- guard


def test_guard_accepts_reads_and_rejects_writes():
    assert SqlGuard.require_read_only("select 1;") == "select 1"
    assert SqlGuard.is_read_only("with x as (select 1) select * from x")
    assert SqlGuard.is_read_only("select * from t where note = 'drop table x; delete'")
    assert not SqlGuard.is_read_only("select 1; drop table x")
    assert not SqlGuard.is_read_only("delete from t")
    assert not SqlGuard.is_read_only("with d as (delete from t returning *) select * from d")
    assert not SqlGuard.is_read_only("select * into t2 from t")
    assert not SqlGuard.is_read_only("select pg_terminate_backend(1)")
    assert not SqlGuard.is_read_only("explain analyze select 1")
    assert SqlGuard.is_read_only("select $$ drop $$ as x")


def test_query_markers_follow_the_driver_paramstyle():
    sql = "select '?%' as a, x from t where y like 'a%' and z = ? -- what?\n and w = ?"
    assert convert_markers(sql, "qmark") == sql
    assert convert_markers(sql, "pyformat") == \
        "select '?%%' as a, x from t where y like 'a%%' and z = %s -- what?\n and w = %s"
    assert convert_markers(sql, "named").endswith("z = :p1 -- what?\n and w = :p2")
    assert convert_markers(sql, "numeric").endswith("z = :1 -- what?\n and w = :2")


# ------------------------------------------------------------------------------------------------- JSON parity


def test_json_output_matches_jackson():
    assert J.write({"a": 1, "b": [1.5, "x\u0001\n"], "c": None, "d": 5.0, "e": 1e20}) == \
        '{"a":1,"b":[1.5,"x\\u0001\\n"],"c":null,"d":5.0,"e":1.0E20}'
    assert J.pretty([{"a": 1, "b": []}, {}]) == '[ {\n  "a" : 1,\n  "b" : [ ]\n}, { } ]'
    assert J.jstr(0.001) == "0.001" and J.jstr(1e-4) == "1.0E-4" and J.jstr(123456.5) == "123456.5"
    assert J.jstr([1, None, {"k": True}]) == "[1, null, {k=true}]"


def test_page_cursor_is_base64url_json():
    c = encode_cursor(0, "orders")
    assert c == "eyJvIjowLCJ2Ijoib3JkZXJzIn0"
    assert decode_cursor(c) == (0, "orders")
    assert decode_cursor(None) == (0, None)
    with pytest.raises(AgentFrontierError) as e:
        decode_cursor("not a cursor!")
    assert e.value.code == "INVALID_CONFIG"
