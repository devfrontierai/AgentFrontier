"""The template language shared by every implementation, the SQL renderer and the variables templates read.

``{{name}}`` or ``{{name|filter|filter:arg}}``. Filters: ``default:X`` (X may itself contain placeholders), ``int``,
``mul:N``, ``add:N``, ``epochMs``, ``upper``, ``lower``, ``url``, ``join:SEP``, ``removePrefix:X``, ``file``, ``json``,
``base64``; ``ident`` only means something in SQL.

A template that is exactly one placeholder returns the value with its type (number, boolean, list); otherwise the
result is text. In STRICT mode a placeholder without a value makes the whole result None (how optional connection
properties and headers drop out); in LENIENT mode it renders as empty text.
"""

from __future__ import annotations

import atexit
import base64
import datetime as _dt
import os
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from . import json_util as J
from .errors import AgentFrontierError

STRICT = "STRICT"
LENIENT = "LENIENT"


class Mode:
    STRICT = STRICT
    LENIENT = LENIENT


# --------------------------------------------------------------------------------------------------------------- Vars


def lookup(root: Any, name: str) -> Any:
    """Dotted lookup: an exact key wins, otherwise the name is followed through nested maps and lists."""
    if isinstance(root, Mapping):
        if name in root:
            return root[name]
        dot = name.find(".")
        while dot > 0:
            head = name[:dot]
            if head in root:
                v = lookup(root[head], name[dot + 1:])
                if v is not None:
                    return v
            dot = name.find(".", dot + 1)
        return None
    if isinstance(root, (list, tuple)):
        dot = name.find(".")
        head = name if dot < 0 else name[:dot]
        try:
            i = J.parse_int(head)
        except ValueError:
            return None
        v = root[i] if 0 <= i < len(root) else None
        return v if dot < 0 else lookup(v, name[dot + 1:])
    return None


class Vars:
    """Variables a template can reference."""

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[str], Any]):
        self._fn = fn

    def get(self, name: str) -> Any:
        return self._fn(name)

    @staticmethod
    def of(m: Mapping[str, Any] | None) -> "Vars":
        mm = {} if m is None else m
        return Vars(lambda name: lookup(mm, name))

    @staticmethod
    def empty() -> "Vars":
        return Vars(lambda name: None)

    def then(self, second: "Vars") -> "Vars":
        """Variables from this, falling back to ``second``."""
        def f(name: str) -> Any:
            v = self.get(name)
            return v if v is not None else second.get(name)
        return Vars(f)

    def with_(self, extra: Mapping[str, Any]) -> "Vars":
        copy_ = dict(extra)

        def f(name: str) -> Any:
            return copy_[name] if name in copy_ else self.get(name)
        return Vars(f)


# ---------------------------------------------------------------------------------------------------------- TempFiles


