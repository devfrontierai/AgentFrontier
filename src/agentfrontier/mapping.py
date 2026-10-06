"""Turning what a protocol returned into canonical values: coercion, the JSONPath subset, name filters and the
record mapper (extract, map, collect, filter)."""

from __future__ import annotations

import functools
import re
import threading
from decimal import Decimal
from typing import Any, Iterable, Mapping

from . import json_util as J
from . import template as T
from .errors import AgentFrontierError
from .spec_model import Entity, normalize

# ---------------------------------------------------------------------------------------------------------- Coercion

_TRUE = {"true", "t", "yes", "y", "on", "1"}
_FALSE = {"false", "f", "no", "n", "off", "0"}


def coerce(v: Any, type_: str | None) -> Any:
    """Converts a raw value to a canonical field type. Never raises: bad input gives None."""
    if v is None or type_ is None:
        return v
    if type_ == "string":
        return to_text(v)
    if type_ == "long":
        return to_long(v)
    if type_ == "integer":
        lv = to_long(v)
        return None if lv is None else J.wrap_int32(lv)
    if type_ == "double":
        return to_double(v)
    if type_ == "boolean":
        return to_bool(v)
    if type_ == "list":
        return to_list(v)
    return v


def to_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, Decimal):
        return J.plain_decimal(v)
    if isinstance(v, (list, tuple, set)):
        return ",".join("" if x is None else J.jstr(x) for x in v)
    return J.jstr(v)


def to_long(v: Any) -> int | None:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v if J.LONG_MIN <= v <= J.LONG_MAX else None
    if isinstance(v, float):
        return J.double_to_long(v)
    if isinstance(v, Decimal):
        return J.wrap_int64(int(v)) if v.is_finite() else None
    s = J.jtrim(J.jstr(v))
    if not s:
        return None
    try:
        return J.parse_long(s)
    except ValueError:
        try:
            return J.double_to_long(J.parse_double(s))
        except ValueError:
            return None


def to_double(v: Any) -> float | None:
    if J.is_number(v):
        return float(v)
    s = J.jtrim(J.jstr(v))
    if not s:
        return None
    try:
        return J.parse_double(s)
    except ValueError:
        return None


def to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if J.is_number(v):
        return float(v) != 0
    s = J.jtrim(J.jstr(v)).lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def to_list(v: Any) -> list:
    if isinstance(v, (list, tuple, set)):
        return list(v)
    if isinstance(v, str):
        return [J.jtrim(p) for p in v.split(",") if not J.is_blank(p)]
    return [v]


class Coercion:
    coerce = staticmethod(coerce)
    to_text = staticmethod(to_text)
    to_long = staticmethod(to_long)
    to_double = staticmethod(to_double)
    to_bool = staticmethod(to_bool)
    to_list = staticmethod(to_list)


# ---------------------------------------------------------------------------------------------------------- JsonPath

_PATHS: dict[str, list] = {}
_PATHS_LOCK = threading.Lock()


def _compile_path(path: str) -> list:
    if not path.startswith("$"):
        raise AgentFrontierError.invalid_definition("JSONPath must start with $: " + path)
    steps = []  # (key, index, wildcard)
    i = 1
    n = len(path)
    while i < n:
        c = path[i]
        if c == ".":
            j = i + 1
            if j < n and path[j] == "*":
                steps.append((None, None, True))
                i = j + 1
                continue
            while j < n and path[j] != "." and path[j] != "[":
                j += 1
            if j == i + 1:
                raise AgentFrontierError.invalid_definition("empty name in JSONPath " + path)
            steps.append((path[i + 1:j], None, False))
            i = j
        elif c == "[":
            close = _find_close(path, i)
            inner = J.jtrim(path[i + 1:close])
            if inner == "*":
                steps.append((None, None, True))
            elif len(inner) >= 2 and ((inner[0] == "'" and inner[-1] == "'") or (inner[0] == '"' and inner[-1] == '"')):
                steps.append((inner[1:-1], None, False))
            else:
                try:
                    steps.append((None, J.parse_int(inner), False))
                except ValueError:
                    raise AgentFrontierError.invalid_definition("bad JSONPath index '" + inner + "' in " + path) \
                        from None
            i = close + 1
        else:
            raise AgentFrontierError.invalid_definition("unexpected '" + c + "' in JSONPath " + path)
    return steps


