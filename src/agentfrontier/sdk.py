"""Entry point of the SDK.

::

    af = AgentFrontier()                                         # built-in definitions and protocols
    pg = (ConnectionConfig.builder("postgresql")
          .property("host", "localhost").property("database", "retail")
          .auth(AuthConfig.username_password("reader", "${env:PG_PASSWORD}")).build())
    t = af.test(pg)
    with af.open(pg) as s: ...                                   # pull API
    report = af.run(PipelineConfig.from_file("job.json"))        # push API: source to sinks

Thread-safe; create one and share it. Sessions are not thread-safe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from importlib import metadata as _metadata
from pathlib import Path
from typing import Any, Callable, Iterable

from . import json_util as J
from .auth import TokenService
from .definitions import DefinitionRegistry, SourceDefinition
from .engine import Detected, EngineSession
from .errors import AgentFrontierError, root_message
from .model import ConnectionConfig, SessionStats, TestResult
from .pipeline import Pipeline, PipelineConfig, RunListener, RunReport
from .protocols import Protocol, ProtocolContext
from .protocols.files import FilesProtocol
from .protocols.http import HttpProtocol
from .protocols.sql import SqlProtocol
from .secrets import SecretResolver, Secrets
from .sinks import Sink, SinkProvider, built_in
from .spec_model import AuthMethod, SpecResources
from .template import TempFiles, Vars

log = logging.getLogger("agentfrontier")

# the connection block a protocol's auth defaults (oauth2) are read from
_PROTOCOL_BLOCK = {"sql": "python"}


def _entry_points(group: str) -> list:
    try:
        eps = _metadata.entry_points()
        selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ep in selected:
        try:
            obj = ep.load()
            out.append(obj() if isinstance(obj, type) else obj)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot load %s entry point %s: %s", group, ep.name, e)
    return out


class AgentFrontier:
    """The SDK. ``definitions``: extra definition directories (later ones override earlier ones and the built-ins);
    ``protocols`` / ``sinks``: extra protocol engines and sink providers (also discovered through the
    ``agentfrontier.protocols`` and ``agentfrontier.sinks`` entry point groups); ``secret_resolvers``: extra
    ``${scheme:...}`` resolvers; ``environment``: where ``${env:...}`` reads from; ``detection_ttl``: seconds a
    detected version is reused."""

    def __init__(self, definitions: Iterable[str | Path] = (), protocols: Iterable[Protocol] = (),
                 sinks: Iterable[SinkProvider] = (), secret_resolvers: Iterable[SecretResolver] = (),
                 environment: Callable[[str], str | None] | None = None, detection_ttl: float = 600.0):
        self.spec = SpecResources()
        self.definitions = DefinitionRegistry(self.spec, list(definitions))
        self.entity_model = self.spec.entities()
        self.auth_catalog = self.spec.auth_methods()
        p: dict[str, Protocol] = {}
        for x in [SqlProtocol(), HttpProtocol(), FilesProtocol()]:
            p[x.id] = x
        for x in _entry_points("agentfrontier.protocols"):
            p[x.id] = x
        for x in protocols:
            p[x.id] = x
        self._protocols = p
        s: dict[str, SinkProvider] = {}
        for x in built_in():
            s[x.type] = x
        for x in _entry_points("agentfrontier.sinks"):
            s[x.type] = x
        for x in sinks:
            s[x.type] = x
        self._sinks = s
        self.secrets = Secrets(list(secret_resolvers), environment)
        self._tokens = TokenService()
        self._detection_ttl = detection_ttl
        self._detections: dict[str, tuple[Detected, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def create() -> "AgentFrontier":
        return AgentFrontier()

    @staticmethod
    def from_environment(environ: dict | None = None) -> "AgentFrontier":
        """An SDK configured from the environment, for embedding in services: ``AGENTFRONTIER_DEFINITIONS`` lists
        extra definition directories separated by the path separator, commas or semicolons (directories that do not
        exist are ignored). ``AGENTFRONTIER_DRIVERS`` is read by the Java edition only (JDBC jars) and ignored."""
        env = os.environ if environ is None else environ
        dirs = []
        defs = env.get("AGENTFRONTIER_DEFINITIONS")
        if defs is not None:
            for d in re.split("[,;" + re.escape(os.pathsep) + "]", defs):
                if not J.is_blank(d) and Path(J.jtrim(d)).is_dir():
                    dirs.append(Path(J.jtrim(d)))
        return AgentFrontier(definitions=dirs)

    # --------------------------------------------------------------------------------------------- catalog

    def list_definitions(self) -> list[SourceDefinition]:
        """All source definitions, by name."""
        return self.definitions.all()

    def definition(self, type_or_alias: str) -> SourceDefinition | None:
        return self.definitions.find(type_or_alias)

    def auth_methods(self, d: SourceDefinition) -> list[AuthMethod]:
        """Auth methods a definition supports, with their fields: catalog methods plus the definition's own."""
        own = {a.id: a for a in d.auth_methods}
        out = []
        for id_ in d.auth:
            a = own[id_] if id_ in own else self.auth_catalog.find(id_)
            if a is not None:
                out.append(a)
        return out

    @property
    def protocols(self) -> list[str]:
        return list(self._protocols.keys())

    @property
    def sink_types(self) -> list[str]:
        return list(self._sinks.keys())

    # --------------------------------------------------------------------------------------------- connections

    def validate(self, c: ConnectionConfig) -> list[str]:
        """Human readable problems with a connection; empty when it is usable. Secret references are not resolved."""
        problems: list[str] = []
        d = self.definitions.find(c.type)
        if d is None:
            problems.append("unknown source type '" + c.type + "'")
            return problems
        if d.protocol not in self._protocols:
            problems.append(J.jstr(d.name) + " needs protocol '" + J.jstr(d.protocol) + "', which is not installed")
        method = c.auth.method
        auth = next((a for a in self.auth_methods(d) if a.id == method), None)
        if auth is None:
            problems.append("auth method '" + method + "' is not supported by " + J.jstr(d.name) + " (supported: "
                            + J.jlist(d.auth) + ")")
        else:
            for f in auth.fields:
                if f.required and c.auth.get(f.key) is None:
                    problems.append("auth field '" + J.jstr(f.key) + "' is required for " + J.jstr(auth.name))
        carries = _auth_carries_target(d, method)
        for f in d.properties:
            v = c.properties.get(f.key)
            empty = v is None or J.is_blank(J.jstr(v))
            if f.required and empty and f.default_value is None and not carries:
                problems.append("property '" + J.jstr(f.key) + "' is required")
            if not empty and f.type == "select" and f.options and J.jstr(v) not in f.options:
                problems.append("property '" + J.jstr(f.key) + "' must be one of " + J.jlist(f.options))
        return problems

    def test(self, c: ConnectionConfig) -> TestResult:
        """Opens, tests and closes. Never raises for configuration or connection problems. ``latency_ms`` is the
        total time, connect and detection included."""
        start = time.perf_counter_ns()
        try:
            with self.open(c) as s:
                t = s.test()
                return TestResult(t.ok, t.message, t.definition, t.product, t.version, t.variant,
                                  (time.perf_counter_ns() - start) // 1_000_000, t.details)
        except AgentFrontierError as e:
            return TestResult.failed(e.message, (time.perf_counter_ns() - start) // 1_000_000)
        except Exception as e:  # noqa: BLE001
            return TestResult.failed(root_message(e), (time.perf_counter_ns() - start) // 1_000_000)

    def open(self, c: ConnectionConfig) -> EngineSession:
        """Validates, resolves secrets, connects and detects the version."""
        problems = self.validate(c)
        if problems:
            raise AgentFrontierError.invalid_config("; ".join(problems))
        d = self.definitions.get(c.type)
        protocol = self._protocols[d.protocol]
        props: dict[str, Any] = {}
        prop_specs = {f.key: f for f in d.properties}
        for f in prop_specs.values():
            if f.default_value is not None:
                props[f.key] = f.typed_default()
        for k, v in c.properties.items():
            resolved = self.secrets.resolve_deep(v)
            f = prop_specs.get(k)
            if resolved is not None and not (isinstance(resolved, str) and J.is_blank(resolved)):
                props[k] = resolved if f is None else f.convert(resolved)
        opts: dict[str, Any] = {}
        opt_specs = {f.key: f for f in d.options}
        for f in opt_specs.values():
            if f.default_value is not None:
                opts[f.key] = f.typed_default()
        for k, v in c.options.items():
            f = opt_specs.get(k)
            opts[k] = v if f is None else f.convert(v)
        auth_values = self.secrets.resolve_all(c.auth.values)
        resolved_conn = c.with_auth(c.auth.with_values(auth_values))

        method = c.auth.method
        am = next(a for a in self.auth_methods(d) if a.id == method)
        conn_block = d.connection(_PROTOCOL_BLOCK.get(d.protocol or "", d.protocol or ""))
        oauth_defaults = J.get(J.get(J.get(conn_block, "auth"), method), "oauth2")
        vars_for_token = dict(props)
        vars_for_token["auth"] = auth_values
        stats = SessionStats()
        temp = TempFiles()
        ctx = ProtocolContext(d, resolved_conn, props, opts, auth_values,
                              lambda: self._tokens.token(am.provider, auth_values, oauth_defaults,
                                                         Vars.of(vars_for_token)),
                              stats, temp)
        try:
            ps = protocol.open(ctx)
        except BaseException:
            temp.close()
            raise
        session = EngineSession(resolved_conn, d, ps, stats, temp, self.entity_model, props, opts)
        try:
            key = c.id + "|" + str(d.id) + "|" + hashlib.sha1(J.canonical(props).encode("utf-8")).hexdigest()
            with self._lock:
                cached = self._detections.get(key)
            known = cached[0] if cached is not None and time.time() - cached[1] < self._detection_ttl else None
            found = session.detect(known, self.definitions.get)
            if known is None:
                with self._lock:
                    self._detections[key] = (found, time.time())
        except BaseException:
            session.close()
            raise
        return session

    def clear_detection_cache(self) -> None:
        """Forget cached version detection, for example after a server upgrade."""
        with self._lock:
            self._detections.clear()

    # --------------------------------------------------------------------------------------------- pipelines

    def run(self, config: PipelineConfig, listener: RunListener | None = None) -> RunReport:
        return Pipeline(self, config, listener).run()

    def sink(self, config: dict) -> Sink:
        """A sink from its JSON configuration (``{"type": "jsonl", "path": "out"}``)."""
        type_ = J.text(config, "type")
        p = self._sinks.get(type_ or "")
        if p is None:
            raise AgentFrontierError.invalid_config("unknown sink type '" + J.jstr(type_) + "'; available: "
                                                    + J.jlist(self._sinks.keys()))
        return p.create(config, self)


def _auth_carries_target(d: SourceDefinition, method: str) -> bool:
    """True when the auth block for a method supplies its own URL or DSN (connection strings)."""
    conn = d.raw.get("connection")
    if not isinstance(conn, dict):
        return False
    for block in conn.values():
        a = J.get(J.get(block, "auth"), method)
        if isinstance(a, dict) and ("url" in a or "dsn" in a):
            return True
    return False
