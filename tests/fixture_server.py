"""The conformance fixture server: answers routes from a ``routes.json`` (see tests/conformance/README.md)."""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class FixtureServer:
    def __init__(self, case_dir: Path, routes: dict):
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _handle(self) -> None:
                server._handle(self, case_dir, routes)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:" + str(self._httpd.server_address[1])

    def _handle(self, ex: BaseHTTPRequestHandler, case_dir: Path, routes: dict) -> None:
        split = urllib.parse.urlsplit(ex.path)
        path = urllib.parse.unquote_plus(split.path)
        raw = split.query if split.query else None
        query = _parse_pairs(raw) if raw is not None else {}
        self.requests.append(ex.command + " " + path + ("" if raw is None else "?" + raw))
        length = int(ex.headers.get("Content-Length") or 0)
        body = ex.rfile.read(length).decode("utf-8") if length else ""
        form = _parse_pairs(body) if body else {}
        base = self.base_url

        def sub(v: Any) -> str:
            return str(v).replace("{{baseUrl}}", base)

        for r in routes.get("routes") or []:
            if (r.get("method") or "GET").upper() != ex.command.upper() or path != r.get("path"):
                continue
            if not _matches(r.get("query"), query, sub) or not _matches(r.get("form"), form, sub):
                continue
            require = r["require"] if "require" in r else routes.get("require")
            if not self._authorised(ex, require):
                return
            headers = []
            for name, value in (r.get("headers") or {}).items():
                for v in value if isinstance(value, list) else [value]:
                    headers.append((name, sub(v)))
            self._send(ex, 200, sub((case_dir / r["body"]).read_text(encoding="utf-8")), headers)
            return
        if not self._authorised(ex, routes.get("require")):
            return
        self._send(ex, 404, '{"error": "no route for ' + path + '"}')

    def _authorised(self, ex: BaseHTTPRequestHandler, require: Any) -> bool:
        for name, want in ((require or {}).get("headers") or {}).items():
            if str(want) != ex.headers.get(name):
                self._send(ex, 401, '{"error": "unauthorized"}')
                return False
        return True

    @staticmethod
    def _send(ex: BaseHTTPRequestHandler, status: int, body: str, headers: list | None = None) -> None:
        b = body.encode("utf-8")
        ex.send_response(status)
        ex.send_header("Content-Type", "application/json")
        ex.send_header("Content-Length", str(len(b)))
        for name, value in headers or []:
            ex.send_header(name, value)
        ex.end_headers()
        if ex.command != "HEAD":
            ex.wfile.write(b)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _parse_pairs(raw: str) -> dict[str, str]:
    """``a=1&b=2`` (a query string or a form body) to a map, decoded."""
    out: dict[str, str] = {}
    for part in raw.split("&"):
        eq = part.find("=")
        k = urllib.parse.unquote_plus(part if eq < 0 else part[:eq])
        out[k] = "" if eq < 0 else urllib.parse.unquote_plus(part[eq + 1:])
    return out


def _matches(want: Any, have: dict[str, str], sub: Any) -> bool:
    """Every entry of ``want`` holds: ``null`` means absent, anything else equal as text."""
    for k, v in (want or {}).items():
        if (k in have) if v is None else (sub(v) != have.get(k)):
            return False
    return True
