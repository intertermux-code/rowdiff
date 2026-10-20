from helpers import diff, with_reading
from rowdiff.engine import MISMATCHED


def change_label(rows):
    return with_reading(rows, (1, 30), "label", "edited")


def change_value(rows):
    return with_reading(rows, (2, 100), "value", 42.0)


def test_identical_composite_key_tables(sqlite_composite, tmp_path):
    left, right = sqlite_composite()
    report = diff(left, right, table="readings", chunk_size=8,
                  state_file=str(tmp_path / "state.sqlite3"))
    assert report["drift"] is False
    assert report["left"]["primary_key"] == ["device_id", "taken_at"]
    assert report["summary"]["chunks"]["planned"] == 5


def test_composite_boundaries_use_both_columns(sqlite_composite, tmp_path):
    left, right = sqlite_composite()
    report = diff(left, right, table="readings", chunk_size=8,
                  state_file=str(tmp_path / "state.sqlite3"))
    ranges = [c["pk_range"] for c in report["chunks"]]
    assert ranges[0]["from"] == [1, 10]
    assert ranges[1] == {"from": [1, 90], "to": [1, 170]}
    assert ranges[-1] == {"from": [2, 130], "to": None}


def test_changed_column_on_a_composite_key(sqlite_composite, tmp_path):
    left, right = sqlite_composite(mutate_right=change_label)
    report = diff(left, right, table="readings", chunk_size=8,
                  state_file=str(tmp_path / "state.sqlite3"))
    assert report["summary"]["rows"]["changed"] == 1
    chunk = [c for c in report["chunks"] if c["status"] == MISMATCHED][0]
    entry = chunk["row_diff"]["changed"][0]
    assert entry["pk"] == [1, 30]
    assert entry["columns"] == [{"name": "label", "left": "label-1-3",
                                 "right": "edited"}]


def test_added_and_removed_on_a_composite_key(sqlite_composite, tmp_path):
    """Each side owns one key the other lacks: one add, one remove."""
    left_only = (2, 999, 5.0, "left only")
    right_only = (2, 998, 5.0, "right only")
    left, right = sqlite_composite(
        mutate_left=lambda rows: list(rows) + [left_only],
        mutate_right=lambda rows: list(rows) + [right_only])
    report = diff(left, right, table="readings", chunk_size=8,
                  state_file=str(tmp_path / "state.sqlite3"))
    rows = report["summary"]["rows"]
    assert rows["added"] == 1 and rows["removed"] == 1
    added = [entry for chunk in report["chunks"]
             if chunk["status"] == MISMATCHED
             for entry in chunk["row_diff"]["added"]]
    removed = [entry for chunk in report["chunks"]
               if chunk["status"] == MISMATCHED
               for entry in chunk["row_diff"]["removed"]]
    assert added == [[2, 998]]
    assert removed == [[2, 999]]


def test_chunks_tile_the_key_space_without_overlap(sqlite_composite, tmp_path):
    """Every row lands in exactly one chunk, however the boundaries are picked."""
    left, right = sqlite_composite()
    for chunk_size in (1, 3, 8, 17, 40, 100):
        state = str(tmp_path / ("s%d.sqlite3" % chunk_size))
        report = diff(left, right, table="readings", chunk_size=chunk_size,
                      state_file=state)
        counts = [c["left"]["row_count"] for c in report["chunks"]]
        assert sum(counts) == 40, chunk_size
        assert report["drift"] is False


def test_key_order_decides_which_chunk_a_row_lives_in(sqlite_composite, tmp_path):
    """Rows are walked in key order, so the chunk index says where a row sits."""
    left, right = sqlite_composite(mutate_right=change_value)
    report = diff(left, right, table="readings", chunk_size=8,
                  state_file=str(tmp_path / "state.sqlite3"))
    mismatched = [c for c in report["chunks"] if c["status"] == MISMATCHED]
    assert len(mismatched) == 1
    assert mismatched[0]["index"] == 3
    assert mismatched[0]["pk_range"]["from"] == [2, 50]
    assert mismatched[0]["row_diff"]["changed"][0]["pk"] == [2, 100]
