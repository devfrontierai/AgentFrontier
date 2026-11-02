"""Translators: the records of one run as documents other systems accept (spec docs/translators.md). Pure functions
without a clock or randomness; ``conformance/translators`` holds the vectors every edition reproduces.

    from agentfrontier.translators import TranslationContext, translate
    events = translate("openlineage", records, TranslationContext(source="shop", definition="postgresql", ...))
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .. import json_util as J
from ..errors import AgentFrontierError
from ..model import Record

PRODUCER = "https://github.com/devfrontierai/AgentFrontier"


@dataclass
class TranslationContext:
    source: str
    definition: str | None = None
    levels: list[str] = field(default_factory=list)
    run_id: str = ""
    tenant: str = "default"
    started_at: str | None = None
    finished_at: str | None = None
    options: dict = field(default_factory=dict)

    def option(self, key: str, default: Any = None) -> Any:
        v = self.options.get(key)
        return default if v is None or v == "" else v

    @staticmethod
    def from_json(doc: dict) -> "TranslationContext":
        return TranslationContext(J.jstr(doc.get("source")), doc.get("definition"), J.strings(doc.get("levels")),
                                  J.jstr(doc.get("runId") or ""), doc.get("tenant") or "default",
                                  doc.get("startedAt"), doc.get("finishedAt"), dict(doc.get("options") or {}))


@dataclass
class Item:
    """One input: the record, its op and its envelope (empty for plain records)."""

    record: Record
    op: str
    envelope: dict

    @property
    def entity(self) -> str:
        return self.record.entity

    @property
    def v(self) -> dict:
        return self.record.values


def items(records: Iterable[Record]) -> list[Item]:
    out = []
    for r in records:
        env = getattr(r, "envelope", None) or {}
        rec = getattr(r, "record", r)
        out.append(Item(rec, J.jstr(env.get("op", "upsert")), env))
    return out


def upserts(records: Iterable[Record], *entities: str) -> list[Item]:
    return [i for i in items(records) if i.op == "upsert" and (not entities or i.entity in entities)]


# -------------------------------------------------------------------------------------------------------- names


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def level_values(v: dict, levels: list[str], prefix: str = "") -> list[str]:
    out = []
    for lv in levels:
        x = v.get(prefix + _cap(lv)) if prefix else None
        if x is None:
            x = v.get(lv)
        if x is not None:
            out.append(J.jstr(x))
    return out


def qualified(v: dict, levels: list[str], name_key: str = "dataset", prefix: str = "") -> str:
    """Level values and the dataset name joined with dots (``upstream`` / ``ref`` prefixes for other datasets)."""
    return ".".join(level_values(v, levels, prefix) + [J.jstr(v.get(name_key))])


def det_uuid(*parts: str) -> str:
    """UUID version 8 from SHA-256 (docs/translators.md, "Deterministic ids")."""
    b = bytearray(hashlib.sha256(("af-uuid/1|" + "|".join(parts)).encode("utf-8")).digest()[:16])
    b[6] = (b[6] & 0x0F) | 0x80
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]


def epoch_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    from ..template import iso_to_epoch_ms
    return iso_to_epoch_ms(iso)


def month_bounds(iso: str) -> tuple[str, str]:
    """First day of the month of ``iso`` and of the next month, ``YYYY-MM-01T00:00:00Z``."""
    t = _dt.datetime.fromtimestamp(epoch_ms(iso) / 1000, tz=_dt.timezone.utc)  # type: ignore[operator]
    start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), nxt.strftime(fmt)


# -------------------------------------------------------------------------------------------------------- registry

Translator = Callable[[list[Record], TranslationContext], Any]


def _registry() -> dict[str, Translator]:
    from . import datahub, focus, ocsf, opa, openlineage, openmetadata, otel
    return {"openlineage": openlineage.translate, "openmetadata": openmetadata.translate,
            "datahub": datahub.translate, "opa": opa.translate, "otel": otel.translate, "focus": focus.translate,
            "ocsf": ocsf.translate}


def names() -> list[str]:
    return sorted(_registry())


def translate(name: str, records: Iterable[Record], context: TranslationContext) -> Any:
    t = _registry().get(name)
    if t is None:
        raise AgentFrontierError.invalid_config("unknown translator '" + name + "' (known: " + ", ".join(names()) + ")")
    return t(list(records), context)


__all__ = ["TranslationContext", "translate", "names", "PRODUCER", "det_uuid", "qualified", "level_values", "items",
           "upserts", "Item"]
