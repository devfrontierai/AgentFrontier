"""First line of defence for ad hoc queries: accepts exactly one statement that starts like a read and contains no
write, DDL, privilege or server-side file keyword. Literals, quoted identifiers and comments are blanked out before
checking, so ``WHERE note = 'drop table'`` passes and ``SELECT 1; DROP TABLE x`` does not.

Deliberately conservative. The second line of defence is the read-only session or transaction."""

from __future__ import annotations

import re

from .. import json_util as J
from ..errors import AgentFrontierError

ALLOWED_FIRST = {"SELECT", "WITH", "VALUES", "TABLE", "SHOW", "EXPLAIN", "DESCRIBE", "DESC"}

DENIED = {"INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "INTO", "ANALYZE", "DROP", "ALTER", "CREATE", "TRUNCATE",
          "GRANT", "REVOKE", "COPY", "CALL", "EXEC", "EXECUTE", "VACUUM", "REINDEX", "LOCK"}

DENIED_FUNCTIONS = {"PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND", "PG_RELOAD_CONF", "PG_READ_FILE",
                    "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_STAT_FILE", "LO_IMPORT", "LO_EXPORT", "LO_UNLINK", "DBLINK",
                    "DBLINK_EXEC", "SET_CONFIG", "PG_ADVISORY_LOCK", "PG_ROTATE_LOGFILE", "SYSTEM$CANCEL_ALL_QUERIES",
                    "SYSTEM$ABORT_SESSION", "LOAD_FILE", "SLEEP", "BENCHMARK", "DBMS_LOCK", "UTL_HTTP", "UTL_FILE",
                    "XP_CMDSHELL", "OPENROWSET", "OPENDATASOURCE"}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def require_read_only(sql: str | None) -> str:
    """Returns the statement without trailing semicolons, or raises ``QUERY_REJECTED``."""
    if sql is None or J.is_blank(sql):
        raise AgentFrontierError.rejected("empty statement")
    blanked = J.jtrim(blank(sql))
    while blanked.endswith(";"):
        blanked = J.jtrim(blanked[:-1])
    if ";" in blanked:
        raise AgentFrontierError.rejected("only a single statement is allowed")
    first = None
    for m in _WORD.finditer(blanked):
        w = m.group().upper()
        if first is None:
            first = w
            if w not in ALLOWED_FIRST:
                raise AgentFrontierError.rejected("only read statements are allowed (got " + w + ")")
            continue
        if w in DENIED:
            raise AgentFrontierError.rejected("keyword " + w + " is not allowed in a read-only query")
        if w in DENIED_FUNCTIONS:
            raise AgentFrontierError.rejected("function " + w.lower() + " is not allowed")
    if first is None:
        raise AgentFrontierError.rejected("no statement found")
    out = J.jtrim(sql)
    while out.endswith(";"):
        out = J.jtrim(out[:-1])
    return out


def is_read_only(sql: str | None) -> bool:
    try:
        require_read_only(sql)
        return True
    except AgentFrontierError:
        return False


def blank(sql: str) -> str:
    """Replaces comments, string literals, dollar-quoted bodies and quoted identifiers with spaces."""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif c in ("'", '"', "`"):
            i = _skip_quoted(sql, i, c)
            out.append(" '' " if c == "'" else " q ")
        elif c == "$":
            tag_end = _dollar_tag_end(sql, i)
            if tag_end > 0:
                tag = sql[i:tag_end + 1]
                close = sql.find(tag, tag_end + 1)
                i = n if close < 0 else close + len(tag)
                out.append(" '' ")
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        if c == "\\" and quote == "'" and i + 1 < n:
            i += 2
            continue
        i += 1
    return n


def _dollar_tag_end(sql: str, start: int) -> int:
    i = start + 1
    while i < len(sql):
        c = sql[i]
        if c == "$":
            return i
        if not (c.isalnum() or c == "_"):
            return -1
        if i == start + 1 and c.isdigit():
            return -1  # $1 positional parameter
        i += 1
    return -1


def convert_markers(sql: str, paramstyle: str) -> str:
    """Rewrites JDBC-style ``?`` markers (outside literals, quoted identifiers and comments) to a DB-API
    ``paramstyle``; for ``format`` / ``pyformat`` literal ``%`` is doubled."""
    if paramstyle == "qmark":
        return sql
    pct = paramstyle in ("format", "pyformat")
    out = []
    i = 0
    k = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            end = n if end < 0 else end
            seg = sql[i:end]
            out.append(seg.replace("%", "%%") if pct else seg)
            i = end
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            seg = sql[i:end]
            out.append(seg.replace("%", "%%") if pct else seg)
            i = end
        elif c in ("'", '"', "`"):
            end = _skip_quoted(sql, i, c)
            seg = sql[i:end]
            out.append(seg.replace("%", "%%") if pct else seg)
            i = end
        elif c == "?":
            k += 1
            if pct:
                out.append("%s")
            elif paramstyle == "named":
                out.append(":p" + str(k))
            elif paramstyle == "numeric":
                out.append(":" + str(k))
            else:
                out.append("?")
            i += 1
        elif c == "%" and pct:
            out.append("%%")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class SqlGuard:
    require_read_only = staticmethod(require_read_only)
    is_read_only = staticmethod(is_read_only)
    blank = staticmethod(blank)
