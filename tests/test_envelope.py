"""Raw-stream envelope (spec docs/envelope.md) and capability registry (spec docs/capabilities.md)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentfrontier import AgentFrontier, ConnectionConfig, Event, MemorySink, PipelineConfig, Record
from agentfrontier import cli
from agentfrontier.envelope import DEFAULT_TENANT, native_object_id

LEVELS = ["database", "schema"]
SPEC = Path(__file__).resolve().parents[1] / "src" / "agentfrontier" / "spec"


def rec(entity: str, **values) -> Record:
    v = {"source": "pg", "database": "db", "schema": "s"}
    v.update(values)
    return Record(entity, v)


@pytest.mark.parametrize("record, expected", [
    (Record("source", {"source": "pg"}), "pg"),
    (Record("container", {"source": "pg", "database": "db", "level": "database", "name": "db"}), "pg/db"),
    (rec("container", level="schema", name="s"), "pg/db/s"),
    (rec("dataset", name="t"), "pg/db/s/t"),
    (rec("field", dataset="t", name="c"), "pg/db/s/t/c"),
    (rec("index", dataset="t", name="i"), "pg/db/s/t#index/i"),
    (rec("constraint", dataset="t", name="pk"), "pg/db/s/t#constraint/pk"),
    (rec("routine", name="f"), "pg/db/s#routine/f"),
    (rec("policy", dataset="t", name="rls"), "pg/db/s/t#policy/rls"),
    (rec("policy", name="p"), "pg/db/s#policy/p"),
    (rec("principal", name="alice"), "pg/principal/alice"),
    (rec("lineage", dataset="v", upstreamDataset="t"), "pg/db/s/v<-pg/db/s/t"),
    (rec("lineage", dataset="v", field="a", upstreamSchema="raw", upstreamDataset="t", upstreamField="b"),
     "pg/db/s/v/a<-pg/db/raw/t/b"),
    (rec("grant", principal="alice", privilege="SELECT", dataset="t"), "pg/principal/alice->pg/db/s/t#grant/SELECT"),
    (rec("grant", principal="alice", privilege="UPDATE", dataset="t", field="c"),
     "pg/principal/alice->pg/db/s/t/c#grant/UPDATE"),
    (rec("grant", principal="alice", privilege="USAGE"), "pg/principal/alice->pg/db/s#grant/USAGE"),
    (rec("tag", dataset="t", field="c", name="pii"), "pg/db/s/t/c#tag/pii"),
    (rec("statistic", dataset="t", field="c"), "pg/db/s/t/c#statistic"),
    (Record("usage", {"source": "sf", "queryId": "01a"}), "sf/usage/01a"),
])
def test_native_object_id(record: Record, expected: str):
    assert native_object_id(record, LEVELS) == expected


def test_native_object_id_hashes_what_has_no_name():
    a = native_object_id(Record("row", {"source": "pg", "dataset": "t", "values": {"id": 1}}), [])
    b = native_object_id(Record("row", {"source": "pg", "dataset": "t", "values": {"id": 2}}), [])
    assert a.startswith("pg/t#row/") and len(a.rsplit("/", 1)[1]) == 16 and a != b
    u = native_object_id(Record("usage", {"source": "pg", "queryText": "select 1"}), [])
    assert u.startswith("pg/usage/") and len(u.rsplit("/", 1)[1]) == 16
    assert native_object_id(rec("business_term", name="Revenue"), LEVELS) == "pg/db/s#business_term/Revenue"


@pytest.fixture
def shop(tmp_path: Path) -> ConnectionConfig:
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript("create table customers(id integer primary key, name text);"
                      "create table orders(id integer primary key, customer_id integer references customers(id));"
                      "insert into customers values (1, 'Ada'), (2, 'Grace');")
    con.close()
    return ConnectionConfig.from_json({"id": "shop", "type": "sqlite", "properties": {"path": str(db)}})


def _run(conn: ConnectionConfig, **kw) -> list[Record]:
    config = PipelineConfig.of(conn, ["metadata", "data"], [{"type": "memory", "name": "env-test"}])
    for k, v in kw.items():
        setattr(config, k, v)
    report = AgentFrontier().run(config)
    assert report.status == "SUCCEEDED", report.errors
    out = MemorySink.get("env-test").records()
    MemorySink.forget("env-test")
    return out


def test_envelope_is_off_by_default(shop: ConnectionConfig):
    records = _run(shop)
    assert records and not any(isinstance(r, Event) for r in records)
    assert "envelopeVersion" not in records[0].to_json()


def test_events_wrap_records_and_are_stable_across_runs(shop: ConnectionConfig):
    first = _run(shop, envelope=True)
    second = _run(shop, envelope=True, tenant_id=None)
    assert all(isinstance(e, Event) for e in first)
    j = first[0].to_json()
    assert list(j)[:3] == ["envelopeVersion", "eventId", "tenantId"] and list(j)[-1] == "body"
    assert j["tenantId"] == DEFAULT_TENANT and j["connectorId"] == "sqlite" and j["sourceSystemId"] == "shop"
    assert first[0].entity == j["body"]["entity"], "events still read as their record"
    ids = sorted(e.envelope["eventId"] for e in first)
    assert ids == sorted(e.envelope["eventId"] for e in second), "re-reading an unchanged source gives the same ids"
    assert len(set(ids)) == len(ids)
    rows = [e for e in first if e.entity == "row"]
    assert rows and all(e.envelope["category"] == "data_read" and e.envelope["family"] == "data_streams"
                        for e in rows)
    other = _run(shop, envelope=True, tenant_id="acme")
    assert not set(ids) & {e.envelope["eventId"] for e in other}, "the tenant is part of the id"


def test_events_match_the_envelope_schema(shop: ConnectionConfig):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads((SPEC / "schemas" / "envelope.schema.json")
                                                           .read_text(encoding="utf-8")))
    for e in _run(shop, envelope=True):
        errors = [x.message for x in validator.iter_errors(e.to_json())]
        assert not errors, errors


def test_pipeline_json_reads_envelope_settings(shop: ConnectionConfig):
    p = PipelineConfig.from_json({"source": shop.to_json(), "envelope": True, "tenantId": "acme", "sinks": []})
    assert p.envelope and p.tenant_id == "acme"
    assert p.with_sinks([]).envelope and p.with_sinks([]).tenant_id == "acme"
    assert not PipelineConfig.from_json({"source": shop.to_json()}).envelope


def test_jsonl_sink_writes_events(shop: ConnectionConfig, tmp_path: Path):
    out = tmp_path / "out"
    config = PipelineConfig.of(shop, ["metadata"], [{"type": "jsonl", "path": str(out)}])
    config.envelope = True
    AgentFrontier().run(config)
    first = json.loads((out / "dataset.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["category"] == "metadata" and first["body"]["entity"] == "dataset"


def test_capabilities_are_derived_from_the_definition():
    af = AgentFrontier()
    caps = {r.string("operation"): r for r in af.capabilities("sqlite")}
    assert "test" not in caps
    assert caps["query"].string("producesEntity") == "row" and caps["query"].string("category") == "data_read"
    assert caps["fields"].string("category") == "metadata"
    assert all(r.string("access") == "read" and r.string("category") for r in caps.values())
    pg = {r.string("operation"): r for r in af.capabilities("postgres")}
    assert pg["lineage"].string("aspect") == "lineage" and pg["lineage"].string("category") == "lineage"
    assert pg["grants"].string("category") == "identity_authority"


def test_capabilities_read_the_declared_block(tmp_path: Path):
    (tmp_path / "pg2.json").write_text(json.dumps({
        "id": "pg2", "name": "PG with capabilities", "extends": "postgresql",
        "capabilities": {"apiVersion": "16", "limits": {"maxConcurrency": 4},
                         "operations": {"fields": {"incremental": ["watermark"], "comment": "by updated_at"}}}}),
        encoding="utf-8")
    caps = {r.string("operation"): r for r in AgentFrontier(definitions=[tmp_path]).capabilities("pg2")}
    assert caps["fields"].strings("incremental") == ["watermark"]
    assert caps["fields"].string("comment") == "by updated_at"
    assert caps["fields"].values["limits"] == {"maxConcurrency": 4} and caps["datasets"].string("apiVersion") == "16"


def test_cli_capabilities(capsys):
    assert cli.main(["capabilities", "sqlite"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any(c["operation"] == "query" for c in out)
