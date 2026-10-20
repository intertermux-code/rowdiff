"""The ``Dialect`` contract.

Everything rowdiff knows about a database lives here: how to quote identifiers,
how to spell a canonical row string, how to fold a chunk into one checksum
inside the database, and how to read rows back when a chunk disagrees.

Two execution strategies exist per dialect:

``checksum_sql``
    Pushdown. The database returns two values per chunk (row count and
    checksum); no row data crosses the wire. This is what SQLite, Postgres,
    MySQL and DuckDB use by default.

``fetch_rows``
    Fallback and mismatch drill-down. Ordered rows are pulled, each value is
    encoded with :func:`rowdiff.values.normalize` and the row is the
    :data:`rowdiff.values.SEP` join of those encodings, which is what the SQL
    expressions built here produce inside the database.
"""

from __future__ import annotations

import hashlib

from ..errors import ConfigError, RowdiffError, SourceError
from ..values import SEP, classify, normalize


class Column:
    __slots__ = ("name", "type_text", "full_type_text", "category", "is_pk")

    def __init__(self, name, type_text, full_type_text=None, is_pk=False):
        self.name = name
        self.type_text = type_text or ""
        self.full_type_text = full_type_text or type_text or ""
        self.category = self.classify()
        self.is_pk = is_pk

    def classify(self):
        return classify(self.type_text, self.full_type_text)

    def __repr__(self):
        return "Column(%r, %r, pk=%r)" % (self.name, self.type_text, self.is_pk)


