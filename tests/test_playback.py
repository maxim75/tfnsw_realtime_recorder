import gzip
import json
from datetime import date, datetime

from recorder.history import History
from recorder.playback import build_day, day_file, parse_args, read_window
from recorder.windows import SYDNEY_TZ

from test_history import FakeS3, csv_bytes, row, s3_key

DAY = date(2026, 10, 16)
W1800 = int(datetime(2026, 10, 16, 18, 0, tzinfo=SYDNEY_TZ).timestamp())
W1810 = W1800 + 600


def fixture_s3():
    return FakeS3({
        s3_key("buses"): gzip.compress(csv_bytes([row("a")])),
        s3_key("buses", label="2026-10-16T18-10+1100"): gzip.compress(csv_bytes([
            row("b", fetched="2026-10-16T18:10:20+11:00", ts="2026-10-16T18:10:10+11:00"),
        ])),
        s3_key("metro"): gzip.compress(csv_bytes([row("m")])),
        s3_key("buses", label="2026-10-17T00-00+1100"): gzip.compress(csv_bytes([row("next-day")])),
    })


def test_build_day_writes_one_file_with_each_recorded_window(tmp_path):
    s3 = fixture_s3()
    hist = History(s3, "bucket", "vehiclepos", tmp_path / "no-local-data")
    path = day_file(tmp_path, DAY)
    windows, reports = build_day(hist, DAY, path)
    assert path == tmp_path / "playback" / "2026-10-16.sqlite"
    assert (windows, reports) == (2, 3)  # 18:00 (buses + metro) and 18:10; empty windows skipped
    first = json.loads(gzip.decompress(read_window(tmp_path, W1800)))
    assert first["start"] == W1800
    assert [r[0] for r in first["feeds"]["buses"]] == ["a"] and [r[0] for r in first["feeds"]["metro"]] == ["m"]
    assert read_window(tmp_path, W1800 + 1200) is None  # day file exists, nothing recorded then
    assert not list((tmp_path / "playback").glob("*.tmp"))


def test_read_window_without_day_file(tmp_path):
    assert read_window(tmp_path, W1800) is None


def test_history_serves_built_day_without_reading_s3(tmp_path):
    hist = History(fixture_s3(), "bucket", "vehiclepos", tmp_path)
    build_day(hist, DAY, day_file(tmp_path, DAY))
    s3 = FakeS3({})  # empty bucket: anything served must come from the day file
    fresh = History(s3, "bucket", "vehiclepos", tmp_path)
    body = json.loads(gzip.decompress(fresh.window(W1810)))
    assert [r[0] for r in body["feeds"]["buses"]] == ["b"]
    assert (s3.list_calls, s3.get_calls) == (0, 0)


def test_history_falls_back_to_s3_for_windows_missing_from_day_file(tmp_path):
    # e.g. today's file was built at 18:15; later windows still come from S3
    hist = History(FakeS3({s3_key("buses"): gzip.compress(csv_bytes([row("a")]))}), "bucket", "vehiclepos", tmp_path)
    build_day(hist, DAY, day_file(tmp_path, DAY))
    later = FakeS3({s3_key("buses", label="2026-10-16T18-10+1100"): gzip.compress(csv_bytes([
        row("b", fetched="2026-10-16T18:10:20+11:00", ts="2026-10-16T18:10:10+11:00"),
    ]))})
    body = json.loads(gzip.decompress(History(later, "bucket", "vehiclepos", tmp_path).window(W1810)))
    assert [r[0] for r in body["feeds"]["buses"]] == ["b"]


def test_parse_args():
    args = parse_args(["--date", "2026-10-16"])
    assert args.date == "2026-10-16" and args.data_dir is None
    assert parse_args([]).date == "today"


def test_build_skips_windows_that_may_still_change(tmp_path):
    hist = History(fixture_s3(), "bucket", "vehiclepos", tmp_path / "no-local-data")
    # built at 18:25: 18:00 has been final since 18:25, 18:10 not until 18:35
    windows, _ = build_day(hist, DAY, day_file(tmp_path, DAY), now=W1800 + 1500)
    assert windows == 1
    assert read_window(tmp_path, W1800) is not None and read_window(tmp_path, W1810) is None


def test_resolve_date():
    from recorder.playback import resolve_date

    now = datetime(2026, 10, 16, 0, 30, tzinfo=SYDNEY_TZ).timestamp()  # 13:30 UTC on the 15th
    assert resolve_date("yesterday", now) == date(2026, 10, 15)
    assert resolve_date("today", now) == date(2026, 10, 16)
    assert resolve_date("2026-01-02", now) == date(2026, 1, 2)
