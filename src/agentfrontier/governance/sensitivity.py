"""Sensitivity labels (spec docs/sensitivity.md): a small ordered taxonomy (Public < Internal < Confidential <
Restricted) that classifications map to, so policy can be written on a few labels instead of every classification.

    tx = SensitivityTaxonomy.builtin()
    tx.effective(["pii.person_name", "pii.national_id"])      # ("restricted", "classification")
    tx.effective(["pii.national_id"], declared="internal")    # ("internal", "declared"): a person corrected it
    tx.policy_label("restricted")                             # "sensitivity.restricted", for rules and masks

A field's effective label is the one declared for it (by its source or a person), else the highest-ranked label its
classifications map to, else the taxonomy's default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..errors import AgentFrontierError
from .policy import glob_match

__all__ = ["SensitivityLabel", "SensitivityTaxonomy", "POLICY_PREFIX"]

POLICY_PREFIX = "sensitivity."


@dataclass(frozen=True)
class SensitivityLabel:
    id: str
    name: str
    rank: int
    description: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "rank": self.rank, "description": self.description}


class SensitivityTaxonomy:
    """An ordered set of sensitivity labels and the classifications that map to them."""

    def __init__(self, doc: dict):
        if not isinstance(doc, dict) or not doc.get("labels"):
            raise AgentFrontierError.invalid_config("a sensitivity taxonomy needs labels")
        self.version = str(doc.get("version") or "")
        self.labels: list[SensitivityLabel] = sorted(
            (SensitivityLabel(str(x["id"]), str(x.get("name") or x["id"]), int(x["rank"]),
                              str(x.get("description") or "")) for x in doc["labels"]), key=lambda x: x.rank)
        self._by_id = {x.id: x for x in self.labels}
        if len(self._by_id) != len(self.labels):
            raise AgentFrontierError.invalid_config("sensitivity label ids must be unique")
        self.default = str(doc.get("default") or self.labels[0].id)
        if self.default not in self._by_id:
            raise AgentFrontierError.invalid_config("the default sensitivity label " + self.default + " is unknown")
        self.classifications: dict[str, str] = {}
        for key, label in (doc.get("classifications") or {}).items():
            if label not in self._by_id:
                raise AgentFrontierError.invalid_config(key + " maps to unknown sensitivity label " + str(label))
            self.classifications[str(key)] = str(label)

    @staticmethod
    def builtin() -> "SensitivityTaxonomy":
        from ..spec_model import SpecResources
        return SensitivityTaxonomy(SpecResources().read("classification/sensitivity.json"))

    def label(self, label_id: str | None) -> SensitivityLabel | None:
        return self._by_id.get(label_id or "")

    def rank(self, label_id: str | None) -> int:
        """The label's rank; -1 when the taxonomy does not know it."""
        x = self._by_id.get(label_id or "")
        return -1 if x is None else x.rank

    def for_classification(self, name: str) -> str | None:
        """The sensitivity label a classification maps to: an exact entry first, else a glob; None when unmapped."""
        if name in self.classifications:
            return self.classifications[name]
        for key, label in self.classifications.items():
            if "*" in key and glob_match(key, name):
                return label
        return None

    def effective(self, classifications: Iterable[str], declared: str | None = None) -> tuple[str, str]:
        """(label id, origin): ``declared`` when a known label was declared, else the highest-ranked label the
        classifications map to (``classification``), else the default (``default``)."""
        if declared and declared in self._by_id:
            return declared, "declared"
        best: SensitivityLabel | None = None
        for c in classifications:
            x = self._by_id.get(self.for_classification(c) or "")
            if x is not None and (best is None or x.rank > best.rank):
                best = x
        if best is not None:
            return best.id, "classification"
        return self.default, "default"

    @staticmethod
    def policy_label(label_id: str) -> str:
        """The label policy rules and masks see: ``sensitivity.<id>``."""
        return POLICY_PREFIX + label_id

    def at_least(self, label_id: str) -> list[str]:
        """The ids ranked at or above ``label_id``, lowest first (what a mask "Confidential and up" names)."""
        r = self.rank(label_id)
        return [x.id for x in self.labels if x.rank >= r] if r >= 0 else []

    def to_dict(self) -> dict:
        return {"version": self.version, "default": self.default, "labels": [x.to_dict() for x in self.labels],
                "classifications": dict(self.classifications)}
