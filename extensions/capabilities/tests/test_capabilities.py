"""agentfrontier-capabilities: SQL validation, NL-to-SQL with a scripted model (repair, policy, guarded execution,
evaluation), data-quality rules and their translations, dbt generation, LLM helpers and profiles."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentfrontier_capabilities import builtin_profiles, check_schema
from agentfrontier_capabilities.codegen import check_python, dbt_model_files, dejinja, to_dbt_refs
from agentfrontier_capabilities.dq import (Rule, TableShape, dump_yaml, recommend, to_dbt, to_great_expectations,
                                           to_soda, to_sql)
from agentfrontier_capabilities.llm import (AnthropicClient, AzureOpenAiClient, Budget, BudgetExceeded, Request,
                                            ScriptedClient, build_client, structured_output)
from agentfrontier_capabilities.nl2sql import Case, assemble_context, compare_results, evaluate, generate_sql
from agentfrontier_capabilities.sqlcheck import SchemaCatalog, referenced_tables, sqlglot_dialect, validate_sql

CAT = SchemaCatalog({
    "sales.customers": {"customer_id": "integer", "first_name": "text", "email": "text", "segment": "text"},
    "sales.orders": {"order_id": "integer", "customer_id": "integer", "order_date": "date", "status": "text",
                     "total_amount": "numeric"},
    "sales.order_items": {"order_id": "integer", "product_id": "integer", "quantity": "integer",
                          "line_total": "numeric"},
    "catalog.products": {"product_id": "integer", "name": "text", "list_price": "numeric"},
}, labels={"sales.customers": {"email": ["pii.email"]}})
RELS = [("sales.orders", ["customer_id"], "sales.customers", ["customer_id"]),
        ("sales.order_items", ["order_id"], "sales.orders", ["order_id"]),
        ("sales.order_items", ["product_id"], "catalog.products", ["product_id"])]

# ------------------------------------------------------------------------------------------------ validation


@pytest.mark.parametrize("sql, status, failing", [
    ("select o.status, count(*) from orders o group by o.status", "passed", None),
    ("with t as (select customer_id, sum(total_amount) s from sales.orders group by 1) "
     "select c.segment, sum(t.s) from t join sales.customers c using (customer_id) group by 1", "passed", None),
    ("select statuz from sales.orders", "failed", "columns"),
    ("select * from sales.ordrs", "failed", "tables"),
    ("delete from sales.orders", "failed", "read_only"),
    ("create table x as select 1", "failed", "read_only"),
    ("select 1; select 2", "failed", "single_statement"),
    ("selec 1 frm", "failed", "parse"),
    ("select * from sales.orders", "warnings", None),
    ("select o.order_id from sales.orders o, sales.customers c", "warnings", None),   # a comma join is a cross join
    ("select o.order_id from sales.orders o join sales.customers c", "warnings", None),
])
def test_validate_sql(sql: str, status: str, failing: str | None) -> None:
    r = validate_sql(sql, "postgres", CAT)
    assert r.status == status, r.to_json()
    assert r.ran is False
    if failing:
        assert [c.name for c in r.checks if c.status == "fail"] == [failing]


def test_validation_details() -> None:
    r = validate_sql("select statuz from sales.orders", "postgres", CAT)
    assert "did you mean status" in r.errors()[0]
    r = validate_sql("select * from sales.ordrs", "postgres", CAT)
    assert "did you mean sales.orders" in r.errors()[0]
    r = validate_sql("select status, count(*) from sales.orders group by status limit 5", "postgres", CAT,
                     target_dialect="tsql")
    assert r.transpiled and "TOP 5" in r.transpiled.upper()
    assert r.columns == ["sales.orders.status"]
    masked = validate_sql("select email from sales.customers", "postgres", CAT,
                          policy=lambda t, c, labels: (True, "", [{"type": "mask"}] if labels else []))
    assert masked.status == "warnings" and "masked" in masked.checks[-1].message
    denied = validate_sql("select email from sales.customers", "postgres", CAT,
                          policy=lambda t, c, labels: (False, "pii not allowed", []))
    assert denied.status == "failed" and "pii not allowed" in denied.errors()[0]
    plan = "Seq Scan on orders  (cost=0.00..1234.50 rows=65362 width=8)"
    r = validate_sql("select order_id from sales.orders", "postgres", CAT, explain=lambda s: [[plan]], max_cost=100)
    assert r.cost == {"startup": 0.0, "total": 1234.5, "rows": 65362} and r.status == "warnings"
    assert set(referenced_tables("with x as (select 1 from a.b) select * from x join c d on 1=1")) == {("a", "b"),
                                                                                                    (None, "c")}
    assert sqlglot_dialect("sqlserver") == "tsql" and sqlglot_dialect("mariadb") == "mysql"


# ------------------------------------------------------------------------------------------------ NL-to-SQL


def _shop(tmp: Path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp / "shop.db")
    c.executescript("""
        attach database ':memory:' as sales;
        create table sales.customers (customer_id integer primary key, first_name text, email text, segment text);
        create table sales.orders (order_id integer primary key, customer_id integer, order_date text, status text,
                                   total_amount real);
        insert into sales.customers values (1, 'Ada', 'a@x.org', 'enterprise'), (2, 'Alan', 'b@x.org', 'consumer');
        insert into sales.orders values (10, 1, '2025-01-05', 'delivered', 100), (11, 1, '2025-02-01', 'cancelled', 50),
                                        (12, 2, '2025-02-03', 'delivered', 70);
    """)
    return c


def test_context_assembly_with_policy() -> None:
    ctx = assemble_context("revenue by customer segment", CAT, RELS,
                           glossary=[{"name": "revenue", "definition": "delivered order totals",
                                      "expression": "sum(total_amount)"}],
                           allowed=lambda t: t != "catalog.products", column_allowed=lambda t, c: c != "email")
    assert ctx.tables[:2] == ["sales.customers", "sales.orders"] or "sales.customers" in ctx.tables
    assert "email" not in ctx.text and "revenue: delivered order totals" in ctx.text
    assert "sales.orders.customer_id = sales.customers.customer_id" in ctx.text
    ctx = assemble_context("product prices", CAT, RELS, allowed=lambda t: t != "catalog.products")
    assert ctx.excluded == ["catalog.products"] and "Not available to you by policy" in ctx.text


def test_generate_repairs_validates_and_runs(tmp_path: Path) -> None:
    db = _shop(tmp_path)
    model = ScriptedClient([json.dumps({"sql": "select segmnt, sum(total_amount) from sales.orders o "
                                               "join sales.customers c using (customer_id) group by 1"}),
                            "```json\n" + json.dumps({"sql": "select c.segment, sum(o.total_amount) as revenue "
                                                             "from sales.orders o join sales.customers c on "
                                                             "c.customer_id = o.customer_id where o.status <> "
                                                             "'cancelled' group by c.segment",
                                                      "assumptions": ["cancelled orders excluded"]}) + "\n```"])

    def run(sql: str) -> tuple[list[str], list[list]]:
        cur = db.execute(sql)
        return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]
    r = generate_sql("Revenue by customer segment", model, CAT, "sqlite", relationships=RELS, execute=run)
    assert [a["status"] for a in r.attempts] == ["failed", "passed"]
    assert "segmnt" in model.requests[1].messages[0].text() and "did you mean segment" in \
        model.requests[1].messages[0].text()
    assert r.executed and sorted(map(tuple, r.rows)) == [("consumer", 70.0), ("enterprise", 100.0)]
    m = r.to_json()["manifest"]
    assert m["validation"]["status"] == "passed" and m["model"]["provider"] == "scripted"
    assert m["limitations"] == ["cancelled orders excluded"] and m["outputs"][0]["path"] == "query.sql"

    # a write is never executed, however often the model insists
    calls: list[str] = []
    bad = ScriptedClient([json.dumps({"sql": "delete from sales.orders"})] * 2)
    r = generate_sql("Delete everything", bad, CAT, "sqlite", execute=lambda s: calls.append(s) or ([], []))
    assert not r.validation.ok and not r.executed and calls == []
    assert r.manifest.validation["status"] == "failed"


def test_evaluate(tmp_path: Path) -> None:
    db = _shop(tmp_path)
    cases = [Case("orders", "select count(*) from sales.orders"),
             Case("delivered revenue", "select sum(total_amount) from sales.orders where status = 'delivered'")]
    answers = {"orders": "select count(order_id) as n from sales.orders",
               "delivered revenue": "select sum(total_amount) from sales.orders"}
    res = evaluate(cases, answers.get, lambda s: db.execute(s).fetchall())
    assert (res["passed"], res["total"], res["accuracy"]) == (1, 2, 0.5)
    assert compare_results([(1, "a")], [("a", 1.0)])[0]
    assert compare_results([("x", 5)], [(9, "x", 5.0, 3)]) == (True, "same result (with extra columns)")
    assert not compare_results([("x", 5)], [(9, "x", 5)], extra_columns=False)[0]
    assert not compare_results([("x", 5)], [("x", 6)])[0] and not compare_results([("x", 5)], [])[0]


def test_retail_fixture_set_is_well_formed() -> None:
    cases = json.loads((Path(__file__).parent / "fixtures" / "retail_nl2sql.json").read_text(encoding="utf-8"))
    assert len(cases["cases"]) >= 12
    for c in cases["cases"]:
        r = validate_sql(c["reference_sql"], "postgres")
        assert r.ok, (c["question"], r.errors())


# ------------------------------------------------------------------------------------------------ data quality


def test_recommend_and_translate() -> None:
    t = TableShape("sales.orders", [("order_id", "integer", False), ("customer_id", "integer", False),
                                    ("status", "text", False), ("discount_pct", "numeric", True),
                                    ("channel", "text", True)],
                   primary_key=["order_id"], references=[(["customer_id"], "sales.customers", ["customer_id"])],
                   checks=["CHECK ((status = ANY (ARRAY['placed'::text, 'shipped'::text, 'cancelled'::text])))",
                           "CHECK (discount_pct >= 0)"],
                   values={"channel": ["web", "store"]})
    rules = recommend(t)
    kinds = [(r.kind, tuple(r.columns), r.origin) for r in rules]
    assert ("unique", ("order_id",), "constraint") in kinds and ("relationship", ("customer_id",), "constraint") in kinds
    assert ("accepted_values", ("status",), "constraint") in kinds and ("accepted_values", ("channel",), "profile") in kinds
    av = next(r for r in rules if r.kind == "accepted_values" and r.columns == ["status"])
    assert av.params["values"] == ["placed", "shipped", "cancelled"]
    rng = next(r for r in rules if r.kind == "range")
    assert rng.params == {"min": 0} and to_sql(rng).endswith("where discount_pct < 0")
    assert to_sql(next(r for r in rules if r.kind == "relationship")).startswith(
        "select count(*) as failures from sales.orders c where c.customer_id is not null and not exists")
    doc = to_dbt(rules, {"sales.orders": "fct_orders"})
    cols = {c["name"]: c["tests"] for c in doc["models"][0]["columns"]}
    assert cols["order_id"] == ["unique", "not_null"] and doc["models"][0]["name"] == "fct_orders"
    assert cols["customer_id"][1] == {"relationships": {"to": "source('sales', 'customers')", "field": "customer_id"}}
    y = dump_yaml(doc)
    assert "version: 2\nmodels:\n  - name: fct_orders\n" in y and "          - unique\n" in y
    ge = to_great_expectations(rules)
    assert {e["expectation_type"] for e in ge["expectations"]} >= {"expect_column_values_to_be_unique",
                                                                   "expect_column_values_to_be_in_set"}
    soda = to_soda(rules)
    assert soda.startswith("checks for orders:\n") and "  - duplicate_count(order_id) = 0\n" in soda


def test_rules_run_on_sqlite(tmp_path: Path) -> None:
    db = _shop(tmp_path)
    ok = Rule("accepted_values", "sales.orders", ["status"], {"values": ["delivered", "cancelled"]})
    bad = Rule("not_null", "sales.customers", ["segment"])
    bad2 = Rule("unique", "sales.orders", ["customer_id"])
    assert [db.execute(to_sql(r)).fetchone()[0] for r in (ok, bad, bad2)] == [0, 0, 1]


# ------------------------------------------------------------------------------------------------ codegen, llm, profiles


def test_dbt_generation_round_trip() -> None:
    sql = "select c.segment, count(*) as n from sales.orders o join sales.customers c on c.customer_id = o.customer_id group by 1"
    dbt = to_dbt_refs(sql, "postgres", {"sales.orders": "stg_orders"}, {"sales.customers": ("sales", "customers")})
    assert "{{ ref('stg_orders') }} o" in dbt and "{{ source('sales', 'customers') }} c" in dbt
    plain, notes = dejinja("{{ config(materialized='table') }}\n" + dbt, {"stg_orders": "sales.orders"})
    assert validate_sql(plain, "postgres", CAT).ok and notes == ["config() dropped"]
    files = dbt_model_files("fct_segment_orders", dbt, "models/marts", "Orders per segment", "table")
    assert list(files) == ["models/marts/fct_segment_orders.sql", "models/marts/fct_segment_orders.yml"]
    assert files["models/marts/fct_segment_orders.sql"].startswith("{{ config(materialized='table') }}")
    assert "description: Orders per segment" in files["models/marts/fct_segment_orders.yml"]
    assert check_python("x = 1\n") == (True, "compiles") and not check_python("def (:\n")[0]


def test_llm_helpers() -> None:
    m = ScriptedClient(["not json", json.dumps({"sql": "select 1"})])
    v, u = structured_output(m, "write sql", {"type": "object", "required": ["sql"]})
    assert v == {"sql": "select 1"} and "rejected" in m.requests[1].messages[0].text()
    b = Budget(daily_calls=2, task_calls=1)
    b.call(ScriptedClient(["a"]), Request(None, [], []), task="t1")
    with pytest.raises(BudgetExceeded):
        b.acquire("t1")
    b.acquire("t2")
    with pytest.raises(BudgetExceeded):
        b.acquire()
    assert isinstance(build_client("anthropic", None, None, "k"), AnthropicClient)
    az = build_client("azure_openai", "gpt-dep", "https://x.openai.azure.com", "k")
    assert isinstance(az, AzureOpenAiClient) and "model" in az.to_body(Request(None, [], []))
    with pytest.raises(ValueError):
        build_client("azure_openai", "dep", None, "k")


def test_profiles() -> None:
    p = builtin_profiles()
    assert set(p) == {"nl_to_sql", "validate_sql", "dq_rules", "dbt_model"}
    assert p["validate_sql"].deterministic and p["nl_to_sql"].needs_llm and p["nl_to_sql"].instructions
    assert p["nl_to_sql"].check_input({"question": "orders per month"}) == []
    assert p["nl_to_sql"].check_input({"question": 3}) == ["$.question: expected string"]
    assert check_schema({"type": "object", "required": ["a"]}, {}) == ["$.a: required"]
    assert p["dq_rules"].check_input({"table": "t", "format": "xml"})[0].startswith("$.format: must be one of")


def test_proposed_terms_are_inferred_and_grounded() -> None:
    from agentfrontier_capabilities.terms import propose_terms
    model = ScriptedClient([json.dumps({"terms": [
        {"name": "Net revenue", "definition": "Order totals without cancelled orders",
         "columns": ["sales.orders.total_amount", "sales.orders.status"], "expression": "sum(total_amount)",
         "confidence": 0.9},
        {"name": "Churn", "definition": "made up", "columns": ["sales.subscriptions.ended_at"]},
        {"name": "Revenue", "definition": "already known", "columns": ["sales.orders.total_amount"]}]})])
    terms = propose_terms(model, CAT, known=["Revenue"])
    assert [t["name"] for t in terms] == ["Net revenue"]
    assert terms[0]["assertion"] == "inferred" and terms[0]["reviewStatus"] == "pending"
    assert terms[0]["columns"] == ["sales.orders.total_amount", "sales.orders.status"]
