"""Pipelines: one source, what to extract, where to put it.

The run walks the hierarchy, runs every active operation in the selected aspects at the scope it declares, and
streams records to the sinks:

1. the source record and source-scoped operations (principals, ...);
2. containers level by level, with the operations scoped to each level;
3. each leaf scope (schema; the root for sources without levels), ``parallelism`` at a time, one session each:
   datasets page by page, per-batch operations (fields, keys, indexes, statistics) and leaf-scoped operations
   (lineage, routines, grants).

A failing leaf scope is recorded and the run goes on; the run is then PARTIAL.

With ``incremental`` (spec docs/incremental.md) the run emits only changes, deletions and tombstones and keeps a
checkpoint between runs; with ``runHealth`` (docs/observability.md) it ends with ``quality_result`` records about
itself; with ``lineage.parse`` (docs/lineage.md, Python only) it adds column lineage parsed from view SQL.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

from . import json_util as J
from . import mapping as M
from .engine import EngineSession
from .envelope import EnvelopeBuilder
from .errors import AgentFrontierError, root_message
from .governance.classify import FieldClassifier
from .governance.enforce import Governance
from .incremental import IncrementalRun, scope_key, store_from_config, tombstone_record
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


def _governance_paths(g: Any, base: Path | None) -> dict:
    """The governance block with its file references made absolute: ``policy`` (a rules file) or ``policy.rules``,
    ``classify.rules`` and ``authority``."""
    if not isinstance(g, dict):
        return {}
    g = J.deep_copy(g)
    pol = g.get("policy")
    if isinstance(pol, str):
        g["policy"] = {"rules": str(_resolve(base, pol))}
    elif isinstance(pol, dict) and isinstance(pol.get("rules"), str):
        pol["rules"] = str(_resolve(base, pol["rules"]))
    cls = g.get("classify")
    if isinstance(cls, dict) and cls.get("rules") is not None:
        cls["rules"] = [str(_resolve(base, r)) if isinstance(r, str) else r
                        for r in (cls["rules"] if isinstance(cls["rules"], list) else [cls["rules"]])]
    if isinstance(g.get("authority"), str):
        g["authority"] = str(_resolve(base, g["authority"]))
    return g


def _incremental_paths(inc: Any, base: Path | None) -> dict:
    if not isinstance(inc, dict):
        return {}
    inc = J.deep_copy(inc)
    c = inc.get("checkpoints")
    if isinstance(c, str):
        inc["checkpoints"] = str(_resolve(base, c))
    elif isinstance(c, dict) and isinstance(c.get("path"), str):
        c["path"] = str(_resolve(base, c["path"]))
    return inc


@dataclass
class PipelineConfig:
    """``connection``: the source; ``aspects``: what to extract (default metadata); ``operations``: explicit
    operation names, overriding ``aspects`` when not empty; ``scope``: options added to the connection's (filters);
    ``sinks``: sink configurations; ``page_size``: datasets per page; ``batch_size``: datasets per batch;
    ``parallelism``: leaf scopes processed at once, one session each; ``sample_rows``: rows per dataset for aspect
    ``data``; ``envelope``: wrap every record in a raw-stream event (spec docs/envelope.md); ``tenant_id``: the
    events' tenant (``default`` when not set); ``governance``: who the run acts for, the policy, classification and
    auditing (see :func:`governance_from_config`); ``incremental``: ``{"checkpoints": folder, "full": bool,
    "reconcileEvery": n}`` (docs/incremental.md, needs the envelope); ``run_health``: end with ``quality_result``
    records about the run; ``lineage``: ``{"parse": true}`` adds column lineage parsed from view SQL (the
    ``lineage`` extra); ``tracing``: OpenTelemetry spans (the ``otel`` extra)."""

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
    envelope: bool = False
    tenant_id: str | None = None
    governance: dict = field(default_factory=dict)
    incremental: dict = field(default_factory=dict)
    run_health: bool = False
    lineage: dict = field(default_factory=dict)
    tracing: bool = False

    def __post_init__(self) -> None:
        self.aspects = list(self.aspects) if self.aspects else ["metadata"]
        self.operations = list(self.operations or [])
        self.scope = dict(self.scope or {})
        self.sinks = list(self.sinks or [])
        self.governance = dict(self.governance or {})
        self.incremental = dict(self.incremental or {})
        self.lineage = dict(self.lineage or {})
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
                              self.page_size, self.batch_size, self.parallelism, self.sample_rows, self.name,
                              self.envelope, self.tenant_id, self.governance, self.incremental, self.run_health,
                              self.lineage, self.tracing)

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
        """Relative paths (the source file, sink paths, the checkpoint folder) resolve against ``base_dir``."""
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
                              J.integer(settings, "sampleRows", 0), J.text(n, "name"), J.boolean(n, "envelope", False),
                              J.text(n, "tenantId"), _governance_paths(n.get("governance"), base),
                              _incremental_paths(n.get("incremental"), base), J.boolean(n, "runHealth", False),
                              J.to_map(n.get("lineage")), J.boolean(n, "tracing", False))


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
    audit: dict | None = None
    policy: dict | None = None
    incremental: dict | None = None

    def count(self, entity: str) -> int:
        return self.counts.get(entity, 0)

    def to_map(self) -> dict:
        out = {"runId": self.run_id, "pipeline": self.pipeline, "source": self.source,
               "definition": self.definition, "version": self.version, "variant": self.variant,
               "status": self.status, "counts": {k: self.counts[k] for k in sorted(self.counts)},
               "warnings": list(self.warnings), "errors": list(self.errors), "elapsedMs": self.elapsed_ms,
               "sourceLoad": {"statements": self.load.statements, "rows": self.load.rows,
                              "elapsedMs": self.load.elapsed_ms, "connections": self.load.connections,
                              "retries": self.load.retries},
               "startedAt": self.started_at, "finishedAt": self.finished_at}
        if self.policy is not None:
            out["policy"] = dict(self.policy)
        if self.audit is not None:
            out["audit"] = dict(self.audit)
        if self.incremental is not None:
            out["incremental"] = dict(self.incremental)
        return out

    to_dict = to_map


class _SkipRun(Exception):
    """Internal: nothing to run because the sinks could not be created."""


@dataclass(frozen=True)
class _Origin:
    """Where emitted records come from, for incremental runs: the producer, its scope and the operation."""

    producer: str | None = None
    scope: str = ""
    op_name: str | None = None
    change: str = "upsert"


_NO_ORIGIN = _Origin()


class _Tracer:
    """OpenTelemetry spans when ``tracing`` is on and ``opentelemetry-api`` is installed; otherwise nothing."""

    def __init__(self, enabled: bool, warn: Callable[[str], None]):
        self._tracer = None
        if not enabled:
            return
        try:
            from opentelemetry import trace
            self._tracer = trace.get_tracer("agentfrontier")
        except ImportError:
            warn("tracing is on but opentelemetry is not installed (pip install \"agentfrontier[otel]\")")

    @contextlib.contextmanager
    def span(self, name: str, attributes: dict) -> Iterator[Any]:
        if self._tracer is None:
            yield None
            return
        from opentelemetry.trace import Status, StatusCode
        with self._tracer.start_as_current_span(name, attributes={k: v for k, v in attributes.items()
                                                                  if v is not None}) as span:
            try:
                yield span
            except BaseException as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise


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
        self._envelope: EnvelopeBuilder | None = None
        self._gov: Governance | None = None
        self._started = False
        self._levels: list[str] = []
        self._inc: IncrementalRun | None = None
        self._tracer: _Tracer | None = None
        self._dialect: str | None = None
        self._schema_fields: dict[tuple, dict[str, str]] = {}

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
        store = checkpoint_key = None
        self._tracer = _Tracer(self.config.tracing, self._add_warning)
        try:
            for sink_config in self.config.sinks:
                self._sinks.append(self.sdk.sink(sink_config))
        except Exception as e:  # noqa: BLE001 - reported, like any other failure
            self._add_error("sink configuration: " + (e.message if isinstance(e, AgentFrontierError) else root_message(e)))
        if self.config.incremental and not self.config.envelope:
            self._add_error("incremental runs need the raw-stream envelope (\"envelope\": true)")
        try:
            if self._errors:
                raise _SkipRun()
            self._gov = Governance.from_config(self.config.governance, run_id=run_id, source=connection.id,
                                               aspect_of=self.sdk.entity_model.aspect_of,
                                               resolve=self.sdk.secrets.resolve_deep, audit_sink=self._write)
            with self._tracer.span("agentfrontier.run", {"agentfrontier.pipeline": self.config.name,
                                                         "agentfrontier.source": connection.id}), \
                    self.sdk.open(connection) as main:
                definition = main.definition.id
                version = main.info.version
                variant = main.info.variant
                self._dialect = main.definition.dialect
                ctx = RunContext(run_id, self.config.name, connection.id, definition, main.info,
                                 [lv.level for lv in main.hierarchy], list(self.config.aspects), instant_str(started))
                if self.config.envelope:
                    self._envelope = EnvelopeBuilder(self.sdk.entity_model, self.sdk.streams, self.config.tenant_id,
                                                     connection.id if connection.id is not None else definition,
                                                     definition, run_id, ctx.levels)
                self._levels = list(ctx.levels)
                if self.config.incremental:
                    store = store_from_config(self.config.incremental.get("checkpoints"))
                    checkpoint_key = "/".join([self._envelope.tenant_id if self._envelope else "default",
                                               J.jstr(connection.id), J.jstr(self.config.name)])
                    self._inc = IncrementalRun(store.load(checkpoint_key),
                                               J.boolean(self.config.incremental, "full", False),
                                               J.integer(self.config.incremental, "reconcileEvery", 0),
                                               self.sdk.entity_model.event_entities, self._levels)
                for sink in self._sinks:
                    sink.begin(ctx)
                self._started = True
                self._gov.audit_decision("run.start", self._gov.resource("source", connection.id, entity="source"),
                                         None, "success")
                self._event("started", {"runId": run_id, "definition": definition, "version": J.jstr(version)})
                metadata = self._wants("metadata")
                if metadata:
                    self._emit([main.source()], origin=_Origin("source", ""))
                    self._cover("source", Scope.root())
                for name, op in self._selected(main, "source"):
                    self._run_op(main, name, op, Scope.root(), None, {"source"})
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
                if self._inc is not None:
                    self._tombstones()
        except _SkipRun:
            pass
        except AgentFrontierError as e:
            self._add_error(e.message)
            log.warning("pipeline %s failed: %s", self.config.name, e.message)
        except Exception as e:  # noqa: BLE001 - a run always ends with a report
            self._add_error(root_message(e))
            log.warning("pipeline %s failed", self.config.name, exc_info=True)
        status = "SUCCEEDED" if not self._errors else ("PARTIAL" if self._leaves_ok > 0 else "FAILED")
        finished = _dt.datetime.now(_dt.timezone.utc)
        if self.config.run_health and self._started:
            try:
                self._health(status, finished, time.time() - t0, connection.id)
            except Exception as e:  # noqa: BLE001
                log.warning("run health records failed: %s", e)
        audit = policy = None
        gov = self._gov
        if gov is not None and not gov.trivial:
            policy = gov.summary()
            if gov.audit is not None and (gov.audit_all or gov.audit.events):
                if self._started:
                    gov.audit_decision("run.finish", gov.resource("source", connection.id, entity="source"), None,
                                       status.lower(), sum(self._counts.values()))
                audit = gov.audit.manifest()
        inc_summary = None
        if self._inc is not None:
            inc_summary = dict(self._inc.changes, reconcile=self._inc.reconcile, checkpoint="pending")
        report = RunReport(run_id, self.config.name, connection.id, definition, version, variant, status,
                           dict(self._counts), list(self._warnings), list(self._errors),
                           int(round((time.time() - t0) * 1000)), self._load, instant_str(started),
                           instant_str(finished), audit, policy, inc_summary)
        sinks_ok = True
        for sink in self._sinks:
            try:
                sink.end(report)
            except Exception as e:  # noqa: BLE001
                sinks_ok = False
                log.warning("sink end failed: %s", e)
        for sink in self._sinks:
            try:
                sink.close()
            except Exception as e:  # noqa: BLE001
                log.debug("sink close failed: %s", e)
        if self._inc is not None and store is not None and checkpoint_key is not None:
            inc_summary["checkpoint"] = "not committed"  # type: ignore[index]
            if status != "FAILED" and sinks_ok:
                try:
                    store.save(checkpoint_key, self._inc.checkpoint(run_id))
                    inc_summary["checkpoint"] = "committed"  # type: ignore[index]
                except Exception as e:  # noqa: BLE001
                    log.warning("cannot save checkpoint %s: %s", checkpoint_key, e)
                    inc_summary["checkpoint"] = "failed: " + root_message(e)  # type: ignore[index]
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
            self._emit(containers, origin=_Origin("containers:" + level.level, scope_key(scope.as_map())))
            self._cover("containers:" + level.level, scope)
        leaf_level = depth == len(levels) - 1
        for c in containers:
            child = scope.with_(level.level, c.string("name"))
            self._event("container", {"level": level.level, "name": c.string("name")})
            if not leaf_level:
                for name, op in self._selected(s, level.level):
                    self._run_op(s, name, op, child, None, {level.level})
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
        skey = scope_key(scope.as_map())
        try:
            with self._tracer.span("agentfrontier.scope", {"agentfrontier.scope": str(scope)}):  # type: ignore[union-attr]
                levels = s.hierarchy
                leaf_level = levels[-1].level if levels else None
                batch_ops = self._selected(s, "batch")
                metadata = self._wants("metadata")
                data = self._wants("data")
                gov = self._gov
                classifier = FieldClassifier(gov.classifier) if gov is not None and gov.classifier is not None \
                    else None
                parse = self._parse_lineage()
                views: list[Record] = []
                failed: set[str] = set()
                cursor = None
                while True:
                    page = s.datasets(scope, cursor, self.config.page_size)
                    if metadata:
                        self._emit(page.records, origin=_Origin("datasets", skey))
                    if parse:
                        views.extend(r for r in page.records if r.get("definition") is not None
                                     and J.jstr(r.get("type")) in ("view", "materialized_view"))
                    names = [r.string("name") for r in page.records]
                    for i in range(0, len(names), self.config.batch_size):
                        batch = names[i:i + self.config.batch_size]
                        for name, op in batch_ops:
                            if not self._run_op(s, name, op, scope, batch, {"batch"}, classifier):
                                failed.add(name)
                    if data and s.definition.operation("sample") is not None:
                        for n in names:
                            self._sample(s, scope, n, classifier)
                    if classifier is not None:
                        self._emit_labels(classifier.flush(), skey)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                if leaf_level is not None:
                    for name, op in self._selected(s, leaf_level):
                        self._run_op(s, name, op, scope, None, {leaf_level})
                if parse and views:
                    self._emit_parsed_lineage(views, scope, skey)
                if self._inc is not None:
                    self._cover("datasets", scope)
                    for name, op in batch_ops:
                        if name not in failed and (op.get("incremental") is None or self._inc.reconcile):
                            self._cover("op:" + name, scope)
                    self._cover("classify", scope)
                    self._cover("lineage:parsed", scope)
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

    # ------------------------------------------------------------------------------------------------ operations

    def _run_op(self, s: EngineSession, name: str, op: dict, scope: Scope, datasets: list[str] | None,
                kinds: set[str], classifier: FieldClassifier | None = None) -> bool:
        """Runs one operation behind the policy enforcement point and emits its records. Returns whether it
        completed: ran without an error (also one its ``onError`` turned into a warning) and was not denied."""
        inc = self._inc
        inc_spec = op.get("incremental") if inc is not None and isinstance(op.get("incremental"), dict) else None
        skey = scope_key(scope.as_map())
        params: dict = {}
        if inc_spec is not None:
            since = inc.since(name, skey)  # type: ignore[union-attr]
            if since is not None:
                params["since"] = since
        gov = self._gov
        governed = gov is not None and not gov.trivial
        res: dict = {}
        d = None
        if governed:
            entity = J.text(op, "entity", "record") or "record"
            res = gov.resource("operation", s.connection.id, scope.as_map(), operation=name, entity=entity,
                               aspect=J.text(op, "aspect", self.sdk.entity_model.aspect_of(entity)))
            d = gov.decide("read", res)
            if not d.allowed:
                self._add_warning("policy denied operation " + name + ("" if scope.is_root else " at " + str(scope))
                                  + ": " + J.jstr(d.reason))
                gov.audit_decision("read", res, d, "denied", 0)
                self._finish_position(s, name, inc_spec, skey, False)
                return False
        before = s.failures
        with self._tracer.span("agentfrontier.operation", {"agentfrontier.operation": name,  # type: ignore[union-attr]
                                                           "agentfrontier.scope": str(scope)}) as span:
            try:
                records = s.run_operation(name, op, scope, datasets, kinds, params)
            except Exception:
                if governed:
                    gov.audit_decision("read", res, d, "failure")
                raise
            if span is not None:
                span.set_attribute("agentfrontier.records", len(records))
        completed = s.failures == before
        if governed:
            gov.audit_decision("read", res, d, "success", len(records))
        deleted_field = J.text(inc_spec, "deleted") if inc_spec is not None else None
        removed: list[Record] = []
        if deleted_field:
            kept = []
            for r in records:
                flag = r.values.pop(deleted_field, None)
                props = r.values.get("properties")
                if flag is None and isinstance(props, dict):
                    flag = props.pop(deleted_field, None)
                    if not props:
                        r.values.pop("properties", None)
                (removed if flag is not None and flag is not False else kept).append(r)
            records = kept
        self._emit(records, op, classifier, origin=_Origin("op:" + name, skey, name))
        if removed:
            self._emit(removed, op, origin=_Origin("op:" + name, skey, name, "delete"))
        self._finish_position(s, name, inc_spec, skey, completed)
        if inc is not None and completed and datasets is None and (inc_spec is None or inc.reconcile):
            inc.cover("op:" + name, skey)
        return completed

    def _finish_position(self, s: EngineSession, name: str, inc_spec: dict | None, skey: str, completed: bool) -> None:
        if inc_spec is None or self._inc is None:
            return
        key = name + "@" + skey
        if J.text(inc_spec, "mode") == "watermark":
            self._inc.finish_watermark(key, completed)
        elif J.text(inc_spec, "mode") == "delta_token":
            token = M.json_path_first(s.last_body(name), J.text(inc_spec, "token", "$.deltaLink"))
            self._inc.finish_token(key, token, completed)

    def _cover(self, producer: str, scope: Scope) -> None:
        if self._inc is not None:
            self._inc.cover(producer, scope_key(scope.as_map()))

    def _sample(self, s: EngineSession, scope: Scope, n: str, classifier: FieldClassifier | None) -> None:
        """Samples one dataset behind the policy enforcement point. Its values are classified before the rows are
        released, so masking sees the labels they produce."""
        gov = self._gov
        governed = gov is not None and not gov.trivial
        res: dict = {}
        d = None
        if governed:
            res = gov.resource("dataset", s.connection.id, scope.as_map(), dataset=n, entity="row", aspect="data",
                               operation="sample")
            if classifier is not None:
                res["labels"] = sorted(set(res.get("labels") or []) | set(classifier.pending_labels(n)))
            d = gov.decide("sample", res)
            if not d.allowed:
                self._add_warning("policy denied sample of " + J.jstr(n) + ": " + J.jstr(d.reason))
                gov.audit_decision("sample", res, d, "denied", 0)
                return
        try:
            rows = s.sample(scope, n, self.config.sample_rows)
        except AgentFrontierError as e:
            self._add_warning("sample of " + J.jstr(n) + ": " + e.message)
            if governed:
                gov.audit_decision("sample", res, d, "failure", reason=e.message)
            return
        base: dict = {"source": s.connection.id}
        base.update(scope.as_map())
        if classifier is not None:
            classifier.observe_rows(base, n, [r.values for r in rows])
            self._emit_labels(classifier.flush(n), scope_key(scope.as_map()))
        tagged = []
        for r in rows:
            v = dict(base)
            v["dataset"] = n
            v["values"] = r.values
            tagged.append(Record("row", v))
        if governed:
            gov.audit_decision("sample", res, d, "success", len(tagged))
        self._emit(tagged, action="sample")

    def _emit_labels(self, labels: list[Record], skey: str = "") -> None:
        """Classifier output: each label is an inferred assertion with its own confidence."""
        for r in labels:
            self._emit([r], {"assertion": "inferred", "confidence": r.get("confidence")},
                       origin=_Origin("classify", skey))

    # ------------------------------------------------------------------------------------------------ parsed lineage

    def _parse_lineage(self) -> bool:
        return J.boolean(self.config.lineage, "parse", False) and "lineage" in self._wanted_aspects()

    def _wanted_aspects(self) -> list[str]:
        return list(self.config.aspects) if not self.config.operations else ["metadata", "lineage"]

    def _emit_parsed_lineage(self, views: list[Record], scope: Scope, skey: str) -> None:
        from .lineage_parse import LineageParser, ParseUnavailable
        try:
            with self._lock:
                schema = {k: dict(v) for k, v in self._schema_fields.items()}
            parser = LineageParser(self._dialect, self._levels, schema)
        except ParseUnavailable as e:
            self._add_warning(str(e))
            return
        for v in views:
            try:
                parsed = parser.view_lineage(v)
            except Exception as e:  # noqa: BLE001 - SQL that does not parse is skipped
                self._add_warning("cannot parse the definition of " + J.jstr(v.get("name")) + ": " + root_message(e))
                continue
            for r, confidence in parsed:
                self._emit([r], {"assertion": "parsed", "confidence": confidence},
                           origin=_Origin("lineage:parsed", skey))

    # ------------------------------------------------------------------------------------------------ incremental

    def _tombstones(self) -> None:
        inc = self._inc
        assert inc is not None
        gov = self._gov
        for object_id, ident in inc.tombstones():
            rec = tombstone_record(ident)
            if gov is not None and not gov.trivial:
                released = gov.release([rec], "read", self._levels)
                if not released:
                    continue
                rec = released[0]
            self._write([rec], None, _Origin(change="tombstone"), object_id)

    # ------------------------------------------------------------------------------------------------ health

    def _health(self, status: str, finished: _dt.datetime, elapsed_s: float, source: str) -> None:
        """quality_result records about the run itself (spec docs/observability.md)."""
        measured = instant_str(finished)
        with self._lock:
            records_total = sum(self._counts.values())
            errors = list(self._errors)
            warnings = list(self._warnings)
            retries = self._load.retries

        def check(name: str, value: float, st: str, severity: str, message: str | None = None,
                  field_: str | None = None) -> Record:
            v = {"source": source, "check": name, "objectType": "source", "field": field_, "status": st,
                 "value": float(value), "severity": severity, "measuredAt": measured, "message": message}
            return Record("quality_result", {k: x for k, x in v.items() if x is not None})

        out = [
            check("run.status", {"SUCCEEDED": 1.0, "PARTIAL": 0.5}.get(status, 0.0),
                  {"SUCCEEDED": "pass", "PARTIAL": "warn"}.get(status, "fail"),
                  {"SUCCEEDED": "info", "PARTIAL": "medium"}.get(status, "high")),
            check("run.records", records_total, "pass", "info"),
            check("run.duration", round(elapsed_s, 3), "pass", "info"),
            check("run.errors", len(errors), "pass" if not errors else "fail", "info" if not errors else "high",
                  errors[0] if errors else None),
            check("run.warnings", len(warnings), "pass" if not warnings else "warn",
                  "info" if not warnings else "low", warnings[0] if warnings else None),
            check("run.retries", retries, "pass" if not retries else "warn", "info" if not retries else "low"),
        ]
        if self._inc is not None:
            for key, w in sorted(self._inc.watermarks.items()):
                value = w.get("value") if isinstance(w, dict) else None
                if value is None or J.is_number(value):
                    continue
                try:
                    from .template import iso_to_epoch_ms
                    lag = (finished.timestamp() * 1000 - iso_to_epoch_ms(J.jstr(value))) / 1000.0
                except AgentFrontierError:
                    continue
                out.append(check("freshness.lag", round(max(0.0, lag), 3), "pass", "info", None, key.split("@")[0]))
        self._write(out)

    # ------------------------------------------------------------------------------------------------ emission

    def _emit(self, records: list[Record], operation: dict | None = None,
              classifier: FieldClassifier | None = None, action: str = "read", origin: _Origin = _NO_ORIGIN) -> None:
        """Releases records through the policy enforcement point (obligations applied, denied ones withheld) and
        hands them to the sinks. ``operation`` is the definition operation that produced them (for the envelope's
        ``assertion`` and ``confidence``)."""
        if not records:
            return
        if self._parse_lineage():
            with self._lock:
                for r in records:
                    if r.entity == "field" and r.get("dataset") is not None and r.get("name") is not None:
                        key = tuple(r.get(lv) for lv in self._levels) + (r.get("dataset"),)
                        self._schema_fields.setdefault(key, {})[J.jstr(r.get("name"))] = J.jstr(r.get("dataType"))
        gov = self._gov
        if gov is not None and not gov.trivial:
            if classifier is not None:
                for r in records:
                    if r.entity == "field":
                        classifier.observe_field(r)
            gov.observe(records)
            records = gov.release(records, action, self._levels)
        self._write(records, operation, origin)

    def _write(self, records: list[Record], operation: dict | None = None, origin: _Origin = _NO_ORIGIN,
               object_id: str | None = None) -> None:
        """Hands records to the sinks, wrapped in raw-stream events when the envelope is on; in incremental runs
        only what changed."""
        if not records:
            return
        if self._envelope is not None:
            observed_at = instant_str(_dt.datetime.now(_dt.timezone.utc))
            records = [self._envelope.wrap(r, observed_at, operation, origin.change, object_id) for r in records]
            inc = self._inc
            if inc is not None and origin.change != "tombstone":
                records = [e for e in records if self._changed(inc, e, operation, origin)]
        if not records:
            return
        with self._lock:
            for r in records:
                self._counts[r.entity] = self._counts.get(r.entity, 0) + 1
        for sink in self._sinks:
            sink.write(records)

    def _changed(self, inc: IncrementalRun, e: Any, operation: dict | None, origin: _Origin) -> bool:
        spec = operation.get("incremental") if isinstance(operation, dict) else None
        if isinstance(spec, dict) and J.text(spec, "mode") == "watermark" and origin.op_name is not None:
            key = origin.op_name + "@" + origin.scope
            event_id = J.jstr(e.envelope.get("eventId"))
            inc.watermark_observed(key, e.record.values.get(J.jstr(spec.get("field"))), event_id)
            if e.entity in inc.event_entities and inc.in_boundary(key, event_id):
                return False
        return inc.accept(e, origin.producer, origin.scope, origin.change)

    def _event(self, type_: str, data: dict) -> None:
        if self.listener is None:
            return
        try:
            self.listener(type_, data)
        except Exception as e:  # noqa: BLE001
            log.debug("listener failed: %s", e)
