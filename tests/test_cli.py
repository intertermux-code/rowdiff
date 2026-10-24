import json

import pytest

from helpers import with_row
from rowdiff.cli import EXIT_DRIFT, EXIT_ERROR, EXIT_OK, main


def argv(left, right, *extra, **kwargs):
    args = ["diff", "--left", "sqlite://%s" % left, "--right",
            "sqlite://%s" % right, "--table", kwargs.get("table", "users"),
            "--chunk-size", str(kwargs.get("chunk_size", 25)),
            "--state", kwargs["state"]]
    return args + list(extra)


def test_text_output_and_exit_code_for_identical_tables(sqlite_pair, tmp_path,
                                                        capsys):
    left, right = sqlite_pair()
    code = main(argv(left, right, state=str(tmp_path / "s.sqlite3")))
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "status   ok" in out
    assert "drift    0 added, 0 removed, 0 changed" in out
    assert "chunks   5 planned" in out


def test_drift_exit_code_and_details(sqlite_pair, tmp_path, capsys):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    code = main(argv(left, right, state=str(tmp_path / "s.sqlite3")))
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT
    assert "status   drift" in out
    assert "drift    0 added, 0 removed, 1 changed" in out
    assert "MISMATCHED chunk 1" in out
    assert "~ id=42  name 'user-042' -> 'renamed'" in out


def test_json_format_is_machine_readable(sqlite_pair, tmp_path, capsys):
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    code = main(argv(left, right, "--format", "json", "--quiet",
                     state=str(tmp_path / "s.sqlite3")))
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_DRIFT
    assert payload["kind"] == "rowdiff.drift"
    assert payload["summary"]["rows"]["changed"] == 1


def test_output_file(sqlite_pair, tmp_path, capsys):
    left, right = sqlite_pair()
    target = tmp_path / "report.json"
    code = main(argv(left, right, "--format", "json", "--output", str(target),
                     state=str(tmp_path / "s.sqlite3")))
    assert code == EXIT_OK
    assert capsys.readouterr().out == ""
    assert json.loads(target.read_text())["drift"] is False


def test_progress_lines_go_to_stderr_not_stdout(sqlite_pair, tmp_path, capsys):
    left, right = sqlite_pair()
    main(argv(left, right, "--format", "json", state=str(tmp_path / "s.sqlite3")))
    captured = capsys.readouterr()
    assert captured.out.startswith("{")
    assert captured.err.count("chunk ") == 5


def test_quiet_says_nothing_on_stderr(sqlite_pair, tmp_path, capsys):
    left, right = sqlite_pair()
    main(argv(left, right, "--quiet", state=str(tmp_path / "s.sqlite3")))
    assert capsys.readouterr().err == ""


def test_composite_key_from_the_command_line(sqlite_composite, tmp_path, capsys):
    left, right = sqlite_composite()
    code = main(argv(left, right, "--pk", "device_id", "--pk", "taken_at",
                     state=str(tmp_path / "s.sqlite3"), table="readings"))
    assert code == EXIT_OK
    assert "readings.device_id,taken_at" in capsys.readouterr().out


def test_unwritable_output_path_is_a_clean_error(sqlite_pair, tmp_path, capsys):
    left, right = sqlite_pair()
    code = main(argv(left, right, "--output", str(tmp_path / "nope" / "r.json"),
                     state=str(tmp_path / "s.sqlite3")))
    assert code == EXIT_ERROR
    assert "cannot write" in capsys.readouterr().err


def test_bad_dsn_is_reported_without_a_traceback(tmp_path, capsys):
    code = main(["diff", "--left", "mssql://h/db", "--right", "sqlite://x.db",
                 "--table", "t", "--state", str(tmp_path / "s.sqlite3")])
    assert code == EXIT_ERROR
    assert "unknown data source scheme" in capsys.readouterr().err


def test_missing_state_directory_is_a_clean_error(tmp_path, sqlite_pair, capsys):
    left, right = sqlite_pair()
    code = main(argv(left, right, state=str(tmp_path / "nope" / "s.sqlite3")))
    assert code == EXIT_ERROR
    assert "rowdiff: " in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert "rowdiff 0." in capsys.readouterr().out


def test_table_is_required(capsys):
    with pytest.raises(SystemExit):
        main(["diff", "--left", "sqlite://a", "--right", "sqlite://b"])


@pytest.mark.parametrize("predicate,drift", [
    ("id % 2 = 0", False),
    ("id % 2 = 1", True),
])
def test_where_scopes_the_command_line_run(sqlite_pair, tmp_path, capsys,
                                           predicate, drift):
    """Row 41 is odd, so only the odd-keyed predicate can see it change."""
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 41, "name", "renamed"))
    code = main(argv(left, right, "--where", predicate, "--format", "json",
                     "--quiet", state=str(tmp_path / "s.sqlite3")))
    payload = json.loads(capsys.readouterr().out)
    assert payload["options"]["where"] == predicate
    assert payload["drift"] is drift
    assert payload["summary"]["rows"]["left_total"] == 60
    assert code == (EXIT_DRIFT if drift else EXIT_OK)


@pytest.mark.parametrize("chunk_size,message", [
    (0, "positive integer"),
    (-1, "positive integer"),
    (1000001, "at most 1000000"),
])
def test_out_of_range_chunk_size_is_a_clean_error(chunk_size, message,
                                                  sqlite_pair, tmp_path,
                                                  capsys):
    left, right = sqlite_pair()
    code = main(argv(left, right, "--quiet", chunk_size=chunk_size,
                     state=str(tmp_path / "s.sqlite3")))
    assert code == EXIT_ERROR
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("chunk_size", [1, 1000000])
def test_the_chunk_size_bounds_are_accepted(chunk_size, sqlite_pair, tmp_path,
                                            capsys):
    left, right = sqlite_pair()
    code = main(argv(left, right, "--quiet", chunk_size=chunk_size,
                     state=str(tmp_path / "s.sqlite3")))
    assert code == EXIT_OK
    assert capsys.readouterr().err == ""


def test_unbounded_chunk_still_reads_as_text(sqlite_pair, tmp_path, capsys):
    """``--chunk-size 1`` leaves the lower bound unset, unlike every other size."""
    left, right = sqlite_pair(
        mutate_right=lambda rows: with_row(rows, 42, "name", "renamed"))
    code = main(argv(left, right, chunk_size=1,
                     state=str(tmp_path / "s.sqlite3")))
    out = capsys.readouterr().out
    assert code == EXIT_DRIFT
    assert "MISMATCHED chunk 0  id -inf .. inf" in out
    assert "drift    0 added, 0 removed, 1 changed" in out
    assert "~ id=42  name 'user-042' -> 'renamed'" in out
