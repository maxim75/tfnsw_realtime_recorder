import gzip
import json
from datetime import date, datetime

from recorder.cities import load_cities
from recorder.history import History as _History
from recorder.playback import build_day, day_file, parse_args, read_window
from recorder.windows import SYDNEY_TZ

from test_history import FakeS3, csv_bytes, row, s3_key

SYDNEY = load_cities(env={})["sydney"]


def History(*args, **kwargs):
    return _History(*args, city=SYDNEY, **kwargs)

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
    assert parse_args([]).city == "sydney" and parse_args(["--city", "prague"]).city == "prague"


def test_build_skips_windows_that_may_still_change(tmp_path):
    hist = History(fixture_s3(), "bucket", "vehiclepos", tmp_path / "no-local-data")
    # built at 18:25: 18:00 has been final since 18:25, 18:10 not until 18:35
    windows, _ = build_day(hist, DAY, day_file(tmp_path, DAY), now=W1800 + 1500)
    assert windows == 1
    assert read_window(tmp_path, W1800) is not None and read_window(tmp_path, W1810) is None


def test_resolve_date():
    from recorder.playback import resolve_date

    now = datetime(2026, 10, 16, 0, 30, tzinfo=SYDNEY_TZ).timestamp()  # 13:30 UTC on the 15th
    assert resolve_date("yesterday", SYDNEY_TZ, now) == date(2026, 10, 15)
    assert resolve_date("today", SYDNEY_TZ, now) == date(2026, 10, 16)
    assert resolve_date("2026-01-02", SYDNEY_TZ, now) == date(2026, 1, 2)


def test_day_windows_counts_by_zone_and_dst():
    from zoneinfo import ZoneInfo

    from recorder.playback import day_windows

    prague = ZoneInfo("Europe/Prague")
    assert len(day_windows(date(2026, 10, 16), prague)) == 144
    assert len(day_windows(date(2026, 10, 25), prague)) == 150  # DST ends
    assert len(day_windows(date(2026, 3, 29), prague)) == 138  # DST starts
    assert len(day_windows(date(2026, 4, 5), SYDNEY_TZ)) == 150


def test_resolve_date_uses_the_given_zone():
    from zoneinfo import ZoneInfo

    from recorder.playback import resolve_date

    now = datetime(2026, 10, 16, 23, 30, tzinfo=ZoneInfo("UTC")).timestamp()  # 01:30 on the 17th in Prague
    assert resolve_date("today", ZoneInfo("Europe/Prague"), now) == date(2026, 10, 17)
    assert resolve_date("today", ZoneInfo("America/New_York"), now) == date(2026, 10, 16)


def test_main_builds_into_the_city_folder(tmp_path, monkeypatch):
    import recorder.config as config
    import recorder.uploader as uploader
    from recorder.playback import main

    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for name, value in {"S3_BUCKET": "bucket", "S3_ACCESS_KEY_ID": "id", "S3_SECRET_ACCESS_KEY": "s"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("S3_PREFIX", raising=False)
    monkeypatch.setattr(uploader, "make_s3_client", lambda s3: fixture_s3())
    assert main(["--date", "2026-10-16", "--data-dir", str(tmp_path)]) == 0
    assert read_window(tmp_path / "sydney", W1800) is not None
    assert main(["--city", "prague", "--date", "2026-10-16", "--data-dir", str(tmp_path)]) == 0
    assert (tmp_path / "prague" / "playback" / "2026-10-16.sqlite").is_file()


def test_day_files_live_in_each_citys_folder(tmp_path):
    cities = load_cities(env={})
    sydney, prague = tmp_path / "sydney", tmp_path / "prague"
    assert day_file(sydney, DAY) == tmp_path / "sydney" / "playback" / "2026-10-16.sqlite"
    build_day(History(fixture_s3(), "bucket", "vehiclepos", sydney), DAY, day_file(sydney, DAY))
    assert read_window(sydney, W1800) is not None
    assert read_window(prague, W1800) is None  # other city's folder has no day file
    assert cities["prague"].timezone.key == "Europe/Prague"


def test_prague_day_is_bounded_in_prague_time(tmp_path):
    from zoneinfo import ZoneInfo

    from recorder.playback import day_windows

    tz = ZoneInfo("Europe/Prague")
    starts = day_windows(date(2026, 10, 16), tz)
    assert datetime.fromtimestamp(starts[0], tz=tz).strftime("%Y-%m-%d %H:%M") == "2026-10-16 00:00"
    assert datetime.fromtimestamp(starts[-1], tz=tz).strftime("%H:%M") == "23:50"
    w = int(datetime(2026, 10, 16, 18, 0, tzinfo=tz).timestamp())
    path = day_file(tmp_path, date(2026, 10, 16))
    path.parent.mkdir(parents=True)
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE windows (start INTEGER PRIMARY KEY, reports INTEGER, body BLOB)")
        db.execute("INSERT INTO windows VALUES (?, 1, ?)", (w, b"x"))
    assert read_window(tmp_path, w, tz) == b"x"
