"""The semantic cache contract: how cache keys are derived, what a hit must prove before it is served, and which
events invalidate an entry. Products build the store and its metrics (decision D4); the SDK fixes the rules so
every store behaves the same.

* :class:`CacheScope`: who asks, for what purpose, about which source, and the versions an answer depends on
  (schema, policy, model, embedding). Two requests share an entry only when all of it matches.
* :func:`cache_key`: a SHA-256 over the kind, the scope and the normalised request, in canonical JSON.
* :class:`CacheEntry`: the value plus what it depends on (resources such as ``retail:sales.orders``) and when it
  expires.
* :class:`CacheStore`: the store protocol. :class:`MemoryCacheStore` is the reference implementation.
* :class:`SemanticCache`: lookups with **mandatory revalidation**: a hit is served only when its versions still
  match the current ones, it has not expired, and the caller's ``revalidate`` check (typically a fresh policy
  decision) passes. Otherwise it counts as stale and is dropped.
* :meth:`SemanticCache.on_event`: change events (a dataset changed, a crawl finished) drop the entries that depend
  on the resource; policy events drop every entry made under another policy version.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

__all__ = ["CacheScope", "CacheEntry", "CacheStore", "MemoryCacheStore", "SemanticCache", "CacheStats", "cache_key",
           "normalise_intent", "KINDS"]

KINDS = ("result", "context", "artifact")


def normalise_intent(text: str) -> str:
    """Case, whitespace and trailing punctuation do not change what is asked."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    return re.sub(r"[?.!;]+$", "", t).strip()


@dataclass(frozen=True)
class CacheScope:
    tenant: str = "local"
    principal: str = "anonymous"
    purpose: str = "default"
    source: str | None = None
    schema_version: str | None = None
    policy_version: str | None = None
    model_version: str | None = None
    embedding_version: str | None = None

    def versions(self) -> dict[str, str | None]:
        return {"schema": self.schema_version, "policy": self.policy_version, "model": self.model_version,
                "embedding": self.embedding_version}

    def to_json(self) -> dict:
        return {"tenant": self.tenant, "principal": self.principal, "purpose": self.purpose, "source": self.source,
                "versions": self.versions()}


def _canonical(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def cache_key(kind: str, scope: CacheScope, request: Any) -> str:
    """The key of a request: kind, scope (versions included) and the request, in canonical JSON. A string request
    is normalised as an intent."""
    if kind not in KINDS:
        raise ValueError("cache kind must be one of " + ", ".join(KINDS))
    req = normalise_intent(request) if isinstance(request, str) else request
    return hashlib.sha256(_canonical({"kind": kind, "scope": scope.to_json(), "request": req}).encode()).hexdigest()


@dataclass
class CacheEntry:
    key: str
    kind: str
    value: Any
    scope: CacheScope
    depends_on: frozenset[str] = frozenset()
    created_at: float = field(default_factory=time.time)
    ttl_s: float | None = None
    hits: int = 0

    def expired(self, now: float | None = None) -> bool:
        return self.ttl_s is not None and (now or time.time()) - self.created_at > self.ttl_s

    def to_json(self) -> dict:
        return {"key": self.key, "kind": self.kind, "scope": self.scope.to_json(), "dependsOn": sorted(self.depends_on),
                "createdAt": self.created_at, "ttlS": self.ttl_s, "hits": self.hits}


class CacheStore(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def put(self, entry: CacheEntry) -> None: ...

    def delete(self, key: str) -> None: ...

    def entries(self) -> Iterable[CacheEntry]: ...


class MemoryCacheStore:
    """Entries in memory, least recently used evicted beyond ``max_entries``."""

    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self._d: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            e = self._d.pop(key, None)
            if e is not None:
                self._d[key] = e  # most recently used last
            return e

    def put(self, entry: CacheEntry) -> None:
        with self._lock:
            self._d.pop(entry.key, None)
            self._d[entry.key] = entry
            while len(self._d) > self.max_entries:
                self._d.pop(next(iter(self._d)))

    def delete(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)

    def entries(self) -> list[CacheEntry]:
        with self._lock:
            return list(self._d.values())


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stale: int = 0
    puts: int = 0
    invalidated: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)

    def count(self, kind: str, what: str) -> None:
        setattr(self, what, getattr(self, what) + 1)
        k = self.by_kind.setdefault(kind, {"hits": 0, "misses": 0, "stale": 0, "puts": 0})
        if what in k:
            k[what] += 1

    def to_json(self) -> dict:
        lookups = self.hits + self.misses + self.stale
        return {"hits": self.hits, "misses": self.misses, "stale": self.stale, "puts": self.puts,
                "invalidated": self.invalidated, "hitRate": round(self.hits / lookups, 3) if lookups else None,
                "byKind": self.by_kind}


class SemanticCache:
    """Lookups and writes under the contract, over any :class:`CacheStore`."""

    def __init__(self, store: CacheStore | None = None, default_ttl_s: float | None = None):
        self.store: CacheStore = store or MemoryCacheStore()
        self.default_ttl_s = default_ttl_s
        self.stats = CacheStats()

    def get(self, kind: str, scope: CacheScope, request: Any,
            revalidate: Callable[[CacheEntry], bool] | None = None) -> CacheEntry | None:
        """The entry, only if it is still valid: same versions (they are in the key), not expired, and
        ``revalidate`` passes. A stale entry is removed and reported as stale."""
        key = cache_key(kind, scope, request)
        e = self.store.get(key)
        if e is None:
            self.stats.count(kind, "misses")
            return None
        ok = not e.expired() and e.scope.versions() == scope.versions()
        if ok and revalidate is not None:
            try:
                ok = bool(revalidate(e))
            except Exception:  # noqa: BLE001 - a check that fails is a failed check
                ok = False
        if not ok:
            self.store.delete(key)
            self.stats.count(kind, "stale")
            return None
        e.hits += 1
        self.stats.count(kind, "hits")
        return e

    def put(self, kind: str, scope: CacheScope, request: Any, value: Any, depends_on: Iterable[str] = (),
            ttl_s: float | None = None) -> CacheEntry:
        e = CacheEntry(cache_key(kind, scope, request), kind, value, scope, frozenset(depends_on),
                       ttl_s=self.default_ttl_s if ttl_s is None else ttl_s)
        self.store.put(e)
        self.stats.count(kind, "puts")
        return e

    def on_event(self, event: dict) -> int:
        """Invalidation. ``{"type": "change", "resources": [...]}`` drops entries that depend on any of them (a
        resource ending in ``*`` is a prefix: ``retail:*`` is the whole source); ``{"type": "policy", "version":
        v}`` drops entries made under another policy version. Returns how many were dropped."""
        typ = event.get("type")
        drop: list[str] = []
        if typ == "change":
            res = [str(r) for r in event.get("resources") or []]
            exact = {r for r in res if not r.endswith("*")}
            prefixes = [r[:-1] for r in res if r.endswith("*")]
            for e in self.store.entries():
                if e.depends_on & exact or any(d.startswith(p) for d in e.depends_on for p in prefixes):
                    drop.append(e.key)
        elif typ == "policy":
            v = event.get("version")
            drop = [e.key for e in self.store.entries() if e.scope.policy_version != v]
        else:
            raise ValueError("cache event type must be change or policy")
        for k in drop:
            self.store.delete(k)
        self.stats.invalidated += len(drop)
        return len(drop)

    def summary(self) -> dict:
        entries = list(self.store.entries())
        by_kind: dict[str, int] = {}
        for e in entries:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {**self.stats.to_json(), "entries": len(entries), "entriesByKind": by_kind}
