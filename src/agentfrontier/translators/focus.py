"""FOCUS 1.1-aligned CSV rows for usage and cost records (spec docs/translators.md, "focus")."""

from __future__ import annotations

from .. import json_util as J
from ..model import Record
from ..sinks.files import cell, csv_line
from . import TranslationContext, month_bounds, upserts

COLUMNS = ["BilledCost", "BillingAccountId", "BillingCurrency", "BillingPeriodEnd", "BillingPeriodStart",
           "ChargeCategory", "ChargePeriodEnd", "ChargePeriodStart", "ConsumedQuantity", "ConsumedUnit",
           "EffectiveCost", "InvoiceIssuerName", "ProviderName", "PublisherName", "RegionId", "ResourceId",
           "ServiceName", "SubAccountId", "x_CachedInputTokens", "x_CacheWriteTokens", "x_InputTokens",
           "x_InvoiceStatus", "x_Model", "x_OutputTokens", "x_Requests"]
_FROM = {"BilledCost": "billedCost", "BillingAccountId": "billingAccountId", "BillingCurrency": "billingCurrency",
         "ChargePeriodEnd": "chargePeriodEnd", "ChargePeriodStart": "chargePeriodStart",
         "ConsumedQuantity": "consumedQuantity", "ConsumedUnit": "consumedUnit", "EffectiveCost": "effectiveCost",
         "InvoiceIssuerName": "provider", "ProviderName": "provider", "PublisherName": "provider",
         "RegionId": "regionId", "ResourceId": "resourceId", "ServiceName": "serviceName",
         "SubAccountId": "subAccountId", "x_CachedInputTokens": "cachedInputTokens",
         "x_CacheWriteTokens": "cacheWriteTokens", "x_InputTokens": "inputTokens", "x_InvoiceStatus": "invoiceStatus",
         "x_Model": "model", "x_OutputTokens": "outputTokens", "x_Requests": "requests"}


def translate(records: list[Record], ctx: TranslationContext) -> str:
    lines = [csv_line(COLUMNS)]
    for i in upserts(records, "usage_cost"):
        v = i.v
        row = {c: v.get(f) for c, f in _FROM.items()}
        cat = v.get("chargeCategory")
        row["ChargeCategory"] = None if cat is None else J.jstr(cat)[:1].upper() + J.jstr(cat)[1:]
        if v.get("chargePeriodStart"):
            row["BillingPeriodStart"], row["BillingPeriodEnd"] = month_bounds(J.jstr(v["chargePeriodStart"]))
        lines.append(csv_line([cell(row.get(c)) for c in COLUMNS]))
    return "".join(lines)
