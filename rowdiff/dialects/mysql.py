"""MySQL / MariaDB adapter (adapter only: SQL unit tested, no live server here).

``GROUP_CONCAT`` truncates at ``group_concat_max_len`` (1024 bytes by default),
which would silently fold a chunk into a wrong digest, so the session value is
raised to cover the largest realistic chunk: one digest per row.
"""

from __future__ import annotations

from ..dsn import DSN
from ..errors import ConfigError, SourceError
from .base import Column, Dialect

GROUP_CONCAT_MAX_LEN = 64 * 1024 * 1024


class MySQL(Dialect):
    name = "mysql"
    placeholder = "%s"
    row_values = True

    def connect(self, dsn: DSN):
        try:
            import pymysql
        except ImportError:
            pymysql = None
        if pymysql is not None:
            return pymysql.connect(**_params(dsn), autocommit=True)
        try:
            import MySQLdb
        except ImportError as exc:
            raise SourceError(
                "the mysql driver is missing; pip install 'rowdiff[mysql]'") from exc
        conn = MySQLdb.connect(**_params(dsn))
        conn.autocommit(True)
        return conn

    def prepare(self, conn):
        cur = conn.cursor()
        try:
            cur.execute("SET SESSION group_concat_max_len = %d"
                        % GROUP_CONCAT_MAX_LEN)
        finally:
            cur.close()

    def table_columns(self, conn, table):
        schema, name = _split_table(table, conn)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT column_name, data_type, column_type"
                " FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s"
                " ORDER BY ordinal_position", (schema, name))
            found = cur.fetchall()
            cur.execute(
                "SELECT column_name FROM information_schema.statistics"
                " WHERE table_schema = %s AND table_name = %s"
                "  AND index_name = 'PRIMARY' ORDER BY seq_in_index",
                (schema, name))
            pk = [row[0] for row in cur.fetchall()]
        finally:
            cur.close()
        return [Column(n, t, full, is_pk=(n in pk))
                for n, t, full in found]

    def quote(self, ident):
        if "`" in ident or "\\" in ident or "\x00" in ident:
            raise ConfigError("unsafe identifier %r" % ident)
        return "`%s`" % ident

    def null_literal(self):
        return "'\\\\N'"

    def mod(self, expr, n):
        return "MOD(%s, %d)" % (expr, n)

    def column_expr(self, column):
        ref = self.quote(column.name)
        null = self.null_literal()
        if column.category == "bool":
            return ("(CASE WHEN %s IS NULL THEN %s WHEN LOWER(CAST(%s AS CHAR))"
                    " IN ('0', 'false', 'f', 'no', '') THEN 'false'"
                    " ELSE 'true' END)" % (ref, null, ref))
        if column.category == "bytes":
            return "(CASE WHEN %s IS NULL THEN %s ELSE CONCAT('x', HEX(%s)) END)" % (
                ref, null, ref)
        return "(COALESCE(CAST(%s AS CHAR), %s))" % (ref, null)

    def row_expr(self, columns):
        # CHAR(31) is VARBINARY here, which would poison the digest input.
        return "(CONCAT_WS(CONVERT(0x1F USING UTF8MB4), %s))" % ", ".join(
            self.column_expr(c) for c in columns)

    def digest(self, expr):
        if self.algorithm == "md5":
            return "MD5(%s)" % expr
        return "SHA2(%s, 256)" % expr

    def checksum_sql(self, table, pk, columns, lo, hi, where=None):
        where, params = self.range_sql(pk, lo, hi, where=where)
        inner = self.digest(self.row_expr(columns))
        folded = "GROUP_CONCAT(%s ORDER BY %s SEPARATOR '')" % (inner, inner)
        return ("SELECT COUNT(*), %s FROM %s WHERE %s"
                % (self.digest(folded), self.quote_table(table),
                   where)), params


def _params(dsn: DSN):
    settings = {"database": dsn.database}
    if dsn.host:
        settings["host"] = dsn.host
    if dsn.port:
        settings["port"] = int(dsn.port)
    if dsn.user:
        settings["user"] = dsn.user
    if dsn.password:
        settings["password"] = dsn.password
    settings.update(dsn.params)
    return settings


def _split_table(table, conn):
    parts = table.split(".")
    if len(parts) > 1:
        return parts[-2], parts[-1]
    cur = conn.cursor()
    try:
        cur.execute("SELECT DATABASE()")
        schema = cur.fetchone()[0]
    finally:
        cur.close()
    return schema, parts[0]