class Dialect:
    name = "generic"
    placeholder = "?"
    #: False means chunks are checksummed by the client after fetching rows.
    pushdown = True
    #: Row-value comparisons (``(a, b) >= (?, ?)``) are available.
    row_values = False

    def __init__(self, algorithm="md5"):
        if algorithm not in ("md5", "sha256"):
            raise ConfigError("unsupported digest algorithm %r" % algorithm)
        self.algorithm = algorithm

    # -- connection ---------------------------------------------------------

    def connect(self, dsn):
        raise NotImplementedError

    def prepare(self, conn):
        """Per connection setup that pushdown needs (UDFs, session variables)."""

    def pushdown_available(self, conn):
        return True

    def close(self, conn):
        conn.close()

    # -- identifiers --------------------------------------------------------

    def quote(self, ident):
        if "`" in ident or '"' in ident or "\\" in ident or "\x00" in ident:
            raise ConfigError("unsafe identifier %r" % ident)
        return '"%s"' % ident

    def quote_table(self, table):
        parts = table.split(".")
        if not 1 <= len(parts) <= 3 or any(not p for p in parts):
            raise ConfigError("bad table reference %r" % table)
        return ".".join(self.quote(p) for p in parts)

    def bind(self, count):
        return ", ".join([self.placeholder] * count)

    # -- introspection ------------------------------------------------------

    def table_columns(self, conn, table):
        raise NotImplementedError

    def resolve(self, conn, table, pk_names=None, column_names=None):
        """Return ``(columns, pk_columns)`` honouring the user's selection."""
        try:
            all_columns = self.table_columns(conn, table)
        except Exception as exc:
            raise ConfigError("cannot read the columns of %s: %s"
                              % (table, exc)) from exc
        if not all_columns:
            raise ConfigError("table %s has no columns or does not exist" % table)
        by_name = {c.name: c for c in all_columns}
        declared_pk = [c for c in all_columns if c.is_pk]
        if pk_names:
            missing = [p for p in pk_names if p not in by_name]
            if missing:
                raise ConfigError("primary key column(s) not found: %s"
                                  % ", ".join(missing))
            pk = [by_name[p] for p in pk_names]
        elif declared_pk:
            pk = declared_pk
        else:
            raise ConfigError(
                "%s has no primary key; pass --pk" % table)
        if column_names:
            missing = [c for c in column_names if c not in by_name]
            if missing:
                raise ConfigError("column(s) not found: %s" % ", ".join(missing))
            columns = [by_name[c] for c in column_names]
        else:
            columns = all_columns
        for column in pk:
            if column not in columns:
                columns.append(column)
        return columns, pk

    # -- canonical row encoding --------------------------------------------

    def null_literal(self):
        return "'\\N'"

    def sep_literal(self):
        return "char(31)"

    def column_expr(self, column):
        """A non-NULL text expression for one column."""
        raise NotImplementedError

    def row_expr(self, columns):
        raise NotImplementedError

    def digest(self, expr):
        raise NotImplementedError

    def mod(self, expr, n):
        return "%s %% %d" % (expr, n)

    # -- chunk iteration ----------------------------------------------------

    def boundaries_sql(self, table, pk, chunk_size):
        names = ", ".join(self.quote(c.name) for c in pk)
        return (
            "SELECT %s FROM (SELECT %s, row_number() OVER (ORDER BY %s) AS %s"
            " FROM (SELECT DISTINCT %s FROM %s) %s) %s WHERE %s = 1 ORDER BY %s"
            % (names, names, names, self.quote("rowdiff_rn"), names,
               self.quote_table(table), self.quote("rowdiff_d"),
               self.quote("rowdiff_w"),
               self.mod(self.quote("rowdiff_rn"), chunk_size),
               self.quote("rowdiff_rn")))

    def boundaries(self, conn, table, pk, chunk_size):
        sql = self.boundaries_sql(table, pk, chunk_size)
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return [tuple(row) for row in cur.fetchall()]
        except Exception as exc:
            raise RowdiffError("chunk planning failed for %s: %s"
                               % (table, exc)) from exc
        finally:
            cur.close()

    # -- range predicates ---------------------------------------------------

    def _lex(self, pk, values, inclusive):
        """``(a, b, c) >= (x, y, z)`` built from single column comparisons.

        Only the final disjunct may compare inclusively: ``a >= x`` on its own
        would also accept ``a == x`` with a smaller ``b``, which overlaps the
        previous chunk.
        """
        last = ">=" if inclusive else "<"
        strict = ">" if inclusive else "<"
        parts = []
        params = []
        for i in range(len(pk)):
            conds = []
            for j in range(i):
                conds.append("%s = %s" % (self.quote(pk[j].name), self.placeholder))
                params.append(values[j])
            conds.append("%s %s %s" % (self.quote(pk[i].name),
                                       last if i == len(pk) - 1 else strict,
                                       self.placeholder))
            params.append(values[i])
            parts.append("(%s)" % " AND ".join(conds))
        return "(%s)" % " OR ".join(parts), params

    def _row_value(self, pk, values, inclusive):
        op = ">=" if inclusive else "<"
        if len(pk) == 1:
            return "%s %s %s" % (self.quote(pk[0].name), op,
                                 self.placeholder), list(values)
        names = ", ".join(self.quote(c.name) for c in pk)
        return "(%s) %s (%s)" % (names, op, self.bind(len(pk))), list(values)

    def _cmp(self, pk, values, inclusive):
        if self.row_values:
            return self._row_value(pk, values, inclusive)
        return self._lex(pk, values, inclusive)

    def range_sql(self, pk, lo, hi):
        """Half open ``[lo, hi)`` over the primary key, ``None`` means unbounded."""
        clauses, params = [], []
        if lo is not None:
            sql, bound = self._cmp(pk, lo, True)
            clauses.append(sql)
            params.extend(bound)
        if hi is not None:
            sql, bound = self._cmp(pk, hi, False)
            clauses.append(sql)
            params.extend(bound)
        if not clauses:
            return "1 = 1", []
        return " AND ".join(clauses), params

    # -- chunk checksums ----------------------------------------------------

    def checksum_sql(self, table, pk, columns, lo, hi):
        raise NotImplementedError

    def checksum(self, conn, table, pk, columns, lo, hi):
        sql, params = self.checksum_sql(table, pk, columns, lo, hi)
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
        finally:
            cur.close()
        count = int(row[0] or 0)
        digest = row[1]
        if digest is None:
            digest = self.empty_digest()
        return count, str(digest).lower()

    def empty_digest(self):
        return hashlib.new(self.algorithm, b"").hexdigest()

    def client_digest(self, rows, columns):
        """Digest over canonical rows; byte compatible with the pushdown SQL.

        ``fetch_rows`` already encoded every value, so this only joins and
        hashes: normalizing twice would re-encode the hex of a blob.
        """
        digests = []
        for _key, values in rows:
            joined = SEP.join(values[c.name] for c in columns)
            digests.append(
                hashlib.new(self.algorithm, joined.encode("utf-8")).hexdigest())
        return hashlib.new(
            self.algorithm, "".join(sorted(digests)).encode("ascii")).hexdigest()

    def checksum_client(self, conn, table, pk, columns, lo, hi):
        """``--no-pushdown`` path: ordered rows come back, the digest is ours."""
        rows = self.fetch_rows(conn, table, pk, columns, lo, hi)
        return len(rows), self.client_digest(rows, columns)

    def checksum_any(self, conn, table, pk, columns, lo, hi, pushdown=True):
        if pushdown and self.pushdown_available(conn):
            return self.checksum(conn, table, pk, columns, lo, hi)
        return self.checksum_client(conn, table, pk, columns, lo, hi)

    # -- rows ---------------------------------------------------------------

    def selected(self, pk, columns):
        seen, ordered = set(), []
        for column in list(pk) + list(columns):
            if column.name not in seen:
                seen.add(column.name)
                ordered.append(column)
        return ordered

    def select_sql(self, table, pk, columns, lo, hi):
        names = ", ".join(self.quote(c.name) for c in self.selected(pk, columns))
        sql = "SELECT %s FROM %s" % (names, self.quote_table(table))
        where, params = self.range_sql(pk, lo, hi)
        sql += " WHERE " + where
        sql += " ORDER BY " + ", ".join(
            "%s ASC" % self.quote(c.name) for c in pk)
        return sql, params

    def fetch_rows(self, conn, table, pk, columns, lo, hi):
        """Ordered ``(key, canonical_values)`` for ``[lo, hi)``.

        ``canonical_values`` is a dict of column name to the same text the
        SQL expressions in :meth:`row_expr` would have produced, so the
        fallback checksum and the drill-down report agree with pushdown.
        """
        wanted = self.selected(pk, columns)
        out = []
        sql, params = self.select_sql(table, pk, columns, lo, hi)
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            labels = [d[0].lower() for d in cur.description]
            positions = [labels.index(c.name.lower()) for c in wanted]
            for raw in cur.fetchall():
                picked = [raw[i] for i in positions]
                key = tuple(picked[:len(pk)])
                values = {}
                for column, value in zip(wanted, picked):
                    values[column.name] = self.canonical(column, value)
                out.append((key, values))
        finally:
            cur.close()
        return out

    def canonical(self, column, value):
        return normalize(value, column.category)

    def connect_or_fail(self, dsn):
        try:
            return self.connect(dsn)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(
                "cannot connect to %s: %s" % (dsn.redacted, exc)) from exc
