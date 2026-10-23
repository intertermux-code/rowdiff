import pytest

from helpers import (chunk_row_counts, diff, reported_keys, without_row,
                     with_extra_row, with_row)
from rowdiff.engine import MATCHED, MISMATCHED


def test_identical_tables_show_no_drift(sqlite_pair, tmp_path):
    left, right = sqlite_pair()
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))

    assert report["drift"] is False
    assert report["status"] == "ok"
    chunks = report["summary"]["chunks"]
    assert chunks["planned"] == 5
    assert chunks["matched"] == chunks["planned"]
    assert chunks["mismatched"] == 0 and chunks["errors"] == 0
    rows = report["summary"]["rows"]
    assert (rows["added"], rows["removed"], rows["changed"]) == (0, 0, 0)
    assert rows["left_total"] == rows["right_total"] == 120


def test_identical_diff_never_fetches_rows(monkeypatch, sqlite_pair, tmp_path):
    """Pushdown is the default: matched chunks cost two values, not rows."""
    from rowdiff.dialects.base import Dialect

    def explode(self, *args, **kwargs):
        raise AssertionError("a chunk was fetched instead of checksummed")

    monkeypatch.setattr(Dialect, "fetch_rows", explode)
    left, right = sqlite_pair()
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))
    assert report["drift"] is False
    assert all(c["status"] == MATCHED for c in report["chunks"])


def test_row_added_on_the_right(sqlite_pair, tmp_path):
    extra = (121, "user-121", 181.5, "note 121", 1, b"fp-121")
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_extra_row(rows, extra))
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))

    assert report["status"] == "drift"
    assert report["summary"]["rows"]["added"] == 1
    bad = [c for c in report["chunks"] if c["status"] == MISMATCHED]
    assert len(bad) == 1
    assert bad[0]["row_diff"]["added"] == [[121]]
    assert bad[0]["row_diff"]["removed"] == []
    assert bad[0]["left"]["row_count"] == 20
    assert bad[0]["right"]["row_count"] == 21


def test_row_removed_on_the_right(sqlite_pair, tmp_path):
    left, right = sqlite_pair(mutate_right=lambda rows: without_row(rows, 40))
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))

    assert report["summary"]["rows"]["removed"] == 1
    bad = [c for c in report["chunks"] if c["status"] == MISMATCHED]
    assert bad[0]["row_diff"]["removed"] == [[40]]
    assert bad[0]["pk_range"] == {"from": [26], "to": [51]}


def test_changed_value_reports_the_column_that_differs(sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))

    assert report["summary"]["rows"]["changed"] == 1
    entry = [c for c in report["chunks"]
             if c["status"] == MISMATCHED][0]["row_diff"]["changed"]
    assert entry[0]["pk"] == [42]
    assert entry[0]["columns"] == [{"name": "name", "left": "user-042",
                                    "right": "renamed"}]


def test_several_columns_of_one_row(sqlite_pair, tmp_path):
    def mutate(rows):
        rows = with_row(rows, 42, "score", 999.0)
        rows = with_row(rows, 42, "note", "changed note")
        rows = with_row(rows, 45, "active", 1)
        return rows

    left, right = sqlite_pair(mutate_right=mutate)
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))

    assert report["summary"]["rows"]["changed"] == 2
    changed = [c for c in report["chunks"]
               if c["status"] == MISMATCHED][0]["row_diff"]["changed"]
    by_pk = {row["pk"][0]: [c["name"] for c in row["columns"]] for row in changed}
    assert sorted(by_pk[42]) == ["note", "score"]
    assert by_pk[45] == ["active"]


def test_null_becomes_a_visible_value(sqlite_pair, tmp_path):
    """note is NULL for every 7th row; clearing another one is drift."""
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 43, "note", None))
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))

    changed = [c for c in report["chunks"]
               if c["status"] == MISMATCHED][0]["row_diff"]["changed"]
    assert changed[0]["pk"] == [43]
    assert changed[0]["columns"] == [{"name": "note", "left": "note 43",
                                      "right": "\\N"}]


def test_boolean_and_blob_columns(sqlite_pair, tmp_path):
    def mutate(rows):
        rows = with_row(rows, 30, "active", 1)
        rows = with_row(rows, 30, "fingerprint", b"fp-30!")
        return rows

    left, right = sqlite_pair(mutate_right=mutate)
    report = diff(left, right, state_file=str(tmp_path / "state.sqlite3"))
    changed = [c for c in report["chunks"]
               if c["status"] == MISMATCHED][0]["row_diff"]["changed"]
    columns = {c["name"]: c for c in changed[0]["columns"]}
    assert columns["active"]["left"] == "false"
    assert columns["active"]["right"] == "true"
    assert columns["fingerprint"]["left"] == "x" + b"fp-30".hex().upper()
    assert columns["fingerprint"]["right"] == "x" + b"fp-30!".hex().upper()


