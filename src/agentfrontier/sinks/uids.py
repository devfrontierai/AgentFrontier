"""Stable identifiers for records, shared by graph-style sinks in every implementation:

* source: ``<source>``; container: ``<source>/<level values down to it>``;
* dataset: ``<source>/<level values>/<name>``; field: ``<dataset uid>/<name>``;
* index: ``<dataset uid>#index/<name>``; constraint: ``<dataset uid>#constraint/<name>``;
* routine: ``<container uid>#routine/<name>``; policy: ``<dataset or container uid>#policy/<name>``;
* principal: ``<source>/principal/<name>``.

A reference to another dataset (foreign key target, lineage upstream) uses ``ref<Level>`` / ``upstream<Level>``
values, falling back to the record's own level values.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .. import json_util as J
from ..model import Record


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _join(parts: list) -> str:
    return "/".join(J.jstr(p) for p in parts)


def row(r: Record, levels: list[str]) -> dict:
    """The helper values a graph mapping receives for a record, plus ``props``."""
    out: dict[str, Any] = {}
    source = r.string("source")
    path = path_of(source, r, levels, "")
    dataset = r.string("dataset")
    dataset_uid = None if dataset is None else path + "/" + dataset
    e = r.entity
    if e == "source":
        out["uid"] = source
    elif e == "container":
        level = r.string("level")
        parts: list = [source]
        for lv in levels:
            v = r.string(lv)
            if v is None:
                break
            parts.append(v)
            if lv == level:
                break
        out["uid"] = _join(parts)
        out["parentUid"] = _join(parts[:max(1, len(parts) - 1)])
    elif e == "dataset":
        out["uid"] = path + "/" + J.jstr(r.string("name"))
        out["parentUid"] = path
    elif e == "field":
        out["datasetUid"] = dataset_uid
        out["uid"] = J.jstr(dataset_uid) + "/" + J.jstr(r.string("name"))
    elif e == "index":
        out["datasetUid"] = dataset_uid
        out["uid"] = J.jstr(dataset_uid) + "#index/" + J.jstr(r.string("name"))
    elif e == "constraint":
        out["datasetUid"] = dataset_uid
        out["uid"] = J.jstr(dataset_uid) + "#constraint/" + J.jstr(r.string("name"))
        out["columnUids"] = _children(J.jstr(dataset_uid), r.strings("columns"))
        ref_dataset = r.string("refDataset")
        if ref_dataset is not None:
            ref_uid = path_of(source, r, levels, "ref") + "/" + ref_dataset
            out["refUid"] = ref_uid
            out["refColumnUids"] = _children(ref_uid, r.strings("refColumns"))
    elif e == "lineage":
        out["datasetUid"] = dataset_uid
        out["upstreamUid"] = path_of(source, r, levels, "upstream") + "/" + J.jstr(r.string("upstreamDataset"))
    elif e == "routine":
        out["parentUid"] = path
        out["uid"] = path + "#routine/" + J.jstr(r.string("name"))
    elif e == "principal":
        out["uid"] = J.jstr(source) + "/principal/" + J.jstr(r.string("name"))
    elif e == "grant":
        out["principalUid"] = J.jstr(source) + "/principal/" + J.jstr(r.string("principal"))
        out["datasetUid"] = dataset_uid
    elif e == "policy":
        out["datasetUid"] = dataset_uid
        out["uid"] = (dataset_uid if dataset_uid is not None else path) + "#policy/" + J.jstr(r.string("name"))
    elif e in ("tag", "statistic"):
        out["datasetUid"] = dataset_uid
        f = r.string("field")
        if f is not None and dataset_uid is not None:
            out["fieldUid"] = dataset_uid + "/" + f
    else:
        out["uid"] = path + "#" + e + "/" + J.jstr(r.string("name"))
    out["props"] = props(r)
    return out


def path_of(source: str | None, r: Record, levels: list[str], prefix: str) -> str:
    """``source/level values``, from ``prefix<Level>`` when present else the record's own levels."""
    parts: list = [source]
    for lv in levels:
        v = r.string(prefix + _cap(lv)) if prefix else None
        if v is None:
            v = r.string(lv)
        if v is not None:
            parts.append(v)
    return _join(parts)


def _children(parent: str, names: list) -> list[str]:
    return [parent + "/" + J.jstr(n) for n in names]


def props(r: Record) -> dict:
    """Graph-friendly properties: scalars and lists of scalars as they are, maps as JSON text, extras from
    ``properties`` merged in without overriding canonical values, nulls dropped."""
    out: dict[str, Any] = {"entity": r.entity}
    for k, v in r.values.items():
        if k != "properties":
            _put(out, k, v)
    for k, v in r.properties.items():
        if k not in out:
            _put(out, k, v)
    return out


def _put(out: dict, k: str, v: Any) -> None:
    if v is None:
        return
    if isinstance(v, dict):
        out[k] = J.write(v)
    elif isinstance(v, (list, tuple, set)):
        scalars = not any(isinstance(o, (dict, list, tuple, set)) for o in v)
        out[k] = list(v) if scalars else J.write(v)
    elif isinstance(v, Decimal):
        out[k] = float(v)
    else:
        out[k] = v


class Uids:
    row = staticmethod(row)
    path = staticmethod(path_of)
    props = staticmethod(props)
