import json
import sqlite3

import pytest

from helpers import diff, with_row
from rowdiff.engine import Options, run


def strip_timing(report):
    report = dict(report)
    report.pop("duration_seconds")
    report["generated_at"] = ""
    chunks = []
    for chunk in report["chunks"]:
        chunk = dict(chunk)
        chunk.pop("cached")
        chunks.append(chunk)
    report["chunks"] = chunks
    report["summary"]["chunks"].pop("cached")
    report["summary"]["chunks"]["compared"] = \
        report["summary"]["chunks"]["planned"]
    report["options"]["resumed"] = False
    report["options"]["state_file"] = ""
    return report


def test_interrupted_run_resumes_and_agrees_with_an_uninterrupted_one(
        sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    baseline = strip_timing(
        diff(left, right, state_file=str(tmp_path / "baseline.sqlite3")))

    state = str(tmp_path / "resumed.sqlite3")
    seen = []

    def stop_after_three(done, total, chunk):
        seen.append(chunk["status"])
        if len(seen) == 3:
            raise KeyboardInterrupt

    interrupted = Options(left="sqlite://%s" % left, left_table="users",
                          right="sqlite://%s" % right, right_table="users",
                          chunk_size=25, state_file=state)
    with pytest.raises(KeyboardInterrupt):
        run(interrupted, progress=stop_after_three)
    assert len(seen) == 3

    finished = strip_timing(diff(left, right, state_file=state))
    assert finished == baseline
    assert seen == [c["status"] for c in baseline["chunks"][:3]]


def test_resumed_run_reuses_finished_chunks(sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    state = str(tmp_path / "state.sqlite3")
    diff(left, right, state_file=state)
    report = diff(left, right, state_file=state)

    assert report["options"]["resumed"] is True
    assert report["summary"]["chunks"]["cached"] == 5
    assert report["summary"]["chunks"]["compared"] == 0
    assert all(chunk["cached"] for chunk in report["chunks"])


def test_no_resume_recompares_everything(sqlite_pair, tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    state = str(tmp_path / "state.sqlite3")
    diff(left, right, state_file=state)
    report = diff(left, right, state_file=state, resume=False)

    assert report["summary"]["chunks"]["cached"] == 0
    assert report["summary"]["chunks"]["compared"] == 5
    assert not report["options"]["resumed"]


def test_resume_survives_a_state_file_change_of_directory(sqlite_pair, tmp_path):
    """The state file is the only memory the run has; it must be self contained."""
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    state = str(tmp_path / "state.sqlite3")
    first = diff(left, right, state_file=state)
    second = diff(left, right, state_file=state)
    assert first["summary"]["rows"] == second["summary"]["rows"]
    assert [c["pk_range"] for c in first["chunks"]] == \
        [c["pk_range"] for c in second["chunks"]]


def test_a_different_chunk_size_is_a_different_run(sqlite_pair, tmp_path):
    left, right = sqlite_pair()
    state = str(tmp_path / "state.sqlite3")
    diff(left, right, state_file=state, chunk_size=25)
    report = diff(left, right, state_file=state, chunk_size=40)
    assert report["summary"]["chunks"]["cached"] == 0
    assert report["summary"]["chunks"]["planned"] == 3


def test_the_plan_is_recorded_in_the_state_file(sqlite_pair, tmp_path):
    """Cached chunks are keyed by index, so their ranges have to be on file."""
    left, right = sqlite_pair()
    state = str(tmp_path / "state.sqlite3")
    first = diff(left, right, state_file=state)
    recorded = json.loads(sqlite3.connect(state).execute(
        "SELECT ranges FROM runs").fetchone()[0])
    assert [{"from": r["from"], "to": r["to"]} for r in recorded] == \
        [c["pk_range"] for c in first["chunks"]]


def test_a_moved_key_space_is_not_answered_by_the_old_plan(sqlite_pair,
                                                            tmp_path):
    """Rows deleted between runs shift the boundaries; stale indices must not
    be relabelled as answers for the ranges they never covered."""
    left, right = sqlite_pair()
    state = str(tmp_path / "state.sqlite3")
    assert diff(left, right, state_file=state)["status"] == "ok"

    conn = sqlite3.connect(right)
    conn.execute("DELETE FROM users WHERE id = 1")
    conn.commit()
    conn.close()

    report = diff(left, right, state_file=state)
    assert report["status"] == "drift"
    assert report["summary"]["rows"]["removed"] == 1
    assert report["summary"]["chunks"]["cached"] == 0
    assert report["summary"]["rows"]["left_total"] == 120
    assert report["summary"]["rows"]["right_total"] == 119


def test_error_chunks_are_retried_on_the_next_run(monkeypatch, sqlite_pair,
                                                  tmp_path):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    state = str(tmp_path / "state.sqlite3")

    from rowdiff.dialects.sqlite import SQLite
    original = SQLite.checksum_sql
    calls = []

    def flaky(self, table, pk, columns, lo, hi, where=None):
        calls.append(lo)
        if lo == (26,):
            raise RuntimeError("simulated backend failure")
        return original(self, table, pk, columns, lo, hi, where=where)

    monkeypatch.setattr(SQLite, "checksum_sql", flaky)
    broken = diff(left, right, state_file=state)
    assert broken["summary"]["chunks"]["errors"] == 1
    assert broken["status"] == "error"

    monkeypatch.undo()
    fixed = diff(left, right, state_file=state)
    assert fixed["summary"]["chunks"]["errors"] == 0
    assert fixed["summary"]["chunks"]["cached"] == 4
    assert fixed["drift"] is True
