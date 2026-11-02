"""Phase 3: graph stores and builders with access filters and history, and the sinks that deliver translated runs
(OpenLineage, OpenMetadata, DataHub, OPA, OTLP, FOCUS, OCSF) against a fixture server."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from agentfrontier import AgentFrontier, ConnectionConfig, MemoryCheckpointStore, PipelineConfig
from agentfrontier.graph import ALLOW_ALL, GraphSink, JsonGraphStore, policy_access
from agentfrontier.governance.policy import RulePDP

sys.path.insert(0, str(Path(__file__).parent))
from fixture_server import FixtureServer  # noqa: E402

SHOP = """
create table customers (id integer primary key, email text);
create table orders (id integer primary key, customer_id integer references customers(id), total real);
create table secrets (id integer primary key, api_key text);
create view v_big as select o.id, o.total, c.email from orders o join customers c on c.id = o.customer_id
  where o.total > 100;
"""


@pytest.fixture
def shop(tmp_path: Path):
    db = tmp_path / "shop.db"
    c = sqlite3.connect(db)
    c.executescript(SHOP)
    c.close()
    return db, ConnectionConfig.from_json({"id": "shop", "type": "sqlite", "properties": {"path": str(db)}})


def _run(conn, sinks, **kw):
    cfg = PipelineConfig.of(conn, kw.pop("aspects", ["metadata", "lineage"]), sinks)
    for k, v in kw.items():
        setattr(cfg, k, v)
    report = AgentFrontier().run(cfg)
    assert report.status == "SUCCEEDED", report.errors
    return report


def test_graph_from_a_run_with_access_filter(shop, tmp_path: Path):
    pytest.importorskip("sqlglot")
    _, conn = shop
    _run(conn, [{"type": "graph", "store": "memory", "name": "g-" + tmp_path.name}], lineage={"parse": True},
         envelope=True)
    g = GraphSink.named("g-" + tmp_path.name)
    types = {n["type"] for n in g.nodes()}
    assert types == {"Source", "Dataset", "Field"}
    fk = [e for e in g.edges("REFERENCES")]
    assert [(e["from"], e["to"]) for e in fk] == [("shop/orders", "shop/customers")]
    up = g.traversal(["shop/v_big/email"], ["DERIVED_FROM"], "out", 3, ALLOW_ALL)
    assert up == [["shop/v_big/email", "shop/customers/email"]]
    parsed = next(e for e in g.edges("DERIVED_FROM") if e["from"] == "shop/v_big/total")
    assert parsed["assertion"] == "parsed" and parsed["confidence"] == 0.9
    # a policy that hides customers hides its node, its edges, and whatever lies behind it
    pdp = RulePDP({"rules": [{"id": "no-customers", "effect": "deny",
                              "match": {"resource.name": "customers"}}]})
    access = policy_access(pdp, "alice", [])
    hood = g.neighborhood("shop/v_big", 1, "out", ["DERIVED_FROM"], access)
    assert [n["id"] for n in hood["nodes"]] == ["shop/orders", "shop/v_big"]
    assert all("customers" not in e["to"] for e in hood["edges"])
    with pytest.raises(Exception, match="access filter"):
        g.neighborhood("shop/v_big", 1, "out", None, None)


def test_tombstones_close_nodes_and_keep_history(shop, tmp_path: Path):
    db, conn = shop
    path = tmp_path / "graph.json"
    store = "graph-" + tmp_path.name
    sinks = [{"type": "graph", "store": "json", "path": str(path)}]
    inc = {"checkpoints": {"type": "memory", "name": store}}
    first = _run(conn, sinks, envelope=True, incremental=inc)
    c = sqlite3.connect(db)
    c.executescript("drop table secrets;")
    c.close()
    second = _run(conn, sinks, envelope=True, incremental=inc)
    MemoryCheckpointStore.forget(store)
    g = JsonGraphStore(path)
    secrets = g.node("shop/secrets")
    assert secrets is not None and secrets["validTo"] and secrets["validFrom"]
    assert g.node("shop/secrets/api_key")["validTo"]
    assert "shop/secrets" not in [n["id"] for n in g.neighborhood("shop", 1, "out", None, ALLOW_ALL)["nodes"]]
    old = g.neighborhood("shop", 1, "out", None, ALLOW_ALL, history=True)
    assert "shop/secrets" in [n["id"] for n in old["nodes"]]
    then = g.as_of(secrets["validFrom"])
    assert then.node("shop/secrets") is not None and "validTo" not in then.node("shop/secrets")
    assert first.run_id != second.run_id


def test_translator_sinks_to_files(shop, tmp_path: Path):
    _, conn = shop
    sinks = [{"type": "openlineage", "path": str(tmp_path / "ol.jsonl")},
             {"type": "datahub", "path": str(tmp_path / "dh.jsonl"), "options": {"env": "DEV"}},
             {"type": "openmetadata", "path": str(tmp_path / "om.jsonl")}]
    pytest.importorskip("sqlglot")
    _run(conn, sinks, envelope=True, tenant_id="acme", lineage={"parse": True})
    ol = [json.loads(x) for x in (tmp_path / "ol.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["job"]["name"] for e in ol] == ["v_big"]
    assert {i["name"] for i in ol[0]["inputs"]} == {"orders", "customers"}
    dh = [json.loads(x) for x in (tmp_path / "dh.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(m["aspectName"] == "upstreamLineage" for m in dh)
    assert all(m["entityUrn"].endswith(",DEV)") for m in dh)
    om = [json.loads(x) for x in (tmp_path / "om.jsonl").read_text(encoding="utf-8").splitlines()]
    assert om[0]["body"] == {"name": "shop", "serviceType": "SQLite"}
    with pytest.raises(Exception, match="url' or 'path"):
        AgentFrontier().sink({"type": "openlineage"})


def test_translator_sinks_over_http(shop, tmp_path: Path):
    pytest.importorskip("sqlglot")
    _, conn = shop
    (tmp_path / "ok.json").write_text("{}", encoding="utf-8")
    (tmp_path / "table.json").write_text('{"id": "11111111-1111-4111-8111-111111111111"}', encoding="utf-8")
    routes = {"require": {"headers": {"Authorization": "Bearer t0k"}}, "routes": [
        {"method": "POST", "path": "/api/v1/lineage", "body": "ok.json"},
        {"method": "POST", "path": "/gms/aspects", "query": {"action": "ingestProposal"}, "body": "ok.json"},
        {"method": "PUT", "path": "/om/api/v1/services/databaseServices", "body": "ok.json"},
        {"method": "PUT", "path": "/om/api/v1/databases", "body": "ok.json"},
        {"method": "PUT", "path": "/om/api/v1/databaseSchemas", "body": "ok.json"},
        {"method": "PUT", "path": "/om/api/v1/tables", "body": "ok.json"},
        {"method": "PUT", "path": "/om/api/v1/lineage", "body": "ok.json"},
        {"path": "/om/api/v1/tables/name/shop.default.default.v_big", "body": "table.json"},
        {"path": "/om/api/v1/tables/name/shop.default.default.orders", "body": "table.json"},
        {"path": "/om/api/v1/tables/name/shop.default.default.customers", "body": "table.json"},
        {"method": "PUT", "path": "/opa/v1/data/agentfrontier/shop", "body": "ok.json"},
        {"method": "POST", "path": "/otel/v1/logs", "body": "ok.json"}]}
    server = FixtureServer(tmp_path, routes)
    try:
        base = server.base_url
        sinks = [{"type": "openlineage", "url": base, "token": "t0k"},
                 {"type": "datahub", "url": base + "/gms", "token": "t0k"},
                 {"type": "openmetadata", "url": base + "/om/api", "token": "t0k"},
                 {"type": "opa", "url": base + "/opa", "token": "t0k"},
                 {"type": "otlp", "url": base + "/otel", "token": "t0k"}]
        _run(conn, sinks, envelope=True, run_health=True, aspects=["metadata", "lineage", "governance"],
             lineage={"parse": True})
        req = server.requests
        assert sum(r.startswith("POST /api/v1/lineage") for r in req) == 1
        assert sum(r.startswith("POST /gms/aspects") for r in req) >= 10
        assert sum(r.startswith("PUT /om/api/v1/tables") for r in req) == 4
        assert sum(r.startswith("PUT /om/api/v1/lineage") for r in req) == 2
        assert any(r.startswith("GET /om/api/v1/tables/name/") for r in req)
        assert "PUT /opa/v1/data/agentfrontier/shop" in req and "POST /otel/v1/logs" in req
    finally:
        server.close()


def test_focus_and_ocsf_files(tmp_path: Path):
    from agentfrontier.model import Record
    from agentfrontier.sinks import RunContext
    from agentfrontier.sinks.translated import FocusSink, OcsfSink
    af = AgentFrontier()
    with pytest.raises(Exception, match="path"):
        FocusSink({"type": "focus", "url": "http://x"}, af)
    f = FocusSink({"type": "focus", "path": str(tmp_path / "focus.csv")}, af)
    o = OcsfSink({"type": "ocsf", "path": str(tmp_path / "ocsf.jsonl")}, af)
    for s in (f, o):
        s.begin(RunContext("r1", "p", "src", None, None, [], []))
        s.write([Record("usage_cost", {"source": "src", "provider": "Anthropic", "billedCost": 1.5,
                                       "chargePeriodStart": "2026-10-30T00:00:00Z", "chargeCategory": "usage"}),
                 Record("audit_event", {"source": "src", "actor": "a", "action": "read", "outcome": "success",
                                        "occurredAt": "2026-10-30T00:00:00Z"})])
        s.end(type("R", (), {"started_at": "2026-10-30T00:00:00Z", "finished_at": "2026-10-30T00:00:01Z"})())
    lines = (tmp_path / "focus.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("BilledCost,") and lines[1].startswith("1.5,") and len(lines) == 2
    assert json.loads((tmp_path / "ocsf.jsonl").read_text(encoding="utf-8"))["class_uid"] == 6003


def test_dbt_project_graph(tmp_path: Path):
    project = Path(__file__).parent / "conformance" / "files-dbt" / "project"
    conn = ConnectionConfig.from_json({"id": "dbt", "type": "dbt", "properties": {"projectDir": str(project)}})
    _run(conn, [{"type": "graph", "store": "memory", "name": "dbt-" + tmp_path.name}],
         aspects=["metadata", "lineage", "context"])
    g = GraphSink.named("dbt-" + tmp_path.name)
    assert {n["props"]["name"] for n in g.nodes("Metric")} == {"revenue", "average_order_value"}
    paths = g.traversal(["dbt/analytics/marts/fct_orders"], ["DERIVED_FROM"], "out", 5, ALLOW_ALL)
    assert ["dbt/analytics/marts/fct_orders", "dbt/analytics/staging/stg_orders", "dbt/raw_db/shop_raw/orders"] in paths
