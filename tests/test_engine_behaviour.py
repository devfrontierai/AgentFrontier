"""Engine paths the conformance cases do not reach: paging modes, alternatives, onError, retries, auth failures,
user definition directories and overlays, the files engine edge cases and the json / http sinks."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from agentfrontier import (AgentFrontier, AgentFrontierError, AuthConfig, ConnectionConfig, PipelineConfig, Scope)

ITEMS = [{"id": i, "name": "item%02d" % i} for i in range(1, 8)]


class Api:
    """A tiny API with every paging style the engine supports."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        self.posts: list[dict] = []
        self.token_requests: list[str] = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                if self.path == "/sink":
                    api.posts.append(json.loads(body))
                    return api.send(self, 200, {})
                if self.path == "/token":
                    api.token_requests.append(body)
                    return api.send(self, 200, {"access_token": "k1", "token_type": "Bearer", "expires_in": 3600})
                return api.send(self, 200, {"form": body, "ctype": self.headers.get("Content-Type")})

            def do_GET(self):  # noqa: N802
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                api.hits[u.path] = api.hits.get(u.path, 0) + 1
                if self.headers.get("X-Key") != "k1":
                    return api.send(self, 401, {"error": "who are you"})
                limit = int(q.get("limit", "3"))
                if u.path == "/offset":
                    off = int(q.get("offset", "0"))
                    return api.send(self, 200, ITEMS[off:off + limit])
                if u.path == "/page":
                    p = int(q.get("page", "1"))
                    return api.send(self, 200, ITEMS[(p - 1) * limit:p * limit])
                if u.path == "/keyset":
                    after = q.get("after", "")
                    rest = [x for x in ITEMS if x["name"] > after]
                    return api.send(self, 200, rest[:limit])
                if u.path == "/link":
                    p = int(q.get("p", "1"))
                    chunk = ITEMS[(p - 1) * 3:p * 3]
                    headers = {}
                    if p * 3 < len(ITEMS):
                        headers["Link"] = '<' + "/link?p=" + str(p + 1) + '>; rel="next"'
                    return api.send(self, 200, chunk, headers)
                if u.path == "/next":
                    p = int(q.get("p", "1"))
                    nxt = "/next?p=" + str(p + 1) if p * 3 < len(ITEMS) else None
                    return api.send(self, 200, {"items": ITEMS[(p - 1) * 3:p * 3], "next": nxt})
                if u.path == "/flaky":
                    if api.hits[u.path] < 3:
                        return api.send(self, 503, {"error": "busy"}, {"Retry-After": "0"})
                    return api.send(self, 200, [{"name": "ok"}])
                if u.path == "/broken":
                    return api.send(self, 500, {"error": "kaput"})
                if u.path == "/good":
                    return api.send(self, 200, [{"name": "fallback"}])
                return api.send(self, 404, {"error": "no " + u.path})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:" + str(self.httpd.server_address[1])

    @staticmethod
    def send(h: BaseHTTPRequestHandler, status: int, body: object, headers: dict | None = None) -> None:
        b = json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(b)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


DEFINITION = {
    "id": "paging_api", "name": "Paging API", "protocol": "http",
    "properties": [{"key": "baseUrl", "type": "string", "required": True}],
    "auth": ["api_key", "oauth2_client_credentials"],
    "connection": {"http": {"baseUrl": "{{baseUrl}}", "retry": {"attempts": 3, "on": [503], "backoffMs": 1},
                            "auth": {"api_key": {"headers": {"X-Key": "{{auth.apiKey}}"}},
                                     "oauth2_client_credentials": {"oauth2": {"tokenUrl": "{{baseUrl}}/token",
                                                                              "scope": "all"},
                                                                   "headers": {"X-Key": "{{token.accessToken}}"}}}}},
    "hierarchy": [],
    "operations": {
        "test": {"entity": "source", "request": {"path": "/flaky"}},
        "datasets": {"entity": "dataset", "paging": {"mode": "offset", "size": 3},
                     "request": {"path": "/offset", "query": {"offset": "{{offset}}", "limit": "{{limit}}"}}},
        "by_page": {"entity": "routine", "paging": {"mode": "page", "size": 3},
                    "request": {"path": "/page", "query": {"page": "{{page}}", "limit": "{{limit}}"}}},
        "by_key": {"entity": "principal", "paging": {"mode": "keyset", "size": 3},
                   "request": {"path": "/keyset", "query": {"after": "{{after}}", "limit": "{{limit}}"}}},
        "by_link": {"entity": "tag", "paging": {"mode": "linkHeader", "size": 3}, "request": {"path": "/link"}},
        "by_next": {"entity": "usage", "paging": {"mode": "nextUrl", "next": "$.next", "size": 3},
                    "request": {"path": "/next"}, "records": "$.items[*]", "fields": {"queryId": "name"}},
        "with_fallback": {"entity": "policy", "request": {"path": "/broken"},
                          "alternatives": [{"request": {"path": "/good"}}]},
        "warns": {"entity": "statistic", "onError": "warn", "request": {"path": "/broken"}},
        "skips": {"entity": "grant", "onError": "skip", "request": {"path": "/broken"}},
        "posts": {"entity": "lineage", "request": {"method": "POST", "path": "/form", "form": {"a": "x y", "b": "{{none}}"}}},
    },
}


