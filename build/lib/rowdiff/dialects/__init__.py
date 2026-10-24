"""Dialect registry.

``sqlite`` needs no driver, the rest are installed through extras:
``rowdiff[postgres]``, ``rowdiff[mysql]``, ``rowdiff[duckdb]``.
"""

from __future__ import annotations

from ..errors import ConfigError
from .duckdb import DuckDB
from .mysql import MySQL
from .postgres import Postgres
from .sqlite import SQLite

_CLASSES = {
    "sqlite": SQLite,
    "postgres": Postgres,
    "mysql": MySQL,
    "duckdb": DuckDB,
}

#: Which dialects rowdiff is verified against end to end in its own test suite.
TESTED = ("sqlite",)
ADAPTER_ONLY = ("duckdb", "postgres", "mysql")


def dialect_for(dsn, algorithm="md5"):
    cls = _CLASSES.get(dsn.family)
    if cls is None:
        raise ConfigError("no adapter for scheme %r (have: %s)"
                          % (dsn.scheme, ", ".join(sorted(_CLASSES))))
    return cls(algorithm=algorithm)


def names():
    return sorted(_CLASSES)
