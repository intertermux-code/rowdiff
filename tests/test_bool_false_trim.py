"""Whitespace-padded boolean spellings must digest the same in SQL and Python.

The fallback strips a boolean text value before matching it against
values._FALSE/_TRUE (values.py). The pushdown CASE must TRIM as well, otherwise
' N' digests as true in the database and false in here and two tables that hold
the same booleans in different padding are reported as drift.

SQLite has flexible typing so padded strings are stored and observed here; the
dialect SQL shape is also asserted for MySQL. Postgres and DuckDB evaluate
native booleans (empirically, their native cast does not accept padded
boolean-ish strings, so there is no padded-value divergence path for their
classified BOOLEAN columns) — their expressions are left unchanged and asserted.
"""

import sqlite3

import pytest

from rowdiff import values
from rowdiff.cli import EXIT_OK, EXIT_DRIFT, main
from rowdiff.dialects.base import Column
from rowdiff.dialects.duckdb import DuckDB
from rowdiff.dialects.mysql import MySQL
from rowdiff.dialects.postgres import Postgres
from rowdiff.dialects.sqlite import SQLite

BOOL_COL = Column("flag", "BOOLEAN")


def _padded_spellings():
    """Leading/trailing ASCII whitespace variants derived from _FALSE/_TRUE.

    An empty false spelling ('') cannot carry padding and is excluded. Covers the
    full ASCII whitespace set Python's strip() removes (space, tab, LF, VT, FF,
    CR) so the pushdown TRIM and fallback stay aligned.
    """
    pads = (" ", "\t", "\n", "\r", "\x0b", "\x0c")
    out = []
    for word in sorted(set(values._FALSE) | set(values._TRUE)):
        if not word:
            continue
        expected = "false" if word in values._FALSE else "true"
        for pad in pads:
            out.extend([(word + pad, expected), (pad + word, expected)])
        out.append((" " + word.upper(), expected))
    return out


PADDED = _padded_spellings()
PADDED_FALSE = [(t, e) for t, e in PADDED if e == "false"]
PADDED_TRUE = [(t, e) for t, e in PADDED if e == "true"]
ASCII_WHITESPACE = (" ", "\t", "\n", "\r", "\x0b", "\x0c")


def flag_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE flag (id INTEGER PRIMARY KEY, value BOOLEAN)")
    conn.executemany("INSERT INTO flag VALUES (?, ?)", rows)
    conn.commit()
    return conn


def flag_dialect(conn):
    dialect = SQLite("md5")
    dialect.prepare(conn)
    columns, pk = dialect.resolve(conn, "flag", None, None)
    return dialect, columns, pk


def pushdown_and_fallback(conn):
    dialect, columns, pk = flag_dialect(conn)
    return (dialect.checksum(conn, "flag", pk, columns, None, None),
            dialect.checksum_client(conn, "flag", pk, columns, None, None))


def sql_encoded(conn):
    dialect, columns, _pk = flag_dialect(conn)
    return conn.execute("SELECT " + dialect.column_expr(columns[1]) +
                        " FROM flag").fetchone()[0]


@pytest.mark.parametrize("text,expected", PADDED)
def test_fallback_normalizes_padded_spellings(text, expected):
    assert values.normalize(text, values.BOOL) == expected


@pytest.mark.parametrize("text,expected", PADDED)
def test_sqlite_pushdown_matches_fallback_on_padded_spellings(tmp_path, text,
                                                              expected):
    conn = flag_db(str(tmp_path / "one.db"), [(1, text)])
    try:
        (pushed_count, pushed), (client_count, client) = \
            pushdown_and_fallback(conn)
        assert pushed_count == client_count == 1
        assert sql_encoded(conn) == expected
        assert pushed == client, "diverged on %r" % (text,)
    finally:
        conn.close()


@pytest.mark.parametrize("text", ["false", "true", "n", "yes", "0", "1", ""])
def test_sqlite_pushdown_still_agrees_on_unpadded_values(tmp_path, text):
    conn = flag_db(str(tmp_path / "one.db"), [(1, text)])
    try:
        (_pushed_count, pushed), (_client_count, client) = \
            pushdown_and_fallback(conn)
        assert pushed == client, "diverged on %r" % (text,)
    finally:
        conn.close()


def test_sqlite_pushdown_bool_expr_trims():
    expr = SQLite().column_expr(BOOL_COL)
    assert 'LOWER(TRIM(CAST("flag" AS TEXT)' in expr
    for code in ("char(9)", "char(10)", "char(11)", "char(12)", "char(13)", "' '"):
        assert code in expr, "missing %s" % code


def test_mysql_pushdown_bool_expr_trims():
    expr = MySQL().column_expr(BOOL_COL)
    assert "LOWER(REGEXP_REPLACE(CAST(`flag` AS CHAR)" in expr
    assert "[[:space:]]" in expr


def test_postgres_pushdown_bool_expr_unchanged_native():
    """Empirically, Postgres native boolean cast does not accept padded
    boolean-ish strings, so the classified BOOLEAN path cannot diverge on
    padding; keep native evaluation."""
    expr = Postgres().column_expr(BOOL_COL)
    assert "WHEN" in expr and '"flag"' in expr
    assert "TRIM(" not in expr


def test_duckdb_pushdown_bool_expr_unchanged_native():
    """Empirically, DuckDB native BOOLEAN cast rejects padded boolean-ish
    strings, so classified BOOLEAN columns cannot carry them; keep native
    evaluation."""
    expr = DuckDB().column_expr(BOOL_COL)
    assert "WHEN" in expr and '"flag"' in expr
    assert "TRIM(" not in expr


@pytest.mark.parametrize("text,expected", [("  N ", "false"), (" yes\t", "true")])
def test_sqlite_pushdown_agrees_on_mixed_surrounding_whitespace(tmp_path, text,
                                                                 expected):
    conn = flag_db(str(tmp_path / "one.db"), [(1, text)])
    try:
        (_pushed_count, pushed), (_client_count, client) = \
            pushdown_and_fallback(conn)
        assert sql_encoded(conn) == expected
        assert pushed == client
    finally:
        conn.close()


def test_cli_no_drift_between_padded_and_plain_bools(tmp_path, capsys):
    for name, rows in (
            ("left", [(1, " N"), (2, "n "), (3, "F "), (4, " yes ")]),
            ("right", [(1, "false"), (2, "false"), (3, "false"), (4, "true")])):
        flag_db(str(tmp_path / ("%s.db" % name)), rows).close()
    code = main(["diff", "--left", "sqlite://%s" % (tmp_path / "left.db"),
                 "--right", "sqlite://%s" % (tmp_path / "right.db"),
                 "--table", "flag", "--chunk-size", "25",
                 "--state", str(tmp_path / "s.sqlite3"), "--no-resume",
                 "--quiet"])
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "status   ok" in out


def test_cli_real_boolean_drift_still_reported(tmp_path, capsys):
    """Padding must not hide a real difference: 'N' (false) vs 'true' drifts."""
    for name, rows in (("left", [(1, " N")]), ("right", [(1, "true")])):
        flag_db(str(tmp_path / ("%s.db" % name)), rows).close()
    code = main(["diff", "--left", "sqlite://%s" % (tmp_path / "left.db"),
                 "--right", "sqlite://%s" % (tmp_path / "right.db"),
                 "--table", "flag", "--chunk-size", "25",
                 "--state", str(tmp_path / "s.sqlite3"), "--no-resume"])
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT, out
    assert "id=1" in out