def _find_close(path: str, open_: int) -> int:
    quote = None
    for i in range(open_ + 1, len(path)):
        c = path[i]
        if quote is not None:
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
        elif c == "]":
            return i
    raise AgentFrontierError.invalid_definition("unclosed [ in JSONPath " + path)


def json_path_read(root: Any, path: str) -> list:
    """The JSONPath subset definitions use, over plain trees: ``$``, ``.name``, ``['name']``, ``[3]``, ``[*]``,
    ``.*``."""
    steps = _PATHS.get(path)
    if steps is None:
        steps = _compile_path(path)
        with _PATHS_LOCK:
            _PATHS[path] = steps
    current = [root]
    for key, index, wildcard in steps:
        nxt = []
        for o in current:
            if wildcard:
                if isinstance(o, dict):
                    nxt.extend(o.values())
                elif isinstance(o, list):
                    nxt.extend(o)
            elif index is not None:
                if isinstance(o, list):
                    i = len(o) + index if index < 0 else index
                    if 0 <= i < len(o):
                        nxt.append(o[i])
            elif isinstance(o, dict) and key in o:
                nxt.append(o[key])
        current = nxt
    return current


def json_path_first(root: Any, path: str) -> Any:
    r = json_path_read(root, path)
    return r[0] if r else None


class JsonPath:
    read = staticmethod(json_path_read)
    first = staticmethod(json_path_first)


# ---------------------------------------------------------------------------------------------------------- NameFilter


def _glob(glob: str) -> re.Pattern:
    re_ = []
    for c in J.jtrim(glob):
        if c == "*":
            re_.append(".*")
        elif c == "?":
            re_.append(".")
        else:
            re_.append(re.escape(c))
    return re.compile("".join(re_), re.IGNORECASE | re.DOTALL)


class NameFilter:
    """Include / exclude filter over names with ``*`` and ``?`` wildcards, case-insensitive. An empty include list
    includes everything; excludes always win."""

    def __init__(self, includes: Iterable[str] = (), excludes: Iterable[str] = ()):
        self._includes = [_glob(g) for g in includes]
        self._excludes = [_glob(g) for g in excludes]

    @staticmethod
    def all() -> "NameFilter":
        return NameFilter()

    def accepts(self, name: str | None) -> bool:
        if name is None:
            return True
        for p in self._excludes:
            if p.fullmatch(name):
                return False
        if not self._includes:
            return True
        return any(p.fullmatch(name) for p in self._includes)

    @property
    def is_empty(self) -> bool:
        return not self._includes and not self._excludes


# ------------------------------------------------------------------------------------------------------- RecordMapper


def _as_record(o: Any) -> dict:
    if isinstance(o, dict):
        return dict(o)
    return {"value": o}


def extract(spec: Any, body: Any) -> list[dict]:
    """Record objects from a parsed JSON body."""
    out: list[dict] = []
    if spec is None:
        if isinstance(body, list):
            out.extend(_as_record(x) for x in body)
        elif body is not None:
            out.append(_as_record(body))
        return out
    if isinstance(spec, str):
        for o in json_path_read(body, spec):
            if isinstance(o, list) and not spec.endswith("]"):
                out.extend(_as_record(x) for x in o)
            else:
                out.append(_as_record(o))
        return out
    path = J.text(spec, "path", "$")
    tree = J.get(spec, "tree")
    entries = J.boolean(spec, "entries", False)
    for node in json_path_read(body, path):
        if isinstance(tree, dict):
            if isinstance(node, dict):
                _walk_tree(node, J.text(tree, "children", "children"), J.text(tree, "separator", "."), None, 0, out,
                           [0])
        elif entries:
            if isinstance(node, dict):
                for k, v in node.items():
                    r = dict(v) if isinstance(v, dict) else {"value": "" if v is None else v}
                    r["key"] = J.jstr(k)
                    out.append(r)
        elif isinstance(node, list):
            out.extend(_as_record(x) for x in node)
        else:
            out.append(_as_record(node))
    return out


