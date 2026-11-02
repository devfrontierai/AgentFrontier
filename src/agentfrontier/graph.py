"""Graphs built from records (spec docs/graph.md): a store interface with in-memory and JSON stores, a mandatory
access filter on traversal, and builders for the asset, lineage, authority, quality and audit graphs. Python only
(decision D1); the ids are the shared uids of the Neo4j sink, so the graphs line up with it.

    store = MemoryGraphStore()
    builder = GraphBuilder(store, ["asset", "lineage"], levels=["database", "schema"])
    builder.add(records)                       # records or raw-stream events
    store.neighborhood("shop/db/sales/orders", 2, "both", None, ALLOW_ALL)
"""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from . import json_util as J
from .errors import AgentFrontierError
from .model import Record
from .sinks import RunContext, Sink
from .sinks import uids

Node = dict
Edge = dict
AccessFilter = Callable[[dict], bool]


def ALLOW_ALL(node: dict) -> bool:  # noqa: N802 - a constant-like function
    """The access filter that allows every node; pass it explicitly to see everything."""
    return True


GRAPHS = ("asset", "lineage", "authority", "quality", "audit")
_TYPES = {"source": "Source", "container": "Container", "dataset": "Dataset", "field": "Field", "index": "Index",
          "routine": "Routine", "principal": "Principal", "policy": "Policy", "business_term": "BusinessTerm",
          "metric": "Metric", "quality_result": "QualityResult", "audit_event": "AuditEvent"}
_GRAPH_OF = {"source": "asset", "container": "asset", "dataset": "asset", "field": "asset", "index": "asset",
             "routine": "asset", "constraint": "asset", "tag": "asset", "label": "asset", "business_term": "asset",
             "metric": "asset", "lineage": "lineage", "principal": "authority", "grant": "authority",
             "delegation": "authority", "relationship": "authority", "policy": "authority",
             "quality_result": "quality", "statistic": "quality", "audit_event": "audit"}


def edge_id(from_: str, type_: str, to: str, key: str | None = None) -> str:
    return from_ + "|" + type_ + "|" + to + ("" if key is None else "|" + key)


# -------------------------------------------------------------------------------------------------------- stores


class GraphStore:
    """The store contract. Implementations are thread-safe."""

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        raise NotImplementedError

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        raise NotImplementedError

    def node(self, id_: str) -> Node | None:
        raise NotImplementedError

    def nodes(self, type_: str | None = None, where: dict | None = None) -> list[Node]:
        raise NotImplementedError

    def neighborhood(self, id_: str, depth: int, direction: str, edge_types: Iterable[str] | None,
                     access: AccessFilter, history: bool = False) -> dict:
        raise NotImplementedError

    def traversal(self, start_ids: Iterable[str], edge_types: Iterable[str] | None, direction: str, max_depth: int,
                  access: AccessFilter, history: bool = False) -> list[list[str]]:
        raise NotImplementedError

    def bulk_load(self, nodes: Iterable[Node], edges: Iterable[Edge]) -> None:
        self.upsert_nodes(nodes)
        self.upsert_edges(edges)

    def snapshot(self) -> dict:
        raise NotImplementedError

    def export(self, path: str | Path) -> None:
        Path(path).write_text(J.pretty(self.snapshot()), encoding="utf-8")

    def close_missing(self, source: str, run_id: str, at: str) -> int:
        raise NotImplementedError

    def as_of(self, t: str) -> "GraphStore":
        raise NotImplementedError


