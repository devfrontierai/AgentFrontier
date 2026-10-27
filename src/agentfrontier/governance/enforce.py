"""Policy enforcement (spec docs/policy.md §2, §4, §5): asks the PDP before and after every fetch, applies
obligations (masking, dropped fields, row filters, row limits) and refuses what it cannot enforce.

::

    gov = Governance(RulePDP.from_file("rules.json"), subject="alice", purpose="support",
                     classifier=RuleClassifier.builtin())
    with af.open(conn, governance=gov) as s:        # a GovernedSession
        rows = s.sample(Scope.of("schema", "sales"), "customers")   # masked as the rules say
"""

from __future__ import annotations

import hashlib
import threading
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .. import json_util as J
from ..errors import POLICY_DENIED, AgentFrontierError
from ..model import Page, QueryRequest, QueryResult, Record, Scope
from .audit import AuditLog
from .classify import FieldClassifier, LabelIndex, RuleClassifier, value_text
from .policy import Decision, FailClosedPDP, PolicyDecisionPoint, RulePDP, glob_match, lookup, matches

if TYPE_CHECKING:  # pragma: no cover
    from ..engine import EngineSession

KNOWN_OBLIGATIONS = ("mask", "drop_fields", "filter_rows", "limit_rows", "approval", "audit")
_RESOURCE_TYPES = {"source": "source", "container": "container", "dataset": "dataset", "field": "field",
                   "row": "dataset"}
_MAX_CACHE = 10_000


class PolicyDenied(AgentFrontierError):
    """The policy decision point denied a request (or allowed it with obligations the SDK cannot meet)."""

    def __init__(self, decision: Decision, request: dict):
        super().__init__(POLICY_DENIED, "policy denied " + J.jstr(request.get("action")) + " on "
                         + describe(request.get("resource") or {}) + ": " + J.jstr(decision.reason))
        self.decision = decision
        self.request = request


class ApprovalRequired(PolicyDenied):
    """The policy allows the request only after human approval, which the caller's workflow must obtain."""


def describe(resource: dict) -> str:
    """``source/level=value/.../dataset=x/field=y`` (or ``operation=``, ``statement``) for messages and audit."""
    parts = [J.jstr(resource.get("source")) if resource.get("source") is not None else "*"]
    for k, v in (resource.get("path") or {}).items():
        parts.append(k + "=" + J.jstr(v))
    for k in ("dataset", "field", "operation"):
        if resource.get(k) is not None:
            parts.append(k + "=" + J.jstr(resource[k]))
    return "/".join(parts)


def mask_value(v: Any, method: str = "redact", keep: int = 4, salt: str = "") -> Any:
    """One masked value (docs/policy.md §5); ``None`` stays ``None``."""
    if v is None:
        return None
    if method == "null":
        return None
    if method == "hash":
        return "sha256:" + hashlib.sha256((salt + value_text(v)).encode("utf-8")).hexdigest()
    if method == "partial":
        t = value_text(v)
        if len(t) <= keep:
            return "****"
        return "*" * (len(t) - keep) + t[len(t) - keep:]
    return "****"


def _selected(o: dict, keys: Iterable[str], field_labels: dict[str, list[str]]) -> list[str]:
    fields = set(J.strings(o.get("fields")))
    globs = J.strings(o.get("labels"))
    out = []
    for k in keys:
        if k in fields or (globs and any(glob_match(g, lab) for g in globs for lab in field_labels.get(k, ()))):
            out.append(k)
    return out


def apply_to_row(obligations: Iterable[dict], row: dict, field_labels: dict[str, list[str]]) -> dict | None:
    """A row (column → value) with the obligations applied; ``None`` when a row filter drops it."""
    out = dict(row)
    for o in obligations:
        t = o.get("type")
        if t == "mask":
            for k in _selected(o, list(out), field_labels):
                out[k] = mask_value(out[k], J.text(o, "method", "redact") or "redact", J.integer(o, "keep", 4),
                                    J.text(o, "salt", "") or "")
        elif t == "drop_fields":
            for k in _selected(o, list(out), field_labels):
                del out[k]
        elif t == "filter_rows":
            where = o.get("where") or {}
            if not all(matches(c, lookup(row, p)) for p, c in where.items()):
                return None
    return out


