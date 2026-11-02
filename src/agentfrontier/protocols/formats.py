"""Schemas of binary file formats (spec docs/storage.md): Avro from the container file header (standard library),
Parquet from the file footer (``pip install "agentfrontier[parquet]"``; without it Parquet files have no fields)."""

from __future__ import annotations

import datetime as _dt
import decimal
import io
import json
import logging
from typing import TYPE_CHECKING, Any

from .. import json_util as J

if TYPE_CHECKING:  # pragma: no cover
    from .storage import Storage

log = logging.getLogger("agentfrontier.files")

AVRO_MAGIC = b"Obj\x01"
PARQUET_MAGIC = b"PAR1"
_TAIL = 64 * 1024


# -------------------------------------------------------------------------------------------------------------- Avro


def _zigzag(data: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    acc = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated Avro header")
        b = data[pos]
        pos += 1
        acc |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
    return (acc >> 1) ^ -(acc & 1), pos


def avro_schema(data: bytes) -> Any:
    """The writer schema of an Avro object container file (its ``avro.schema`` metadata entry)."""
    if not data.startswith(AVRO_MAGIC):
        raise ValueError("not an Avro container file")
    pos = len(AVRO_MAGIC)
    while True:
        count, pos = _zigzag(data, pos)
        if count == 0:
            break
        if count < 0:
            count = -count
            _, pos = _zigzag(data, pos)  # block size in bytes
        for _ in range(count):
            n, pos = _zigzag(data, pos)
            key = data[pos:pos + n].decode("utf-8")
            pos += n
            n, pos = _zigzag(data, pos)
            value = data[pos:pos + n]
            pos += n
            if key == "avro.schema":
                return json.loads(value.decode("utf-8"))
    raise ValueError("the Avro header has no avro.schema")


def _avro_type(t: Any) -> tuple[str, bool]:
    """(type name, nullable) of a field type: a union with null is nullable with the other branch's type, a logical
    type wins over the underlying one, a named complex type is written as its kind."""
    nullable = False
    if isinstance(t, list):
        others = [x for x in t if x != "null"]
        nullable = len(others) < len(t)
        if len(others) != 1:
            return "union", nullable
        t = others[0]
    if isinstance(t, dict):
        return J.jstr(t.get("logicalType") or t.get("type")), nullable
    return J.jstr(t), nullable


def avro_fields(data: bytes) -> list[dict]:
    try:
        schema = avro_schema(data)
    except (ValueError, UnicodeDecodeError) as e:
        log.debug("no Avro schema: %s", e)
        return []
    if not isinstance(schema, dict) or schema.get("type") != "record":
        return []
    out = []
    for i, f in enumerate(schema.get("fields") or []):
        t, nullable = _avro_type(f.get("type"))
        out.append({"name": f.get("name"), "ordinal": i + 1, "data_type": t, "nullable": nullable, "parent": None})
    return out


# ----------------------------------------------------------------------------------------------------------- Parquet


def _pyarrow() -> Any:
    try:
        import pyarrow.parquet as pq
        return pq
    except ImportError:
        return None


def _footer_buffer(storage: "Storage", path: str) -> Any:
    """A file-like object holding enough of a Parquet file to read its footer: the whole local file, or the tail
    behind a fake leading magic number (the footer never points before itself for schema reads)."""
    local = storage.local_path(path)
    if local is not None:
        return local
    tail = storage.read_tail(path, _TAIL)
    if len(tail) < 8 or tail[-4:] != PARQUET_MAGIC:
        raise ValueError("not a Parquet file")
    need = int.from_bytes(tail[-8:-4], "little") + 8
    if need > len(tail):
        tail = storage.read_tail(path, need)
    return io.BytesIO(PARQUET_MAGIC + tail[-need:])


def parquet_fields(storage: "Storage", path: str) -> list[dict]:
    pq = _pyarrow()
    if pq is None:
        log.debug("pyarrow is not installed: no fields for %s", path)
        return []
    try:
        schema = pq.read_schema(_footer_buffer(storage, path))
    except Exception as e:  # noqa: BLE001 - a broken file has no fields
        log.debug("cannot read the Parquet footer of %s: %s", path, e)
        return []
    return [{"name": f.name, "ordinal": i + 1, "data_type": str(f.type), "nullable": bool(f.nullable),
             "parent": None} for i, f in enumerate(schema)]


def _plain(v: Any) -> Any:
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def parquet_rows(storage: "Storage", path: str, limit: int, max_bytes: int) -> list[dict]:
    pq = _pyarrow()
    if pq is None:
        return []
    local = storage.local_path(path)
    if local is not None:
        source: Any = local
    else:
        data, whole = storage.read(path, max_bytes)
        if not whole:
            return []
        source = io.BytesIO(data)
    try:
        f = pq.ParquetFile(source)
        for batch in f.iter_batches(batch_size=max(1, limit)):
            return [{k: _plain(v) for k, v in r.items()} for r in batch.to_pylist()[:limit]]
    except Exception as e:  # noqa: BLE001
        log.debug("cannot read rows of %s: %s", path, e)
    return []


__all__ = ["avro_schema", "avro_fields", "parquet_fields", "parquet_rows"]