class MemoryGraphStore(GraphStore):
    """Nodes and edges in dictionaries, with outgoing and incoming adjacency indexes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._out: dict[str, set[str]] = {}
        self._in: dict[str, set[str]] = {}

    # ---------------------------------------------------------------------------------------------- writes

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        with self._lock:
            for n in nodes:
                old = self._nodes.get(n["id"])
                if old is not None:
                    n = _merge_node(old, n)
                self._nodes[n["id"]] = n

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        with self._lock:
            for e in edges:
                old = self._edges.get(e["id"])
                if old is not None and old.get("validFrom") and not e.get("validTo"):
                    e = dict(e, validFrom=old["validFrom"])
                self._edges[e["id"]] = e
                self._out.setdefault(e["from"], set()).add(e["id"])
                self._in.setdefault(e["to"], set()).add(e["id"])

    def close(self, id_: str, at: str, run_id: str | None = None, event_id: str | None = None) -> None:
        """Ends a node's validity and that of every edge from or to it (a tombstone or deletion)."""
        with self._lock:
            n = self._nodes.get(id_)
            if n is None:
                return
            n = dict(n, validTo=at)
            if run_id is not None:
                n["runId"] = run_id
            if event_id is not None:
                n["eventId"] = event_id
            self._nodes[id_] = n
            for eid in list(self._out.get(id_, ())) + list(self._in.get(id_, ())):
                e = self._edges[eid]
                if not e.get("validTo"):
                    self._edges[eid] = dict(e, validTo=at)

    def close_edges_of(self, from_id: str, types: set[str], keep: set[str], at: str) -> None:
        """Ends the outgoing edges of the given types a record no longer produces."""
        with self._lock:
            for eid in list(self._out.get(from_id, ())):
                e = self._edges[eid]
                if e["type"] in types and eid not in keep and not e.get("validTo"):
                    self._edges[eid] = dict(e, validTo=at)

    # ---------------------------------------------------------------------------------------------- reads

    def node(self, id_: str) -> Node | None:
        with self._lock:
            n = self._nodes.get(id_)
            return None if n is None else dict(n)

    def nodes(self, type_: str | None = None, where: dict | None = None) -> list[Node]:
        with self._lock:
            out = []
            for n in self._nodes.values():
                if type_ is not None and n.get("type") != type_:
                    continue
                props = n.get("props") or {}
                if where and any(J.jstr(props.get(k)) != J.jstr(v) for k, v in where.items()):
                    continue
                out.append(dict(n))
            return sorted(out, key=lambda x: x["id"])

    def edges(self, type_: str | None = None) -> list[Edge]:
        with self._lock:
            return sorted((dict(e) for e in self._edges.values() if type_ is None or e["type"] == type_),
                          key=lambda x: x["id"])

    def _steps(self, id_: str, direction: str, types: set[str] | None, history: bool) -> list[tuple[Edge, str]]:
        out = []
        if direction in ("out", "both"):
            for eid in self._out.get(id_, ()):
                e = self._edges[eid]
                if (types is None or e["type"] in types) and (history or not e.get("validTo")):
                    out.append((e, e["to"]))
        if direction in ("in", "both"):
            for eid in self._in.get(id_, ()):
                e = self._edges[eid]
                if (types is None or e["type"] in types) and (history or not e.get("validTo")):
                    out.append((e, e["from"]))
        return sorted(out, key=lambda x: x[0]["id"])

    def _visible(self, id_: str, access: AccessFilter, history: bool) -> bool:
        n = self._nodes.get(id_)
        return n is not None and (history or not n.get("validTo")) and bool(access(n))

    def neighborhood(self, id_: str, depth: int, direction: str, edge_types: Iterable[str] | None,
                     access: AccessFilter, history: bool = False) -> dict:
        _check(direction, access)
        types = None if edge_types is None else set(edge_types)
        with self._lock:
            if not self._visible(id_, access, history):
                return {"nodes": [], "edges": []}
            seen = {id_}
            edges: dict[str, Edge] = {}
            frontier = deque([(id_, 0)])
            while frontier:
                cur, d = frontier.popleft()
                if d >= depth:
                    continue
                for e, other in self._steps(cur, direction, types, history):
                    if not self._visible(other, access, history):
                        continue  # neither the node nor the edge to it, and no traversal through it
                    edges[e["id"]] = e
                    if other not in seen:
                        seen.add(other)
                        frontier.append((other, d + 1))
            return {"nodes": [dict(self._nodes[i]) for i in sorted(seen)],
                    "edges": [dict(edges[k]) for k in sorted(edges)]}

    def traversal(self, start_ids: Iterable[str], edge_types: Iterable[str] | None, direction: str, max_depth: int,
                  access: AccessFilter, history: bool = False) -> list[list[str]]:
        _check(direction, access)
        types = None if edge_types is None else set(edge_types)
        paths: list[list[str]] = []
        with self._lock:
            for start in start_ids:
                if not self._visible(start, access, history):
                    continue
                queue = deque([[start]])
                while queue:
                    path = queue.popleft()
                    if len(path) - 1 >= max_depth:
                        continue
                    for _, other in self._steps(path[-1], direction, types, history):
                        if other in path or not self._visible(other, access, history):
                            continue
                        p = path + [other]
                        paths.append(p)
                        queue.append(p)
        return sorted(paths, key=lambda p: (len(p), p))

    def snapshot(self) -> dict:
        with self._lock:
            return {"nodes": [self._nodes[k] for k in sorted(self._nodes)],
                    "edges": [self._edges[k] for k in sorted(self._edges)]}

    def close_missing(self, source: str, run_id: str, at: str) -> int:
        n = 0
        with self._lock:
            for id_, node in list(self._nodes.items()):
                if node.get("source") == source and node.get("runId") != run_id and not node.get("validTo") \
                        and not (node.get("props") or {}).get("stub"):
                    self.close(id_, at)
                    n += 1
        return n

    def as_of(self, t: str) -> "MemoryGraphStore":
        view = MemoryGraphStore()
        with self._lock:
            def valid(x: dict) -> bool:
                return (not x.get("validFrom") or x["validFrom"] <= t) and (not x.get("validTo") or x["validTo"] > t)
            view.upsert_nodes(dict(n, validTo=None) if n.get("validTo") else dict(n)
                              for n in self._nodes.values() if valid(n))
            view.upsert_edges(dict(e) for e in self._edges.values()
                              if valid(e) and e["from"] in view._nodes and e["to"] in view._nodes)
        for n in view._nodes.values():
            n.pop("validTo", None)
        for e in view._edges.values():
            e.pop("validTo", None)
        return view


