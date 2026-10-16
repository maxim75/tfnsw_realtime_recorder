import csv
import gzip
from datetime import datetime, timezone

import pytest

from recorder.writer import FIELDS, RotatingWriter, recover_open_files


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def row(vehicle="v1"):
    return {f: "" for f in FIELDS} | {"vehicle_id": vehicle}


def read_rows(path):
    with gzip.open(path, "rt", newline="") as f:
        return list(csv.DictReader(f))


def test_appends_to_window_file_with_one_header(tmp_path):
    done = []
    w = RotatingWriter("ferries/MFF", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 11), [row("a")])
    w.write(utc(2026, 10, 16, 1, 12), [row("b"), row("c")])
    assert w.current_path == tmp_path / "open/ferries_MFF/ferries_MFF_2026-10-16T12-10+1100.csv.gz"
    assert w.rows_in_window == 3
    assert [r["vehicle_id"] for r in read_rows(w.current_path)] == ["a", "b", "c"]
    assert done == []


def test_rotates_on_new_window(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 19), [row("a")])
    w.write(utc(2026, 10, 16, 1, 20), [row("b")])
    assert done == [tmp_path / "complete/buses/buses_2026-10-16T12-10+1100.csv.gz"]
    assert [r["vehicle_id"] for r in read_rows(done[0])] == ["a"]
    assert w.current_path.name == "buses_2026-10-16T12-20+1100.csv.gz"
    assert w.rows_in_window == 1


def test_no_rows_in_new_window_still_closes_old_file(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 19), [row()])
    w.write(utc(2026, 10, 16, 1, 21), [])
    assert len(done) == 1
    assert w.current_path is None


def test_rotate_if_ended(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 19), [row()])
    w.rotate_if_ended(utc(2026, 10, 16, 1, 19, 59))
    assert done == []
    w.rotate_if_ended(utc(2026, 10, 16, 1, 20, 0, 500_000))
    assert len(done) == 1


def test_close_is_idempotent(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.close()
    w.write(utc(2026, 10, 16, 1, 15), [row()])
    w.close()
    w.close()
    assert len(done) == 1


def test_part_suffix_when_name_taken(tmp_path):
    done = []
    first = RotatingWriter("buses", tmp_path, done.append)
    first.write(utc(2026, 10, 16, 1, 11), [row()])
    first.close()
    second = RotatingWriter("buses", tmp_path, done.append)
    second.write(utc(2026, 10, 16, 1, 12), [row()])
    assert second.current_path.name == "buses_2026-10-16T12-10+1100_p2.csv.gz"


def test_recover_open_files(tmp_path):
    w = RotatingWriter("buses", tmp_path, lambda p: None)
    w.write(utc(2026, 10, 16, 1, 11), [row()])  # simulate crash: never closed
    recovered = recover_open_files(tmp_path)
    assert recovered == [tmp_path / "complete/buses/buses_2026-10-16T12-10+1100.csv.gz"]
    assert not list((tmp_path / "open").rglob("*.csv.gz"))
    assert len(read_rows(recovered[0])) == 1


def test_failed_first_write_leaves_no_state_and_next_write_has_header(tmp_path, monkeypatch):
    w = RotatingWriter("buses", tmp_path, lambda p: None)
    real = csv.DictWriter.writerows
    calls = []

    def flaky(self, rows):
        if not calls:
            calls.append(1)
            raise OSError(28, "No space left on device")
        return real(self, rows)

    monkeypatch.setattr(csv.DictWriter, "writerows", flaky)
    with pytest.raises(OSError):
        w.write(utc(2026, 10, 16, 1, 11), [row("a")])
    assert w.current_path is None and w.rows_in_window == 0
    assert not list((tmp_path / "open").rglob("*.csv.gz"))  # partial file removed
    w.write(utc(2026, 10, 16, 1, 12), [row("b")])
    assert [r["vehicle_id"] for r in read_rows(w.current_path)] == ["b"]
    assert w.rows_in_window == 1
