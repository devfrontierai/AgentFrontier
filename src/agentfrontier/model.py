"""The value types of the pull API: records, scopes, pages, test and query results, connection configuration."""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import json_util as J
from .errors import AgentFrontierError

# ------------------------------------------------------------------------------------------------------------ Record


class Record:
    """One canonical record: an entity type (``dataset``, ``field``, ``lineage``, ...) and its values.

    Values hold the hierarchy levels (for example ``database``, ``schema``), the canonical fields of the entity (see
    ``spec/entities.json``) and a ``properties`` map with whatever the source returned that has no canonical home.
    Every record also carries ``source``, the connection id. Values are JSON-friendly.
    """

    __slots__ = ("entity", "values")

    def __init__(self, entity: str, values: Mapping[str, Any]):
        self.entity = entity
        self.values = dict(values)

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def has(self, key: str) -> bool:
        return self.values.get(key) is not None

    def string(self, key: str) -> str | None:
        v = self.values.get(key)
        return None if v is None else J.jstr(v)

    def long_value(self, key: str) -> int | None:
        v = self.values.get(key)
        if J.is_number(v):
            return J.double_to_long(v) if isinstance(v, float) else int(v)
        if isinstance(v, str) and not J.is_blank(v):
            try:
                return J.parse_long(J.jtrim(v))
            except ValueError:
                return None
        return None

    def int_value(self, key: str) -> int | None:
        v = self.long_value(key)
        return None if v is None else J.wrap_int32(v)

    def double_value(self, key: str) -> float | None:
        v = self.values.get(key)
        return float(v) if J.is_number(v) else None

    def bool(self, key: str, fallback: bool | None = None) -> bool | None:
        v = self.values.get(key)
        return v if isinstance(v, bool) else fallback

    def strings(self, key: str) -> list[str | None]:
        v = self.values.get(key)
        if isinstance(v, list):
            return [None if o is None else J.jstr(o) for o in v]
        return [] if v is None else [J.jstr(v)]

    @property
    def properties(self) -> dict:
        p = self.values.get("properties")
        return p if isinstance(p, dict) else {}

    def get_or_property(self, key: str) -> Any:
        v = self.values.get(key)
        return v if v is not None else self.properties.get(key)

    def to_json(self) -> dict:
        out = {"entity": self.entity}
        out.update(self.values)
        return out

    def __str__(self) -> str:
        return J.write(self.to_json())

    def __repr__(self) -> str:
        return "Record(" + str(self) + ")"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Record) and other.entity == self.entity and other.values == self.values


# ------------------------------------------------------------------------------------------------------------- Scope


class Scope:
    """A position in a source's hierarchy: the value of each level from the top down, for example
    ``database=retail, schema=sales``. The root scope is empty."""

    __slots__ = ("_levels",)

    def __init__(self, levels: Mapping[str, str] | None = None):
        self._levels: dict[str, str] = dict(levels or {})

    @staticmethod
    def root() -> "Scope":
        return _ROOT

    @staticmethod
    def of(*args: Any) -> "Scope":
        """``Scope.of("database", "x", "schema", "y")`` or ``Scope.of({"database": "x"})``."""
        if len(args) == 1 and isinstance(args[0], Mapping):
            s = _ROOT
            for k, v in args[0].items():
                s = s.with_(k, v)
            return s
        if len(args) % 2:
            raise ValueError("Scope.of needs level/value pairs")
        s = _ROOT
        for i in range(0, len(args), 2):
            s = s.with_(args[i], args[i + 1])
        return s

    def with_(self, level: str, value: str) -> "Scope":
        m = dict(self._levels)
        m[level] = value
        return Scope(m)

    def get(self, level: str) -> str | None:
        return self._levels.get(level)

    def get_or_default(self, level: str, fallback: str) -> str:
        v = self._levels.get(level)
        return fallback if v is None else v

    def has(self, level: str) -> bool:
        return level in self._levels

    @property
    def depth(self) -> int:
        return len(self._levels)

    @property
    def is_root(self) -> bool:
        return not self._levels

    def as_map(self) -> dict[str, str]:
        return dict(self._levels)

    def values(self) -> list[str]:
        return list(self._levels.values())

    def up_to(self, level: str) -> "Scope":
        """The scope with only the levels down to and including ``level``; root if the level is absent."""
        if level not in self._levels:
            return _ROOT
        m = {}
        for k, v in self._levels.items():
            m[k] = v
            if k == level:
                break
        return Scope(m)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Scope) and other._levels == self._levels

    def __hash__(self) -> int:
        return hash(tuple(self._levels.items()))

    def __str__(self) -> str:
        if not self._levels:
            return "(root)"
        return "/".join(k + "=" + J.jstr(v) for k, v in self._levels.items())

    def __repr__(self) -> str:
        return "Scope(" + str(self) + ")"


