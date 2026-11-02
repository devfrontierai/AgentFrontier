"""File sinks shared with the Java edition (spec docs/sinks.md): ``csv``, the partitioned layout of ``jsonl``,
``csv`` and ``parquet``, and the ``parquet`` sink itself (Python only, ``pip install "agentfrontier[parquet]"``)."""

from __future__ import annotations

import threading
import urllib.parse
from pathlib import Path
from typing import Any, TextIO

from .. import json_util as J
from ..errors import SINK_FAILED, AgentFrontierError
from ..model import Record
from ..spec_model import EntityModel
from . import RunContext, Sink, run_json

PARTITION_KEYS = ("tenant", "source", "category")
ENVELOPE_COLUMNS = ("eventId", "op", "entity", "nativeObjectId", "tenantId", "sourceSystemId", "connectorId",
                    "runId", "family", "category", "observedAt", "assertion", "confidence")


def partition_by(c: dict) -> list[str]:
    keys = J.strings(c.get("partitionBy"))
    for k in keys:
        if k not in PARTITION_KEYS:
            raise AgentFrontierError.invalid_config("partitionBy accepts " + ", ".join(PARTITION_KEYS) + ", not " + k)
    return keys


def _encode(v: Any) -> str:
    return urllib.parse.quote("null" if v is None else J.jstr(v), safe="-_.~")


def partition_dir(r: Record, keys: list[str], model: EntityModel) -> str:
    """``tenant=acme/source=warehouse/category=metadata`` for the record (empty without partition keys)."""
    parts = []
    env = getattr(r, "envelope", None)
    for k in keys:
        if k == "tenant":
            v = env.get("tenantId") if isinstance(env, dict) else "default"
        elif k == "source":
            v = r.values.get("source")
        else:
            v = model.category_of(r.entity)
        parts.append(k + "=" + _encode(v))
    return "/".join(parts)


def _clear(directory: Path, suffix: str, recursive: bool) -> None:
    if not directory.exists():
        return
    for p in (directory.rglob("*" + suffix) if recursive else directory.glob("*" + suffix)):
        if p.is_file():
            p.unlink()


# -------------------------------------------------------------------------------------------------------- columns


def cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    return J.canonical(v)


def csv_line(cells: list[str]) -> str:
    out = []
    for c in cells:
        if any(ch in c for ch in ',"\r\n'):
            c = '"' + c.replace('"', '""') + '"'
        out.append(c)
    return ",".join(out) + "\n"


class Columns:
    """The columns of one entity's file (docs/sinks.md, "Columns")."""

    def __init__(self, entity: str, levels: list[str], model: EntityModel, envelope: bool):
        self.entity = entity
        self.levels = list(levels)
        self.envelope = envelope
        if entity == "row":
            fields = ["dataset", "values"]
        else:
            e = model.entity(entity)
            fields = [f for f in (e.fields if e is not None else {}) if f not in self.levels]
        self.fields = fields
        self.types = {} if entity == "row" or model.entity(entity) is None else dict(model.entity(entity).fields)
        self.body = ["source"] + self.levels + fields
        self.names = (list(ENVELOPE_COLUMNS) if envelope else []) + self.body + ["extra", "properties"]
        self._known = {"entity", "source", "properties"} | set(self.levels) | set(fields)

    def values(self, r: Record) -> list[Any]:
        """The record's values in column order; ``extra`` and ``properties`` as maps (or None)."""
        out: list[Any] = []
        if self.envelope:
            env = getattr(r, "envelope", {}) or {}
            out.extend(env.get(c) for c in ENVELOPE_COLUMNS)
        v = r.values
        out.extend(v.get(c) for c in self.body)
        extra = {k: x for k, x in v.items() if k not in self._known}
        props = v.get("properties")
        out.append(extra or None)
        out.append(props if isinstance(props, dict) and props else None)
        return out


# -------------------------------------------------------------------------------------------------------- csv


