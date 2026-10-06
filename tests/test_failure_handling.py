"""A run that cannot start still ends with a report (mirrors FailureHandlingTest.java)."""
from __future__ import annotations

import base64
import sqlite3

import pytest

from agentfrontier import AgentFrontier, AgentFrontierError, AuthConfig, ConnectionConfig, PipelineConfig, Scope
from agentfrontier.spec_model import FieldSpec


def test_unreachable_source_gives_failed_report_and_run_json(tmp_path):
    out = tmp_path / "never-created"
    c = ConnectionConfig.of("postgresql", {"host": "127.0.0.1", "port": 1, "connectTimeoutSeconds": 2},
                            AuthConfig.of("username_password", {"username": "u", "password": "p"}))
    r = AgentFrontier.create().run(PipelineConfig.of(c, ["metadata"], [{"type": "jsonl", "path": str(out)}]))
    assert r.status == "FAILED"
    assert r.errors
    assert (out / "run.json").exists()


def test_invalid_sink_config_is_reported_not_raised():
    c = ConnectionConfig.of("sqlite", {"path": "x.db"})
    r = AgentFrontier.create().run(PipelineConfig.of(c, ["metadata"], [{"type": "no-such-sink"}]))
    assert r.status == "FAILED"
    assert "no-such-sink" in r.errors[0]


def test_foreign_cursor_is_rejected(tmp_path):
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    with AgentFrontier.create().open(ConnectionConfig.of("sqlite", {"path": str(db)})) as s:
        bogus = base64.urlsafe_b64encode(b'{"o":7,"v":null}').decode().rstrip("=")
        with pytest.raises(AgentFrontierError) as e:
            s.datasets(Scope.root(), bogus, 10)
        assert e.value.code == "INVALID_CONFIG"


def test_boolean_fields_accept_numbers_and_words():
    f = FieldSpec("x", type="boolean")
    assert f.convert(1) is True
    assert f.convert(0) is False
    assert f.convert("yes") is True
    assert f.convert("false") is False
