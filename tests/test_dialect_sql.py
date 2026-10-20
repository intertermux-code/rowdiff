"""SQL text produced for dialects whose drivers are not installed here.

These tests pin the generated statements: the SQLite path is proven against
real databases elsewhere, so the remaining adapters are covered at the string
level instead of silently shipping unverified SQL.
"""

import pytest

from rowdiff.dialects import dialect_for
from rowdiff.dialects.base import Column
from rowdiff.dsn import parse_dsn
from rowdiff.errors import ConfigError
from rowdiff.dialects.duckdb import DuckDB
from rowdiff.dialects.mysql import MySQL
from rowdiff.dialects.postgres import Postgres
from rowdiff.dialects.sqlite import SQLite

PK = [Column("id", "INTEGER", is_pk=True)]
COMPOSITE = [Column("device", "INTEGER", is_pk=True),
             Column("taken_at", "BIGINT", is_pk=True)]
COLUMNS = [Column("id", "INTEGER", is_pk=True),
           Column("name", "TEXT"),
           Column("score", "REAL"),
           Column("note", "TEXT"),
           Column("active", "BOOLEAN"),
           Column("fingerprint", "BLOB")]


def sql(dialect, lo=(10,), hi=(20,)):
    return dialect.checksum_sql("users", PK, COLUMNS, lo, hi)


# -- SQLite ----------------------------------------------------------------


def test_sqlite_folds_rows_through_a_registered_aggregate():
    statement, params = sql(SQLite(), lo=None, hi=None)
    assert statement.startswith("SELECT COUNT(*), rowdiff_fold(")
    assert "FROM \"users\" WHERE 1 = 1" in statement
    assert "COALESCE(CAST(\"note\" AS TEXT), '\\N')" in statement
    assert "|| char(31) ||" in statement
    assert "CASE WHEN \"active\" IS NULL" in statement
    assert "'x' || HEX(\"fingerprint\")" in statement
    assert params == []


def test_sqlite_composite_range_is_expanded_into_column_comparisons():
    statement, params = SQLite().range_sql(COMPOSITE, (1, 50), (2, 10))
    assert statement == ('(("device" > ?) OR ("device" = ? AND "taken_at" >= ?))'
                         ' AND (("device" < ?) OR ("device" = ? AND "taken_at" < ?))')
    assert params == [1, 1, 50, 2, 2, 10]


def test_sqlite_single_sided_ranges():
    _statement, params = SQLite().range_sql(PK, None, (20,))
    assert params == [20]
    statement, _params = SQLite().range_sql(PK, None, None)
    assert statement == "1 = 1"


def test_sqlite_boundaries_take_every_nth_key():
    statement = SQLite().boundaries_sql("users", PK, 5000)
    assert "SELECT DISTINCT \"id\" FROM \"users\"" in statement
    assert "row_number() OVER (ORDER BY \"id\")" in statement
    assert "% 5000 = 1" in statement


# -- Postgres --------------------------------------------------------------


def test_postgres_pushes_the_digest_into_the_database():
    statement, params = sql(Postgres(), lo=(10,), hi=(20,))
    assert "STRING_AGG(MD5(" in statement
    assert statement.count("ORDER BY MD5(") == 1
    assert statement.startswith("SELECT COUNT(*), MD5(STRING_AGG(")
    assert "CONCAT_WS(chr(31)" in statement
    assert "COALESCE(CAST(\"score\" AS TEXT), '\\N')" in statement
    assert "UPPER(ENCODE(\"fingerprint\", 'hex'))" in statement
    assert params == [10, 20]


def test_postgres_uses_row_value_comparisons_and_percent_placeholders():
    statement, params = Postgres().range_sql(COMPOSITE, (1, 50), None)
    assert statement == '("device", "taken_at") >= (%s, %s)'
    assert params == [1, 50]