@pytest.fixture(scope="module")
def api():
    a = Api()
    yield a
    a.close()


@pytest.fixture()
def af(tmp_path):
    d = tmp_path / "defs"
    d.mkdir()
    (d / "paging_api.json").write_text(json.dumps(DEFINITION), encoding="utf-8")
    return AgentFrontier(definitions=[d])


def conn(api: Api, key: str = "k1") -> ConnectionConfig:
    return ConnectionConfig("paging_api", {"baseUrl": api.base}, AuthConfig.api_key(key), id="api")


def names(records) -> list:
    return [r.string("name") for r in records]


def test_every_paging_mode(af, api):
    with af.open(conn(api)) as s:
        page = s.datasets(Scope.root(), None, 3)
        all_ = list(page.records)
        while page.next_cursor:
            page = s.datasets(Scope.root(), page.next_cursor, 3)
            all_.extend(page.records)
        assert names(all_) == [x["name"] for x in ITEMS]
        assert names(s.records("routine", Scope.root())) == [x["name"] for x in ITEMS]
        assert names(s.records("principal", Scope.root())) == [x["name"] for x in ITEMS]
        assert names(s.records("tag", Scope.root())) == [x["name"] for x in ITEMS]
        assert [r.string("queryId") for r in s.records("usage", Scope.root())] == [x["name"] for x in ITEMS]


def test_alternatives_on_error_and_forms(af, api):
    with af.open(conn(api)) as s:
        assert names(s.records("policy", Scope.root())) == ["fallback"]
        assert s.records("statistic", Scope.root()) == []
        assert s.records("grant", Scope.root()) == []
        assert any("warns skipped" in w and "HTTP 500" in w for w in s.warnings)
        assert len(s.warnings) == 1
        r = s.records("lineage", Scope.root())[0]
        assert r.properties["form"] == "a=x%20y" and r.properties["ctype"] == "application/x-www-form-urlencoded"
        with pytest.raises(AgentFrontierError) as e:
            s.run("nope", Scope.root(), {})
        assert e.value.code == "UNSUPPORTED"


def test_retries_and_auth_failures(af, api):
    api.hits.pop("/flaky", None)
    t = af.test(conn(api))
    assert t.ok, t.message
    assert api.hits["/flaky"] == 3
    bad = af.test(conn(api, "wrong"))
    assert not bad.ok and "returned HTTP 401" in bad.message
    with pytest.raises(AgentFrontierError) as e:
        with af.open(conn(api, "wrong")) as s:
            s.records("routine", Scope.root())
    assert e.value.code == "AUTH_FAILED"


def test_oauth2_client_credentials_token_is_fetched_once(af, api):
    c = ConnectionConfig("paging_api", {"baseUrl": api.base},
                         AuthConfig.oauth2_client_credentials(None, "cid", "s ecret"), id="oauth")
    with af.open(c) as s:
        assert len(s.records("routine", Scope.root())) == 7
        assert len(s.records("principal", Scope.root())) == 7
    assert api.token_requests == ["grant_type=client_credentials&client_id=cid&client_secret=s+ecret&scope=all"]


