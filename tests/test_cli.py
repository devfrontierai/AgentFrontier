"""The CLI: same commands, options and JSON output shapes as the Java CLI."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agentfrontier.cli import main


@pytest.fixture()
def sqlite_conn(tmp_path):
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript("create table customers (id integer primary key, name text not null);"
                      "insert into customers values (1, 'Ann'), (2, 'Bob'), (3, 'Cy');"
                      "create view names as select name from customers;")
    con.close()
    f = tmp_path / "conn.json"
    f.write_text(json.dumps({"id": "cli-sqlite", "type": "sqlite", "properties": {"path": str(db)}}), encoding="utf-8")
    return f


def run(capsys, *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_help_and_usage(capsys):
    assert run(capsys)[0] == 2
    code, out, _ = run(capsys, "--help")
    assert code == 0 and "Usage: agentfrontier" in out
    code, _, err = run(capsys, "nope")
    assert code == 2 and "unknown command" in err
    code, _, err = run(capsys, "describe")
    assert code == 2 and "missing type" in err


def test_definitions_and_describe(capsys):
    code, out, _ = run(capsys, "definitions")
    defs = json.loads(out)
    assert code == 0 and {"postgresql", "sqlite", "local_files"} <= {d["id"] for d in defs}
    assert out.startswith('[ {\n  "id" : ')
    code, out, _ = run(capsys, "describe", "postgres")
    d = json.loads(out)
    assert d["id"] == "postgresql" and d["operations"]["fields"] == "field @ datasets"
    assert d["auth"][0]["fields"] == ["username", "password (secret)"]


def test_validate(capsys, sqlite_conn, tmp_path):
    code, out, _ = run(capsys, "validate")
    assert code == 0 and json.loads(out) == {"ok": True, "problems": []}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"type": "oracle", "auth": {"method": "username_password", "username": "x"}}))
    code, out, _ = run(capsys, "validate", str(sqlite_conn), str(bad))
    r = json.loads(out)
    assert code == 1 and not r["ok"] and any("service" in p for p in r["problems"])


def test_test_extract_query_sample(capsys, sqlite_conn, tmp_path):
    code, out, _ = run(capsys, "test", str(sqlite_conn))
    t = json.loads(out)
    assert code == 0 and t["ok"] and t["definition"] == "sqlite" and t["details"]["tables"] == 1
    assert list(t) == ["ok", "message", "definition", "product", "version", "variant", "latencyMs", "details"]

    target = tmp_path / "out"
    code, out, err = run(capsys, "extract", str(sqlite_conn), "--sink", "jsonl:" + str(target), "--drivers", "x")
    report = json.loads(out)
    assert code == 0 and report["status"] == "SUCCEEDED" and report["counts"]["dataset"] == 2
    assert "--drivers is used by the Java edition only" in err
    lines = (target / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"entity":"dataset","source":"cli-sqlite","name":"customers","type":"table"}'
    assert json.loads((target / "run.json").read_text(encoding="utf-8"))["report"]["status"] == "SUCCEEDED"

    code, out, _ = run(capsys, "query", str(sqlite_conn), "select id, name from customers order by id", "--max-rows",
                       "2")
    q = json.loads(out)
    assert code == 0 and q["rows"] == [[1, "Ann"], [2, "Bob"]] and q["truncated"] is True
    assert q["columns"][0] == {"name": "id", "typeName": "INTEGER", "category": "NUMBER"}
    code, _, err = run(capsys, "query", str(sqlite_conn), "delete from customers")
    assert code == 1 and err.startswith("QUERY_REJECTED")

    code, out, _ = run(capsys, "sample", str(sqlite_conn), "customers", "--limit", "1")
    assert code == 0 and json.loads(out) == [{"id": 1, "name": "Ann"}]


def test_extract_to_console(capsys, sqlite_conn):
    code, out, err = run(capsys, "extract", str(sqlite_conn))
    assert code == 0
    recs = [json.loads(x) for x in out.splitlines()]
    assert {r["entity"] for r in recs} >= {"source", "dataset", "field"}
    assert json.loads(err.strip().splitlines()[-1])["status"] == "SUCCEEDED"