def test_postgres_sha256_and_modulo():
    sha = Postgres(algorithm="sha256")
    statement, _params = sql(sha, lo=None, hi=None)
    assert "ENCODE(SHA256(CONVERT_TO(" in statement
    assert "MOD(" in sha.boundaries_sql("users", PK, 100)


def test_postgres_boolean_columns():
    statement, _params = sql(Postgres(), lo=None, hi=None)
    assert 'CASE WHEN "active" IS NULL THEN \'\\N\' WHEN "active" THEN \'true\'' in statement


# -- MySQL -----------------------------------------------------------------


def test_mysql_uses_group_concat_and_backticks():
    dialect = MySQL()
    statement, params = dialect.checksum_sql("orders", [Column("id", "INT")],
                                             [Column("id", "INT"),
                                              Column("note", "VARCHAR")],
                                             (1,), (9,))
    assert statement.startswith("SELECT COUNT(*), MD5(GROUP_CONCAT(MD5(")
    assert "SEPARATOR ''" in statement
    assert "FROM `orders` WHERE `id` >= %s AND `id` < %s" in statement
    assert "CONVERT(0x1F USING UTF8MB4)" in statement
    assert "'\\\\N'" in statement
    assert params == [1, 9]


def test_mysql_tinyint_one_is_a_boolean_not_a_number():
    dialect = MySQL()
    column = Column("shipped", "tinyint", "tinyint(1)")
    assert column.category == "bool"
    assert "LOWER(CAST(`shipped` AS CHAR))" in dialect.column_expr(column)
    assert Column("count", "tinyint", "tinyint(4)").category == "number"


def test_mysql_sha256_and_composite_row_values():
    statement, _params = MySQL(algorithm="sha256").checksum_sql(
        "t", COMPOSITE, COLUMNS, None, None)
    assert "SHA2(" in statement
    assert "SHA256(" not in statement
    text, params = MySQL().range_sql(COMPOSITE, (1, 50), (1, 90))
    assert text == '(`device`, `taken_at`) >= (%s, %s) AND (`device`,' \
                   ' `taken_at`) < (%s, %s)'
    assert params == [1, 50, 1, 90]


# -- DuckDB ----------------------------------------------------------------


def test_duckdb_string_agg_with_question_placeholders():
    statement, params = DuckDB().checksum_sql("metrics", COMPOSITE, COLUMNS,
                                              (1, 10), (2, 20))
    assert statement.startswith("SELECT COUNT(*), MD5(STRING_AGG(MD5(")
    assert "FROM \"metrics\" WHERE (\"device\", \"taken_at\") >= (?, ?)" in statement
    assert params == [1, 10, 2, 20]
    assert "chr(31)" in statement


def test_duckdb_casts_to_varchar_and_hashes_sha256_natively():
    dialect = DuckDB(algorithm="sha256")
    statement, _params = dialect.checksum_sql("m", PK, COLUMNS, None, None)
    assert "SHA256(" in statement
    assert "COALESCE(CAST(\"note\" AS VARCHAR), '\\N')" in statement


# -- registry and quoting --------------------------------------------------


@pytest.mark.parametrize("uri,expected", [
    ("sqlite:///a.db", SQLite),
    ("duckdb:///a.duckdb", DuckDB),
    ("postgres://u@h/db", Postgres),
    ("postgresql://u@h/db", Postgres),
    ("mysql://u@h/db", MySQL),
])
def test_registry_maps_uris_to_adapters(uri, expected):
    assert isinstance(dialect_for(parse_dsn(uri)), expected)


def test_identifiers_are_quoted_and_ambiguous_ones_are_refused():
    dialect = SQLite()
    assert dialect.quote_table("main.users") == '"main"."users"'
    assert dialect.quote_table("order items") == '"order items"'
    with pytest.raises(ConfigError):
        dialect.quote('ev"il')
    with pytest.raises(ConfigError):
        dialect.quote_table("users.")
    with pytest.raises(ConfigError):
        MySQL().quote("ev`il")


def test_digest_algorithms_are_validated():
    with pytest.raises(Exception):
        SQLite(algorithm="sha1")
