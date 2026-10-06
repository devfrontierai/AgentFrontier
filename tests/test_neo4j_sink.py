"""The neo4j sink against a running Neo4j (skipped when not reachable or the driver is not installed).

The server may be shared with other applications, so the test writes only nodes of its own unique ``source`` id,
uses a copy of the default mapping without its ``setup`` statements (no schema constraints are created), and
deletes everything it wrote afterwards.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sqlite3
import uuid

import pytest

from agentfrontier import AgentFrontier, ConnectionConfig, PipelineConfig
from agentfrontier.model import Record
from agentfrontier.sinks.uids import row as uid_row

pytestmark = pytest.mark.neo4j

URI = os.environ.get("AF_TEST_NEO4J_URI", "neo4j://127.0.0.1:7687")
USER = os.environ.get("AF_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("AF_TEST_NEO4J_PASSWORD")


def _reachable() -> bool:
    if not PASSWORD:
        return False
    try:
        with socket.create_connection(("127.0.0.1", 7687), timeout=0.5):
            return True
    except OSError:
        return False


def test_uids_follow_the_shared_scheme():
    levels = ["database", "schema"]
    base = {"source": "s", "database": "d", "schema": "sc"}
    assert uid_row(Record("container", {"source": "s", "database": "d", "level": "database", "name": "d"}),
                   levels)["uid"] == "s/d"
    c = uid_row(Record("container", {**base, "level": "schema", "name": "sc"}), levels)
    assert (c["uid"], c["parentUid"]) == ("s/d/sc", "s/d")
    assert uid_row(Record("dataset", {**base, "name": "t"}), levels)["uid"] == "s/d/sc/t"
    assert uid_row(Record("field", {**base, "dataset": "t", "name": "id"}), levels)["uid"] == "s/d/sc/t/id"
    fk = uid_row(Record("constraint", {**base, "dataset": "t", "name": "fk", "columns": ["a"], "refDataset": "u",
                                       "refSchema": "other", "refColumns": ["id"]}), levels)
    assert fk["refUid"] == "s/d/other/u" and fk["refColumnUids"] == ["s/d/other/u/id"]
    assert fk["columnUids"] == ["s/d/sc/t/a"]
    lin = uid_row(Record("lineage", {**base, "dataset": "v", "upstreamDataset": "t"}), levels)
    assert lin["upstreamUid"] == "s/d/sc/t"
    assert uid_row(Record("principal", {"source": "s", "name": "bob"}), levels)["uid"] == "s/principal/bob"
    p = uid_row(Record("dataset", {**base, "name": "t", "properties": {"x": {"a": 1}, "name": "no"}}), levels)["props"]
    assert p == {"entity": "dataset", "source": "s", "database": "d", "schema": "sc", "name": "t", "x": '{"a":1}'}


def test_neo4j_sink_writes_and_cleans_up(tmp_path):
    if importlib.util.find_spec("neo4j") is None:
        pytest.skip("neo4j driver not installed")
    if not _reachable():
        pytest.skip("Neo4j not running on 127.0.0.1:7687")
    import neo4j

    af = AgentFrontier()
    source = "af-py-test-" + uuid.uuid4().hex[:12]
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript("create table customers (id integer primary key, email text not null);"
                      "create table orders (id integer primary key, customer_id integer references customers(id));"
                      "create view v as select o.id from orders o;")
    con.close()
    mapping = af.spec.read("sinks/neo4j-default.json")
    mapping["setup"] = []
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps(mapping), encoding="utf-8")
    conn = ConnectionConfig("sqlite", {"path": str(db)}, id=source)
    sink = {"type": "neo4j", "uri": URI, "username": USER, "password": PASSWORD, "mapping": str(mapping_file),
            "removeStale": True}
    driver = neo4j.GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        try:
            driver.verify_connectivity()
        except Exception as e:  # noqa: BLE001
            pytest.skip("Neo4j not usable: " + str(e))
        report = af.run(PipelineConfig.of(conn, ["metadata"], [sink]))
        assert report.status == "SUCCEEDED", report.errors
        with driver.session() as s:
            def count(q: str) -> int:
                return s.run(q, source=source).single()[0]
            assert count("MATCH (n:Source {uid: $source}) RETURN count(n)") == 1
            assert count("MATCH (n:Dataset) WHERE n.source = $source RETURN count(n)") == 3
            assert count("MATCH (:Dataset {uid: $source + '/customers'})-[:HAS_FIELD]->(f:Field) RETURN count(f)") == 2
            assert count("MATCH (:Dataset {uid: $source + '/orders'})-[:REFERENCES]->"
                         "(:Dataset {uid: $source + '/customers'}) RETURN count(*)") == 1
            assert count("MATCH (f:Field {uid: $source + '/customers/id'}) WHERE f.isPrimaryKey RETURN count(f)") == 1
    finally:
        with driver.session() as s:
            s.run("MATCH (n) WHERE n.source = $source OR n.uid = $source OR n.uid STARTS WITH $source + '/' "
                  "DETACH DELETE n", source=source).consume()
            left = s.run("MATCH (n) WHERE n.source = $source OR n.uid STARTS WITH $source + '/' RETURN count(n)",
                         source=source).single()[0]
        driver.close()
    assert left == 0
