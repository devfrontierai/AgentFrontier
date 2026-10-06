"""Mirrors the Java DatabaseIT: the same assertions against real databases loaded with the shop fixture (the
``testing/`` docker compose of the Java repository). A database that is not reachable, or whose driver extra is not
installed, is skipped."""

from __future__ import annotations

import importlib.util
import json
import socket
from dataclasses import dataclass

import pytest

from agentfrontier import (AgentFrontier, AgentFrontierError, AuthConfig, ConnectionConfig, PipelineConfig,
                           QueryRequest, Record, Scope)

pytestmark = pytest.mark.database


@dataclass
class Target:
    name: str
    host: str
    port: int
    module: str
    connection: ConnectionConfig
    shop: Scope
    audit: Scope
    version_prefix: str
    lineage: bool
    policies: bool
    legacy: bool


def _targets() -> list[Target]:
    t = []
    for port, major in ((55496, 9), (55411, 11), (55414, 14), (55417, 17)):
        t.append(Target("postgresql-" + str(major), "localhost", port, "psycopg",
                        ConnectionConfig.builder("postgresql").id("it-pg" + str(major))
                        .property("host", "localhost").property("port", port).property("database", "shopdb")
                        .auth(AuthConfig.username_password("af_reader", "af_reader_pw")).build(),
                        Scope.of("database", "shopdb", "schema", "shop"), Scope.of("database", "shopdb", "schema", "audit"),
                        str(major), True, True, major < 12))
    t.append(Target("mysql-8.4", "localhost", 53306, "pymysql",
                    ConnectionConfig.builder("mysql").id("it-mysql")
                    .property("host", "localhost").property("port", 53306).property("databases", ["shop", "audit"])
                    .property("allowPublicKeyRetrieval", True).property("sslMode", "DISABLED")
                    .auth(AuthConfig.username_password("af_reader", "af_reader_pw")).build(),
                    Scope.of("database", "shop"), Scope.of("database", "audit"), "8.4", True, False, False))
    t.append(Target("oracle-23", "localhost", 51521, "oracledb",
                    ConnectionConfig.builder("oracle").id("it-oracle")
                    .property("host", "localhost").property("port", 51521).property("service", "FREEPDB1")
                    .option("includeSchemas", ["SHOP", "AUDIT_LOG"])
                    .auth(AuthConfig.username_password("af_reader", "af_reader_pw")).build(),
                    Scope.of("schema", "SHOP"), Scope.of("schema", "AUDIT_LOG"), "23", True, False, False))
    return t


TARGETS = _targets()


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def lc(s: str | None) -> str | None:
    return None if s is None else s.lower()


def one(records: list[Record], dataset: str, name: str) -> Record:
    for r in records:
        if lc(r.string("dataset")) == dataset and lc(r.string("name")) == name:
            return r
    raise AssertionError("no " + dataset + "." + name + " in " + str(records))


def lower(items: list) -> list:
    return [lc(x) for x in items]


