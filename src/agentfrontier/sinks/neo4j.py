"""Writes records to Neo4j with the official ``neo4j`` driver.

What each entity becomes in the graph is configuration: the ``mapping`` file (default
``spec/sinks/neo4j-default.json``) gives, per entity, Cypher statements that receive ``$rows``, ``$source`` and
``$runId``; each row holds the record's ``props`` and the helper uids from :mod:`agentfrontier.sinks.uids`.

Configuration: ``uri``, ``username``, ``password`` (secret references allowed), ``database``, ``mapping``
("default" or a file), ``batchSize`` (500), ``removeStale`` (false: when true and the run succeeded, nodes of this
source not seen in this run are removed).
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path
from typing import Any

from .. import json_util as J
from ..errors import SINK_FAILED, AgentFrontierError, root_message
from ..model import Record
from . import RunContext, Sink
from .uids import row as uid_row

log = logging.getLogger("agentfrontier.neo4j")


def _require(c: dict, key: str) -> str:
    v = J.text(c, key)
    if v is None:
        raise AgentFrontierError.invalid_config("neo4j sink needs '" + key + "'")
    return v


class Neo4jSink(Sink):
    def __init__(self, config: dict, sdk: Any):
        if importlib.util.find_spec("neo4j") is None:
            raise AgentFrontierError.unsupported('the neo4j sink needs the neo4j driver: pip install '
                                                 '"agentfrontier[neo4j]"')
        self.uri = _require(config, "uri")
        self.username = sdk.secrets.resolve(J.text(config, "username", "neo4j"))
        self.password = sdk.secrets.resolve(J.text(config, "password", ""))
        self.database = J.text(config, "database")
        self.batch_size = J.integer(config, "batchSize", 500)
        self.remove_stale = J.boolean(config, "removeStale", False)
        m = J.text(config, "mapping", "default")
        if m == "default":
            self.mapping = sdk.spec.read("sinks/neo4j-default.json")
        else:
            try:
                self.mapping = J.loads_first(Path(m).read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise AgentFrontierError.invalid_config("cannot read neo4j mapping " + m + ": " + str(e)) from e
        self.statements: dict[str, list[tuple[Any, str]]] = {}
        ents = self.mapping.get("entities") if isinstance(self.mapping, dict) else None
        for entity, stmts in (ents.items() if isinstance(ents, dict) else []):
            lst = []
            for s in stmts if isinstance(stmts, list) else []:
                lst.append((None, s) if isinstance(s, str) else (J.get(s, "when"), J.text(s, "cypher")))
            self.statements[entity] = lst
        self._driver: Any = None
        self._ctx: RunContext | None = None
        self._lock = threading.Lock()

    def _session(self) -> Any:
        return self._driver.session() if self.database is None else self._driver.session(database=self.database)

    def begin(self, context: RunContext) -> None:
        import neo4j
        self._ctx = context
        try:
            self._driver = neo4j.GraphDatabase.driver(self.uri, auth=neo4j.basic_auth(self.username, self.password))
            self._driver.verify_connectivity()
        except Exception as e:  # noqa: BLE001
            raise AgentFrontierError(SINK_FAILED, "cannot reach Neo4j at " + self.uri + ": " + root_message(e), e) \
                from e
        try:
            with self._session() as s:
                for stmt in self.mapping.get("setup") or []:
                    s.run(J.as_text(stmt)).consume()
        except Exception as e:  # noqa: BLE001
            raise AgentFrontierError(SINK_FAILED, "neo4j setup failed: " + root_message(e), e) from e

    def write(self, records: list[Record]) -> None:
        with self._lock:
            by_entity: dict[str, list[Record]] = {}
            for r in records:
                by_entity.setdefault(r.entity, []).append(r)
            assert self._ctx is not None
            try:
                with self._session() as s:
                    for entity, all_ in by_entity.items():
                        stmts = self.statements.get(entity)
                        if not stmts:
                            continue
                        for i in range(0, len(all_), self.batch_size):
                            chunk = all_[i:i + self.batch_size]
                            for when, cypher in stmts:
                                rows = [uid_row(r, self._ctx.levels) for r in chunk if _matches(when, r)]
                                if not rows:
                                    continue
                                params = {"rows": rows, "source": self._ctx.source, "runId": self._ctx.run_id}
                                s.execute_write(lambda tx, c=cypher, p=params: tx.run(c, p).consume())
            except AgentFrontierError:
                raise
            except Exception as e:  # noqa: BLE001
                raise AgentFrontierError(SINK_FAILED, "neo4j write failed: " + root_message(e), e) from e

    def end(self, report: Any) -> None:
        if self.remove_stale and report.status == "SUCCEEDED" and self._driver is not None and self._ctx is not None:
            with self._session() as s:
                for stmt in self.mapping.get("cleanup") or []:
                    params = {"source": self._ctx.source, "runId": self._ctx.run_id}
                    s.execute_write(lambda tx, c=J.as_text(stmt), p=params: tx.run(c, p).consume())

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception as e:  # noqa: BLE001
                log.debug("closing neo4j driver failed: %s", e)


def _matches(when: Any, r: Record) -> bool:
    if not isinstance(when, dict):
        return True
    for k, v in when.items():
        if J.as_text(v) != r.string(k):
            return False
    return True
