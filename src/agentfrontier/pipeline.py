"""Pipelines: one source, what to extract, where to put it.

The run walks the hierarchy, runs every active operation in the selected aspects at the scope it declares, and
streams records to the sinks:

1. the source record and source-scoped operations (principals, ...);
2. containers level by level, with the operations scoped to each level;
3. each leaf scope (schema; the root for sources without levels), ``parallelism`` at a time, one session each:
   datasets page by page, per-batch operations (fields, keys, indexes, statistics) and leaf-scoped operations
   (lineage, routines, grants).

A failing leaf scope is recorded and the run goes on; the run is then PARTIAL.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from . import json_util as J
from .engine import EngineSession
from .errors import AgentFrontierError, root_message
from .model import ConnectionConfig, Record, Scope, Snapshot
from .sinks import RunContext, Sink

if TYPE_CHECKING:  # pragma: no cover
    from .sdk import AgentFrontier

log = logging.getLogger("agentfrontier.pipeline")

RunListener = Callable[[str, dict], None]


def instant_str(t: _dt.datetime) -> str:
    """Java ``Instant.toString``: UTC, fraction in groups of three digits, omitted when zero."""
    t = t.astimezone(_dt.timezone.utc)
    base = t.strftime("%Y-%m-%dT%H:%M:%S")
    us = t.microsecond
    if us:
        base += ".%03d" % (us // 1000) if us % 1000 == 0 else ".%06d" % us
    return base + "Z"


def _resolve(base: Path | None, p: str) -> Path:
    path = Path(p)
    if path.is_absolute() or base is None:
        return path
    return Path(_normpath(base / path))


def _normpath(p: Path) -> str:
    import os
    return os.path.normpath(str(p))


@dataclass
class PipelineConfig:
    """``connection``: the source; ``aspects``: what to extract (default metadata); ``operations``: explicit
    operation names, overriding ``aspects`` when not empty; ``scope``: options added to the connection's (filters);
    ``sinks``: sink configurations; ``page_size``: datasets per page; ``batch_size``: datasets per batch;
    ``parallelism``: leaf scopes processed at once, one session each; ``sample_rows``: rows per dataset for aspect
    ``data``."""

    connection: ConnectionConfig
    aspects: list[str] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    sinks: list[dict] = field(default_factory=list)
    page_size: int = 0
    batch_size: int = 0
    parallelism: int = 0
    sample_rows: int = 0
    name: str | None = None

    def __post_init__(self) -> None:
        self.aspects = list(self.aspects) if self.aspects else ["metadata"]
        self.operations = list(self.operations or [])
        self.scope = dict(self.scope or {})
        self.sinks = list(self.sinks or [])
        self.page_size = 500 if not self.page_size or self.page_size <= 0 else self.page_size
        self.batch_size = 200 if not self.batch_size or self.batch_size <= 0 else self.batch_size
        self.parallelism = 1 if not self.parallelism or self.parallelism <= 0 else self.parallelism
        self.sample_rows = 10 if not self.sample_rows or self.sample_rows <= 0 else self.sample_rows
        if self.name is None or J.is_blank(self.name):
            self.name = self.connection.id

    @staticmethod
    def of(connection: ConnectionConfig, aspects: Iterable[str] | None = None,
           sinks: Iterable[dict] | None = None) -> "PipelineConfig":
        return PipelineConfig(connection, list(aspects or []), sinks=list(sinks or []))

    def with_sinks(self, new_sinks: Iterable[dict]) -> "PipelineConfig":
        return PipelineConfig(self.connection, self.aspects, self.operations, self.scope, list(new_sinks),
                              self.page_size, self.batch_size, self.parallelism, self.sample_rows, self.name)

    @staticmethod
    def from_file(path: str | Path) -> "PipelineConfig":
        p = Path(path)
        try:
            doc = J.loads_first(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise AgentFrontierError.invalid_config("cannot read pipeline " + str(path) + ": " + str(e)) from e
        return PipelineConfig.from_json(doc, p.absolute().parent)

    @staticmethod
    def from_json(n: Any, base_dir: str | Path | None = None) -> "PipelineConfig":
        """Relative paths (the source file, sink paths) resolve against ``base_dir``."""
        if isinstance(n, str):
            n = J.parse(n)
        if not isinstance(n, dict):
            raise AgentFrontierError.invalid_config("a pipeline must be a JSON object")
        base = None if base_dir is None else Path(base_dir)
        src = n.get("source") if "source" in n else n.get("connection")
        if src is None:
            raise AgentFrontierError.invalid_config("pipeline needs a 'source' (connection object or file)")
        connection = ConnectionConfig.from_file(_resolve(base, src)) if isinstance(src, str) \
            else ConnectionConfig.from_json(src)
        sinks = []
        for s in n.get("sinks") or []:
            c = J.deep_copy(s)
            if isinstance(c, dict) and "path" in c and base is not None:
                c["path"] = str(_resolve(base, J.as_text(c["path"])))
            sinks.append(c)
        settings = n.get("settings") if isinstance(n.get("settings"), dict) else {}
        return PipelineConfig(connection, J.strings(n.get("aspects")), J.strings(n.get("operations")),
                              J.to_map(n.get("scope")), sinks, J.integer(settings, "pageSize", 0),
                              J.integer(settings, "batchSize", 0), J.integer(settings, "parallelism", 0),
                              J.integer(settings, "sampleRows", 0), J.text(n, "name"))


@dataclass(frozen=True)
class RunReport:
    """Outcome of a pipeline run. ``status`` is SUCCEEDED, PARTIAL (some scopes failed) or FAILED."""

    run_id: str
    pipeline: str
    source: str
    definition: str | None
    version: str | None
    variant: str | None
    status: str
    counts: dict
    warnings: list
    errors: list
    elapsed_ms: int
    load: Snapshot
    started_at: str
    finished_at: str

    def count(self, entity: str) -> int:
        return self.counts.get(entity, 0)

    def to_map(self) -> dict:
        return {"runId": self.run_id, "pipeline": self.pipeline, "source": self.source,
                "definition": self.definition, "version": self.version, "variant": self.variant,
                "status": self.status, "counts": {k: self.counts[k] for k in sorted(self.counts)},
                "warnings": list(self.warnings), "errors": list(self.errors), "elapsedMs": self.elapsed_ms,
                "sourceLoad": {"statements": self.load.statements, "rows": self.load.rows,
                               "elapsedMs": self.load.elapsed_ms, "connections": self.load.connections},
                "startedAt": self.started_at, "finishedAt": self.finished_at}

    to_dict = to_map


class _SkipRun(Exception):
    """Internal: nothing to run because the sinks could not be created."""


class Pipeline:
    def __init__(self, sdk: "AgentFrontier", config: PipelineConfig, listener: RunListener | None = None):
        self.sdk = sdk
        self.config = config
        self.listener = listener
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._errors: list[str] = []
        self._warnings: list[str] = []  # insertion-ordered set
        self._sinks: list[Sink] = []
        self._leaves_ok = 0
        self._load = Snapshot()

    def _add_warning(self, w: str) -> None:
        with self._lock:
            if w not in self._warnings:
                self._warnings.append(w)

    def _add_error(self, e: str) -> None:
        with self._lock:
            self._errors.append(e)

    def run(self) -> RunReport:
        started = _dt.datetime.now(_dt.timezone.utc)
        t0 = time.time()
        now = _dt.datetime.now()
        run_id = self.config.name + "-" + now.strftime("%Y%m%d-%H%M%S-") + "%03d" % (now.microsecond // 1000)
        connection = self.config.connection if not self.config.scope \
            else self.config.connection.with_options(self.config.scope)
        definition = version = variant = None
        try:
            for sink_config in self.config.sinks:
                self._sinks.append(self.sdk.sink(sink_config))
        except Exception as e:  # noqa: BLE001 - reported, like any other failure
            self._add_error("sink configuration: " + (e.message if isinstance(e, AgentFrontierError) else root_message(e)))
        try:
            if self._errors:
                raise _SkipRun()
            with self.sdk.open(connection) as main:
                definition = main.definition.id
                version = main.info.version
                variant = main.info.variant
                ctx = RunContext(run_id, self.config.name, connection.id, definition, main.info,
                                 [lv.level for lv in main.hierarchy], list(self.config.aspects), instant_str(started))
                for sink in self._sinks:
                    sink.begin(ctx)
                self._event("started", {"runId": run_id, "definition": definition, "version": J.jstr(version)})
                metadata = self._wants("metadata")
                if metadata:
                    self._emit([main.source()])
                for name, op in self._selected(main, "source"):
                    self._emit(main.run_operation(name, op, Scope.root(), None, {"source"}, {}))
                leaves: list[Scope] = []
                self._walk(main, Scope.root(), 0, leaves, metadata)
                if len(leaves) == 1 or self.config.parallelism == 1:
                    for leaf in leaves:
                        self._leaf(main, leaf)
                else:
                    self._parallel(connection, leaves)
                for w in main.warnings:
                    self._add_warning(w)
                with self._lock:
                    self._load = self._load.plus(main.stats.snapshot())
        except _SkipRun:
            pass
        except AgentFrontierError as e:
            self._add_error(e.message)
            log.warning("pipeline %s failed: %s", self.config.name, e.message)
        except Exception as e:  # noqa: BLE001 - a run always ends with a report
            self._add_error(root_message(e))
            log.warning("pipeline %s failed", self.config.name, exc_info=True)
        finished = _dt.datetime.now(_dt.timezone.utc)
        status = "SUCCEEDED" if not self._errors else ("PARTIAL" if self._leaves_ok > 0 else "FAILED")
        report = RunReport(run_id, self.config.name, connection.id, definition, version, variant, status,
                           dict(self._counts), list(self._warnings), list(self._errors),
                           int(round((time.time() - t0) * 1000)), self._load, instant_str(started),
                           instant_str(finished))
        for sink in self._sinks:
            try:
                sink.end(report)
            except Exception as e:  # noqa: BLE001
                log.warning("sink end failed: %s", e)
        for sink in self._sinks:
            try:
                sink.close()
            except Exception as e:  # noqa: BLE001
                log.debug("sink close failed: %s", e)
        self._event("finished", report.to_map())
        return report

    def _walk(self, s: EngineSession, scope: Scope, depth: int, leaves: list[Scope], metadata: bool) -> None:
        levels = s.hierarchy
        if depth == len(levels):
            leaves.append(scope)
            return
        level = levels[depth]
        containers = s.containers(scope)
        if metadata:
            self._emit(containers)
        leaf_level = depth == len(levels) - 1
        for c in containers:
            child = scope.with_(level.level, c.string("name"))
            self._event("container", {"level": level.level, "name": c.string("name")})
            if not leaf_level:
                for name, op in self._selected(s, level.level):
                    self._emit(s.run_operation(name, op, child, None, {level.level}, {}))
            self._walk(s, child, depth + 1, leaves, metadata)

    def _parallel(self, connection: ConnectionConfig, leaves: list[Scope]) -> None:
        local = threading.local()
        opened: list[EngineSession] = []
        opened_lock = threading.Lock()

        def work(leaf: Scope) -> None:
            w = getattr(local, "session", None)
            if w is None:
                try:
                    w = self.sdk.open(connection)
                except AgentFrontierError as e:
                    self._add_error(str(leaf) + ": " + e.message)
                    return
                local.session = w
                with opened_lock:
                    opened.append(w)
            self._leaf(w, leaf)

        try:
            with ThreadPoolExecutor(max_workers=self.config.parallelism, thread_name_prefix="agentfrontier-worker") \
                    as pool:
                futures = [pool.submit(work, leaf) for leaf in leaves]
                for f in futures:
                    f.result()
        except Exception as e:  # noqa: BLE001
            self._add_error("parallel run: " + root_message(e))
        finally:
            for w in opened:
                for x in w.warnings:
                    self._add_warning(x)
                with self._lock:
                    self._load = self._load.plus(w.stats.snapshot())
                w.close()

    def _leaf(self, s: EngineSession, scope: Scope) -> None:
        self._event("scope.started", {"scope": str(scope)})
        t0 = time.perf_counter_ns()
        try:
            levels = s.hierarchy
            leaf_level = levels[-1].level if levels else None
            batch_ops = self._selected(s, "batch")
            metadata = self._wants("metadata")
            data = self._wants("data")
            cursor = None
            while True:
                page = s.datasets(scope, cursor, self.config.page_size)
                if metadata:
                    self._emit(page.records)
                names = [r.string("name") for r in page.records]
                for i in range(0, len(names), self.config.batch_size):
                    batch = names[i:i + self.config.batch_size]
                    for name, op in batch_ops:
                        self._emit(s.run_operation(name, op, scope, batch, {"batch"}, {}))
                if data and s.definition.operation("sample") is not None:
                    for n in names:
                        try:
                            rows = s.sample(scope, n, self.config.sample_rows)
                            tagged = []
                            for r in rows:
                                v: dict = {"source": s.connection.id}
                                v.update(scope.as_map())
                                v["dataset"] = n
                                v["values"] = r.values
                                tagged.append(Record("row", v))
                            self._emit(tagged)
                        except AgentFrontierError as e:
                            self._add_warning("sample of " + J.jstr(n) + ": " + e.message)
                cursor = page.next_cursor
                if cursor is None:
                    break
            if leaf_level is not None:
                for name, op in self._selected(s, leaf_level):
                    self._emit(s.run_operation(name, op, scope, None, {leaf_level}, {}))
            with self._lock:
                self._leaves_ok += 1
            self._event("scope.finished", {"scope": str(scope), "elapsedMs": (time.perf_counter_ns() - t0) // 1_000_000})
        except Exception as e:  # noqa: BLE001 - one failing scope does not stop the run
            msg = str(scope) + ": " + (e.message if isinstance(e, AgentFrontierError) else root_message(e))
            self._add_error(msg)
            self._event("error", {"scope": str(scope), "message": msg})
            log.warning("pipeline %s: %s", self.config.name, msg)

    def _selected(self, s: EngineSession, scope_kind: str) -> list[tuple[str, dict]]:
        """Operations of the selected aspects (or names) whose scope is ``scope_kind``: ``source``, a level name,
        or ``batch`` for dataset / datasets scoped ones."""
        lists = {lv.list for lv in s.hierarchy if lv.list is not None}
        out = []
        for name, op in s.definition.operations.items():
            entity = J.text(op, "entity", "record")
            if name in lists or name in ("test", "source", "sample") or entity in ("dataset", "container"):
                continue
            aspect = J.text(op, "aspect", self.sdk.entity_model.aspect_of(entity))
            chosen = (aspect in self.config.aspects) if not self.config.operations \
                else (name in self.config.operations)
            if not chosen:
                continue
            op_scope = J.text(op, "scope", "source")
            kind = "batch" if op_scope in ("dataset", "datasets") else op_scope
            if kind == scope_kind:
                out.append((name, op))
        return out

    def _wants(self, aspect: str) -> bool:
        return (aspect in self.config.aspects) if not self.config.operations else aspect == "metadata"

    def _emit(self, records: list[Record]) -> None:
        if not records:
            return
        with self._lock:
            for r in records:
                self._counts[r.entity] = self._counts.get(r.entity, 0) + 1
        for sink in self._sinks:
            sink.write(records)

    def _event(self, type_: str, data: dict) -> None:
        if self.listener is None:
            return
        try:
            self.listener(type_, data)
        except Exception as e:  # noqa: BLE001
            log.debug("listener failed: %s", e)
