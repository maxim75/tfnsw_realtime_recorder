import gzip
import json
from datetime import date, datetime

from recorder.cities import load_cities
from recorder.history import History as _History
from recorder.static_export import decode_window, encode_window, export_day
from recorder.windows import SYDNEY_TZ

from test_history import FakeS3, csv_bytes, row, s3_key

SYDNEY = load_cities(env={})["sydney"]


def History(*args, **kwargs):
    return _History(*args, city=SYDNEY, **kwargs)

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
        ["a", "2510_333", W1800 + 10, 151.20001, -33.80002, 90, 5.5, "", ""],
        ["a", "2510_333", W1800 + 40, 151.20101, -33.80052, 93, 6.1, "", ""],  # bearing to whole degrees
        ["b", "2510_380", W1800 - 20, 151.1, -33.9, None, None, "", ""],
    ]
    assert decoded["metro"] == [["m", "SMNW_M1", W1800 + 15, 151.0, -33.7, 180, 20.0, "", ""]]


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
    assert index["format"] == 2 and index["city"] == "sydney" and index["name"] == SYDNEY.name
    assert index["center"] == list(SYDNEY.center) and index["zoom"] == SYDNEY.zoom
    assert index["attribution"] == SYDNEY.attribution
    assert index["date"] == "2026-10-16" and index["timezone"] == "Australia/Sydney"
    assert index["start"] == int(datetime(2026, 10, 16, tzinfo=SYDNEY_TZ).timestamp())
    assert index["end"] == index["start"] + 86400
    assert index["windows"] == {str(W1800): 2, str(W1800 + 600): 1}
    assert summary == {"windows": 2, "reports": 3}
    window = json.loads(gzip.decompress((out / "w" / f"{W1800}.json.gz").read_bytes()))
    assert sorted(r[0] for r in decode_window(window)["buses"]) == ["a", "b"]
    assert {v[4] for v in window["vehicles"] if v[0] == 0} == {"bus"}  # feed mode as fallback


def test_export_day_replaces_previous_export(tmp_path):
    out = tmp_path / "data"
    (out / "w").mkdir(parents=True)
    (out / "w" / "123.json.gz").write_bytes(b"old")
    export_day(History(FakeS3({}), "bucket", "vehiclepos", tmp_path / "x"), DAY, out, now=W1800 + 86400)
    assert not (out / "w" / "123.json.gz").exists()
    assert json.loads((out / "index.json").read_text())["windows"] == {}


def test_v2_window_round_trips_label_and_mode():
    labelled = {
        "buses": [r + ["333", "bus"] for r in FEEDS["buses"]],
        "metro": [r + ["M1", "metro"] for r in FEEDS["metro"]],
    }
    data = encode_window(W1800, labelled)
    assert data["vehicles"] == [[0, "a", "2510_333", "333", "bus"], [0, "b", "2510_380", "333", "bus"],
                                [1, "m", "SMNW_M1", "M1", "metro"]]
    decoded = decode_window(data)
    assert decoded["metro"] == [["m", "SMNW_M1", W1800 + 15, 151.0, -33.7, 180, 20.0, "M1", "metro"]]
    assert [r[7:] for r in decoded["buses"]] == [["333", "bus"]] * 3


def test_reports_without_label_and_mode_get_empty_label_and_the_feed_mode():
    data = encode_window(W1800, FEEDS, {"buses": "bus", "metro": "metro"})
    assert data["vehicles"][0] == [0, "a", "2510_333", "", "bus"]
    assert data["vehicles"][-1] == [1, "m", "SMNW_M1", "", "metro"]
    assert encode_window(W1800, FEEDS)["vehicles"][0][3:] == ["", ""]


def test_decode_reads_v1_windows():
    v1 = encode_window(W1800, FEEDS)
    v1["vehicles"] = [v[:3] for v in v1["vehicles"]]
    assert decode_window(v1)["metro"] == [["m", "SMNW_M1", W1800 + 15, 151.0, -33.7, 180, 20.0]]


def test_parse_args_city_defaults_to_sydney():
    from recorder.static_export import parse_args

    assert parse_args(["--out", "x"]).city == "sydney"
    assert parse_args(["--out", "x", "--city", "prague"]).city == "prague"


