# rowdiff

[![PyPI version](https://img.shields.io/pypi/v/rowdiff.svg)](https://pypi.org/project/rowdiff/)
[![CI](https://github.com/intertermux-code/rowdiff/actions/workflows/test.yml/badge.svg)](https://github.com/intertermux-code/rowdiff/actions/workflows/test.yml)

Cross-database row diff that computes checksums **inside** the databases
instead of streaming both tables into Python.

```
rowdiff diff --left  sqlite://prod.db     --table orders --chunk-size 1000 \
             --right sqlite://staging.db
```

```
chunks   6 planned, 6 compared, 0 cached
         5 matched, 1 mismatched, 0 errors
rows     500 left, 500 right
drift    1 added, 1 removed, 1 changed
status   drift (0.009s)

chunks with differences
  MISMATCHED chunk 1  id (101) .. (201)
    left  100 rows  9d992cdd2ebff4e41555c50403679aeb
    right 100 rows  d15fa09ef2c98047061f26918f2edace
    ~ id=101  amount '328.25' -> '999.0', payload 'x70313031' -> 'x6368616E676564'
```

## Why this exists

Datafold's `data-diff` was the tool everyone reached for when they needed to
know whether two tables actually hold the same rows. It was sunset in May 2024,
and the community fork that followed has not moved in over a year. Meanwhile the
need did not go away: verifying a warehouse migration, checking that a dbt model
rewrite is byte-for-byte equivalent, gating a CI job on "prod and staging agree",
proving a backfill finished.

The obvious substitutes all have a catch. `SELECT * ORDER BY pk` diffs pull both
tables over the network into the client, so they die somewhere around a million
rows and they ship your data to whoever runs the script. Hashing whole tables
tells you *that* they differ, never *where*. Vendor tools charge per scan.

`rowdiff` takes the middle path: split the key space into chunks, ask each
database to fold its own chunk into a single digest, compare digests, and only
read rows for the handful of chunks that disagree. A clean comparison of a
50 million row table moves a few hundred integers and digests across the wire.

Design commitments:

* **Pushdown first.** The database does the hashing. SQLite goes through a
  registered aggregate; Postgres, MySQL and DuckDB get native digest SQL.
* **Standard library first.** `pip install rowdiff` installs *no* dependencies.
  Comparing two SQLite files needs nothing beyond CPython 3.8+. Drivers for other
  databases are optional extras.
* **Interruptible.** Progress lands in a local SQLite file; a re-run continues.
* **Machine readable.** `--format json` emits a documented drift report, so CI
  can gate on it without parsing prose.

## Install

```sh
pip install rowdiff                 # SQLite only, zero dependencies
pip install "rowdiff[postgres]"     # psycopg 3
pip install "rowdiff[mysql]"        # PyMySQL
pip install "rowdiff[duckdb]"       # duckdb
pip install "rowdiff[all]"
```

From a checkout for development:

```sh
git clone https://github.com/intertermux-code/rowdiff
cd rowdiff
python -m venv .venv && .venv/bin/pip install -e .[dev]  # or: pip install -e .
```

## Quickstart

Two SQLite files, no drivers, no server:

```sh
python - <<'PY'
import sqlite3
DDL = "CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL)"
def seed(path, rows):
    conn = sqlite3.connect(path); conn.execute(DDL)
    conn.executemany("INSERT INTO orders VALUES (?,?,?)", rows); conn.commit(); conn.close()
seed("prod.db",    [(i, "eu", i * 1.5) for i in range(1, 1001)])
seed("staging.db", [(i, "eu", i * 1.5) for i in range(1, 1001)] + [(1001, "ap", 0.0)])
PY

rowdiff diff --left sqlite://prod.db --right sqlite://staging.db \
             --table orders --chunk-size 500
echo $?   # 1: drift detected
```

Compare tables with different names, or in different engines:

```sh
rowdiff diff \
  --left "postgres://reader@warehouse:5432/analytics" --left-table  orders_v1 \
  --right "duckdb:///lake/mirror.duckdb"              --right-table orders_copy \
  --pk id --columns id,region,amount --chunk-size 25000
```

## How it works

**Chunking.** Each side answers one cheap query for every `--chunk-size`-th
distinct primary key:

```sql
SELECT "id" FROM (
  SELECT "id", row_number() OVER (ORDER BY "id") AS "rowdiff_rn"
  FROM (SELECT DISTINCT "id" FROM "users") "rowdiff_d"
) "rowdiff_w" WHERE MOD("rowdiff_rn", 5000) = 1 ORDER BY "rowdiff_rn"
```

The two boundary lists are merged, so a key range that only one side knows about
still gets compared. Chunk *i* is the half open range `[lo_i, hi_i)`; the last
chunk runs unbounded, which is what catches rows appended while the diff runs.
Composite keys are walked in key order, using row-value comparisons where the
dialect has them and an expanded `(a > x) OR (a = x AND b >= y)` form on SQLite
(the leading comparison must be strict, or chunks would overlap).

**Chunk checksum.** A row is encoded to one text value, then

```
digest(row)       = H(canonical_row_string)
checksum(chunk)   = H(concat of digest(row) sorted as text)
```

Sorting the per-row digests instead of relying on `ORDER BY` physical row order
makes the checksum independent of storage order, identical on both sides, and
the same formula for every dialect. `H` is MD5 by default, SHA-256 with
`--algorithm sha256`.

A chunk is matched when its row count *and* checksum agree. Only mismatched
chunks are read back row by row, and reported as added / removed / changed with
their key and the differing columns.

**Canonical row string.** Column values are encoded to text by the database
itself, joined with the ASCII unit separator (`x1f`), which cannot appear in the
encodings:

| kind | encoding | SQL, SQLite / Postgres |
| --- | --- | --- |
| NULL | `\N` | `COALESCE(CAST(c AS TEXT), '\N')` |
| text | as stored | `CAST(c AS TEXT)` |
| number | dialect's own cast | `CAST(c AS TEXT)` / `c::text` |
| boolean | `true` / `false` | `CASE WHEN c IS NULL … WHEN c IN (0,'false',…) THEN 'false' ELSE 'true' END` / `CASE WHEN c THEN 'true' ELSE 'false' END` |
| binary | `x` + uppercase hex | `'x' \|\| HEX(c)` / `'x' \|\| UPPER(ENCODE(c,'hex'))` |
| date/time | dialect's own cast | `CAST(c AS TEXT)` / `c::text` |

### Dialects and adapters

| dialect | driver | status | checksum path |
| --- | --- | --- | --- |
| `sqlite` | stdlib `sqlite3` | **tested end to end** | `rowdiff_fold` aggregate registered on the connection (UDF pushdown) |
| `duckdb` | `rowdiff[duckdb]` | adapter only | `MD5(STRING_AGG(MD5(row) ORDER BY MD5(row)))` |
| `postgres` | `rowdiff[postgres]`, psycopg 3 | adapter only | same as DuckDB; `SHA256(CONVERT_TO(row,'UTF8'))` for `--algorithm sha256` |
| `mysql` / `mariadb` | `rowdiff[mysql]`, PyMySQL | adapter only | `MD5(GROUP_CONCAT(MD5(row) ORDER BY MD5(row) SEPARATOR ''))`; `SHA2(c,256)` for SHA-256 |

"Tested" means the CI suite diffs real tables against that engine. "Adapter
only" means the generated SQL is unit tested as text and the code path is
complete, but nobody has run it against a live server yet — treat a first
connection as a verification step, and start such a comparison with
`--no-pushdown` if you want the row data rather than the SQL to do the work.

SQLite is the only dialect that needs a user defined function, because SQLite
has no digest built in (note that `sha256()` is not compiled into most stock
SQLite builds, including the one Apple ships). The UDF is what keeps pushdown
honest: SQL builds the row string, the aggregate folds digests in process, and
two values come back. If the driver cannot register aggregates, `rowdiff`
detects that and falls back to client side hashing of the chunk's rows.

`group_concat_max_len` defaults to 1024 bytes on MySQL, which would silently
truncate a chunk's concatenated digests into a wrong checksum, so the session
value is raised to 64 MB on connect.

## CLI reference

```
rowdiff [--version] COMMAND ...
rowdiff diff [OPTIONS]
```

| option | default | meaning |
| --- | --- | --- |
| `--left DSN`, `--right DSN` | required | data source URIs (see below) |
| `--table NAME` | — | table used on both sides |
| `--left-table NAME`, `--right-table NAME` | — | per side table name; overrides `--table` |
| `--pk COLUMN` | declared PK | repeatable; order defines the key sort order |
| `--columns COLUMN` | all columns | repeatable; the PK is always included |
| `--chunk-size N` | `5000` | distinct keys per chunk |
| `--where PREDICATE` | — | raw SQL predicate applied to both sides (e.g. `tenant_id = 42`) |
| `--algorithm md5\|sha256` | `md5` | digest used for row and chunk hashes |
| `--state PATH` | `.rowdiff-state.sqlite3` | SQLite file holding per-chunk results |
| `--no-resume` | off | recompare every chunk, ignoring finished ones |
| `--no-pushdown` | off | checksum client side after fetching each chunk's rows |
| `--max-diff-rows N` | `50` | row details listed per mismatched chunk; counts stay exact |
| `--format text\|json` | `text` | report format on stdout |
| `--output PATH` | stdout | write the report to a file instead |
| `--quiet` | off | suppress per-chunk progress on stderr |

Data source URIs:

```
sqlite:///absolute/path.db        sqlite://relative.db        sqlite://            (in memory)
duckdb:///warehouse/lake.duckdb
postgres://user:pw@host:5432/db?sslmode=require        postgresql://… (alias)
mysql://user:pw@host:3306/db                          mariadb://… (alias)
```

Passwords are never echoed: reports and error messages show
`postgres://user:***@host:5432/db`.

Exit codes, for CI:

| code | meaning |
| --- | --- |
| `0` | tables agree |
| `1` | drift found (mismatched chunks) |
| `2` | rowdiff could not run (bad URI, missing driver, unreadable table, chunk errors) |
| `130` | interrupted; finished chunks are already in the state file |

Progress goes to stderr, the report to stdout, so `rowdiff diff … --format json
| jq` works while the run is still visible on a terminal.

## Resuming

Every finished chunk is written to `--state` as `(status, counts, checksums,
row diff)` keyed by a fingerprint of the request: dialects, sources, tables, key
columns, compared columns, chunk size, algorithm and pushdown mode. Same
command, same file, same plan — so a re-run after a crash, a `SIGTERM`, or a
`Ctrl-C` skips the work already done and reports exactly what an uninterrupted
run would have reported.

```sh
rowdiff diff --left sqlite://a.db --right sqlite://b.db --table events   # interrupted
rowdiff diff --left sqlite://a.db --right sqlite://b.db --table events   # 412 cached, 88 compared
rowdiff diff --left sqlite://a.db --right sqlite://b.db --table events --no-resume
```

Because the fingerprint is what makes a cached chunk reusable, a chunk that
ended in an error is *not* recorded: the next run retries it. The flip side is
that resume assumes the tables have not moved between runs. If they may have,
use `--no-resume`, or point `--state` at a fresh file. The state file is
append-only per run and safe to delete at any time.

## The drift report

`--format json` emits one object per run. Every field below is always present
unless marked optional.

```json
{
  "schema_version": 1,
  "kind": "rowdiff.drift",
  "status": "ok | drift | error",
  "drift": true,
  "generated_at": "2026-10-20T09:14:03Z",
  "duration_seconds": 0.009,
  "left": {
    "source": "sqlite://prod.db",
    "dialect": "sqlite",
    "table": "orders",
    "primary_key": ["id"],
    "columns": ["id", "region", "amount"]
  },
  "right": { "…": "same shape as left" },
  "options": {
    "chunk_size": 100, "where": null, "algorithm": "md5", "pushdown": true,
    "resumed": false, "state_file": ".rowdiff-state.sqlite3",
    "max_diff_rows": 50
  },
  "summary": {
    "chunks": { "planned": 7, "compared": 7, "cached": 0,
                "matched": 4, "mismatched": 3, "errors": 0 },
    "rows":   { "left_total": 500, "right_total": 500,
                "added": 1, "removed": 1, "changed": 1 }
  },
  "chunks": [ { "index": 1,
                "pk_range": { "from": [101], "to": [201] },
                "status": "mismatched",
                "left":  { "row_count": 100, "checksum": "9d992c…" },
                "right": { "row_count": 100, "checksum": "d15fa0…" },
                "rows": { "added": 0, "removed": 0, "changed": 1 },
                "row_diff": {
                  "added":   [[501]],
                  "removed": [[250]],
                  "changed": [{ "pk": [101],
                                "columns": [{ "name": "amount",
                                               "left": "328.25",
                                               "right": "999.0" }] }],
                  "truncated": false
                },
                "cached": false,
                "error": { "message": "…" } } ]
}
```

* `chunks[].status` is `matched`, `mismatched` or `error`. `row_diff` is `null`
  for `matched`; `error` is only present for `error`, and its `left`/`right`
  counts and checksums are `null`.
* `pk_range.from` / `.to` are arrays of key values, one per `--pk` column; `to`
  is `null` for the final unbounded chunk. Values are JSON in the key's own
  shape (numbers, strings, booleans), with bytea keys as `x…` hex and
  dates as ISO 8601.
* `row_diff` lists are capped at `max_diff_rows`; `rows` and `summary.rows`
  always count every difference, and `truncated` says whether the list was cut.
* Chunk checksums are hex digests, 32 characters for MD5, 64 for SHA-256.

A CI gate is then just:

```sh
rowdiff diff --left "$SRC" --right "$DST" --table orders --format json --quiet \
             --output drift.json || { jq -r '.summary' drift.json; exit 1; }
```

## Limitations, honestly

* **Same column names, same meaning.** rowdiff matches columns by name. It will
  not translate `customer_id` into `id`, cast across type families, or ignore
  columns for you beyond what `--columns` says.
* **Cross-dialect type formatting is native.** Numbers, dates and timestamps are
  hashed through each engine's own cast, so `1.0` vs `1`, `2026-01-02 03:04:05`
  vs `2026-01-02T03:04:05Z`, or a numeric with a different scale can read as
  drift when the values are semantically equal. Homogeneous comparisons
  (SQLite↔SQLite, Postgres↔Postgres) are exact; heterogeneous ones are best
  narrowed with `--columns`. Text collation differences (case or accent
  insensitive MySQL columns) behave the same way.
* **Digest choice.** MD5 is the default because Postgres and MySQL make it
  cheap and chunk checksums are an equality test, not a security boundary. Use
  `--algorithm sha256` if a second-preimage matters to you.
* **`--where` is raw SQL by design.** The predicate is interpolated into generated SQL on both sides without parameterization or validation; treat it like any ad-hoc SQL (review it, scope it to read-only, and never feed untrusted input). It is applied identically to chunking and row-report queries so the two cannot disagree about scope.
* **Chunk sizes are key-count based, not byte based.** A chunk of 5000 rows of
  wide `TEXT` still transfers 5000 rows if that chunk mismatches.
* **One key range per chunk, keys must be comparable.** Two sources whose key
  types differ (int vs text) are refused rather than guessed at. Tables with no
  primary key need `--pk`, and rowdiff cannot stop duplicate keys from making a
  chunk ambiguous.
* **Data that moves while you diff.** Appended rows land in the final unbounded
  chunk and show up as drift; deleted ranges can make a resumed run stale until
  you use `--no-resume`.
* **Adapter-only dialects.** DuckDB, Postgres and MySQL are covered by SQL text
  tests only, and the `--pk`/`information_schema` introspection for them has not
  been run against a live server. MySQL needs 8.0+ (window functions), Postgres
  9.4+ and a SQLite build with aggregate UDF support (all standard ones).
* **No streaming between sides, no parallel chunk workers yet.** One chunk after
  another, single connection per side. Chunk-level parallelism is the obvious
  next step and the state model already supports it.

## Development

```sh
pip install -e ".[dev]"
pytest                      # 139 tests, no network, no servers
pytest -q tests/test_diff_sqlite.py
```

The suite covers identical tables, added/removed/changed rows, composite keys,
chunk tiling, interrupt-and-resume, error retry, MD5/SHA-256 parity,
pushdown/fallback digest parity, the JSON contract, the CLI exit codes, and the
SQL text emitted for every adapter.

## Releasing

1. Bump the version in `pyproject.toml` and `rowdiff/__init__.py`.
2. `git tag vX.Y.Z`
3. Push the tag: `git push origin vX.Y.Z`.

`publish.yml` builds the sdist and wheel, runs `twine check`, and publishes to
PyPI via OIDC trusted publishing. The workflow fails early if the tag version
does not match `pyproject.toml`.

One-time manual step: register the trusted publisher on pypi.org for project
`rowdiff`, owner `intertermux-code/rowdiff`, workflow filename `publish.yml`,
environment `pypi`. This must be done by the maintainer and cannot be
automated.

## License

MIT, see [LICENSE](LICENSE).
