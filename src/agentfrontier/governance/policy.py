"""Policy decisions (spec docs/policy.md): the request and decision contract, the built-in rule PDP, an OPA adapter,
combination and fail-closed wrapping.

Requests and decisions are plain JSON-friendly dicts on the wire; :class:`Decision` is the typed form::

    pdp = RulePDP.from_file("rules.json")
    d = pdp.decide({"subject": "alice", "action": "sample",
                    "resource": {"type": "dataset", "source": "retail", "dataset": "customers"}})
    d.allowed, d.obligations
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Protocol

from .. import json_util as J
from ..errors import AgentFrontierError

if TYPE_CHECKING:  # pragma: no cover
    from .authority import AuthorityGraph

DEFAULT_HIGH_RISK = ("write", "delete", "execute")
READ_PRIVILEGES = ("SELECT", "READ", "ALL", "ALL PRIVILEGES", "OWNERSHIP")
_NATIVE_ACTIONS = ("sample", "query")


# ------------------------------------------------------------------------------------------------------------ Decision


@dataclass(frozen=True)
class Decision:
    """``decision`` is ``allow`` or ``deny``; ``obligations`` must all be met before anything is released."""

    decision: str
    obligations: list = field(default_factory=list)
    policy_version: str | None = None
    evidence: list = field(default_factory=list)
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @staticmethod
    def allow(obligations: Iterable[dict] = (), policy_version: str | None = None, evidence: Iterable[dict] = (),
              reason: str | None = None) -> "Decision":
        return Decision("allow", list(obligations), policy_version, list(evidence), reason)

    @staticmethod
    def deny(reason: str, policy_version: str | None = None, evidence: Iterable[dict] = ()) -> "Decision":
        return Decision("deny", [], policy_version, list(evidence), reason)

    def to_json(self) -> dict:
        return {"decision": self.decision, "obligations": [dict(o) for o in self.obligations],
                "policyVersion": self.policy_version, "evidence": [dict(e) for e in self.evidence],
                "reason": self.reason}

    @staticmethod
    def from_json(n: Any) -> "Decision":
        """Reads a decision document; ``allow: true/false`` is accepted for ``decision`` (OPA style)."""
        if isinstance(n, bool):
            return Decision("allow" if n else "deny")
        if not isinstance(n, dict):
            raise AgentFrontierError.invalid_config("a policy decision must be an object or a boolean")
        d = n.get("decision")
        if d is None and isinstance(n.get("allow"), bool):
            d = "allow" if n["allow"] else "deny"
        d = J.jstr(d).lower() if d is not None else ""
        if d in ("permit", "allowed"):
            d = "allow"
        if d not in ("allow", "deny"):
            raise AgentFrontierError.invalid_config("policy decision must be 'allow' or 'deny', got '" + d + "'")
        obligations = n.get("obligations") or []
        evidence = n.get("evidence") or []
        if not isinstance(obligations, list) or not all(isinstance(o, dict) for o in obligations):
            raise AgentFrontierError.invalid_config("policy obligations must be a list of objects")
        return Decision(d, [dict(o) for o in obligations], J.text(n, "policyVersion"),
                        [e if isinstance(e, dict) else {"reason": J.jstr(e)} for e in evidence]
                        if isinstance(evidence, list) else [], J.text(n, "reason"))


class PolicyDecisionPoint(Protocol):
    """Anything that answers a policy request. Implementations must be thread-safe."""

    def decide(self, request: dict) -> Decision: ...


def normalise_request(request: dict) -> dict:
    """The request with shorthands expanded (``subject: "alice"`` → ``{"id": "alice"}``) and optional keys present."""
    r = dict(request)
    for k in ("subject", "delegateAgent"):
        v = r.get(k)
        if isinstance(v, str):
            r[k] = {"id": v}
        elif v is None:
            r[k] = None
    if r.get("subject") is None:
        raise AgentFrontierError.invalid_config("a policy request needs a subject")
    if J.is_blank(J.text(r, "action") or ""):
        raise AgentFrontierError.invalid_config("a policy request needs an action")
    res = dict(r.get("resource") or {})
    res["labels"] = sorted({J.jstr(x) for x in res.get("labels") or []})
    res.setdefault("type", "record")
    r["resource"] = res
    r.setdefault("purpose", None)
    r["context"] = dict(r.get("context") or {})
    return r


# ---------------------------------------------------------------------------------------------------------- matching


_GLOBS: dict[str, re.Pattern] = {}


def glob_match(pattern: str, text: str) -> bool:
    """``*`` any run of characters, ``?`` one character; the whole text, case-sensitive."""
    p = _GLOBS.get(pattern)
    if p is None:
        p = re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern), re.S)
        _GLOBS[pattern] = p
    return p.fullmatch(text) is not None


def lookup(doc: Any, path: str) -> Any:
    """Value at a dotted path, ``None`` when any part is missing."""
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_num(a) and _is_num(b):
        return float(a) == float(b)
    return a == b


def _elements(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _pattern_hit(pattern: Any, element: Any) -> bool:
    if isinstance(pattern, str) and isinstance(element, str):
        return glob_match(pattern, element)
    return _eq(pattern, element)


_OPS = ("in", "anyOf", "allOf", "noneOf", "glob", "exists", "not", "gte", "lte")


def matches(cond: Any, value: Any) -> bool:
    """Whether ``value`` satisfies a rule condition (docs/policy.md §3.2)."""
    if isinstance(cond, list):
        return any(_eq(c, e) for c in cond for e in (_elements(value) if isinstance(value, list) else [value]))
    if not isinstance(cond, dict):
        if isinstance(value, list):
            return any(_eq(cond, e) for e in value)
        return _eq(cond, value)
    unknown = [k for k in cond if k not in _OPS]
    if unknown:
        raise AgentFrontierError.invalid_config("unknown policy condition '" + unknown[0] + "'")
    els = _elements(value)
    for op, arg in cond.items():
        if op == "in":
            ok = matches(list(arg) if isinstance(arg, list) else [arg], value)
        elif op == "anyOf":
            ok = any(_pattern_hit(p, e) for p in _elements(arg) for e in els)
        elif op == "allOf":
            ok = all(any(_pattern_hit(p, e) for e in els) for p in _elements(arg))
        elif op == "noneOf":
            ok = not any(_pattern_hit(p, e) for p in _elements(arg) for e in els)
        elif op == "glob":
            ok = any(isinstance(e, str) and glob_match(J.jstr(arg), e) for e in els)
        elif op == "exists":
            ok = (value is not None) == bool(arg)
        elif op == "not":
            ok = not matches(arg, value)
        else:  # gte / lte
            if not _is_num(value) or not _is_num(arg):
                ok = False
            else:
                ok = value >= arg if op == "gte" else value <= arg
        if not ok:
            return False
    return True


# -------------------------------------------------------------------------------------------------------- rule PDP


def _canon(o: Any) -> str:
    return J.canonical(o)


def merge_obligations(lists: Iterable[Iterable[dict]]) -> list[dict]:
    """Obligations in order, without duplicates."""
    seen: set[str] = set()
    out = []
    for lst in lists:
        for o in lst:
            k = _canon(o)
            if k not in seen:
                seen.add(k)
                out.append(dict(o))
    return out


def load_document(path: str | Path) -> Any:
    """A JSON file, or a YAML file (``.yaml``/``.yml``, needs PyYAML: ``pip install agentfrontier[yaml]``)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise AgentFrontierError.invalid_config("cannot read " + str(path) + ": " + str(e)) from e
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise AgentFrontierError.invalid_config("reading " + str(path) + " needs PyYAML: pip install "
                                                    "agentfrontier[yaml]") from None
        return yaml.safe_load(text)
    try:
        return J.loads_first(text)
    except ValueError as e:
        raise AgentFrontierError.invalid_config("cannot parse " + str(path) + ": " + str(e)) from e


