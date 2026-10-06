"""JSON and text helpers that reproduce the Java reference's behaviour exactly where it shows in output.

The Java edition uses Jackson and ``String.valueOf``; records, cursors and CLI output must be byte-for-byte the same
in every implementation, so this module provides:

* :func:`write` / :func:`pretty`: Jackson's compact and default-pretty-printer JSON output;
* :func:`jstr`: Java's ``String.valueOf`` (``true``, ``5.0``, ``[a, b]``, ``{k=v}``);
* :func:`text`, :func:`integer`, :func:`boolean`, :func:`strings`, :func:`lines`: the ``Json`` helpers that read
  definition JSON (``asText`` semantics);
* :func:`jtrim`, :func:`is_blank`, :func:`url_encode` and friends.
"""

from __future__ import annotations

import copy
import json
import math
import re
from decimal import Decimal
from typing import Any, Iterable

from .errors import AgentFrontierError

_MISSING = object()

# ------------------------------------------------------------------------------------------------------- text basics


def jtrim(s: str) -> str:
    """Java ``String.trim()``: strips characters up to and including U+0020 from both ends."""
    i, j = 0, len(s)
    while i < j and s[i] <= " ":
        i += 1
    while j > i and s[j - 1] <= " ":
        j -= 1
    return s[i:j]


_NOT_JAVA_WS = "\x85\xa0  "


def _java_ws(c: str) -> bool:
    return c.isspace() and c not in _NOT_JAVA_WS


def is_blank(s: str) -> bool:
    """Java ``String.isBlank()`` (``Character.isWhitespace``)."""
    return all(_java_ws(c) for c in s)


def strip_leading(s: str) -> str:
    i = 0
    while i < len(s) and _java_ws(s[i]):
        i += 1
    return s[i:]


def strip_trailing(s: str) -> str:
    j = len(s)
    while j > 0 and _java_ws(s[j - 1]):
        j -= 1
    return s[:j]


