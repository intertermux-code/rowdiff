import hashlib
import sqlite3

import pytest

from rowdiff import values
from rowdiff.chunking import (decode_key, decode_value, encode_key,
                              encode_value, merge_boundaries, plan_ranges)
from rowdiff.dialects.base import Column
from rowdiff.dialects.sqlite import SQLite, make_fold_aggregate
from rowdiff.dsn import parse_dsn
from rowdiff.engine import Options
from rowdiff.errors import ConfigError


# -- data source URIs ------------------------------------------------------


def test_file_uris():
    absolute = parse_dsn("sqlite:///var/data/a.db")
    assert absolute.path == "/var/data/a.db"
    assert absolute.family == "sqlite"
    relative = parse_dsn("duckdb://warehouse.duckdb")
    assert relative.path == "warehouse.duckdb"
    assert parse_dsn("sqlite://").path == ":memory:"


def test_server_uris_keep_their_parts():
    dsn = parse_dsn("postgres://analyst:s3cret@warehouse:5432/marts?sslmode=require")
    assert (dsn.host, dsn.port, dsn.user, dsn.database) == \
        ("warehouse", 5432, "analyst", "marts")
    assert dsn.params == {"sslmode": "require"}
    assert dsn.redacted == "postgres://analyst:***@warehouse:5432/marts"


def test_bad_uris_are_rejected():
    for uri in ("nodb://x", "/plain/path.db", "postgres://u@h"):
        with pytest.raises(ConfigError):
            parse_dsn(uri)


def test_dsn_repr_never_leaks_the_password():
    dsn = parse_dsn("mysql://root:pw@127.0.0.1/shop")
    assert "pw" not in repr(dsn)


# -- request validation ----------------------------------------------------


def options(chunk_size):
    """An Options object for a run that never reaches a database."""
    return Options(left="sqlite://left.db", left_table="users",
                   right="sqlite://right.db", right_table="users",
                   state_file="state.sqlite3", chunk_size=chunk_size)


@pytest.mark.parametrize("chunk_size", [0, -1, -1000])
def test_a_chunk_size_below_one_is_refused(chunk_size):
    with pytest.raises(ConfigError, match="positive integer"):
        options(chunk_size)


def test_a_chunk_size_above_the_cap_is_refused():
    with pytest.raises(ConfigError, match="at most 1000000"):
        options(1000001)


@pytest.mark.parametrize("chunk_size", [1, 1000000])
def test_the_chunk_size_bounds_are_allowed(chunk_size):
    assert options(chunk_size).chunk_size == chunk_size


# -- type classification and canonical values ------------------------------


@pytest.mark.parametrize("declared,category", [
    ("INTEGER", values.NUMBER),
    ("double precision", values.NUMBER),
    ("VARCHAR(255)", values.TEXT),
    ("BOOLEAN", values.BOOL),
    ("bytea", values.BYTES),
    ("BLOB", values.BYTES),
    ("TIMESTAMP WITH TIME ZONE", values.TEMPORAL),
    ("date", values.TEMPORAL),
    ("jsonb", values.TEXT),
])
def test_declared_types_map_onto_categories(declared, category):
    assert Column("c", declared).category == category


def test_boolean_lookalikes_stay_numbers():
    assert Column("n", "tinyint", "tinyint(4)").category == values.NUMBER
    assert Column("b", "tinyint", "tinyint(1)").category == values.BOOL


@pytest.mark.parametrize("value,category,expected", [
    (None, values.TEXT, "\\N"),
    (True, values.BOOL, "true"),
    (0, values.BOOL, "false"),
    ("f", values.BOOL, "false"),
    (b"\x00\xff", values.BYTES, "x00FF"),
    (7, values.NUMBER, "7"),
    (7.5, values.NUMBER, "7.5"),
    ("text", values.TEXT, "text"),
])
def test_canonical_encodings(value, category, expected):
    assert values.normalize(value, category) == expected


def test_a_row_is_the_separator_join_of_its_encoded_columns():
    row = [1, "a", None]
    kinds = [values.NUMBER, values.TEXT, values.TEXT]
    joined = values.SEP.join(
        values.normalize(v, k) for v, k in zip(row, kinds))
    assert joined == "1\x1fa\x1f\\N"


# -- the SQLite aggregate reproduces in Python -----------------------------


@pytest.mark.parametrize("algorithm", ["md5", "sha256"])
def test_fold_aggregate_matches_the_documented_definition(algorithm):
    aggregate = make_fold_aggregate(algorithm)()
    for row in ("beta", "alpha", "gamma"):
        aggregate.step(row)
    expected_rows = sorted(
        hashlib.new(algorithm, r.encode()).hexdigest() for r in
        ("beta", "alpha", "gamma"))
    assert aggregate.finalize() == hashlib.new(
        algorithm, "".join(expected_rows).encode("ascii")).hexdigest()


def test_empty_aggregate_is_the_digest_of_nothing():
    assert make_fold_aggregate("md5")().finalize() == SQLite().empty_digest()


