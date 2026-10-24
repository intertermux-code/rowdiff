"""Canonical value encoding shared by the SQL pushdown and the Python fallback.

A row is compared as the concatenation of its canonically encoded columns. The
SQL side builds the same string with per-dialect expressions; the Python side
builds it here. Both must agree, otherwise a chunk would look unchanged in SQL
and changed in the row report (or the other way round).
"""

from __future__ import annotations

import datetime
import decimal

NULL = "\\N"
SEP = "\x1f"

BOOL = "bool"
BYTES = "bytes"
NUMBER = "number"
TEMPORAL = "temporal"
TEXT = "text"

_TRUE = ("true", "t", "1", "yes", "y")
_FALSE = ("false", "f", "0", "no", "n", "")

TEXT_TYPES = ("char", "varchar", "nchar", "nvarchar", "clob", "text", "uuid",
              "name", "string")
NUMBER_TYPES = ("int", "serial", "numeric", "decimal", "float", "double",
                "real", "money", "bigint", "smallint", "mediumint")
TEMPORAL_TYPES = ("date", "time", "timestamp", "datetime", "year")
BYTES_TYPES = ("blob", "bytea", "binary", "varbinary", "varbin", "bytes")


def classify(type_text, column_type_text=None):
    """Map a declared column type onto one of the five canonical categories.

    ``column_type_text`` is the vendor-specific full definition (MySQL reports
    ``tinyint(1)`` there while ``data_type`` is just ``tinyint``); it only
    carries information for booleans, which SQL dialects spell inconsistently.
    """
    t = (type_text or "").lower()
    full = (column_type_text or type_text or "").lower()
    if "bool" in t or "boolean" in t:
        return BOOL
    if _is_mysql_boolean(full):
        return BOOL
    for prefix in BYTES_TYPES:
        if t.startswith(prefix):
            return BYTES
    for prefix in TEMPORAL_TYPES:
        if t.startswith(prefix):
            return TEMPORAL
    for prefix in NUMBER_TYPES:
        if t.startswith(prefix) or prefix in t:
            return NUMBER
    if any(word in t for word in ("json", "xml", "array", "struct", "map",
                                  "interval", "inet", "cidr", "macaddr",
                                  "enum", "set", "point", "hstore")):
        # Everything exotic is compared through its textual cast; the dialect
        # decides what that cast spells.
        return TEXT
    return TEXT


def _is_mysql_boolean(full):
    return full.startswith("tinyint(1)")


def normalize(value, category):
    """Encode a DBAPI value the way the SQL expression for the same column would."""
    if value is None:
        return NULL
    if category == BOOL:
        return _normalize_bool(value)
    if category == BYTES:
        return _normalize_bytes(value)
    if category == NUMBER:
        return _normalize_number(value)
    if category == TEMPORAL:
        return _normalize_temporal(value)
    if isinstance(value, (bytes, bytearray)):
        return _normalize_bytes(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, decimal.Decimal, float)):
        return _normalize_number(value)
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
        return _normalize_temporal(value)
    return str(value)


def _normalize_bool(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, (int, float, decimal.Decimal)):
        return "false" if value == 0 else "true"
    text = str(value).strip().lower()
    if text in _FALSE:
        return "false"
    if text in _TRUE:
        return "true"
    return "true" if text else "false"


def _normalize_bytes(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return "x" + bytes(value).hex().upper()


def _normalize_number(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "Infinity" if value > 0 else "-Infinity"
        return repr(value)
    return str(value)


def _normalize_temporal(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat(" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    return str(value)