def _walk_tree(level: dict, children_key: str, sep: str, prefix: str | None, depth: int, out: list,
               position: list) -> None:
    for k, v in level.items():
        name = k if prefix is None else prefix + sep + k
        r: dict = {}
        children = None
        if isinstance(v, dict):
            for fk, fv in v.items():
                if fk == children_key:
                    children = fv
                elif not isinstance(fv, (dict, list)):
                    r[J.jstr(fk)] = fv
        r["name"] = name
        r["key"] = k
        r["parent"] = prefix
        r["depth"] = depth
        position[0] += 1
        r["position"] = position[0]
        out.append(r)
        if isinstance(children, dict):
            _walk_tree(children, children_key, sep, name, depth + 1, out, position)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def map_rows(op: Mapping[str, Any], rows: list[dict], entity: Entity | None, levels: list[str]) -> list[dict]:
    """Maps raw rows to canonical values (``entity`` None maps flat)."""
    canonical: dict[str, str] = {}
    types: dict[str, str] = {}
    if entity is not None:
        for f, t in entity.fields.items():
            canonical[normalize(f)] = f
            types[f] = t
        for lv in levels:
            for f in (lv, "ref" + _cap(lv), "upstream" + _cap(lv)):
                canonical.setdefault(normalize(f), f)
                types.setdefault(f, "string")
    fields = op.get("fields")
    set_ = op.get("set")
    drop_extras = J.text(op, "extras") == "drop"
    out = []
    for row in rows:
        values: dict[str, Any] = {}
        props: dict[str, Any] = {}
        consumed: set[str] = set()
        if isinstance(fields, dict):
            for target, spec in fields.items():
                if spec is None or spec is False:
                    consumed.add(target.lower())
                    continue
                v = _evaluate(spec, row, consumed)
                c = None if entity is None else canonical.get(normalize(target))
                if entity is None:
                    values[target] = v
                elif c is not None:
                    values[c] = v
                else:
                    props[target] = v
        for k, x in row.items():
            if k.lower() in consumed:
                continue
            if entity is None:
                if values.get(k) is None:
                    values[k] = x
                continue
            c = canonical.get(normalize(k))
            if c is not None:
                if values.get(c) is None:
                    values[c] = x
            elif not drop_extras:
                ek = extra_key(k)
                if props.get(ek) is None:
                    props[ek] = x
        if isinstance(set_, dict):
            for k, x in set_.items():
                c = k if entity is None else canonical.get(normalize(k))
                if c is not None:
                    values[c] = J.deep_copy(x)
                else:
                    props[k] = J.deep_copy(x)
        if entity is not None:
            for k in list(values.keys()):
                raw = values[k]
                if isinstance(raw, str) and raw == "":
                    raw = None
                v = coerce(raw, types.get(k))
                en = entity.enums.get(k)
                if en is not None and isinstance(v, str):
                    v = en.normalize(v)
                values[k] = v
            values = {k: x for k, x in values.items() if x is not None}
            props = {k: x for k, x in props.items() if x is not None}
            if props:
                values["properties"] = props
        out.append(values)
    return out


def extra_key(k: str) -> str:
    """Upper case column names (Oracle, Snowflake) become lower case; anything else is kept as written."""
    letter = False
    for c in k:
        if c.isalpha():
            letter = True
            if c.islower():
                return k
    return k.lower() if letter else k


_ELSE = object()


def _evaluate(spec: Any, row: dict, consumed: set[str]) -> Any:
    if isinstance(spec, str):
        return raw(row, spec, consumed)
    if not isinstance(spec, dict):
        return J.deep_copy(spec)
    if "value" in spec:
        v = J.deep_copy(spec["value"])
    elif "template" in spec:
        v = T.render(J.as_text(spec["template"]), T.Vars.of(row), T.LENIENT)
    elif "from" in spec:
        v = raw(row, J.as_text(spec["from"]), consumed)
    else:
        raise AgentFrontierError.invalid_definition("field mapping needs 'from', 'value' or 'template': "
                                                    + J.write(spec))
    values = spec.get("values")
    if isinstance(values, dict) and v is not None:
        key = J.jstr(v)
        hit = values[key] if key in values else _ELSE
        if hit is _ELSE:
            for ek, ev in values.items():
                if J.equals_ignore_case(ek, key):
                    hit = ev
                    break
        if hit is not _ELSE:
            v = J.deep_copy(hit)
        elif "else" in spec:
            v = J.deep_copy(spec["else"])
    if v is None and "default" in spec:
        v = J.deep_copy(spec["default"])
    if isinstance(v, str) and "split" in spec:
        sep = J.as_text(spec["split"])
        parts = list(v) if sep == "" else v.split(sep)
        v = [J.jtrim(p) for p in parts if not J.is_blank(p)]
    if "as" in spec:
        v = coerce(v, J.as_text(spec["as"]))
    return v


