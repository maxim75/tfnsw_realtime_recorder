import gzip
import json
from datetime import date, datetime

from recorder.history import History
from recorder.static_export import decode_window, encode_window, export_day
from recorder.windows import SYDNEY_TZ

from test_history import FakeS3, csv_bytes, row, s3_key

DAY = date(2026, 10, 16)
W1800 = int(datetime(2026, 10, 16, 18, 0, tzinfo=SYDNEY_TZ).timestamp())

FEEDS = {
    "buses": [
        ["a", "2510_333", W1800 + 10, 151.20001, -33.80002, 90.0, 5.5],
        ["b", "2510_380", W1800 - 20, 151.1, -33.9, None, None],  # GPS time before window start
        ["a", "2510_333", W1800 + 40, 151.20101, -33.80052, 92.5, 6.1],
    ],
    "metro": [["m", "SMNW_M1", W1800 + 15, 151.0, -33.7, 180.0, 20.0]],
}


def test_compact_window_round_trips():
    data = encode_window(W1800, FEEDS)
    assert data["start"] == W1800
    assert data["feeds"] == ["buses", "metro"]
    decoded = decode_window(data)
    assert sorted(decoded["buses"], key=lambda r: (r[0], r[2])) == [
        ["a", "2510_333", W1800 + 10, 151.20001, -33.80002, 90, 5.5],
        ["a", "2510_333", W1800 + 40, 151.20101, -33.80052, 93, 6.1],  # bearing to whole degrees
        ["b", "2510_380", W1800 - 20, 151.1, -33.9, None, None],
    ]
    assert decoded["metro"] == [["m", "SMNW_M1", W1800 + 15, 151.0, -33.7, 180, 20.0]]


def test_compact_window_is_much_smaller_than_report_json():
    reports = [[f"v{i % 300}", f"2510_{i % 40}", W1800 + i // 300 * 30, 151.2 + i % 300 / 1e3 + i // 300 / 1e4,
                -33.8 - i % 300 / 1e3, float(i % 360), 5.0] for i in range(6000)]
    plain = gzip.compress(json.dumps({"feeds": {"buses": reports}}).encode())
    compact = gzip.compress(json.dumps(encode_window(W1800, {"buses": reports}), separators=(",", ":")).encode())
    assert len(compact) < len(plain) / 2


def test_export_day_writes_index_and_window_files(tmp_path):
    s3 = FakeS3({
        s3_key("buses"): gzip.compress(csv_bytes([row("a"), row("b", ts="2026-10-16T18:00:05+11:00")])),
        s3_key("metro", label="2026-10-16T18-10+1100"): gzip.compress(csv_bytes([
            row("m", fetched="2026-10-16T18:10:20+11:00", ts="2026-10-16T18:10:10+11:00"),
        ])),
    })
    hist = History(s3, "bucket", "vehiclepos", tmp_path / "no-local")
    out = tmp_path / "site" / "data"
    summary = export_day(hist, DAY, out, now=W1800 + 86400)
    index = json.loads((out / "index.json").read_text())
    assert index["date"] == "2026-10-16" and index["timezone"] == "Australia/Sydney"
    assert index["start"] == int(datetime(2026, 10, 16, tzinfo=SYDNEY_TZ).timestamp())
    assert index["end"] == index["start"] + 86400
    assert index["windows"] == {str(W1800): 2, str(W1800 + 600): 1}
    assert summary == {"windows": 2, "reports": 3}
    window = json.loads(gzip.decompress((out / "w" / f"{W1800}.json.gz").read_bytes()))
    assert sorted(r[0] for r in decode_window(window)["buses"]) == ["a", "b"]


def test_export_day_replaces_previous_export(tmp_path):
    out = tmp_path / "data"
    (out / "w").mkdir(parents=True)
    (out / "w" / "123.json.gz").write_bytes(b"old")
    export_day(History(FakeS3({}), "bucket", "vehiclepos", tmp_path / "x"), DAY, out, now=W1800 + 86400)
    assert not (out / "w" / "123.json.gz").exists()
    assert json.loads((out / "index.json").read_text())["windows"] == {}
