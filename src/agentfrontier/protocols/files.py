"""The file storage engine: folders are containers, files are datasets, fields are inferred from content.

Built-in operations (``files.op``): ``stat`` (connection test), ``folders`` (folders holding matching files, root is
``.``), ``files`` (matching files directly in a folder), ``schema`` (inferred fields of one file) and ``rows`` (first
rows of one file). Storage ``local`` covers local disks, network shares and synced cloud folders.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from typing import Any

from .. import json_util as J
from .. import template as T
from ..errors import AgentFrontierError
from ..model import Scope
from . import Protocol, ProtocolContext, ProtocolSession, RawResult
from .inference import BOM, CsvReader, PathGlob, delimited, file_format, json_records, json_schema


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(int(ts // 1), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FilesSession(ProtocolSession):
    def __init__(self, ctx: ProtocolContext):
        self.ctx = ctx
        files = ctx.definition.connection("files")
        storage = J.text(files, "storage", "local")
        if storage != "local":
            raise AgentFrontierError.unsupported("file storage '" + storage + "' is not available in this version")
        r = T.render_text(J.text(files, "root"), ctx.connect_vars({}), T.STRICT, None)
        if r is None:
            raise AgentFrontierError.invalid_config("property 'root' is required")
        self.root = os.path.normpath(os.path.abspath(r))
        h = ctx.hierarchy
        self.folder_level = h[0].level if h else None

    def execute(self, op: dict, vars: T.Vars, scope: Scope) -> RawResult:
        spec = op.get("files") if isinstance(op.get("files"), dict) else {}
        kind = J.text(spec, "op", "")
        start = time.perf_counter_ns()
        rows: list[dict] = []
        try:
            if kind == "stat":
                rows.append(self._stat())
            elif kind == "folders":
                rows.extend(self._folders(self._globs(spec, vars)))
            elif kind == "files":
                rows.extend(self._files(self._globs(spec, vars),
                                        None if self.folder_level is None else scope.get(self.folder_level)))
            elif kind == "schema":
                rows.extend(self._schema(self._file(scope, vars), _int_var(spec.get("sampleRows"), vars, 1000),
                                         J.jstr(vars.get("dataset"))))
            elif kind == "rows":
                rows.extend(self._rows(self._file(scope, vars), _int_var(spec.get("limit"), vars, 100)))
            else:
                raise AgentFrontierError.invalid_definition("unknown files op '" + kind + "'")
            return RawResult.of_rows(rows)
        except (OSError, ValueError) as e:
            raise AgentFrontierError.query("cannot read " + self.root + ": " + str(e), e) from e
        finally:
            self.ctx.stats.record_statement(len(rows), time.perf_counter_ns() - start)

    def _stat(self) -> dict:
        if not os.path.isdir(self.root):
            raise AgentFrontierError.connection("folder not found: " + self.root, None)
        return {"root": self.root, "readable": os.access(self.root, os.R_OK)}

    def _globs(self, spec: dict, vars: T.Vars) -> PathGlob:
        return PathGlob(_list(spec.get("include"), vars), _list(spec.get("exclude"), vars))

    def _matching(self, glob: PathGlob) -> list[str]:
        if not os.path.isdir(self.root):
            raise AgentFrontierError.connection("folder not found: " + self.root, None)
        out = []
        for base, dirs, names in os.walk(self.root):
            dirs.sort()
            for n in names:
                p = os.path.join(base, n)
                if os.path.isfile(p) and glob.accepts(self._relative(p)):
                    out.append(p)
        out.sort()
        return out

    def _relative(self, p: str) -> str:
        return os.path.relpath(p, self.root).replace("\\", "/")

    def _folder_of(self, p: str) -> str:
        rel = self._relative(p)
        i = rel.rfind("/")
        return "." if i < 0 else rel[:i]

    def _folders(self, glob: PathGlob) -> list[dict]:
        counts: dict[str, int] = {}
        for p in self._matching(glob):
            f = self._folder_of(p)
            counts[f] = counts.get(f, 0) + 1
        return [{"name": f, "file_count": counts[f]} for f in sorted(counts)]

    def _files(self, glob: PathGlob, folder: str | None) -> list[dict]:
        out = []
        for p in self._matching(glob):
            if folder is not None and self._folder_of(p) != folder:
                continue
            st = os.stat(p)
            out.append({"name": self._relative(p) if folder is None else os.path.basename(p), "type": "file",
                        "format": file_format(os.path.basename(p)), "size_bytes": st.st_size,
                        "updated_at": _iso(st.st_mtime), "location": self._relative(p)})
        out.sort(key=lambda r: J.jstr(r.get("name")))
        return out

    def _file(self, scope: Scope, vars: T.Vars) -> str:
        dataset = vars.get("dataset")
        if dataset is None:
            raise AgentFrontierError.invalid_definition("files op needs a dataset")
        folder = None if self.folder_level is None else scope.get(self.folder_level)
        ds = J.jstr(dataset)
        p = os.path.join(self.root, ds) if folder is None or folder == "." else os.path.join(self.root, folder, ds)
        p = os.path.normpath(p)
        if os.path.commonpath([os.path.normcase(p), os.path.normcase(self.root)]) != os.path.normcase(self.root):
            raise AgentFrontierError.invalid_config("path escapes the root folder: " + ds)
        return p

    @staticmethod
    def _open(p: str):  # noqa: ANN205
        return open(p, "r", encoding="utf-8", errors="replace", newline="")

    def _schema(self, p: str, sample_rows: int, dataset: str) -> list[dict]:
        fmt = file_format(os.path.basename(p))
        with self._open(p) as stream:
            if fmt == "csv":
                r = delimited(stream, ",", sample_rows)
            elif fmt == "tsv":
                r = delimited(stream, "\t", sample_rows)
            elif fmt in ("json", "jsonl"):
                recs, complete = json_records(stream, fmt == "jsonl", sample_rows)
                r = json_schema(recs, complete)
            else:
                return []
        return [{"dataset": dataset, "name": f.name, "ordinal": f.ordinal, "data_type": f.data_type,
                 "nullable": f.nullable, "parent": f.parent} for f in r.fields]

    def _rows(self, p: str, limit: int) -> list[dict]:
        fmt = file_format(os.path.basename(p))
        out: list[dict] = []
        with self._open(p) as stream:
            if fmt in ("csv", "tsv"):
                csv = CsvReader(stream, "," if fmt == "csv" else "\t")
                header = csv.next()
                while header is not None and len(out) < limit:
                    rec = csv.next()
                    if rec is None:
                        break
                    if len(rec) == 1 and rec[0] == "":
                        continue
                    m: dict[str, Any] = {}
                    for i, h in enumerate(header):
                        h = J.jtrim(h).replace(BOM, "")
                        m["column_" + str(i + 1) if not h else h] = rec[i] if i < len(rec) else None
                    out.append(m)
            elif fmt in ("json", "jsonl"):
                recs, _ = json_records(stream, fmt == "jsonl", limit)
                out.extend(recs)
        return out

    def close(self) -> None:
        pass


def _list(n: Any, vars: T.Vars) -> list[str]:
    if n is None:
        return []
    v = T.render(n, vars, T.LENIENT) if isinstance(n, str) else J.deep_copy(n)
    if isinstance(v, (list, tuple, set)):
        return [J.jstr(x) for x in v]
    if v is None:
        return []
    return [J.jtrim(s) for s in J.jstr(v).split(",") if not J.is_blank(s)]


def _int_var(n: Any, vars: T.Vars, fallback: int) -> int:
    if n is None:
        return fallback
    v = T.render(n, vars, T.LENIENT) if isinstance(n, str) else n
    try:
        return fallback if v is None or J.is_blank(J.jstr(v)) else J.wrap_int32(J.double_to_long(J.parse_double(J.jstr(v))))
    except ValueError:
        return fallback


class FilesProtocol(Protocol):
    id = "files"

    def open(self, context: ProtocolContext) -> ProtocolSession:
        return FilesSession(context)
