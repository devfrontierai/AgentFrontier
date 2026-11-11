"""Provenance manifests for generated artifacts (SQL, dbt changes, tests, fixtures, reports): what went in, which
tools and models made it, how it was validated and who approved it, with content hashes so a later reader can tell
whether the artifact or its inputs changed.

A manifest is plain JSON (``to_json``). Its ``digest`` covers everything except the approvals and itself, so an
approval does not change what was approved. Standard library only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Manifest", "sha256_text", "VALIDATION_STATES"]

# "ran": checks were executed against a target; "static": parsed and checked, not executed; "not_run": nothing
VALIDATION_STATES = ("passed", "failed", "warnings", "static", "not_run")


def sha256_text(text: str | bytes) -> str:
    b = text.encode("utf-8") if isinstance(text, str) else text
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Manifest:
    kind: str
    title: str
    id: str = field(default_factory=lambda: "art-" + uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now)
    created_by: str = "agentfrontier"
    inputs: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    model: dict | None = None
    validation: dict = field(default_factory=lambda: {"status": "not_run", "checks": []})
    approvals: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def add_input(self, kind: str, ref: str, content: str | bytes | None = None, version: str | None = None,
                  **extra: Any) -> "Manifest":
        """An input: a catalog object, a question, a file, a crawl. ``content`` is hashed, never stored."""
        d = {"kind": kind, "ref": ref, **extra}
        if version is not None:
            d["version"] = version
        if content is not None:
            d["hash"] = sha256_text(content)
        self.inputs.append(d)
        return self

    def add_output(self, path: str, content: str | bytes, **extra: Any) -> "Manifest":
        self.outputs.append({"path": path, "hash": sha256_text(content), "bytes": len(content), **extra})
        return self

    def add_tool(self, name: str, version: str | None = None, **extra: Any) -> "Manifest":
        self.tools.append({"name": name, **({"version": version} if version else {}), **extra})
        return self

    def set_model(self, provider: str, model: str, **usage: Any) -> "Manifest":
        self.model = {"provider": provider, "model": model, **usage}
        return self

    def set_validation(self, status: str, checks: list[dict], target: str | None = None) -> "Manifest":
        if status not in VALIDATION_STATES:
            raise ValueError("validation status must be one of " + ", ".join(VALIDATION_STATES))
        self.validation = {"status": status, "checks": checks, "at": _now(), **({"target": target} if target else {})}
        return self

    def approve(self, by: str, decision: str = "approved", note: str | None = None) -> "Manifest":
        self.approvals.append({"by": by, "decision": decision, "at": _now(), **({"note": note} if note else {}),
                               "digest": self.digest()})
        return self

    def _body(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "createdAt": self.created_at,
                "createdBy": self.created_by, "inputs": self.inputs, "outputs": self.outputs, "tools": self.tools,
                "model": self.model, "validation": self.validation, "limitations": self.limitations}

    def digest(self) -> str:
        return sha256_text(json.dumps(self._body(), sort_keys=True, separators=(",", ":"), default=str))

    def to_json(self) -> dict:
        return {**self._body(), "approvals": self.approvals, "digest": self.digest()}

    @staticmethod
    def from_json(d: dict) -> "Manifest":
        m = Manifest(d["kind"], d["title"], d["id"], d.get("createdAt") or _now(), d.get("createdBy") or "agentfrontier",
                     list(d.get("inputs") or []), list(d.get("outputs") or []), list(d.get("tools") or []),
                     d.get("model"), d.get("validation") or {"status": "not_run", "checks": []},
                     list(d.get("approvals") or []), list(d.get("limitations") or []))
        return m
