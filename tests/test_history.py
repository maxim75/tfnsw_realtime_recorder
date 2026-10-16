import csv
import gzip
import io
import json
from datetime import datetime

import pytest

from recorder.history import History
from recorder.windows import SYDNEY_TZ
from recorder.writer import FIELDS

START = int(datetime(2026, 10, 16, 18, 0, tzinfo=SYDNEY_TZ).timestamp())  # window 18:00-18:10
LABEL = "2026-10-16T18-00+1100"


def csv_bytes(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerows({f: "" for f in FIELDS} | r for r in rows)
    return buf.getvalue().encode()


def row(vehicle, fetched="2026-10-16T18:00:20+11:00", ts="2026-10-16T18:00:10+11:00", lat="-33.8", lon="151.2"):
    return {"fetched_at": fetched, "timestamp": ts, "vehicle_id": vehicle, "route_id": "2510_333",
            "latitude": lat, "longitude": lon, "bearing": "90.0", "speed": "5.55"}


def s3_key(feed_dir, label=LABEL, part=""):
    return f"vehiclepos/feed={feed_dir}/date={label[:10]}/{feed_dir}_{label}{part}.csv.gz"


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.list_calls = 0
        self.get_calls = 0

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self.list_calls += 1
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        return {"Body": io.BytesIO(self.objects[Key])}


def local_file(data_dir, kind, feed_dir, name, data):
    path = data_dir / kind / feed_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def decode(body):
    return json.loads(gzip.decompress(body))


def make(tmp_path, objects=None, now=START + 3600):
    clock = [now]
    s3 = FakeS3(objects or {}) if objects is not None else None
    hist = History(s3, "bucket" if s3 else None, "vehiclepos", tmp_path, clock=lambda: clock[0])
    return hist, s3, clock


def test_window_merges_s3_parts_and_local_files_without_duplicates(tmp_path):
    objects = {
        s3_key("buses"): gzip.compress(csv_bytes([row("a"), row("b", ts="2026-10-16T18:00:05+11:00")])),
        # _p2 repeats a's report (ambiguous upload retry) and adds a later one
        s3_key("buses", part="_p2"): gzip.compress(csv_bytes([
            row("a"), row("a", fetched="2026-10-16T18:04:20+11:00", ts="2026-10-16T18:04:10+11:00"),
        ])),
        s3_key("metro"): gzip.compress(csv_bytes([row("m")])),
        s3_key("buses", label="2026-10-16T18-10+1100"): gzip.compress(csv_bytes([row("other-window")])),
    }
    hist, _, _ = make(tmp_path, objects)
    # not yet uploaded: a finished part in complete/ and the part being written in open/,
    # whose last gzip member is cut off mid-write
    local_file(tmp_path, "complete", "buses", f"buses_{LABEL}_p3.csv.gz",
               gzip.compress(csv_bytes([row("c", fetched="2026-10-16T18:06:20+11:00", ts="2026-10-16T18:06:00+11:00")])))
    good = gzip.compress(csv_bytes([row("d", fetched="2026-10-16T18:08:20+11:00", ts="2026-10-16T18:08:00+11:00")]))
    local_file(tmp_path, "open", "buses", f"buses_{LABEL}_p4.csv.gz", good + gzip.compress(b"x" * 200)[:15])

    body = decode(hist.window(START))
    assert body["start"] == START
    buses = body["feeds"]["buses"]
    assert [(r[0], r[2]) for r in buses] == [
        ("b", START + 5), ("a", START + 10), ("a", START + 250), ("c", START + 360), ("d", START + 480),
    ]
    assert buses[0] == ["b", "2510_333", START + 5, 151.2, -33.8, 90.0, 5.5]
    assert [r[0] for r in body["feeds"]["metro"]] == ["m"]


def test_skips_reports_without_fix_or_stale_gps_time(tmp_path):
    objects = {s3_key("buses"): gzip.compress(csv_bytes([
        row("nofix", lat="0.000000", lon="0.000000"),
        row("stale", ts="2024-09-19T09:02:05+10:00"),
        row("ok"),
    ]))}
    hist, _, _ = make(tmp_path, objects)
    assert [r[0] for r in decode(hist.window(START))["feeds"]["buses"]] == ["ok"]


def test_rejects_start_that_is_not_a_window_boundary(tmp_path):
    hist, _, _ = make(tmp_path, {})
    with pytest.raises(ValueError):
        hist.window(START + 60)


def test_finished_window_is_cached(tmp_path):
    hist, s3, _ = make(tmp_path, {s3_key("buses"): gzip.compress(csv_bytes([row("a")]))})
    first = hist.window(START)
    gets, lists = s3.get_calls, s3.list_calls
    assert hist.window(START) == first
    assert (s3.get_calls, s3.list_calls) == (gets, lists)


def test_current_window_is_refreshed(tmp_path):
    objects = {s3_key("buses"): gzip.compress(csv_bytes([row("a")]))}
    hist, s3, clock = make(tmp_path, objects, now=START + 300)  # window still being recorded
    assert [r[0] for r in decode(hist.window(START))["feeds"]["buses"]] == ["a"]
    objects[s3_key("buses", part="_p2")] = gzip.compress(csv_bytes([
        row("b", fetched="2026-10-16T18:05:20+11:00", ts="2026-10-16T18:05:10+11:00"),
    ]))
    assert [r[0] for r in decode(hist.window(START))["feeds"]["buses"]] == ["a"]  # served from cache
    clock[0] += 60
    assert [r[0] for r in decode(hist.window(START))["feeds"]["buses"]] == ["a", "b"]


def test_local_files_only_without_s3(tmp_path):
    hist, _, _ = make(tmp_path, objects=None)
    local_file(tmp_path, "complete", "sydneytrains", f"sydneytrains_{LABEL}.csv.gz", gzip.compress(csv_bytes([row("t")])))
    body = decode(hist.window(START))
    assert [r[0] for r in body["feeds"]["sydneytrains"]] == ["t"]
    assert "buses" not in body["feeds"]
