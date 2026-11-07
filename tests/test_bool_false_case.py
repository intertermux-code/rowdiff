"""The SQLite pushdown must fold letter case before matching the false list.

The fallback lowercases a boolean before it looks it up in values._FALSE, so a
chunk has to digest the same way whichever path computed it. The pushdown CASE
matched the list against the raw text, so 'N' digested as true in SQL and false
in Python: two tables holding the same booleans in different letter cases were
reported as drift.

MySQL already wraps its list in LOWER() and Postgres and DuckDB evaluate the
column natively, so only the SQLite expression needed the fold.
"""

import sqlite3

import pytest

from rowdiff import values
from rowdiff.cli import EXIT_OK, main
from rowdiff.dialects.base import Column
from rowdiff.dialects.mysql import MySQL
from rowdiff.dialects.sqlite import SQLite

BOOL_COL = Column("flag", "BOOLEAN")


def _case_variants(word):
    """Letter-case spellings of ``word`` that differ from the lowercase one."""
    if not word.isalpha():
        return []
    mixed = "".join(c.upper() if i % 2 else c.lower()
                    for i, c in enumerate(word))
    return sorted({word.upper(), word.title(), mixed} - {word})


UPPERCASE_FALSE = [text for word in values._FALSE if word.isalpha()
                   for text in _case_variants(word)]

TRUE_SPELLINGS = sorted({text for word in values._TRUE
                         for text in (word, word.upper(), word.title())}
                        - set(values._FALSE))


def flag_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE flag (id INTEGER PRIMARY KEY, value BOOLEAN)")
    conn.executemany("INSERT INTO flag VALUES (?, ?)", rows)
    conn.commit()
    return conn


def flag_dialect(conn):
    """Dialect, columns and key for the single ``flag`` table."""
    dialect = SQLite("md5")
    dialect.prepare(conn)
    columns, pk = dialect.resolve(conn, "flag", None, None)
    return dialect, columns, pk


def pushdown_and_fallback(conn):
    """The two digests of one table, one per execution path."""
    dialect, columns, pk = flag_dialect(conn)
    return (dialect.checksum(conn, "flag", pk, columns, None, None),
            dialect.checksum_client(conn, "flag", pk, columns, None, None))


def sql_encoded(conn):
    """What the pushdown expression yields for the row stored in ``conn``."""
    dialect, columns, _pk = flag_dialect(conn)
    return conn.execute("SELECT " + dialect.column_expr(columns[1]) +
                        " FROM flag").fetchone()[0]


@pytest.mark.parametrize("text", UPPERCASE_FALSE)
def test_fallback_reads_uppercase_false_spellings_as_false(text):
    """The fallback is the reference: all of these mean false."""
    assert values.normalize(text, values.BOOL) == "false"


@pytest.mark.parametrize("text", UPPERCASE_FALSE)
def test_sqlite_pushdown_matches_fallback_on_uppercase_false(tmp_path, text):
    conn = flag_db(str(tmp_path / "one.db"), [(1, text)])
    try:
        (pushed_count, pushed), (client_count, client) = \
            pushdown_and_fallback(conn)
        assert pushed_count == client_count == 1
        assert pushed == client, "diverged on %r" % (text,)
    finally:
        conn.close()


@pytest.mark.parametrize("text", ["false", "f", "no", "n", "0", ""])
def test_sqlite_pushdown_still_reads_lowercase_false_spellings(text, tmp_path):
    conn = flag_db(str(tmp_path / "one.db"), [(1, text)])
    try:
        assert sql_encoded(conn) == "false"
    finally:
        conn.close()


@pytest.mark.parametrize("text", TRUE_SPELLINGS)
def test_sqlite_pushdown_still_reads_true_spellings_as_true(text, tmp_path):
    """The fold must not move anything into the false list."""
    conn = flag_db(str(tmp_path / "one.db"), [(1, text)])
    try:
        (_pushed_count, pushed), (_client_count, client) = \
            pushdown_and_fallback(conn)
        assert sql_encoded(conn) == "true"
        assert values.normalize(text, values.BOOL) == "true"
        assert pushed == client, "diverged on %r" % (text,)
    finally:
        conn.close()


def test_sqlite_pushdown_bool_expr_folds_case():
    expr = SQLite().column_expr(BOOL_COL)
    assert 'LOWER(TRIM(CAST("flag" AS TEXT)' in expr
    assert "char(9)" in expr


def test_mysql_pushdown_bool_expr_still_folds_case():
    """Still case-insensitive and now trims, matching the fallback."""
    expr = MySQL().column_expr(BOOL_COL)
    assert "LOWER(REGEXP_REPLACE(CAST(`flag` AS CHAR)" in expr


def test_sqlite_pushdown_and_fallback_agree_on_a_mixed_case_column(tmp_path):
    conn = flag_db(str(tmp_path / "mixed.db"),
                   [(1, "N"), (2, "False"), (3, "NO"), (4, "F"),
                    (5, "false"), (6, "yes"), (7, "True")])
    try:
        (pushed_count, pushed), (client_count, client) = \
            pushdown_and_fallback(conn)
        assert pushed_count == client_count == 7
        assert pushed == client
    finally:
        conn.close()


def test_cli_no_drift_between_upper_and_lowercase_bools(tmp_path, capsys):
    """'N' vs 'false' is the same boolean, so a diff of the two is clean."""
    for name, rows in (("left", [(1, "N"), (2, "False"), (3, "NO"), (4, "F")]),
                       ("right", [(1, "false"), (2, "false"), (3, "false"),
                                  (4, "false")])):
        flag_db(str(tmp_path / ("%s.db" % name)), rows).close()
    code = main(["diff", "--left", "sqlite://%s" % (tmp_path / "left.db"),
                 "--right", "sqlite://%s" % (tmp_path / "right.db"),
                 "--table", "flag", "--chunk-size", "25",
                 "--state", str(tmp_path / "s.sqlite3"), "--no-resume",
                 "--quiet"])
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "status   ok" in out
