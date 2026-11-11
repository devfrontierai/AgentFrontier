"""Masking operators for real data that has to leave its system: redaction, salted hashing, keyed tokenization
(deterministic, optionally reversible through a :class:`TokenVault`), format-preserving pseudonymisation and
generalisation.

``format_preserving`` keeps the shape of a value (digits stay digits, letters stay letters, separators stay) using a
keyed substitution. It is a pseudonymisation for test data, **not** NIST FF1/FF3 encryption: do not use it where a
standard FPE scheme is required. The token vault here is in memory; a durable vault is an Enterprise Edition
component (decision D2).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import threading
from typing import Any, Callable, Protocol

__all__ = ["TokenVault", "MemoryTokenVault", "redact", "hash_value", "tokenize", "detokenize", "format_preserving",
           "generalise", "mask_rows", "METHODS"]


class TokenVault(Protocol):
    def put(self, token: str, value: Any) -> None: ...

    def get(self, token: str) -> Any: ...


class MemoryTokenVault:
    def __init__(self) -> None:
        self._d: dict[str, Any] = {}
        self._lock = threading.Lock()

    def put(self, token: str, value: Any) -> None:
        with self._lock:
            self._d[token] = value

    def get(self, token: str) -> Any:
        with self._lock:
            if token not in self._d:
                raise KeyError("unknown token")
            return self._d[token]


def _key(key: str | bytes) -> bytes:
    return key.encode() if isinstance(key, str) else key


def redact(v: Any, keep: int = 0) -> Any:
    if v is None:
        return None
    s = str(v)
    return "*" * max(0, len(s) - keep) + (s[-keep:] if keep else "")


def hash_value(v: Any, salt: str = "") -> Any:
    if v is None:
        return None
    return hashlib.sha256((salt + str(v)).encode()).hexdigest()[:16]


def tokenize(v: Any, key: str | bytes, vault: TokenVault | None = None, prefix: str = "tok_") -> Any:
    """The same value and key always give the same token; with a vault the token can be reversed."""
    if v is None:
        return None
    mac = hmac.new(_key(key), str(v).encode(), hashlib.sha256).digest()
    t = prefix + base64.b32encode(mac[:10]).decode().lower()
    if vault is not None:
        vault.put(t, v)
    return t


def detokenize(token: str, vault: TokenVault) -> Any:
    return vault.get(token)


def format_preserving(v: Any, key: str | bytes) -> Any:
    """Digits to digits, letters to letters (case kept), everything else unchanged; keyed and deterministic."""
    if v is None:
        return None
    s = str(v)
    out = []
    for i, ch in enumerate(s):
        d = hmac.new(_key(key), (str(i) + ":" + s).encode(), hashlib.sha256).digest()[0]
        if ch.isdigit():
            out.append(str((int(ch) + d) % 10))
        elif ch.isalpha() and ch.isascii():
            base = ord("A") if ch.isupper() else ord("a")
            out.append(chr(base + (ord(ch) - base + d) % 26))
        else:
            out.append(ch)
    return "".join(out)


def generalise(v: Any, level: str = "month", bucket: float = 10) -> Any:
    """Dates to their month or year; numbers to the start of their bucket."""
    if v is None:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        d = v.date() if isinstance(v, dt.datetime) else v
        return d.replace(day=1) if level == "month" else d.replace(month=1, day=1)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (v // bucket) * bucket
    return v


METHODS: dict[str, Callable[..., Any]] = {"redact": redact, "hash": hash_value, "tokenize": tokenize,
                                          "format_preserving": format_preserving, "generalise": generalise}


def mask_rows(rows: list[dict], plan: dict[str, str], key: str = "", vault: TokenVault | None = None) -> list[dict]:
    """Rows with the planned columns masked: ``{"email": "tokenize", "phone": "format_preserving"}``."""
    out = []
    for r in rows:
        m = dict(r)
        for col, method in plan.items():
            if col not in m:
                continue
            if method == "tokenize":
                m[col] = tokenize(m[col], key, vault)
            elif method == "format_preserving":
                m[col] = format_preserving(m[col], key)
            elif method == "hash":
                m[col] = hash_value(m[col], key)
            elif method == "redact":
                m[col] = redact(m[col])
            elif method == "generalise":
                m[col] = generalise(m[col])
            else:
                raise ValueError("unknown masking method " + method)
        out.append(m)
    return out
