"""Mirrors the Java SpecTest: the built-in spec is consistent and definitions resolve the same way."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agentfrontier import AgentFrontier, AuthConfig, ConnectionConfig, Facts
from agentfrontier import json_util as J

SPEC = Path(__file__).resolve().parents[1] / "src" / "agentfrontier" / "spec"


def test_index_lists_every_definition_file():
    defs = SPEC / "definitions"
    on_disk = set()
    for base, _dirs, names in os.walk(defs):
        for n in names:
            if n.endswith(".json"):
                on_disk.add(os.path.relpath(os.path.join(base, n), defs).replace("\\", "/"))
    index = json.loads((SPEC / "index.json").read_text(encoding="utf-8"))
    assert on_disk == set(J.strings(index.get("definitions"))), \
        "spec/index.json must list exactly the files under spec/definitions"


def test_index_lists_every_sink_mapping():
    index = json.loads((SPEC / "index.json").read_text(encoding="utf-8"))
    on_disk = {p.name for p in (SPEC / "sinks").glob("*.json")}
    assert on_disk == set(J.strings(index.get("sinks")))


def test_every_built_in_definition_is_valid():
    af = AgentFrontier()
    problems = af.definitions.validate_all()
    assert not problems, "\n".join(problems)
    assert len(af.list_definitions()) >= 11


def test_extends_merges_and_does_not_inherit_aliases():
    af = AgentFrontier()
    rs = af.definitions.get("redshift")
    assert rs.protocol == "sql"
    assert rs.connection("jdbc")["driver"] == "org.postgresql.Driver"
    assert rs.connection("python")["module"] == "psycopg"
    assert rs.aliases == []
    assert "lineage" not in rs.operations, "null in a child removes the parent's operation"
    assert rs.overlays == [], "redshift replaces PostgreSQL's version overlays"
    assert af.definitions.canonical_id("postgres") == "postgresql"


def test_external_overlay_file_is_attached_to_its_definition():
    af = AgentFrontier()
    pg = af.definitions.get("postgresql")
    whens = [J.as_text(o["when"]["version"]) for o in pg.overlays]
    assert whens == ["<12", "<11", "<10"]


def test_overlays_apply_by_version():
    af = AgentFrontier()
    pg = af.definitions.get("postgresql")
    modern = J.lines(pg.effective(Facts("16.4", None)).operation("fields")["sql"])
    old = J.lines(pg.effective(Facts("9.6.24", None)).operation("fields")["sql"])
    assert "attgenerated" in modern
    assert "attidentity" not in old
    assert "relispartition" not in J.lines(pg.effective(Facts("9.6.24", None)).operation("datasets")["sql"])


def test_mysql_lineage_needs_eight_zero_thirteen():
    af = AgentFrontier()
    my = af.definitions.get("mysql")
    assert "lineage" in my.effective(Facts("8.4.3", None)).operations
    assert "lineage" not in my.effective(Facts("5.7.44", None)).operations
    assert "lineage" not in my.effective(Facts("11.4.2", "mariadb")).operations


def test_validate_reports_missing_properties_and_auth_fields():
    af = AgentFrontier()
    bad = ConnectionConfig.builder("oracle").auth(AuthConfig.of("username_password", {"username": "x"})).build()
    p = af.validate(bad)
    assert any("service" in x for x in p), p
    assert any("password" in x for x in p), p
    cs = ConnectionConfig.builder("oracle").auth(AuthConfig.connection_string("jdbc:oracle:thin:u/p@//h:1521/s")).build()
    assert af.validate(cs) == [], "a connection string carries the target"
    assert any("not supported" in x for x in
               af.validate(ConnectionConfig.builder("postgresql").auth(AuthConfig.api_key("k")).build()))


def test_connection_json_accepts_both_auth_shapes():
    a = ConnectionConfig.from_json('{"id": "x", "type": "postgresql", "auth": {"method": "username_password", '
                                   '"username": "u", "password": "p"}}')
    b = ConnectionConfig.from_json('{"id": "x", "type": "postgresql", "auth": {"type": "username_password", '
                                   '"values": {"username": "u", "password": "p"}}}')
    assert a.auth.values == b.auth.values
    assert b.auth.method == "username_password"
    assert 'p"' not in str(a), "str() never prints values"


def test_every_sql_definition_has_a_python_block():
    af = AgentFrontier()
    for d in af.list_definitions():
        if d.protocol == "sql":
            assert isinstance(d.connection("python"), dict), d.id


def test_from_environment_reads_definition_directories(tmp_path):
    extra = tmp_path / "defs"
    extra.mkdir()
    (extra / "my_pg.json").write_text(json.dumps({"id": "my_pg", "name": "My PG", "extends": "postgresql"}),
                                      encoding="utf-8")
    af = AgentFrontier.from_environment({"AGENTFRONTIER_DEFINITIONS": str(extra) + os.pathsep
                                         + str(tmp_path / "missing") + ",", "AGENTFRONTIER_DRIVERS": "x"})
    assert af.definition("my_pg") is not None
    assert af.definition("my_pg").protocol == "sql"


def test_spec_copies_match_the_shared_spec():
    """Set AGENTFRONTIER_SPEC_SOURCE to the shared spec folder (for example ../agentfrontier-java/spec) to check
    that the copies in this repository are identical; skipped otherwise."""
    import subprocess
    import sys

    import pytest

    src = os.environ.get("AGENTFRONTIER_SPEC_SOURCE")
    if not src or not (Path(src) / "index.json").is_file():
        pytest.skip("AGENTFRONTIER_SPEC_SOURCE not set")
    script = Path(__file__).resolve().parents[1] / "scripts" / "sync_spec.py"
    r = subprocess.run([sys.executable, str(script), src, "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
