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


# -- quoting edge cases ----------------------------------------------------
#
# Every statement is assembled out of user supplied names, so one unquoted
# identifier is either a syntax error or, worse, a silently different table.
# The names below need quoting for three different reasons: they contain a
# space, they are mixed case, or they are reserved words.

TRICKY_TABLE = "Analytics.Order Events"
TRICKY_PK = [Column("Order Id", "INTEGER", is_pk=True)]
TRICKY_COLUMNS = [
    Column("Order Id", "INTEGER", is_pk=True),
    Column("Customer Name", "TEXT"),
    Column("select", "TEXT"),
    Column("Group", "BOOLEAN"),
    Column("2024 total", "NUMERIC"),
    Column("raw data", "BLOB"),
]
TRICKY_NAMES = [c.name for c in TRICKY_COLUMNS] + ["Analytics", "Order Events"]
# A boundaries query only mentions the key, the table, and the predicate.
BOUNDARY_NAMES = ["Order Id", "Group", "Analytics", "Order Events"]

# SQLite quotes like Postgres but expands ranges into column comparisons; its
# own statements are pinned above and against live databases elsewhere.
DIALECTS = [
    (Postgres, '"Analytics"."Order Events"', "%s"),
    (MySQL, "`Analytics`.`Order Events`", "%s"),
    (DuckDB, '"Analytics"."Order Events"', "?"),
]


def assert_quoted_only(statement, dialect, names):
    """Each of ``names`` may appear only inside its own quoting."""
    for name in names:
        token = dialect.quote(name)
        assert token in statement, name
        assert name not in statement.replace(token, ""), name


@pytest.mark.parametrize("dialect_cls,quoted_table,placeholder", DIALECTS)
def test_chunk_checksum_quotes_every_tricky_name(dialect_cls, quoted_table,
                                                 placeholder):
    dialect = dialect_cls()
    statement, params = dialect.checksum_sql(TRICKY_TABLE, TRICKY_PK,
                                             TRICKY_COLUMNS, (10,), (20,))
    key = dialect.quote("Order Id")
    assert ("FROM %s WHERE %s >= %s AND %s < %s"
            % (quoted_table, key, placeholder, key, placeholder)) in statement
    assert_quoted_only(statement, dialect, TRICKY_NAMES)
    assert params == [10, 20]


@pytest.mark.parametrize("dialect_cls,quoted_table,_placeholder", DIALECTS)
def test_boundaries_quote_the_key_and_plan_inside_the_predicate(dialect_cls,
                                                                quoted_table,
                                                                _placeholder):
    dialect = dialect_cls()
    predicate = "%s = 1" % dialect.quote("Group")
    statement = dialect.boundaries_sql(TRICKY_TABLE, TRICKY_PK, 100,
                                       where=predicate)
    planned = "SELECT DISTINCT %s FROM %s WHERE %s" % (
        dialect.quote("Order Id"), quoted_table, predicate)
    assert planned in statement
    assert dialect.mod(dialect.quote("rowdiff_rn"), 100) + " = 1" in statement
    assert_quoted_only(statement, dialect, BOUNDARY_NAMES)


@pytest.mark.parametrize("dialect_cls,quoted_table,placeholder", DIALECTS)
def test_drill_down_select_is_fully_quoted(dialect_cls, quoted_table,
                                           placeholder):
    dialect = dialect_cls()
    statement, params = dialect.select_sql(TRICKY_TABLE, TRICKY_PK,
                                           TRICKY_COLUMNS, (10,), None)
    assert statement == "SELECT %s FROM %s WHERE %s >= %s ORDER BY %s ASC" % (
        ", ".join(dialect.quote(c.name) for c in TRICKY_COLUMNS),
        quoted_table, dialect.quote("Order Id"), placeholder,
        dialect.quote("Order Id"))
    assert params == [10]
    assert_quoted_only(statement, dialect, TRICKY_NAMES)


@pytest.mark.parametrize("dialect_cls,_quoted_table,placeholder", DIALECTS)
def test_a_predicate_is_wrapped_so_it_cannot_rebind_the_range(dialect_cls,
                                                              _quoted_table,
                                                              placeholder):
    """Unparenthesised, ``... AND x = 1 OR y`` would widen the chunk."""
    dialect = dialect_cls()
    predicate = "tenant = 42 OR %s IS NULL" % dialect.quote("Customer Name")
    statement, params = dialect.checksum_sql(TRICKY_TABLE, TRICKY_PK,
                                             TRICKY_COLUMNS, (10,), None,
                                             where=predicate)
    assert ("WHERE %s >= %s AND (%s)"
            % (dialect.quote("Order Id"), placeholder, predicate)) in statement
    assert params == [10]


@pytest.mark.parametrize("dialect_cls,table,quoted", [
    (Postgres, "sales.select", '"sales"."select"'),
    (MySQL, "sales.select", "`sales`.`select`"),
    (DuckDB, "sales.select", '"sales"."select"'),
    (Postgres, "shop.Analytics.Order Events",
     '"shop"."Analytics"."Order Events"'),
    (MySQL, "shop.Analytics.Order Events",
     "`shop`.`Analytics`.`Order Events`"),
    (DuckDB, "shop.Analytics.Order Events",
     '"shop"."Analytics"."Order Events"'),
])
def test_schema_qualified_tables_are_quoted_part_by_part(dialect_cls, table,
                                                         quoted):
    dialect = dialect_cls()
    assert dialect.quote_table(table) == quoted
    statement, _params = dialect.checksum_sql(table, TRICKY_PK, TRICKY_COLUMNS,
                                              None, None)
    assert " FROM %s WHERE 1 = 1" % quoted in statement
    assert "FROM %s)" % quoted in dialect.boundaries_sql(table, TRICKY_PK, 50)


def test_mixed_case_and_reserved_names_are_wrapped_by_each_dialect():
    for dialect in (Postgres(), SQLite(), DuckDB()):
        assert dialect.quote("Group") == '"Group"'
        assert dialect.quote("2024 total") == '"2024 total"'
        assert (dialect.quote_table("Analytics.Order Events")
                == '"Analytics"."Order Events"')
    assert MySQL().quote("Group") == "`Group`"
    assert MySQL().quote("2024 total") == "`2024 total`"
    assert (MySQL().quote_table("Analytics.Order Events")
            == "`Analytics`.`Order Events`")


def test_mysql_never_emits_a_double_quoted_identifier():
    """MySQL reads "x" as a string literal, so it could not be a name."""
    statement, _params = MySQL().checksum_sql(TRICKY_TABLE, TRICKY_PK,
                                              TRICKY_COLUMNS, (10,), (20,))
    assert '"' not in statement


def test_duckdb_pairs_double_quoted_names_with_question_marks():
    statement, params = DuckDB().checksum_sql(TRICKY_TABLE, TRICKY_PK,
                                              TRICKY_COLUMNS, (10,), (20,))
    assert 'WHERE "Order Id" >= ? AND "Order Id" < ?' in statement
    assert "%s" not in statement
    assert params == [10, 20]