class JsonGraphStore(MemoryGraphStore):
    """The memory store loaded from one file (the snapshot document) and saved back with :meth:`save`."""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            try:
                doc = J.loads_first(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise AgentFrontierError.invalid_config("cannot read graph " + str(self.path) + ": " + str(e)) from e
            self.bulk_load(doc.get("nodes") or [], doc.get("edges") or [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(J.pretty(self.snapshot()), encoding="utf-8")
        tmp.replace(self.path)


def _check(direction: str, access: Any) -> None:
    if direction not in ("out", "in", "both"):
        raise AgentFrontierError.invalid_config("direction must be out, in or both")
    if not callable(access):
        raise AgentFrontierError.invalid_config("an access filter is required (pass ALLOW_ALL to see everything)")


def _merge_node(old: Node, new: Node) -> Node:
    """A stub never overwrites a real node; a real node replaces a stub's props; validFrom is kept."""
    if (new.get("props") or {}).get("stub") and not (old.get("props") or {}).get("stub"):
        return old if not old.get("validTo") else dict(old)
    out = dict(new)
    if old.get("validFrom") and not new.get("validTo"):
        out["validFrom"] = old["validFrom"]
    return out


def policy_access(pdp: Any, subject: str | None, levels: list[str], purpose: str | None = None) -> AccessFilter:
    """An access filter from a policy decision point: a node is visible when the PDP allows ``read`` on it."""
    def allow(node: dict) -> bool:
        props = node.get("props") or {}
        resource: dict = {"type": J.jstr(node.get("type")).lower(), "source": node.get("source"),
                          "id": node.get("id"), "labels": list(props.get("labels") or [])}
        for k in levels + ["dataset", "field", "name"]:
            if props.get(k) is not None:
                resource[k] = props[k]
        req = {"subject": subject, "action": "read", "resource": resource}
        if purpose is not None:
            req["purpose"] = purpose
        return bool(pdp.decide(req).allowed)
    return allow


# -------------------------------------------------------------------------------------------------------- builders


class GraphBuilder:
    """Turns records or raw-stream events into nodes and edges in a store (spec docs/graph.md, "Builders")."""

    def __init__(self, store: MemoryGraphStore, graphs: Iterable[str] | None = None, levels: list[str] | None = None,
                 tenant: str | None = None, run_id: str | None = None, observed_at: str | None = None):
        self.store = store
        self.graphs = set(graphs or GRAPHS)
        unknown = self.graphs - set(GRAPHS)
        if unknown:
            raise AgentFrontierError.invalid_config("unknown graphs " + J.jlist(sorted(unknown)))
        self.levels = list(levels or [])
        self.tenant = tenant or "default"
        self.run_id = run_id
        self.observed_at = observed_at
        self._audit_last: dict[str, tuple[int, str]] = {}

    def add(self, records: Iterable[Record]) -> None:
        for r in records:
            graph = _GRAPH_OF.get(r.entity)
            if graph is None or graph not in self.graphs:
                continue
            env = getattr(r, "envelope", None) or {}
            op = env.get("op", "upsert")
            meta = {"tenant": env.get("tenantId", self.tenant), "assertion": env.get("assertion", "observed"),
                    "confidence": env.get("confidence", 1.0), "runId": env.get("runId", self.run_id),
                    "eventId": env.get("eventId"), "at": env.get("observedAt", self.observed_at)}
            rec = getattr(r, "record", r)
            if op in ("tombstone", "delete"):
                self._remove(rec, meta)
            else:
                getattr(self, "_" + r.entity)(rec, meta)

    # ---------------------------------------------------------------------------------------------- helpers

    def _node(self, id_: str, type_: str, r: Record | None, meta: dict, props: dict | None = None,
              stub: bool = False) -> None:
        if props is None:
            props = {k: v for k, v in (r.values if r is not None else {}).items() if v is not None}
        if stub:
            props = dict(props, stub=True)
        n: dict = {"id": id_, "type": type_, "tenant": meta["tenant"],
                   "source": props.get("source") if r is None else r.values.get("source"), "props": props,
                   "assertion": meta["assertion"], "confidence": meta["confidence"]}
        if meta.get("at"):
            n["validFrom"] = meta["at"]
        if meta.get("runId"):
            n["runId"] = meta["runId"]
        if meta.get("eventId"):
            n["eventId"] = meta["eventId"]
        self.store.upsert_nodes([n])

    def _stub(self, id_: str, type_: str, meta: dict, props: dict) -> None:
        if self.store.node(id_) is None:
            self._node(id_, type_, None, meta, props, stub=True)

    def _edge(self, from_: str, type_: str, to: str, meta: dict, props: dict | None = None,
              key: str | None = None) -> str:
        e: dict = {"id": edge_id(from_, type_, to, key), "type": type_, "from": from_, "to": to,
                   "props": {k: v for k, v in (props or {}).items() if v is not None}, "tenant": meta["tenant"],
                   "assertion": meta["assertion"], "confidence": meta["confidence"]}
        if meta.get("at"):
            e["validFrom"] = meta["at"]
        if meta.get("runId"):
            e["runId"] = meta["runId"]
        if meta.get("eventId"):
            e["eventId"] = meta["eventId"]
        self.store.upsert_edges([e])
        return e["id"]

    def _u(self, r: Record) -> dict:
        return uids.row(r, self.levels)

    def _parent(self, r: Record, u: dict, meta: dict) -> None:
        parent = u.get("parentUid")
        if parent:
            self._stub(parent, "Source" if parent == r.values.get("source") else "Container", meta,
                       {"source": r.values.get("source")})
            self._edge(parent, "CONTAINS", u["uid"], meta)

    def _dataset_uid(self, r: Record, prefix: str = "", name_key: str = "dataset") -> str | None:
        name = r.values.get(name_key)
        if name is None:
            return None
        return uids.path_of(r.values.get("source"), r, self.levels, prefix) + "/" + J.jstr(name)

    # ---------------------------------------------------------------------------------------------- asset

    def _source(self, r: Record, meta: dict) -> None:
        self._node(J.jstr(r.values.get("source")), "Source", r, meta)

    def _container(self, r: Record, meta: dict) -> None:
        u = self._u(r)
        self._node(u["uid"], "Container", r, meta)
        self._parent(r, u, meta)

    def _dataset(self, r: Record, meta: dict) -> None:
        u = self._u(r)
        self._node(u["uid"], "Dataset", r, meta)
        self._parent(r, u, meta)

    def _routine(self, r: Record, meta: dict) -> None:
        u = self._u(r)
        self._node(u["uid"], "Routine", r, meta)
        self._parent(r, u, meta)

    def _field(self, r: Record, meta: dict) -> None:
        u = self._u(r)
        ds = u.get("datasetUid")
        self._node(u["uid"], "Field", r, meta)
        if ds:
            self._stub(ds, "Dataset", meta, {"source": r.values.get("source"), "name": r.values.get("dataset")})
            self._edge(ds, "HAS_FIELD", u["uid"], meta)

    def _index(self, r: Record, meta: dict) -> None:
        u = self._u(r)
        self._node(u["uid"], "Index", r, meta)
        if u.get("datasetUid"):
            self._edge(u["datasetUid"], "HAS_INDEX", u["uid"], meta)

    def _constraint(self, r: Record, meta: dict) -> None:
        if r.values.get("type") != "foreign_key" or r.values.get("refDataset") is None:
            return
        u = self._u(r)
        ds, ref = u.get("datasetUid"), u.get("refUid")
        if not ds or not ref:
            return
        self._stub(ref, "Dataset", meta, {"source": r.values.get("source"), "name": r.values.get("refDataset")})
        self._edge(ds, "REFERENCES", ref, meta, {"name": r.values.get("name"), "columns": r.values.get("columns"),
                                                 "refColumns": r.values.get("refColumns")}, r.values.get("name"))

    def _target(self, r: Record) -> str:
        """The most specific object a record names: its field, its dataset, else its level path."""
        ds = self._dataset_uid(r)
        if ds is not None and r.values.get("field") is not None:
            return ds + "/" + J.jstr(r.values["field"])
        return ds if ds is not None else uids.path_of(r.values.get("source"), r, self.levels, "")

    def _tag(self, r: Record, meta: dict) -> None:
        name, value = r.values.get("name"), r.values.get("value")
        tid = J.jstr(r.values.get("source")) + "/tag/" + J.jstr(name) + ("" if value is None else "=" + J.jstr(value))
        self._node(tid, "Tag", None, meta, {"source": r.values.get("source"), "name": name, "value": value})
        self._edge(self._target(r), "TAGGED", tid, meta)

    def _label(self, r: Record, meta: dict) -> None:
        lid = J.jstr(r.values.get("source")) + "/label/" + J.jstr(r.values.get("name"))
        self._node(lid, "Label", None, meta, {"source": r.values.get("source"), "name": r.values.get("name"),
                                               "labelType": r.values.get("labelType")})
        self._edge(self._target(r), "LABELED", lid, meta, {"engine": r.values.get("engine"),
                                                            "reviewStatus": r.values.get("reviewStatus")},)

    def _context_node(self, r: Record, meta: dict, type_: str, kind: str) -> None:
        nid = J.jstr(r.values.get("source")) + "/" + kind + "/" + J.jstr(r.values.get("name"))
        self._node(nid, type_, r, meta)
        if r.values.get("dataset") is not None:
            self._edge(nid, "DEFINES", self._target(r), meta)

    def _business_term(self, r: Record, meta: dict) -> None:
        self._context_node(r, meta, "BusinessTerm", "term")

    def _metric(self, r: Record, meta: dict) -> None:
        self._context_node(r, meta, "Metric", "metric")

    # ---------------------------------------------------------------------------------------------- lineage

    def _lineage(self, r: Record, meta: dict) -> None:
        down = self._dataset_uid(r)
        up = self._dataset_uid(r, "upstream", "upstreamDataset")
        if down is None or up is None:
            return
        src = r.values.get("source")
        self._stub(down, "Dataset", meta, {"source": src, "name": r.values.get("dataset")})
        self._stub(up, "Dataset", meta, {"source": src, "name": r.values.get("upstreamDataset")})
        self._edge(down, "DERIVED_FROM", up, meta, {"kind": r.values.get("kind")})
        f, uf = r.values.get("field"), r.values.get("upstreamField")
        if f is not None and uf is not None:
            df, ufid = down + "/" + J.jstr(f), up + "/" + J.jstr(uf)
            self._stub(df, "Field", meta, {"source": src, "dataset": r.values.get("dataset"), "name": f})
            self._stub(ufid, "Field", meta, {"source": src, "dataset": r.values.get("upstreamDataset"), "name": uf})
            self._edge(df, "DERIVED_FROM", ufid, meta, {"kind": r.values.get("kind"),
                                                          "transformation": r.values.get("transformation")})

    # ---------------------------------------------------------------------------------------------- authority

    def _principal(self, r: Record, meta: dict) -> None:
        pid = J.jstr(r.values.get("source")) + "/principal/" + J.jstr(r.values.get("name"))
        self._node(pid, "Principal", r, meta)
        for g in r.values.get("memberOf") or []:
            gid = J.jstr(r.values.get("source")) + "/principal/" + J.jstr(g)
            self._stub(gid, "Principal", meta, {"source": r.values.get("source"), "name": g})
            self._edge(pid, "MEMBER_OF", gid, meta)

    def _grant(self, r: Record, meta: dict) -> None:
        pid = J.jstr(r.values.get("source")) + "/principal/" + J.jstr(r.values.get("principal"))
        self._stub(pid, "Principal", meta, {"source": r.values.get("source"), "name": r.values.get("principal")})
        target = self._target(r)
        if self.store.node(target) is None:
            self._stub(target, "Dataset" if r.values.get("dataset") is not None else "Container", meta,
                       {"source": r.values.get("source"), "name": r.values.get("dataset")})
        self._edge(pid, "HAS_PRIVILEGE", target, meta, {"privilege": r.values.get("privilege"),
                                                         "grantable": r.values.get("grantable")},
                   J.jstr(r.values.get("privilege")))

    def _delegation(self, r: Record, meta: dict) -> None:
        src = J.jstr(r.values.get("source"))
        p, d = src + "/principal/" + J.jstr(r.values.get("principal")), src + "/principal/" + J.jstr(r.values.get("delegate"))
        for pid, name in ((p, r.values.get("principal")), (d, r.values.get("delegate"))):
            self._stub(pid, "Principal", meta, {"source": src, "name": name})
        self._edge(p, "DELEGATES", d, meta, {"scope": r.values.get("scope"), "validFrom": r.values.get("validFrom"),
                                             "validTo": r.values.get("validTo")})

    def _relationship(self, r: Record, meta: dict) -> None:
        if r.values.get("predicate") != "member_of":
            return
        src = J.jstr(r.values.get("source"))
        s, o = src + "/principal/" + J.jstr(r.values.get("subject")), src + "/principal/" + J.jstr(r.values.get("object"))
        for pid, name in ((s, r.values.get("subject")), (o, r.values.get("object"))):
            self._stub(pid, "Principal", meta, {"source": src, "name": name})
        self._edge(s, "MEMBER_OF", o, meta)

    def _policy(self, r: Record, meta: dict) -> None:
        u = self._u(r)
        self._node(u["uid"], "Policy", r, meta)
        if u.get("datasetUid"):
            self._edge(u["uid"], "APPLIES_TO", u["datasetUid"], meta)

    # ---------------------------------------------------------------------------------------------- quality, audit

    def _quality_result(self, r: Record, meta: dict) -> None:
        v = r.values
        qid = J.jstr(v.get("source")) + "#quality/" + J.jstr(v.get("check")) + "/" \
            + J.jstr(v.get("dataset")) + "/" + J.jstr(v.get("field")) + "@" + J.jstr(v.get("measuredAt"))
        self._node(qid, "QualityResult", r, meta)
        target = self._target(r) if v.get("dataset") is not None else J.jstr(v.get("source"))
        self._stub(target, "Dataset" if v.get("dataset") is not None else "Source", meta, {"source": v.get("source")})
        self._edge(qid, "CHECKS", target, meta)

    def _statistic(self, r: Record, meta: dict) -> None:
        target = self._target(r)
        n = self.store.node(target)
        if n is not None:
            props = dict(n.get("props") or {})
            for k in ("rowCount", "nullFraction", "distinctCount", "avgWidth", "min", "max"):
                if r.values.get(k) is not None:
                    props[k] = r.values[k]
            self.store.upsert_nodes([dict(n, props=props)])

    def _audit_event(self, r: Record, meta: dict) -> None:
        v = r.values
        chain = J.jstr(v.get("correlationId") or meta.get("runId"))
        aid = J.jstr(v.get("source")) + "#audit/" + chain + "/" + J.jstr(v.get("sequence"))
        self._node(aid, "AuditEvent", r, meta)
        seq = v.get("sequence")
        last = self._audit_last.get(chain)
        if last is not None and isinstance(seq, int) and seq > last[0]:
            self._edge(last[1], "PRECEDES", aid, meta)
        if isinstance(seq, int):
            self._audit_last[chain] = (seq, aid)
        res = v.get("resource")
        if res is not None and self.store.node(J.jstr(res)) is not None:
            self._edge(aid, "ABOUT", J.jstr(res), meta)

    # ---------------------------------------------------------------------------------------------- removal

    def _remove(self, r: Record, meta: dict) -> None:
        at = meta.get("at") or ""
        e = r.entity
        if e in ("source", "container", "dataset", "field", "index", "routine", "policy"):
            u = self._u(r)
            self.store.close(u["uid"], at, meta.get("runId"), meta.get("eventId"))
        elif e == "principal":
            self.store.close(J.jstr(r.values.get("source")) + "/principal/" + J.jstr(r.values.get("name")), at)
        elif e == "lineage":
            down, up = self._dataset_uid(r), self._dataset_uid(r, "upstream", "upstreamDataset")
            if down and up:
                f, uf = r.values.get("field"), r.values.get("upstreamField")
                if f is not None and uf is not None:
                    self._close_edge(edge_id(down + "/" + J.jstr(f), "DERIVED_FROM", up + "/" + J.jstr(uf)), at)
                else:
                    self._close_edge(edge_id(down, "DERIVED_FROM", up), at)
        elif e == "grant":
            pid = J.jstr(r.values.get("source")) + "/principal/" + J.jstr(r.values.get("principal"))
            self._close_edge(edge_id(pid, "HAS_PRIVILEGE", self._target(r), J.jstr(r.values.get("privilege"))), at)
        elif e == "constraint":
            u = self._u(r)
            if u.get("datasetUid") and u.get("refUid"):
                self._close_edge(edge_id(u["datasetUid"], "REFERENCES", u["refUid"], r.values.get("name")), at)
        elif e in ("business_term", "metric"):
            kind = "term" if e == "business_term" else "metric"
            self.store.close(J.jstr(r.values.get("source")) + "/" + kind + "/" + J.jstr(r.values.get("name")), at)

    def _close_edge(self, eid: str, at: str) -> None:
        with self.store._lock:  # noqa: SLF001 - the builder owns its store's edges
            e = self.store._edges.get(eid)  # noqa: SLF001
            if e is not None and not e.get("validTo"):
                self.store._edges[eid] = dict(e, validTo=at)  # noqa: SLF001


# -------------------------------------------------------------------------------------------------------- sink


class GraphSink(Sink):
    """``{"type": "graph", "store": "json" | "memory", "path": ..., "name": ..., "graphs": [...]}``: builds the graphs
    from a run's records; the JSON store is saved when the run ends."""

    _NAMED: dict[str, MemoryGraphStore] = {}

    def __init__(self, c: dict):
        kind = J.text(c, "store", "json")
        if kind == "json":
            path = J.text(c, "path")
            if not path:
                raise AgentFrontierError.invalid_config("graph sink with store json needs 'path'")
            self.store: MemoryGraphStore = JsonGraphStore(path)
        elif kind == "memory":
            name = J.text(c, "name", "default") or "default"
            self.store = GraphSink._NAMED.setdefault(name, MemoryGraphStore())
        else:
            raise AgentFrontierError.invalid_config("graph sink store must be json or memory")
        self.graphs = J.strings(c.get("graphs")) or list(GRAPHS)
        self.builder: GraphBuilder | None = None
        self._lock = threading.Lock()

    @staticmethod
    def named(name: str) -> MemoryGraphStore | None:
        return GraphSink._NAMED.get(name)

    def begin(self, context: RunContext) -> None:
        self.builder = GraphBuilder(self.store, self.graphs, list(context.levels), None, context.run_id,
                                    context.started_at)

    def write(self, records: list[Record]) -> None:
        with self._lock:
            assert self.builder is not None
            self.builder.add(records)

    def end(self, report: Any) -> None:
        if isinstance(self.store, JsonGraphStore):
            self.store.save()


__all__ = ["GraphStore", "MemoryGraphStore", "JsonGraphStore", "GraphBuilder", "GraphSink", "ALLOW_ALL",
           "policy_access", "edge_id", "GRAPHS"]