def test_fold_aggregate_flags_the_dialect_on_interrupt():
    """A KeyboardInterrupt inside a callback flags the dialect and re-raises.

    sqlite3 swallows exceptions raised inside aggregate callbacks into
    OperationalError, so the dialect must remember the interrupt for the
    engine to re-raise it as KeyboardInterrupt (exit 130) rather than record
    a chunk error (exit 2).
    """
    dialect = SQLite("md5")
    conn = sqlite3.connect(":memory:")
    dialect.prepare(conn)
    aggregate = make_fold_aggregate("md5", dialect)()

    real_new = hashlib.new

    def boom(*args, **kwargs):
        raise KeyboardInterrupt()

    hashlib.new = boom
    try:
        with pytest.raises(KeyboardInterrupt):
            aggregate.step("a row")
        aggregate.rows = ["a row"]
        with pytest.raises(KeyboardInterrupt):
            aggregate.finalize()
    finally:
        hashlib.new = real_new

    assert dialect._rowdiff_interrupted is True


def test_checksum_re_raises_a_swallowed_interrupt(tmp_path, monkeypatch):
    """OperationalError from an interrupted fold becomes KeyboardInterrupt.

    Only with the dialect fix does a swallowed interrupt surface as
    KeyboardInterrupt here; without it the engine would record a chunk error
    (exit 2) instead of exiting 130.
    """
    from conftest import USERS_DDL, build, users

    path = build(str(tmp_path / "d.db"), "users", USERS_DDL, users())
    dialect = SQLite("md5")
    conn = sqlite3.connect(path)
    dialect.prepare(conn)
    columns, pk = dialect.resolve(conn, "users", None, None)

    def boom(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(hashlib, "new", boom)
    try:
        with pytest.raises(KeyboardInterrupt):
            dialect.checksum(conn, "users", pk, columns, None, None)
    finally:
        conn.close()

    assert dialect._rowdiff_interrupted is True


def test_checksum_passes_through_genuine_errors(tmp_path):
    """A genuine OperationalError is not mistaken for an interrupt.

    Without the interrupt flag the same path raises OperationalError, so
    the engine keeps recording a chunk error and exiting 2.
    """
    from conftest import USERS_DDL, build, users

    path = build(str(tmp_path / "d.db"), "users", USERS_DDL, users())
    dialect = SQLite("md5")
    conn = sqlite3.connect(path)
    dialect.prepare(conn)
    columns, pk = dialect.resolve(conn, "users", None, None)

    try:
        with pytest.raises(sqlite3.OperationalError):
            dialect.checksum(conn, "users", pk, columns, None, None,
                             where="no_such_column = 1")
    finally:
        conn.close()


def test_udf_and_client_hashing_agree_on_a_chunk(tmp_path):
    from conftest import USERS_DDL, build, users
    path = build(str(tmp_path / "d.db"), "users", USERS_DDL, users())
    dialect = SQLite()
    conn = sqlite3.connect(path)
    dialect.prepare(conn)
    columns, pk = dialect.resolve(conn, "users", None, None)

    pushed_count, pushed_digest = dialect.checksum(
        conn, "users", pk, columns, (26,), (51,))
    pulled_count, pulled_digest = dialect.checksum_client(
        conn, "users", pk, columns, (26,), (51,))
    assert (pushed_count, pulled_count) == (25, 25)
    assert pushed_digest == pulled_digest

    conn.close()


def test_a_driver_without_aggregates_falls_back(tmp_path):
    from conftest import USERS_DDL, build, users
    path = build(str(tmp_path / "d.db"), "users", USERS_DDL, users()[:9])
    conn = NoAggregate(sqlite3.connect(path))
    dialect = SQLite()

    assert dialect.pushdown_available(conn) is False
    count, digest = dialect.checksum_any(conn, "users",
                                        [Column("id", "INTEGER")],
                                        [Column("id", "INTEGER"),
                                         Column("name", "TEXT")],
                                        None, None, pushdown=True)
    assert count == 9
    assert len(digest) == 32
    conn.close()


class NoAggregate:
    """Pretends to be a DBAPI driver that cannot register user defined types."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        if name in ("create_aggregate", "create_function"):
            raise AttributeError(name)
        return getattr(self._conn, name)


# -- chunk planning --------------------------------------------------------


def test_ranges_tile_the_key_space():
    ranges = plan_ranges([(1,), (2,), (3,)])
    assert ranges == [((1,), (2,)), ((2,), (3,)), ((3,), None)]


def test_overlapping_boundaries_are_merged_and_deduped():
    merged = merge_boundaries([(1,), (30,)], [(1,), (15,), (30,)])
    assert merged == [(1,), (15,), (30,)]


def test_incomparable_key_types_stop_the_run():
    with pytest.raises(ConfigError):
        merge_boundaries([(1,)], [("a",)])


def test_key_values_survive_the_state_file_round_trip():
    import datetime
    samples = [((1, "x"), (None, None)), ((7.5,), None)]
    for key, _ in samples:
        assert decode_key(encode_key(key)) == key
    moment = datetime.datetime(2026, 1, 2, 3, 4, 5)
    assert decode_value(encode_value(moment)) == moment
    assert decode_value(encode_value(b"\x01\x02")) == b"\x01\x02"
    assert decode_value(encode_value(True)) is True
