"""Governance (spec docs/policy.md, classification.md, authority.md, audit.md): the shared vectors in
conformance/governance, the pipeline and session enforcement points, the authority graph, the OPA adapter and the
CLI commands."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agentfrontier import AgentFrontier
from agentfrontier.cli import main as cli
from agentfrontier.governance import (ApprovalRequired, AuditLog, AuthorityGraph, Decision, FailClosedPDP,
                                      Governance, OpaPDP, PolicyDenied, RuleClassifier, RulePDP, mask_value,
                                      reconcile, verify, verify_directory)
from agentfrontier.governance.audit import chain_hash
from agentfrontier.model import ConnectionConfig, Scope
from agentfrontier.pipeline import PipelineConfig

VECTORS = Path(__file__).parent / "conformance" / "governance"


def _vectors(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ shared vectors


@pytest.mark.parametrize("case", _vectors("policy-decisions.json")["cases"], ids=lambda c: c["name"])
def test_policy_decision_vectors(case: dict) -> None:
    pdp = RulePDP(_vectors("policy-decisions.json")["rules"])
    d = pdp.decide(case["request"])
    want = case["expected"]
    assert d.decision == want["decision"]
    assert d.obligations == want["obligations"]
    assert d.policy_version == want["policyVersion"]
    assert [e.get("rule") for e in d.evidence] == want["rules"]


@pytest.mark.parametrize("case", _vectors("masking.json")["cases"], ids=lambda c: c["method"] + ":" + str(c["value"]))
def test_masking_vectors(case: dict) -> None:
    assert mask_value(case["value"], case["method"], case.get("keep", 4), case.get("salt", "")) == case["expected"]


@pytest.mark.parametrize("case", _vectors("classification.json")["cases"], ids=lambda c: c["name"])
def test_classification_vectors(case: dict) -> None:
    hits = RuleClassifier.builtin().classify(case["name"], case["comment"], case["values"])
    got = [{"label": h.label, "labelType": h.label_type, "value": h.value, "confidence": h.confidence,
            "evidence": h.evidence} for h in hits]
    assert got == case["expected"]


def test_classification_rule_version_matches_vectors() -> None:
    assert RuleClassifier.builtin().versions["pii.email"] == _vectors("classification.json")["ruleVersion"]


def test_audit_chain_vector() -> None:
    v = _vectors("audit-chain.json")
    for e in v["events"]:
        assert chain_hash(e) == e["hash"]
    ok = verify(v["events"], v["manifest"], v["key"], v["runId"])
    assert ok.ok, ok.problems
    assert verify(v["events"], v["manifest"], "wrong-key", v["runId"]).ok is False
    for t in v["tampered"]:
        events = [dict(e) for e in v["events"]]
        if "set" in t:
            events[t["event"] - 1].update(t["set"])
        if "remove" in t:
            del events[t["remove"] - 1]
        if "swap" in t:
            a, b = t["swap"]
            events[a - 1]["sequence"], events[b - 1]["sequence"] = events[b - 1]["sequence"], events[a - 1]["sequence"]
        assert verify(events, v["manifest"], v["key"], v["runId"]).ok is False, t["name"]


# ------------------------------------------------------------------------------------------------ policy details


def test_obligations_the_sdk_cannot_meet_deny() -> None:
    rules = {"rules": [
        {"id": "needs-approval", "effect": "allow", "match": {"resource.dataset": "payroll"},
         "obligations": [{"type": "approval", "reason": "payroll needs HR sign-off"}]},
        {"id": "exotic", "effect": "allow", "match": {"resource.dataset": "x"},
         "obligations": [{"type": "watermark"}]},
        {"id": "advice", "effect": "allow", "match": {"resource.dataset": "y"},
         "obligations": [{"type": "watermark", "advice": True}]}]}
    gov = Governance(RulePDP(rules), subject="alice")
    with pytest.raises(ApprovalRequired, match="HR sign-off"):
        gov.authorize("sample", gov.resource("dataset", "s", dataset="payroll"))
    with pytest.raises(PolicyDenied, match="not understood"):
        gov.authorize("sample", gov.resource("dataset", "s", dataset="x"))
    assert gov.authorize("sample", gov.resource("dataset", "s", dataset="y")).allowed


def test_a_failing_pdp_fails_closed() -> None:
    class Broken:
        def decide(self, request: dict) -> Decision:
            raise RuntimeError("down")

    req = {"subject": "a", "action": "read", "resource": {"type": "dataset"}}
    assert FailClosedPDP(Broken()).decide(req).decision == "deny"
    relaxed = FailClosedPDP(Broken(), on_unavailable="allow")
    assert relaxed.decide(req).decision == "allow"
    assert relaxed.decide(dict(req, action="write")).decision == "deny"


def test_bad_rules_are_rejected_up_front() -> None:
    from agentfrontier.errors import AgentFrontierError
    with pytest.raises(AgentFrontierError, match="unknown policy condition"):
        RulePDP({"rules": [{"id": "r", "effect": "deny", "match": {"action": {"regex": "x"}}}]})
    with pytest.raises(AgentFrontierError, match="duplicate"):
        RulePDP({"rules": [{"id": "r", "effect": "deny"}, {"id": "r", "effect": "allow"}]})


# ------------------------------------------------------------------------------------------------ OPA


class _Opa(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Opa.seen.append((self.path, self.headers.get("Authorization"), body))
        dataset = body["input"]["resource"].get("dataset")
        result: object = {"allow": dataset != "secret",
                          "obligations": [{"type": "mask", "fields": ["email"]}] if dataset == "customers" else [],
                          "policyVersion": "rego-7"}
        if dataset == "plain":
            result = True
        out = json.dumps({"result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def test_opa_adapter() -> None:
    srv = HTTPServer(("127.0.0.1", 0), _Opa)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:" + str(srv.server_address[1])
        opa = OpaPDP(url, "af/decision", token="t0k")
        d = opa.decide({"subject": "alice", "action": "sample", "resource": {"type": "dataset", "dataset": "customers"}})
        assert d.allowed and d.obligations == [{"type": "mask", "fields": ["email"]}]
        assert d.policy_version == "rego-7"
        assert not opa.decide({"subject": "a", "action": "read", "resource": {"dataset": "secret"}}).allowed
        assert opa.decide({"subject": "a", "action": "read", "resource": {"dataset": "plain"}}).allowed
        path, auth, body = _Opa.seen[0]
        assert path == "/v1/data/af/decision" and auth == "Bearer t0k"
        assert body["input"]["subject"] == {"id": "alice"}
    finally:
        srv.shutdown()
    down = FailClosedPDP(OpaPDP("http://127.0.0.1:1", timeout=0.5))
    d = down.decide({"subject": "a", "action": "read", "resource": {"type": "dataset"}})
    assert d.decision == "deny" and "unavailable" in d.reason


# ------------------------------------------------------------------------------------------------ authority graph


def _access_records() -> list[dict]:
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    return [
        {"entity": "principal", "source": "shop", "name": "alice", "type": "user", "memberOf": ["analysts"]},
        {"entity": "principal", "source": "shop", "name": "analysts", "type": "role", "memberOf": ["reporting"]},
        {"entity": "principal", "source": "shop", "name": "reporting", "type": "role", "memberOf": ["analysts"]},
        {"entity": "principal", "source": "shop", "name": "admin", "type": "user", "superuser": True},
        {"entity": "principal", "source": "shop", "name": "bob", "type": "user"},
        {"entity": "grant", "source": "shop", "schema": "sales", "principal": "reporting", "privilege": "SELECT",
         "objectType": "table", "dataset": "orders"},
        {"entity": "grant", "source": "shop", "schema": "public", "principal": "PUBLIC", "privilege": "SELECT",
         "objectType": "table", "dataset": "notices"},
        {"entity": "grant", "source": "shop", "schema": "sales", "principal": "alice", "privilege": "UPDATE",
         "objectType": "table", "dataset": "orders"},
        {"entity": "delegation", "source": "shop", "principal": "alice", "delegate": "support-bot",
         "delegateType": "agent", "scope": "SELECT:sales.*", "validTo": future},
        {"entity": "delegation", "source": "shop", "principal": "admin", "delegate": "support-bot",
         "delegateType": "agent", "scope": "*", "validTo": past},
        {"entity": "agent_permission", "source": "shop", "agent": "support-bot", "privilege": "READ",
         "resource": "tickets", "resourceType": "dataset"},
    ]


def test_effective_authority_with_paths() -> None:
    g = AuthorityGraph.from_records(_access_records())
    alice = {(e.privilege, e.resource): e for e in g.effective("alice")}
    assert alice[("SELECT", "sales.orders")].path == ("alice", "analysts", "reporting")
    assert alice[("SELECT", "sales.orders")].via == "group"
    assert alice[("SELECT", "public.notices")].via == "public"
    assert alice[("UPDATE", "sales.orders")].via == "direct"
    bot = {(e.privilege, e.resource, e.via) for e in g.effective("support-bot")}
    # the delegation passes on SELECT on sales.* only; the expired one passes on nothing
    assert ("SELECT", "sales.orders", "delegation") in bot
    assert not any(r == "public.notices" or p == "UPDATE" or v == "superuser" for p, r, v in bot)
    assert ("READ", "tickets", "permission") in bot
    who = {e.principal for e in g.who_can("SELECT", "sales.orders")}
    assert {"alice", "analysts", "reporting", "support-bot", "admin"} <= who and "bob" not in who
    graph = g.export()
    kinds = {e["kind"] for e in graph["edges"]}
    assert {"member_of", "granted", "delegated_to", "permitted", "may_access"} <= kinds
    rels = g.relationships()
    assert any(r.get("predicate") == "may_access" and r.get("subject") == "alice" for r in rels)


def test_rule_pdp_with_native_grants() -> None:
    pdp = RulePDP(None, AuthorityGraph.from_records(_access_records()))
    def ask(who: str, dataset: str, schema: str = "sales", action: str = "sample") -> str:
        return pdp.decide({"subject": who, "action": action, "resource": {
            "type": "dataset", "source": "shop", "path": {"schema": schema}, "dataset": dataset}}).decision
    assert ask("alice", "orders") == "allow"
    assert ask("bob", "orders") == "deny"
    assert ask("bob", "notices", "public") == "allow"  # PUBLIC
    assert ask("admin", "anything") == "allow"  # superuser
    assert ask("bob", "orders", action="read") == "allow"  # metadata reads are not native-gated
    d = pdp.decide({"subject": "bob", "action": "query", "resource": {"type": "dataset", "dataset": "orders",
                                                                       "path": {"schema": "sales"}}})
    assert d.evidence[0]["source"] == "native-grants"


# ------------------------------------------------------------------------------------------------ reconciliation


def test_reconcile_modes() -> None:
    base = {"entity": "label", "source": "shop", "schema": "sales", "objectType": "field", "dataset": "customers",
            "field": "email"}
    src = dict(base, name="sensitivity", value="internal", labelType="sensitivity")
    purview = dict(base, name="sensitivity", value="confidential", labelType="sensitivity", engine="purview")
    rules = dict(base, name="pii.email", value="confidential", labelType="classification",
                 engine="agentfrontier-rules")
    rejected = dict(base, name="pii.phone", labelType="classification", engine="agentfrontier-rules",
                    reviewStatus="rejected")
    expired = dict(base, name="legal_hold", labelType="retention", expiresAt="2000-01-01T00:00:00Z")
    labels = [src, purview, rules, rejected, expired]

    def eff(mode: str, **kw: object) -> set:
        return {(x["name"], x.get("value")) for x in reconcile(labels, mode, **kw).effective}

    union = reconcile(labels)
    assert {(x["name"], x.get("value")) for x in union.effective} == {("sensitivity", "internal"),
                                                                       ("pii.email", "confidential")}
    assert union.conflicts[0]["loser"]["engine"] == "purview"
    assert eff("source_authoritative") == {("sensitivity", "internal"), ("pii.email", "confidential")}
    assert eff("engine_authoritative", engines=["purview"]) == {("sensitivity", "confidential"),
                                                                ("pii.email", "confidential")}
    assert eff("advisory_only") == {("sensitivity", "internal")}
    assert eff("union_with_precedence", precedence=["purview", "source"]) == {("sensitivity", "confidential"),
                                                                              ("pii.email", "confidential")}


# ------------------------------------------------------------------------------------------------ enforcement


@pytest.fixture()
def shop(tmp_path: Path) -> ConnectionConfig:
    db = tmp_path / "shop.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, full_name TEXT, email TEXT, phone_number TEXT,
                                card_number TEXT, city TEXT);
        INSERT INTO customers VALUES (1, 'Ada Lovelace', 'ada@example.com', '+44 20-7946-0958', '4111111111111111',
                                      'London');
        INSERT INTO customers VALUES (2, 'Alan Turing', 'alan@example.org', '+44 161-496-0000', '5500000000000004',
                                      'Manchester');
        CREATE TABLE api_keys (id INTEGER PRIMARY KEY, service TEXT, api_key TEXT);
        INSERT INTO api_keys VALUES (1, 'github', 'ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
        CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL);
        INSERT INTO products VALUES (1, 'Widget', 9.5);
    """)
    con.commit()
    con.close()
    return ConnectionConfig.from_json({"id": "shop", "type": "sqlite", "properties": {"path": str(db)}})


