"""Chunk planning and the value codec used to persist chunk boundaries.

Chunk boundaries come from the databases: each side answers one query for every
``chunk_size``-th distinct primary key, and the engine compares the half open
range ``[lo, hi)`` that falls out of the merged boundary list. Only key values
cross the wire here, never rows.

Boundaries are written to the state file so a resumed run walks exactly the same
ranges, which means key values must survive a JSON round trip with their type
intact: plain scalars go through as they are, anything else gets a tagged form.
"""

from __future__ import annotations

import base64
import datetime

from .errors import ConfigError


def merge_boundaries(*key_lists):
    merged = set()
    for keys in key_lists:
        merged.update(keys)
    try:
        return sorted(merged)
    except TypeError as exc:
        raise ConfigError(
            "primary key values from the two sources are not comparable (%s);"
            " both sides must use the same key type" % exc) from exc


def plan_ranges(bounds):
    """``[b0, b1), [b1, b2), ... [bn, unbounded)``."""
    ranges = []
    for index, low in enumerate(bounds):
        high = bounds[index + 1] if index + 1 < len(bounds) else None
        ranges.append((low, high))
    return ranges


def encode_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"~": "b", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, datetime.datetime):
        return {"~": "dt", "v": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"~": "d", "v": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"~": "t", "v": value.isoformat()}
    return {"~": "s", "v": str(value)}


def decode_value(encoded):
    if isinstance(encoded, dict):
        kind, text = encoded["~"], encoded["v"]
        if kind == "b":
            return base64.b64decode(text.encode("ascii"))
        if kind == "dt":
            return datetime.datetime.fromisoformat(text)
        if kind == "d":
            return datetime.date.fromisoformat(text)
        if kind == "t":
            return datetime.time.fromisoformat(text)
        return text
    return encoded


def encode_key(key):
    if isinstance(key, (list, tuple)):
        return [encode_value(v) for v in key]
    return [encode_value(key)]


def decode_key(encoded):
    return tuple(decode_value(v) for v in encoded)