def test_diff_only_selected_columns(tmp_path):
    from conftest import USERS_DDL, build, users
    left = build(str(tmp_path / "l.db"), "users", USERS_DDL, users())
    right = build(str(tmp_path / "r.db"), "users", USERS_DDL,
                  with_row(users(), 42, "score", 1.0))
    report = diff(left, right, columns=["name"],
                  state_file=str(tmp_path / "state.sqlite3"))
    assert report["drift"] is False
    assert report["left"]["columns"] == ["name", "id"]


def test_drift_inside_selected_columns(sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    report = diff(left, right, columns=["name"],
                  state_file=str(tmp_path / "state.sqlite3"))
    assert report["summary"]["rows"]["changed"] == 1


def test_sha256_reports_the_same_drift_as_md5(sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    md5 = diff(left, right, algorithm="md5",
               state_file=str(tmp_path / "md5.sqlite3"))
    sha = diff(left, right, algorithm="sha256",
               state_file=str(tmp_path / "sha.sqlite3"))
    assert md5["summary"] == sha["summary"]
    assert all(len(c["left"]["checksum"]) == 32 for c in md5["chunks"])
    assert all(len(c["left"]["checksum"]) == 64 for c in sha["chunks"])


def test_client_side_hashing_matches_pushdown(sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    pushed = diff(left, right, state_file=str(tmp_path / "a.sqlite3"))
    pulled = diff(left, right, pushdown=False,
                  state_file=str(tmp_path / "b.sqlite3"))
    assert [c["left"]["checksum"] for c in pushed["chunks"]] == \
        [c["left"]["checksum"] for c in pulled["chunks"]]
    assert pushed["summary"] == pulled["summary"]


def test_two_empty_tables_compare_equal(tmp_path):
    import sqlite3

    from conftest import USERS_DDL
    paths = []
    for name in ("left", "right"):
        path = str(tmp_path / ("%s.db" % name))
        conn = sqlite3.connect(path)
        conn.execute(USERS_DDL)
        conn.close()
        paths.append(path)

    report = diff(paths[0], paths[1], state_file=str(tmp_path / "s.sqlite3"))
    assert report["drift"] is False
    assert report["summary"]["chunks"]["planned"] == 1
    assert report["summary"]["rows"]["left_total"] == 0
    assert report["summary"]["rows"]["right_total"] == 0


def test_max_diff_rows_truncates_details_but_not_counts(sqlite_pair, tmp_path):
    def drop_every_fifth(rows):
        return [row for row in rows if row[0] % 5]

    left, right = sqlite_pair(mutate_right=drop_every_fifth)
    report = diff(left, right, max_diff_rows=3,
                  state_file=str(tmp_path / "state.sqlite3"))
    assert report["summary"]["rows"]["removed"] == 24
    listed = sum(len(c["row_diff"]["removed"]) for c in report["chunks"]
                 if c["status"] == MISMATCHED)
    truncated = [c for c in report["chunks"] if c["status"] == MISMATCHED]
    assert listed < 24
    assert any(c["row_diff"]["truncated"] for c in truncated)


def test_missing_primary_key_is_a_config_error(tmp_path):
    from conftest import build
    ddl = "CREATE TABLE loose (a INTEGER, b TEXT)"
    left = build(str(tmp_path / "l.db"), "loose", ddl, [(1, "x")])
    right = build(str(tmp_path / "r.db"), "loose", ddl, [(1, "x")])
    from rowdiff.errors import ConfigError
    with pytest.raises(ConfigError, match="no primary key"):
        diff(left, right, table="loose", state_file=str(tmp_path / "s.sqlite3"))


# -- --where scoping -------------------------------------------------------
#
# ``id % 2 = 0`` leaves a rejected key between every pair of accepted ones, so
# a chunk range always holds rows the predicate excludes. A prefix predicate
# like ``id < 60`` would only exercise the boundaries.


def test_rows_outside_the_predicate_reach_neither_checksum_nor_report(
        sqlite_pair, tmp_path):
    extra = (121, "user-121", 181.5, "note 121", 1, b"fp-121")

    def mutate(rows):
        rows = with_extra_row(rows, extra)
        return with_row(rows, 41, "name", "renamed")

    left, right = sqlite_pair(mutate_right=mutate)
    whole = diff(left, right, state_file=str(tmp_path / "all.sqlite3"))
    assert whole["drift"] is True
    assert (whole["summary"]["rows"]["added"],
            whole["summary"]["rows"]["changed"]) == (1, 1)

    scoped = diff(left, right, where="id % 2 = 0",
                  state_file=str(tmp_path / "even.sqlite3"))
    assert scoped["status"] == "ok"
    assert scoped["drift"] is False
    assert all(c["status"] == MATCHED for c in scoped["chunks"])
    rows = scoped["summary"]["rows"]
    assert (rows["added"], rows["removed"], rows["changed"]) == (0, 0, 0)
    # 60 of the 120 keys satisfy the predicate, and planning sees only those.
    assert rows["left_total"] == rows["right_total"] == 60
    assert scoped["summary"]["chunks"]["planned"] == 3
    assert chunk_row_counts(scoped) == [25, 25, 10]


def test_the_predicate_sees_drift_inside_and_is_blind_outside(
        sqlite_pair, tmp_path):
    def mutate(rows):
        rows = with_row(rows, 42, "name", "renamed")
        return with_row(rows, 71, "note", "off predicate")

    left, right = sqlite_pair(mutate_right=mutate)
    scoped = diff(left, right, where="id % 2 = 0",
                  state_file=str(tmp_path / "scoped.sqlite3"))
    assert scoped["summary"]["rows"]["changed"] == 1
    assert reported_keys(scoped) == {42}

    whole = diff(left, right, state_file=str(tmp_path / "whole.sqlite3"))
    assert whole["summary"]["rows"]["changed"] == 2
    assert reported_keys(whole) == {42, 71}


def test_scoped_verdicts_agree_across_chunk_sizes(sqlite_pair, tmp_path):
    def mutate(rows):
        rows = with_row(rows, 42, "name", "renamed")
        return with_row(rows, 90, "score", 999.0)

    left, right = sqlite_pair(mutate_right=mutate)
    narrow = diff(left, right, chunk_size=7, where="id % 2 = 0",
                  state_file=str(tmp_path / "narrow.sqlite3"))
    # 5000 is the default chunk size: one chunk covers the whole key space.
    wide = diff(left, right, chunk_size=5000, where="id % 2 = 0",
                state_file=str(tmp_path / "wide.sqlite3"))

    assert narrow["drift"] is wide["drift"] is True
    assert narrow["summary"]["rows"] == wide["summary"]["rows"]
    assert reported_keys(narrow) == reported_keys(wide) == {42, 90}
    assert (narrow["summary"]["chunks"]["planned"]
            > wide["summary"]["chunks"]["planned"])
    for report in (narrow, wide):
        assert sum(chunk_row_counts(report)) == 60
        assert sum(chunk_row_counts(report, "right")) == 60


def test_an_unchanged_diff_stays_clean_at_every_chunk_size(
        sqlite_pair, tmp_path):
    left, right = sqlite_pair()
    reports = [diff(left, right, chunk_size=size, where="id % 3 = 0",
                    state_file=str(tmp_path / ("%d.sqlite3" % size)))
               for size in (1, 4, 25, 5000)]
    assert all(r["drift"] is False for r in reports)
    assert all(all(c["status"] == MATCHED for c in r["chunks"])
               for r in reports)
    assert {r["summary"]["rows"]["left_total"] for r in reports} == {40}
    for report in reports:
        assert sum(chunk_row_counts(report)) == 40


def test_a_predicate_matching_nothing_compares_empty_chunks(
        sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    report = diff(left, right, where="id > 1000",
                  state_file=str(tmp_path / "none.sqlite3"))
    assert report["drift"] is False
    assert report["summary"]["chunks"]["planned"] == 1
    assert chunk_row_counts(report) == [0]
    assert chunk_row_counts(report, "right") == [0]
    assert report["summary"]["rows"]["left_total"] == 0


def test_the_predicate_is_part_of_the_report(sqlite_pair, tmp_path):
    left, right = sqlite_pair()
    scoped = diff(left, right, where="active = 1",
                  state_file=str(tmp_path / "opt.sqlite3"))
    assert scoped["options"]["where"] == "active = 1"
    # conftest sets active = 1 for every id that is not a multiple of three.
    assert scoped["summary"]["rows"]["left_total"] == 80

    unscoped = diff(left, right, state_file=str(tmp_path / "plain.sqlite3"))
    assert unscoped["options"]["where"] is None


def test_a_mismatch_is_still_drill_downable_under_a_predicate(
        sqlite_pair, tmp_path):
    """The row query carries the predicate too, so details omit excluded rows."""
    def mutate(rows):
        rows = with_row(rows, 44, "name", "renamed")
        return with_row(rows, 44, "note", "also renamed")

    left, right = sqlite_pair(mutate_right=mutate)
    report = diff(left, right, where="id % 2 = 0",
                  state_file=str(tmp_path / "drill.sqlite3"))
    entry = [c for c in report["chunks"] if c["status"] == MISMATCHED][0]
    assert entry["row_diff"]["changed"] == [
        {"pk": [44], "columns": [
            {"name": "name", "left": "user-044", "right": "renamed"},
            {"name": "note", "left": "note 44", "right": "also renamed"}]}]
