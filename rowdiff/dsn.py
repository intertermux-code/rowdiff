"""Data source URIs.

    sqlite:///var/data/a.db        file backed database
    sqlite://relative.db           file backed database, relative path
    sqlite://                      in-memory database (tests, experiments)
    duckdb:///warehouse/analytics.duckdb
    postgres://user:pass@host:5432/dbname?sslmode=require
    mysql://user:pass@host:3306/dbname

Passwords never leave the process in clear text: ``DSN.redacted`` is what the
CLI, logs and JSON reports print.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

from .errors import ConfigError

FILE_SCHEMES = ("sqlite", "duckdb")
DRIVER_SCHEMES = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
}


class DSN:
    def __init__(self, uri):
        self.uri = uri
        if ":" not in uri or uri.startswith("/"):
            raise ConfigError("data source must be a URI, e.g. sqlite:///path/to.db")
        parsed = urlparse(uri if "://" in uri else uri.replace(":", "://", 1))
        scheme = parsed.scheme.lower()
        if scheme not in FILE_SCHEMES and scheme not in DRIVER_SCHEMES:
            raise ConfigError("unknown data source scheme %r" % scheme)
        self.scheme = scheme
        self.family = DRIVER_SCHEMES.get(scheme, scheme)
        self.params = dict(parse_qsl(parsed.query))
        if self.family in FILE_SCHEMES:
            path = parsed.netloc + parsed.path
            if not path:
                path = ":memory:"
            self.path = path
            self.host = self.port = self.user = self.password = self.database = None
        else:
            self.path = None
            self.host = parsed.hostname or "localhost"
            self.port = parsed.port
            self.user = parsed.username
            self.password = parsed.password
            self.database = parsed.path.lstrip("/") or None
            if not self.database:
                raise ConfigError("%s URI needs a database name" % scheme)

    @property
    def redacted(self):
        if self.family in FILE_SCHEMES:
            return "%s://%s" % (self.scheme, self.path)
        host = self.host
        if self.port:
            host = "%s:%s" % (host, self.port)
        user = self.user if self.password is None else "%s:***" % self.user
        return "%s://%s@%s/%s" % (self.scheme, user, host, self.database)

    def __repr__(self):
        return "DSN(%r)" % self.redacted


def parse_dsn(uri):
    return DSN(uri)