def test_connection_refused_is_connection_failed(af):
    c = ConnectionConfig("paging_api", {"baseUrl": "http://127.0.0.1:9"}, AuthConfig.api_key("k1"))
    t = af.test(c)
    assert not t.ok and "request to http://127.0.0.1:9 failed" in t.message


def test_json_and_http_sinks(af, api, tmp_path):
    out = tmp_path / "all.json"
    sinks = [{"type": "json", "path": str(out)}, {"type": "http", "url": api.base + "/sink", "batchSize": 2}]
    r = af.run(PipelineConfig.of(conn(api), ["metadata"], sinks))
    assert r.status == "SUCCEEDED", r.errors
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert len(doc["records"]["dataset"]) == 7 and doc["run"]["report"]["counts"]["dataset"] == 7
    events = [p["event"] for p in api.posts]
    assert events[0] == "begin" and events[-1] == "end" and "records" in events
    assert sum(len(p.get("records", [])) for p in api.posts) == sum(r.counts.values())


def test_user_overlay_and_override(tmp_path):
    d = tmp_path / "defs"
    d.mkdir()
    (d / "overlay.json").write_text(json.dumps({"overlay": "sqlite", "when": {"version": ">=3"},
                                                "operations": {"datasets": {"filter": {"name": {"exclude": ["b*"]}}}}}))
    db = tmp_path / "x.db"
    import sqlite3
    con = sqlite3.connect(db)
    con.executescript("create table alpha (id int); create table beta (id int);")
    con.close()
    af = AgentFrontier(definitions=[d])
    with af.open(ConnectionConfig("sqlite", {"path": str(db)})) as s:
        assert names(s.datasets(Scope.root()).records) == ["alpha"]
    with af.open(ConnectionConfig("sqlite", {"path": str(db)}, options={"excludeDatasets": "al*"})) as s:
        assert names(s.datasets(Scope.root()).records) == []


def test_files_engine_edges(tmp_path):
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "a.csv").write_bytes(b'\xef\xbb\xbfid,,note\r\n1,x,"multi\nline"\r\n\r\n2,,"q ""x"""\r\n')
    (root / "sub" / "b.json").write_text('[{"a": 1}, {"a": 2.5, "b": {"c": null}}, 3]', encoding="utf-8")
    af = AgentFrontier()
    c = ConnectionConfig("local_files", {"root": str(root)})
    with af.open(c) as s:
        folders = {r.string("name"): r.properties["file_count"] for r in s.containers(Scope.root())}
        assert folders == {".": 1, "sub": 1}
        f = s.records("field", Scope.of("folder", "."), ["a.csv"])
        assert [(r.string("name"), r.string("dataType"), r.bool("nullable")) for r in f] == \
            [("id", "integer", False), ("column_2", "string", True), ("note", "string", False)]
        rows = s.sample(Scope.of("folder", "."), "a.csv", 5)
        assert [r.values for r in rows] == [{"id": "1", "column_2": "x", "note": "multi\nline"},
                                            {"id": "2", "column_2": "", "note": 'q "x"'}]
        f = s.records("field", Scope.of("folder", "sub"), ["b.json"])
        assert [(r.string("name"), r.string("dataType"), r.bool("nullable"), r.string("parent")) for r in f] == \
            [("a", "double", False, None), ("b", "object", True, None), ("b.c", "string", True, "b")]
        with pytest.raises(AgentFrontierError) as e:
            s.sample(Scope.of("folder", "."), "../../etc/passwd", 1)
        assert e.value.code == "INVALID_CONFIG"


def test_parallel_pipeline(tmp_path):
    root = tmp_path / "data"
    for i in range(4):
        (root / ("f" + str(i))).mkdir(parents=True)
        (root / ("f" + str(i)) / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    af = AgentFrontier()
    started = time.time()
    r = af.run(PipelineConfig(ConnectionConfig("local_files", {"root": str(root)}), ["metadata", "data"],
                              sinks=[{"type": "memory", "name": "par"}], parallelism=3))
    assert r.status == "SUCCEEDED", r.errors
    assert r.counts == {"source": 1, "container": 4, "dataset": 4, "field": 8, "row": 4}
    assert r.load.connections == 0 and r.load.statements > 0
    assert time.time() - started < 30
