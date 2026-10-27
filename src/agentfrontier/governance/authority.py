"""The authority graph (spec docs/authority.md): who can do what, and why, from collected ``principal``, ``grant``,
``delegation`` and ``agent_permission`` records. Explanatory, not authoritative: the source system enforces its own
grants.

::

    g = AuthorityGraph.from_directory("out")           # a jsonl extraction with the governance aspect
    for e in g.effective("alice"): print(e.privilege, e.resource, " -> ".join(e.path))
    g.who_can("SELECT", "sales.orders")
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .. import json_util as J
from ..model import Record
from .policy import glob_match

PUBLIC = "PUBLIC"
_GRANT_FIELDS = {"entity", "source", "properties", "principal", "privilege", "objectType", "dataset", "field",
                 "grantor", "grantable"}


@dataclass(frozen=True)
class EffectiveGrant:
    """One privilege a principal (or agent) effectively holds, and the path that explains it."""

    principal: str
    privilege: str
    source: str | None
    levels: tuple  # ((level, value), ...)
    object_type: str | None
    dataset: str | None
    field_name: str | None
    path: tuple  # principal, groups..., grantee [, "delegated by x"]
    via: str  # direct | group | public | superuser | delegation | permission
    grantable: bool = False

    @property
    def resource(self) -> str:
        """Dotted name: level values, dataset, field."""
        parts = [v for _, v in self.levels]
        for x in (self.dataset, self.field_name):
            if x is not None:
                parts.append(x)
        return ".".join(parts) if parts else "*"

    def to_json(self) -> dict:
        return {"principal": self.principal, "privilege": self.privilege, "source": self.source,
                "resource": self.resource, "objectType": self.object_type, "via": self.via,
                "path": list(self.path), "grantable": self.grantable}


@dataclass
class _Grant:
    grantee: str
    privilege: str
    source: str | None
    levels: tuple
    object_type: str | None
    dataset: str | None
    field_name: str | None
    grantable: bool


@dataclass
class _Delegation:
    principal: str
    delegate: str
    scope: list = field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None
    source: str | None = None


def _instant(s: str | None) -> _dt.datetime | None:
    if s is None or J.is_blank(s):
        return None
    try:
        t = _dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=_dt.timezone.utc)


def _body(o: dict) -> dict:
    """A record from a jsonl line, with or without the raw-stream envelope."""
    return o["body"] if isinstance(o.get("body"), dict) and "envelopeVersion" in o else o


class AuthorityGraph:
    def __init__(self) -> None:
        self._principals: dict[str, dict] = {}  # name -> {type, superuser, memberOf, source}
        self._members: dict[str, set[str]] = {}  # principal -> direct groups
        self._grants: list[_Grant] = []
        self._delegations: list[_Delegation] = []
        self._permissions: list[_Grant] = []

    # ------------------------------------------------------------------------------------------------- building

    @staticmethod
    def from_records(records: Iterable[Record | dict]) -> "AuthorityGraph":
        g = AuthorityGraph()
        g.add(records)
        return g

    @staticmethod
    def from_directory(directory: str | Path) -> "AuthorityGraph":
        """Reads ``principal``, ``relationship`` (``member_of``), ``grant``, ``delegation`` and ``agent_permission``
        JSON Lines files (plain records or raw-stream events) from a jsonl sink's directory."""
        d = Path(directory)
        out: list[dict] = []
        for entity in ("principal", "relationship", "grant", "delegation", "agent_permission"):
            p = d / (entity + ".jsonl")
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(_body(json.loads(line)))
        return AuthorityGraph.from_records(out)

    def add(self, records: Iterable[Record | dict]) -> None:
        for r in records:
            v = r.to_json() if isinstance(r, Record) else dict(r)
            entity = v.get("entity")
            if entity == "principal":
                self._add_principal(v)
            elif entity == "grant":
                g = self._grant(v, J.text(v, "principal"))
                if g is not None:
                    self._grants.append(g)
            elif entity == "delegation":
                p, dlg = J.text(v, "principal"), J.text(v, "delegate")
                if p and dlg:
                    scope = J.text(v, "scope") or "*"
                    self._delegations.append(_Delegation(p, dlg, scope.split(), J.text(v, "validFrom"),
                                                         J.text(v, "validTo"), J.text(v, "source")))
            elif entity == "relationship":
                # memberships listed per group (identity sources) arrive as member_of relationships
                s, o = J.text(v, "subject"), J.text(v, "object")
                if J.text(v, "predicate") == "member_of" and s and o:
                    self._members.setdefault(s, set()).add(o)
            elif entity == "agent_permission":
                g = self._permission(v)
                if g is not None:
                    self._permissions.append(g)

    def _add_principal(self, v: dict) -> None:
        name = J.text(v, "name")
        if not name:
            return
        groups = v.get("memberOf")
        groups = J.strings(groups) if groups is not None else []
        self._principals[name] = {"type": J.text(v, "type"), "superuser": v.get("superuser") is True,
                                  "source": J.text(v, "source")}
        self._members.setdefault(name, set()).update(g for g in groups if g)

    @staticmethod
    def _grant(v: dict, grantee: str | None) -> _Grant | None:
        priv = J.text(v, "privilege")
        if not grantee or not priv:
            return None
        levels = tuple((k, J.jstr(x)) for k, x in v.items() if k not in _GRANT_FIELDS and x is not None
                       and not isinstance(x, (dict, list)))
        return _Grant(grantee, priv.upper(), J.text(v, "source"), levels, J.text(v, "objectType"),
                      J.text(v, "dataset"), J.text(v, "field"), v.get("grantable") is True)

    @staticmethod
    def _permission(v: dict) -> _Grant | None:
        agent, priv = J.text(v, "agent"), J.text(v, "privilege")
        if not agent or not priv:
            return None
        resource = J.text(v, "resource")
        return _Grant(agent, priv.upper(), J.text(v, "source"), (), J.text(v, "resourceType"), resource, None, False)

    # ------------------------------------------------------------------------------------------------- queries

    def _resolve(self, name: str) -> str:
        if name in self._principals or name in self._members:
            return name
        low = name.lower()
        for k in self._principals:
            if k.lower() == low:
                return k
        return name

    def groups(self, principal: str) -> dict[str, tuple]:
        """Every group a principal belongs to, transitively, with the membership path (cycles ignored)."""
        start = self._resolve(principal)
        paths: dict[str, tuple] = {}
        frontier = [(start, (start,))]
        while frontier:
            nxt = []
            for p, path in frontier:
                for grp in sorted(self._members.get(p, ())):
                    if grp not in paths and grp != start:
                        paths[grp] = path + (grp,)
                        nxt.append((grp, path + (grp,)))
            frontier = nxt
        return paths

    def effective(self, principal: str, at: _dt.datetime | None = None) -> list[EffectiveGrant]:
        """Effective grants of a principal or agent: its own, its groups', ``PUBLIC``'s, its agent permissions,
        and what valid delegations pass on to it, narrowed by their scope."""
        out = self._own(principal)
        name = self._resolve(principal)
        now = at or _dt.datetime.now(_dt.timezone.utc)
        for g in self._permissions:
            if g.grantee == name:
                out.append(self._eff(name, g, (name, g.grantee), "permission"))
        for d in self._delegations:
            if d.delegate != name or not self._valid(d, now):
                continue
            for e in self._own(d.principal):
                if self._in_scope(e, d.scope):
                    out.append(EffectiveGrant(name, e.privilege, e.source, e.levels, e.object_type, e.dataset,
                                              e.field_name, (name, "delegated by " + d.principal) + e.path,
                                              "delegation", False))
        return _dedupe(out)

    def _own(self, principal: str) -> list[EffectiveGrant]:
        name = self._resolve(principal)
        groups = self.groups(name)
        out: list[EffectiveGrant] = []
        info = self._principals.get(name) or {}
        known = name in self._principals or name in self._members  # PUBLIC covers the source's own principals
        if info.get("superuser"):
            out.append(EffectiveGrant(name, "ALL", info.get("source"), (), None, None, None, (name,), "superuser"))
        for g in self._grants:
            if g.grantee == name:
                out.append(self._eff(name, g, (name,), "direct"))
            elif g.grantee in groups:
                out.append(self._eff(name, g, groups[g.grantee], "group"))
            elif known and g.grantee.upper() == PUBLIC:
                out.append(self._eff(name, g, (name, PUBLIC), "public"))
        return out

    @staticmethod
    def _eff(name: str, g: _Grant, path: tuple, via: str) -> EffectiveGrant:
        return EffectiveGrant(name, g.privilege, g.source, g.levels, g.object_type, g.dataset, g.field_name, path,
                              via, g.grantable)

    @staticmethod
    def _valid(d: _Delegation, now: _dt.datetime) -> bool:
        f, t = _instant(d.valid_from), _instant(d.valid_to)
        return (f is None or f <= now) and (t is None or now < t)

    @staticmethod
    def _in_scope(e: EffectiveGrant, scope: list[str]) -> bool:
        for token in scope:
            if token == "*":
                return True
            priv, _, glob = token.partition(":")
            if priv.upper() not in (e.privilege, "*") and not (e.privilege == "ALL" and e.via == "superuser"):
                continue
            if not glob or resource_matches(glob, e.resource):
                return True
        return False

    def holds(self, principal: str, privileges: Iterable[str], source: str | None, path: dict,
              dataset: str) -> bool:
        """Whether the principal effectively holds one of the privileges on a dataset: a grant on the dataset, or a
        container-level grant on a scope that contains it. Column grants do not count."""
        wanted = {p.upper() for p in privileges}
        for e in self.effective(principal):
            if e.via == "superuser":
                return True
            if e.privilege not in wanted or (source is not None and e.source is not None and e.source != source):
                continue
            if e.field_name is not None:
                continue
            if not all(path.get(k) == v for k, v in e.levels if k in path):
                continue
            if e.dataset is None or e.dataset == dataset:
                return True
        return False

    def who_can(self, privilege: str, resource: str) -> list[EffectiveGrant]:
        """Principals and agents holding a privilege on a resource (a dotted name, matched as in a delegation
        scope glob)."""
        names = set(self._principals) | set(self._members) | {d.delegate for d in self._delegations} \
            | {g.grantee for g in self._permissions}
        priv = privilege.upper()
        out = []
        for n in sorted(names):
            for e in self.effective(n):
                if (e.privilege == priv or e.via == "superuser") and (e.via == "superuser"
                                                                         or resource_matches(e.resource, resource)
                                                                         or resource_matches(resource, e.resource)):
                    out.append(e)
        return out

    # ------------------------------------------------------------------------------------------------- outputs

    def relationships(self, source: str | None = None) -> list[Record]:
        """``relationship`` records: ``member_of``, ``delegated_to`` and the inferred ``may_access``."""
        out: list[Record] = []
        for p in sorted(self._members):
            for grp in sorted(self._members[p]):
                out.append(Record("relationship", {"source": source or self._source_of(p), "subject": p,
                                                   "subjectType": "principal", "predicate": "member_of",
                                                   "object": grp, "objectType": "principal"}))
        for d in self._delegations:
            out.append(Record("relationship", {"source": source or d.source, "subject": d.principal,
                                               "subjectType": "principal", "predicate": "delegated_to",
                                               "object": d.delegate, "objectType": "principal",
                                               "validFrom": d.valid_from, "validTo": d.valid_to,
                                               "properties": {"scope": " ".join(d.scope)}}))
        for p in sorted(set(self._principals) | {d.delegate for d in self._delegations}):
            for e in self.effective(p):
                out.append(Record("relationship", {"source": source or e.source, "subject": p,
                                                   "subjectType": "principal", "predicate": "may_access",
                                                   "object": e.resource, "objectType": e.object_type or "resource",
                                                   "properties": {"privilege": e.privilege, "via": e.via,
                                                                  "path": list(e.path)}}))
        return out

    def _source_of(self, p: str) -> str | None:
        return (self._principals.get(p) or {}).get("source")

    def export(self) -> dict:
        """``{"nodes": [...], "edges": [...]}``: principals, resources, memberships, grants, delegations, permissions,
        and the inferred effective ``may_access`` edges."""
        nodes: dict[str, dict] = {}
        edges: list[dict] = []

        def principal(n: str) -> str:
            pid = "principal:" + n
            if pid not in nodes:
                info = self._principals.get(n) or {}
                nodes[pid] = {"id": pid, "kind": "principal", "name": n, "type": info.get("type"),
                              "source": info.get("source")}
            return pid

        def resource(e: EffectiveGrant | _Grant, src: str | None) -> str:
            eg = e if isinstance(e, EffectiveGrant) else self._eff("", e, (), "")
            rid = "resource:" + J.jstr(src) + ":" + eg.resource
            if rid not in nodes:
                nodes[rid] = {"id": rid, "kind": "resource", "name": eg.resource, "type": eg.object_type,
                              "source": src}
            return rid

        for p in sorted(self._members):
            for grp in sorted(self._members[p]):
                edges.append({"from": principal(p), "to": principal(grp), "kind": "member_of"})
        for g in self._grants:
            edges.append({"from": principal(g.grantee), "to": resource(g, g.source), "kind": "granted",
                          "privilege": g.privilege, "grantable": g.grantable})
        for d in self._delegations:
            edges.append({"from": principal(d.principal), "to": principal(d.delegate), "kind": "delegated_to",
                          "scope": " ".join(d.scope), "validFrom": d.valid_from, "validTo": d.valid_to})
        for g in self._permissions:
            edges.append({"from": principal(g.grantee), "to": resource(g, g.source), "kind": "permitted",
                          "privilege": g.privilege})
        for p in sorted(set(self._principals) | {d.delegate for d in self._delegations}):
            for e in self.effective(p):
                edges.append({"from": principal(p), "to": resource(e, e.source), "kind": "may_access",
                              "privilege": e.privilege, "via": e.via, "path": list(e.path), "inferred": True})
        return {"nodes": list(nodes.values()), "edges": edges}


def resource_matches(glob: str, name: str) -> bool:
    """A resource glob against a dotted name; a glob with fewer parts matches the last parts of the name."""
    g_parts, n_parts = glob.split("."), name.split(".")
    if len(g_parts) < len(n_parts):
        n_parts = n_parts[len(n_parts) - len(g_parts):]
    if glob_match(".".join(g_parts), ".".join(n_parts)):
        return True
    return glob_match(glob, name)


def _dedupe(items: list[EffectiveGrant]) -> list[EffectiveGrant]:
    seen = set()
    out = []
    for e in items:
        k = (e.privilege, e.source, e.levels, e.object_type, e.dataset, e.field_name, e.path)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out