def equals_ignore_case(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        if x.upper() == y.upper() or x.lower() == y.lower():
            continue
        return False
    return True


def is_number(v: Any) -> bool:
    """A Java ``Number``: int, float or Decimal, but not bool."""
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def java_double_str(d: float) -> str:
    """Java ``Double.toString``."""
    if d != d:
        return "NaN"
    if d == math.inf:
        return "Infinity"
    if d == -math.inf:
        return "-Infinity"
    if d == 0.0:
        return "-0.0" if math.copysign(1.0, d) < 0 else "0.0"
    sign = "-" if d < 0 else ""
    t = Decimal(repr(abs(d))).as_tuple()
    digits = "".join(str(x) for x in t.digits).rstrip("0") or "0"
    # exponent of the first digit (scientific)
    sci = len("".join(str(x) for x in t.digits)) - 1 + t.exponent
    a = abs(d)
    if 1e-3 <= a < 1e7:
        if sci >= 0:
            int_part = digits[: sci + 1].ljust(sci + 1, "0")
            frac = digits[sci + 1:] or "0"
        else:
            int_part = "0"
            frac = "0" * (-sci - 1) + digits
        return sign + int_part + "." + frac
    mant = digits[0] + "." + (digits[1:] or "0")
    return sign + mant + "E" + str(sci)


def java_decimal_str(d: Decimal) -> str:
    """``BigDecimal.toString`` (scientific only for small adjusted exponents or positive exponents)."""
    return str(d)


def plain_decimal(d: Decimal) -> str:
    """``BigDecimal.toPlainString``."""
    return format(d, "f")


def jstr(v: Any) -> str:
    """Java ``String.valueOf`` for the plain values the SDK handles."""
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return java_double_str(v)
    if isinstance(v, Decimal):
        return java_decimal_str(v)
    if isinstance(v, (list, tuple, set, frozenset)):
        return "[" + ", ".join(jstr(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(jstr(k) + "=" + jstr(x) for k, x in v.items()) + "}"
    return str(v)


def jlist(items: Iterable[Any]) -> str:
    """Java ``List.toString``."""
    return "[" + ", ".join(jstr(x) for x in items) + "]"


_URL_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-*_")


def url_encode(s: str) -> str:
    """``URLEncoder.encode(s, UTF-8)``: form encoding, space as ``+``."""
    out = []
    for b in s.encode("utf-8", errors="replace"):
        if b in _URL_SAFE:
            out.append(chr(b))
        elif b == 0x20:
            out.append("+")
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def url_encode20(s: str) -> str:
    """URL encoding used for query strings and the ``url`` filter: form encoding with ``%20`` for spaces."""
    return url_encode(s).replace("+", "%20")


_JLONG_RE = re.compile(r"[+-]?[0-9]+", re.ASCII)
_JDOUBLE_RE = re.compile(r"[+-]?(NaN|Infinity|(([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?))[fFdD]?", re.ASCII)

LONG_MIN = -(2 ** 63)
LONG_MAX = 2 ** 63 - 1


def parse_long(s: str) -> int:
    """``Long.parseLong``; raises ValueError like NumberFormatException."""
    if not _JLONG_RE.fullmatch(s):
        raise ValueError("For input string: \"" + s + "\"")
    v = int(s)
    if v < LONG_MIN or v > LONG_MAX:
        raise ValueError("For input string: \"" + s + "\"")
    return v


def parse_int(s: str) -> int:
    """``Integer.parseInt``."""
    if not _JLONG_RE.fullmatch(s):
        raise ValueError("For input string: \"" + s + "\"")
    v = int(s)
    if v < -(2 ** 31) or v > 2 ** 31 - 1:
        raise ValueError("For input string: \"" + s + "\"")
    return v


def parse_double(s: str) -> float:
    """``Double.parseDouble`` (decimal forms; leading and trailing whitespace allowed)."""
    t = jtrim(s)
    m = _JDOUBLE_RE.fullmatch(t)
    if not m:
        raise ValueError("For input string: \"" + s + "\"")
    body = t
    if body and body[-1] in "fFdD" and "Infinity" not in body and "NaN" not in body:
        body = body[:-1]
    body = body.replace("Infinity", "inf").replace("NaN", "nan")
    return float(body)


def double_to_long(d: float) -> int:
    """Java ``(long) double``: truncation, NaN to 0, saturation."""
    if d != d:
        return 0
    if d >= 9.223372036854775807e18:
        return LONG_MAX
    if d <= -9.223372036854775808e18:
        return LONG_MIN
    return int(d)


def wrap_int32(v: int) -> int:
    """Java ``(int) long``."""
    return ((v + 2 ** 31) % 2 ** 32) - 2 ** 31


def wrap_int64(v: int) -> int:
    return ((v + 2 ** 63) % 2 ** 64) - 2 ** 63


# ------------------------------------------------------------------------------------------------------- JSON output

_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\f": "\\f", "\n": "\\n", "\r": "\\r"}


def _quote(s: str) -> str:
    out = ['"']
    for c in s:
        e = _ESC.get(c)
        if e is not None:
            out.append(e)
        elif c < " ":
            out.append("\\u%04X" % ord(c))
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (math.inf, -math.inf):
            return _quote(java_double_str(v))
        return java_double_str(v)
    if isinstance(v, Decimal):
        return java_decimal_str(v)
    if isinstance(v, str):
        return _quote(v)
    return _quote(str(v))


def _plain(v: Any) -> Any:
    to_json = getattr(v, "to_json", None)
    if callable(to_json) and not isinstance(v, (dict, list, str)):
        return to_json()
    to_dict = getattr(v, "to_dict", None)
    if callable(to_dict) and not isinstance(v, (dict, list, str)):
        return to_dict()
    return v


def write(v: Any) -> str:
    """Compact JSON exactly as Jackson writes it (``{"a":1,"b":[1,2]}``)."""
    v = _plain(v)
    if isinstance(v, dict):
        return "{" + ",".join(_quote(str(k)) + ":" + write(x) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple, set, frozenset)):
        return "[" + ",".join(write(x) for x in v) + "]"
    return _scalar(v)


def pretty(v: Any) -> str:
    """JSON as Jackson's default pretty printer writes it (2-space objects, inline arrays)."""
    out: list[str] = []
    _pretty(_plain(v), 0, out)
    return "".join(out)


def _pretty(v: Any, nesting: int, out: list[str]) -> None:
    v = _plain(v)
    if isinstance(v, dict):
        out.append("{")
        if not v:
            out.append(" }")
            return
        first = True
        for k, x in v.items():
            out.append("" if first else ",")
            first = False
            out.append("\n" + "  " * (nesting + 1))
            out.append(_quote(str(k)) + " : ")
            _pretty(x, nesting + 1, out)
        out.append("\n" + "  " * nesting + "}")
        return
    if isinstance(v, (list, tuple, set, frozenset)):
        out.append("[")
        if not v:
            out.append(" ]")
            return
        first = True
        for x in v:
            out.append(" " if first else ", ")
            first = False
            _pretty(x, nesting, out)
        out.append(" ]")
        return
    out.append(_scalar(v))


def canonical(v: Any) -> str:
    """Compact JSON with keys sorted at every level (for comparing records as sets)."""
    v = _plain(v)
    if isinstance(v, dict):
        return "{" + ",".join(_quote(str(k)) + ":" + canonical(v[k]) for k in sorted(v, key=str)) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(canonical(x) for x in v) + "]"
    return _scalar(v)


# ------------------------------------------------------------------------------------------------------- JSON input


def parse(text_: str, what: str | None = None) -> Any:
    """Parses the first JSON value of a text (Jackson ignores trailing content)."""
    try:
        return loads_first(text_)
    except ValueError as e:
        if what is None:
            raise AgentFrontierError.invalid_config("invalid JSON: " + str(e)) from e
        raise AgentFrontierError.invalid_definition("cannot read " + what + ": " + str(e)) from e


def loads_first(text_: str) -> Any:
    """First JSON value in a text; ``None`` for an empty text. Raises ValueError when invalid."""
    s = text_.lstrip(" \t\r\n﻿")
    if not s:
        return None
    v, _ = json.JSONDecoder().raw_decode(s)
    return v


def deep_copy(v: Any) -> Any:
    return copy.deepcopy(v)


def as_text(node: Any) -> str:
    """Jackson ``JsonNode.asText()``: scalars as text, containers as empty text, null as ``"null"``."""
    if node is None:
        return "null"
    if isinstance(node, (dict, list)):
        return ""
    return jstr(node)


def get(node: Any, field: str) -> Any:
    """``node.get(field)``: the value or None (missing or JSON null)."""
    if isinstance(node, dict):
        return node.get(field)
    return None


def has(node: Any, field: str) -> bool:
    return isinstance(node, dict) and field in node


def text(node: Any, field: str, fallback: str | None = None) -> str | None:
    """``Json.text``: the field as text, the fallback when it is missing or null."""
    v = get(node, field)
    if v is None:
        return fallback
    return as_text(v)


def boolean(node: Any, field: str, fallback: bool) -> bool:
    v = get(node, field)
    if v is None:
        return fallback
    if isinstance(v, bool):
        return v
    return equals_ignore_case(as_text(v), "true")


def as_int(v: Any) -> int:
    """Jackson ``asInt`` for a number node."""
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, float):
        return wrap_int32(double_to_long(v))
    if isinstance(v, Decimal):
        return wrap_int32(int(v))
    return wrap_int32(int(v))


def integer(node: Any, field: str, fallback: int) -> int:
    v = get(node, field)
    if v is None:
        return fallback
    if is_number(v):
        return as_int(v)
    return parse_int(jtrim(as_text(v)))


def as_double(node: Any, fallback: float = 0.0) -> float:
    """Jackson ``asDouble(default)``."""
    if node is None or isinstance(node, (dict, list)):
        return fallback
    if isinstance(node, bool):
        return 1.0 if node else 0.0
    if is_number(node):
        return float(node)
    try:
        return parse_double(str(node))
    except ValueError:
        return fallback


def lines(node: Any) -> str | None:
    """A SQL statement written either as one string or as an array of lines."""
    if node is None:
        return None
    if isinstance(node, list):
        return "\n".join(as_text(x) for x in node)
    return as_text(node)


def strings(node: Any) -> list[str]:
    """Strings of an array, or a comma separated text; empty for null."""
    out: list[str] = []
    if node is None:
        return out
    if isinstance(node, list):
        for c in node:
            if c is not None:
                out.append(as_text(c))
        return out
    for s in as_text(node).split(","):
        if not is_blank(s):
            out.append(jtrim(s))
    return out


def to_map(node: Any) -> dict:
    """A JSON object as a new dict; empty for null or non-objects."""
    if not isinstance(node, dict):
        return {}
    return copy.deepcopy(node)