_ROOT = Scope()

# -------------------------------------------------------------------------------------------------------- small types


@dataclass(frozen=True)
class Page:
    """One page of records. ``next_cursor`` is None on the last page; a page can be empty and still have a next
    cursor when filters removed every record of it."""

    records: list[Record]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True)
class SourceInfo:
    """What a session found out about its source when it opened."""

    definition: str | None
    requested: str | None
    product: str | None
    version: str | None
    banner: str | None
    variant: str | None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TestResult:
    """Outcome of a connection test."""

    __test__ = False  # not a pytest class

    ok: bool
    message: str | None
    definition: str | None
    product: str | None
    version: str | None
    variant: str | None
    latency_ms: int
    details: dict = field(default_factory=dict)

    @staticmethod
    def failed(message: str, latency_ms: int) -> "TestResult":
        return TestResult(False, message, None, None, None, None, latency_ms, {})

    def to_dict(self) -> dict:
        """The JSON shape shared with the Java edition."""
        return {"ok": self.ok, "message": self.message, "definition": self.definition, "product": self.product,
                "version": self.version, "variant": self.variant, "latencyMs": self.latency_ms,
                "details": dict(self.details)}


DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT_SECONDS = 60


class QueryRequest:
    """A read-only query against a SQL source.

    ``sql``: one SELECT-style statement; anything that could write is rejected before it is sent. ``scope``: where to
    run it when the source needs a connection per level. ``max_rows``: rows to return at most (the result is marked
    truncated). ``timeout_seconds``: statement timeout where the driver supports one. ``parameters``: positional
    ``?`` parameters, bound and never concatenated.
    """

    __slots__ = ("sql", "scope", "max_rows", "timeout_seconds", "parameters")

    def __init__(self, sql: str, scope: Scope | None = None, max_rows: int = DEFAULT_MAX_ROWS,
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS, parameters: Iterable[Any] | None = None):
        self.sql = sql
        self.scope = Scope.root() if scope is None else scope
        self.max_rows = DEFAULT_MAX_ROWS if not max_rows or max_rows <= 0 else int(max_rows)
        self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS if not timeout_seconds or timeout_seconds <= 0 \
            else int(timeout_seconds)
        self.parameters = list(parameters or [])

    @staticmethod
    def of(sql: str) -> "QueryRequest":
        return QueryRequest(sql)


class Category(str, enum.Enum):
    """Coarse column type used by charting and analysis."""

    NUMBER = "NUMBER"
    TEXT = "TEXT"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    BOOLEAN = "BOOLEAN"
    OTHER = "OTHER"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Column:
    name: str
    type_name: str | None
    category: Category

    def to_dict(self) -> dict:
        return {"name": self.name, "typeName": self.type_name, "category": self.category.value}


@dataclass(frozen=True)
class QueryResult:
    """Tabular result of a read-only query. Values are JSON-friendly: numbers, booleans, strings (dates and
    timestamps as ISO-8601) and lists."""

    columns: list[Column]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def column_index(self, name: str) -> int:
        for i, c in enumerate(self.columns):
            if J.equals_ignore_case(c.name, name):
                return i
        return -1

    def column(self, name: str) -> list[Any]:
        idx = self.column_index(name)
        if idx < 0:
            raise ValueError("no column '" + name + "' in result")
        return [r[idx] for r in self.rows]

    def to_dict(self) -> dict:
        return {"columns": [c.to_dict() for c in self.columns], "rows": [list(r) for r in self.rows],
                "truncated": self.truncated, "elapsedMs": self.elapsed_ms}


@dataclass(frozen=True)
class Snapshot:
    """Load put on a source: statements or requests sent, rows read, time spent waiting, connections opened."""

    statements: int = 0
    rows: int = 0
    elapsed_ms: int = 0
    connections: int = 0

    def minus(self, earlier: "Snapshot") -> "Snapshot":
        return Snapshot(self.statements - earlier.statements, self.rows - earlier.rows,
                        self.elapsed_ms - earlier.elapsed_ms, self.connections - earlier.connections)

    def plus(self, other: "Snapshot") -> "Snapshot":
        return Snapshot(self.statements + other.statements, self.rows + other.rows,
                        self.elapsed_ms + other.elapsed_ms, self.connections + other.connections)


