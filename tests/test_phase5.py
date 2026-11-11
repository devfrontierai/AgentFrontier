"""Phase 5 contracts in the core: the semantic cache (keys, mandatory revalidation, invalidation by change and policy
events) and provenance manifests."""

from __future__ import annotations

import time

import pytest

from agentfrontier.cache import CacheScope, MemoryCacheStore, SemanticCache, cache_key, normalise_intent
from agentfrontier.provenance import Manifest, sha256_text


def test_keys_cover_scope_versions_and_normalised_intent() -> None:
    s = CacheScope("t1", "alice", "analysis", "retail", "crawl-1", "p1", "claude-opus-5-5")
    assert cache_key("result", s, "How many orders?") == cache_key("result", s, "  how many ORDERS ")
    assert normalise_intent("Revenue by month?!") == "revenue by month"
    other = [CacheScope("t1", "bob", "analysis", "retail", "crawl-1", "p1", "claude-opus-5-5"),
             CacheScope("t1", "alice", "support", "retail", "crawl-1", "p1", "claude-opus-5-5"),
             CacheScope("t1", "alice", "analysis", "retail", "crawl-2", "p1", "claude-opus-5-5"),
             CacheScope("t1", "alice", "analysis", "retail", "crawl-1", "p2", "claude-opus-5-5")]
    assert len({cache_key("result", s, "q")} | {cache_key("result", o, "q") for o in other}) == 5
    assert cache_key("result", s, "q") != cache_key("context", s, "q")
    with pytest.raises(ValueError):
        cache_key("rows", s, "q")


def test_hits_are_revalidated_and_stale_entries_dropped() -> None:
    c = SemanticCache(MemoryCacheStore(10))
    s = CacheScope(principal="alice", source="retail", schema_version="r1", policy_version="p1")
    assert c.get("result", s, "q") is None
    c.put("result", s, "q", {"rows": 3}, depends_on=["retail:sales.orders"])
    e = c.get("result", s, "q", revalidate=lambda e: True)
    assert e is not None and e.value == {"rows": 3} and e.hits == 1
    assert c.get("result", s, "q", revalidate=lambda e: False) is None  # policy re-check failed: stale, dropped
    assert c.get("result", s, "q") is None
    c.put("result", s, "q2", 1, ttl_s=0.01)
    time.sleep(0.03)
    assert c.get("result", s, "q2") is None
    st = c.summary()
    assert (st["hits"], st["stale"], st["misses"]) == (1, 2, 2)


def test_change_and_policy_events_invalidate() -> None:
    c = SemanticCache()
    s1 = CacheScope(source="retail", policy_version="p1")
    c.put("result", s1, "a", 1, depends_on=["retail:sales.orders"])
    c.put("result", s1, "b", 2, depends_on=["retail:sales.customers"])
    c.put("context", s1, "c", 3, depends_on=["warehouse:x.y"])
    assert c.on_event({"type": "change", "resources": ["retail:sales.orders"]}) == 1
    assert c.on_event({"type": "change", "resources": ["retail:*"]}) == 1
    assert c.on_event({"type": "policy", "version": "p2"}) == 1
    assert c.summary()["entries"] == 0 and c.stats.invalidated == 3
    with pytest.raises(ValueError):
        c.on_event({"type": "other"})


def test_lru_eviction() -> None:
    st = MemoryCacheStore(2)
    c = SemanticCache(st)
    s = CacheScope()
    c.put("result", s, "a", 1)
    c.put("result", s, "b", 2)
    c.get("result", s, "a")
    c.put("result", s, "c", 3)
    assert c.get("result", s, "b") is None and c.get("result", s, "a") is not None


def test_provenance_manifest() -> None:
    m = Manifest("sql", "Monthly revenue")
    m.add_input("table", "retail:sales.orders", version="crawl-7").add_input("question", "q", "monthly revenue")
    m.add_output("analyses/monthly.sql", "select 1").add_tool("validate_sql", "1.0.0")
    m.set_model("anthropic", "claude-opus-5-5", inputTokens=10)
    m.set_validation("static", [{"name": "parse", "status": "pass"}])
    before = m.digest()
    m.approve("pankaj")
    d = m.to_json()
    assert d["digest"] == before and d["approvals"][0]["digest"] == before  # approving does not change the digest
    assert d["inputs"][1]["hash"] == sha256_text("monthly revenue") and "content" not in d["inputs"][1]
    assert Manifest.from_json(d).digest() == before
    with pytest.raises(ValueError):
        m.set_validation("ok", [])
