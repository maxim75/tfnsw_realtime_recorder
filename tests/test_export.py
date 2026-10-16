import csv
import gzip
import io
from datetime import datetime, timedelta

import pytest

from recorder.export import default_output, export, list_keys, parse_args, time_range
from recorder.windows import SYDNEY_TZ
from recorder.writer import FIELDS


def syd(*args):
    return datetime(*args, tzinfo=SYDNEY_TZ)


def gz_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerows({f: "" for f in FIELDS} | r for r in rows)
    return gzip.compress(buf.getvalue().encode())


def row(vehicle, fetched, ts=None, route="2510_333"):
    return {
        "fetched_at": fetched,
        "timestamp": ts if ts is not None else fetched,
        "vehicle_id": vehicle,
        "route_id": route,
        "latitude": "-33.8",
        "longitude": "151.2",
    }


def key(feed_dir, label, part=""):
    return f"vehiclepos/feed={feed_dir}/date={label[:10]}/{feed_dir}_{label}{part}.csv.gz"


class FakeS3:
    def __init__(self, objects, page_size=2):
        self.objects = objects
        self.page_size = page_size

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        resp = {"Contents": [{"Key": k} for k in page], "IsTruncated": start + self.page_size < len(keys)}
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def test_list_keys_selects_overlapping_windows_across_days_and_pages():
    objects = {
        key("buses", "2026-10-16T23-40+1100"): b"",
        key("buses", "2026-10-16T23-50+1100"): b"",
        key("buses", "2026-10-16T23-50+1100", "_p2"): b"",
        key("buses", "2026-10-17T00-00+1100"): b"",
        key("buses", "2026-10-17T00-10+1100"): b"",
        key("metro", "2026-10-16T23-50+1100"): b"",
    }
    keys = list_keys(FakeS3(objects), "b", "vehiclepos", "buses", syd(2026, 10, 16, 23, 55), syd(2026, 10, 17, 0, 10))
    assert [k.rsplit("/", 1)[1] for k in keys] == [
        "buses_2026-10-16T23-50+1100.csv.gz",
        "buses_2026-10-16T23-50+1100_p2.csv.gz",
        "buses_2026-10-17T00-00+1100.csv.gz",
    ]


def test_export_filters_dedupes_and_adds_time_and_feed():
    w1 = key("buses", "2026-10-16T18-00+1100")
    w1p2 = key("buses", "2026-10-16T18-00+1100", "_p2")
    w2 = key("buses", "2026-10-16T18-10+1100")
    objects = {
        w1: gz_csv([
            row("a", "2026-10-16T18:00:10+11:00", ts="2026-10-16T07:00:05+00:00"),
            row("b", "2026-10-16T18:00:10+11:00", route="2510_999"),
        ]),
        # _p2 repeats vehicle a's report (ambiguous retry) and adds a new one
        w1p2: gz_csv([
            row("a", "2026-10-16T18:04:10+11:00", ts="2026-10-16T07:00:05+00:00"),
            row("a", "2026-10-16T18:04:40+11:00"),
        ]),
        w2: gz_csv([row("a", "2026-10-16T18:10:10+11:00")]),  # outside --to
    }
    out = io.StringIO()
    files, rows = export(
        FakeS3(objects), "b", "vehiclepos", ["buses"],
        syd(2026, 10, 16, 18, 0), syd(2026, 10, 16, 18, 10), {"333"}, out,
    )
    result = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert (files, rows) == (2, 2)
    assert list(result[0]) == ["feed", "time", *FIELDS]
    assert [(r["feed"], r["vehicle_id"], r["time"]) for r in result] == [
        ("buses", "a", "2026-10-16 18:00:05"),  # GPS time, in Sydney local time
        ("buses", "a", "2026-10-16 18:04:40"),
    ]


def test_route_filter_accepts_full_route_id():
    objects = {key("buses", "2026-10-16T18-00+1100"): gz_csv([row("a", "2026-10-16T18:00:10+11:00")])}
    out = io.StringIO()
    _, rows = export(FakeS3(objects), "b", "vehiclepos", ["buses"],
                     syd(2026, 10, 16), syd(2026, 10, 17), {"2510_333"}, out)
    assert rows == 1


def test_time_range_defaults_to_whole_day_and_accepts_24_00():
    assert time_range("2026-10-16", None, None) == (syd(2026, 10, 16), syd(2026, 10, 17))
    assert time_range("2026-10-16", "18:00", "24:00") == (syd(2026, 10, 16, 18), syd(2026, 10, 17))
    with pytest.raises(ValueError):
        time_range("2026-10-16", "19:00", "18:00")


