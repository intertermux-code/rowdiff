"""The diff engine: plan chunks, checksum them inside the databases, and drill
down only into the chunks that disagree."""

from __future__ import annotations

import datetime
import decimal
import sqlite3
import time

from .chunking import merge_boundaries, plan_ranges
from .dialects import dialect_for
from .dsn import parse_dsn
from .errors import ConfigError
from .state import State, fingerprint as make_fingerprint

SCHEMA_VERSION = 1
DEFAULT_STATE_FILE = ".rowdiff-state.sqlite3"

MATCHED = "matched"
MISMATCHED = "mismatched"
ERROR = "error"


class Options:
    def __init__(self, left, left_table, right, right_table, pk=None,
                 columns=None, chunk_size=5000, algorithm="md5",
                 state_file=DEFAULT_STATE_FILE, resume=True, pushdown=True,
                 max_diff_rows=50, where=None):
        if chunk_size < 1:
            raise ConfigError("--chunk-size must be a positive integer")
        if chunk_size > 1000000:
            raise ConfigError("--chunk-size must be at most 1000000")
        if max_diff_rows < 0:
            raise ConfigError("--max-diff-rows cannot be negative")
        self.where = where
        self.left = parse_dsn(left)
        self.right = parse_dsn(right)
        self.left_table = left_table
        self.right_table = right_table
        self.pk = list(pk) if pk else None
        self.columns = list(columns) if columns else None
        self.chunk_size = chunk_size
        self.algorithm = algorithm
        self.state_file = state_file
        self.resume = resume
        self.pushdown = pushdown
        self.max_diff_rows = max_diff_rows


class Side:
    """One connection, one table, the compared columns in a fixed order."""

    def __init__(self, dsn, table, algorithm):
        self.dsn = dsn
        self.table = table
        self.dialect = dialect_for(dsn, algorithm)
        self.conn = self.dialect.connect_or_fail(dsn)
        self.dialect.prepare(self.conn)
        self.columns = None
        self.pk = None

    def close(self):
        self.dialect.close(self.conn)

    def checksum(self, lo, hi, pushdown):
        return self.dialect.checksum_any(
            self.conn, self.table, self.pk, self.columns, lo, hi, pushdown,
            where=getattr(self.dialect, "where", None))

    def rows(self, lo, hi):
        return self.dialect.fetch_rows(
            self.conn, self.table, self.pk, self.columns, lo, hi,
            where=getattr(self.dialect, "where", None))


def run(options, progress=None):
    started = time.time()
    sides = []
    try:
        left = Side(options.left, options.left_table, options.algorithm)
        sides.append(left)
        right = Side(options.right, options.right_table, options.algorithm)
        sides.append(right)
        _align_columns(left, right, options)
        left.dialect.where = options.where
        right.dialect.where = options.where
        report = _diff(options, left, right, progress)
    finally:
        for side in sides:
            side.close()
    report["duration_seconds"] = round(time.time() - started, 3)
    return report


def _align_columns(left, right, options):
    """Both sides compare the same names in the same order, or the run stops."""
    left.columns, left.pk = left.dialect.resolve(
        left.conn, left.table, options.pk, options.columns)
    names = [c.name for c in left.columns]
    pk_names = [c.name for c in left.pk]
    right.columns, right.pk = right.dialect.resolve(
        right.conn, right.table, pk_names, names)
    if [c.name for c in right.pk] != pk_names:
        raise ConfigError("primary key columns differ between the two tables")


def _diff(options, left, right, progress):
    state = _open_state(options, left, right)
    try:
        ranges = state.ranges or _plan(options, left, right)
        state.ranges = ranges or [(None, None)]
        chunks = []
        total = len(state.ranges)
        for index, (lo, hi) in enumerate(state.ranges):
            result = _chunk(options, state, index, lo, hi, left, right)
            chunks.append(result)
            if progress:
                progress(index + 1, total, result)
        return _report(options, state, left, right, chunks)
    finally:
        state.close()


def _plan(options, left, right):
    bounds = merge_boundaries(
        left.dialect.boundaries(left.conn, left.table, left.pk,
                                options.chunk_size, where=options.where),
        right.dialect.boundaries(right.conn, right.table, right.pk,
                                 options.chunk_size, where=options.where))
    return plan_ranges(bounds)


def _open_state(options, left, right):
    parts = {
        "left": [left.dialect.name, options.left.redacted, options.left_table],
        "right": [right.dialect.name, options.right.redacted,
                  options.right_table],
        "pk": [c.name for c in left.pk],
        "columns": [c.name for c in left.columns],
        "chunk_size": options.chunk_size,
        "where": options.where,
        "algorithm": options.algorithm,
        "pushdown": options.pushdown,
    }
    try:
        return State(options.state_file, make_fingerprint(parts), parts, [],
                     resume=options.resume)
    except sqlite3.Error as exc:
        raise ConfigError("cannot use the state file %s: %s"
                          % (options.state_file, exc)) from exc


