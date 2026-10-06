"""``agentfrontier <command> [arguments] [options]``. Results go to standard output as JSON, logs to standard error,
so the CLI composes with anything. Exit code 0 on success, 1 on failure, 2 on bad usage."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from . import json_util as J
from .definitions import DefinitionRegistry
from .errors import AgentFrontierError
from .model import ConnectionConfig, QueryRequest, Scope
from .pipeline import PipelineConfig, RunReport
from .sdk import AgentFrontier

USAGE = """AgentFrontier: one engine per protocol, one JSON file per source.

Usage: agentfrontier <command> [arguments] [options]

Commands
  definitions                         list source definitions
  describe <type>                     properties, auth methods, hierarchy and operations of a definition
  validate [file...]                  check definitions (all, or the given files), connections or pipelines
  test <connection.json>              connect, detect the version, run the test operation
  extract <connection.json>           extract to sinks
      --aspects metadata,lineage,...  what to extract (default metadata)
      --sink jsonl:<dir> | json:<file> | console | <sink.json>   (repeatable; default console)
      --parallelism N  --page-size N  --batch-size N
  run <pipeline.json>                 run a pipeline file
  query <connection.json> "<sql>"     read-only query  [--max-rows N] [--scope level=value,...]
  sample <connection.json> <dataset>  first rows of a dataset [--scope level=value,...] [--limit N]

Options
  --definitions <dir>                 extra definitions or overlays (repeatable)
  --drivers <dir>                     Java edition only (JDBC driver jars); accepted and ignored here
