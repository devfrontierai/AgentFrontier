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
        query: dict[str, str] = {}
        raw = split.query if split.query else None
        if raw is not None:
            for part in raw.split("&"):
                eq = part.find("=")
                k = urllib.parse.unquote_plus(part if eq < 0 else part[:eq])
                v = "" if eq < 0 else urllib.parse.unquote_plus(part[eq + 1:])
                query[k] = v
        self.requests.append(ex.command + " " + path + ("" if raw is None else "?" + raw))
        length = int(ex.headers.get("Content-Length") or 0)
        if length:
            ex.rfile.read(length)
        for name, want in ((routes.get("require") or {}).get("headers") or {}).items():
            if str(want) != ex.headers.get(name):
                self._send(ex, 401, '{"error": "unauthorized"}')
                return
        for r in routes.get("routes") or []:
            if (r.get("method") or "GET").upper() != ex.command.upper() or path != r.get("path"):
                continue
            ok = True
            for k, v in (r.get("query") or {}).items():
                if (k in query) if v is None else (str(v) != query.get(k)):
                    ok = False
                    break
            if ok:
                self._send(ex, 200, (case_dir / r["body"]).read_text(encoding="utf-8"))
                return
        self._send(ex, 404, '{"error": "no route for ' + path + '"}')

    @staticmethod
    def _send(ex: BaseHTTPRequestHandler, status: int, body: str) -> None:
        b = body.encode("utf-8")
        ex.send_response(status)
        ex.send_header("Content-Type", "application/json")
        ex.send_header("Content-Length", str(len(b)))
        ex.end_headers()
        if ex.command != "HEAD":
            ex.wfile.write(b)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
