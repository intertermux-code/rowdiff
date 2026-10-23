from rowdiff.engine import Options, run


def diff(left, right, table="users", **kwargs):
    kwargs.setdefault("chunk_size", 25)
    options = Options(left="sqlite://%s" % left, left_table=table,
                      right="sqlite://%s" % right, right_table=table, **kwargs)
    return run(options)


def with_row(rows, primary_key, column, value):
    """Copy ``rows`` with one column of one row replaced."""
    names = ["id", "name", "score", "note", "active", "fingerprint"]
    index = names.index(column)
    staged = []
    for row in rows:
        row = list(row)
        if row[0] == primary_key:
            row[index] = value
        staged.append(tuple(row))
    return staged


def without_row(rows, primary_key):
    return [row for row in rows if row[0] != primary_key]


def with_extra_row(rows, row):
    return list(rows) + [row]


def with_reading(rows, key, column, value):
    """Same as :func:`with_row` for the composite ``readings`` table."""
    names = ["device_id", "taken_at", "value", "label"]
    index = names.index(column)
    staged = []
    for row in rows:
        row = list(row)
        if tuple(row[:2]) == key:
            row[index] = value
        staged.append(tuple(row))
    return staged


def reported_keys(report):
    """Every primary key the row-level drill-down listed, across all chunks."""
    keys = set()
    for chunk in report["chunks"]:
        detail = chunk["row_diff"]
        if detail:
            for kind in ("added", "removed"):
                keys.update(pk[0] for pk in detail[kind])
            keys.update(entry["pk"][0] for entry in detail["changed"])
    return keys


def chunk_row_counts(report, side="left"):
    return [chunk[side]["row_count"] for chunk in report["chunks"]]
