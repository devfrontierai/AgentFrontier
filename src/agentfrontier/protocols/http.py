"""The HTTP engine: REST and JSON APIs, described by the definition's ``connection.http`` block and ``request``
operations.

Every request is built from three layers, later ones winning: the definition's ``connection.http`` (base URL,
headers, query), the block for the connection's auth method (headers, query, ``basic``), and the operation's
``request`` (method, path, query, headers, body or form). Values are templates; a header or query value that renders
to nothing is left out.
"""

from __future__ import annotations

import base64
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import json_util as J
from .. import template as T
from ..errors import AUTH_FAILED, INVALID_CONFIG, QUERY_FAILED, AgentFrontierError, root_message
from ..model import Scope
from . import Protocol, ProtocolContext, ProtocolSession, RawResult

MAX_ERROR_BODY = 500


class _NoRedirectToHttp(urllib.request.HTTPRedirectHandler):
    """Follows redirects like Java's ``Redirect.NORMAL``: never from https to http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if req.full_url.lower().startswith("https:") and newurl.lower().startswith("http:"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def form_encode(pairs: dict[str, str]) -> str:
    return "&".join(J.url_encode20(k) + "=" + J.url_encode20(v) for k, v in pairs.items())


class HttpSession(ProtocolSession):
    def __init__(self, ctx: ProtocolContext):
        self.ctx = ctx
        http = ctx.definition.connection("http")
        if http is None:
            raise AgentFrontierError.unsupported("definition '" + str(ctx.definition.id)
                                                 + "' has no connection.http block")
        self.http = http if isinstance(http, dict) else {}
        auth_all = self.http.get("auth")
        a = auth_all.get(ctx.auth_method) if isinstance(auth_all, dict) else None
        if a is None:
            ids = list(auth_all.keys()) if isinstance(auth_all, dict) else []
            raise AgentFrontierError.unsupported("auth method '" + ctx.auth_method + "' is not supported by "
                                                 + str(ctx.definition.name) + " (supported: " + J.jlist(ids) + ")")
        self.auth_block = a if isinstance(a, dict) else {}
        v = ctx.connect_vars({})
        t = T.render(J.text(self.http, "timeoutSeconds", "30"), v, T.LENIENT)
        ts = J.jstr(t)
        self.timeout = J.parse_long(J.jtrim("30" if J.is_blank(ts) else ts))
        verify = T.render(J.text(self.http, "verifyTls", "true"), v, T.LENIENT)
        context = None
        if J.equals_ignore_case(J.jstr(verify), "false"):
            context = ssl._create_unverified_context()  # noqa: S323 - the user asked for it
        handlers: list = [_NoRedirectToHttp(), urllib.request.ProxyHandler(_proxies())]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)
        retry = self.http.get("retry")
        self.attempts = max(1, J.integer(retry, "attempts", 1))
        on = J.get(retry, "on")
        self.retry_on = {J.as_int(x) if J.is_number(x) else _int_or(x) for x in (on if isinstance(on, list) else [])}
        self.backoff_ms = J.integer(retry, "backoffMs", 500)
        rps = J.as_double(J.get(J.get(self.http, "rateLimit"), "requestsPerSecond"), 0.0)
        self.min_interval_ns = int(1_000_000_000 / rps) if rps > 0 else 0
        self.last_request = 0

    # ------------------------------------------------------------------------------------------------ requests

    def execute(self, op: dict, op_vars: T.Vars, scope: Scope) -> RawResult:
        req = op.get("request") if isinstance(op.get("request"), dict) else {}
        vars = op_vars.then(self.ctx.connect_vars({}))
        tf = self.ctx.temp_files
        base = T.render_text(J.text(self.http, "baseUrl"), vars, T.STRICT, tf)
        if base is None:
            raise AgentFrontierError.invalid_config("base URL for " + str(self.ctx.definition.name) + " is not set")
        next_url = op_vars.get("nextUrl")
        if next_url is not None:
            url = urllib.parse.urljoin(base if base.endswith("/") else base + "/", J.jstr(next_url))
        else:
            path = T.render_text(J.text(req, "path", ""), vars, T.LENIENT, tf)
            query: dict[str, str] = {}
            self._add_all(query, self.http.get("query"), vars)
            self._add_all(query, self.auth_block.get("query"), vars)
            self._add_all(query, req.get("query"), vars)
            u = base[:-1] if base.endswith("/") else base
            if path:
                u += path if path.startswith("/") else "/" + path
            if query:
                u += "?" if "?" not in u else "&"
                u += form_encode(query)
            url = u
        headers: dict[str, tuple[str, str]] = {}  # lower-case name to (name as first written, value)
        self._add_headers(headers, self.http.get("headers"), vars)
        self._add_headers(headers, self.auth_block.get("headers"), vars)
        self._add_headers(headers, req.get("headers"), vars)
        basic = self.auth_block.get("basic")
        if basic is not None:
            user = T.render_text(J.text(basic, "username", ""), vars, T.LENIENT, None)
            pw = T.render_text(J.text(basic, "password", ""), vars, T.LENIENT, None)
            token = base64.b64encode((user + ":" + pw).encode("utf-8")).decode("ascii")
            _put_header(headers, "Authorization", "Basic " + token)
        method = J.text(req, "method", "GET").upper()
        body: bytes | None = None
        if "body" in req:
            body = J.write(T.render_json(req.get("body"), vars, T.STRICT, tf)).encode("utf-8")
            _put_header_if_absent(headers, "Content-Type", "application/json")
        elif "form" in req:
            form: dict[str, str] = {}
            self._add_all(form, req.get("form"), vars)
            body = form_encode(form).encode("utf-8")
            _put_header_if_absent(headers, "Content-Type", "application/x-www-form-urlencoded")
        return self._send(method, url, {k: v for k, v in headers.values()}, body)

    def _add_all(self, target: dict[str, str], block: Any, vars: T.Vars) -> None:
        if not isinstance(block, dict):
            return
        for k, val in block.items():
            key = T.render_text(k, vars, T.LENIENT, None)
            v = T.render(val if isinstance(val, str) else J.write(val), vars, T.STRICT, self.ctx.temp_files)
            if v is not None and key and T.as_text(v) != "":
                target[key] = T.as_text(v)

    def _add_headers(self, target: dict[str, tuple[str, str]], block: Any, vars: T.Vars) -> None:
        tmp: dict[str, str] = {}
        self._add_all(tmp, block, vars)
        for k, v in tmp.items():
            _put_header(target, k, v)

    def _send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> RawResult:
        start = time.perf_counter_ns()
        records = 0
        try:
            attempt = 1
            while True:
                self._throttle()
                try:
                    request = urllib.request.Request(url, data=body, method=method, headers=headers)
                except ValueError as e:
                    raise AgentFrontierError(INVALID_CONFIG, "invalid request URL for " + self.ctx.definition.name
                                             + ": " + str(e)) from e
                status = 0
                text = ""
                resp_headers: dict[str, list[str]] = {}
                try:
                    with self.opener.open(request, timeout=self.timeout) as resp:
                        status = resp.status
                        raw = resp.read()
                        resp_headers = _headers(resp.headers)
                        text = _decode(raw, resp.headers)
                except urllib.error.HTTPError as e:
                    status = e.code
                    try:
                        raw = e.read()
                    except Exception:  # noqa: BLE001
                        raw = b""
                    resp_headers = _headers(e.headers) if e.headers is not None else {}
                    text = _decode(raw, e.headers)
                except (urllib.error.URLError, OSError, ValueError) as e:
                    if attempt < self.attempts:
                        _sleep(self.backoff_ms * (1 << (attempt - 1)))
                        attempt += 1
                        continue
                    raise AgentFrontierError.connection("request to " + _host_of(url) + " failed: "
                                                        + root_message(_reason(e)), e) from e
                if 200 <= status < 300:
                    parsed = _parse(text, resp_headers)
                    records = len(parsed) if isinstance(parsed, list) else 1
                    return RawResult.of_body(parsed, resp_headers)
                if status in self.retry_on and attempt < self.attempts:
                    wait = _seconds((resp_headers.get("retry-after") or [None])[0])
                    _sleep(wait * 1000 if wait >= 0 else self.backoff_ms * (1 << (attempt - 1)))
                    attempt += 1
                    continue
                code = AUTH_FAILED if status in (401, 403) else QUERY_FAILED
                path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
                raise AgentFrontierError(code, method + " " + path + " returned HTTP " + str(status)
                                         + ("" if J.is_blank(text) else ": " + _abbreviate(text)))
        finally:
            self.ctx.stats.record_statement(records, time.perf_counter_ns() - start)

    def _throttle(self) -> None:
        if self.min_interval_ns <= 0:
            return
        wait = self.last_request + self.min_interval_ns - time.perf_counter_ns()
        if wait > 0:
            time.sleep(wait / 1e9)
        self.last_request = time.perf_counter_ns()

    def close(self) -> None:
        pass


def _proxies() -> dict:
    """Proxies only when ``AGENTFRONTIER_HTTP_PROXY_FROM_ENV`` is set; like the Java client, no proxy by default."""
    import os
    if os.environ.get("AGENTFRONTIER_HTTP_PROXY_FROM_ENV", "").lower() in ("1", "true", "yes"):
        return urllib.request.getproxies()
    return {}


def _int_or(x: Any) -> int:
    try:
        return int(J.as_text(x))
    except ValueError:
        return 0


def _put_header(target: dict[str, tuple[str, str]], name: str, value: str) -> None:
    low = name.lower()
    existing = target.get(low)
    target[low] = (existing[0] if existing else name, value)


def _put_header_if_absent(target: dict[str, tuple[str, str]], name: str, value: str) -> None:
    if name.lower() not in target:
        target[name.lower()] = (name, value)


def _headers(h: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if h is None:
        return out
    for k, v in h.items():
        out.setdefault(k.lower(), []).append(v)
    return out


def _decode(raw: bytes, headers: Any) -> str:
    charset = None
    try:
        charset = headers.get_content_charset() if headers is not None else None
    except Exception:  # noqa: BLE001
        charset = None
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _parse(body: str, headers: dict[str, list[str]]) -> Any:
    if not body or J.is_blank(body):
        return None
    ctype = (headers.get("content-type") or [""])[0].lower()
    trimmed = J.strip_leading(body)
    if "json" in ctype or trimmed.startswith("{") or trimmed.startswith("["):
        try:
            return J.loads_first(body)
        except ValueError as e:
            raise AgentFrontierError.query("response is not valid JSON: " + str(e), e) from e
    return body


def _seconds(retry_after: str | None) -> int:
    if retry_after is None:
        return -1
    try:
        return J.parse_long(J.jtrim(retry_after))
    except ValueError:
        return -1


def _sleep(ms: int) -> None:
    time.sleep(min(ms, 30_000) / 1000.0)


def _host_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return p.scheme + "://" + p.netloc


def _reason(e: BaseException) -> BaseException:
    r = getattr(e, "reason", None)
    return r if isinstance(r, BaseException) else e


_WS = re.compile(r"[ \t\n\x0b\f\r]+")


def _abbreviate(s: str) -> str:
    one = J.jtrim(_WS.sub(" ", s))
    return one[:MAX_ERROR_BODY] + "…" if len(one) > MAX_ERROR_BODY else one


class HttpProtocol(Protocol):
    id = "http"

    def open(self, context: ProtocolContext) -> ProtocolSession:
        return HttpSession(context)
