"""Where a pipeline's records go. Built in:

* ``jsonl``: one ``<entity>.jsonl`` file per entity plus ``run.json`` in ``path``;
* ``json``: one document ``{"run": ..., "records": {"dataset": [...], ...}}`` at ``path``;
* ``console``: JSON Lines on standard output;
* ``memory``: kept in the process, read back with ``MemorySink.get(name)``;
* ``http``: POSTs batches to ``url`` as ``{"event": "records", "run": ..., "records": [...]}``;
* ``neo4j``: writes a graph with the official ``neo4j`` driver (``pip install "agentfrontier[neo4j]"``).

Calls arrive in order ``begin``, any number of ``write``, ``end``, then ``close`` (also after a failure). ``write``
may be called from several threads; implementations synchronise.
"""

from __future__ import annotations

import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TextIO

from .. import json_util as J
from .. import template as T
from ..errors import SINK_FAILED, AgentFrontierError
from ..model import Record, SourceInfo

if TYPE_CHECKING:  # pragma: no cover
    from ..sdk import AgentFrontier


@dataclass(frozen=True)
class RunContext:
    """What a sink knows about the run it receives."""

    run_id: str
    pipeline: str
    source: str
    definition: str | None
    info: SourceInfo | None
    levels: list[str] = field(default_factory=list)
    aspects: list[str] = field(default_factory=list)
    started_at: str = ""


class Sink:
    def begin(self, context: RunContext) -> None:
        pass

    def write(self, records: list[Record]) -> None:
        raise NotImplementedError

    def end(self, report: Any) -> None:
        pass

    def close(self) -> None:
        pass


class SinkProvider:
    """Creates sinks of one ``type`` from their JSON configuration."""

    type: str = ""

    def create(self, config: dict, sdk: "AgentFrontier") -> Sink:
        raise NotImplementedError


class _Provider(SinkProvider):
    def __init__(self, type_: str, factory: Callable[[dict, Any], Sink]):
        self.type = type_
        self._factory = factory

    def create(self, config: dict, sdk: Any) -> Sink:
        return self._factory(config, sdk)


def require(c: dict, key: str) -> str:
    v = J.text(c, key)
    if v is None or J.is_blank(v):
        raise AgentFrontierError.invalid_config(J.jstr(J.text(c, "type")) + " sink needs '" + key + "'")
    return v


def run_json(ctx: RunContext) -> dict:
    return {"runId": ctx.run_id, "pipeline": ctx.pipeline, "source": ctx.source, "definition": ctx.definition,
            "version": None if ctx.info is None else ctx.info.version,
            "variant": None if ctx.info is None else ctx.info.variant,
            "levels": list(ctx.levels), "aspects": list(ctx.aspects), "startedAt": ctx.started_at}


# ------------------------------------------------------------------------------------------------------------- jsonl


class JsonlSink(Sink):
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self._writers: dict[str, TextIO] = {}
        self._lock = threading.Lock()
        self._ctx: RunContext | None = None

    def begin(self, context: RunContext) -> None:
        self._ctx = context
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for p in self.dir.iterdir():
                if p.name.endswith(".jsonl") and p.is_file():
                    p.unlink()
        except OSError as e:
            raise AgentFrontierError(SINK_FAILED, "cannot prepare " + str(self.dir) + ": " + str(e), e) from e

    def write(self, records: list[Record]) -> None:
        with self._lock:
            try:
                for r in records:
                    w = self._writers.get(r.entity)
                    if w is None:
                        w = open(self.dir / (r.entity + ".jsonl"), "a", encoding="utf-8", newline="\n")
                        self._writers[r.entity] = w
                    w.write(J.write(r.to_json()))
                    w.write("\n")
            except OSError as e:
                raise AgentFrontierError(SINK_FAILED, "cannot write to " + str(self.dir) + ": " + str(e), e) from e

    def end(self, report: Any) -> None:
        with self._lock:
            try:
                for w in self._writers.values():
                    w.flush()
                run = {} if self._ctx is None else run_json(self._ctx)
                run["report"] = report.to_map()
                self.dir.mkdir(parents=True, exist_ok=True)
                (self.dir / "run.json").write_text(J.pretty(run), encoding="utf-8")
            except OSError as e:
                raise AgentFrontierError(SINK_FAILED, "cannot write run.json: " + str(e), e) from e

    def close(self) -> None:
        with self._lock:
            for w in self._writers.values():
                try:
                    w.close()
                except OSError:
                    pass
            self._writers.clear()


# -------------------------------------------------------------------------------------------------------------- json


