"""Resume state.

A local SQLite file keeps one row per chunk per run, keyed by a fingerprint of
the request. Re-running the same command reuses finished chunks, so an
interrupted diff picks up where it stopped and reports exactly what it would
have reported had it never been interrupted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_key     TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    started_at  TEXT NOT NULL,
    options     TEXT NOT NULL,
    ranges      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    run_key     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    status      TEXT NOT NULL,
    left_count  INTEGER,
    right_count INTEGER,
    left_hash   TEXT,
    right_hash  TEXT,
    added       INTEGER,
    removed     INTEGER,
    changed     INTEGER,
    detail      TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_key, idx)
);
"""


class State:
    def __init__(self, path, fingerprint, options, ranges, resume=True):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        existing = None
        if resume:
            row = self.conn.execute(
                "SELECT run_key, ranges FROM runs WHERE fingerprint = ?",
                (fingerprint,)).fetchone()
            if row:
                existing = row
        if existing:
            self.run_key, ranges_json = existing
            self.ranges = [tuple(_decode_range(r))
                           for r in json.loads(ranges_json)]
            self.resumed = True
        else:
            suffix = "" if resume else _now()
            self.run_key = hashlib.sha256(
                (fingerprint + suffix).encode()).hexdigest()[:16]
            self.ranges = ranges
            self.resumed = False
            # A resumable run owns the plain fingerprint; --no-resume runs get a
            # unique one so they never mask the run a later invocation resumes.
            stored = fingerprint if resume else "%s#%s" % (fingerprint, self.run_key)
            self.conn.execute(
                "INSERT INTO runs (run_key, fingerprint, started_at, options,"
                " ranges) VALUES (?, ?, ?, ?, ?)",
                (self.run_key, stored, _now(),
                 json.dumps(options, sort_keys=True, default=str),
                 json.dumps([_encode_range(r) for r in ranges],
                            sort_keys=True, default=str)))
            self.conn.commit()

    @property
    def total_chunks(self):
        return len(self.ranges)

    def set_ranges(self, ranges):
        """Record the plan, so the next run can tell whether it is still valid."""
        if self.ranges == ranges:
            return
        self.ranges = ranges
        self.conn.execute(
            "UPDATE runs SET ranges = ? WHERE run_key = ?",
            (json.dumps([_encode_range(r) for r in ranges],
                        sort_keys=True, default=str), self.run_key))
        self.conn.commit()

    def forget_chunks(self):
        self.conn.execute("DELETE FROM chunks WHERE run_key = ?",
                          (self.run_key,))
        self.conn.commit()

    def cached(self, index):
        row = self.conn.execute(
            "SELECT status, left_count, right_count, left_hash, right_hash,"
            " added, removed, changed, detail FROM chunks"
            " WHERE run_key = ? AND idx = ?",
            (self.run_key, index)).fetchone()
        if not row:
            return None
        return {
            "status": row[0], "left_count": row[1], "right_count": row[2],
            "left_hash": row[3], "right_hash": row[4], "added": row[5],
            "removed": row[6], "changed": row[7],
            "detail": json.loads(row[8]) if row[8] else None,
        }

    def save(self, index, result):
        self.conn.execute(
            "INSERT OR REPLACE INTO chunks (run_key, idx, status, left_count,"
            " right_count, left_hash, right_hash, added, removed, changed,"
            " detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_key, index, result["status"], result["left"]["row_count"],
             result["right"]["row_count"], result["left"]["checksum"],
             result["right"]["checksum"], result["rows"]["added"],
             result["rows"]["removed"], result["rows"]["changed"],
             json.dumps(result.get("row_diff"), sort_keys=True, default=str),
             _now()))
        self.conn.commit()

    def drop(self, index):
        self.conn.execute("DELETE FROM chunks WHERE run_key = ? AND idx = ?",
                          (self.run_key, index))
        self.conn.commit()

    def close(self):
        self.conn.close()


def _encode_range(rng):
    from .chunking import encode_key
    return {"from": encode_key(rng[0]), "to": encode_key(rng[1]) if rng[1] is not None else None}


def _decode_range(encoded):
    from .chunking import decode_key
    lo = decode_key(encoded["from"])
    hi = decode_key(encoded["to"]) if encoded["to"] is not None else None
    return lo, hi


def fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True,
                                      default=str).encode()).hexdigest()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
