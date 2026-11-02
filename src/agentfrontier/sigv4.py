"""AWS Signature Version 4 (spec docs/auth.md, "aws_sigv4"); conformance/auth/sigv4.json holds the test vectors."""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re
import urllib.parse
from dataclasses import dataclass

ALGORITHM = "AWS4-HMAC-SHA256"
_UNRESERVED = "-_.~"
_SPACES = re.compile(r" +")


@dataclass(frozen=True)
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


def _encode(s: str) -> str:
    return urllib.parse.quote(s, safe=_UNRESERVED)


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def canonical_uri(path: str, service: str) -> str:
    if not path:
        return "/"
    encoded = "/".join(_encode(urllib.parse.unquote(seg)) for seg in path.split("/"))
    if service != "s3":
        encoded = "/".join(_encode(seg) for seg in encoded.split("/"))
    return encoded if encoded.startswith("/") else "/" + encoded


def canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if part == "":
            continue
        k, _, v = part.partition("=")
        pairs.append((_encode(urllib.parse.unquote_plus(k)), _encode(urllib.parse.unquote_plus(v))))
    pairs.sort()
    return "&".join(k + "=" + v for k, v in pairs)


def _host(parts: urllib.parse.SplitResult) -> str:
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        host += ":" + str(port)
    return host


def amz_date(now: _dt.datetime) -> str:
    return now.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sign(method: str, url: str, headers: dict[str, str], body: bytes | None, creds: Credentials, region: str,
         service: str, now: _dt.datetime | None = None) -> dict[str, str]:
    """The headers to add to the request: ``X-Amz-Date``, ``X-Amz-Security-Token`` with a session token,
    ``X-Amz-Content-Sha256`` for S3, and ``Authorization``. ``headers`` are the request's own headers."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    stamp = amz_date(now)
    day = stamp[:8]
    payload_hash = _sha256_hex(body or b"")
    added: dict[str, str] = {"X-Amz-Date": stamp}
    if creds.session_token:
        added["X-Amz-Security-Token"] = creds.session_token
    if service == "s3":
        added["X-Amz-Content-Sha256"] = payload_hash
    parts = urllib.parse.urlsplit(url)
    signed: dict[str, str] = {"host": _host(parts)}
    for name, value in list(headers.items()) + list(added.items()):
        low = name.lower()
        if low.startswith("x-amz-") or low in ("range", "content-type"):
            signed[low] = _SPACES.sub(" ", str(value).strip())
    names = sorted(signed)
    canonical = "\n".join([
        method.upper(),
        canonical_uri(parts.path, service),
        canonical_query(parts.query),
        "".join(n + ":" + signed[n] + "\n" for n in names),
        ";".join(names),
        payload_hash,
    ])
    scope = day + "/" + region + "/" + service + "/aws4_request"
    to_sign = "\n".join([ALGORITHM, stamp, scope, _sha256_hex(canonical.encode("utf-8"))])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + creds.secret_access_key).encode("utf-8"), day), region), service),
                "aws4_request")
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    added["Authorization"] = (ALGORITHM + " Credential=" + creds.access_key_id + "/" + scope + ", SignedHeaders="
                              + ";".join(names) + ", Signature=" + signature)
    return added


__all__ = ["Credentials", "sign", "canonical_uri", "canonical_query", "amz_date"]