class RulePDP:
    """The built-in PDP: ordered rules, deny overrides allow, fail closed for high-risk actions. With an
    :class:`~agentfrontier.governance.authority.AuthorityGraph`, ``sample`` and ``query`` on a dataset also need an
    effective native read grant."""

    def __init__(self, rules: dict | None = None, authority: "AuthorityGraph | None" = None):
        doc = rules or {}
        if not isinstance(doc, dict):
            raise AgentFrontierError.invalid_config("a rules document must be an object")
        self.version = J.text(doc, "version")
        self.default = J.text(doc, "defaultDecision", "allow")
        if self.default not in ("allow", "deny"):
            raise AgentFrontierError.invalid_config("defaultDecision must be 'allow' or 'deny'")
        hr = doc.get("highRiskActions")
        self.high_risk = tuple(J.strings(hr)) if hr is not None else DEFAULT_HIGH_RISK
        self.rules: list[dict] = []
        ids: set[str] = set()
        for r in doc.get("rules") or []:
            rid = J.text(r, "id")
            effect = J.text(r, "effect")
            if J.is_blank(rid) or effect not in ("allow", "deny"):
                raise AgentFrontierError.invalid_config("every policy rule needs an id and effect allow or deny")
            if rid in ids:
                raise AgentFrontierError.invalid_config("duplicate policy rule id '" + rid + "'")
            ids.add(rid)
            match = r.get("match") or {}
            for c in match.values():  # fail on unknown operators now, not at the first request
                matches(c, None)
            self.rules.append(r)
        self.authority = authority

    @staticmethod
    def from_file(path: str | Path, authority: "AuthorityGraph | None" = None) -> "RulePDP":
        return RulePDP(load_document(path), authority)

    @property
    def is_empty(self) -> bool:
        return not self.rules and self.authority is None and self.default == "allow"

    def decide(self, request: dict) -> Decision:
        req = normalise_request(request)
        action = J.text(req, "action") or ""
        denies, allows = [], []
        for r in self.rules:
            match = r.get("match") or {}
            if all(matches(c, lookup(req, path)) for path, c in match.items()):
                (denies if r["effect"] == "deny" else allows).append(r)
        evidence = [{"source": "rules", "rule": r["id"], "reason": J.text(r, "reason")} for r in denies]
        native = self._native_denial(req, action)
        if native is not None:
            evidence.append(native)
        if evidence:
            return Decision.deny(evidence[0]["reason"] or ("denied by rule " + J.jstr(evidence[0]["rule"])),
                                 self.version, evidence)
        if allows:
            return Decision.allow(merge_obligations(r.get("obligations") or [] for r in allows), self.version,
                                  [{"source": "rules", "rule": r["id"], "reason": J.text(r, "reason")}
                                   for r in allows],
                                  "allowed by rule " + ", ".join(r["id"] for r in allows))
        if action in self.high_risk:
            return Decision.deny("high-risk action '" + action + "' needs a rule that allows it", self.version,
                                 [{"source": "rules", "rule": None, "reason": "fail closed for high-risk actions"}])
        if self.default == "deny":
            return Decision.deny("no rule allows this request", self.version,
                                 [{"source": "rules", "rule": None, "reason": "default decision"}])
        return Decision.allow((), self.version, [{"source": "rules", "rule": None, "reason": "default decision"}],
                              "default decision")

    def _native_denial(self, req: dict, action: str) -> dict | None:
        res = req["resource"]
        dataset = J.text(res, "dataset") or ""
        if self.authority is None or action not in _NATIVE_ACTIONS or J.is_blank(dataset):
            return None
        subject = J.text(req["subject"], "id") or ""
        if self.authority.holds(subject, READ_PRIVILEGES, J.text(res, "source"), res.get("path") or {}, dataset):
            return None
        return {"source": "native-grants", "rule": None,
                "reason": subject + " has no native read grant on " + dataset}