class Governance:
    """The policy enforcement point of a run or session: who is asking, for what purpose, which PDP decides,
    which labels are known, and the audit log. Thread-safe."""

    def __init__(self, pdp: PolicyDecisionPoint | None = None, subject: str | dict = "agentfrontier",
                 delegate_agent: str | dict | None = None, purpose: str | None = None, context: dict | None = None,
                 classifier: RuleClassifier | bool | None = None, labels: LabelIndex | None = None,
                 audit: AuditLog | None = None, aspect_of: Callable[[str], str | None] | None = None,
                 audit_all: bool = True):
        if pdp is None:
            pdp = RulePDP()
        self._empty = isinstance(pdp, RulePDP) and pdp.is_empty
        self.pdp = pdp if isinstance(pdp, FailClosedPDP) else FailClosedPDP(pdp)
        self.subject = subject
        self.delegate_agent = delegate_agent
        self.purpose = purpose
        self.context = dict(context or {})
        self.classifier = RuleClassifier.builtin() if classifier is True else (classifier or None)
        self.labels = labels if labels is not None else LabelIndex()
        self.audit = audit
        self.audit_all = audit_all
        self.aspect_of = aspect_of
        self._cache: dict[str, Decision] = {}
        self._lock = threading.Lock()
        self.withheld = 0
        self.masked = 0
        self.denied_requests = 0

    @property
    def trivial(self) -> bool:
        """True when nothing can be denied, masked, labelled or audited: enforcement can be skipped."""
        return self._empty and self.classifier is None and self.audit is None

    @property
    def actor(self) -> str:
        s = self.subject
        return J.jstr(s.get("id")) if isinstance(s, dict) else J.jstr(s)

    def request(self, action: str, resource: dict) -> dict:
        return {"subject": self.subject, "delegateAgent": self.delegate_agent, "action": action,
                "resource": resource, "purpose": self.purpose, "context": self.context}

    # ------------------------------------------------------------------------------------------------- decisions

    def decide(self, action: str, resource: dict) -> Decision:
        """The PDP's decision, made effective: unknown obligations and approval obligations turn it into a deny."""
        req = self.request(action, resource)
        key = J.canonical(req)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        d = self._effective(self.pdp.decide(req))
        with self._lock:
            if len(self._cache) >= _MAX_CACHE:
                self._cache.clear()
            self._cache[key] = d
            if not d.allowed:
                self.denied_requests += 1
        return d

    @staticmethod
    def _effective(d: Decision) -> Decision:
        if not d.allowed:
            return d
        for o in d.obligations:
            if o.get("advice") is True:
                continue
            t = o.get("type")
            if t == "approval":
                reason = "approval required" + (": " + J.jstr(o["reason"]) if o.get("reason") else "")
                return Decision("deny", d.obligations, d.policy_version,
                                d.evidence + [{"source": "pep", "rule": None, "reason": reason}], reason)
            if t not in KNOWN_OBLIGATIONS:
                reason = "obligation '" + J.jstr(t) + "' is not understood"
                return Decision("deny", d.obligations, d.policy_version,
                                d.evidence + [{"source": "pep", "rule": None, "reason": reason}], reason)
        return d

    def authorize(self, action: str, resource: dict) -> Decision:
        """The decision, or :class:`PolicyDenied` / :class:`ApprovalRequired` when it is a deny."""
        d = self.decide(action, resource)
        if not d.allowed:
            req = self.request(action, resource)
            if any(o.get("type") == "approval" for o in d.obligations):
                raise ApprovalRequired(d, req)
            raise PolicyDenied(d, req)
        return d

    def audit_decision(self, action: str, resource: dict, d: Decision | None, outcome: str,
                       records: int | None = None, reason: str | None = None) -> None:
        """Records an audit event when there is an audit log and either everything is audited or the decision
        carries an ``audit`` obligation."""
        force = d is not None and any(o.get("type") == "audit" for o in d.obligations)
        if self.audit is None or not (self.audit_all or force):
            return
        decision = None
        if d is not None:
            decision = "deny" if not d.allowed else ("allow_with_obligations" if d.obligations else "allow")
        s = self.subject
        self.audit.record(action, self.actor, J.text(s, "type") if isinstance(s, dict) else None,
                          describe(resource), resource.get("type"), self.purpose, decision,
                          None if d is None else d.policy_version, outcome, records,
                          reason if reason is not None else (None if d is None or d.allowed else d.reason))

    # ------------------------------------------------------------------------------------------------- resources

    def resource(self, type_: str, source: str | None, path: dict | None = None, **extra: Any) -> dict:
        r = {"type": type_, "source": source, "path": dict(path or {})}
        r.update({k: v for k, v in extra.items() if v is not None})
        ds = r.get("dataset")
        if "labels" not in r and ds is not None:
            r["labels"] = self.labels.labels(source, r["path"], ds, r.get("field"))
        return r

    def record_resource(self, r: Record, levels: Iterable[str]) -> dict:
        v = r.values
        path = {lv: J.jstr(v[lv]) for lv in levels if v.get(lv) is not None}
        type_ = _RESOURCE_TYPES.get(r.entity, "record")
        dataset = v.get("name") if r.entity == "dataset" else v.get("dataset")
        field = v.get("name") if r.entity == "field" else v.get("field") if r.entity != "row" else None
        return self.resource(type_, J.text(v, "source"), path, dataset=None if dataset is None else J.jstr(dataset),
                             field=None if field is None else J.jstr(field), entity=r.entity,
                             aspect=self.aspect_of(r.entity) if self.aspect_of is not None and r.entity != "row"
                             else ("data" if r.entity == "row" else None))

    # ------------------------------------------------------------------------------------------------- release

    def observe(self, records: Iterable[Record]) -> None:
        """Remembers label and tag records, so later decisions see them."""
        labs = [r for r in records if r.entity in ("label", "tag")]
        if labs:
            self.labels.add(labs)

    def release(self, records: list[Record], action: str, levels: Iterable[str],
                withheld_out: list | None = None) -> list[Record]:
        """The records that may be released, with obligations applied; withheld ones are counted and audited (and
        listed in ``withheld_out`` as ``(resource, decision, count)`` when given)."""
        if self._empty:  # the empty rule PDP allows every read without obligations
            return records
        levels = list(levels)
        out: list[Record] = []
        withheld: dict[str, tuple[dict, Decision, int]] = {}
        per_dataset_rows: dict[str, int] = {}
        field_labels_cache: dict[tuple, dict] = {}
        for r in records:
            res = self.record_resource(r, levels)
            d = self.decide(action, res)
            if not d.allowed:
                k = describe(res)
                prev = withheld.get(k)
                withheld[k] = (res, d, (prev[2] if prev else 0) + 1)
                continue
            if not d.obligations:
                out.append(r)
                continue
            if r.entity == "row":
                ds_key = (res.get("source"), J.canonical(res.get("path")), res.get("dataset"))
                fl = field_labels_cache.get(ds_key)
                if fl is None:
                    fl = self.labels.field_labels(res.get("source"), res.get("path") or {}, res.get("dataset"))
                    field_labels_cache[ds_key] = fl
                limit = min((J.integer(o, "max", 0) for o in d.obligations if o.get("type") == "limit_rows"),
                            default=None)
                count_key = J.canonical(list(ds_key))
                if limit is not None and per_dataset_rows.get(count_key, 0) >= limit:
                    continue
                nested = isinstance(r.values.get("values"), dict)
                row = r.values["values"] if nested else r.values
                new_row = apply_to_row(d.obligations, row, fl)
                if new_row is None:
                    continue
                per_dataset_rows[count_key] = per_dataset_rows.get(count_key, 0) + 1
                if new_row != row:
                    with self._lock:
                        self.masked += 1
                if nested:
                    v = dict(r.values)
                    v["values"] = new_row
                    out.append(Record("row", v))
                else:
                    out.append(Record("row", new_row))
            else:
                new_v = apply_to_row([o for o in d.obligations if o.get("type") in ("mask", "drop_fields")],
                                     r.values, {})
                out.append(r if new_v == r.values else Record(r.entity, new_v or {}))
        for res, d, n in withheld.values():
            with self._lock:
                self.withheld += n
            self.audit_decision("release", res, d, "denied", n)
            if withheld_out is not None:
                withheld_out.append((res, d, n))
        return out

    def summary(self) -> dict:
        with self._lock:
            return {"deniedRequests": self.denied_requests, "withheldRecords": self.withheld,
                    "maskedRows": self.masked}

    @staticmethod
    def from_config(cfg: Any, run_id: str, source: str | None = None,
                    aspect_of: Callable[[str], str | None] | None = None, resolve: Callable[[Any], Any] | None = None,
                    audit_sink: Callable[[list[Record]], None] | None = None) -> "Governance":
        """Governance from a pipeline's ``governance`` block (paths already absolute)::

            {"subject": "alice" | {"id": ..., "type": ...}, "delegateAgent": ..., "purpose": ..., "context": {...},
             "policy": "rules.json" | {"rules": ..., "opa": {...}, "onUnavailable": "deny"},
             "authority": "<jsonl directory with principal/grant/delegation records>",
             "classify": true | {"rules": [...], "builtin": true, "reconcile": "union_with_precedence",
                                 "engines": [...], "precedence": [...]},
             "audit": true | {"key": "${env:AUDIT_KEY}", "all": true}}

        ``resolve`` resolves secret references (the OPA token, the audit key)."""
        from .authority import AuthorityGraph
        from .policy import load_document, pdp_from_config
        cfg = cfg if isinstance(cfg, dict) else {}
        authority = None
        if isinstance(cfg.get("authority"), str):
            authority = AuthorityGraph.from_directory(cfg["authority"])
        policy = cfg.get("policy")
        pdp: PolicyDecisionPoint | None = None
        if policy is not None or authority is not None:
            pdp = pdp_from_config(policy or {}, None, authority, resolve)
        classify = cfg.get("classify")
        classifier = None
        mode = J.text(cfg, "reconcile", "union_with_precedence") or "union_with_precedence"
        engines: list[str] = []
        precedence = None
        if classify is True:
            classifier = RuleClassifier.builtin()
        elif isinstance(classify, dict):
            extra = classify.get("rules") or []
            docs = [load_document(x) if isinstance(x, str) else x for x in extra]
            if classify.get("builtin", True) is not False:
                classifier = RuleClassifier.builtin(docs, J.text(classify, "engine"))
            else:
                classifier = RuleClassifier(docs, J.text(classify, "engine"))
            mode = J.text(classify, "reconcile", mode) or mode
            engines = J.strings(classify.get("engines"))
            precedence = J.strings(classify.get("precedence")) if classify.get("precedence") is not None else None
        audit_cfg = cfg.get("audit")
        audit = None
        audit_all = True
        if audit_cfg:
            key = None
            if isinstance(audit_cfg, dict):
                key = audit_cfg.get("key")
                if key is not None and resolve is not None:
                    key = resolve(key)
                audit_all = audit_cfg.get("all", True) is not False
            audit = AuditLog(run_id, source, None if key is None else J.jstr(key), audit_sink)
        elif pdp is not None:
            # a decision's audit obligation still needs somewhere to write
            audit = AuditLog(run_id, source, None, audit_sink)
            audit_all = False
        subject = cfg.get("subject") or "agentfrontier"
        return Governance(pdp, subject, cfg.get("delegateAgent"), J.text(cfg, "purpose"), J.to_map(cfg.get("context")),
                          classifier, LabelIndex(mode, engines, precedence), audit, aspect_of, audit_all)


