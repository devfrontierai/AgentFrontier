"""The file storage engine: folders are containers, files are datasets, fields are inferred from content.

Built-in operations (``files.op``): ``stat`` (connection test), ``folders`` (folders holding matching files, root is
``.``), ``files`` (matching files directly in a folder), ``schema`` (inferred fields of one file), ``rows`` (first
rows of one file), ``tags`` (tags of one file), ``grants`` (who may read the bucket or directory) and ``document``
(records selected from one JSON file, such as a dbt manifest). Storage
``local`` covers local disks, network shares and synced cloud folders; ``s3``, ``adls`` and ``gcs`` read object
storage (spec docs/storage.md).
"""

from __future__ import annotations

import io
import time
from typing import Any

from .. import json_util as J
from .. import mapping as M
from .. import template as T
from ..errors import AgentFrontierError
from ..model import Scope
from . import Protocol, ProtocolContext, ProtocolSession, RawResult
from .formats import avro_fields, parquet_fields, parquet_rows
from .inference import BOM, CsvReader, PathGlob, delimited, file_format, json_records, json_schema
from .storage import DEFAULT_MAX_BYTES, Entry, LocalStorage, Storage, open_storage


class FilesSession(ProtocolSession):
    def __init__(self, ctx: ProtocolContext):
        self.ctx = ctx
        files = ctx.definition.connection("files")
        files = files if isinstance(files, dict) else {}
        self.storage: Storage = open_storage(ctx, files)
        self.max_bytes = _int_var(files.get("maxBytes"), ctx.connect_vars({}), DEFAULT_MAX_BYTES)
        if self.max_bytes <= 0:
            self.max_bytes = DEFAULT_MAX_BYTES
        self.max_document_bytes = _int_var(files.get("maxDocumentBytes"), ctx.connect_vars({}), 256 * 1024 * 1024)
        h = ctx.hierarchy
        self.folder_level = h[0].level if h else None
        self._documents: dict[str, Any] = {}

    @property
    def root(self) -> str:
        return self.storage.describe()

    def execute(self, op: dict, vars: T.Vars, scope: Scope) -> RawResult:
        spec = op.get("files") if isinstance(op.get("files"), dict) else {}
        kind = J.text(spec, "op", "")
        start = time.perf_counter_ns()
        rows: list[dict] = []
        try:
            if kind == "stat":
                rows.append(self.storage.stat())
            elif kind == "folders":
                rows.extend(self._folders(self._globs(spec, vars)))
            elif kind == "files":
                rows.extend(self._files(self._globs(spec, vars),
                                        None if self.folder_level is None else scope.get(self.folder_level)))
            elif kind == "schema":
                rows.extend(self._schema(self._path(scope, vars), _int_var(spec.get("sampleRows"), vars, 1000),
                                         J.jstr(vars.get("dataset"))))
            elif kind == "rows":
                rows.extend(self._rows(self._path(scope, vars), _int_var(spec.get("limit"), vars, 100)))
            elif kind == "tags":
                rows.extend(self.storage.tags(self._path(scope, vars)))
            elif kind == "grants":
                rows.extend(self.storage.grants())
            elif kind == "document":
                rows.extend(self._document(spec, vars))
            else:
                raise AgentFrontierError.invalid_definition("unknown files op '" + kind + "'")
            return RawResult.of_rows(rows)
        except (OSError, ValueError) as e:
            raise AgentFrontierError.query("cannot read " + self.root + ": " + str(e), e) from e
        finally:
            if isinstance(self.storage, LocalStorage):
                self.ctx.stats.record_statement(len(rows), time.perf_counter_ns() - start)

    def _document(self, spec: dict, vars: T.Vars) -> list[dict]:
        """Records selected from one JSON file (spec docs/storage.md, "Documents")."""
        path = T.render_text(J.text(spec, "path", ""), vars, T.STRICT, None)
        if not path:
            raise AgentFrontierError.invalid_config("document op needs a path")
        rel = "/".join(p for p in path.replace("\\", "/").split("/") if p not in ("", "."))
        if ".." in rel.split("/"):
            raise AgentFrontierError.invalid_config("path escapes the root folder: " + path)
        doc = self._documents.get(rel)
        if doc is None:
            data, whole = self.storage.read(rel, self.max_document_bytes)
            if not whole:
                raise AgentFrontierError.query("document " + rel + " is larger than maxDocumentBytes")
            try:
                doc = J.loads_first(data.decode("utf-8", errors="replace"))
            except ValueError as e:
                raise AgentFrontierError.query("document " + rel + " is not JSON: " + str(e), e) from e
            self._documents[rel] = doc
        paths = spec.get("records", "$")
        selected: list = []
        for p in (paths if isinstance(paths, list) else [paths]):
            selected.extend(M.json_path_read(doc, J.jstr(p)))
        where = spec.get("where") if isinstance(spec.get("where"), dict) else {}
        wanted = {k: T.render(v, vars, T.LENIENT) if isinstance(v, str) else J.deep_copy(v)
                  for k, v in where.items()}
        out: list[dict] = []
        each = J.text(spec, "each")
        for o in selected:
            if not isinstance(o, dict) or not all(_holds(T.lookup(o, k), w) for k, w in wanted.items()):
                continue
            if each is None:
                out.append(o)
            else:
                out.extend(x if isinstance(x, dict) else {"value": x} for x in M.json_path_read(o, each))
        return out

    def _globs(self, spec: dict, vars: T.Vars) -> PathGlob:
        return PathGlob(_list(spec.get("include"), vars), _list(spec.get("exclude"), vars))

    def _matching(self, glob: PathGlob) -> list[Entry]:
        return [e for e in self.storage.list() if glob.accepts(e.path)]

    @staticmethod
    def _folder_of(path: str) -> str:
        i = path.rfind("/")
        return "." if i < 0 else path[:i]

    def _folders(self, glob: PathGlob) -> list[dict]:
        counts: dict[str, int] = {}
        for e in self._matching(glob):
            f = self._folder_of(e.path)
            counts[f] = counts.get(f, 0) + 1
        return [{"name": f, "file_count": counts[f]} for f in sorted(counts)]

    def _files(self, glob: PathGlob, folder: str | None) -> list[dict]:
        out = []
        for e in self._matching(glob):
            if folder is not None and self._folder_of(e.path) != folder:
                continue
            base = e.path.rsplit("/", 1)[-1]
            r = {"name": e.path if folder is None else base, "type": "file", "format": file_format(base),
                 "size_bytes": e.size, "updated_at": e.updated_at, "location": e.location}
            r.update(e.properties)
            out.append(r)
        out.sort(key=lambda r: J.jstr(r.get("name")))
        return out

    def _path(self, scope: Scope, vars: T.Vars) -> str:
        dataset = vars.get("dataset")
        if dataset is None:
            raise AgentFrontierError.invalid_definition("files op needs a dataset")
        folder = None if self.folder_level is None else scope.get(self.folder_level)
        ds = J.jstr(dataset)
        path = ds if folder is None or folder == "." else folder + "/" + ds
        parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts:
            raise AgentFrontierError.invalid_config("path escapes the root folder: " + ds)
        return "/".join(parts)

    def _text(self, path: str) -> tuple[io.StringIO | Any, bool]:
        """A text stream over a file: the whole local file, or the first ``maxBytes`` of a remote one (with the
        last, incomplete line dropped when the file is longer)."""
        local = self.storage.local_path(path)
        if local is not None:
            return open(local, "r", encoding="utf-8", errors="replace", newline=""), True
        data, whole = self.storage.read(path, self.max_bytes)
        if not whole:
            cut = data.rfind(b"\n")
            data = data[:cut + 1] if cut >= 0 else b""
        return io.StringIO(data.decode("utf-8", errors="replace"), newline=""), whole

    def _schema(self, path: str, sample_rows: int, dataset: str) -> list[dict]:
        fmt = file_format(path.rsplit("/", 1)[-1])
        if fmt == "avro":
            fields = avro_fields(self.storage.read(path, self.max_bytes)[0])
        elif fmt == "parquet":
            fields = parquet_fields(self.storage, path)
        else:
            stream, whole = self._text(path)
            with stream:
                if fmt == "csv":
                    r = delimited(stream, ",", sample_rows)
                elif fmt == "tsv":
                    r = delimited(stream, "\t", sample_rows)
                elif fmt == "jsonl":
                    recs, complete = json_records(stream, True, sample_rows)
                    r = json_schema(recs, complete)
                elif fmt == "json":
                    if not whole:
                        return []
                    recs, complete = json_records(stream, False, sample_rows)
                    r = json_schema(recs, complete)
                else:
                    return []
            fields = [{"name": f.name, "ordinal": f.ordinal, "data_type": f.data_type, "nullable": f.nullable,
                       "parent": f.parent} for f in r.fields]
        return [dict({"dataset": dataset}, **f) for f in fields]

    def _rows(self, path: str, limit: int) -> list[dict]:
        fmt = file_format(path.rsplit("/", 1)[-1])
        if fmt == "parquet":
            return parquet_rows(self.storage, path, limit, self.max_bytes)
        if fmt not in ("csv", "tsv", "json", "jsonl"):
            return []
        out: list[dict] = []
        stream, whole = self._text(path)
        with stream:
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
            elif fmt == "jsonl" or whole:
                recs, _ = json_records(stream, fmt == "jsonl", limit)
                out.extend(recs)
        return out

    def close(self) -> None:
        pass


def _holds(have: Any, want: Any) -> bool:
    """A ``where`` entry: equal as text, or equal to any item of a list."""
    if have is None:
        return False
    if isinstance(want, (list, tuple)):
        return any(J.jstr(have) == J.jstr(w) for w in want if w is not None)
    return want is not None and J.jstr(have) == J.jstr(want)


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