@pytest.mark.parametrize("t", TARGETS, ids=[t.name for t in TARGETS])
def test_database(t: Target, tmp_path):
    if importlib.util.find_spec(t.module) is None:
        pytest.skip(t.module + " not installed (pip install the matching agentfrontier extra)")
    if not _reachable(t.host, t.port):
        pytest.skip(t.name + " not running; start testing/docker-compose.yml")
    af = AgentFrontier()
    test = af.test(t.connection)
    if not test.ok and "ORA-12514" in (test.message or ""):
        pytest.skip("database still starting: " + str(test.message))
    assert test.ok, t.name + ": " + str(test.message)
    assert test.version.startswith(t.version_prefix), "detected version " + str(test.version)

    with af.open(t.connection) as s:
        # hierarchy
        parent = t.shop.up_to(s.hierarchy[0].level) if t.shop.depth > 1 else Scope.root()
        containers = {lc(r.string("name")) for r in s.containers(parent)}
        assert lc(t.shop.values()[-1]) in containers, "containers " + str(containers)

        # datasets, three per page
        page = s.datasets(t.shop, None, 3)
        datasets = list(page.records)
        cursor = page.next_cursor
        while cursor is not None:
            p = s.datasets(t.shop, cursor, 3)
            datasets.extend(p.records)
            cursor = p.next_cursor
        types = {lc(r.string("name")): r.string("type") for r in datasets}
        assert types.get("customers") == "table"
        assert types.get("order_totals") == "view"
        assert {"products", "orders", "order_items"} <= set(types), types
        assert "events_2024" not in types, "partitions are skipped by default"
        names = [r.string("name") for r in datasets]

        # fields
        fields = s.records("field", t.shop, names)
        assert one(fields, "customers", "id").bool("isGenerated") is True
        assert one(fields, "customers", "email").bool("nullable") is False
        assert one(fields, "customers", "email").string("comment") == "Login email"
        if not t.legacy:
            assert one(fields, "products", "price_with_tax").bool("isGenerated") is True
        assert one(fields, "customers", "id").int_value("ordinal") == 1

        # keys and indexes
        keys = s.records("constraint", t.shop, names)
        pk = next(r for r in keys if lc(r.string("dataset")) == "order_items" and r.string("type") == "primary_key")
        assert lower(pk.strings("columns")) == ["order_id", "line_no"]
        fk = next(r for r in keys if lc(r.string("dataset")) == "orders" and r.string("type") == "foreign_key")
        assert lc(fk.string("refDataset")) == "customers"
        assert lower(fk.strings("refColumns")) == ["id"]
        indexes = s.records("index", t.shop, names)
        assert lower(one(indexes, "orders", "idx_orders_customer").strings("columns")) == ["customer_id"]

        # cross-schema foreign key
        audit_names = [r.string("name") for r in s.datasets(t.audit, None, 100).records]
        audit_fk = next(r for r in s.records("constraint", t.audit, audit_names) if r.string("type") == "foreign_key")
        assert lc(audit_fk.string("refDataset")) == "customers"
        shop_name = lc(t.shop.values()[-1])
        ref_level = s.hierarchy[-1].level
        assert lc(audit_fk.string("ref" + ref_level[:1].upper() + ref_level[1:])) == shop_name

        # lineage
        if t.lineage:
            upstream = {lc(r.string("upstreamDataset")) for r in s.records("lineage", t.shop)
                        if lc(r.string("dataset")) == "order_totals"}
            assert upstream == {"orders", "order_items", "products"}
        # governance
        if t.policies:
            policy = s.records("policy", t.shop)[0]
            assert policy.string("name") == "gold_only"
            assert policy.string("type") == "row_access"
        assert any("af_reader" in lc(r.string("principal")) for r in s.records("grant", t.shop)), "grants to af_reader"

        # data
        products = next(n for n in names if n.lower() == "products")
        assert len(s.sample(t.shop, products, 5)) == 2
        qualified = ("SHOP" if t.connection.type == "oracle" else "shop") + ".orders"
        q = s.query(QueryRequest("select count(*) as n from " + qualified, t.shop, 10, 30, []))
        assert int(q.rows[0][0]) == 2
        assert q.columns[0].category.value == "NUMBER"
        with pytest.raises(AgentFrontierError):
            s.query(QueryRequest.of("delete from " + qualified))
        # bound parameters and the row cap
        q2 = s.query(QueryRequest("select id from " + qualified + " where id >= ? order by id", t.shop, 1, 30, [0]))
        assert q2.row_count == 1 and q2.truncated
        assert s.stats.snapshot().statements > 5

    # the whole source through a pipeline, with every aspect
    out = tmp_path / ("out-" + t.name)
    p = PipelineConfig.of(t.connection, ["metadata", "lineage", "governance", "statistics"],
                          [{"type": "jsonl", "path": str(out)}])
    r = af.run(PipelineConfig(p.connection, p.aspects, None, None, p.sinks, 2, 2, 2, 0))
    assert r.status == "SUCCEEDED", r.errors
    assert r.count("dataset") >= 6, r.counts
    assert r.count("field") >= 20, r.counts
    assert (out / "field.jsonl").stat().st_size > 0
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["report"]["counts"] is not None