# ------------------------------------------------------------------------------------------------ governed session


class GovernedSession:
    """An :class:`~agentfrontier.engine.EngineSession` behind the policy enforcement point. Reads are checked before
    they run (``PolicyDenied`` when refused) and their records are released through the PDP; everything else is
    the session's own."""

    def __init__(self, session: "EngineSession", governance: Governance):
        self._s = session
        self.governance = governance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)

    def __enter__(self) -> "GovernedSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._s.close()

    @property
    def session(self) -> "EngineSession":
        return self._s

    def _levels(self) -> list[str]:
        return [lv.level for lv in self._s.hierarchy]

    def _source(self) -> str | None:
        return self._s.connection.id

    def _guard(self, action: str, res: dict, fetch: Callable[[], list[Record]],
               all_or_nothing: bool = False) -> list[Record]:
        """Decides, fetches and releases. ``all_or_nothing``: when every record is withheld, raise instead of
        returning nothing (for samples, where an empty answer would look like an empty table)."""
        g = self.governance
        try:
            d = g.authorize(action, res)
        except PolicyDenied as e:
            g.audit_decision(action, res, e.decision, "denied", 0)
            raise
        try:
            recs = fetch()
        except Exception:
            g.audit_decision(action, res, d, "failure")
            raise
        g.observe(recs)
        withheld: list = []
        out = g.release(recs, action, self._levels(), withheld)
        if all_or_nothing and recs and not out and withheld:
            g.audit_decision(action, res, withheld[0][1], "denied", 0)
            raise PolicyDenied(withheld[0][1], g.request(action, withheld[0][0]))
        g.audit_decision(action, res, d, "success", len(out))
        return out

    def source(self) -> Record:
        g = self.governance
        res = g.resource("source", self._source(), entity="source")
        out = self._guard("read", res, lambda: [self._s.source()])
        if not out:
            raise PolicyDenied(Decision.deny("the source record was withheld"), g.request("read", res))
        return out[0]

    def containers(self, parent: Scope) -> list[Record]:
        res = self.governance.resource("container", self._source(), parent.as_map(), entity="container")
        return self._guard("read", res, lambda: self._s.containers(parent))

    def datasets(self, scope: Scope, cursor: str | None = None, page_size: int = 0) -> Page:
        res = self.governance.resource("operation", self._source(), scope.as_map(), entity="dataset")
        holder: list[Page] = []

        def fetch() -> list[Record]:
            p = self._s.datasets(scope, cursor, page_size)
            holder.append(p)
            return p.records
        recs = self._guard("read", res, fetch)
        return Page(recs, holder[0].next_cursor)

    def records(self, entity: str, scope: Scope, datasets: Iterable[str] | None = None) -> list[Record]:
        g = self.governance
        aspect = g.aspect_of(entity) if g.aspect_of is not None else None
        res = g.resource("operation", self._source(), scope.as_map(), entity=entity, aspect=aspect)
        ds = None if datasets is None else list(datasets)
        return self._guard("read", res, lambda: self._s.records(entity, scope, ds))

    def run(self, operation: str, scope: Scope, params: dict | None = None) -> list[Record]:
        res = self.governance.resource("operation", self._source(), scope.as_map(), operation=operation)
        return self._guard("read", res, lambda: self._s.run(operation, scope, params))

    def sample(self, scope: Scope, dataset: str, limit: int = 10) -> list[Record]:
        g = self.governance
        res = g.resource("dataset", self._source(), scope.as_map(), dataset=dataset, entity="row", aspect="data",
                         operation="sample")

        def fetch() -> list[Record]:
            rows = self._s.sample(scope, dataset, limit)
            if g.classifier is not None:
                fc = FieldClassifier(g.classifier)
                base = {"source": self._source()}
                base.update(scope.as_map())
                fc.observe_rows(base, dataset, [r.values for r in rows])
                g.observe(fc.flush())
            out = []
            for r in rows:
                v: dict = {"source": self._source()}
                v.update(scope.as_map())
                v["dataset"] = dataset
                v["values"] = r.values
                out.append(Record("row", v))
            return out
        released = self._guard("sample", res, fetch, all_or_nothing=True)
        return [Record("row", r.values["values"]) for r in released]

    def query(self, request: QueryRequest | str) -> QueryResult:
        """A read-only query, decided on its statement; masks and row obligations apply to the result's columns
        (their labels come from classifying the column names and values)."""
        g = self.governance
        if isinstance(request, str):
            request = QueryRequest(request)
        scope = request.scope.as_map() if getattr(request, "scope", None) is not None else {}
        res = g.resource("query", self._source(), scope, statement=request.sql, entity="row", aspect="data")
        try:
            d = g.authorize("query", res)
        except PolicyDenied as e:
            g.audit_decision("query", res, e.decision, "denied", 0)
            raise
        try:
            result = self._s.query(request)
        except Exception:
            g.audit_decision("query", res, d, "failure")
            raise
        if not d.obligations:
            g.audit_decision("query", res, d, "success", result.row_count)
            return result
        names = [c.name for c in result.columns]
        field_labels: dict[str, list[str]] = {}
        if g.classifier is not None:
            for i, n in enumerate(names):
                hits = g.classifier.classify(n, None, [row[i] for row in result.rows])
                field_labels[n] = sorted({h.label for h in hits})
        limit = min((J.integer(o, "max", 0) for o in d.obligations if o.get("type") == "limit_rows"), default=None)
        rows = []
        for row in result.rows:
            if limit is not None and len(rows) >= limit:
                break
            new = apply_to_row(d.obligations, dict(zip(names, row)), field_labels)
            if new is not None:
                rows.append(new)
        dropped = {k for o in d.obligations if o.get("type") == "drop_fields"
                   for k in _selected(o, names, field_labels)}
        kept = [c for c in result.columns if c.name not in dropped]
        out = QueryResult(kept, [[r.get(c.name) for c in kept] for r in rows],
                          result.truncated or (limit is not None and len(result.rows) > limit), result.elapsed_ms)
        g.audit_decision("query", res, d, "success", out.row_count)
        return out