class CsvSink(Sink):
    def __init__(self, directory: str | Path, partition: list[str], model: EntityModel):
        self.dir = Path(directory)
        self.partition = partition
        self.model = model
        self._writers: dict[str, tuple[TextIO, Columns]] = {}
        self._lock = threading.Lock()
        self._ctx: RunContext | None = None

    def begin(self, context: RunContext) -> None:
        self._ctx = context
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _clear(self.dir, ".csv", bool(self.partition))
        except OSError as e:
            raise AgentFrontierError(SINK_FAILED, "cannot prepare " + str(self.dir) + ": " + str(e), e) from e

    def write(self, records: list[Record]) -> None:
        levels = [] if self._ctx is None else list(self._ctx.levels)
        with self._lock:
            try:
                for r in records:
                    sub = partition_dir(r, self.partition, self.model)
                    rel = (sub + "/" if sub else "") + r.entity + ".csv"
                    w = self._writers.get(rel)
                    if w is None:
                        path = self.dir / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        cols = Columns(r.entity, levels, self.model, hasattr(r, "envelope"))
                        f = open(path, "w", encoding="utf-8", newline="")
                        f.write(csv_line(cols.names))
                        w = (f, cols)
                        self._writers[rel] = w
                    w[0].write(csv_line([cell(x) for x in w[1].values(r)]))
            except OSError as e:
                raise AgentFrontierError(SINK_FAILED, "cannot write to " + str(self.dir) + ": " + str(e), e) from e

    def end(self, report: Any) -> None:
        with self._lock:
            try:
                for f, _ in self._writers.values():
                    f.flush()
                run = {} if self._ctx is None else run_json(self._ctx)
                run["report"] = report.to_map()
                self.dir.mkdir(parents=True, exist_ok=True)
                (self.dir / "run.json").write_text(J.pretty(run), encoding="utf-8")
            except OSError as e:
                raise AgentFrontierError(SINK_FAILED, "cannot write run.json: " + str(e), e) from e

    def close(self) -> None:
        with self._lock:
            for f, _ in self._writers.values():
                try:
                    f.close()
                except OSError:
                    pass
            self._writers.clear()


# -------------------------------------------------------------------------------------------------------- parquet


class ParquetSink(Sink):
    """Buffers each file's rows and writes them when the run ends."""

    def __init__(self, directory: str | Path, partition: list[str], model: EntityModel):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            raise AgentFrontierError.invalid_config("the parquet sink needs pyarrow: pip install "
                                                    "\"agentfrontier[parquet]\"") from None
        self.dir = Path(directory)
        self.partition = partition
        self.model = model
        self._files: dict[str, tuple[Columns, list[list[Any]]]] = {}
        self._lock = threading.Lock()
        self._ctx: RunContext | None = None

    def begin(self, context: RunContext) -> None:
        self._ctx = context
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _clear(self.dir, ".parquet", bool(self.partition))
        except OSError as e:
            raise AgentFrontierError(SINK_FAILED, "cannot prepare " + str(self.dir) + ": " + str(e), e) from e

    def write(self, records: list[Record]) -> None:
        levels = [] if self._ctx is None else list(self._ctx.levels)
        with self._lock:
            for r in records:
                sub = partition_dir(r, self.partition, self.model)
                rel = (sub + "/" if sub else "") + r.entity + ".parquet"
                f = self._files.get(rel)
                if f is None:
                    f = (Columns(r.entity, levels, self.model, hasattr(r, "envelope")), [])
                    self._files[rel] = f
                f[1].append(f[0].values(r))

    def _arrow_type(self, cols: Columns, name: str) -> Any:
        import pyarrow as pa
        if name == "confidence" and cols.envelope:
            return pa.float64()
        t = cols.types.get(name) if name in cols.fields else None
        return {"long": pa.int64(), "integer": pa.int64(), "double": pa.float64(),
                "boolean": pa.bool_()}.get(t or "", pa.string())

    def end(self, report: Any) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq
        with self._lock:
            try:
                for rel, (cols, rows) in self._files.items():
                    arrays = []
                    fields = []
                    for i, name in enumerate(cols.names):
                        t = self._arrow_type(cols, name)
                        raw = [row[i] for row in rows]
                        if t == pa.string():
                            data = [None if v is None else (v if isinstance(v, str) else J.canonical(v)) for v in raw]
                        else:
                            data = [_typed(v, t) for v in raw]
                        arrays.append(pa.array(data, type=t))
                        fields.append(pa.field(name, t))
                    path = self.dir / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    pq.write_table(pa.Table.from_arrays(arrays, schema=pa.schema(fields)), str(path))
                run = {} if self._ctx is None else run_json(self._ctx)
                run["report"] = report.to_map()
                (self.dir / "run.json").write_text(J.pretty(run), encoding="utf-8")
            except OSError as e:
                raise AgentFrontierError(SINK_FAILED, "cannot write to " + str(self.dir) + ": " + str(e), e) from e


def _typed(v: Any, t: Any) -> Any:
    import pyarrow as pa
    if v is None:
        return None
    try:
        if t == pa.bool_():
            return v if isinstance(v, bool) else None
        if t == pa.int64():
            return int(v) if not isinstance(v, bool) else None
        if t == pa.float64():
            return float(v) if not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None
    return v


__all__ = ["CsvSink", "ParquetSink", "Columns", "partition_by", "partition_dir", "cell", "csv_line"]
