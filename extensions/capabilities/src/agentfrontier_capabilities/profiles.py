"""Capability profiles: what a capability takes and returns, how it works and which version it is, so a product
(Avero, the gateway) can list capabilities, validate a call before making it and record the version in provenance.

A profile is a directory with ``capability.json`` (name, version, description, ``needsLlm``, ``deterministic``,
``inputSchema``, ``outputSchema``, examples) and ``instructions.md``. The built-in profiles ship in
``agentfrontier_capabilities/capability_profiles``. Input checks use a small JSON Schema subset (type, required, properties,
enum, items, minimum, maximum, minLength), so no dependency is needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["CapabilityProfile", "load_profile", "builtin_profiles", "check_schema"]

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}


def check_schema(schema: dict, value: Any, path: str = "$") -> list[str]:
    """Problems with a value against a JSON Schema subset; empty when it fits."""
    out: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        ok = any(v == "null" and value is None or v in _TYPES and isinstance(value, _TYPES[v])
                 and not (v in ("integer", "number") and isinstance(value, bool)) for v in types)
        if not ok:
            return [path + ": expected " + "/".join(types)]
    if "enum" in schema and value not in schema["enum"]:
        out.append(path + ": must be one of " + ", ".join(map(str, schema["enum"])))
    if isinstance(value, dict):
        for k in schema.get("required") or []:
            if k not in value:
                out.append(path + "." + k + ": required")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                out += check_schema(sub, value[k], path + "." + k)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            out += check_schema(schema["items"], v, path + "[" + str(i) + "]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(path + ": below " + str(schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            out.append(path + ": above " + str(schema["maximum"]))
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        out.append(path + ": too short")
    return out


@dataclass
class CapabilityProfile:
    name: str
    version: str
    description: str
    needs_llm: bool
    deterministic: bool
    input_schema: dict
    output_schema: dict
    instructions: str = ""
    examples: list[dict] = field(default_factory=list)

    def check_input(self, value: Any) -> list[str]:
        return check_schema(self.input_schema, value)

    def to_json(self) -> dict:
        return {"name": self.name, "version": self.version, "description": self.description,
                "needsLlm": self.needs_llm, "deterministic": self.deterministic, "inputSchema": self.input_schema,
                "outputSchema": self.output_schema, "examples": self.examples}


def load_profile(directory: str | Path) -> CapabilityProfile:
    d = Path(directory)
    m = json.loads((d / "capability.json").read_text(encoding="utf-8"))
    ins = d / "instructions.md"
    return CapabilityProfile(m["name"], m["version"], m.get("description", ""), bool(m.get("needsLlm")),
                             bool(m.get("deterministic")), m.get("inputSchema") or {}, m.get("outputSchema") or {},
                             ins.read_text(encoding="utf-8") if ins.exists() else "", list(m.get("examples") or []))


def builtin_profiles() -> dict[str, CapabilityProfile]:
    root = Path(__file__).parent / "capability_profiles"
    out = {}
    for d in sorted(p for p in root.iterdir() if (p / "capability.json").exists()):
        p = load_profile(d)
        out[p.name] = p
    return out
