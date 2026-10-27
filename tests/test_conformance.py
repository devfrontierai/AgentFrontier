"""Runs every case under tests/conformance (a copy of the shared spec/conformance); see its README.

The expected files are the contract shared with the Java edition: they are never regenerated here. On a mismatch the
actual records are written to ``out/conformance-actual/<case>.jsonl`` (``<case>-envelope.jsonl`` for events) for
inspection.
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
from agentfrontier.envelope import event_id

sys.path.insert(0, str(Path(__file__).parent))
from fixture_server import FixtureServer  # noqa: E402

ROOT = Path(__file__).parent / "conformance"
CASES = sorted(p.name for p in ROOT.iterdir() if (p / "case.json").exists())
ENVELOPE_CASES = [c for c in CASES if "envelope" in json.loads((ROOT / c / "case.json").read_text(encoding="utf-8"))]
# envelope properties that depend on the run, not on the source
RUN_DEPENDENT = ("runId", "observedAt", "eventId")


def test_there_are_at_least_four_cases():
    assert len(CASES) >= 4


def _strip(r: dict, ignore: dict) -> dict:
    for f in ignore.get(r.get("entity"), []) or []:
        r.pop(f, None)
    return r


def _expected(file: Path, normalise) -> set[str]:
    """Normalised expected records; empty when the file does not exist yet (the comparison then fails and writes the
    actual records for review)."""
    out: set[str] = set()
    if not file.exists():
        return out
    for line in file.read_text(encoding="utf-8").splitlines():
        if not J.is_blank(line):
            out.add(J.canonical(normalise(json.loads(line))))
    return out


def _compare(case: str, suffix: str, expected: set[str], actual: set[str]) -> None:
    if expected != actual:
        out = Path(__file__).parents[1] / "out" / "conformance-actual" / (case + suffix + ".jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(sorted(actual)) + "\n", encoding="utf-8")
        pytest.fail("records differ (actual written to " + str(out) + ")\nmissing:\n"
                    + "\n".join(sorted(expected - actual)) + "\nunexpected:\n" + "\n".join(sorted(actual - expected)))


class _Case:
    """Sets up one case (fixture server, SQLite database) and runs its pipeline into a memory sink."""

    def __init__(self, case: str, tmp_path: Path):
        self.name = case
        self.dir = ROOT / case
        self.spec = json.loads((self.dir / "case.json").read_text(encoding="utf-8"))
        self.server = None
        vars = {"caseDir": str(self.dir.resolve()).replace("\\", "/"),
                "workDir": str(tmp_path.resolve()).replace("\\", "/")}
        if "server" in self.spec:
            routes = json.loads((self.dir / self.spec["server"]["routes"]).read_text(encoding="utf-8"))
            self.server = FixtureServer(self.dir, routes)
            vars["baseUrl"] = self.server.base_url
        self.conn = ConnectionConfig.from_json(T.render_json(self.spec["connection"], T.Vars.of(vars), T.LENIENT, None))
        if "sqlite" in (self.spec.get("setup") or {}):
            db = sqlite3.connect(self.conn.properties["path"])
            try:
                script = (self.dir / self.spec["setup"]["sqlite"]).read_text(encoding="utf-8")
                for stmt in re.split(r";\s*\n", script):
                    if not J.is_blank(stmt):
                        db.execute(stmt)
                db.commit()
            finally:
                db.close()
        self.af = AgentFrontier()
        self.ignore = self.spec.get("ignore") or {}

    def run(self, envelope: bool = False, tenant_id: str | None = None) -> list:
        sink_name = "conformance-" + self.name + ("-envelope" if envelope else "")
        config = PipelineConfig.of(self.conn, J.strings((self.spec.get("pipeline") or {}).get("aspects")),
                                   [{"type": "memory", "name": sink_name}])
        config.envelope = envelope
        config.tenant_id = tenant_id
        report = self.af.run(config)
        assert report.status == "SUCCEEDED", report.errors
        records = MemorySink.get(sink_name).records()
        MemorySink.forget(sink_name)
        return records

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


@pytest.fixture
def open_case(tmp_path: Path):
    cases: list[_Case] = []

    def open_(name: str) -> _Case:
        cases.append(_Case(name, tmp_path))
        return cases[-1]

    yield open_
    for c in cases:
        c.close()


@pytest.mark.parametrize("case", CASES)
def test_case(case: str, open_case):
    c = open_case(case)
    records = c.run()
    actual = {J.canonical(_strip(r.to_json(), c.ignore)) for r in records}
    expected = _expected(c.dir / (c.spec.get("expected") or "expected.jsonl"), lambda r: _strip(r, c.ignore))
    _compare(case, "", expected, actual)

    if "sample" in c.spec:
        sm = c.spec["sample"]
        scope = Scope.of({k: J.jstr(v) for k, v in (sm.get("scope") or {}).items()})
        with c.af.open(c.conn) as s:
            rows = [r.values for r in s.sample(scope, J.as_text(sm["dataset"]), int(sm.get("limit", 5)))]
        want = json.loads((c.dir / "expected-sample.json").read_text(encoding="utf-8"))
        assert J.canonical(rows) == J.canonical(want), "sample rows differ"


@pytest.mark.parametrize("case", ENVELOPE_CASES)
def test_case_envelope(case: str, open_case):
    """The same pipeline with the envelope on: events compared without their run-dependent properties, and every
    eventId recomputed from the event (spec docs/envelope.md)."""
    c = open_case(case)
    env_spec = c.spec["envelope"]
    events = [e.to_json() for e in c.run(envelope=True, tenant_id=env_spec.get("tenantId"))]
    for e in events:
        assert e["eventId"] == event_id(e["tenantId"], e["sourceSystemId"], e["entity"], e["nativeObjectId"],
                                        e["op"], e["body"]), e

    def normalise(e: dict) -> dict:
        for k in RUN_DEPENDENT:
            e.pop(k, None)
        _strip(e["body"], c.ignore)
        return e

    actual = {J.canonical(normalise(e)) for e in events}
    _compare(case, "-envelope", _expected(c.dir / env_spec["expected"], normalise), actual)


def test_event_id_vectors():
    """The shared eventId test vectors: every implementation computes the same ids."""
    vectors = json.loads((ROOT / "event-ids.json").read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 5
    for v in vectors:
        i = v["input"]
        assert event_id(i["tenantId"], i["sourceSystemId"], i["entity"], i["nativeObjectId"], i["op"], i["body"]) \
            == v["eventId"], v["description"]
