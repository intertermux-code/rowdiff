"""Postgres adapter (adapter only: its SQL is unit tested, no live server here)."""

from __future__ import annotations

from ..dsn import DSN
from ..errors import SourceError
from .base import Column, Dialect


class Postgres(Dialect):
    name = "postgres"
    placeholder = "%s"
    row_values = True

    def connect(self, dsn: DSN):
        try:
            import psycopg
        except ImportError:
            psycopg = None
        if psycopg is not None:
            conn = psycopg.connect(_params(dsn), autocommit=True)
            return conn
        try:
            import psycopg2
        except ImportError as exc:
            raise SourceError(
                "the postgres driver is missing; pip install 'rowdiff[postgres]'") from exc
        return psycopg2.connect(_params(dsn))

    def table_columns(self, conn, table):
        schema, name = _split_table(table)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT column_name, data_type, udt_name"
                " FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s"
                " ORDER BY ordinal_position", (schema, name))
            found = cur.fetchall()
            cur.execute(
                "SELECT a.attname FROM pg_index i"
                " JOIN pg_attribute a ON a.attrelid = i.indrelid"
                "  AND a.attnum = ANY(i.indkey)"
                " WHERE i.indrelid = %s::regclass AND i.indisprimary",
                ("\"%s\".\"%s\"" % (schema, name)))
            pk = [row[0] for row in cur.fetchall()]
        finally:
            cur.close()
        return [Column(n, t if t != "USER-DEFINED" else u, is_pk=(n in pk))
                for n, t, u in found]

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
                   " UPPER(ENCODE(%s, 'hex')) END)" % (ref, null, ref)
        return "(COALESCE(CAST(%s AS TEXT), %s))" % (ref, null)

    def row_expr(self, columns):
        return "(CONCAT_WS(%s, %s))" % (
            self.sep_literal(),
            ", ".join(self.column_expr(c) for c in columns))

    def digest(self, expr):
        if self.algorithm == "md5":
            return "MD5(%s)" % expr
        return "ENCODE(SHA256(CONVERT_TO(%s, 'UTF8')), 'hex')" % expr

    def checksum_sql(self, table, pk, columns, lo, hi):
        where, params = self.range_sql(pk, lo, hi)
        inner = self.digest(self.row_expr(columns))
        folded = "STRING_AGG(%s, '' ORDER BY %s)" % (inner, inner)
        return ("SELECT COUNT(*), %s FROM %s WHERE %s"
                % (self.digest(folded), self.quote_table(table),
                   where)), params


def _params(dsn: DSN):
    settings = {"dbname": dsn.database}
    if dsn.host:
        settings["host"] = dsn.host
    if dsn.port:
        settings["port"] = dsn.port
    if dsn.user:
        settings["user"] = dsn.user
    if dsn.password:
        settings["password"] = dsn.password
    settings.update(dsn.params)
    return settings


def _split_table(table):
    parts = table.split(".")
    if len(parts) == 1:
        return "public", parts[0]
    return parts[-2], parts[-1]