def _chunk(options, state, index, lo, hi, left, right):
    cached = state.cached(index)
    if cached:
        return {
            "index": index,
            "pk_range": _range_json(lo, hi),
            "status": cached["status"],
            "left": {"row_count": cached["left_count"],
                     "checksum": cached["left_hash"]},
            "right": {"row_count": cached["right_count"],
                      "checksum": cached["right_hash"]},
            "rows": {"added": cached["added"], "removed": cached["removed"],
                     "changed": cached["changed"]},
            "row_diff": cached["detail"],
            "cached": True,
        }

    try:
        left_count, left_hash = left.checksum(lo, hi, options.pushdown)
        right_count, right_hash = right.checksum(lo, hi, options.pushdown)
    except Exception as exc:
        # Errors are not persisted: a resumed run retries them.
        return {
            "index": index,
            "pk_range": _range_json(lo, hi),
            "status": ERROR,
            "left": {"row_count": None, "checksum": None},
            "right": {"row_count": None, "checksum": None},
            "rows": {"added": None, "removed": None, "changed": None},
            "row_diff": None,
            "cached": False,
            "error": {"message": _explain(exc)},
        }

    if (left_count, left_hash) == (right_count, right_hash):
        result = {
            "index": index,
            "pk_range": _range_json(lo, hi),
            "status": MATCHED,
            "left": {"row_count": left_count, "checksum": left_hash},
            "right": {"row_count": right_count, "checksum": right_hash},
            "rows": {"added": 0, "removed": 0, "changed": 0},
            "row_diff": None,
            "cached": False,
        }
    else:
        diff = _row_diff(options, left, right, lo, hi)
        result = {
            "index": index,
            "pk_range": _range_json(lo, hi),
            "status": MISMATCHED,
            "left": {"row_count": left_count, "checksum": left_hash},
            "right": {"row_count": right_count, "checksum": right_hash},
            "rows": diff["counts"],
            "row_diff": diff["detail"],
            "cached": False,
        }
    state.save(index, result)
    return result


def _row_diff(options, left, right, lo, hi):
    left_rows = dict(left.rows(lo, hi))
    right_rows = dict(right.rows(lo, hi))
    names = [c.name for c in left.columns]
    added = [k for k in _sorted_keys(right_rows) if k not in left_rows]
    removed = [k for k in _sorted_keys(left_rows) if k not in right_rows]
    changed = []
    for key in _sorted_keys(left_rows):
        other = right_rows.get(key)
        if other is None or other == left_rows[key]:
            continue
        columns = [{"name": n, "left": left_rows[key][n], "right": other[n]}
                   for n in names if left_rows[key][n] != other[n]]
        changed.append({"pk": _key_json(key), "columns": columns})

    limit = options.max_diff_rows
    detail = {
        "added": [_key_json(k) for k in added[:limit]],
        "removed": [_key_json(k) for k in removed[:limit]],
        "changed": changed[:limit],
        "truncated": (len(added) > limit or len(removed) > limit
                      or len(changed) > limit),
    }
    return {"counts": {"added": len(added), "removed": len(removed),
                       "changed": len(changed)},
            "detail": detail}


def _report(options, state, left, right, chunks):
    counts = {MATCHED: 0, MISMATCHED: 0, ERROR: 0}
    cached = added = removed = changed = 0
    left_total = right_total = 0
    for chunk in chunks:
        counts[chunk["status"]] += 1
        if chunk["cached"]:
            cached += 1
        left_total += chunk["left"]["row_count"] or 0
        right_total += chunk["right"]["row_count"] or 0
        added += chunk["rows"]["added"] or 0
        removed += chunk["rows"]["removed"] or 0
        changed += chunk["rows"]["changed"] or 0

    drift = counts[MISMATCHED] > 0
    status = "error" if counts[ERROR] else ("drift" if drift else "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "rowdiff.drift",
        "status": status,
        "drift": drift,
        "generated_at": _now(),
        "left": _side_json(left),
        "right": _side_json(right),
        "options": {
            "chunk_size": options.chunk_size,
            "where": options.where,
            "algorithm": options.algorithm,
            "pushdown": options.pushdown,
            "resumed": state.resumed,
            "state_file": options.state_file,
            "max_diff_rows": options.max_diff_rows,
        },
        "summary": {
            "chunks": {
                "planned": len(chunks),
                "compared": len(chunks) - cached,
                "cached": cached,
                "matched": counts[MATCHED],
                "mismatched": counts[MISMATCHED],
                "errors": counts[ERROR],
            },
            "rows": {
                "left_total": left_total,
                "right_total": right_total,
                "added": added,
                "removed": removed,
                "changed": changed,
            },
        },
        "chunks": chunks,
    }


def _side_json(side):
    return {
        "source": side.dsn.redacted,
        "dialect": side.dialect.name,
        "table": side.table,
        "primary_key": [c.name for c in side.pk],
        "columns": [c.name for c in side.columns],
    }


def _range_json(lo, hi):
    return {"from": _key_json(lo) if lo is not None else None,
            "to": _key_json(hi) if hi is not None else None}


def _key_json(key):
    values = key if isinstance(key, (list, tuple)) else [key]
    return [_json_value(v) for v in values]


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "x" + bytes(value).hex().upper()
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


def _sorted_keys(mapping):
    keys = list(mapping)
    try:
        return sorted(keys)
    except TypeError:
        return sorted(keys, key=lambda k: [_json_value(v) for v in k])


def _explain(exc):
    return "%s: %s" % (type(exc).__name__, exc)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
