"""Phase 1 behaviour the conformance cases do not reach: checkpoint stores, failed runs and replay, reconciliation
cadence, file sinks, Parquet, parsed lineage, run health, managed identity tokens, the capability registry and the
command line."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from agentfrontier import (AgentFrontier, ConnectionConfig, FileCheckpointStore, MemoryCheckpointStore, MemorySink,
                           PipelineConfig)
from agentfrontier import template as T
from agentfrontier.auth import TokenService
from agentfrontier.cli import main as cli

sys.path.insert(0, str(Path(__file__).parent))
from fixture_server import FixtureServer  # noqa: E402


def _db(path: Path, script: str) -> None:
    c = sqlite3.connect(path)
    try:
        c.executescript(script)
        c.commit()
    finally:
        c.close()


SHOP = """
create table customers (id integer primary key, name text, email text);
create table orders (id integer primary key, customer_id integer references customers(id), total real);
create view big_orders as select o.id, o.total, c.email from orders o join customers c on c.id = o.customer_id
  where o.total > 100;
"""


def _run(conn: ConnectionConfig, store: str, sink: str, **kw):
    cfg = PipelineConfig.of(conn, kw.pop("aspects", ["metadata"]), [{"type": "memory", "name": sink}])
    cfg.envelope = True
    cfg.tenant_id = "t"
    cfg.incremental = {"checkpoints": {"type": "memory", "name": store}, **kw.pop("incremental", {})}
    for k, v in kw.items():
        setattr(cfg, k, v)
    report = AgentFrontier().run(cfg)
    events = MemorySink.get(sink).records()
    return report, events


@pytest.fixture
def shop(tmp_path: Path):
    db = tmp_path / "shop.db"
    _db(db, SHOP)
    conn = ConnectionConfig.from_json({"id": "shop", "type": "sqlite", "properties": {"path": str(db)}})
    name = "phase1-" + tmp_path.name
    yield db, conn, name
    MemoryCheckpointStore.forget(name)
    MemorySink.forget(name)


def test_to_epoch_ms_filter():
    v = T.Vars.of({"t": "2026-10-30T12:15:00.250+02:00", "n": 5})
    assert T.render("{{t|toEpochMs}}", v, T.STRICT) == 1793355300250
    assert T.render("{{n|toEpochMs}}", v, T.STRICT) == 5
    assert T.render("{{missing|toEpochMs}}", v, T.STRICT) is None


def test_file_checkpoint_store_round_trip(tmp_path: Path):
    store = FileCheckpointStore(tmp_path)
    assert store.load("acme/warehouse/nightly") is None
    store.save("acme/ware house/nightly", {"checkpointVersion": 1, "runs": 3})
    assert store.load("acme/ware house/nightly") == {"checkpointVersion": 1, "runs": 3}
    assert (tmp_path / "acme" / "ware%20house" / "nightly.json").exists()
    assert not list(tmp_path.rglob("*.tmp"))
    store.save("acme/warehouse/shop.v2", {"runs": 1})  # a dot in the pipeline name is kept
    assert (tmp_path / "acme" / "warehouse" / "shop.v2.json").exists()


def test_incremental_needs_the_envelope(shop):
    _, conn, name = shop
    cfg = PipelineConfig.of(conn, ["metadata"], [{"type": "memory", "name": name}])
    cfg.incremental = {"checkpoints": {"type": "memory", "name": name}}
    report = AgentFrontier().run(cfg)
    assert report.status == "FAILED"
    assert "envelope" in report.errors[0]


def test_failed_run_keeps_the_checkpoint_and_replays_the_same_events(shop):
    db, conn, name = shop
    first, _ = _run(conn, name, name)
    assert first.incremental["checkpoint"] == "committed"
    _db(db, "create table payments (id integer primary key, amount real); drop table customers;")
    failed, _ = _run(conn, name, name)  # the view now points at a missing table: the only scope fails
    assert failed.status == "FAILED"
    assert failed.incremental["checkpoint"] == "not committed"
    _db(db, "drop view big_orders;")
    a, events_a = _run(conn, name, name)
    assert a.status == "SUCCEEDED" and a.incremental["checkpoint"] == "committed"
    assert {e.envelope["op"] for e in events_a} == {"upsert", "tombstone"}
    # replaying from the same checkpoint gives exactly the same events (same eventIds)
    store = MemoryCheckpointStore(name)
    key = "t/shop/shop"
    MemoryCheckpointStore.forget(name)
    _run(conn, name, name, incremental={"full": True})
    _db(db, "create table refunds (id integer primary key);")
    snapshot = store.load(key)
    _, once = _run(conn, name, name)
    store.save(key, snapshot)
    _, twice = _run(conn, name, name)
    assert [e.envelope["eventId"] for e in once] == [e.envelope["eventId"] for e in twice]


def test_reconcile_every(shop):
    _, conn, name = shop
    seen = [_run(conn, name, name, incremental={"reconcileEvery": 2})[0].incremental["reconcile"] for _ in range(5)]
    assert seen == [True, False, True, False, True]


def test_partitioned_jsonl_and_csv_sinks(shop, tmp_path: Path):
    _, conn, name = shop
    cfg = PipelineConfig.of(conn, ["metadata"], [{"type": "jsonl", "path": str(tmp_path / "j"),
                                                  "partitionBy": ["tenant", "category"]},
                                                 {"type": "csv", "path": str(tmp_path / "c"),
                                                  "partitionBy": ["source"]}])
    cfg.envelope = True
    cfg.tenant_id = "acme"
    assert AgentFrontier().run(cfg).status == "SUCCEEDED"
    assert (tmp_path / "j" / "tenant=acme" / "category=metadata" / "dataset.jsonl").exists()
    assert (tmp_path / "j" / "run.json").exists()
    header = (tmp_path / "c" / "source=shop" / "field.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("eventId,op,entity,nativeObjectId,")


def test_parquet_sink_and_inference(shop, tmp_path: Path):
    pq = pytest.importorskip("pyarrow.parquet")
    _, conn, name = shop
    cfg = PipelineConfig.of(conn, ["metadata"], [{"type": "parquet", "path": str(tmp_path / "p")}])
    assert AgentFrontier().run(cfg).status == "SUCCEEDED"
    table = pq.read_table(tmp_path / "p" / "field.parquet")
    assert table.num_rows == 9
    assert str(table.schema.field("ordinal").type) == "int64"
    # the files engine reads the schema back from the footer, and rows from the file
    files = ConnectionConfig.from_json({"id": "pq", "type": "local_files", "properties": {"root": str(tmp_path / "p")}})
    out = PipelineConfig.of(files, ["metadata"], [{"type": "memory", "name": name + "-pq"}])
    AgentFrontier().run(out)
    fields = [r for r in MemorySink.get(name + "-pq").records("field") if r.get("dataset") == "field.parquet"]
    assert {r.get("name") for r in fields} >= {"source", "dataset", "name", "ordinal", "dataType"}
    with AgentFrontier().open(files) as s:
        from agentfrontier import Scope
        rows = s.sample(Scope.of({"folder": "."}), "field.parquet", 2)
    assert len(rows) == 2 and rows[0].get("source") == "shop"
    MemorySink.forget(name + "-pq")


def test_parsed_lineage_from_views(shop):
    pytest.importorskip("sqlglot")
    _, conn, name = shop
    cfg = PipelineConfig.of(conn, ["metadata", "lineage"], [{"type": "memory", "name": name}])
    cfg.envelope = True
    cfg.lineage = {"parse": True}
    AgentFrontier().run(cfg)
    lineage = [e for e in MemorySink.get(name).records("lineage")]
    got = {(e.get("field"), e.get("upstreamDataset"), e.get("upstreamField"), e.get("transformation"),
            e.envelope["assertion"], e.envelope["confidence"]) for e in lineage}
    assert got == {("id", "orders", "id", "identity", "parsed", 0.9),
                   ("total", "orders", "total", "identity", "parsed", 0.9),
                   ("email", "customers", "email", "identity", "parsed", 0.9)}


def test_run_health_records(shop):
    _, conn, name = shop
    report, events = _run(conn, name, name, run_health=True)
    health = {e.get("check"): e for e in events if e.entity == "quality_result"}
    assert set(health) == {"run.status", "run.records", "run.duration", "run.errors", "run.warnings", "run.retries"}
    assert health["run.status"].get("status") == "pass" and health["run.status"].get("value") == 1.0
    assert health["run.records"].get("value") == float(sum(v for k, v in report.counts.items()
                                                           if k != "quality_result"))
    assert health["run.errors"].get("status") == "pass"
    # health records are events: emitted again on every run, never diffed or tombstoned
    _, again = _run(conn, name, name, run_health=True)
    assert {e.entity for e in again} == {"quality_result"}


def test_managed_identity_token(tmp_path: Path):
    (tmp_path / "token.json").write_text(json.dumps({"access_token": "mi-token", "expires_on": "4102444800",
                                                     "token_type": "Bearer"}), encoding="utf-8")
    server = FixtureServer(tmp_path, {"routes": [{
        "path": "/msi", "require": {"headers": {"Metadata": "true"}},
        "query": {"api-version": "2018-02-01", "resource": "https://storage.azure.com", "client_id": "uami"},
        "body": "token.json"}]})
    try:
        t = TokenService().token("azure_managed_identity",
                                 {"clientId": "uami", "identityEndpoint": server.base_url + "/msi"},
                                 {"scope": "https://storage.azure.com/.default"}, T.Vars.empty())
        assert t == {"accessToken": "mi-token", "tokenType": "Bearer"}
        # without a scope from the definition the provider asks for nothing
        assert TokenService().token("azure_managed_identity", {}, None, T.Vars.empty()) == {}
    finally:
        server.close()


def test_capabilities_report_incremental_modes():
    caps = {r.get("operation"): r.get("incremental") for r in AgentFrontier().capabilities("snowflake")}
    assert caps["usage"] == ["watermark"]
    assert caps["fields"] == ["full_diff"]


def test_cli_incremental_extract(shop, tmp_path: Path, capsys):
    db, _, _ = shop
    conn_file = tmp_path / "conn.json"
    conn_file.write_text(json.dumps({"id": "shop", "type": "sqlite", "properties": {"path": str(db)}}),
                         encoding="utf-8")
    args = ["extract", str(conn_file), "--sink", "jsonl:" + str(tmp_path / "out"), "--incremental",
            str(tmp_path / "state"), "--tenant", "acme"]
    assert cli(args) == 0
    assert cli(args) == 0
    report = json.loads((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))["report"]
    # the sinks see the report before the checkpoint is committed
    assert report["incremental"]["upserts"] == 0 and report["incremental"]["checkpoint"] == "pending"
    checkpoint = json.loads((tmp_path / "state" / "acme" / "shop" / "shop.json").read_text(encoding="utf-8"))
    assert checkpoint["runs"] == 2 and checkpoint["checkpointVersion"] == 1
