"""Source definitions: the registry (built-ins plus user directories, overlays, ``extends``), version ranges,
``when`` conditions, the JSON merge and the resolved :class:`SourceDefinition` view."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import json_util as J
from .errors import AgentFrontierError
from .spec_model import AuthMethod, FieldSpec, SpecResources

# ------------------------------------------------------------------------------------------------------ VersionRange

_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_COMPARATOR = re.compile(r"(<=|>=|!=|<|>|=)?\s*([0-9][0-9.x*]*)")
_INT_MAX = 2 ** 31 - 1


def _components(v: str) -> list[int]:
    parts = v.split(".")
    while len(parts) > 1 and parts[-1] == "":  # Java's split drops trailing empty strings
        parts.pop()
    out = []
    for p in parts:
        try:
            out.append(J.parse_int(p))
        except ValueError:
            out.append(_INT_MAX)  # absurdly long component; compares as "very new"
    return out


class VersionRange:
    """Version conditions as written in ``when.version``: comparators (``<12``, ``>=8.0.13``), several separated by
    spaces must all hold, alternatives separated by ``||``, a bare version is a prefix match (``9.6``, ``9.x``)."""

    def __init__(self, text: str, alternatives: list[list[tuple[str, list[int]]]]):
        self.text = text
        self._alternatives = alternatives

    @staticmethod
    def parse(text: str) -> "VersionRange":
        alts = []
        for alt in re.split(r"\|\|", text):
            rest = J.jtrim(alt)
            clauses = []
            pos = 0
            while pos < len(rest):
                if rest[pos].isspace():
                    pos += 1
                    continue
                m = _COMPARATOR.search(rest, pos)
                if m is None or m.start() != pos:
                    raise AgentFrontierError.invalid_definition("bad version condition '" + text + "'")
                op = m.group(1) if m.group(1) is not None else "~"
                v = re.sub(r"\.?[x*]+$", "", m.group(2))
                clauses.append((op, _components(v if v else "0")))
                pos = m.end()
            if not clauses:
                raise AgentFrontierError.invalid_definition("empty version condition in '" + text + "'")
            alts.append(clauses)
        return VersionRange(text, alts)

    def matches(self, version: str | None) -> bool:
        v = VersionRange.extract(version)
        if v is None:
            return False
        for alt in self._alternatives:
            if all(_holds(op, c, v) for op, c in alt):
                return True
        return False

    @staticmethod
    def compare(a: list[int], b: list[int]) -> int:
        n = max(len(a), len(b))
        for i in range(n):
            x = a[i] if i < len(a) else 0
            y = b[i] if i < len(b) else 0
            if x != y:
                return -1 if x < y else 1
        return 0

    @staticmethod
    def extract(text: str | None) -> list[int] | None:
        """The first dotted number in the text, as components; None when there is none."""
        if text is None:
            return None
        m = _NUMBER.search(text)
        return _components(m.group()) if m else None

    def __str__(self) -> str:
        return self.text


def _holds(op: str, c: list[int], v: list[int]) -> bool:
    cmp = VersionRange.compare(v, c)
    if op == "<":
        return cmp < 0
    if op == "<=":
        return cmp <= 0
    if op == ">":
        return cmp > 0
    if op == ">=":
        return cmp >= 0
    if op == "=":
        return cmp == 0
    if op == "!=":
        return cmp != 0
    for i, p in enumerate(c):
        a = v[i] if i < len(v) else 0
        if a != p:
            return False
    return True


# --------------------------------------------------------------------------------------------------------- Condition


@dataclass(frozen=True)
class Facts:
    """What a ``when`` condition is evaluated against."""

    version: str | None
    variant: str | None
    options: Mapping[str, Any] = field(default_factory=dict)
    properties: Mapping[str, Any] = field(default_factory=dict)


def condition_holds(when: Any, facts: Facts) -> bool:
    """Every key present must hold: ``version`` (range), ``variant`` (id or list), ``option`` / ``property``
    (name to expected value, compared as text)."""
    if not isinstance(when, dict):
        return True
    v = when.get("version")
    if v is not None and not VersionRange.parse(J.as_text(v)).matches(facts.version):
        return False
    variant = when.get("variant")
    if variant is not None:
        allowed = J.strings(variant)
        if facts.variant is None or not any(J.equals_ignore_case(a, facts.variant) for a in allowed):
            return False
    return _matches_all(when.get("option"), facts.options) and _matches_all(when.get("property"), facts.properties)


def _matches_all(expected: Any, actual: Mapping[str, Any]) -> bool:
    if not isinstance(expected, dict):
        return True
    for k, e in expected.items():
        a = actual.get(k)
        want = None if e is None else J.as_text(e)
        if want != (None if a is None else J.jstr(a)):
            return False
    return True


# ---------------------------------------------------------------------------------------------------------- JsonMerge


def json_merge(base: dict, patch: Any) -> dict:
    """Objects merge key by key, recursively; a null in the patch removes the key; arrays and scalars in the patch
    replace what was there. Returns a new object; neither input is modified."""
    out = copy.deepcopy(base)
    _apply(out, patch)
    return out


def _apply(target: dict, patch: Any) -> None:
    if not isinstance(patch, dict):
        return
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _apply(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


# -------------------------------------------------------------------------------------------------------------- Level


@dataclass(frozen=True)
class Level:
    """One level of a source's hierarchy.

    ``list``: operation that lists this level's containers (None for a fixed level). ``value``: template for the
    configured value. ``select``: connection property holding the user's selection. ``connect``: connection property
    this level's value replaces when the source needs one connection per container. ``exclude``: names always left
    out.
    """

    level: str | None
    label: str | None = None
    list: str | None = None
    value: str | None = None
    select: str | None = None
    connect: str | None = None
    exclude: tuple = ()

    def __post_init__(self) -> None:
        if self.label is None:
            object.__setattr__(self, "label", self.level)
        object.__setattr__(self, "exclude", tuple(self.exclude or ()))

    @staticmethod
    def from_json(n: Any) -> "Level":
        return Level(J.text(n, "level"), J.text(n, "label"), J.text(n, "list"), J.text(n, "value"),
                     J.text(n, "select"), J.text(n, "connect"), tuple(J.strings(J.get(n, "exclude"))))

    def _cap(self) -> str:
        return self.level[:1].upper() + self.level[1:]

    @property
    def include_option(self) -> str:
        """``includeSchemas`` for level ``schema``."""
        return "include" + self._cap() + "s"

    @property
    def exclude_option(self) -> str:
        return "exclude" + self._cap() + "s"

    def to_dict(self) -> dict:
        return {"level": self.level, "label": self.label, "list": self.list, "value": self.value,
                "select": self.select, "connect": self.connect, "exclude": list(self.exclude)}


# --------------------------------------------------------------------------------------------------- SourceDefinition


class SourceDefinition:
    """A resolved source definition: ``extends`` applied, external overlay files attached. After a session detects
    the version, :meth:`effective` returns the definition with matching overlays applied and operations whose
    ``when`` does not hold removed. Treat as immutable; :meth:`json` returns a copy."""

    def __init__(self, doc: dict):
        self._json = doc

    def json(self) -> dict:
        return copy.deepcopy(self._json)

    @property
    def raw(self) -> dict:
        """The underlying JSON (do not modify)."""
        return self._json

    @property
    def id(self) -> str | None:
        return J.text(self._json, "id")

    @property
    def name(self) -> str | None:
        return J.text(self._json, "name", self.id)

    @property
    def protocol(self) -> str | None:
        return J.text(self._json, "protocol")

    @property
    def category(self) -> str | None:
        return J.text(self._json, "category")

    @property
    def status(self) -> str:
        return J.text(self._json, "status", "experimental")

    @property
    def description(self) -> str | None:
        return J.text(self._json, "description")

    @property
    def dialect(self) -> str | None:
        return J.text(self._json, "dialect")

    @property
    def aliases(self) -> list[str]:
        return J.strings(self._json.get("aliases"))

    @property
    def properties(self) -> list[FieldSpec]:
        return FieldSpec.list_from_json(self._json.get("properties"))

    @property
    def options(self) -> list[FieldSpec]:
        return FieldSpec.list_from_json(self._json.get("options"))

    @property
    def auth(self) -> list[str]:
        """Auth method ids, the first one being the default."""
        return J.strings(self._json.get("auth"))

    @property
    def auth_methods(self) -> list[AuthMethod]:
        """Auth methods declared by the definition itself, in addition to the catalog."""
        a = self._json.get("authMethods")
        return [AuthMethod.from_json(n) for n in a] if isinstance(a, list) else []

    def connection(self, key: str) -> Any:
        """The protocol-specific connection block (``jdbc``, ``python``, ``http``, ``files``); None when absent."""
        c = self._json.get("connection")
        return c.get(key) if isinstance(c, dict) else None

    @property
    def session(self) -> dict:
        s = self._json.get("session")
        return s if isinstance(s, dict) else {}

    @property
    def version(self) -> Any:
        return self._json.get("version")

    @property
    def variants(self) -> list:
        v = self._json.get("variants")
        return v if isinstance(v, list) else []

    @property
    def overlays(self) -> list:
        v = self._json.get("overlays")
        return v if isinstance(v, list) else []

    @property
    def hierarchy(self) -> list[Level]:
        h = self._json.get("hierarchy")
        return [Level.from_json(n) for n in h] if isinstance(h, list) else []

    def level(self, name: str) -> Level | None:
        for lv in self.hierarchy:
            if lv.level == name:
                return lv
        return None

    @property
    def operations(self) -> dict[str, dict]:
        """Operations by name, in definition order."""
        ops = self._json.get("operations")
        if not isinstance(ops, dict):
            return {}
        return {k: v for k, v in ops.items() if isinstance(v, dict)}

    def operation(self, name: str) -> dict | None:
        ops = self._json.get("operations")
        n = ops.get(name) if isinstance(ops, dict) else None
        return n if isinstance(n, dict) else None

    def effective(self, facts: Facts) -> "SourceDefinition":
        """The definition with every overlay whose ``when`` holds merged in (in order), and every operation whose own
        ``when`` does not hold removed."""
        out = copy.deepcopy(self._json)
        for overlay in self.overlays:
            if condition_holds(J.get(overlay, "when"), facts):
                patch = copy.deepcopy(overlay) if isinstance(overlay, dict) else {}
                for k in ("when", "description", "overlay", "specVersion"):
                    patch.pop(k, None)
                out = json_merge(out, patch)
        out.pop("overlays", None)
        ops = out.get("operations")
        if isinstance(ops, dict):
            for k in [k for k, v in ops.items() if not condition_holds(J.get(v, "when"), facts)]:
                del ops[k]
        return SourceDefinition(out)

    def __str__(self) -> str:
        return "SourceDefinition[" + str(self.id) + "]"

    __repr__ = __str__


# ------------------------------------------------------------------------------------------------ DefinitionRegistry

_ID = re.compile(r"[a-z][a-z0-9_]*")
_FIXED_SCOPES = {"source", "dataset", "datasets"}
_PAGING = ["keyset", "offset", "page", "cursor", "nextUrl", "linkHeader"]


class DefinitionRegistry:
    """All source definitions the SDK knows: the built-in ones, then any directories the caller adds. A later file
    with the same ``id`` replaces an earlier one. A file with ``"overlay": "<id>"`` patches that definition for the
    versions or variants its ``when`` names; overlays are appended to the definition's own ``overlays`` in load
    order. Resolution applies ``extends`` (deep merge onto the parent; ``aliases`` are not inherited)."""

    def __init__(self, built_in: SpecResources | None = None, directories: Iterable[str | Path] = ()):
        self._lock = threading.RLock()
        self._raw: dict[str, dict] = {}
        self._origin: dict[str, str] = {}
        self._overlays: dict[str, list[dict]] = {}
        self._aliases: dict[str, str] = {}
        self._resolved: dict[str, SourceDefinition] = {}
        if built_in is not None:
            for f in built_in.definition_files():
                self.add(built_in.read(f), "package:" + f)
        for d in directories:
            self.add_directory(d)

    def add_directory(self, directory: str | Path) -> None:
        d = Path(directory)
        if not d.is_dir():
            raise AgentFrontierError.invalid_config("definitions directory not found: " + str(directory))
        files = []
        for base, _dirs, names in os.walk(d):
            for n in names:
                if n.endswith(".json"):
                    files.append(os.path.join(base, n))
        try:
            for f in sorted(files):
                doc = J.loads_first(Path(f).read_text(encoding="utf-8"))
                self.add(doc, f)
        except (OSError, ValueError) as e:
            raise AgentFrontierError.invalid_definition("cannot read definitions in " + str(directory) + ": " + str(e)) \
                from e
        with self._lock:
            self._resolved.clear()

    def add(self, doc: Any, origin: str) -> None:
        """Adds one definition or overlay document; anything else (a connection file, say) is ignored."""
        if not isinstance(doc, dict):
            return
        with self._lock:
            overlay_of = J.text(doc, "overlay")
            if overlay_of is not None:
                self._overlays.setdefault(overlay_of, []).append(doc)
                self._resolved.clear()
                return
            id_ = J.text(doc, "id")
            if id_ is None or (doc.get("protocol") is None and doc.get("extends") is None):
                return
            if not _ID.fullmatch(id_):
                raise AgentFrontierError.invalid_definition(origin + ": id '" + id_ + "' must match " + _ID.pattern)
            self._raw[id_] = doc
            self._origin[id_] = origin
            for a in J.strings(doc.get("aliases")):
                self._aliases[a] = id_
            self._resolved.clear()

    def canonical_id(self, type_or_alias: str) -> str:
        if type_or_alias in self._raw:
            return type_or_alias
        return self._aliases.get(type_or_alias, type_or_alias)

    def find(self, type_or_alias: str) -> SourceDefinition | None:
        with self._lock:
            id_ = self.canonical_id(type_or_alias)
            if id_ not in self._raw:
                return None
            d = self._resolved.get(id_)
            if d is None:
                d = SourceDefinition(self._resolve_json(id_, []))
                self._resolved[id_] = d
            return d

    def get(self, type_or_alias: str) -> SourceDefinition:
        d = self.find(type_or_alias)
        if d is None:
            raise AgentFrontierError.invalid_config("unknown source type '" + type_or_alias + "'; known: "
                                                    + J.jlist(self.ids()))
        return d

    def ids(self) -> list[str]:
        return list(self._raw.keys())

    def all(self) -> list[SourceDefinition]:
        out = [self.get(i) for i in list(self._raw.keys())]
        out.sort(key=lambda d: (d.name or "").lower())
        return out

    def origin(self, id_: str) -> str | None:
        return self._origin.get(self.canonical_id(id_))

    def _resolve_json(self, id_: str, visiting: list[str]) -> dict:
        if id_ in visiting:
            raise AgentFrontierError.invalid_definition("definition 'extends' cycle: " + J.jlist(visiting))
        visiting.append(id_)
        own = self._raw.get(id_)
        if own is None:
            raise AgentFrontierError.invalid_definition("definition '" + id_ + "' not found (needed by "
                                                        + J.jlist(visiting) + ")")
        parent = J.text(own, "extends")
        if parent is not None:
            base = self._resolve_json(self.canonical_id(parent), visiting)
            patch = copy.deepcopy(own)
            patch.pop("extends", None)
            result = json_merge(base, patch)
            result["aliases"] = copy.deepcopy(own["aliases"]) if "aliases" in own else []
            result["extends"] = parent
        else:
            result = copy.deepcopy(own)
        extra = self._overlays.get(id_, [])
        if extra:
            lst = result.get("overlays")
            if not isinstance(lst, list):
                lst = []
                result["overlays"] = lst
            for o in extra:
                c = copy.deepcopy(o)
                c.pop("overlay", None)
                lst.append(c)
        visiting.remove(id_)
        return result

    def validate_all(self) -> list[str]:
        """Problems with every definition; empty when all are usable."""
        problems: list[str] = []
        for id_ in list(self._raw.keys()):
            try:
                problems.extend(DefinitionRegistry.validate(self.get(id_)))
            except AgentFrontierError as e:
                problems.append(id_ + ": " + e.message)
        for target in self._overlays:
            if target not in self._raw:
                problems.append("overlay for unknown definition '" + target + "'")
        return problems

    @staticmethod
    def validate(d: SourceDefinition) -> list[str]:
        p: list[str] = []
        id_ = str(d.id)
        if d.protocol is None:
            p.append(id_ + ": protocol is required")
        ops = d.operations
        levels: list[str] = []
        for lv in d.hierarchy:
            if lv.level is None:
                p.append(id_ + ": every hierarchy level needs a 'level' name")
                continue
            if lv.level not in levels:
                levels.append(lv.level)
            if lv.list is not None and lv.list not in ops:
                p.append(id_ + ": hierarchy level '" + lv.level + "' lists with unknown operation '" + lv.list + "'")
            if lv.list is None and lv.value is None:
                p.append(id_ + ": hierarchy level '" + lv.level + "' needs 'list' or 'value'")
        for name, op in ops.items():
            where = id_ + ".operations." + name
            if J.text(op, "entity") is None:
                p.append(where + ": 'entity' is required")
            scope = J.text(op, "scope")
            if scope is not None and scope not in _FIXED_SCOPES and scope not in levels:
                p.append(where + ": scope '" + scope + "' is not source, dataset, datasets or a hierarchy level "
                         + J.jlist(levels))
            paging = op.get("paging")
            if paging is not None and J.text(paging, "mode", "") not in _PAGING:
                p.append(where + ": paging mode must be one of " + J.jlist(_PAGING))
            if op.get("sql") is None and op.get("request") is None and op.get("files") is None \
                    and op.get("alternatives") is None:
                p.append(where + ": needs 'sql', 'request' or 'files'")
        return p


def load_json_file(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