def test_cities_json_merge_keeps_other_cities_in_config_order(tmp_path):
    from recorder.static_export import update_cities_index

    cities = load_cities(env={})
    order = list(cities)
    prague, sydney = cities["prague"], cities["sydney"]
    update_cities_index(tmp_path, prague, date(2026, 10, 15), order)
    path = tmp_path / "cities.json"
    extra = json.loads(path.read_text())
    extra["cities"].append({"id": "zzz", "name": "Elsewhere"})
    path.write_text(json.dumps(extra))
    update_cities_index(tmp_path, sydney, DAY, order)
    update_cities_index(tmp_path, prague, date(2026, 10, 16), order)
    data = json.loads(path.read_text())
    assert data["format"] == 2
    by_id = {c["id"]: c for c in data["cities"]}
    assert by_id["prague"] == {"id": "prague", "name": prague.name, "timezone": "Europe/Prague",
                               "center": list(prague.center), "zoom": prague.zoom,
                               "attribution": prague.attribution, "date": "2026-10-16"}
    assert by_id["sydney"]["date"] == "2026-10-16" and by_id["zzz"]["name"] == "Elsewhere"
    assert data["cities"][-1]["id"] == "zzz"


def test_main_exports_into_the_city_folder_and_leaves_other_cities_alone(tmp_path, monkeypatch):
    import recorder.config as config
    import recorder.static_export as static_export
    import recorder.uploader as uploader

    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for name, value in {"S3_BUCKET": "bucket", "S3_ACCESS_KEY_ID": "id", "S3_SECRET_ACCESS_KEY": "s"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("S3_PREFIX", raising=False)
    monkeypatch.setattr(uploader, "make_s3_client", lambda s3: FakeS3({}))
    out = tmp_path / "data"
    (out / "sydney" / "w").mkdir(parents=True)
    (out / "sydney" / "w" / "1.json.gz").write_bytes(b"sydney")
    (out / "w").mkdir()
    (out / "w" / "2.json.gz").write_bytes(b"flat")  # old flat layout stays untouched
    args = ["--city", "prague", "--date", "2026-10-15", "--out", str(out), "--data-dir", str(tmp_path / "rec")]
    assert static_export.main(args) == 0
    assert json.loads((out / "prague" / "index.json").read_text())["city"] == "prague"
    assert [c["id"] for c in json.loads((out / "cities.json").read_text())["cities"]] == ["prague"]
    assert (out / "sydney" / "w" / "1.json.gz").exists() and (out / "w" / "2.json.gz").exists()


def test_prague_export_uses_prague_day_bounds(tmp_path):
    prague = load_cities(env={})["prague"]
    hist = _History(FakeS3({}), "bucket", "vehiclepos/city=prague", tmp_path / "x", city=prague)
    export_day(hist, date(2026, 10, 25), tmp_path / "out", now=2e9)  # DST ends: 25 hours
    index = json.loads((tmp_path / "out" / "index.json").read_text())
    assert index["timezone"] == "Europe/Prague" and index["city"] == "prague"
    assert index["end"] - index["start"] == 25 * 3600


def test_main_labels_come_from_refreshed_routes(tmp_path, monkeypatch):
    import recorder.config as config
    import recorder.history as history
    import recorder.static_export as static_export
    import recorder.uploader as uploader
    from test_routes import FakeSession, _ok

    session = FakeSession(_ok())
    monkeypatch.setattr("recorder.routes.requests.get", session.get)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for name, value in {"S3_BUCKET": "bucket", "S3_ACCESS_KEY_ID": "id", "S3_SECRET_ACCESS_KEY": "s"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(uploader, "make_s3_client", lambda s3: FakeS3({}))
    seen = []

    class Spy(history.History):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs["routes"])
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(history, "History", Spy)
    args = ["--city", "prague", "--date", "2026-10-15", "--out", str(tmp_path / "o"), "--data-dir", str(tmp_path / "rec")]
    assert static_export.main(args) == 0
    assert len(session.calls) == 1 and seen[0].label("L992") == "B"


def test_cities_index_rejects_a_corrupt_file_instead_of_dropping_cities(tmp_path):
    import pytest
    from recorder.static_export import update_cities_index

    sydney = load_cities(env={})["sydney"]
    path = tmp_path / "cities.json"
    for bad in ("{not json", "[]", '{"cities": 3}'):
        path.write_text(bad)
        with pytest.raises(ValueError, match="cities.json"):
            update_cities_index(tmp_path, sydney, DAY, ["sydney"])
        assert path.read_text() == bad  # untouched


def test_cities_index_skips_entries_without_an_id(tmp_path, caplog):
    from recorder.static_export import update_cities_index

    sydney = load_cities(env={})["sydney"]
    (tmp_path / "cities.json").write_text(json.dumps({"cities": [{"name": "nameless"}, {"id": "zzz", "name": "Z"}]}))
    update_cities_index(tmp_path, sydney, DAY, ["sydney"])
    ids = [c["id"] for c in json.loads((tmp_path / "cities.json").read_text())["cities"]]
    assert ids == ["sydney", "zzz"] and "without an id" in caplog.text
