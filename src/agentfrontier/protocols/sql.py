"""The relational engine: any database with a DB-API 2.0 driver, described by the definition's
``connection.python`` block (driver module, ``connect`` keyword arguments, and per auth method the arguments or the
DSN to use) and its SQL operations.

Connections open lazily: one for the configured target, plus one per container value for levels that declare
``connect`` (a PostgreSQL connection reaches a single database). Metadata reads run in autocommit mode; the
definition's ``session.init`` statements run once per connection (a failure is logged and the session goes on).
"""

from __future__ import annotations

import importlib
import logging
import time
from decimal import Decimal
from typing import Any, Callable

from .. import json_util as J
from .. import template as T
from ..errors import AgentFrontierError, root_message
from ..model import Column, QueryRequest, QueryResult, Scope
from . import Protocol, ProtocolContext, ProtocolSession, RawResult
from .sql_values import column_type, float32_columns, to_json
from .sqlguard import convert_markers, require_read_only

log = logging.getLogger("agentfrontier.sql")

_EXTRAS = {"psycopg": "postgres", "psycopg2": "postgres", "pymysql": "mysql", "oracledb": "oracle",
           "snowflake.connector": "snowflake", "pymssql": "sqlserver"}


def _family(module_name: str) -> str:
    m = module_name.lower()
    if m.startswith("psycopg"):
        return "psycopg"
    if m in ("pymysql", "mysqldb", "mysql.connector"):
        return "mysql"
    if m in ("oracledb", "cx_oracle"):
        return "oracle"
    if m == "sqlite3":
        return "sqlite"
    return m


