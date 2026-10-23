"""DuckDB adapter (adapter only: SQL unit tested, driver not installed here)."""

from __future__ import annotations

from ..dsn import DSN
from ..errors import SourceError
from .base import Column, Dialect


class DuckDB(Dialect):
    name = "duckdb"
    placeholder = "?"
    row_values = True

    def connect(self, dsn: DSN):
        try:
            import duckdb
        except ImportError as exc:
            raise SourceError(
                "the duckdb driver is missing; pip install 'rowdiff[duckdb]'") from exc
        return duckdb.connect(dsn.path, read_only=bool(dsn.params.get("read_only")))

    def table_columns(self, conn, table):
        schema, name = _split_table(table)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = ? AND table_name = ?"
                " ORDER BY ordinal_position", (schema, name))
            found = cur.fetchall()
            cur.execute(
                "SELECT constraint_column_names FROM duckdb_constraints()"
                " WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
                (name,))
            pk = []
            for row in cur.fetchall():
                pk.extend(row[0])
        finally:
            cur.close()
        return [Column(n, t, is_pk=(n in pk)) for n, t in found]

    def sep_literal(self):
        return "chr(31)"

    def mod(self, expr, n):
        return "MOD(%s, %d)" % (expr, n)

    def column_expr(self, column):
        ref = self.quote(column.name)
        null = self.null_literal()
        if column.category == "bool":
            return "(CASE WHEN %s IS NULL THEN %s WHEN %s THEN 'true'" \
                   " ELSE 'false' END)" % (ref, null, ref)
        if column.category == "bytes":
            return "(CASE WHEN %s IS NULL THEN %s ELSE 'x' ||" \
                   " UPPER(HEX(%s)) END)" % (ref, null, ref)
        return "(COALESCE(CAST(%s AS VARCHAR), %s))" % (ref, null)

    def row_expr(self, columns):
        return "(CONCAT_WS(%s, %s))" % (
            self.sep_literal(),
            ", ".join(self.column_expr(c) for c in columns))

    def digest(self, expr):
        if self.algorithm == "md5":
            return "MD5(%s)" % expr
        return "SHA256(%s)" % expr

    def checksum_sql(self, table, pk, columns, lo, hi, where=None):
        where, params = self.range_sql(pk, lo, hi, where=where)
        inner = self.digest(self.row_expr(columns))
        folded = "STRING_AGG(%s, '' ORDER BY %s)" % (inner, inner)
        return ("SELECT COUNT(*), %s FROM %s WHERE %s"
                % (self.digest(folded), self.quote_table(table),
                   where)), params


def _split_table(table):
    parts = table.split(".")
    if len(parts) > 1:
        return parts[-2], parts[-1]
    return "main", parts[0]