class TempFiles:
    """Files staged for drivers that want a path rather than content (certificates, private keys). Readable only by
    the current user where the file system supports it; deleted when the session closes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: list[str] = []
        self._by_content: dict[str, str] = {}

    def write(self, content: str) -> str:
        with self._lock:
            existing = self._by_content.get(content)
            if existing is not None:
                return existing
            try:
                fd, path = tempfile.mkstemp(prefix="agentfrontier-", suffix=".tmp")
                try:
                    if os.name == "posix":
                        os.fchmod(fd, 0o600)
                    os.write(fd, content.encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError as e:
                raise AgentFrontierError.connection("cannot stage a credential file: " + str(e), e) from e
            self._files.append(path)
            self._by_content[content] = path
            _ALL_TEMP.add(path)
            return path

    def close(self) -> None:
        with self._lock:
            for f in self._files:
                try:
                    os.remove(f)
                except OSError:
                    pass
                _ALL_TEMP.discard(f)
            self._files.clear()
            self._by_content.clear()


_ALL_TEMP: set[str] = set()


@atexit.register
def _cleanup_temp() -> None:  # the fallback, like deleteOnExit
    for f in list(_ALL_TEMP):
        try:
            os.remove(f)
        except OSError:
            pass


# ----------------------------------------------------------------------------------------------------------- Template


@dataclass(frozen=True)
class Placeholder:
    """A parsed ``{{...}}``."""

    name: str
    filters: tuple  # of (name, arg-or-None)
    start: int
    end: int

    def has(self, filter_name: str) -> bool:
        return any(f[0] == filter_name for f in self.filters)


_CACHE: dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def placeholders(text: str | None) -> tuple:
    """Placeholders of a text, in order."""
    if text is None:
        return ()
    ps = _CACHE.get(text)
    if ps is None:
        ps = _parse(text)
        with _CACHE_LOCK:
            if len(_CACHE) > 20000:
                _CACHE.clear()
            _CACHE[text] = ps
    return ps


def _abbreviate(s: str) -> str:
    return s[:80] + "..." if len(s) > 80 else s


def _parse(text: str) -> tuple:
    out = []
    i = 0
    n = len(text)
    while True:
        open_ = text.find("{{", i)
        if open_ < 0:
            break
        depth = 1
        j = open_ + 2
        while j < n - 1 and depth > 0:
            if text.startswith("{{", j):
                depth += 1
                j += 2
            elif text.startswith("}}", j):
                depth -= 1
                j += 2
            else:
                j += 1
        if depth > 0:
            raise AgentFrontierError.invalid_definition("unclosed {{ in template: " + _abbreviate(text))
        inner = J.jtrim(text[open_ + 2:j - 2])
        parts = _split_top_level(inner)
        name = J.jtrim(parts[0])
        filters = []
        for k in range(1, len(parts)):
            f = J.jtrim(parts[k])
            colon = f.find(":")
            filters.append((f, None) if colon < 0 else (J.jtrim(f[:colon]), f[colon + 1:]))
        out.append(Placeholder(name, tuple(filters), open_, j))
        i = j
    return tuple(out)


def _split_top_level(s: str) -> list[str]:
    """Splits on '|' outside nested {{ }}."""
    parts = []
    depth = 0
    start = 0
    i = 0
    while i < len(s):
        if s.startswith("{{", i):
            depth += 1
            i += 1
        elif s.startswith("}}", i):
            depth -= 1
            i += 1
        elif s[i] == "|" and depth == 0:
            parts.append(s[start:i])
            start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


def render(text: str | None, vars: Vars, mode: str = STRICT, files: TempFiles | None = None) -> Any:
    if text is None:
        return None
    ps = placeholders(text)
    if not ps:
        return text
    if len(ps) == 1 and ps[0].start == 0 and ps[0].end == len(text):
        v = value(ps[0], vars, files)
        return "" if v is None and mode == LENIENT else v
    b = []
    pos = 0
    for p in ps:
        b.append(text[pos:p.start])
        v = value(p, vars, files)
        if v is None:
            if mode == STRICT:
                return None
        else:
            b.append(as_text(v))
        pos = p.end
    b.append(text[pos:])
    return "".join(b)


def render_text(text: str | None, vars: Vars, mode: str = STRICT, files: TempFiles | None = None) -> str | None:
    """Text form of :func:`render`; None stays None."""
    v = render(text, vars, mode, files)
    return None if v is None else as_text(v)


def render_json(node: Any, vars: Vars, mode: str = STRICT, files: TempFiles | None = None) -> Any:
    """Renders every string inside a JSON object or array (keys too), keeping the structure. Values that render to
    None are dropped."""
    if node is None:
        return None
    if isinstance(node, str):
        return render(node, vars, mode, files)
    if isinstance(node, dict):
        out = {}
        for k, x in node.items():
            key = render_text(k, vars, LENIENT, files)
            v = render_json(x, vars, mode, files)
            if v is not None and key:
                out[key] = v
        return out
    if isinstance(node, list):
        out_l = []
        for x in node:
            v = render_json(x, vars, mode, files)
            if v is not None:
                out_l.append(v)
        return out_l
    return J.deep_copy(node)


def value(p: Placeholder, vars: Vars, files: TempFiles | None = None) -> Any:
    """The placeholder's variable with its filters applied."""
    v = vars.get(p.name)
    for f in p.filters:
        v = apply(f[0], f[1], v, vars, files)
    return v


def _decimal(v: Any) -> Decimal:
    s = J.jtrim(J.jstr(v))
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ValueError("Character array is missing \"exponent\" mark 'e' or 'E'." if not s else
                         "invalid number '" + s + "'") from e
    if not d.is_finite():
        raise ValueError("invalid number '" + s + "'")
    return d


def _num_result(r: Decimal) -> Any:
    if r == r.to_integral_value():
        return J.wrap_int64(int(r))
    return float(r)


def apply(name: str, arg: str | None, v: Any, vars: Vars, files: TempFiles | None) -> Any:
    if name == "default":
        if v is None or (isinstance(v, str) and v == ""):
            return "" if arg is None else render(arg, vars, LENIENT, files)
        return v
    if name == "ident":
        return v  # applied by the SQL renderer
    if name == "int":
        if v is None:
            return None
        if J.is_number(v):
            return J.double_to_long(v) if isinstance(v, float) else J.wrap_int64(int(v))
        try:
            return J.parse_long(J.jtrim(J.jstr(v)))
        except ValueError:
            raise AgentFrontierError.invalid_config("expected a whole number, got '" + J.jstr(v) + "'") from None
    if name == "mul":
        if v is None:
            return None
        return _num_result(_decimal(v) * _decimal(arg))
    if name == "add":
        if v is None:
            return None
        return _num_result(_decimal(v) + _decimal(arg))
    if name == "epochMs":
        if v is None or J.is_blank(J.jstr(v)):
            return None
        ms = int(_decimal(v))  # BigDecimal.longValue truncates
        secs = ms // 1000
        t = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=secs)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    if name == "upper":
        return None if v is None else as_text(v).upper()
    if name == "lower":
        return None if v is None else as_text(v).lower()
    if name == "url":
        return None if v is None else J.url_encode20(as_text(v))
    if name == "join":
        if v is None:
            return None
        sep = "," if arg is None else arg
        if isinstance(v, (list, tuple, set)):
            return sep.join(_text_or_null(x) for x in v)
        return as_text(v)
    if name == "removePrefix":
        if v is None or arg is None:
            return v
        s = as_text(v)
        return s[len(arg):] if s.startswith(arg) else s
    if name == "file":
        if v is None:
            return None
        if files is None:
            raise AgentFrontierError.invalid_definition("the file filter is not available here")
        return files.write(as_text(v))
    if name == "json":
        return None if v is None else J.write(v)
    if name == "base64":
        return None if v is None else base64.b64encode(as_text(v).encode("utf-8")).decode("ascii")
    raise AgentFrontierError.invalid_definition("unknown template filter '" + name + "'")