# ------------------------------------------------------------------------------------------------- adapters


class OpaPDP:
    """Open Policy Agent over its REST data API: posts ``{"input": request}`` to ``<url>/v1/data/<path>``."""

    def __init__(self, url: str, path: str = "agentfrontier/decision", token: str | None = None,
                 timeout: float = 5.0):
        self.endpoint = url.rstrip("/") + "/v1/data/" + path.strip("/")
        self.token = token
        self.timeout = timeout

    def decide(self, request: dict) -> Decision:
        body = json.dumps({"input": normalise_request(request)}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - configured endpoint
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AgentFrontierError.connection("OPA at " + self.endpoint + ": " + str(e), e) from e
        if not isinstance(doc, dict) or "result" not in doc:
            raise AgentFrontierError.connection("OPA at " + self.endpoint + " returned no result (is the policy "
                                                "path right?)")
        d = Decision.from_json(doc["result"])
        if not d.evidence:
            d = Decision(d.decision, d.obligations, d.policy_version,
                         [{"source": "opa", "rule": None, "reason": d.reason}], d.reason)
        return d


class CombinedPDP:
    """Asks every PDP; any deny wins, otherwise obligations are merged."""

    def __init__(self, pdps: Iterable[PolicyDecisionPoint]):
        self.pdps = list(pdps)

    def decide(self, request: dict) -> Decision:
        decisions = [p.decide(request) for p in self.pdps]
        version = "+".join(d.policy_version for d in decisions if d.policy_version) or None
        evidence = [e for d in decisions for e in d.evidence]
        denied = [d for d in decisions if not d.allowed]
        if denied:
            return Decision.deny(denied[0].reason or "denied", version,
                                 [e for d in denied for e in d.evidence])
        return Decision.allow(merge_obligations(d.obligations for d in decisions), version, evidence,
                              "; ".join(d.reason for d in decisions if d.reason) or None)


class FailClosedPDP:
    """Turns a failing PDP into a deny. ``on_unavailable="allow"`` relaxes that for actions that are not high-risk."""

    def __init__(self, pdp: PolicyDecisionPoint, on_unavailable: str = "deny",
                 high_risk: Iterable[str] = DEFAULT_HIGH_RISK):
        if on_unavailable not in ("allow", "deny"):
            raise AgentFrontierError.invalid_config("onUnavailable must be 'allow' or 'deny'")
        self.pdp = pdp
        self.on_unavailable = on_unavailable
        self.high_risk = tuple(high_risk)

    def decide(self, request: dict) -> Decision:
        try:
            return self.pdp.decide(request)
        except Exception as e:  # noqa: BLE001 - every failure must end in a decision
            msg = "policy decision point unavailable: " + (e.message if isinstance(e, AgentFrontierError) else str(e))
            ev = [{"source": "pep", "rule": None, "reason": msg}]
            action = J.text(request, "action") or ""
            if self.on_unavailable == "allow" and action not in self.high_risk:
                return Decision.allow((), None, ev, msg)
            return Decision.deny(msg, None, ev)


def pdp_from_config(cfg: Any, base_dir: str | Path | None = None,
                    authority: "AuthorityGraph | None" = None, resolve=None) -> PolicyDecisionPoint:
    """A PDP from a pipeline's ``governance.policy`` block::

        {"rules": "rules.json" | {...}, "opa": {"url": ..., "path": ..., "token": ...}, "onUnavailable": "deny"}

    ``resolve`` resolves secret references (``${env:...}``) in the OPA token."""
    cfg = cfg or {}
    if isinstance(cfg, str):
        cfg = {"rules": cfg}
    base = None if base_dir is None else Path(base_dir)
    pdps: list[PolicyDecisionPoint] = []
    rules = cfg.get("rules")
    high_risk: Iterable[str] = DEFAULT_HIGH_RISK
    if isinstance(rules, str):
        p = Path(rules)
        rule_pdp = RulePDP.from_file(p if p.is_absolute() or base is None else base / p, authority)
    else:
        rule_pdp = RulePDP(rules, authority)
    high_risk = rule_pdp.high_risk
    pdps.append(rule_pdp)
    opa = cfg.get("opa")
    if isinstance(opa, dict):
        token = opa.get("token")
        if token is not None and resolve is not None:
            token = resolve(token)
        pdps.append(OpaPDP(J.text(opa, "url") or "", J.text(opa, "path", "agentfrontier/decision"),
                           None if token is None else J.jstr(token), float(opa.get("timeout") or 5.0)))
    pdp: PolicyDecisionPoint = pdps[0] if len(pdps) == 1 else CombinedPDP(pdps)
    return FailClosedPDP(pdp, J.text(cfg, "onUnavailable", "deny"), high_risk)
