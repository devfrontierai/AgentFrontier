"""Where the files of the ``files`` protocol live (spec docs/storage.md): a local folder, an S3 bucket, an Azure Data
Lake Storage Gen2 file system or a Google Cloud Storage bucket. Every backend lists the same entries and reads the
same bytes, so the files engine above it does not care where a file is."""

from __future__ import annotations

import datetime as _dt
import email.utils
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import json_util as J
from .. import template as T
from ..auth import aws_credentials
from ..errors import AUTH_FAILED, QUERY_FAILED, AgentFrontierError, root_message
from ..sigv4 import sign
from . import ProtocolContext

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
_ISO_SECONDS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
_RETRY_ON = (429, 500, 502, 503, 504)


@dataclass
class Entry:
    """One file: its path relative to the root or prefix, size, modification time (ISO-8601 UTC without fractional
    seconds), location and backend-specific properties."""

    path: str
    size: int | None
    updated_at: str | None
    location: str
    properties: dict = field(default_factory=dict)


def iso_seconds(ts: float) -> str:
    return _dt.datetime.fromtimestamp(int(ts // 1), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_text(v: str | None) -> str | None:
    """``2026-10-30T10:00:00.000Z`` to ``2026-10-30T10:00:00Z``."""
    if not v:
        return None
    m = _ISO_SECONDS.match(v)
    return None if m is None else m.group(1) + "Z"


def _rfc1123(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return iso_seconds(email.utils.parsedate_to_datetime(v).timestamp())
    except (TypeError, ValueError):
        return None


def _enc(s: str, safe: str = "") -> str:
    return urllib.parse.quote(s, safe="-_.~" + safe)


def _query(pairs: list[tuple[str, str | None]]) -> str:
    return "&".join(_enc(k) + ("" if v is None else "=" + _enc(v)) for k, v in pairs)


def _int(v: Any) -> int | None:
    try:
        return None if v is None else int(J.jstr(v))
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            return c.text
    return None


class Storage:
    """A backend. ``read`` returns at most ``max_bytes`` bytes and whether that is the whole file."""

    def stat(self) -> dict:
        raise NotImplementedError

    def list(self) -> list[Entry]:
        raise NotImplementedError

    def read(self, path: str, max_bytes: int) -> tuple[bytes, bool]:
        raise NotImplementedError

    def read_tail(self, path: str, n: int) -> bytes:
        """The last ``n`` bytes of a file (Parquet footers)."""
        raise NotImplementedError

    def local_path(self, path: str) -> str | None:
        """A path on the local file system, when there is one."""
        return None

    def tags(self, path: str) -> list[dict]:
        return []

    def grants(self) -> list[dict]:
        return []

    def describe(self) -> str:
        raise NotImplementedError


# ------------------------------------------------------------------------------------------------------------- local


class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = os.path.normpath(os.path.abspath(root))

    def describe(self) -> str:
        return self.root

    def _require(self) -> None:
        if not os.path.isdir(self.root):
            raise AgentFrontierError.connection("folder not found: " + self.root, None)

    def stat(self) -> dict:
        self._require()
        return {"root": self.root, "readable": os.access(self.root, os.R_OK)}

    def list(self) -> list[Entry]:
        self._require()
        out = []
        for base, dirs, names in os.walk(self.root):
            dirs.sort()
            for n in names:
                p = os.path.join(base, n)
                if os.path.isfile(p):
                    rel = os.path.relpath(p, self.root).replace("\\", "/")
                    st = os.stat(p)
                    out.append(Entry(rel, st.st_size, iso_seconds(st.st_mtime), rel))
        out.sort(key=lambda e: e.path)
        return out

    def local_path(self, path: str) -> str:
        p = os.path.normpath(os.path.join(self.root, path))
        if os.path.commonpath([os.path.normcase(p), os.path.normcase(self.root)]) != os.path.normcase(self.root):
            raise AgentFrontierError.invalid_config("path escapes the root folder: " + path)
        return p

    def read(self, path: str, max_bytes: int) -> tuple[bytes, bool]:
        with open(self.local_path(path), "rb") as f:
            data = f.read(max_bytes + 1)
        return data[:max_bytes], len(data) <= max_bytes

    def read_tail(self, path: str, n: int) -> bytes:
        p = self.local_path(path)
        size = os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(max(0, size - n))
            return f.read()


# ------------------------------------------------------------------------------------------------------------- HTTP


class _Http:
    """Requests of the cloud backends: no proxy unless asked (like the http engine), three attempts on throttling and
    server errors, every request counted in the session's statistics."""

    def __init__(self, ctx: ProtocolContext, signer: Callable[[str, str, dict, bytes | None], dict] | None,
                 headers: dict[str, str], sas: str | None):
        self.ctx = ctx
        self.signer = signer
        self.headers = headers
        self.sas = sas
        from .http import _proxies  # same proxy rule as the http engine
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies()))

    def send(self, method: str, url: str, headers: dict[str, str] | None = None,
             max_bytes: int | None = None, missing_ok: bool = False) -> tuple[int, dict[str, str], bytes, bool]:
        """(status, lower-case headers, body, whether the body was read whole)."""
        if self.sas:
            url += ("&" if "?" in url else "?") + self.sas.lstrip("?")
        h = dict(self.headers)
        h.update(headers or {})
        start = time.perf_counter_ns()
        try:
            for attempt in range(1, 4):
                sent = h if self.signer is None else dict(h, **self.signer(method, url, h, None))
                req = urllib.request.Request(url, method=method, headers=sent)
                try:
                    with self.opener.open(req, timeout=60) as resp:
                        data = resp.read() if max_bytes is None else resp.read(max_bytes + 1)
                        whole = max_bytes is None or len(data) <= max_bytes
                        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, \
                            data if whole else data[:max_bytes], whole
                except urllib.error.HTTPError as e:
                    if e.code in _RETRY_ON and attempt < 3:
                        self.ctx.stats.record_retry()
                        time.sleep(0.5 * attempt)
                        continue
                    if e.code == 404 and missing_ok:
                        return 404, {}, b"", True
                    detail = ""
                    try:
                        detail = e.read()[:300].decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
                    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
                    msg = method + " " + path + " returned HTTP " + str(e.code) + (": " + " ".join(detail.split())
                                                                                   if detail.strip() else "")
                    raise AgentFrontierError(AUTH_FAILED if e.code in (401, 403) else QUERY_FAILED, msg) from None
                except (urllib.error.URLError, OSError) as e:
                    if attempt < 3:
                        self.ctx.stats.record_retry()
                        time.sleep(0.5 * attempt)
                        continue
                    p = urllib.parse.urlsplit(url)
                    raise AgentFrontierError.connection("request to " + p.scheme + "://" + p.netloc + " failed: "
                                                        + root_message(e), e) from e
            raise AssertionError("unreachable")
        finally:
            self.ctx.stats.record_statement(1, time.perf_counter_ns() - start)


def _auth_http(ctx: ProtocolContext, block: dict, extra_headers: dict[str, str]) -> _Http:
    """The auth block of the connection's method: ``headers`` (templates, may use ``token.*``), ``sigv4`` and
    ``sas``."""
    auth_all = block.get("auth") if isinstance(block.get("auth"), dict) else {}
    a = auth_all.get(ctx.auth_method)
    if a is None:
        raise AgentFrontierError.unsupported("auth method '" + ctx.auth_method + "' is not supported by "
                                             + str(ctx.definition.name) + " (supported: "
                                             + J.jlist(list(auth_all.keys())) + ")")
    a = a if isinstance(a, dict) else {}
    vars = ctx.connect_vars({})
    headers = dict(extra_headers)
    for k, v in (a.get("headers") or {}).items():
        r = T.render_text(J.jstr(v), vars, T.STRICT, None)
        if r:
            headers[k] = r
    signer = None
    sigv4 = a.get("sigv4")
    if isinstance(sigv4, dict):
        token = ctx.token()
        service = T.render_text(J.text(sigv4, "service", "s3"), vars, T.LENIENT, None) or "s3"
        region = T.render_text(J.text(sigv4, "region", ""), vars, T.LENIENT, None) or J.jstr(token.get("region"))
        creds = aws_credentials(token)

        def signer(m: str, u: str, h: dict, b: bytes | None) -> dict:
            return sign(m, u, h, b, creds, region, service)
    sas = T.render_text(J.text(a, "sas"), vars, T.STRICT, None) if a.get("sas") is not None else None
    return _Http(ctx, signer, headers, sas or None)


def _dir_prefix(prefix: str | None) -> str:
    p = (prefix or "").strip().strip("/")
    return p + "/" if p else ""


class _Cloud(Storage):
    def __init__(self, http: _Http, prefix: str | None):
        self.http = http
        self.prefix = _dir_prefix(prefix)
        self._entries: list[Entry] | None = None
        self._by_path: dict[str, Entry] = {}

    def _rel(self, name: str) -> str | None:
        if not name.startswith(self.prefix) or name.endswith("/"):
            return None
        rel = name[len(self.prefix):]
        return rel or None

    def list(self) -> list[Entry]:
        if self._entries is None:
            out = sorted(self._list_all(), key=lambda e: e.path)
            self._entries = out
            self._by_path = {e.path: e for e in out}
        return self._entries

    def _list_all(self) -> list[Entry]:
        raise NotImplementedError

    def stat(self) -> dict:
        self.list()
        return {"root": self.describe(), "readable": True}

    def _object_url(self, path: str) -> str:
        raise NotImplementedError

    def read(self, path: str, max_bytes: int) -> tuple[bytes, bool]:
        _, _, data, whole = self.http.send("GET", self._object_url(path), {"Range": "bytes=0-" + str(max_bytes - 1)},
                                           max_bytes)
        entry = self._by_path.get(path)
        if whole and entry is not None and entry.size is not None and len(data) < entry.size:
            whole = False  # a 206 answer holding the first max_bytes of a longer file
        return data, whole

    def read_tail(self, path: str, n: int) -> bytes:
        _, _, data, _ = self.http.send("GET", self._object_url(path), {"Range": "bytes=-" + str(n)})
        return data


# ---------------------------------------------------------------------------------------------------------------- S3


class S3Storage(_Cloud):
    def __init__(self, http: _Http, bucket: str, prefix: str | None, region: str | None, endpoint: str | None):
        super().__init__(http, prefix)
        self.bucket = bucket
        region = region or "us-east-1"
        self.base = (endpoint.rstrip("/") + "/" + _enc(bucket) + "/") if endpoint \
            else "https://" + bucket + ".s3." + region + ".amazonaws.com/"

    def describe(self) -> str:
        return "s3://" + self.bucket + "/" + self.prefix

    def _object_url(self, path: str) -> str:
        return self.base + _enc(self.prefix + path, "/")

    def _list_all(self) -> list[Entry]:
        out: list[Entry] = []
        token = None
        for _ in range(100_000):
            pairs: list[tuple[str, str | None]] = [("list-type", "2"), ("prefix", self.prefix)]
            if token:
                pairs.append(("continuation-token", token))
            _, _, body, _ = self.http.send("GET", self.base + "?" + _query(pairs))
            root = _xml(body)
            for c in root:
                if _local(c.tag) != "Contents":
                    continue
                key = _child_text(c, "Key") or ""
                rel = self._rel(key)
                if rel is None:
                    continue
                props = {"etag": _strip_quotes(_child_text(c, "ETag"))}
                sc = _child_text(c, "StorageClass")
                if sc:
                    props["storageClass"] = sc
                out.append(Entry(rel, _int(_child_text(c, "Size")), _iso_text(_child_text(c, "LastModified")),
                                 "s3://" + self.bucket + "/" + key, props))
            truncated = (_child_text(root, "IsTruncated") or "").lower() == "true"
            token = _child_text(root, "NextContinuationToken")
            if not truncated or not token:
                break
        return out

    def tags(self, path: str) -> list[dict]:
        _, _, body, _ = self.http.send("GET", self._object_url(path) + "?tagging")
        out = []
        for el in _xml(body).iter():
            if _local(el.tag) == "Tag":
                out.append({"name": _child_text(el, "Key"), "value": _child_text(el, "Value"),
                            "objectType": "dataset"})
        return out

    def grants(self) -> list[dict]:
        _, _, body, _ = self.http.send("GET", self.base + "?acl")
        out = []
        for el in _xml(body).iter():
            if _local(el.tag) != "Grant":
                continue
            grantee = next((c for c in el if _local(c.tag) == "Grantee"), None)
            name = None
            if grantee is not None:
                uri = _child_text(grantee, "URI")
                name = _child_text(grantee, "DisplayName") or _child_text(grantee, "ID") \
                    or (uri.rstrip("/").rsplit("/", 1)[-1] if uri else None) or _child_text(grantee, "EmailAddress")
            out.append({"principal": name, "privilege": _child_text(el, "Permission"), "objectType": "bucket"})
        return out


def _strip_quotes(v: str | None) -> str | None:
    return None if v is None else v.strip('"')


def _xml(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise AgentFrontierError.query("the storage service returned unreadable XML: " + str(e), e) from None


def _json(body: bytes) -> Any:
    try:
        return J.loads_first(body.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise AgentFrontierError.query("the storage service returned unreadable JSON: " + str(e), e) from None


# -------------------------------------------------------------------------------------------------------------- ADLS


class AdlsStorage(_Cloud):
    def __init__(self, http: _Http, account: str, filesystem: str, prefix: str | None, endpoint: str | None):
        super().__init__(http, prefix)
        self.account = account
        self.filesystem = filesystem
        self.endpoint = (endpoint or "https://" + account + ".dfs.core.windows.net").rstrip("/")

    def describe(self) -> str:
        return "abfss://" + self.filesystem + "@" + self.account + ".dfs.core.windows.net/" + self.prefix

    def _object_url(self, path: str) -> str:
        return self.endpoint + "/" + _enc(self.filesystem) + "/" + _enc(self.prefix + path, "/")

    def _list_all(self) -> list[Entry]:
        out: list[Entry] = []
        token = None
        for _ in range(100_000):
            pairs: list[tuple[str, str | None]] = [("resource", "filesystem"), ("recursive", "true")]
            if self.prefix:
                pairs.append(("directory", self.prefix.rstrip("/")))
            if token:
                pairs.append(("continuation", token))
            _, headers, body, _ = self.http.send("GET", self.endpoint + "/" + _enc(self.filesystem) + "?"
                                                 + _query(pairs))
            for p in J.get(_json(body), "paths") or []:
                if J.jstr(J.get(p, "isDirectory")).lower() == "true":
                    continue
                name = J.text(p, "name") or ""
                rel = self._rel(name)
                if rel is None:
                    continue
                out.append(Entry(rel, _int(J.get(p, "contentLength")), _rfc1123(J.text(p, "lastModified")),
                                 "abfss://" + self.filesystem + "@" + self.account + ".dfs.core.windows.net/" + name,
                                 {"etag": _strip_quotes(J.text(p, "etag"))}))
            token = headers.get("x-ms-continuation")
            if not token:
                break
        return out

    def grants(self) -> list[dict]:
        url = self.endpoint + "/" + _enc(self.filesystem) + "/" + _enc(self.prefix.rstrip("/"), "/") \
            + "?action=getAccessControl"
        _, headers, _, _ = self.http.send("HEAD", url)
        out = []
        for entry in (headers.get("x-ms-acl") or "").split(","):
            parts = entry.strip().split(":")
            if len(parts) != 3 or parts[0] not in ("user", "group") or not parts[1]:
                continue
            out.append({"principal": parts[1], "privilege": parts[2], "objectType": "directory",
                        "principalType": parts[0]})
        return out


# --------------------------------------------------------------------------------------------------------------- GCS


class GcsStorage(_Cloud):
    def __init__(self, http: _Http, bucket: str, prefix: str | None, endpoint: str | None):
        super().__init__(http, prefix)
        self.bucket = bucket
        self.endpoint = (endpoint or "https://storage.googleapis.com").rstrip("/")
        self.base = self.endpoint + "/storage/v1/b/" + _enc(bucket)

    def describe(self) -> str:
        return "gs://" + self.bucket + "/" + self.prefix

    def _object_url(self, path: str) -> str:
        return self.base + "/o/" + _enc(self.prefix + path) + "?alt=media"

    def _list_all(self) -> list[Entry]:
        out: list[Entry] = []
        token = None
        for _ in range(100_000):
            pairs: list[tuple[str, str | None]] = [("prefix", self.prefix)]
            if token:
                pairs.append(("pageToken", token))
            _, _, body, _ = self.http.send("GET", self.base + "/o?" + _query(pairs))
            doc = _json(body)
            for o in J.get(doc, "items") or []:
                name = J.text(o, "name") or ""
                rel = self._rel(name)
                if rel is None:
                    continue
                props: dict = {"etag": J.text(o, "etag")}
                for k in ("generation", "contentType"):
                    if J.text(o, k) is not None:
                        props[k] = J.text(o, k)
                meta = J.get(o, "metadata")
                e = Entry(rel, _int(J.get(o, "size")), _iso_text(J.text(o, "updated")),
                          "gs://" + self.bucket + "/" + name, props)
                e.metadata = dict(meta) if isinstance(meta, dict) else {}  # type: ignore[attr-defined]
                out.append(e)
            token = J.text(doc, "nextPageToken")
            if not token:
                break
        return out

    def tags(self, path: str) -> list[dict]:
        self.list()
        e = self._by_path.get(path)
        meta = getattr(e, "metadata", {}) if e is not None else {}
        return [{"name": k, "value": J.jstr(meta[k]), "objectType": "dataset"} for k in sorted(meta)]

    def grants(self) -> list[dict]:
        _, _, body, _ = self.http.send("GET", self.base + "/iam")
        out = []
        for b in J.get(_json(body), "bindings") or []:
            for m in J.get(b, "members") or []:
                out.append({"principal": J.jstr(m), "privilege": J.text(b, "role"), "objectType": "bucket"})
        return out


# ------------------------------------------------------------------------------------------------------------ factory


def open_storage(ctx: ProtocolContext, block: dict) -> Storage:
    storage = J.text(block, "storage", "local")
    vars = ctx.connect_vars({})

    def val(key: str, required: bool = False) -> str | None:
        v = T.render_text(J.text(block, key), vars, T.STRICT, None) if J.text(block, key) is not None else None
        if (v is None or J.is_blank(v)) and required:
            raise AgentFrontierError.invalid_config("property '" + key + "' is required")
        return None if v is None or J.is_blank(v) else v

    if storage == "local":
        return LocalStorage(val("root", True))  # type: ignore[arg-type]
    if storage == "s3":
        return S3Storage(_auth_http(ctx, block, {}), val("bucket", True), val("prefix"),  # type: ignore[arg-type]
                         val("region"), val("endpoint"))
    if storage == "adls":
        return AdlsStorage(_auth_http(ctx, block, {"x-ms-version": "2021-08-06"}), val("account", True),
                           val("filesystem", True), val("prefix"), val("endpoint"))  # type: ignore[arg-type]
    if storage == "gcs":
        return GcsStorage(_auth_http(ctx, block, {}), val("bucket", True), val("prefix"),  # type: ignore[arg-type]
                          val("endpoint"))
    raise AgentFrontierError.unsupported("file storage '" + storage + "' is not available in this version")


__all__ = ["Entry", "Storage", "LocalStorage", "S3Storage", "AdlsStorage", "GcsStorage", "open_storage",
           "DEFAULT_MAX_BYTES"]
