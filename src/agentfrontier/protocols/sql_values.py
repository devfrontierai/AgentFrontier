"""Converts DB-API values to JSON-friendly ones, the same way every implementation does: integral decimals become
integers, dates ``yyyy-MM-dd``, timestamps ``yyyy-MM-ddTHH:mm:ss`` with six fractional digits only when the fraction is
not zero, binary values a short description, arrays lists. Also the coarse column categories of query results."""

from __future__ import annotations

import datetime as _dt
import json
import struct
import uuid
from decimal import Decimal
from typing import Any

from ..model import Category

MAX_TEXT = 100_000
_LONG_MIN = -(2 ** 63)
_LONG_MAX = 2 ** 63 - 1


def cap(s: str) -> str:
    return s[:MAX_TEXT] + "…" if len(s) > MAX_TEXT else s


def timestamp(t: _dt.datetime) -> str:
    """``yyyy-MM-ddTHH:mm:ss``, plus ``.ffffff`` when there is a fraction."""
    base = "%04d-%02d-%02dT%02d:%02d:%02d" % (t.year, t.month, t.day, t.hour, t.minute, t.second)
    return base if t.microsecond == 0 else base + ".%06d" % t.microsecond


def local_time(t: _dt.time) -> str:
    """Java ``LocalTime.toString``: ``HH:mm``, ``HH:mm:ss`` or with a 3, 6 or 9 digit fraction."""
    s = "%02d:%02d" % (t.hour, t.minute)
    if t.second or t.microsecond:
        s += ":%02d" % t.second
        if t.microsecond:
            if t.microsecond % 1000 == 0:
                s += ".%03d" % (t.microsecond // 1000)
            else:
                s += ".%06d" % t.microsecond
    return s


def offset_id(off: _dt.timedelta) -> str:
    """Java ``ZoneOffset.getId`` with ``Z`` written as ``+00:00``."""
    total = int(off.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    out = "%s%02d:%02d" % (sign, h, m)
    if s:
        out += ":%02d" % s
    return out


def float32_columns(family: str, description: Any) -> set[int]:
    """Indexes of single-precision float columns (PostgreSQL ``real``, MySQL ``FLOAT``, Oracle ``BINARY_FLOAT``).
    JDBC reads them as ``Float`` and widens to double, so the value must go through single precision to match."""
    out = set()
    for i, d in enumerate(description or []):
        code = d[1] if len(d) > 1 else None
        if (family == "psycopg" and code == 700) or (family == "mysql" and code == 4)                 or (family == "oracle" and getattr(code, "name", "") == "DB_TYPE_BINARY_FLOAT"):
            out.add(i)
    return out


def _f32(v: float) -> float:
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except (OverflowError, struct.error):
        return v


def to_json(v: Any, utc: bool = False, single: bool = False) -> Any:
    """One value read from a driver, as a JSON-friendly value (``single``: a single-precision float column)."""
    if v is None:
        return None
    if isinstance(v, str):
        return cap(v)
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if _LONG_MIN <= v <= _LONG_MAX else str(v)
    if isinstance(v, float):
        return _f32(v) if single else v
    if isinstance(v, Decimal):
        if not v.is_finite():
            return float(v)
        if v == 0:
            return 0
        if v == v.to_integral_value():
            return int(v)
        return float(v)
    if isinstance(v, _dt.datetime):
        if v.tzinfo is not None and v.utcoffset() is not None:
            if utc:
                v = v.astimezone(_dt.timezone.utc)
            return timestamp(v.replace(tzinfo=None)) + offset_id(v.utcoffset())
        return timestamp(v)
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, _dt.time):
        return local_time(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary " + str(len(bytes(v))) + " bytes>"
    if isinstance(v, (list, tuple)):
        return [to_json(x, utc, single) for x in v]
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, _dt.timedelta):
        return str(v)
    read = getattr(v, "read", None)
    if callable(read) and type(v).__name__ in ("LOB", "AsyncLOB"):  # oracledb CLOB / BLOB
        data = read()
        if isinstance(data, (bytes, bytearray)):
            return "<binary " + str(len(data)) + " bytes>"
        return None if data is None else cap(str(data))[:MAX_TEXT]
    return str(v)


# ---------------------------------------------------------------------------------------------------------- categories

_PG_NUMBER = {"int2", "int4", "int8", "numeric", "float4", "float8", "oid", "money", "smallint", "integer", "bigint",
              "real", "double precision"}
_PG_TEXT = {"text", "varchar", "bpchar", "char", "name", "citext", "character varying", "character"}


def category_for_name(type_name: str | None) -> Category:
    """Category from a type name (PostgreSQL, MySQL, Oracle and SQLite spellings)."""
    if not type_name:
        return Category.OTHER
    t = type_name.lower()
    if t in _PG_NUMBER or t in ("tinyint", "mediumint", "int", "decimal", "double", "float", "number",
                                "binary_float", "binary_double", "binary_integer", "real", "year"):
        return Category.NUMBER
    if t in _PG_TEXT or t in ("varchar2", "nvarchar2", "nchar", "long", "clob", "nclob", "tinytext", "mediumtext",
                              "longtext", "string", "var_string", "enum", "set"):
        return Category.TEXT
    if t == "date":
        return Category.DATE
    if t in ("timestamp", "timestamptz", "datetime", "timestamp_tz", "timestamp_ltz", "timestamp with time zone",
             "timestamp without time zone", "timestamp with local time zone"):
        return Category.TIMESTAMP
    if t in ("bool", "boolean"):
        return Category.BOOLEAN
    return Category.OTHER


_MYSQL_NAMES = {0: "DECIMAL", 1: "TINYINT", 2: "SMALLINT", 3: "INT", 4: "FLOAT", 5: "DOUBLE", 6: "NULL",
                7: "TIMESTAMP", 8: "BIGINT", 9: "MEDIUMINT", 10: "DATE", 11: "TIME", 12: "DATETIME", 13: "YEAR",
                15: "VARCHAR", 16: "BIT", 245: "JSON", 246: "DECIMAL", 247: "ENUM", 248: "SET", 249: "TINYBLOB",
                250: "MEDIUMBLOB", 251: "LONGBLOB", 252: "BLOB", 253: "VARCHAR", 254: "CHAR", 255: "GEOMETRY"}

_ORACLE_NAMES = {"VARCHAR": "VARCHAR2", "NVARCHAR": "NVARCHAR2", "TIMESTAMP_TZ": "TIMESTAMP WITH TIME ZONE",
                 "TIMESTAMP_LTZ": "TIMESTAMP WITH LOCAL TIME ZONE"}


def column_type(family: str, module: Any, conn: Any, desc: tuple, sample: list) -> tuple[str | None, Category]:
    """Type name and category of a result column, from the driver's ``cursor.description`` entry."""
    code = desc[1] if len(desc) > 1 else None
    if family == "psycopg":
        name = None
        try:
            info = conn.adapters.types.get(code)
            name = info.name if info is not None else None
        except Exception:  # noqa: BLE001 - best effort
            name = None
        cat = category_for_name(name)
        if name in ("bit", "varbit"):
            cat = Category.OTHER
        return name, cat
    if family == "mysql" and isinstance(code, int):
        name = _MYSQL_NAMES.get(code, str(code))
        if name == "BIT":
            return name, Category.OTHER
        if name in ("TINYBLOB", "MEDIUMBLOB", "LONGBLOB", "BLOB"):
            text = any(isinstance(x, str) for x in sample)
            return ("TEXT" if text else "BLOB"), (Category.TEXT if text else Category.OTHER)
        if name == "DATE":
            return name, Category.DATE
        if name in ("TIMESTAMP", "DATETIME"):
            return name, Category.TIMESTAMP
        return name, category_for_name(name)
    if family == "oracle" and code is not None:
        raw = getattr(code, "name", str(code))
        name = raw[8:] if raw.startswith("DB_TYPE_") else raw
        name = _ORACLE_NAMES.get(name, name)
        if name == "DATE" or name.startswith("TIMESTAMP"):
            return name, Category.TIMESTAMP
        return name, category_for_name(name)
    if family == "sqlite":
        first = next((x for x in sample if x is not None), None)
        if isinstance(first, bool) or isinstance(first, int):
            return "INTEGER", Category.NUMBER
        if isinstance(first, float):
            return "REAL", Category.NUMBER
        if isinstance(first, str):
            return "TEXT", Category.TEXT
        if isinstance(first, (bytes, bytearray)):
            return "BLOB", Category.OTHER
        return None, Category.OTHER
    # any other DB-API driver: compare with the module's type objects
    name = None if code is None else str(getattr(code, "name", code))
    try:
        if code is not None:
            if hasattr(module, "NUMBER") and code == module.NUMBER:
                return name, Category.NUMBER
            if hasattr(module, "STRING") and code == module.STRING:
                return name, Category.TEXT
            if hasattr(module, "DATETIME") and code == module.DATETIME:
                return name, Category.TIMESTAMP
    except Exception:  # noqa: BLE001 - type objects of some drivers do not compare
        pass
    return name, category_for_name(name)
