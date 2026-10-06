"""Runs every case under tests/conformance (a copy of the shared spec/conformance); see its README.

The expected files are the contract shared with the Java edition: they are never regenerated here. On a mismatch the
actual records are written to ``out/conformance-actual/<case>.jsonl`` for inspection.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from agentfrontier import AgentFrontier, ConnectionConfig, MemorySink, PipelineConfig, Scope
from agentfrontier import json_util as J
from agentfrontier import template as T

sys.path.insert(0, str(Path(__file__).parent))
from fixture_server import FixtureServer  # noqa: E402

ROOT = Path(__file__).parent / "conformance"
CASES = sorted(p.name for p in ROOT.iterdir() if (p / "case.json").exists())


def test_there_are_at_least_four_cases():
    assert len(CASES) >= 4


def _strip(r: dict, ignore: dict) -> dict:
    for f in ignore.get(r.get("entity"), []) or []:
        r.pop(f, None)
    return r


@pytest.mark.parametrize("case", CASES)
def test_case(case: str, tmp_path: Path):
    d = ROOT / case
    c = json.loads((d / "case.json").read_text(encoding="utf-8"))
    server = None
    try:
        vars = {"caseDir": str(d.resolve()).replace("\\", "/"), "workDir": str(tmp_path.resolve()).replace("\\", "/")}
        if "server" in c:
            routes = json.loads((d / c["server"]["routes"]).read_text(encoding="utf-8"))
            server = FixtureServer(d, routes)
            vars["baseUrl"] = server.base_url
        conn_json = T.render_json(c["connection"], T.Vars.of(vars), T.LENIENT, None)
        conn = ConnectionConfig.from_json(conn_json)
        if "sqlite" in (c.get("setup") or {}):
            db = sqlite3.connect(conn.properties["path"])
            try:
                script = (d / c["setup"]["sqlite"]).read_text(encoding="utf-8")
                for stmt in re.split(r";\s*\n", script):
                    if not J.is_blank(stmt):
                        db.execute(stmt)
                db.commit()
            finally:
                db.close()
        af = AgentFrontier()
        sink_name = "conformance-" + case
        report = af.run(PipelineConfig.of(conn, J.strings((c.get("pipeline") or {}).get("aspects")),
                                          [{"type": "memory", "name": sink_name}]))
        assert report.status == "SUCCEEDED", report.errors
        records = MemorySink.get(sink_name).records()
        MemorySink.forget(sink_name)
        ignore = c.get("ignore") or {}
        actual = {J.canonical(_strip(r.to_json(), ignore)) for r in records}
        expected_file = d / (c.get("expected") or "expected.jsonl")
        expected = set()
        for line in expected_file.read_text(encoding="utf-8").splitlines():
            if not J.is_blank(line):
                expected.add(J.canonical(_strip(json.loads(line), ignore)))
        if expected != actual:
            out = Path(__file__).parents[1] / "out" / "conformance-actual" / (case + ".jsonl")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(sorted(actual)) + "\n", encoding="utf-8")
            pytest.fail("records differ (actual written to " + str(out) + ")\nmissing:\n"
                        + "\n".join(sorted(expected - actual)) + "\nunexpected:\n" + "\n".join(sorted(actual - expected)))

        if "sample" in c:
            sm = c["sample"]
            scope = Scope.of({k: J.jstr(v) for k, v in (sm.get("scope") or {}).items()})
            with af.open(conn) as s:
                rows = [r.values for r in s.sample(scope, J.as_text(sm["dataset"]), int(sm.get("limit", 5)))]
            want = json.loads((d / "expected-sample.json").read_text(encoding="utf-8"))
            assert J.canonical(rows) == J.canonical(want), "sample rows differ"
    finally:
        if server is not None:
            server.close()
