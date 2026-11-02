"""An OPA data document for the authority graph (spec docs/translators.md, "opa")."""

from __future__ import annotations

from .. import json_util as J
from ..model import Record
from . import TranslationContext, level_values, upserts


def translate(records: list[Record], ctx: TranslationContext) -> dict:
    principals: dict[str, dict] = {}
    memberships: dict[str, set[str]] = {}
    grants: list[dict] = []
    delegations: list[dict] = []
    for i in upserts(records, "principal", "grant", "delegation", "relationship"):
        v = i.v
        if i.entity == "principal":
            name = J.jstr(v.get("name"))
            p = {"type": v.get("type")}
            for k in ("canLogin", "superuser"):
                if v.get(k) is not None:
                    p[k] = v[k]
            principals[name] = {k: x for k, x in p.items() if x is not None}
            for g in v.get("memberOf") or []:
                memberships.setdefault(name, set()).add(J.jstr(g))
        elif i.entity == "relationship" and v.get("predicate") == "member_of":
            memberships.setdefault(J.jstr(v.get("subject")), set()).add(J.jstr(v.get("object")))
        elif i.entity == "grant":
            parts = level_values(v, ctx.levels)
            if v.get("dataset") is not None:
                parts.append(J.jstr(v["dataset"]))
                if v.get("field") is not None:
                    parts.append(J.jstr(v["field"]))
            g = {"principal": J.jstr(v.get("principal")), "privilege": J.jstr(v.get("privilege")),
                 "objectType": v.get("objectType"), "object": ".".join(parts)}
            grants.append({k: x for k, x in g.items() if x is not None})
        elif i.entity == "delegation":
            d = {"principal": J.jstr(v.get("principal")), "delegate": J.jstr(v.get("delegate"))}
            for k in ("scope", "validFrom", "validTo"):
                if v.get(k) is not None:
                    d[k] = v[k]
            delegations.append(d)
    return {"principals": {k: principals[k] for k in sorted(principals)},
            "memberships": {k: sorted(memberships[k]) for k in sorted(memberships)},
            "grants": sorted(grants, key=lambda g: (g["object"], g["principal"], g["privilege"])),
            "delegations": sorted(delegations, key=lambda d: (d["principal"], d["delegate"]))}
