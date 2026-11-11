"""agentfrontier-synth: generation with foreign-key integrity and scenarios (AVERO.md workflow B2), determinism,
exports loaded into SQLite with its own foreign-key check, masking operators and privacy and utility reports."""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal

import pytest

from agentfrontier_synth import (ColumnSpec, FixtureSpec, GenerationError, Scenario, TableSpec, exact_match_share,
                                 export, fixture_manifest, generate, k_anonymity, utility_report, validate_dataset)
from agentfrontier_synth.checks import ks_distance, tv_distance
from agentfrontier_synth.operators import (MemoryTokenVault, detokenize, format_preserving, generalise, hash_value,
                                           mask_rows, redact, tokenize)


def shop(rows: int = 5000, seed: int = 7) -> FixtureSpec:
    customers = TableSpec("customers", [
        ColumnSpec("customer_id", "integer", False), ColumnSpec("first_name", "text", False),
        ColumnSpec("last_name", "text", False), ColumnSpec("email", "text", False, unique=True),
        ColumnSpec("segment", "text", False, values=["consumer", "small_business", "enterprise"],
                   weights=[0.7, 0.2, 0.1]),
        ColumnSpec("signup_date", "date", False, min="2023-01-01", max="2025-12-31"),
        ColumnSpec("referred_by", "integer", True)],
        rows=rows // 5, primary_key=["customer_id"], foreign_keys=[(["referred_by"], "customers", ["customer_id"])])
    orders = TableSpec("orders", [
        ColumnSpec("order_id", "integer", False), ColumnSpec("customer_id", "integer", False),
        ColumnSpec("order_date", "date", False, min="2024-01-01", max="2025-12-31"),
        ColumnSpec("status", "text", False, values=["placed", "shipped", "delivered", "cancelled", "refunded"],
                   weights=[0.1, 0.2, 0.65, 0.05, 0.0]),
        ColumnSpec("total_amount", "numeric(12,2)", False, min=0),
        ColumnSpec("shipped_at", "timestamp", True, min="2024-01-01", max="2025-12-31", null_share=0.1)],
        rows=rows, primary_key=["order_id"], foreign_keys=[(["customer_id"], "customers", ["customer_id"])])
    items = TableSpec("order_items", [
        ColumnSpec("order_id", "integer", False), ColumnSpec("line_number", "integer", False, min=1, max=4),
        ColumnSpec("quantity", "integer", False, min=1, max=5),
        ColumnSpec("line_total", "numeric(12,2)", False, min=1, max=400)],
        rows=rows * 2, primary_key=["order_id", "line_number"], foreign_keys=[(["order_id"], "orders", ["order_id"])])
    payments = TableSpec("payments", [
        ColumnSpec("payment_id", "integer", False), ColumnSpec("order_id", "integer", False),
        ColumnSpec("method", "text", False, values=["card", "paypal", "bank_transfer"]),
        ColumnSpec("status", "text", False, values=["captured", "failed"], weights=[1, 0]),
        ColumnSpec("amount", "numeric(12,2)", False, min=1, max=900)],
        rows=rows, primary_key=["payment_id"], foreign_keys=[(["order_id"], "orders", ["order_id"])])
    return FixtureSpec([payments, items, orders, customers], seed, [
        Scenario("orders", "refunds", share=0.03, set={"status": "refunded"}),
        Scenario("orders", "duplicate submissions", count=25, duplicate=True),
        Scenario("payments", "failed payments", share=0.04, set={"status": "failed"})],
        [{"type": "sum", "table": "orders", "column": "total_amount", "from": "order_items.line_total",
          "key": "order_id"}])


def test_generation_keeps_integrity_and_scenarios() -> None:
    spec = shop()
    data, report = generate(spec)
    assert report["order"] == ["customers", "orders", "order_items", "payments"]
    assert report["rows"]["orders"] == 5025 and report["rows"]["customers"] == 1000
    v = validate_dataset(spec, data)
    assert v["ok"], [c for c in v["checks"] if c["status"] == "fail"]
    st = [o["status"] for o in data["orders"]]
    assert st.count("refunded") >= 150 and sum(1 for p in data["payments"] if p["status"] == "failed") >= 200
    dupes = data["orders"][-25:]
    assert all(any(o["customer_id"] == d["customer_id"] and o["order_date"] == d["order_date"]
                   and o["order_id"] != d["order_id"] for o in data["orders"][:-25]) for d in dupes)
    totals = {}
    for it in data["order_items"]:
        totals[it["order_id"]] = totals.get(it["order_id"], Decimal(0)) + it["line_total"]
    assert all(o["total_amount"] == totals.get(o["order_id"], Decimal("0.00")) for o in data["orders"][:200])
    assert all(o["email"].endswith("@example.test") for o in data["customers"])
    assert any(c["referred_by"] is not None for c in data["customers"])


def test_same_seed_same_data_and_spec_round_trip() -> None:
    a, _ = generate(shop(200))
    b, _ = generate(FixtureSpec.from_json(shop(200).to_json()))
    c, _ = generate(shop(200, seed=8))
    assert a == b and a != c
    assert shop(200).digest() == FixtureSpec.from_json(shop(200).to_json()).digest()


def test_exports_load_into_sqlite_with_valid_keys() -> None:
    spec = shop(300)
    data, report = generate(spec)
    files = export(spec, data, report["order"], "sql", "sqlite")
    db = sqlite3.connect(":memory:")
    db.executescript("""
        pragma foreign_keys = on;
        create table customers (customer_id integer primary key, first_name text, last_name text, email text unique,
            segment text, signup_date text, referred_by integer references customers(customer_id));
        create table orders (order_id integer primary key, customer_id integer references customers(customer_id),
            order_date text, status text, total_amount real, shipped_at text);
        create table order_items (order_id integer references orders(order_id), line_number integer, quantity integer,
            line_total real, primary key (order_id, line_number));
        create table payments (payment_id integer primary key, order_id integer references orders(order_id),
            method text, status text, amount real);
    """)
    db.executescript(files["fixture.sql"])
    assert db.execute("pragma foreign_key_check").fetchall() == []
    assert db.execute("select count(*) from orders").fetchone()[0] == report["rows"]["orders"]
    csvs = export(spec, data, report["order"], "csv")
    assert list(csvs)[0] == "customers.csv" and csvs["customers.csv"].splitlines()[0].startswith("customer_id,")
    m = fixture_manifest(spec, report, validate_dataset(spec, data), csvs).to_json()
    assert m["validation"]["status"] == "passed" and len(m["outputs"]) == 4 and m["inputs"][0]["seed"] == 7


def test_generation_errors() -> None:
    cyc = FixtureSpec([TableSpec("a", [ColumnSpec("id", "integer"), ColumnSpec("b_id", "integer")],
                                 foreign_keys=[(["b_id"], "b", ["id"])], primary_key=["id"]),
                       TableSpec("b", [ColumnSpec("id", "integer"), ColumnSpec("a_id", "integer")],
                                 foreign_keys=[(["a_id"], "a", ["id"])], primary_key=["id"])])
    with pytest.raises(GenerationError, match="cycle"):
        generate(cyc)
    tight = FixtureSpec([TableSpec("t", [ColumnSpec("v", "integer", unique=True, min=1, max=3)], rows=10)])
    with pytest.raises(GenerationError, match="unique"):
        generate(tight)


def test_masking_operators() -> None:
    vault = MemoryTokenVault()
    t = tokenize("jane@example.com", "k1", vault)
    assert t == tokenize("jane@example.com", "k1") and t != tokenize("jane@example.com", "k2")
    assert detokenize(t, vault) == "jane@example.com" and t.startswith("tok_")
    fp = format_preserving("AB-1234-x", "k")
    assert len(fp) == 9 and fp[2] == "-" and fp[3:7].isdigit() and fp[:2].isupper() and fp != "AB-1234-x"
    assert fp == format_preserving("AB-1234-x", "k")
    assert redact("4111111111111111", 4) == "************1111" and len(hash_value("x", "s")) == 16
    assert generalise(dt.date(2025, 3, 17)) == dt.date(2025, 3, 1) and generalise(37, bucket=10) == 30
    rows = mask_rows([{"email": "a@b.c", "phone": "555-1234", "n": 1}], {"email": "tokenize", "phone": "format_preserving"},
                     "key")
    assert rows[0]["n"] == 1 and rows[0]["email"].startswith("tok_") and rows[0]["phone"][3] == "-"
    with pytest.raises(ValueError):
        mask_rows([{"a": 1}], {"a": "shred"})


def test_privacy_and_utility_reports() -> None:
    data, _ = generate(shop(500))
    k = k_anonymity(data["customers"], ["segment"])
    assert k["k"] >= 5 and k["groups"] == 3
    assert k_anonymity(data["customers"], ["email"])["k"] == 1
    real = [{"email": "user1.555@example.test", "segment": "consumer"}]
    assert exact_match_share(data["customers"], real, ["email"]) <= 0.01
    assert ks_distance([1, 2, 3], [1, 2, 3]) == 0 and ks_distance([1, 2], [5, 6]) == 1
    assert tv_distance("aab", "aab") == 0 and tv_distance("aa", "bb") == 1
    u = utility_report(data["orders"], data["orders"][:1000], ["total_amount", "status"])
    assert u["total_amount"]["metric"] == "ks" and u["status"]["metric"] == "total_variation"
    assert u["status"]["distance"] < 0.1