class SqlSession(ProtocolSession):
    def __init__(self, ctx: ProtocolContext):
        self.ctx = ctx
        py = ctx.definition.connection("python")
        if not isinstance(py, dict):
            raise AgentFrontierError.unsupported("definition '" + str(ctx.definition.id)
                                                 + "' has no connection.python block, so it cannot be used from Python")
        self.py = py
        self.session = ctx.definition.session
        self.quote = J.text(self.session, "quote", '"')
        self.module_name = J.text(py, "module")
        if not self.module_name:
            raise AgentFrontierError.invalid_definition("connection.python.module is required")
        self.family = _family(self.module_name)
        self._module: Any = None
        self._connections: dict[tuple, Any] = {}

    # ------------------------------------------------------------------------------------------------ driver

    @property
    def module(self) -> Any:
        if self._module is None:
            try:
                self._module = importlib.import_module(self.module_name)
            except ImportError:
                package = J.text(self.py, "package", self.module_name)
                extra = _EXTRAS.get(self.module_name)
                hint = ' (pip install "agentfrontier[' + extra + ']")' if extra else ""
                raise AgentFrontierError.unsupported("Python driver module '" + self.module_name
                                                     + "' not found. Install " + package + hint + ".") from None
        return self._module

    @property
    def paramstyle(self) -> str:
        return getattr(self.module, "paramstyle", "qmark")

    def _marker(self) -> tuple[Callable[[int], str], bool]:
        style = self.paramstyle
        if style in ("format", "pyformat"):
            return (lambda i: "%s"), True
        if style == "named":
            return (lambda i: ":p" + str(i)), False
        if style == "numeric":
            return (lambda i: ":" + str(i)), False
        return (lambda i: "?"), False

    def _params(self, binds: list) -> Any:
        if self.paramstyle == "named":
            return {"p" + str(i + 1): v for i, v in enumerate(binds)}
        return list(binds) if self.family != "sqlite" else tuple(binds)

    # ------------------------------------------------------------------------------------------------ connections

    def connection(self, scope: Scope | None) -> Any:
        overrides: dict[str, Any] = {}
        for lv in self.ctx.hierarchy:
            if lv.connect is not None and scope is not None and scope.has(lv.level):
                v = scope.get(lv.level)
                if v != T.as_text(self.ctx.properties.get(lv.connect)):
                    overrides[lv.connect] = v
        overrides = dict(sorted(overrides.items()))
        key = tuple(overrides.items())
        c = self._connections.get(key)
        if c is not None and not self._is_closed(c):
            return c
        c = self._connect(overrides)
        self._connections[key] = c
        return c

    @staticmethod
    def _is_closed(c: Any) -> bool:
        closed = getattr(c, "closed", False)
        if closed is True or (isinstance(closed, int) and not isinstance(closed, bool) and closed != 0):
            return True
        return getattr(c, "open", True) is False

    def _put_all(self, kwargs: dict, block: Any, vars: T.Vars) -> None:
        if not isinstance(block, dict):
            return
        for k, t in block.items():
            v = T.render(t, vars, T.STRICT, self.ctx.temp_files) if isinstance(t, str) else t
            if v is None or T.as_text(v) == "":
                kwargs.pop(k, None)
            else:
                kwargs[k] = v

    @staticmethod
    def _missing(template: str, vars: T.Vars) -> str:
        names = [p.name for p in T.placeholders(template) if T.value(p, vars, None) is None]
        return ", ".join(names) if names else "a value"

    def _connect(self, overrides: dict) -> Any:
        ctx = self.ctx
        name = str(ctx.definition.name)
        method = ctx.auth_method
        auth_all = self.py.get("auth")
        auth_block = auth_all.get(method) if isinstance(auth_all, dict) else None
        if auth_block is None:
            raise AgentFrontierError.unsupported("auth method '" + method + "' is not supported by " + name
                                                 + " over Python (supported: "
                                                 + J.jlist(auth_all.keys() if isinstance(auth_all, dict) else [])
                                                 + ")")
        vars = ctx.connect_vars(overrides)
        module = self.module
        args: list = []
        kwargs: dict = {}
        dsn_template = J.text(auth_block, "dsn", J.text(self.py, "dsn"))
        if dsn_template is not None:
            dsn = T.render_text(dsn_template, vars, T.STRICT, ctx.temp_files)
            if dsn is None:
                raise AgentFrontierError.invalid_config("cannot build the connection string for " + name
                                                        + ": missing " + self._missing(dsn_template, vars))
            if overrides and "dsn" in auth_block and dsn == T.render_text(dsn_template, ctx.connect_vars({}),
                                                                         T.STRICT, ctx.temp_files):
                raise AgentFrontierError.unsupported(
                    "auth method '" + method + "' fixes the target in its connection string, so " + name
                    + " cannot switch to " + J.jstr(overrides) + "; read one target per connection")
            args.append(dsn)
        if "dsn" not in auth_block:
            # a connection string in the auth block carries the target itself, so the base arguments stay out
            self._put_all(kwargs, self.py.get("connect"), vars)
        self._put_all(kwargs, auth_block.get("connect"), vars)
        start = time.perf_counter_ns()
        try:
            c = module.connect(*args, **kwargs)
        except AgentFrontierError:
            raise
        except Exception as e:  # noqa: BLE001 - every driver has its own error classes
            target = "" if not overrides else " (" + J.jstr(next(iter(overrides.values()))) + ")"
            raise AgentFrontierError.connection("cannot connect to " + name + target + ": " + root_message(e), e) \
                from e
        ctx.stats.record_connection()
        self._set_autocommit(c, True)
        if self.family == "psycopg":
            # what PgJDBC sets at startup, so float values come back with every digit
            self._quiet(c, "set extra_float_digits = 3")
        if self.family == "oracle":
            # NUMBER as Decimal, like JDBC's BigDecimal: 25.00 is then the integer 25, not the float 25.0
            number = getattr(module, "DB_TYPE_NUMBER", None)

            def handler(cursor: Any, meta: Any) -> Any:
                if number is not None and meta.type_code is number:
                    return cursor.var(Decimal, arraysize=cursor.arraysize)
                return None
            try:
                c.outputtypehandler = handler
            except Exception as e:  # noqa: BLE001
                log.debug("%s: output type handler not set: %s", ctx.definition.id, e)
        if J.boolean(self.session, "readOnly", True) and self.family == "psycopg":
            try:
                c.read_only = True
            except Exception as e:  # noqa: BLE001
                log.debug("%s: read_only not supported: %s", ctx.definition.id, e)
        inits = self.session.get("init")
        for init in inits if isinstance(inits, list) else []:
            stmt = T.render_text(J.as_text(init), vars, T.LENIENT, ctx.temp_files)
            try:
                cur = c.cursor()
                try:
                    cur.execute(stmt)
                finally:
                    cur.close()
            except Exception as e:  # noqa: BLE001
                log.warning("%s: session init '%s' failed: %s", ctx.definition.id, J.as_text(init), root_message(e))
                self._recover(c)
        log.debug("connected to %s %s in %d ms", ctx.definition.id, overrides,
                  (time.perf_counter_ns() - start) // 1_000_000)
        return c

    def _quiet(self, c: Any, stmt: str) -> None:
        try:
            cur = c.cursor()
            try:
                cur.execute(stmt)
            finally:
                cur.close()
        except Exception as e:  # noqa: BLE001
            log.debug("%s: '%s' failed: %s", self.ctx.definition.id, stmt, e)
            self._recover(c)

    def _recover(self, c: Any) -> None:
        try:
            if not self._get_autocommit(c):
                c.rollback()
        except Exception:  # noqa: BLE001
            pass

    def _get_autocommit(self, c: Any) -> bool:
        if self.family == "sqlite":
            return c.isolation_level is None
        a = getattr(c, "autocommit", None)
        if callable(a):
            g = getattr(c, "get_autocommit", None)
            try:
                return bool(g()) if callable(g) else True
            except Exception:  # noqa: BLE001
                return True
        return bool(a)

    def _set_autocommit(self, c: Any, on: bool) -> None:
        try:
            if self.family == "sqlite":
                c.isolation_level = None if on else "DEFERRED"
            elif callable(getattr(c, "autocommit", None)):
                c.autocommit(on)
            else:
                c.autocommit = on
        except Exception as e:  # noqa: BLE001
            log.debug("%s: cannot set autocommit %s: %s", self.ctx.definition.id, on, e)

    # ------------------------------------------------------------------------------------------------ timeouts

    def _apply_timeout(self, c: Any, cur: Any, seconds: int, in_txn: bool) -> Callable[[], None]:
        """Statement timeout where the driver supports one; returns the function that restores the previous state."""
        if seconds <= 0:
            return lambda: None
        try:
            if self.family == "oracle":
                prev = c.call_timeout
                c.call_timeout = seconds * 1000

                def restore_oracle() -> None:
                    c.call_timeout = prev
                return restore_oracle
            if self.family == "psycopg":
                if in_txn:
                    cur.execute("set local statement_timeout = " + str(seconds * 1000))
                    return lambda: None
                cur.execute("set statement_timeout = " + str(seconds * 1000))
                return lambda: cur.execute("reset statement_timeout")
            if self.family == "mysql":
                cur.execute("set session max_execution_time = " + str(seconds * 1000))
                return lambda: cur.execute("set session max_execution_time = default")
            if self.family == "sqlite":
                deadline = time.monotonic() + seconds
                c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
                return lambda: c.set_progress_handler(None, 0)
        except Exception as e:  # noqa: BLE001
            log.debug("%s: statement timeout not applied: %s", self.ctx.definition.id, e)
        return lambda: None

    # ------------------------------------------------------------------------------------------------ operations

    def execute(self, op: dict, vars: T.Vars, scope: Scope) -> RawResult:
        sql = J.lines(op.get("sql"))
        if sql is None:
            raise AgentFrontierError.invalid_definition("operation has no sql")
        marker, escape = self._marker()
        r = T.render_sql(sql, vars, self.quote, marker, escape, self.ctx.temp_files)
        c = self.connection(scope)
        start = time.perf_counter_ns()
        rows: list[dict] = []
        try:
            cur = c.cursor()
            try:
                try:
                    cur.arraysize = J.integer(self.session, "fetchSize", 1000)
                except Exception:  # noqa: BLE001 - optional
                    pass
                timeout = J.integer(op, "timeoutSeconds", J.integer(self.session, "timeoutSeconds", 0))
                restore = self._apply_timeout(c, cur, timeout, False)
                try:
                    cur.execute(r.sql, self._params(r.binds))
                    if cur.description is None:
                        return RawResult.of_rows(rows)
                    labels = [d[0] for d in cur.description]
                    f32 = float32_columns(self.family, cur.description)
                    utc = self.family == "psycopg"
                    for rec in cur.fetchall():
                        row: dict = {}
                        for i, label in enumerate(labels):
                            row[label] = to_json(rec[i], utc, i in f32)
                        rows.append(row)
                finally:
                    restore()
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            return RawResult.of_rows(rows)
        except AgentFrontierError:
            raise
        except Exception as e:  # noqa: BLE001
            self._recover(c)
            raise AgentFrontierError.query(root_message(e), e) from e
        finally:
            self.ctx.stats.record_statement(len(rows), time.perf_counter_ns() - start)

    # ------------------------------------------------------------------------------------------------ ad hoc queries

    def query(self, request: QueryRequest) -> QueryResult:
        sql = require_read_only(request.sql)
        c = self.connection(request.scope)
        txn = J.boolean(self.session, "readOnlyTransaction", True)
        start = time.perf_counter_ns()
        rows_read = 0
        auto = self._get_autocommit(c)
        restore: Callable[[], None] = lambda: None  # noqa: E731
        try:
            if txn:
                self._set_autocommit(c, False)
            cur = c.cursor()
            try:
                try:
                    try:
                        cur.arraysize = min(request.max_rows + 1, 1000)
                    except Exception:  # noqa: BLE001
                        pass
                    restore = self._apply_timeout(c, cur, request.timeout_seconds, txn)
                    cur.execute(convert_markers(sql, self.paramstyle), self._params(request.parameters))
                    desc = cur.description or []
                    fetched = cur.fetchmany(request.max_rows + 1) if desc else []
                    truncated = len(fetched) > request.max_rows
                    fetched = fetched[:request.max_rows]
                    utc = self.family == "psycopg"
                    f32 = float32_columns(self.family, desc)
                    rows = [[to_json(v, utc, i in f32) for i, v in enumerate(rec)] for rec in fetched]
                    cols = []
                    for i, d in enumerate(desc):
                        type_name, cat = column_type(self.family, self.module, c, d, [rec[i] for rec in fetched])
                        cols.append(Column(d[0], type_name, cat))
                    rows_read = len(rows)
                    return QueryResult(cols, rows, truncated, (time.perf_counter_ns() - start) // 1_000_000)
                finally:
                    if txn:
                        c.rollback()
                    try:
                        restore()
                    except Exception as e:  # noqa: BLE001
                        log.debug("%s: restoring the statement timeout failed: %s", self.ctx.definition.id, e)
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
        except AgentFrontierError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AgentFrontierError.query(root_message(e), e) from e
        finally:
            self.ctx.stats.record_statement(rows_read, time.perf_counter_ns() - start)
            if txn:
                try:
                    self._set_autocommit(c, auto)
                except Exception:  # noqa: BLE001 - the connection closes with the session anyway
                    pass

    def close(self) -> None:
        for c in self._connections.values():
            try:
                c.close()
            except Exception as e:  # noqa: BLE001
                log.debug("closing connection failed: %s", e)
        self._connections.clear()


class SqlProtocol(Protocol):
    id = "sql"

    def open(self, context: ProtocolContext) -> ProtocolSession:
        return SqlSession(context)
