"""The built-in spec (``spec/`` package data) and its typed views: entity model, auth catalog, field specs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from . import json_util as J
from .errors import AgentFrontierError

# --------------------------------------------------------------------------------------------------------- FieldSpec


@dataclass(frozen=True)
class FieldSpec:
    """One configuration field (a connection property, an option or an auth field).

    ``type`` is one of string, integer, boolean, select, text, list, secret or secret_text.
    """

    key: str | None
    label: str | None = None
    type: str = "string"
    required: bool = False
    default_value: str | None = None
    help: str | None = None
    options: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type is None:
            object.__setattr__(self, "type", "string")
        if self.label is None:
            object.__setattr__(self, "label", self.key)
        if self.options is None:
            object.__setattr__(self, "options", [])

    @property
    def secret(self) -> bool:
        return self.type in ("secret", "secret_text")

    @staticmethod
    def from_json(n: Any) -> "FieldSpec":
        opts = n.get("options") if isinstance(n, dict) else None
        o = [J.as_text(x) for x in opts] if isinstance(opts, list) else []
        d = n.get("default") if isinstance(n, dict) else None
        return FieldSpec(J.text(n, "key"), J.text(n, "label"), J.text(n, "type", "string"),
                         J.boolean(n, "required", False), None if d is None else J.as_text(d), J.text(n, "help"), o)

    @staticmethod
    def list_from_json(array: Any) -> list["FieldSpec"]:
        if not isinstance(array, list):
            return []
        return [FieldSpec.from_json(n) for n in array]

    def typed_default(self) -> Any:
        """The default converted to this field's type."""
        return self.convert(self.default_value)

    def convert(self, v: Any) -> Any:
        """Converts a raw value to this field's type; unparseable values are returned unchanged."""
        if v is None:
            return None
        if self.type == "integer":
            if J.is_number(v):
                return J.double_to_long(v) if isinstance(v, float) else int(v)
            s = J.jtrim(J.jstr(v))
            if not s:
                return None
            try:
                return J.parse_long(s)
            except ValueError:
                return v
        if self.type == "boolean":
            if isinstance(v, bool):
                return v
            if J.is_number(v):
                return v != 0
            s = J.jtrim(J.jstr(v)).lower()
            return None if not s else s in ("true", "1", "yes", "y", "on", "t")
        if self.type == "list":
            if isinstance(v, list):
                return v
            return [J.jtrim(s) for s in J.jstr(v).split(",") if not J.is_blank(s)]
        return v

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "required": self.required,
                "defaultValue": self.default_value, "help": self.help, "options": list(self.options)}


# -------------------------------------------------------------------------------------------------------- AuthMethod


@dataclass(frozen=True)
class AuthMethod:
    """One way of authenticating: its fields and the provider that turns them into credentials at connect time."""

    id: str | None
    name: str | None
    description: str | None
    provider: str
    fields: list[FieldSpec]

    @staticmethod
    def from_json(n: Any) -> "AuthMethod":
        return AuthMethod(J.text(n, "id"), J.text(n, "name"), J.text(n, "description"),
                          J.text(n, "provider", "static"), FieldSpec.list_from_json(J.get(n, "fields")))

    def is_secret(self, key: str) -> bool:
        if self.id == "custom":
            return True
        return any(f.key == key and f.secret for f in self.fields)


class AuthCatalog:
    """The auth methods from ``spec/auth-methods.json``."""

    def __init__(self, doc: Any):
        self._methods: dict[str, AuthMethod] = {}
        for n in (doc.get("methods") if isinstance(doc, dict) else None) or []:
            a = AuthMethod.from_json(n)
            self._methods[a.id] = a

    def all(self) -> list[AuthMethod]:
        return list(self._methods.values())

    def find(self, id_: str) -> AuthMethod | None:
        return self._methods.get(id_)


# ------------------------------------------------------------------------------------------------------- EntityModel


def normalize(name: str) -> str:
    """Lower case without underscores, dashes or spaces: the form column names are matched in."""
    return "".join(c.lower() for c in name if c not in "_- ")


@dataclass(frozen=True)
class Enumeration:
    values: list[str]
    synonyms: dict[str, str]

    def normalize(self, raw: str | None) -> str | None:
        """Lower case, spaces and dashes to underscores, then synonyms. Unknown values are kept (normalised)."""
        if raw is None:
            return None
        v = J.jtrim(raw).lower().replace(" ", "_").replace("-", "_")
        s = self.synonyms.get(v)
        return s if s is not None else v


@dataclass(frozen=True)
class Entity:
    name: str
    aspect: str
    fields: dict[str, str]
    enums: dict[str, Enumeration]

    def canonical(self, column: str) -> str | None:
        norm = normalize(column)
        for f in self.fields:
            if normalize(f) == norm:
                return f
        return None


class EntityModel:
    """The canonical entities from ``spec/entities.json``."""

    def __init__(self, doc: Any):
        self._entities: dict[str, Entity] = {}
        for name, e in ((doc or {}).get("entities") or {}).items():
            fields = {k: J.as_text(v) for k, v in ((e or {}).get("fields") or {}).items()}
            enums = {}
            for en_name, en in ((e or {}).get("enums") or {}).items():
                enums[en_name] = Enumeration(J.strings(J.get(en, "values")),
                                             {k: J.as_text(v) for k, v in (J.get(en, "synonyms") or {}).items()})
            self._entities[name] = Entity(name, J.text(e, "aspect", "metadata"), fields, enums)
        self._aspects = {k: J.as_text(v) for k, v in ((doc or {}).get("aspects") or {}).items()}

    def entity(self, name: str) -> Entity | None:
        return self._entities.get(name)

    @property
    def entities(self) -> dict[str, Entity]:
        return dict(self._entities)

    @property
    def aspects(self) -> dict[str, str]:
        return dict(self._aspects)

    def aspect_of(self, entity: str) -> str:
        """Aspect an entity belongs to; ``data`` for rows, unknown entities default to ``metadata``."""
        if entity == "row":
            return "data"
        e = self._entities.get(entity)
        return "metadata" if e is None else e.aspect

    @staticmethod
    def known_types() -> list[str]:
        return ["string", "long", "integer", "double", "boolean", "list", "any"]

    normalize = staticmethod(normalize)


# ----------------------------------------------------------------------------------------------------- SpecResources


class SpecResources:
    """The built-in spec packaged as ``agentfrontier/spec``. The file list comes from ``index.json``."""

    PACKAGE = "agentfrontier"

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def read_text(self, relative: str) -> str:
        try:
            node = resources.files(self.PACKAGE).joinpath("spec")
            for part in relative.split("/"):
                node = node.joinpath(part)
            return node.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as e:
            raise AgentFrontierError.invalid_definition("built-in spec file missing from the package: " + relative) \
                from e

    def read(self, relative: str) -> Any:
        """A built-in spec file, parsed (a fresh copy every call)."""
        if relative not in self._cache:
            try:
                self._cache[relative] = json.loads(self.read_text(relative))
            except ValueError as e:
                raise AgentFrontierError.invalid_definition("cannot read " + relative + ": " + str(e)) from e
        return J.deep_copy(self._cache[relative])

    def index(self) -> Any:
        return self.read("index.json")

    def definition_files(self) -> list[str]:
        return ["definitions/" + J.as_text(n) for n in (self.index().get("definitions") or [])]

    def entities(self) -> EntityModel:
        return EntityModel(self.read("entities.json"))

    def auth_methods(self) -> AuthCatalog:
        return AuthCatalog(self.read("auth-methods.json"))
