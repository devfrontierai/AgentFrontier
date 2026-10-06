"""Runs a definition. This module is the same algorithm in every language implementation; the protocols only
execute single statements or requests."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import json_util as J
from . import mapping as M
from . import template as T
from .definitions import Facts, Level, SourceDefinition, VersionRange, json_merge
from .errors import (AUTH_FAILED, CONNECTION_FAILED, INVALID_CONFIG, UNSUPPORTED, AgentFrontierError,
                     root_message)
from .model import (ConnectionConfig, Page, QueryRequest, QueryResult, Record, Scope, SessionStats, SourceInfo,
                    TestResult)
from .protocols import ProtocolSession, RawResult
from .spec_model import EntityModel

log = logging.getLogger("agentfrontier.engine")

MAX_PAGES = 100_000
DEFAULT_PAGE = 1000
SPECIAL = {"test", "source", "sample"}
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;[^,]*rel="?next"?')


@dataclass(frozen=True)
class Detected:
    """What detection found; cached by :class:`AgentFrontier` so repeated sessions do not repeat it."""

    definition: str | None
    version: str | None
    banner: str | None
    variant: str | None


@dataclass
class _Mapped:
    records: list[dict]
    raw_count: int
    raw: RawResult


def _with_entity(op: dict, entity: str) -> dict:
    o = J.deep_copy(op)
    if "entity" not in o:
        o["entity"] = entity
    return o


def _name_only(name: str) -> dict:
    return {"name": name}


def as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [J.jtrim(J.jstr(o)) for o in v if o is not None and not J.is_blank(J.jstr(o))]
    return [J.jtrim(s) for s in J.jstr(v).split(",") if not J.is_blank(s)]


def encode_cursor(op: int, value: Any) -> str:
    raw = J.write({"o": op, "v": value}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[int, Any]:
    if not cursor:
        return 0, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        n = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        o = n.get("o", 0) if isinstance(n, dict) else 0
        o = J.as_int(o) if J.is_number(o) else int(J.as_text(o))
        return o, n.get("v") if isinstance(n, dict) else None
    except Exception:  # noqa: BLE001
        raise AgentFrontierError.invalid_config("invalid page cursor") from None


class EngineSession:
    """An open handle on one source, the pull-style API. Not thread-safe: open one session per worker thread. Always
    close it (or use it as a context manager); it may hold connections."""

    def __init__(self, connection: ConnectionConfig, requested: SourceDefinition, proto: ProtocolSession,
                 stats: SessionStats, temp: T.TempFiles, model: EntityModel, properties: dict, options: dict):
        self._connection = connection
        self._requested = requested
        self._def = requested
        self._proto = proto
        self._stats = stats
        self._temp = temp
        self._model = model
        self._properties = properties
        self._options = options
        self._warnings: list[str] = []
        self._warned: set[str] = set()
        self._lock = threading.Lock()
        self._alternative_choice: dict[str, int] = {}
        self._scope_cache: dict[str, tuple[Scope, list[dict]]] = {}
        self._info: SourceInfo | None = None

    def __enter__(self) -> "EngineSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ============================================================================================ detection

    def detect(self, known: Detected | None, lookup: Callable[[str], SourceDefinition]) -> Detected:
        """Finds the version and variant, switches definition when a variant says so, and applies overlays."""
        variant = None
        current = self._requested
        if known is not None:
            version, banner, variant = known.version, known.banner, known.variant
            if known.definition is not None and known.definition != self._requested.id:
                current = lookup(known.definition)
        else:
            version, banner = self._read_version(self._requested)
            for v in self._requested.variants:
                if not self._variant_matches(v, version, banner):
                    continue
                switch_to = J.text(v, "definition")
                variant = J.text(v, "variant", switch_to)
                if switch_to is not None:
                    target = lookup(switch_to)
                    if target.protocol is not None and target.protocol == self._requested.protocol:
                        current = target
                        if target.version is not None:
                            again = self._read_version(target)
                            version = again[0] if again[0] is not None else version
                            banner = again[1] if again[1] is not None else banner
                break
        self._def = current.effective(Facts(version, variant, self._options, self._properties))
        self._info = SourceInfo(self._def.id, self._requested.id, self._def.name, version, banner, variant, {})
        log.debug("%s: %s version %s variant %s", self._connection.id, self._def.id, version, variant)
        return Detected(self._def.id, version, banner, variant)

    def _read_version(self, d: SourceDefinition) -> tuple[str | None, str | None]:
        spec = d.version
        if spec is None:
            return None, None
        try:
            recs = self.run_all("version", _with_entity(spec, "source"), Scope.root(), self.base_vars(Scope.root()))
            if not recs:
                return None, None
            r = recs[0]
            text = None if r.get("version") is None else J.jstr(r.get("version"))
            banner = None if r.get("banner") is None else J.jstr(r.get("banner"))
            pattern = J.text(spec, "pattern")
            version = None
            for candidate in (text, banner):
                if candidate is None or version is not None:
                    continue
                if pattern is not None:
                    m = re.search(pattern, candidate)
                    if m:
                        version = m.group(1) if m.re.groups > 0 else m.group()
                else:
                    c = VersionRange.extract(candidate)
                    if c is not None:
                        version = ".".join(str(x) for x in c)
            return version, (text if banner is None else banner)
        except AgentFrontierError as e:
            if e.code in (CONNECTION_FAILED, AUTH_FAILED, INVALID_CONFIG, UNSUPPORTED):
                raise
            self._warn("version", "cannot detect the version (" + e.message
                       + "); version-specific queries are not applied")
            return None, None

    def _variant_matches(self, v: Any, version: str | None, banner: str | None) -> bool:
        match = J.text(v, "match")
        if match is not None:
            hay = (banner or "") + " " + (version or "")
            return re.search(match, hay, re.IGNORECASE) is not None
        probe = J.text(v, "probe")
        if probe is not None:
            op: dict = {"entity": "source"}
            if self._requested.protocol == "http":
                op["request"] = {"path": probe}
            else:
                op["sql"] = probe
            try:
                self._proto.execute(op, T.Vars.of(self.base_vars(Scope.root())), Scope.root())
                return True
            except AgentFrontierError:
                return False
        return False

    # ============================================================================================ API

    @property
    def connection(self) -> ConnectionConfig:
        return self._connection

    @property
    def definition(self) -> SourceDefinition:
        """The definition in effect: after variant detection and with the overlays for the detected version."""
        return self._def

    @property
    def info(self) -> SourceInfo:
        return self._info  # type: ignore[return-value]

    @property
    def hierarchy(self) -> list[Level]:
        return self._def.hierarchy

    def test(self) -> TestResult:
        """Runs the definition's test operation. Never raises for connection or permission problems."""
        start = time.perf_counter_ns()
        try:
            op = self._def.operation("test")
            name = "test"
            if op is None:
                op = self._def.operation("source")
                name = "source"
            details: dict = {}
            if op is not None:
                recs = self.run_all(name, _with_entity(op, "source"), Scope.root(), self.base_vars(Scope.root()))
                if recs:
                    for k, v in recs[0].items():
                        if isinstance(v, dict):
                            for a, b in v.items():
                                details[J.jstr(a)] = b
                        else:
                            details[k] = v
            elif self._def.version is None:
                raise AgentFrontierError.invalid_definition("definition has no test, source or version operation")
            info = self.info
            return TestResult(True, "connected", self._def.id, self._def.name, info.version, info.variant,
                              (time.perf_counter_ns() - start) // 1_000_000, details)
        except AgentFrontierError as e:
            return TestResult.failed(e.message, (time.perf_counter_ns() - start) // 1_000_000)
        except Exception as e:  # noqa: BLE001
            return TestResult.failed(root_message(e), (time.perf_counter_ns() - start) // 1_000_000)

    def source(self) -> Record:
        """The source record (product, version, current login, ...)."""
        values: dict = {}
        op = self._def.operation("source")
        if op is not None:
            try:
                recs = self.run_all("source", _with_entity(op, "source"), Scope.root(), self.base_vars(Scope.root()))
                if recs:
                    values.update(recs[0])
            except AgentFrontierError as e:
                self._handle("source", op, e)
        info = self.info
        if values.get("product") is None:
            values["product"] = self._def.name
        if info.version is not None and values.get("version") is None:
            values["version"] = info.version
        if info.banner is not None and values.get("banner") is None:
            values["banner"] = info.banner
        if info.variant is not None:
            values["variant"] = info.variant
        values["definition"] = self._def.id
        values["name"] = self._connection.name
        return self.enrich("source", values, Scope.root())

    def containers(self, parent: Scope) -> list[Record]:
        """The containers one level below ``parent``, after the user's selection, the definition's exclusions and
        the ``include<Level>s`` / ``exclude<Level>s`` options."""
        levels = self._def.hierarchy
        depth = parent.depth
        if depth >= len(levels):
            return []
        level = levels[depth]
        vars = self.base_vars(parent)
        value = None if level.value is None else T.render_text(level.value, T.Vars.of(vars), T.LENIENT, self._temp)
        has_value = bool(value)
        selection = [] if level.select is None else as_list(self._properties.get(level.select))
        all_ = len(selection) == 1 and selection[0] == "*"
        found: list[dict] = []
        explicit = False
        if level.list is None:
            if has_value:
                found.append(_name_only(value))
                explicit = True
        else:
            op = self._def.operation(level.list)
            if op is None:
                raise AgentFrontierError.invalid_definition("list operation '" + level.list + "' is not active")
            listed = self.run_all(level.list, _with_entity(op, "container"), parent, vars)
            listed = M.filter_records(op.get("filter"), listed, self._options)
            if not selection and has_value:
                for r in listed:
                    if J.equals_ignore_case(value, J.jstr(r.get("name"))):
                        found.append(r)
                        break
                if not found:
                    found.append(_name_only(value))
                explicit = True
            elif not selection or all_:
                found.extend(listed)
            else:
                f = M.NameFilter(selection, [])
                for r in listed:
                    if f.accepts(J.jstr(r.get("name"))):
                        found.append(r)
                explicit = True
        system = M.NameFilter([], list(level.exclude))
        user = M.NameFilter(as_list(self._options.get(level.include_option)),
                            as_list(self._options.get(level.exclude_option)))
        out = []
        for r in found:
            name = J.jstr(r.get("name"))
            if (not explicit and not system.accepts(name)) or not user.accepts(name):
                continue
            values = dict(r)
            values["level"] = level.level
            values[level.level] = name
            out.append(self.enrich("container", values, parent))
        return out

    def datasets(self, scope: Scope, cursor: str | None = None, page_size: int = 0) -> Page:
        """One page of datasets in a leaf scope, after the ``includeDatasets`` / ``excludeDatasets`` options."""
        ops = self.ops_for("dataset")
        if not ops:
            return Page([], None)
        index, cur = decode_cursor(cursor)
        if index < 0 or index >= len(ops):
            raise AgentFrontierError.invalid_config("invalid page cursor")
        name, op = ops[index]
        mode = J.text(op.get("paging"), "mode")
        nxt = None
        if mode is None:
            records = self.run_all(name, op, scope, self.base_vars(scope))
        else:
            size = page_size if page_size > 0 else J.integer(op.get("paging"), "size", DEFAULT_PAGE)
            vars = self.base_vars(scope)
            vars.update(self._page_vars(op, mode, cur, size))
            m = self._execute_once(name, op, scope, vars)
            records = m.records
            nxt = self._next_cursor(op, mode, m, cur, size)
        next_cursor = encode_cursor(index, nxt) if nxt is not None else (
            encode_cursor(index + 1, None) if index + 1 < len(ops) else None)
        records = M.filter_records(op.get("filter"), records, self._options)
        f = self._dataset_filter()
        out = [self.enrich("dataset", r, scope) for r in records
               if f.accepts(None if r.get("name") is None else J.jstr(r.get("name")))]
        return Page(out, next_cursor)

    def _dataset_filter(self) -> M.NameFilter:
        inc = as_list(self._options.get("includeDatasets")) + as_list(self._options.get("includeTables"))
        exc = as_list(self._options.get("excludeDatasets")) + as_list(self._options.get("excludeTables"))
        return M.NameFilter(inc, exc)

    def dataset_names(self, scope: Scope) -> list[str]:
        """All dataset names of a leaf scope, paging through."""
        names: list[str] = []
        cursor = None
        guard = 0
        while True:
            p = self.datasets(scope, cursor, DEFAULT_PAGE)
            names.extend(r.string("name") for r in p.records)
            cursor = p.next_cursor
            guard += 1
            if cursor is None or guard >= MAX_PAGES:
                break
        return names

    def records(self, entity: str, scope: Scope, datasets: Iterable[str] | None = None) -> list[Record]:
        """Records of one entity in a scope: fields, constraints, indexes, statistics, grants for some datasets of a
        leaf scope, or lineage, routines, policies, principals for a whole scope."""
        return self.collect_entity(entity, scope, None if datasets is None else list(datasets), None)

    def collect_entity(self, entity: str, scope: Scope, datasets: list[str] | None,
                       only_scopes: set[str] | None) -> list[Record]:
        out: list[Record] = []
        for name, op in self.ops_for(entity):
            out.extend(self.run_operation(name, op, scope, datasets, only_scopes, {}))
        return out

    def run_operation(self, name: str, op: dict, scope: Scope, datasets: list[str] | None,
                      only_scopes: set[str] | None, params: dict) -> list[Record]:
        """Runs one operation over a scope with the scope semantics of its ``scope``."""
        entity = J.text(op, "entity", "record")
        enabled_by = J.text(op, "enabledBy")
        if enabled_by is not None and M.coerce(self._options.get(enabled_by, True), "boolean") is not True:
            return []
        op_scope = J.text(op, "scope", "source")
        batch = op_scope in ("datasets", "dataset")
        if only_scopes is not None and ("batch" if batch else op_scope) not in only_scopes:
            return []
        recs: list[dict] = []
        try:
            vars = self.base_vars(scope)
            vars.update(params)
            if op_scope == "datasets":
                names = datasets if datasets is not None else self.dataset_names(scope)
                size = max(1, min(J.integer(op, "batch", 2 ** 31 - 1),
                                  J.integer(self._def.session, "maxBatch", 1000)))
                for i in range(0, len(names), size):
                    v = dict(vars)
                    v["datasets"] = names[i:i + size]
                    recs.extend(self.run_all(name, op, scope, v))
            elif op_scope == "dataset":
                names = datasets if datasets is not None else self.dataset_names(scope)
                for d in names:
                    v = dict(vars)
                    v["dataset"] = d
                    for r in self.run_all(name, op, scope, v):
                        if r.get("dataset") is None:
                            r["dataset"] = d
                        recs.append(r)
            else:
                key = Scope.root() if op_scope == "source" else scope.up_to(op_scope)
                cached = self._scope_cache.get(name)
                if cached is not None and cached[0] == key and not params:
                    all_ = cached[1]
                else:
                    bv = self.base_vars(key)
                    bv.update(params)
                    all_ = self.run_all(name, op, key, bv)
                    if not params:
                        self._scope_cache[name] = (key, all_)
                if datasets is not None and entity != "dataset":
                    wanted = set(datasets)
                    for r in all_:
                        d = r.get("dataset")
                        if d is None or J.jstr(d) in wanted:
                            recs.append(r)
                else:
                    recs.extend(all_)
                scope = key
        except AgentFrontierError as ex:
            self._handle(name, op, ex)
            return []
        recs = M.filter_records(op.get("filter"), recs, self._options)
        return [self.enrich(entity, r, scope) for r in recs]

    def supports(self, entity: str) -> bool:
        """Whether any active operation produces this entity."""
        return bool(self.ops_for(entity))

    def run(self, operation: str, scope: Scope, params: dict | None = None) -> list[Record]:
        """Runs one named operation with extra template variables."""
        params = dict(params or {})
        op = self._def.operation(operation)
        if op is None:
            raise AgentFrontierError.unsupported(str(self._def.name) + " has no active operation '" + operation + "'")
        datasets = None
        d = params.get("datasets")
        if isinstance(d, (list, tuple, set)):
            datasets = [J.jstr(o) for o in d]
        return self.run_operation(operation, op, scope, datasets, None, params)

    def sample(self, scope: Scope, dataset: str, limit: int = 10) -> list[Record]:
        """First rows of a dataset, as flat records of entity ``row``."""
        op = self._def.operation("sample")
        if op is None:
            raise AgentFrontierError.unsupported(str(self._def.name) + " has no sample operation")
        vars = self.base_vars(scope)
        vars["dataset"] = dataset
        vars["limit"] = int(limit)
        return [Record("row", r) for r in self.run_all("sample", _with_entity(op, "row"), scope, vars)]

    def query(self, request: QueryRequest | str) -> QueryResult:
        """Read-only ad hoc query (SQL sources)."""
        if isinstance(request, str):
            request = QueryRequest(request)
        return self._proto.query(request)

    @property
    def stats(self) -> SessionStats:
        """Load put on the source so far (``stats.snapshot()``)."""
        return self._stats

    @property
    def warnings(self) -> list[str]:
        with self._lock:
            return list(self._warnings)

    def close(self) -> None:
        try:
            self._proto.close()
        finally:
            self._temp.close()

    # ============================================================================================ execution

    def ops_for(self, entity: str) -> list[tuple[str, dict]]:
        """Active operations producing an entity, in definition order, excluding hierarchy lists and special ones."""
        lists = {lv.list for lv in self._def.hierarchy if lv.list is not None}
        return [(k, v) for k, v in self._def.operations.items()
                if k not in SPECIAL and k not in lists and J.text(v, "entity") == entity]

    def base_vars(self, scope: Scope) -> dict:
        v = dict(self._properties)
        v["options"] = self._options
        v["source"] = self._connection.id
        if self._info is not None:
            v["version"] = self._info.version
            v["variant"] = self._info.variant
        v.update(scope.as_map())
        return v

    def run_all(self, name: str, op: dict, scope: Scope, vars: dict) -> list[dict]:
        """Every page of an operation."""
        paging = op.get("paging")
        mode = J.text(paging, "mode")
        if mode is None:
            return self._execute_once(name, op, scope, vars).records
        size = J.integer(paging, "size", DEFAULT_PAGE)
        all_: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            v = dict(vars)
            v.update(self._page_vars(op, mode, cursor, size))
            m = self._execute_once(name, op, scope, v)
            all_.extend(m.records)
            nxt = self._next_cursor(op, mode, m, cursor, size)
            if nxt is None or nxt == cursor:
                break
            cursor = nxt
        return all_

    def _page_vars(self, op: dict, mode: str, cursor: Any, size: int) -> dict:
        v: dict = {"limit": size}
        paging = op.get("paging")
        if mode == "keyset":
            v["after"] = cursor
        elif mode == "offset":
            v["offset"] = 0 if cursor is None else cursor
        elif mode == "page":
            v["page"] = J.integer(paging, "start", 1) if cursor is None else cursor
        elif mode == "cursor":
            v["cursor"] = cursor
        elif mode in ("nextUrl", "linkHeader"):
            v["nextUrl"] = cursor
        else:
            raise AgentFrontierError.invalid_definition("unknown paging mode " + mode)
        return v

    def _next_cursor(self, op: dict, mode: str, m: _Mapped, cursor: Any, size: int) -> Any:
        paging = op.get("paging")
        full = m.raw_count >= size
        if mode == "keyset":
            if not full or not m.records:
                return None
            return m.records[-1].get(J.text(paging, "key", "name"))
        if mode == "offset":
            return (0 if cursor is None else int(cursor)) + m.raw_count if full else None
        if mode == "page":
            if not full:
                return None
            current = J.integer(paging, "start", 1) if cursor is None else int(cursor)
            return current + 1
        if mode in ("cursor", "nextUrl"):
            if m.raw_count == 0:
                return None
            nxt = M.json_path_first(m.raw.body, J.text(paging, "next", "$.next"))
            return None if nxt is None or J.jstr(nxt) == "" else J.jstr(nxt)
        if mode == "linkHeader":
            for h in m.raw.headers.get("link", []):
                x = _LINK_NEXT.search(h)
                if x:
                    return x.group(1)
            return None
        return None

    def _execute_once(self, name: str, op: dict, scope: Scope, vars: dict) -> _Mapped:
        """One execution, trying the operation's alternatives in order; the one that works is remembered."""
        candidates: list[dict] = []
        main = J.deep_copy(op)
        main.pop("alternatives", None)
        if "sql" in main or "request" in main or "files" in main:
            candidates.append(main)
        alts = op.get("alternatives")
        for alt in alts if isinstance(alts, list) else []:
            candidates.append(json_merge(main, alt))
        if not candidates:
            raise AgentFrontierError.invalid_definition("operation '" + name + "' has nothing to execute")
        first = min(self._alternative_choice.get(name, 0), len(candidates) - 1)
        last: AgentFrontierError | None = None
        for i in range(first, len(candidates)):
            c = candidates[i]
            try:
                raw = self._proto.execute(c, T.Vars.of(vars), scope)
                rows = raw.rows if raw.rows is not None else M.extract(c.get("records"), raw.body)
                entity = J.text(c, "entity", "record")
                mapped = M.map_rows(c, rows, self._model.entity(entity), self._level_names())
                mapped = M.collect(c.get("collect"), mapped)
                if i != first or name not in self._alternative_choice:
                    self._alternative_choice[name] = i
                return _Mapped(mapped, len(rows), raw)
            except AgentFrontierError as e:
                last = e
                if e.code == CONNECTION_FAILED:
                    raise
                if i + 1 < len(candidates):
                    log.debug("%s: operation %s alternative %d failed (%s), trying the next", self._connection.id,
                              name, i, e.message)
        assert last is not None
        raise last

    def _handle(self, name: str, op: dict, e: AgentFrontierError) -> None:
        policy = J.text(op, "onError", "fail")
        if policy == "skip":
            log.debug("%s: operation %s skipped: %s", self._connection.id, name, e.message)
        elif policy == "warn":
            self._warn(name, "operation " + name + " skipped: " + e.message)
        else:
            raise AgentFrontierError(e.code, name + ": " + e.message, e)

    def _warn(self, key: str, message: str) -> None:
        with self._lock:
            if key in self._warned:
                return
            self._warned.add(key)
            self._warnings.append(message)
        log.warning("%s: %s", self._connection.id, message)

    def _level_names(self) -> list[str]:
        return [lv.level for lv in self._def.hierarchy]

    def enrich(self, entity: str, values: dict, scope: Scope) -> Record:
        """Final record: source, the scope's levels (the scope wins), the canonical fields in the model's order, the
        rest, then ``properties``."""
        out: dict = {"source": self._connection.id}
        out.update(scope.as_map())
        e = self._model.entity(entity)
        order = [lv.level for lv in self._def.hierarchy]
        if e is not None:
            order.extend(e.fields.keys())
        for k in order:
            if k not in out and values.get(k) is not None:
                out[k] = values[k]
        for k, x in values.items():
            if k not in out and x is not None and k != "properties":
                out[k] = x
        p = values.get("properties")
        if isinstance(p, dict) and p:
            out["properties"] = p
        return Record(entity, out)
