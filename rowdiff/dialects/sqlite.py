"""SQLite adapter.

SQLite ships no digest function, so pushdown happens through a user defined
aggregate registered on the connection: SQL builds the canonical row string,
the aggregate folds those strings into one digest per chunk, and only
``(row_count, checksum)`` leaves the engine. ``--no-pushdown``, or a driver that
cannot register aggregates, falls back to client side hashing of ordered rows.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .base import Column, Dialect
from ..errors import ConfigError

FOLD_FUNCTION = "rowdiff_fold"


def make_fold_aggregate(algorithm):
    class _Fold:
        def __init__(self):
            self.rows = []

        def step(self, value):
            text = "" if value is None else str(value)
            self.rows.append(
                hashlib.new(algorithm, text.encode("utf-8")).hexdigest())

        def finalize(self):
            joined = "".join(sorted(self.rows)).encode("ascii")
            return hashlib.new(algorithm, joined).hexdigest()

    return _Fold


class SQLite(Dialect):
    name = "sqlite"
    row_values = False

    def connect(self, dsn):
        conn = sqlite3.connect(dsn.path)
        return conn

    def prepare(self, conn):
        if hasattr(conn, "create_aggregate"):
            conn.create_aggregate(FOLD_FUNCTION, 1,
                                  make_fold_aggregate(self.algorithm))

    def pushdown_available(self, conn):
        return hasattr(conn, "create_aggregate")

    def table_columns(self, conn, table):
        schema, _, name = table.rpartition(".")
        if schema:
            sql = 'PRAGMA %s.table_info(%s)' % (_quote(schema), _quote(name))
        else:
            sql = 'PRAGMA table_info(%s)' % _quote(name)
        cur = conn.cursor()
        try:
            cur.execute(sql)
            rows = cur.fetchall()
        finally:
            cur.close()
        return [Column(row[1], row[2], is_pk=bool(row[5])) for row in rows]

    def column_expr(self, column):
        ref = self.quote(column.name)
        if column.category == "bool":
            return ("(CASE WHEN %s IS NULL THEN %s WHEN CAST(%s AS TEXT)"
                    " IN ('0', 'false', 'f', 'no', '') THEN 'false'"
                    " ELSE 'true' END)" % (ref, self.null_literal(), ref))
        if column.category == "bytes":
            return "(CASE WHEN %s IS NULL THEN %s ELSE 'x' || HEX(%s) END)" % (
                ref, self.null_literal(), ref)
        return "COALESCE(CAST(%s AS TEXT), %s)" % (ref, self.null_literal())

    def row_expr(self, columns):
        # concat_ws() only exists in SQLite 3.44+, so join with the operator.
        sep = " || " + self.sep_literal() + " || "
        return "(" + sep.join(self.column_expr(c) for c in columns) + ")"

    def checksum_sql(self, table, pk, columns, lo, hi, where=None):
        rng, params = self.range_sql(pk, lo, hi, where=where)
        return ("SELECT COUNT(*), %s(%s) FROM %s WHERE %s"
                % (FOLD_FUNCTION, self.row_expr(columns),
                   self.quote_table(table), rng), params)


def _quote(name):
    if '"' in name or "\x00" in name:
        raise ConfigError("unsafe identifier %r" % name)
    return '"%s"' % name
