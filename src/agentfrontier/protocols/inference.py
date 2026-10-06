"""Schema inference from file content and path globs. The rules are part of the spec so that every implementation
infers the same schema from the same file:

* CSV / TSV: first row is the header (blank names become ``column_N``). A column's type is the first of boolean,
  integer, double, date, timestamp that every non-empty sampled value matches, else string; an all-empty column is
  string. A column is nullable when any sampled row has it empty or missing.
* JSON / JSON Lines: records are the array items, the lines, or the single object. Nested objects become dotted
  fields with ``parent`` set and type object; arrays are type array and are not descended into. Types merge across
  records: equal stays, integer with double is double, anything else is mixed. A field is nullable when it is null or
  missing in any sampled record.

The CSV reader is a direct port of the Java edition's RFC 4180 reader (rather than the ``csv`` module) so that edge
cases (text after a closing quote, a lone ``\\r``) split exactly the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, TextIO

from .. import json_util as J

_INTEGER = re.compile(r"[+-]?[0-9]+", re.ASCII)
_DOUBLE = re.compile(r"[+-]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,9})?)?"
                        r"(Z|[+-][0-9]{2}:?[0-9]{2})?", re.ASCII)
_ORDER = ["boolean", "integer", "double", "date", "timestamp"]
BOM = "﻿"


@dataclass(frozen=True)
class Field:
    name: str
    ordinal: int
    data_type: str
    nullable: bool
    parent: str | None


@dataclass(frozen=True)
class Result:
    fields: list[Field]
    rows: int
    complete: bool


def file_format(file_name: str) -> str:
    n = file_name.lower()
    if n.endswith(".csv"):
        return "csv"
    if n.endswith(".tsv") or n.endswith(".tab"):
        return "tsv"
    if n.endswith(".jsonl") or n.endswith(".ndjson"):
        return "jsonl"
    if n.endswith(".json"):
        return "json"
    if n.endswith(".parquet"):
        return "parquet"
    if n.endswith(".avro"):
        return "avro"
    dot = n.rfind(".")
    return "unknown" if dot < 0 else n[dot + 1:]


# ------------------------------------------------------------------------------------------------------------- CSV


class CsvReader:
    """RFC 4180 reader: quoted fields, doubled quotes, delimiters and newlines inside quotes. Reads from a text
    stream opened with ``newline=''``."""

    def __init__(self, stream: TextIO, delimiter: str):
        self._in = stream
        self._delimiter = delimiter
        self._peeked: str | None | int = -2

    def _read(self) -> str | None:
        if self._peeked != -2:
            c = self._peeked
            self._peeked = -2
            return c  # type: ignore[return-value]
        c = self._in.read(1)
        return c if c else None

    def _peek(self) -> str | None:
        if self._peeked == -2:
            c = self._in.read(1)
            self._peeked = c if c else None
        return self._peeked  # type: ignore[return-value]

    def next(self) -> list[str] | None:
        c = self._read()
        if c is None:
            return None
        fields: list[str] = []
        f: list[str] = []
        quoted = False
        while True:
            if quoted:
                if c is None:
                    fields.append("".join(f))
                    return fields
                if c == '"':
                    if self._peek() == '"':
                        self._read()
                        f.append('"')
                    else:
                        quoted = False
                else:
                    f.append(c)
            else:
                if c is None or c == "\n":
                    fields.append("".join(f))
                    return fields
                if c == "\r":
                    if self._peek() == "\n":
                        self._read()
                    fields.append("".join(f))
                    return fields
                if c == self._delimiter:
                    fields.append("".join(f))
                    f = []
                elif c == '"' and not f:
                    quoted = True
                else:
                    f.append(c)
            c = self._read()


def delimited(stream: TextIO, delimiter: str, sample_rows: int) -> Result:
    csv = CsvReader(stream, delimiter)
    header = csv.next()
    if header is None:
        return Result([], 0, True)
    n = len(header)
    samples: list[list[str]] = [[] for _ in range(n)]
    nullable = [False] * n
    rows = 0
    complete = True
    while True:
        rec = csv.next()
        if rec is None:
            break
        if rows >= sample_rows:
            complete = False
            break
        if len(rec) == 1 and rec[0] == "":
            continue  # blank line
        rows += 1
        for i in range(n):
            v = J.jtrim(rec[i]) if i < len(rec) else ""
            if not v:
                nullable[i] = True
            else:
                samples[i].append(v)
    fields = []
    for i in range(n):
        name = J.jtrim(header[i])
        if i == 0 and name.startswith(BOM):
            name = name[1:]
        if not name:
            name = "column_" + str(i + 1)
        fields.append(Field(name, i + 1, text_type(samples[i]), nullable[i], None))
    return Result(fields, rows, complete)


def text_type(values: list[str]) -> str:
    if not values:
        return "string"
    for t in _ORDER:
        if all(_matches(t, v) for v in values):
            return t
    return "string"


def _matches(t: str, v: str) -> bool:
    if t == "boolean":
        return J.equals_ignore_case(v, "true") or J.equals_ignore_case(v, "false")
    if t == "integer":
        return _INTEGER.fullmatch(v) is not None
    if t == "double":
        return _DOUBLE.fullmatch(v) is not None
    if t == "date":
        return _DATE.fullmatch(v) is not None
    if t == "timestamp":
        return _TIMESTAMP.fullmatch(v) is not None
    return True


# ------------------------------------------------------------------------------------------------------------ JSON


class _State:
    __slots__ = ("type", "parent", "null_seen", "seen_in", "last_record")

    def __init__(self) -> None:
        self.type: str | None = None
        self.parent: str | None = None
        self.null_seen = False
        self.seen_in = 0
        self.last_record = -1


def json_schema(records: Iterable[Any], complete: bool) -> Result:
    fields: dict[str, _State] = {}
    index = 0
    for r in records:
        if isinstance(r, dict):
            _visit(r, None, fields, index)
            index += 1
    total = index
    out = []
    for ordinal, (name, s) in enumerate(fields.items(), start=1):
        out.append(Field(name, ordinal, "string" if s.type is None else s.type, s.null_seen or s.seen_in < total,
                         s.parent))
    return Result(out, total, complete)


def _visit(obj: dict, prefix: str | None, fields: dict[str, _State], record: int) -> None:
    for k, v in obj.items():
        name = k if prefix is None else prefix + "." + k
        s = fields.get(name)
        if s is None:
            s = _State()
            fields[name] = s
        s.parent = prefix
        if s.last_record != record:
            s.seen_in += 1
            s.last_record = record
        t = json_type(v)
        if t is None:
            s.null_seen = True
        else:
            s.type = merge(s.type, t)
        if isinstance(v, dict):
            _visit(v, name, fields, record)


def json_type(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "double"
    if isinstance(v, str):
        return "string"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return "string"


def merge(a: str | None, b: str) -> str:
    if a is None or a == b:
        return b
    if {a, b} == {"integer", "double"}:
        return "double"
    return "mixed"


def json_records(stream: TextIO, lines: bool, limit: int) -> tuple[list[dict], bool]:
    """Records of a JSON document (array items or the single object) or of JSON Lines, up to ``limit``; and whether
    the sample covered the whole file. Raises ValueError for invalid JSON."""
    out: list[dict] = []
    complete = True
    if lines:
        for line in stream:
            line = line.rstrip("\r\n")
            if J.is_blank(line):
                continue
            if len(out) >= limit:
                complete = False
                break
            n = J.loads_first(line)
            if isinstance(n, dict):
                out.append(n)
        return out, complete
    doc = J.loads_first(stream.read())
    if isinstance(doc, list):
        for n in doc:
            if len(out) >= limit:
                complete = False
                break
            if isinstance(n, dict):
                out.append(n)
    elif isinstance(doc, dict):
        out.append(doc)
    return out, complete


# --------------------------------------------------------------------------------------------------------- PathGlob


def compile_glob(glob: str) -> re.Pattern:
    g = J.jtrim(glob).replace("\\", "/")
    re_ = []
    i = 0
    while i < len(g):
        c = g[i]
        if c == "*":
            if i + 1 < len(g) and g[i + 1] == "*":
                if i + 2 < len(g) and g[i + 2] == "/":
                    re_.append("(?:.*/)?")
                    i += 3
                else:
                    re_.append(".*")
                    i += 2
            else:
                re_.append("[^/]*")
                i += 1
        elif c == "?":
            re_.append("[^/]")
            i += 1
        else:
            re_.append(re.escape(c))
            i += 1
    return re.compile("".join(re_), re.IGNORECASE)


class PathGlob:
    """Glob patterns over relative paths with forward slashes, case-insensitive: ``**`` matches any number of
    folders, ``*`` anything within one name, ``?`` one character."""

    def __init__(self, include: Iterable[str] = (), exclude: Iterable[str] = ()):
        self._include = [compile_glob(g) for g in include]
        self._exclude = [compile_glob(g) for g in exclude]

    def accepts(self, relative_path: str) -> bool:
        for p in self._exclude:
            if p.fullmatch(relative_path):
                return False
        if not self._include:
            return True
        return any(p.fullmatch(relative_path) for p in self._include)
