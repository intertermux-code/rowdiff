import json

from helpers import diff, with_row

TOP = {"schema_version", "kind", "status", "drift", "generated_at", "left",
       "right", "options", "summary", "chunks", "duration_seconds"}
SIDE = {"source", "dialect", "table", "primary_key", "columns"}
OPTIONS = {"chunk_size", "algorithm", "pushdown", "resumed", "state_file",
           "max_diff_rows"}
CHUNK_KEYS = {"index", "pk_range", "status", "left", "right", "rows",
              "row_diff", "cached"}
CHUNK_SUMMARY = {"planned", "compared", "cached", "matched", "mismatched",
                 "errors"}
ROW_SUMMARY = {"left_total", "right_total", "added", "removed", "changed"}
DETAIL = {"added", "removed", "changed", "truncated"}


def drifted_report(sqlite_pair, tmp_path):
    left, right = sqlite_pair(mutate_right=lambda rows: with_row(
        rows, 42, "name", "renamed"))
    return diff(left, right, state_file=str(tmp_path / "state.sqlite3"))


def test_report_round_trips_and_matches_the_documented_shape(
        sqlite_pair, tmp_path):
    report = drifted_report(sqlite_pair, tmp_path)
    text = json.dumps(report)
    parsed = json.loads(text)

    assert set(parsed) == TOP
    assert parsed["schema_version"] == 1
    assert parsed["kind"] == "rowdiff.drift"
    assert parsed["status"] in ("ok", "drift", "error")
    for side in ("left", "right"):
        assert set(parsed[side]) == SIDE
        assert parsed[side]["dialect"] == "sqlite"
    assert set(parsed["options"]) == OPTIONS
    assert set(parsed["summary"]) == {"chunks", "rows"}
    assert set(parsed["summary"]["chunks"]) == CHUNK_SUMMARY
    assert set(parsed["summary"]["rows"]) == ROW_SUMMARY


def test_chunk_entries_are_self_consistent(sqlite_pair, tmp_path):
    report = drifted_report(sqlite_pair, tmp_path)
    for chunk in report["chunks"]:
        assert set(chunk) == CHUNK_KEYS
        assert set(chunk["pk_range"]) == {"from", "to"}
        assert set(chunk["left"]) == set(chunk["right"]) == {"row_count",
                                                            "checksum"}
        assert set(chunk["rows"]) == {"added", "removed", "changed"}
        assert chunk["status"] in ("matched", "mismatched", "error")
        if chunk["status"] == "matched":
            assert chunk["row_diff"] is None
            assert chunk["rows"] == {"added": 0, "removed": 0, "changed": 0}
        if chunk["status"] == "mismatched":
            assert set(chunk["row_diff"]) == DETAIL
            assert chunk["left"]["checksum"] != chunk["right"]["checksum"]


def test_row_diff_entries_carry_key_and_columns(sqlite_pair, tmp_path):
    report = drifted_report(sqlite_pair, tmp_path)
    detail = [c for c in report["chunks"] if c["status"] == "mismatched"][0]
    assert detail["row_diff"]["changed"] == [
        {"pk": [42], "columns": [{"name": "name", "left": "user-042",
                                  "right": "renamed"}]}]
    assert detail["row_diff"]["added"] == []
    assert detail["row_diff"]["removed"] == []
    assert detail["row_diff"]["truncated"] is False


def test_summary_counts_equal_the_sum_of_chunks(sqlite_pair, tmp_path):
    report = drifted_report(sqlite_pair, tmp_path)
    summary = report["summary"]
    assert summary["chunks"]["planned"] == len(report["chunks"])
    assert (summary["chunks"]["matched"] + summary["chunks"]["mismatched"]
            + summary["chunks"]["errors"]) == summary["chunks"]["planned"]
    for key in ("added", "removed", "changed"):
        assert summary["rows"][key] == sum(
            chunk["rows"][key] for chunk in report["chunks"])


def test_credential_bearing_sources_are_redacted(tmp_path):
    """Nothing that looks like a password may reach the report."""
    from rowdiff.dsn import parse_dsn
    dsn = parse_dsn("postgres://analyst:sup3rs3cret@warehouse:5432/marts")
    assert "sup3rs3cret" not in dsn.redacted
    assert dsn.redacted == "postgres://analyst:***@warehouse:5432/marts"
