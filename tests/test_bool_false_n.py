"""Pushdown and fallback must agree on every value in the boolean false list.

The fallback (values._FALSE) is the reference. A divergence means the same
data digests differently depending on which path runs.
"""

import sqlite3

import pytest

from rowdiff import values
from rowdiff.dialects.base import Column
from rowdiff.dialects.duckdb import DuckDB
from rowdiff.dialects.mysql import MySQL
from rowdiff.dialects.postgres import Postgres
from rowdiff.dialects.sqlite import SQLite


BOOL_COL = Column("flag", "BOOLEAN", is_pk=False)


def test_fallback_false_list_includes_n():
    assert "n" in values._FALSE


@pytest.mark.parametrize("text", [v for v in values._FALSE if v != ""])
def test_fallback_normalizes_false_values(text):
    assert values.normalize(text, values.BOOL) == "false"


def test_sqlite_pushdown_matches_fallback_on_n(tmp_path):
    db = str(tmp_path / "n.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, flag BOOLEAN)")
        conn.execute("INSERT INTO t VALUES (1, 'n')")
        conn.commit()
        dialect = SQLite("md5")
        dialect.prepare(conn)
        columns, pk = dialect.resolve(conn, "t", None, None)
        pushed_count, pushed = dialect.checksum(conn, "t", pk, columns, None, None)
        client_count, client = dialect.checksum_client(conn, "t", pk, columns, None, None)
        assert pushed_count == client_count == 1
        assert pushed == client
    finally:
        conn.close()


@pytest.mark.parametrize("text", ["0", "false", "f", "no", "n", ""])
def test_sqlite_pushdown_matches_fallback_on_false_values(tmp_path, text):
    db = str(tmp_path / "f.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, flag BOOLEAN)")
        conn.execute("INSERT INTO t VALUES (1, ?)", (text,))
        conn.commit()
        dialect = SQLite("md5")
        dialect.prepare(conn)
        columns, pk = dialect.resolve(conn, "t", None, None)
        pushed_count, pushed = dialect.checksum(conn, "t", pk, columns, None, None)
        client_count, client = dialect.checksum_client(conn, "t", pk, columns, None, None)
        assert pushed_count == client_count == 1
        assert pushed == client, "diverged on %r" % (text,)
    finally:
        conn.close()


def test_sqlite_pushdown_bool_expr_includes_n():
    expr = SQLite().column_expr(BOOL_COL)
    assert "'n'" in expr


def test_mysql_pushdown_bool_expr_includes_n():
    expr = MySQL().column_expr(BOOL_COL)
    assert "'n'" in expr


def test_postgres_pushdown_bool_expr_present():
    # Postgres uses native boolean evaluation, not a text false list.
    expr = Postgres().column_expr(BOOL_COL)
    assert "WHEN" in expr
    assert "'true'" in expr
    assert "'false'" in expr


def test_duckdb_pushdown_bool_expr_present():
    # DuckDB uses native boolean evaluation, not a text false list.
    expr = DuckDB().column_expr(BOOL_COL)
    assert "WHEN" in expr
    assert "'true'" in expr
    assert "'false'" in expr


def test_sqlite_e2e_identical_n_values_no_drift(tmp_path):
    from rowdiff.engine import Options, run

    paths = []
    for name in ("left", "right"):
        path = str(tmp_path / ("%s.db" % name))
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, flag BOOLEAN)")
        conn.execute("INSERT INTO t VALUES (1, 'n')")
        conn.execute("INSERT INTO t VALUES (2, 'false')")
        conn.execute("INSERT INTO t VALUES (3, '0')")
        conn.commit()
        conn.close()
        paths.append(path)
    report = run(Options(left="sqlite://%s" % paths[0], left_table="t",
                         right="sqlite://%s" % paths[1], right_table="t",
                         chunk_size=25))
    assert report["drift"] is False
    assert report["status"] == "ok"
