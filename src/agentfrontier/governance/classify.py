"""Classification (spec docs/classification.md): rule-based detectors over field names, comments and sample values,
``label`` records with provenance, and reconciliation of source-declared and inferred labels.

::

    c = RuleClassifier.builtin()
    c.classify("customer_email", None, ["a@x.io", "b@y.org"])   # -> [Hit(rule=pii.email, ...)]
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .. import json_util as J
from ..errors import AgentFrontierError
from ..model import Record
from .policy import load_document

DEFAULT_CONFIDENCE = 0.5
MODES = ("source_authoritative", "engine_authoritative", "union_with_precedence", "advisory_only")
SOURCE = "source"
# label fields; anything else on a label record (apart from entity/source/properties) is a hierarchy level
_LABEL_FIELDS = {"entity", "source", "properties", "name", "value", "labelType", "objectType", "dataset", "field",
                 "content", "engine", "ruleVersion", "reviewStatus", "expiresAt", "confidence", "evidence"}


def luhn(text: str) -> bool:
    digits = [c for c in text if c not in " -"]
    if not 13 <= len(digits) <= 19 or not all("0" <= c <= "9" for c in digits):
        return False
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = ord(c) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban(text: str) -> bool:
    s = text.replace(" ", "")
    if len(s) < 15 or len(s) > 34 or not s.isalnum() or not s.isascii():
        return False
    rotated = s[4:] + s[:4]
    num = "".join(str(int(c, 36)) for c in rotated.upper())
    return int(num) % 97 == 1


VALIDATORS = {"luhn": luhn, "iban": iban}


@dataclass(frozen=True)
class Hit:
    """A rule that fired on one field."""

    rule: str
    label: str
    label_type: str
    value: str | None
    confidence: float
    evidence: str


class _Detector:
    def __init__(self, n: dict, kind: str, rule_id: str):
        try:
            self.patterns = [re.compile(p) for p in J.strings(n.get("patterns"))]
        except re.error as e:
            raise AgentFrontierError.invalid_config("classification rule " + rule_id + ": bad " + kind
                                                    + " pattern: " + str(e)) from e
        if not self.patterns:
            raise AgentFrontierError.invalid_config("classification rule " + rule_id + ": " + kind
                                                    + " needs patterns")
        c = n.get("confidence")
        self.confidence = float(c) if c is not None else DEFAULT_CONFIDENCE
        self.min_fraction = float(n.get("minFraction") or 0.8)
        self.min_values = int(n.get("minValues") or 1)
        v = n.get("validator")
        if v is not None and v not in VALIDATORS:
            raise AgentFrontierError.invalid_config("classification rule " + rule_id + ": unknown validator '"
                                                    + J.jstr(v) + "'")
        self.validator = VALIDATORS.get(v) if v is not None else None

    def search(self, text: str | None) -> bool:
        return text is not None and any(p.search(text) for p in self.patterns)

    def full(self, text: str) -> bool:
        return any(p.fullmatch(text) for p in self.patterns) and (self.validator is None or self.validator(text))


class _Rule:
    def __init__(self, n: dict):
        self.id = J.text(n, "id") or ""
        self.label = J.text(n, "label") or self.id
        self.label_type = J.text(n, "labelType", "classification") or "classification"
        self.value = J.text(n, "value")
        if J.is_blank(self.id):
            raise AgentFrontierError.invalid_config("every classification rule needs an id")
        self.name = _Detector(n["name"], "name", self.id) if isinstance(n.get("name"), dict) else None
        self.comment = _Detector(n["comment"], "comment", self.id) if isinstance(n.get("comment"), dict) else None
        self.values = _Detector(n["values"], "values", self.id) if isinstance(n.get("values"), dict) else None
        if self.name is None and self.comment is None and self.values is None:
            raise AgentFrontierError.invalid_config("classification rule " + self.id + " has no detector")


def value_text(v: Any) -> str:
    return v if isinstance(v, str) else J.canonical(v) if isinstance(v, (dict, list)) else J.jstr(v)


class RuleClassifier:
    """Rules from one or more rules documents; the built-in set by default."""

    def __init__(self, documents: Iterable[dict], engine: str | None = None):
        self.rules: list[_Rule] = []
        self.versions: dict[str, str] = {}  # rule id -> ruleVersion
        engines = []
        for doc in documents:
            version = J.text(doc, "version") or "unversioned"
            engines.append(J.text(doc, "engine"))
            for n in doc.get("rules") or []:
                r = _Rule(n)
                if r.id in self.versions:
                    raise AgentFrontierError.invalid_config("duplicate classification rule id '" + r.id + "'")
                self.versions[r.id] = version
                self.rules.append(r)
        self.engine = engine or next((e for e in engines if e), None) or "agentfrontier-rules"

    @staticmethod
    def builtin(extra: Iterable[str | Path | dict] = (), engine: str | None = None) -> "RuleClassifier":
        from ..spec_model import SpecResources
        docs = [SpecResources().classification_rules()]
        for e in extra:
            docs.append(e if isinstance(e, dict) else load_document(e))
        return RuleClassifier(docs, engine)

    def classify(self, name: str | None, comment: str | None = None,
                 values: Iterable[Any] | None = None) -> list[Hit]:
        """Rules that fire on one field. ``values``: its sample values (nulls are ignored)."""
        texts = None
        if values is not None:
            texts = [value_text(v) for v in values if v is not None]
        out = []
        for r in self.rules:
            fired: list[str] = []
            conf = -1.0
            if r.name is not None and r.name.search(name):
                fired.append("name")
                conf = max(conf, r.name.confidence)
            if r.comment is not None and r.comment.search(comment):
                fired.append("comment")
                conf = max(conf, r.comment.confidence)
            if r.values is not None and texts:
                matched = sum(1 for t in texts if r.values.full(t))
                if matched >= r.values.min_values and matched >= r.values.min_fraction * len(texts):
                    fired.append("values:" + str(matched) + "/" + str(len(texts)))
                    conf = max(conf, r.values.confidence)
            if fired:
                out.append(Hit(r.id, r.label, r.label_type, r.value, conf, ",".join(fired)))
        return out

    def label(self, hit: Hit, target: dict) -> Record:
        """A ``label`` record for a hit on the field described by ``target`` (``source``, levels, ``dataset``,
        ``field``)."""
        v = dict(target)
        v.update({"name": hit.label, "value": hit.value, "labelType": hit.label_type, "objectType": "field",
                  "engine": self.engine, "ruleVersion": self.versions.get(hit.rule), "reviewStatus": "unreviewed",
                  "confidence": hit.confidence, "evidence": hit.evidence})
        return Record("label", {k: x for k, x in v.items() if x is not None})


class FieldClassifier:
    """Collects the fields (and sample rows) of one part of a run, then classifies each field once, with every
    detector, when flushed."""

    def __init__(self, classifier: RuleClassifier):
        self.classifier = classifier
        self._fields: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(target: dict) -> tuple:
        return tuple(sorted((k, J.jstr(v)) for k, v in target.items()))

    def observe_field(self, r: Record) -> None:
        target = {k: v for k, v in r.values.items()
                  if k not in ("name", "ordinal", "dataType", "nullable", "defaultValue", "comment", "isGenerated",
                               "maxLength", "numericPrecision", "numericScale", "parent", "properties")}
        target["field"] = r.string("name")
        with self._lock:
            e = self._fields.setdefault(self._key(target), {"target": target, "comment": None, "values": None})
            e["comment"] = r.string("comment")

    def observe_rows(self, base: dict, dataset: str, rows: list[dict]) -> None:
        """``base``: ``source`` and the scope levels; ``rows``: the ``values`` of sampled rows."""
        columns: dict[str, list] = {}
        for row in rows:
            for k, v in row.items():
                columns.setdefault(k, []).append(v)
        with self._lock:
            for col, vals in columns.items():
                target = dict(base)
                target["dataset"] = dataset
                target["field"] = col
                e = self._fields.setdefault(self._key(target), {"target": target, "comment": None, "values": None})
                e["values"] = vals

    def pending_labels(self, dataset: str) -> list[str]:
        """Label names the collected fields of a dataset already get from their names and comments, so a decision
        made before sampling it can take them into account."""
        with self._lock:
            entries = [e for e in self._fields.values() if e["target"].get("dataset") == dataset]
        return sorted({h.label for e in entries for h in self.classifier.classify(e["target"].get("field"),
                                                                                    e["comment"])})

    def flush(self, dataset: str | None = None) -> list[Record]:
        """Label records for the fields collected so far (only those of ``dataset`` when given), which are then
        forgotten."""
        with self._lock:
            keys = [k for k, e in self._fields.items() if dataset is None or e["target"].get("dataset") == dataset]
            pending = [self._fields.pop(k) for k in keys]
        out = []
        for e in pending:
            t = e["target"]
            for h in self.classifier.classify(t.get("field"), e["comment"], e["values"]):
                out.append(self.classifier.label(h, t))
        return out


# ------------------------------------------------------------------------------------------------- reconciliation


def as_label(r: Record | dict) -> dict | None:
    """A label dict from a ``label`` or ``tag`` record (tags are source labels of type ``tag``)."""
    v = r.to_json() if isinstance(r, Record) else dict(r)
    entity = v.get("entity", "label")
    if entity == "tag":
        v = dict(v)
        v["entity"] = "label"
        v.setdefault("labelType", "tag")
        return v
    return v if entity == "label" else None


def origin(label: dict) -> str:
    e = label.get("engine")
    return SOURCE if e is None or J.is_blank(J.jstr(e)) else J.jstr(e)


def object_key(label: dict) -> tuple:
    """What a label is on: source, levels, objectType, dataset, field, content."""
    levels = tuple(sorted((k, J.jstr(v)) for k, v in label.items() if k not in _LABEL_FIELDS and v is not None
                          and not isinstance(v, (dict, list))))
    return (label.get("source"), levels, label.get("objectType"), label.get("dataset"), label.get("field"),
            label.get("content"))


@dataclass
class Reconciled:
    effective: list
    advisory: list
    conflicts: list

    def names(self) -> list[str]:
        return sorted({J.jstr(x.get("name")) for x in self.effective})


def _live(label: dict, now: _dt.datetime) -> bool:
    if J.text(label, "reviewStatus") in ("rejected",):
        return False
    exp = J.text(label, "expiresAt")
    if exp:
        try:
            t = _dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
            if t <= now:
                return False
        except ValueError:
            pass
    return True


def reconcile(labels: Iterable[Record | dict], mode: str = "union_with_precedence", engines: Iterable[str] = (),
              precedence: Iterable[str] | None = None, now: _dt.datetime | None = None) -> Reconciled:
    """Effective labels per docs/classification.md §3, over labels of any number of objects."""
    if mode not in MODES:
        raise AgentFrontierError.invalid_config("reconcile mode must be one of " + J.jlist(MODES))
    now = now or _dt.datetime.now(_dt.timezone.utc)
    by_object: dict[tuple, list[dict]] = {}
    advisory: list[dict] = []
    for r in labels:
        lab = as_label(r)
        if lab is None:
            continue
        if not _live(lab, now):
            advisory.append(lab)
            continue
        by_object.setdefault(object_key(lab), []).append(lab)
    designated = set(engines)
    effective: list[dict] = []
    conflicts: list[dict] = []
    for labs in by_object.values():
        if mode == "advisory_only":
            for lab in labs:
                (effective if origin(lab) == SOURCE else advisory).append(lab)
        elif mode in ("source_authoritative", "engine_authoritative"):
            groups: dict[Any, list[dict]] = {}
            for lab in labs:
                groups.setdefault(lab.get("labelType"), []).append(lab)
            for grp in groups.values():
                if mode == "source_authoritative":
                    wins = [x for x in grp if origin(x) == SOURCE]
                else:
                    wins = [x for x in grp if origin(x) != SOURCE and (not designated or origin(x) in designated)]
                if not wins:
                    effective.extend(grp)
                    continue
                effective.extend(wins)
                won = {(x.get("name"), x.get("value")) for x in wins}
                for x in grp:
                    if x in wins:
                        continue
                    if (x.get("name"), x.get("value")) in won:
                        advisory.append(x)
                    else:
                        conflicts.append({"winner": wins[0], "loser": x})
        else:
            order = list(precedence) if precedence is not None else None
            if order is None:
                externals = []
                for x in labs:
                    o = origin(x)
                    if o not in (SOURCE, "agentfrontier-rules") and o not in externals:
                        externals.append(o)
                order = [SOURCE] + externals + ["agentfrontier-rules"]

            def rank(x: dict) -> int:
                o = origin(x)
                return order.index(o) if o in order else len(order)

            by_name: dict[Any, list[dict]] = {}
            for x in labs:
                by_name.setdefault(x.get("name"), []).append(x)
            for grp in by_name.values():
                grp = sorted(grp, key=rank)
                best = grp[0]
                effective.append(best)
                for x in grp[1:]:
                    if x.get("value") == best.get("value"):
                        advisory.append(x)
                    else:
                        conflicts.append({"winner": best, "loser": x})
    return Reconciled(effective, advisory, conflicts)


class LabelIndex:
    """Labels seen so far in a run, by object, and their effective names under a reconcile mode."""

    def __init__(self, mode: str = "union_with_precedence", engines: Iterable[str] = (),
                 precedence: Iterable[str] | None = None):
        if mode not in MODES:
            raise AgentFrontierError.invalid_config("reconcile mode must be one of " + J.jlist(MODES))
        self.mode = mode
        self.engines = list(engines)
        self.precedence = None if precedence is None else list(precedence)
        self._by_dataset: dict[tuple, dict[tuple, list[dict]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _dataset_key(source: Any, levels: Iterable[tuple], dataset: Any) -> tuple:
        return (source, tuple(sorted((k, J.jstr(v)) for k, v in levels)), dataset)

    def add(self, records: Iterable[Record | dict]) -> None:
        with self._lock:
            for r in records:
                lab = as_label(r)
                if lab is None:
                    continue
                key = object_key(lab)
                dk = (key[0], key[1], key[3])
                self._by_dataset.setdefault(dk, {}).setdefault(key, []).append(lab)

    def labels(self, source: Any, levels: dict, dataset: Any, field: Any = None) -> list[str]:
        """Effective label names of a field, or of a dataset (its own and all its fields') when ``field`` is None."""
        with self._lock:
            objs = self._by_dataset.get(self._dataset_key(source, levels.items(), dataset))
            if not objs:
                return []
            labs = [lab for k, ls in objs.items() if field is None or k[4] == field for lab in ls]
        return reconcile(labs, self.mode, self.engines, self.precedence).names()

    def field_labels(self, source: Any, levels: dict, dataset: Any) -> dict[str, list[str]]:
        """Effective label names of every labelled field of a dataset."""
        with self._lock:
            objs = self._by_dataset.get(self._dataset_key(source, levels.items(), dataset)) or {}
            fields = {k[4] for k in objs if k[4] is not None}
        return {f: self.labels(source, levels, dataset, f) for f in sorted(fields)}
