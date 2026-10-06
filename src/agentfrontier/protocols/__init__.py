"""Protocols: one engine per access protocol. The SDK ships ``sql`` (DB-API 2.0 drivers), ``http`` (REST / JSON
APIs) and ``files`` (local file storage). A new protocol is the only thing that needs code; register it with
``AgentFrontier(protocols=[...])`` or the ``agentfrontier.protocols`` entry point group."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..definitions import Level, SourceDefinition
from ..errors import AgentFrontierError
from ..model import ConnectionConfig, QueryRequest, QueryResult, Scope, SessionStats
from ..template import TempFiles, Vars


@dataclass
class RawResult:
    """What one execution returned, before mapping: ``rows`` (tabular results; None when the result is a body),
    ``body`` (parsed response body), ``headers`` (lower-case names to value lists)."""

    rows: list[dict] | None = None
    body: Any = None
    headers: dict[str, list[str]] = field(default_factory=dict)

    @staticmethod
    def of_rows(rows: list[dict]) -> "RawResult":
        return RawResult(rows, None, {})

    @staticmethod
    def of_body(body: Any, headers: dict[str, list[str]] | None = None) -> "RawResult":
        return RawResult(None, body, headers or {})

    def size(self) -> int:
        if self.rows is not None:
            return len(self.rows)
        if isinstance(self.body, list):
            return len(self.body)
        return 0 if self.body is None else 1


@dataclass
class ProtocolContext:
    """What a protocol gets to open a session."""

    definition: SourceDefinition
    connection: ConnectionConfig
    properties: dict[str, Any]
    options: dict[str, Any]
    auth_values: dict[str, str]
    token: Callable[[], dict]
    stats: SessionStats
    temp_files: TempFiles

    @property
    def auth_method(self) -> str:
        return self.connection.auth.method

    @property
    def hierarchy(self) -> list[Level]:
        return self.definition.hierarchy

    def connect_vars(self, overrides: Mapping[str, Any] | None = None) -> Vars:
        """Variables for rendering a connection block: properties (with ``overrides`` on top), ``options.*``,
        ``auth.*`` and ``token.*``."""
        m = dict(self.properties)
        m.update(overrides or {})
        m["options"] = self.options
        m["auth"] = self.auth_values
        m["token"] = self.token()
        return Vars.of(m)


class ProtocolSession:
    """A protocol's open handle on one source. Not thread-safe; the engine uses one per session."""

    def execute(self, op: dict, vars: Vars, scope: Scope) -> RawResult:
        """Executes one operation (or one alternative of it) once: one statement, one request, one file listing."""
        raise NotImplementedError

    def query(self, request: QueryRequest) -> QueryResult:
        raise AgentFrontierError.unsupported("this source does not run ad hoc queries")

    def close(self) -> None:
        pass


class Protocol:
    """Base class of protocol engines: ``id`` is the value definitions put in ``protocol``."""

    id: str = ""

    def open(self, context: ProtocolContext) -> ProtocolSession:
        raise NotImplementedError