def raw(row: dict, ref: str, consumed: set[str]) -> Any:
    """Value of a column / key: JSONPath when it starts with $, else exact key, case-insensitive key, dotted path."""
    if ref.startswith("$"):
        return json_path_first(row, ref)
    consumed.add(ref.lower())
    if ref in row:
        return row[ref]
    for k, v in row.items():
        if J.equals_ignore_case(k, ref):
            return v
    return T.lookup(row, ref) if "." in ref else None


def _hashable(x: Any) -> Any:
    if isinstance(x, bool):
        return ("b", x)
    if isinstance(x, dict):
        return ("d", tuple((k, _hashable(v)) for k, v in x.items()))
    if isinstance(x, (list, tuple)):
        return ("l", tuple(_hashable(v) for v in x))
    return x


def collect(spec: Any, records: list[dict]) -> list[dict]:
    """Rows with the same ``by`` key merge into one record, ``lists`` fields gathered in ``orderBy`` order."""
    if not isinstance(spec, dict) or not records:
        return records
    by = J.strings(spec.get("by"))
    lists = J.strings(spec.get("lists"))
    order_by = J.text(spec, "orderBy")
    groups: dict = {}
    for r in records:
        key = tuple(_hashable(get(r, b)) for b in by)
        groups.setdefault(key, []).append(r)
    out = []
    for g in groups.values():
        if order_by is not None:
            g = sorted(g, key=functools.cmp_to_key(lambda a, b: _compare(get(a, order_by), get(b, order_by))))
        base = dict(g[0])
        if isinstance(base.get("properties"), dict):
            base["properties"] = dict(base["properties"])
        for f in lists:
            all_: list = []
            for r in g:
                v = get(r, f)
                if isinstance(v, (list, tuple, set)):
                    all_.extend(x for x in v if x is not None)
                elif v is not None:
                    all_.append(v)
            _put(base, f, all_)
        if order_by is not None:
            base.pop(order_by, None)
            p = base.get("properties")
            if isinstance(p, dict):
                p.pop(order_by, None)
                if not p:
                    base.pop("properties", None)
        out.append(base)
    return out


def get(r: dict, field: str) -> Any:
    v = r.get(field)
    if v is None and isinstance(r.get("properties"), dict):
        v = r["properties"].get(field)
    return v


def _put(r: dict, field: str, v: Any) -> None:
    p = r.get("properties")
    if field in r or not (isinstance(p, dict) and field in p):
        r[field] = v
    else:
        p[field] = v


def _compare(a: Any, b: Any) -> int:
    if a is None or b is None:
        if a is None:
            return 0 if b is None else -1
        return 1
    if J.is_number(a) and J.is_number(b):
        x, y = float(a), float(b)
        return -1 if x < y else (1 if x > y else 0)
    sa, sb = J.jstr(a), J.jstr(b)
    return -1 if sa < sb else (1 if sa > sb else 0)


def filter_records(spec: Any, records: list[dict], options: Mapping[str, Any]) -> list[dict]:
    """Applies ``filter``; ``options`` decide ``unless``."""
    if not isinstance(spec, dict):
        return records
    out = records
    for fld, cfg in spec.items():
        unless = J.text(cfg, "unless")
        if unless is not None and to_bool(options.get(unless, False)) is True:
            continue
        f = NameFilter(J.strings(J.get(cfg, "include")), J.strings(J.get(cfg, "exclude")))
        out = [r for r in out if f.accepts(None if get(r, fld) is None else J.jstr(get(r, fld)))]
    return out


class RecordMapper:
    extract = staticmethod(extract)
    map = staticmethod(map_rows)
    collect = staticmethod(collect)
    filter = staticmethod(filter_records)
    extra_key = staticmethod(extra_key)
