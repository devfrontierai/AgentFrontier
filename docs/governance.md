# Governance

AgentFrontier asks a policy decision point (PDP) before it reads anything and before it releases any record. It also
labels sensitive fields, explains who can access what, and keeps a tamper-evident audit trail. The contracts are in
the spec: [policy](https://github.com/devfrontierai/agentfrontier-spec/blob/main/docs/policy.md),
[classification](https://github.com/devfrontierai/agentfrontier-spec/blob/main/docs/classification.md),
[authority](https://github.com/devfrontierai/agentfrontier-spec/blob/main/docs/authority.md) and
[audit](https://github.com/devfrontierai/agentfrontier-spec/blob/main/docs/audit.md). Everything here is standard
library only; YAML rules files need `pip install agentfrontier[yaml]`.

Governance is in the Python edition only: per the plan's parity decision, the Java edition stops at the raw-stream
layer. The spec's vectors in `tests/conformance/governance/` let another edition implement it identically.

## In a pipeline

```json
{"source": "shop.json",
 "aspects": ["metadata", "data"],
 "sinks": [{"type": "jsonl", "path": "out/shop"}],
 "governance": {
   "subject": {"id": "alice", "type": "user", "groups": ["analysts"]},
   "delegateAgent": "support-bot",
   "purpose": "support",
   "policy": "rules.json",
   "classify": true,
   "audit": {"key": "${env:AUDIT_KEY}"}
 }}
```

| Key | Meaning |
|---|---|
| `subject`, `delegateAgent`, `purpose`, `context` | who the run acts for and why; they go into every policy request and audit event |
| `policy` | a rules file, or `{"rules": <file or inline object>, "opa": {"url": ..., "path": ..., "token": ...}, "onUnavailable": "deny"}`. With both, a deny from either wins |
| `authority` | a jsonl folder from an earlier `governance` extraction of the same source: `sample` and `query` then also need an effective native read grant |
| `classify` | `true` for the built-in rules, or `{"rules": [files], "builtin": true, "engine": ..., "reconcile": "union_with_precedence", "engines": [...], "precedence": [...]}` |
| `audit` | `true`, or `{"key": ..., "all": true}`. The key signs the manifest |

What happens during the run:

1. **Before each operation** (`fields`, `indexes`, `lineage`, `grants`, ...), the PDP decides `read` on it. A deny
   skips the operation, with a warning in the report.
2. **Before each sample**, it decides `sample` on the dataset. The request already carries the labels the field
   names and comments produced, so a table of credentials is refused before it is read.
3. **After each fetch**, every record goes through the PDP. Denied records are withheld, and obligations are
   applied: `mask` (by field name or label glob, with `redact`, `null`, `hash` or `partial`), `drop_fields`,
   `filter_rows` and `limit_rows`. An `approval` obligation, or one the SDK does not know, means the data is not
   released.
4. **Classification** writes `label` records (engine `agentfrontier-rules`, `reviewStatus: unreviewed`, with
   `confidence` and `evidence`). It never touches the source's own labels; the reconcile mode decides which labels
   policies see.
5. **Auditing** writes `audit_event` records (`run.start`, every `read` and `sample` with its decision and outcome,
   `release` for withheld records, `run.finish`), hash-chained per run. `run.json` → `report.audit` holds the
   manifest, and `report.policy` the counts of denied requests, withheld records and masked rows.

Without a `governance` block nothing changes: the default PDP allows every read and adds no obligations, and the
output is byte-identical to earlier versions. High-risk actions (`write`, `delete`, `execute`) are always denied
unless a rule allows them.

## A rules file

```json
{"version": "shop-2026-10-27",
 "rules": [
   {"id": "agents-no-secrets", "effect": "deny",
    "match": {"delegateAgent.id": {"exists": true}, "resource.labels": {"anyOf": ["secret.*"]}},
    "reason": "agents never see credentials"},
   {"id": "mask-pii", "effect": "allow", "match": {"action": ["sample", "query"], "purpose": {"not": "support"}},
    "obligations": [{"type": "mask", "labels": ["pii.*"], "method": "hash"},
                    {"type": "mask", "labels": ["pci.*"], "method": "partial"}]},
   {"id": "hr-needs-approval", "effect": "allow", "match": {"resource.path.schema": "hr"},
    "obligations": [{"type": "approval", "reason": "HR data needs sign-off"}]}
 ]}
```

Conditions: equality, lists (any of), `in`, `anyOf` / `allOf` / `noneOf` (globs over list values), `glob`, `exists`,
`not`, `gte`, `lte`. Deny overrides allow. Try a request without a source:

```bash
echo '{"subject": "alice", "delegateAgent": "bot", "action": "sample",
       "resource": {"type": "dataset", "dataset": "keys", "labels": ["secret.credential"]}}' \
  | agentfrontier policy-decide --rules rules.json
```

## In the library

```python
from agentfrontier import AgentFrontier
from agentfrontier.governance import AuditLog, Governance, PolicyDenied, RulePDP

gov = Governance(RulePDP.from_file("rules.json"), subject="alice", purpose="analytics", classifier=True,
                 audit=AuditLog("session-1", "shop"))
with AgentFrontier().open(conn, governance=gov) as s:   # a GovernedSession
    rows = s.sample(Scope.of("schema", "sales"), "customers")       # masked as the rules say
    result = s.query("select email, city from sales.customers")     # columns classified, then masked
    try:
        s.sample(Scope.of("schema", "hr"), "salaries")
    except PolicyDenied as e:                                       # ApprovalRequired is a PolicyDenied
        print(e.decision.reason, e.decision.evidence)
gov.audit.manifest()
```

`AgentFrontier(governance=gov)` makes every `open` governed. Any object with `decide(request) -> Decision` is a PDP:
`OpaPDP(url, path, token)` asks Open Policy Agent, `CombinedPDP([...])` combines several, and `FailClosedPDP` turns
failures into denies (the SDK wraps every PDP in it).

## Classification

```python
from agentfrontier.governance import RuleClassifier, reconcile

c = RuleClassifier.builtin()                       # spec classification/rules.json, plus your own files
c.classify("customer_email", None, ["a@x.io"])     # [Hit(label='pii.email', confidence=0.9, evidence='name,values:1/1')]
reconcile(label_records, "source_authoritative")   # .effective, .advisory, .conflicts
```

The built-in rules cover e-mail, phone, person names, dates of birth, national ids, addresses, IP addresses,
passports, driver licences, payment cards (Luhn-checked), CVV, IBAN (checksum), bank accounts, health data,
passwords and credentials (AWS, GitHub, Slack, OpenAI-style keys, JWTs, private keys). Presidio, Tika and DLP
product adapters are not built yet.

## Authority graph

```bash
agentfrontier extract shop.json --aspects governance --sink jsonl:out/acl
agentfrontier authority out/acl --principal alice           # effective grants with the path that explains each
agentfrontier authority out/acl --who-can SELECT:sales.orders
agentfrontier authority out/acl > graph.json                # {"nodes": [...], "edges": [...]} for a UI such as Evia
```

```python
g = AuthorityGraph.from_directory("out/acl")
for e in g.effective("support-bot"):          # its own permissions plus valid delegations, narrowed by scope
    print(e.privilege, e.resource, " -> ".join(e.path))
g.relationships()                             # member_of / delegated_to / may_access relationship records
```

The graph explains access; it does not grant it. The source system stays the enforcer of its own grants.

## Verifying an audit trail

```bash
AUDIT_KEY=... agentfrontier audit-verify out/shop --key-env AUDIT_KEY
```

It checks every event's hash, the chain, the sequence numbers and the manifest (and its signature with the key).
Changing, removing, reordering or inserting an event makes it fail. Keep the manifest, or at least its
`lastHash`, where whoever writes the events cannot change it.