def _text_or_null(x: Any) -> str:
    t = as_text(x)
    return "null" if t is None else t


def as_text(v: Any) -> str | None:
    """Text form of a value: lists joined with commas, numbers in plain notation."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return J.plain_decimal(v)
    if isinstance(v, float) and v == v and v not in (float("inf"), float("-inf")) and v == round(v) \
            and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, (list, tuple, set)):
        return ",".join(_text_or_null(x) for x in v)
    if isinstance(v, dict):
        return J.write(v)
    return J.jstr(v)


# -------------------------------------------------------------------------------------------------------- SqlRenderer


@dataclass(frozen=True)
class Rendered:
    """A statement ready for the driver."""

    sql: str
    binds: list


def render_sql(sql: str, vars: Vars, quote: str | None = '"', marker: Callable[[int], str] = lambda i: "?",
               escape_percent: bool = False, files: TempFiles | None = None) -> Rendered:
    """Turns a definition's SQL into a statement plus bind values.

    ``{{name}}`` becomes a bind parameter (a list expands to one parameter per item, an empty list to ``null``);
    ``{{name|ident}}`` is inlined as a quoted identifier; ``{{name|int}}`` is inlined as a validated whole number;
    ``[[ ... ]]`` is an optional block, kept only when every placeholder inside has a value.

    ``marker`` gives the parameter marker for a 1-based index; ``escape_percent`` doubles literal ``%`` (for drivers
    with format-style parameters).
    """
    text_ = optional_blocks(sql, vars, files)
    ps = placeholders(text_)
    b: list[str] = []
    binds: list = []
    pos = 0

    def literal(s: str) -> None:
        b.append(s.replace("%", "%%") if escape_percent else s)

    for p in ps:
        literal(text_[pos:p.start])
        v = value(p, vars, files)
        if p.has("ident"):
            if v is None or J.jstr(v) == "":
                raise AgentFrontierError.invalid_config("no value for identifier '" + p.name + "'")
            q = '"' if quote is None else quote
            literal(q + J.jstr(v).replace(q, q + q) + q)
        elif p.has("int"):
            if v is None:
                raise AgentFrontierError.invalid_config("no value for '" + p.name + "'")
            b.append(str(J.double_to_long(v) if isinstance(v, float) else int(v)))
        elif isinstance(v, (list, tuple, set)):
            if not v:
                b.append("null")
            else:
                for k, item in enumerate(v):
                    if k > 0:
                        b.append(", ")
                    binds.append(item)
                    b.append(marker(len(binds)))
        else:
            binds.append(v)
            b.append(marker(len(binds)))
        pos = p.end
    literal(text_[pos:])
    return Rendered("".join(b), binds)


def optional_blocks(sql: str, vars: Vars, files: TempFiles | None = None) -> str:
    if "[[" not in sql:
        return sql
    b = []
    pos = 0
    while True:
        open_ = sql.find("[[", pos)
        if open_ < 0:
            break
        close = sql.find("]]", open_ + 2)
        if close < 0:
            raise AgentFrontierError.invalid_definition("unclosed [[ in SQL")
        b.append(sql[pos:open_])
        inner = sql[open_ + 2:close]
        if _all_present(inner, vars, files):
            b.append(inner)
        pos = close + 2
    b.append(sql[pos:])
    return "".join(b)


def _all_present(block: str, vars: Vars, files: TempFiles | None) -> bool:
    for p in placeholders(block):
        v = value(p, vars, files)
        if v is None or (isinstance(v, str) and v == "") or (isinstance(v, (list, tuple, set)) and not v):
            return False
    return True


class Template:
    """Namespace mirroring the Java ``Template`` class."""

    Mode = Mode
    STRICT = STRICT
    LENIENT = LENIENT
    placeholders = staticmethod(placeholders)
    render = staticmethod(render)
    render_text = staticmethod(render_text)
    render_json = staticmethod(render_json)
    value = staticmethod(value)
    as_text = staticmethod(as_text)


class SqlRenderer:
    render = staticmethod(render_sql)
    optional_blocks = staticmethod(optional_blocks)