class SessionStats:
    """The load a session put on its source. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statements = 0
        self._rows = 0
        self._nanos = 0
        self._connections = 0

    def record_statement(self, row_count: int, elapsed_nanos: int) -> None:
        with self._lock:
            self._statements += 1
            self._rows += row_count
            self._nanos += elapsed_nanos

    def record_connection(self) -> None:
        with self._lock:
            self._connections += 1

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._statements, self._rows, self._nanos // 1_000_000, self._connections)


# ----------------------------------------------------------------------------------------------------- configuration


class AuthConfig:
    """Credentials for a connection: an auth method id from the catalog (``spec/auth-methods.json``, or a method a
    definition declares itself) and that method's field values. Values may be secret references such as
    ``${env:PG_PASSWORD}``; they are resolved when a session opens. ``str()`` prints field names only."""

    __slots__ = ("method", "values")

    def __init__(self, method: str | None = None, values: Mapping[str, Any] | None = None):
        self.method = "none" if method is None or J.is_blank(method) else J.jtrim(method)
        self.values: dict[str, str] = {k: v for k, v in (values or {}).items() if v is not None}

    @staticmethod
    def of(method: str, values: Mapping[str, Any] | None = None) -> "AuthConfig":
        return AuthConfig(method, values)

    @staticmethod
    def none() -> "AuthConfig":
        return AuthConfig("none", {})

    @staticmethod
    def username_only(username: str) -> "AuthConfig":
        return AuthConfig("username_only", {"username": username})

    @staticmethod
    def username_password(username: str, password: str) -> "AuthConfig":
        return AuthConfig("username_password", {"username": username, "password": password})

    @staticmethod
    def token(token: str, username: str | None = None) -> "AuthConfig":
        v = {"token": token} if username is None else {"username": username, "token": token}
        return AuthConfig("token", v)

    @staticmethod
    def api_key(api_key: str) -> "AuthConfig":
        return AuthConfig("api_key", {"apiKey": api_key})

    @staticmethod
    def key_pair(username: str, private_key_pem: str, passphrase: str | None = None) -> "AuthConfig":
        return AuthConfig("key_pair", {"username": username, "privateKey": private_key_pem,
                                       "privateKeyPassphrase": passphrase})

    @staticmethod
    def oauth2_client_credentials(token_url: str | None, client_id: str, client_secret: str,
                                  scope: str | None = None) -> "AuthConfig":
        return AuthConfig("oauth2_client_credentials", {"tokenUrl": token_url, "clientId": client_id,
                                                        "clientSecret": client_secret, "scope": scope})

    @staticmethod
    def connection_string(connection_string: str) -> "AuthConfig":
        return AuthConfig("connection_string", {"connectionString": connection_string})

    def get(self, key: str) -> str | None:
        v = self.values.get(key)
        return None if v is None or J.is_blank(J.jstr(v)) else v

    def with_values(self, replaced: Mapping[str, Any]) -> "AuthConfig":
        return AuthConfig(self.method, replaced)

    def __str__(self) -> str:
        return "AuthConfig[method=" + self.method + ", fields=" + J.jlist(self.values.keys()) + "]"

    __repr__ = __str__


NONE_AUTH = AuthConfig("none", {})


def _copy(m: Mapping[str, Any] | None) -> dict:
    if not m:
        return {}
    return {k: v for k, v in m.items() if v is not None}


class ConnectionConfig:
    """One configured data source: which definition to use (``type``), how to reach it (``properties``), how to
    authenticate (``auth``) and how to behave (``options``).

    The JSON form is the same in every language::

        {"id": "demo-retail", "type": "postgresql",
         "properties": {"host": "localhost", "port": 55432, "database": "retail"},
         "auth": {"method": "username_password", "username": "df_reader", "password": "${env:RETAIL_PASSWORD}"},
         "options": {"excludeSchemas": ["tmp_*"]}}

    ``auth`` may also be written ``{"type": ..., "values": {...}}``.
    """

    __slots__ = ("id", "name", "type", "properties", "auth", "options")

    def __init__(self, type: str | None, properties: Mapping[str, Any] | None = None,
                 auth: AuthConfig | None = None, options: Mapping[str, Any] | None = None,
                 id: str | None = None, name: str | None = None):
        if type is None or J.is_blank(type):
            raise AgentFrontierError.invalid_config("connection type is required")
        self.type = J.jtrim(type)
        if id is None or J.is_blank(id):
            self.id = self.type if (name is None or J.is_blank(name)) else name
        else:
            self.id = id
        self.name = self.id if name is None or J.is_blank(name) else name
        self.properties = _copy(properties)
        self.options = _copy(options)
        self.auth = NONE_AUTH if auth is None else auth

    @staticmethod
    def of(type: str, properties: Mapping[str, Any] | None = None, auth: AuthConfig | None = None) -> "ConnectionConfig":
        return ConnectionConfig(type, properties, auth)

    @staticmethod
    def builder(type: str) -> "ConnectionBuilder":
        return ConnectionBuilder(type)

    @staticmethod
    def from_json(node: Any) -> "ConnectionConfig":
        """From a JSON text or an already parsed object."""
        if isinstance(node, (str, bytes)):
            node = J.parse(node.decode("utf-8") if isinstance(node, bytes) else node)
        if not isinstance(node, dict):
            raise AgentFrontierError.invalid_config("a connection must be a JSON object")
        auth = NONE_AUTH
        a = node.get("auth")
        if isinstance(a, dict):
            method = J.text(a, "method")
            if method is None:
                method = J.text(a, "type")
            values: dict[str, str] = {}
            nested = a.get("values")
            source = nested if isinstance(nested, dict) else a
            for k, v in source.items():
                if not isinstance(nested, dict) and k in ("method", "type"):
                    continue
                if v is not None:
                    values[k] = v if isinstance(v, str) else J.write(v)
            auth = AuthConfig(method, values)

        def txt(key: str) -> str | None:
            v = node.get(key)
            return None if v is None else J.as_text(v)

        return ConnectionConfig(txt("type"), J.to_map(node.get("properties")), auth, J.to_map(node.get("options")),
                                id=txt("id"), name=txt("name"))

    @staticmethod
    def from_file(path: str | Path) -> "ConnectionConfig":
        try:
            content = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise AgentFrontierError.invalid_config("cannot read connection file " + str(path) + ": " + str(e)) from e
        return ConnectionConfig.from_json(content)

    def to_json(self) -> dict:
        """JSON form. Auth values are included as given (they may be secret references); use with care."""
        a = {"method": self.auth.method}
        a.update(self.auth.values)
        return {"id": self.id, "name": self.name, "type": self.type, "properties": dict(self.properties),
                "auth": a, "options": dict(self.options)}

    def with_type(self, new_type: str) -> "ConnectionConfig":
        return ConnectionConfig(new_type, self.properties, self.auth, self.options, self.id, self.name)

    def with_auth(self, new_auth: AuthConfig) -> "ConnectionConfig":
        return ConnectionConfig(self.type, self.properties, new_auth, self.options, self.id, self.name)

    def with_options(self, extra: Mapping[str, Any]) -> "ConnectionConfig":
        merged = dict(self.options)
        merged.update(extra)
        return ConnectionConfig(self.type, self.properties, self.auth, merged, self.id, self.name)

    def with_properties(self, extra: Mapping[str, Any]) -> "ConnectionConfig":
        merged = dict(self.properties)
        merged.update(extra)
        return ConnectionConfig(self.type, merged, self.auth, self.options, self.id, self.name)

    def __str__(self) -> str:
        return ("ConnectionConfig[id=" + self.id + ", type=" + self.type + ", properties="
                + J.jlist(self.properties.keys()) + ", auth=" + str(self.auth) + "]")

    __repr__ = __str__


class ConnectionBuilder:
    def __init__(self, type: str):
        self._type = type
        self._id: str | None = None
        self._name: str | None = None
        self._properties: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._auth: AuthConfig = NONE_AUTH

    def id(self, v: str) -> "ConnectionBuilder":
        self._id = v
        return self

    def name(self, v: str) -> "ConnectionBuilder":
        self._name = v
        return self

    def property(self, key: str, value: Any) -> "ConnectionBuilder":
        self._properties[key] = value
        return self

    def option(self, key: str, value: Any) -> "ConnectionBuilder":
        self._options[key] = value
        return self

    def auth(self, v: AuthConfig) -> "ConnectionBuilder":
        self._auth = v
        return self

    def build(self) -> ConnectionConfig:
        return ConnectionConfig(self._type, self._properties, self._auth, self._options, self._id, self._name)