class JsonSink(Sink):
    def __init__(self, file: str | Path):
        self.file = Path(file)
        self._by_entity: dict[str, list] = {}
        self._lock = threading.Lock()
        self._ctx: RunContext | None = None

    def begin(self, context: RunContext) -> None:
        self._ctx = context

    def write(self, records: list[Record]) -> None:
        with self._lock:
            for r in records:
                self._by_entity.setdefault(r.entity, []).append(r.to_json())

    def end(self, report: Any) -> None:
        with self._lock:
            run = {} if self._ctx is None else run_json(self._ctx)
            run["report"] = report.to_map()
            doc = {"run": run, "records": dict(self._by_entity)}
            try:
                if self.file.parent:
                    self.file.parent.mkdir(parents=True, exist_ok=True)
                self.file.write_text(J.pretty(doc), encoding="utf-8")
            except OSError as e:
                raise AgentFrontierError(SINK_FAILED, "cannot write " + str(self.file) + ": " + str(e), e) from e


# ----------------------------------------------------------------------------------------------------------- console


class ConsoleSink(Sink):
    def __init__(self, out: TextIO | None = None):
        self._out = out
        self._lock = threading.Lock()

    def write(self, records: list[Record]) -> None:
        out = self._out if self._out is not None else sys.stdout
        with self._lock:
            for r in records:
                out.write(J.write(r.to_json()) + "\n")
            out.flush()


# ------------------------------------------------------------------------------------------------------------ memory


class MemorySink(Sink):
    _NAMED: dict[str, "MemorySink"] = {}
    _NAMED_LOCK = threading.Lock()

    def __init__(self) -> None:
        self._records: list[Record] = []
        self._lock = threading.Lock()
        self._report: Any = None

    @staticmethod
    def named(name: str) -> "MemorySink":
        with MemorySink._NAMED_LOCK:
            s = MemorySink._NAMED.get(name)
            if s is None:
                s = MemorySink()
                MemorySink._NAMED[name] = s
            return s

    @staticmethod
    def get(name: str) -> "MemorySink | None":
        return MemorySink._NAMED.get(name)

    @staticmethod
    def forget(name: str) -> None:
        with MemorySink._NAMED_LOCK:
            MemorySink._NAMED.pop(name, None)

    def begin(self, context: RunContext) -> None:
        with self._lock:
            self._records.clear()

    def write(self, records: list[Record]) -> None:
        with self._lock:
            self._records.extend(records)

    def end(self, report: Any) -> None:
        self._report = report

    def records(self, entity: str | None = None) -> list[Record]:
        with self._lock:
            return [r for r in self._records if entity is None or r.entity == entity]

    @property
    def report(self) -> Any:
        return self._report


# -------------------------------------------------------------------------------------------------------------- http


class HttpSink(Sink):
    def __init__(self, c: dict, sdk: Any):
        self.url = require(c, "url")
        self.batch_size = J.integer(c, "batchSize", 500)
        self.headers: dict[str, str] = {}
        h = c.get("headers")
        for k, v in (h.items() if isinstance(h, dict) else []):
            self.headers[k] = sdk.secrets.resolve(T.render_text(J.as_text(v), T.Vars.empty(), T.LENIENT, None))
        self._pending: list[Record] = []
        self._lock = threading.Lock()
        self._ctx: RunContext | None = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def begin(self, context: RunContext) -> None:
        self._ctx = context
        self._post({"event": "begin", "run": run_json(context)})

    def write(self, records: list[Record]) -> None:
        with self._lock:
            self._pending.extend(records)
            if len(self._pending) >= self.batch_size:
                self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        body = {"event": "records", "run": run_json(self._ctx) if self._ctx else {},
                "records": [r.to_json() for r in self._pending]}
        self._pending.clear()
        self._post(body)

    def end(self, report: Any) -> None:
        with self._lock:
            self._flush()
            self._post({"event": "end", "run": run_json(self._ctx) if self._ctx else {}, "report": report.to_map()})

    def _post(self, body: dict) -> None:
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)
        req = urllib.request.Request(self.url, data=J.write(body).encode("utf-8"), method="POST", headers=headers)
        try:
            with self._opener.open(req, timeout=60) as resp:
                resp.read()
                if resp.status // 100 != 2:
                    raise AgentFrontierError(SINK_FAILED, "http sink got HTTP " + str(resp.status))
        except urllib.error.HTTPError as e:
            raise AgentFrontierError(SINK_FAILED, "http sink got HTTP " + str(e.code)) from e
        except (urllib.error.URLError, OSError) as e:
            raise AgentFrontierError(SINK_FAILED, "http sink: " + str(e), e) from e


def _neo4j(c: dict, sdk: Any) -> Sink:
    from .neo4j import Neo4jSink
    return Neo4jSink(c, sdk)


def built_in() -> list[SinkProvider]:
    return [
        _Provider("jsonl", lambda c, sdk: JsonlSink(require(c, "path"))),
        _Provider("json", lambda c, sdk: JsonSink(require(c, "path"))),
        _Provider("console", lambda c, sdk: ConsoleSink()),
        _Provider("memory", lambda c, sdk: MemorySink.named(J.text(c, "name", "default"))),
        _Provider("http", lambda c, sdk: HttpSink(c, sdk)),
        _Provider("neo4j", _neo4j),
    ]


__all__ = ["RunContext", "Sink", "SinkProvider", "JsonlSink", "JsonSink", "ConsoleSink", "MemorySink", "HttpSink",
           "built_in", "run_json", "require"]