RULES = {"version": "shop-1", "rules": [
    {"id": "no-secrets", "effect": "deny",
     "match": {"action": ["sample", "query"], "resource.labels": {"anyOf": ["secret.*"]}}},
    {"id": "mask-pii", "effect": "allow", "match": {"action": ["sample", "query"]},
     "obligations": [{"type": "mask", "labels": ["pii.*"], "method": "hash"},
                     {"type": "mask", "labels": ["pci.*"], "method": "partial"}]},
    {"id": "no-raw-sql-on-keys", "effect": "deny",
     "match": {"action": "query", "resource.statement": {"glob": "*api_keys*"}}}]}


def _lines(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def test_pipeline_classifies_masks_withholds_and_audits(shop: ConnectionConfig, tmp_path: Path) -> None:
    out = tmp_path / "out"
    cfg = PipelineConfig(shop, ["metadata", "data"], sinks=[{"type": "jsonl", "path": str(out)}],
                         governance={"subject": "analyst", "purpose": "analytics", "policy": {"rules": RULES},
                                     "classify": True, "audit": {"key": "k1"}})
    report = AgentFrontier().run(cfg)
    assert report.status == "SUCCEEDED", report.errors

    labels = {(x["dataset"], x["field"], x["name"]): x for x in _lines(out / "label.jsonl")}
    assert labels[("customers", "email", "pii.email")]["evidence"] == "name,values:2/2"
    assert labels[("customers", "email", "pii.email")]["engine"] == "agentfrontier-rules"
    assert ("customers", "full_name", "pii.person_name") in labels
    assert ("customers", "card_number", "pci.card_number") in labels
    assert ("api_keys", "api_key", "secret.credential") in labels
    assert not any(k[1] in ("city", "id") for k in labels)

    rows = {(r["dataset"], r["values"]["id"]): r["values"] for r in _lines(out / "row.jsonl")}
    ada = rows[("customers", 1)]
    assert ada["email"].startswith("sha256:") and ada["full_name"].startswith("sha256:")
    assert ada["card_number"] == "************1111" and ada["city"] == "London"
    assert rows[("products", 1)] == {"id": 1, "name": "Widget", "price": 9.5}
    assert not any(ds == "api_keys" for ds, _ in rows)  # denied before it was sampled
    assert any("policy denied sample of api_keys" in w for w in report.warnings)

    assert report.policy is not None and report.policy["deniedRequests"] >= 1
    assert report.count("label") == len(labels)
    v = verify_directory(out, "k1")
    assert v.ok, v.problems
    events = _lines(out / "audit_event.jsonl")
    actions = [e["action"] for e in sorted(events, key=lambda e: e["sequence"])]
    assert actions[0] == "run.start" and actions[-1] == "run.finish"
    assert any(e["action"] == "sample" and e["outcome"] == "denied" for e in events)
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["report"]["audit"]["events"] == len(events)

    # tamper with one event: the chain no longer verifies
    tampered = [dict(e) for e in events]
    tampered[1]["records"] = 999
    (out / "audit_event.jsonl").write_text("".join(json.dumps(e) + "\n" for e in tampered), encoding="utf-8")
    assert not verify_directory(out, "k1").ok
    assert cli(["audit-verify", str(out)]) == 1


def test_pipeline_with_envelope_marks_labels_inferred(shop: ConnectionConfig, tmp_path: Path) -> None:
    out = tmp_path / "out"
    cfg = PipelineConfig(shop, ["metadata"], sinks=[{"type": "jsonl", "path": str(out)}], envelope=True,
                         governance={"classify": True})
    assert AgentFrontier().run(cfg).status == "SUCCEEDED"
    ev = _lines(out / "label.jsonl")
    assert ev and all(e["assertion"] == "inferred" and e["category"] == "classification_labels" for e in ev)
    email = next(e for e in ev if e["body"]["field"] == "email")
    assert email["confidence"] == email["body"]["confidence"] == 0.7


def test_governance_off_changes_nothing(shop: ConnectionConfig, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    AgentFrontier().run(PipelineConfig(shop, ["metadata", "data"], sinks=[{"type": "jsonl", "path": str(a)}]))
    AgentFrontier().run(PipelineConfig(shop, ["metadata", "data"], sinks=[{"type": "jsonl", "path": str(b)}],
                                       governance={"policy": {"rules": {"rules": []}}}))
    for f in sorted(a.glob("*.jsonl")):
        assert f.read_text(encoding="utf-8") == (b / f.name).read_text(encoding="utf-8"), f.name
    assert not (a / "audit_event.jsonl").exists() and not (b / "audit_event.jsonl").exists()


def test_operation_denied_before_it_runs(shop: ConnectionConfig, tmp_path: Path) -> None:
    out = tmp_path / "out"
    rules = {"rules": [{"id": "no-indexes", "effect": "deny", "match": {"resource.entity": "index"}}]}
    r = AgentFrontier().run(PipelineConfig(shop, ["metadata"], sinks=[{"type": "jsonl", "path": str(out)}],
                                           governance={"policy": {"rules": rules}, "audit": True}))
    assert r.count("index") == 0 and r.count("field") > 0
    assert any("policy denied operation" in w for w in r.warnings)
    assert verify_directory(out).ok


def test_governed_session(shop: ConnectionConfig) -> None:
    log = AuditLog("session-1", "shop")
    gov = Governance(RulePDP(RULES), subject="analyst", purpose="analytics", classifier=True, audit=log)
    with AgentFrontier().open(shop, governance=gov) as s:
        rows = s.sample(Scope.root(), "customers")
        assert rows[0].get("email").startswith("sha256:") and rows[0].get("city") == "London"
        result = s.query("select id, email, city from customers order by id")
        assert result.rows[0][1].startswith("sha256:") and result.rows[0][2] == "London"
        with pytest.raises(PolicyDenied):
            s.query("select * from api_keys")
        with pytest.raises(PolicyDenied):
            s.sample(Scope.root(), "api_keys")  # its values are credentials
        assert s.records("field", Scope.root(), ["products"])  # metadata reads pass
    assert verify(log.events, log.manifest()).ok
    assert [e.get("outcome") for e in log.events].count("denied") >= 2


def test_sdk_wide_governance(shop: ConnectionConfig) -> None:
    rules = {"rules": [{"id": "no-samples", "effect": "deny", "match": {"action": "sample"}}]}
    af = AgentFrontier(governance=Governance(RulePDP(rules), subject="x"))
    with af.open(shop) as s:
        with pytest.raises(PolicyDenied):
            s.sample(Scope.root(), "products")


# ------------------------------------------------------------------------------------------------ CLI


def test_cli_policy_decide_and_authority(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES), encoding="utf-8")
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"subject": "a", "action": "query",
                               "resource": {"type": "query", "statement": "select * from api_keys"}}),
                   encoding="utf-8")
    assert cli(["policy-decide", "--rules", str(rules), str(req)]) == 1
    assert json.loads(capsys.readouterr().out)["evidence"][0]["rule"] == "no-raw-sql-on-keys"

    d = tmp_path / "ext"
    d.mkdir()
    for entity in ("principal", "grant", "delegation", "agent_permission"):
        recs = [r for r in _access_records() if r["entity"] == entity]
        (d / (entity + ".jsonl")).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    assert cli(["authority", str(d), "--principal", "alice"]) == 0
    eff = json.loads(capsys.readouterr().out)
    assert any(e["resource"] == "sales.orders" and e["path"] == ["alice", "analysts", "reporting"] for e in eff)
    assert cli(["authority", str(d), "--who-can", "SELECT:orders"]) == 0
    assert "support-bot" in {e["principal"] for e in json.loads(capsys.readouterr().out)}
    assert cli(["authority", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["nodes"]


def test_cli_extract_with_governance(shop: ConnectionConfig, tmp_path: Path) -> None:
    conn = tmp_path / "shop.json"
    conn.write_text(json.dumps({"id": "shop", "type": "sqlite", "properties": shop.properties}), encoding="utf-8")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES), encoding="utf-8")
    out = tmp_path / "out"
    assert cli(["extract", str(conn), "--aspects", "metadata,data", "--sink", "jsonl:" + str(out), "--policy",
                str(rules), "--subject", "analyst", "--classify", "--audit"]) == 0
    assert cli(["audit-verify", str(out)]) == 0
    assert all(r["values"]["email"].startswith("sha256:") for r in _lines(out / "row.jsonl")
               if r["dataset"] == "customers")