def test_parse_args_and_default_output_name():
    args = parse_args(["--date", "2026-10-16", "--feed", "buses", "--route", "333", "--from", "18:00", "--to", "19:00"])
    assert args.feed == ["buses"] and args.route == ["333"]
    assert default_output(args) == "buses_333_2026-10-16_1800-1900.csv"
    args = parse_args(["--date", "2026-10-16", "--feed", "all"])
    assert default_output(args) == "all_2026-10-16.csv"


def test_parse_args_rejects_unknown_feed():
    with pytest.raises(SystemExit):
        parse_args(["--feed", "nope"])


def test_repeated_report_across_windows_is_written_once():
    objects = {
        key("buses", "2026-10-16T18-00+1100"): gz_csv([row("a", "2026-10-16T18:09:50+11:00", ts="2026-10-16T18:09:40+11:00")]),
        key("buses", "2026-10-16T18-10+1100"): gz_csv([row("a", "2026-10-16T18:10:20+11:00", ts="2026-10-16T18:09:40+11:00")]),
    }
    out = io.StringIO()
    _, rows = export(FakeS3(objects), "b", "vehiclepos", ["buses"],
                     syd(2026, 10, 16), syd(2026, 10, 17), set(), out)
    assert rows == 1


def test_stale_gps_reports_are_dropped():
    objects = {key("buses", "2026-10-16T18-00+1100"): gz_csv([
        row("old", "2026-10-16T18:00:10+11:00", ts="2024-09-19T09:02:05+10:00"),
        row("late", "2026-10-16T18:00:10+11:00", ts="2026-10-16T17:40:00+11:00"),
        row("ok", "2026-10-16T18:00:10+11:00", ts="2026-10-16T17:50:00+11:00"),
    ])}
    out = io.StringIO()
    _, rows = export(FakeS3(objects), "b", "vehiclepos", ["buses"],
                     syd(2026, 10, 16), syd(2026, 10, 17), set(), out, max_age=timedelta(minutes=15))
    assert [r["vehicle_id"] for r in csv.DictReader(io.StringIO(out.getvalue()))] == ["ok"]
    assert rows == 1


def test_max_age_option():
    assert parse_args([]).max_age == 15
    assert parse_args(["--max-age", "0"]).max_age == 0  # 0 keeps every report


def test_city_option_defaults_to_sydney_and_checks_the_city_feeds():
    assert parse_args([]).city == "sydney"
    args = parse_args(["--city", "prague", "--feed", "all", "--date", "2026-10-16"])
    assert args.city == "prague" and args.feed == ["all"]
    assert args.prefix == "vehiclepos/city=prague" and args.tz.key == "Europe/Prague"
    assert default_output(args) == "prague_all_2026-10-16.csv"
    with pytest.raises(SystemExit):
        parse_args(["--city", "prague", "--feed", "buses"])
    with pytest.raises(SystemExit):
        parse_args(["--city", "atlantis"])


def test_prague_export_uses_prague_time():
    from zoneinfo import ZoneInfo

    prague = ZoneInfo("Europe/Prague")
    start, end = time_range("2026-10-16", "12:00", "13:00", prague)
    assert start == datetime(2026, 10, 16, 12, tzinfo=prague)
    k = "vehiclepos/city=prague/feed=all/date=2026-10-16/all_2026-10-16T12-10+0200.csv.gz"
    objects = {k: gz_csv([row("bus-7", "2026-10-16T12:10:20+02:00", route="L136")])}
    out = io.StringIO()
    files, rows = export(FakeS3(objects), "b", "vehiclepos/city=prague", ["all"], start, end, set(), out, tz=prague)
    assert (files, rows) == (1, 1)
    assert list(csv.DictReader(io.StringIO(out.getvalue())))[0]["time"] == "2026-10-16 12:10:20"


def test_route_matches_line_label():
    from recorder.export import route_matches

    label = {"L991": "B"}.get
    assert route_matches("L991", {"B"}, lambda r: label(r, r))
    assert not route_matches("L992", {"B"}, lambda r: label(r, r))
    assert route_matches("2510_333", {"333"})  # id forms still work without a label


def test_export_route_filter_uses_label():
    k = "vehiclepos/city=prague/feed=all/date=2026-10-16/all_2026-10-16T12-10+0200.csv.gz"
    objects = {k: gz_csv([row("x", "2026-10-16T12:10:20+02:00", route="L991"),
                          row("y", "2026-10-16T12:10:20+02:00", route="L22")])}
    from zoneinfo import ZoneInfo
    prague = ZoneInfo("Europe/Prague")
    start, end = time_range("2026-10-16", "12:00", "13:00", prague)
    out = io.StringIO()
    _, rows = export(FakeS3(objects), "b", "vehiclepos/city=prague", ["all"], start, end, {"B"}, out,
                     tz=prague, label=lambda r: "B" if r == "L991" else r)
    assert rows == 1