"""


class _Usage(Exception):
    pass


def _err(s: str) -> None:
    sys.stderr.write(s + "\n")
    sys.stderr.flush()


def _print(o: Any) -> None:
    sys.stdout.write(J.pretty(o) + "\n")
    sys.stdout.flush()


def _need(p: list[str], i: int, what: str) -> str:
    if len(p) <= i:
        raise _Usage("missing " + what + " (see --help)")
    return p[i]


def _int_opt(opts: dict[str, list[str]], key: str) -> int:
    v = opts.get(key)
    if v is None:
        return 0
    try:
        return J.parse_int(v[0])
    except ValueError:
        raise _Usage("--" + key + " needs a whole number, got '" + v[0] + "'") from None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or "--help" in args or "-h" in args:
        sys.stdout.write(USAGE)
        return 2 if not args else 0
    opts: dict[str, list[str]] = {}
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                value = args[i + 1]
                i += 1
            else:
                value = "true"
            opts.setdefault(a[2:], []).append(value)
        else:
            positional.append(a)
        i += 1
    if "drivers" in opts:
        _err("note: --drivers is used by the Java edition only (JDBC jars); the Python edition loads DB-API "
             "drivers installed with pip and ignores it")
    try:
        af = AgentFrontier(definitions=[Path(d) for d in opts.get("definitions", [])])
        if not positional:
            raise _Usage("missing command (see --help)")
        cmd = positional.pop(0)
        if cmd == "definitions":
            return _definitions(af)
        if cmd == "describe":
            return _describe(af, _need(positional, 0, "type"))
        if cmd == "validate":
            return _validate(af, positional)
        if cmd == "test":
            return _test(af, _connection(_need(positional, 0, "connection file")))
        if cmd == "extract":
            return _extract(af, _need(positional, 0, "connection file"), opts)
        if cmd == "run":
            cfg = PipelineConfig.from_file(_need(positional, 0, "pipeline file"))
            return _report(af.run(cfg, lambda e, d: _err("[" + e + "] " + J.write(d))))
        if cmd == "query":
            return _query(af, _connection(_need(positional, 0, "connection file")), _need(positional, 1, "sql"),
                          opts)
        if cmd == "sample":
            return _sample(af, _connection(_need(positional, 0, "connection file")), _need(positional, 1, "dataset"),
                           opts)
        _err("unknown command '" + cmd + "'\n")
        sys.stderr.write(USAGE)
        return 2
    except _Usage as e:
        _err(str(e))
        return 2
    except AgentFrontierError as e:
        _err(e.code + ": " + e.message)
        return 1
    except Exception as e:  # noqa: BLE001
        _err("error: " + type(e).__name__ + ": " + str(e))
        return 1


def _connection(file: str) -> ConnectionConfig:
    return ConnectionConfig.from_file(file)


def _definitions(af: AgentFrontier) -> int:
    out = []
    for d in af.list_definitions():
        out.append({"id": d.id, "name": d.name, "protocol": d.protocol, "category": d.category, "status": d.status,
                    "auth": d.auth, "from": af.definitions.origin(d.id)})
    _print(out)
    return 0


def _describe(af: AgentFrontier, type_: str) -> int:
    d = af.definitions.get(type_)
    auth = []
    for m in af.auth_methods(d):
        auth.append({"id": m.id, "name": m.name,
                     "fields": [J.jstr(f.key) + ("" if f.required else "?") + (" (secret)" if f.secret else "")
                                for f in m.fields]})
    n = {"id": d.id, "name": d.name, "protocol": d.protocol, "category": d.category, "status": d.status,
         "description": d.description, "properties": [f.to_dict() for f in d.properties],
         "options": [f.to_dict() for f in d.options], "auth": auth,
         "hierarchy": [lv.to_dict() for lv in d.hierarchy],
         "operations": {k: J.jstr(J.text(v, "entity")) + " @ " + J.text(v, "scope", "source")
                        for k, v in d.operations.items()},
         "overlays": J.deep_copy(d.raw.get("overlays"))}
    _print(n)
    return 0


def _validate(af: AgentFrontier, files: list[str]) -> int:
    problems: list[str] = []
    if not files:
        problems.extend(af.definitions.validate_all())
    for f in files:
        try:
            n = J.loads_first(Path(f).read_text(encoding="utf-8"))
            if not isinstance(n, dict):
                raise AgentFrontierError.invalid_config("not a JSON object")
            if "overlay" in n:
                if af.definitions.find(J.as_text(n["overlay"])) is None:
                    problems.append(f + ": overlay for unknown definition")
            elif "protocol" in n or "extends" in n:
                reg: DefinitionRegistry = af.definitions
                reg.add(n, f)
                problems.extend(f + ": " + p for p in DefinitionRegistry.validate(reg.get(J.as_text(n.get("id")))))
            elif "sinks" in n:
                p = PipelineConfig.from_json(n, Path(f).absolute().parent)
                problems.extend(f + ": " + x for x in af.validate(p.connection))
            else:
                problems.extend(f + ": " + x for x in af.validate(ConnectionConfig.from_json(n)))
        except AgentFrontierError as e:
            problems.append(f + ": " + e.message)
        except Exception as e:  # noqa: BLE001
            problems.append(f + ": " + str(e))
    _print({"ok": not problems, "problems": problems})
    return 0 if not problems else 1


def _test(af: AgentFrontier, c: ConnectionConfig) -> int:
    t = af.test(c)
    _print(t.to_dict())
    return 0 if t.ok else 1


def _sink_spec(s: str) -> dict:
    if s.endswith(".json") and Path(s).exists():
        try:
            return J.loads_first(Path(s).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AgentFrontierError.invalid_config("cannot read sink file " + s) from None
    colon = s.find(":")
    if colon < 0:
        return {"type": s}
    return {"type": s[:colon], "path": s[colon + 1:]}


def _extract(af: AgentFrontier, file: str, opts: dict[str, list[str]]) -> int:
    sinks = [_sink_spec(s) for s in opts.get("sink", ["console"])]
    aspects = opts["aspects"][0].split(",") if "aspects" in opts else ["metadata"]
    p = PipelineConfig(_connection(file), aspects, None, None, sinks, _int_opt(opts, "page-size"),
                       _int_opt(opts, "batch-size"), _int_opt(opts, "parallelism"), _int_opt(opts, "sample-rows"))
    to_console = any(J.text(s, "type") == "console" for s in sinks)

    def listener(event: str, data: dict) -> None:
        if not to_console or event == "error":
            _err("[" + event + "] " + J.write(data))

    r = af.run(p, listener)
    if to_console:
        _err(J.write(r.to_map()))
        return 1 if r.status == "FAILED" else 0
    return _report(r)


def _report(r: RunReport) -> int:
    _print(r.to_map())
    return 1 if r.status == "FAILED" else 0


def _scope(opts: dict[str, list[str]]) -> Scope:
    s = Scope.root()
    for spec in opts.get("scope", []):
        for part in spec.split(","):
            eq = part.find("=")
            if eq > 0:
                s = s.with_(J.jtrim(part[:eq]), J.jtrim(part[eq + 1:]))
    return s


def _query(af: AgentFrontier, c: ConnectionConfig, sql: str, opts: dict[str, list[str]]) -> int:
    with af.open(c) as s:
        r = s.query(QueryRequest(sql, _scope(opts), _int_opt(opts, "max-rows"), 0, []))
        _print(r.to_dict())
        return 0


def _sample(af: AgentFrontier, c: ConnectionConfig, dataset: str, opts: dict[str, list[str]]) -> int:
    with af.open(c) as s:
        limit = _int_opt(opts, "limit")
        rows = s.sample(_scope(opts), dataset, 10 if limit <= 0 else limit)
        _print([r.values for r in rows])
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
