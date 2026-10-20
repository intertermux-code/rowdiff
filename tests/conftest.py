import sqlite3

import pytest

USERS_DDL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    score REAL,
    note TEXT,
    active BOOLEAN,
    fingerprint BLOB
)
"""

READINGS_DDL = """
CREATE TABLE readings (
    device_id INTEGER NOT NULL,
    taken_at INTEGER NOT NULL,
    value REAL,
    label TEXT,
    PRIMARY KEY (device_id, taken_at)
)
"""


def users(n=120):
    return [(i, "user-%03d" % i, i * 1.5,
             None if i % 7 == 0 else "note %d" % i,
             1 if i % 3 else 0,
             ("fp-%d" % i).encode("utf-8"))
            for i in range(1, n + 1)]


def readings():
    return [(device, tick * 10, tick + device, "label-%d-%d" % (device, tick))
            for device in (1, 2) for tick in range(1, 21)]


def build(path, table, ddl, rows):
    conn = sqlite3.connect(path)
    try:
        conn.execute(ddl)
        conn.executemany("INSERT INTO %s VALUES (%s)"
                         % (table, ", ".join(["?"] * len(rows[0]))), rows)
        conn.commit()
    finally:
        conn.close()
    return path


def _pair(tmp_path, ddl, table, rows, mutate_left, mutate_right):
    paths = []
    for name, mutation in (("left", mutate_left), ("right", mutate_right)):
        staged = list(rows)
        if mutation:
            staged = mutation(staged)
        path = str(tmp_path / ("%s.db" % name))
        build(path, table, ddl, staged)
        paths.append(path)
    return paths


@pytest.fixture
def sqlite_pair(tmp_path):
    """Two SQLite files holding the same 120 user rows."""
    def make(mutate_left=None, mutate_right=None, rows=None):
        return _pair(tmp_path, USERS_DDL, "users",
                     users() if rows is None else rows,
                     mutate_left, mutate_right)

    return make


@pytest.fixture
def sqlite_composite(tmp_path):
    """Two SQLite files holding the same 40 readings on a two column key."""
    def make(mutate_left=None, mutate_right=None):
        return _pair(tmp_path, READINGS_DDL, "readings", readings(),
                     mutate_left, mutate_right)

    return make
