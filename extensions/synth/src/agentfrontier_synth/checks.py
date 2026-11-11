"""Validation of a dataset against its spec (keys, uniqueness, nullability, accepted values, ranges, foreign keys)
and privacy and utility reports: k-anonymity over quasi-identifiers, exact-match leakage against a sample of real
rows, and distribution distance against a reference (Kolmogorov-Smirnov for numbers, total variation for
categories)."""

from __future__ import annotations

import bisect
import datetime as dt
from collections import Counter
from decimal import Decimal
from typing import Any, Iterable

from .spec import FixtureSpec

__all__ = ["validate_dataset", "k_anonymity", "exact_match_share", "ks_distance", "tv_distance", "utility_report"]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return float(v.toordinal() if isinstance(v, dt.date) and not isinstance(v, dt.datetime) else v.timestamp())
    return None


def validate_dataset(spec: FixtureSpec, data: dict[str, list[dict]]) -> dict:
    """Every constraint of the spec, checked on the data: ``{"ok", "checks": [{name, table, status, failures}]}``."""
    checks: list[dict] = []

    def add(name: str, table: str, failures: int, detail: str = "") -> None:
        checks.append({"name": name, "table": table, "status": "pass" if failures == 0 else "fail",
                       "failures": failures, **({"detail": detail} if detail else {})})
    for t in spec.tables:
        rows = data.get(t.name, [])
        if t.primary_key:
            keys = [tuple(r.get(c) for c in t.primary_key) for r in rows]
            add("primary_key " + ",".join(t.primary_key), t.name,
                (len(keys) - len(set(keys))) + sum(1 for k in keys if any(v is None for v in k)))
        for c in t.columns:
            vals = [r.get(c.name) for r in rows]
            if not c.nullable:
                add("not_null " + c.name, t.name, sum(1 for v in vals if v is None))
            if c.unique:
                nn = [v for v in vals if v is not None]
                add("unique " + c.name, t.name, len(nn) - len(set(nn)))
            if c.values:
                allowed = set(c.values)
                add("accepted_values " + c.name, t.name, sum(1 for v in vals if v is not None and v not in allowed))
            lo, hi = _num(c.min) if c.min is not None else None, _num(c.max) if c.max is not None else None
            if lo is not None or hi is not None:
                bad = 0
                for v in vals:
                    n = _num(v)
                    if n is not None and ((lo is not None and n < lo) or (hi is not None and n > hi)):
                        bad += 1
                add("range " + c.name, t.name, bad)
        for cols, parent, refs in t.foreign_keys:
            prow = data.get(parent)
            if prow is None:
                add("foreign_key " + ",".join(cols), t.name, len(rows), parent + " missing")
                continue
            have = {tuple(p.get(r) for r in refs) for p in prow}
            missing = sum(1 for r in rows if all(r.get(c) is not None for c in cols)
                          and tuple(r.get(c) for c in cols) not in have)
            add("foreign_key " + ",".join(cols) + " -> " + parent, t.name, missing)
    return {"ok": all(c["status"] == "pass" for c in checks), "checks": checks}


def k_anonymity(rows: Iterable[dict], quasi_identifiers: list[str]) -> dict:
    """The smallest group of rows sharing the same quasi-identifier values (k), and how many groups are below 5."""
    groups = Counter(tuple(r.get(q) for q in quasi_identifiers) for r in rows)
    if not groups:
        return {"k": None, "groups": 0, "groupsBelow5": 0, "quasiIdentifiers": quasi_identifiers}
    return {"k": min(groups.values()), "groups": len(groups), "groupsBelow5": sum(1 for v in groups.values() if v < 5),
            "quasiIdentifiers": quasi_identifiers}


def exact_match_share(synthetic: list[dict], real: list[dict], columns: list[str]) -> float:
    """Share of synthetic rows identical to a real row on the given columns (0 means no row was reproduced)."""
    if not synthetic:
        return 0.0
    seen = {tuple(str(r.get(c)) for c in columns) for r in real}
    return round(sum(1 for r in synthetic if tuple(str(r.get(c)) for c in columns) in seen) / len(synthetic), 4)


def ks_distance(a: list[float], b: list[float]) -> float | None:
    """Two-sample Kolmogorov-Smirnov statistic: 0 for identical distributions, 1 for disjoint ones."""
    a, b = sorted(a), sorted(b)
    if not a or not b:
        return None
    d = 0.0
    for x in sorted(set(a) | set(b)):
        fa = bisect.bisect_right(a, x) / len(a)
        fb = bisect.bisect_right(b, x) / len(b)
        d = max(d, abs(fa - fb))
    return round(d, 4)


def tv_distance(a: Iterable[Any], b: Iterable[Any]) -> float | None:
    """Total variation distance between two categorical distributions (0 identical, 1 disjoint)."""
    ca, cb = Counter(a), Counter(b)
    na, nb = sum(ca.values()), sum(cb.values())
    if not na or not nb:
        return None
    return round(0.5 * sum(abs(ca[k] / na - cb[k] / nb) for k in set(ca) | set(cb)), 4)


def utility_report(synthetic: list[dict], reference: list[dict], columns: list[str]) -> dict:
    """Per column, how close the synthetic distribution is to the reference one."""
    out = {}
    for c in columns:
        sa = [r.get(c) for r in synthetic if r.get(c) is not None]
        ra = [r.get(c) for r in reference if r.get(c) is not None]
        nums_s = [n for n in (_num(v) for v in sa) if n is not None]
        nums_r = [n for n in (_num(v) for v in ra) if n is not None]
        if nums_s and nums_r and len(nums_s) == len(sa):
            out[c] = {"metric": "ks", "distance": ks_distance(nums_s, nums_r)}
        else:
            out[c] = {"metric": "total_variation", "distance": tv_distance(map(str, sa), map(str, ra))}
    return out
