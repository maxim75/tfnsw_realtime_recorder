import gzip
import io
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from recorder.cities import load_cities
from recorder.config import EnvConfig, S3Config, SettingsStore
from recorder.gtfsrt import Position
from recorder.history import History
from recorder.live import LiveHub
from recorder.recorder import FeedStatus, Recorder
from recorder.routes import RouteInfo
from recorder.web import CityComponents, Components, build_components, create_app

AUTH = ("admin", "secret")
CITIES = load_cities(env={"TFNSW_API_KEY": "k"})


class FakeRecorder:
    def __init__(self):
        self.alive = False
        self.started_at = None

    def start(self):
        self.alive = True

    def stop(self, timeout=None):
        self.alive = False

    def status(self):
        return {"buses": FeedStatus(vehicles=5, last_error="boom")}


class FakeUploader:
    alive = True

    def start(self):
        pass

    def stop(self, timeout=None):
        pass

    def status(self):
        return {"s3_configured": False, "pending": 2}


def make_env(tmp_path, s3=None):
    return EnvConfig(admin_user="admin", admin_password="secret", data_dir=tmp_path, port=8729, s3=s3)


def city_components(city, data_dir, recorder):
    cdir = data_dir / city.id
    routes = RouteInfo(city, cdir)
    return CityComponents(
        city=city, recorder=recorder, live=LiveHub(city, routes),
        history=History(None, None, city.s3_prefix, cdir, city=city, routes=routes), routes=routes,
    )


def make_components(tmp_path, uploader=None, recorders=None):
    recorders = recorders or {}
    return Components(
        env=make_env(tmp_path), settings=SettingsStore(tmp_path / "settings.json", CITIES),
        uploader=uploader or FakeUploader(),
        cities={cid: city_components(c, tmp_path, recorders.get(cid, FakeRecorder())) for cid, c in CITIES.items()},
    )


@pytest.fixture
def ctx(tmp_path):
    comps = make_components(tmp_path)
    with TestClient(create_app(comps.env, comps)) as client:
        yield client, comps


def sydney(comps):
    return comps.cities["sydney"]


def test_requires_auth(ctx):
    client, _ = ctx
    for path in ("/", "/api/status"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Basic"
    assert client.get("/api/status", auth=("admin", "wrong")).status_code == 401
    assert client.put("/api/settings", json={"feeds": [], "interval": 30}).status_code == 401


def test_healthz_is_public_and_reflects_threads(ctx):
    client, comps = ctx
    assert client.get("/healthz").status_code == 200
    sydney(comps).recorder.alive = False
    assert client.get("/healthz").status_code == 500


def test_healthz_unhealthy_when_any_city_recorder_dies(ctx):
    client, comps = ctx
    comps.cities["prague"].recorder.alive = False
    assert client.get("/healthz").status_code == 500


def test_healthz_unhealthy_when_uploader_dies(ctx):
    client, comps = ctx
    comps.uploader.alive = False
    assert client.get("/healthz").status_code == 500


def test_healthz_ignores_disabled_city(tmp_path):
    comps = make_components(tmp_path)
    sydney(comps).recorder = None  # e.g. no TFNSW_API_KEY
    with TestClient(create_app(comps.env, comps)) as client:
        assert client.get("/healthz").status_code == 200
        body = client.get("/api/status", auth=AUTH).json()
    assert body["recorder"]["alive"] is False and body["recorder"]["started_at"] is None


def test_index_page(ctx):
    client, _ = ctx
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "Vehicle position recorder" in r.text


def test_status(ctx):
    client, _ = ctx
    body = client.get("/api/status", auth=AUTH).json()
    assert body["recorder"]["alive"] is True
    assert body["settings"]["interval"] == 30
    assert body["quota"] == {"requests_per_day": 28800, "daily_quota": 60000}
    feeds = {f["feed"]: f for f in body["feeds"]}
    assert len(feeds) == 23
    assert feeds["buses"]["enabled"] is True and feeds["buses"]["vehicles"] == 5
    assert feeds["lightrail/newcastle"]["enabled"] is False
    assert set(feeds["lightrail/newcastle"]) == set(feeds["buses"])  # same keys for every feed
    assert body["uploader"]["pending"] == 2
    assert "secret" not in str(body) and "admin_password" not in str(body)


def test_status_includes_api_version_per_feed(ctx):
    client, _ = ctx
    feeds = {f["feed"]: f for f in client.get("/api/status", auth=AUTH).json()["feeds"]}
    assert feeds["metro"]["version"] == "v2" and feeds["buses"]["version"] == "v1"
    assert "stale" in feeds["buses"] and "data_age_s" in feeds["buses"]


def test_update_settings(ctx):
    client, comps = ctx
    r = client.put("/api/settings", json={"feeds": ["metro"], "interval": 60}, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"feeds": ["metro"], "interval": 60}
    assert comps.settings.get("sydney").feeds == ("metro",)
    assert comps.settings.get("prague").feeds == ("all",)


def test_update_settings_invalid(ctx):
    client, comps = ctx
    r = client.put("/api/settings", json={"feeds": ["nope"], "interval": 60}, auth=AUTH)
    assert r.status_code == 422
    assert "unknown feeds" in r.json()["detail"]
    assert comps.settings.get("sydney").interval == 30


def test_every_route_except_healthz_requires_auth(ctx):
    client, _ = ctx
    checked = []
    for route in client.app.routes:
        path = getattr(route, "path", None)
        if not path or path == "/healthz":
            continue
        methods = getattr(route, "methods", None) or {"GET"}
        for method in methods:
            r = client.request(method, path)
            assert r.status_code in (401, 404, 405), f"{method} {path} -> {r.status_code}"
            checked.append((method, path))
    assert ("GET", "/") in checked and ("GET", "/api/status") in checked


def test_openapi_schema_is_not_served(ctx):
    client, _ = ctx
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/openapi.json", auth=AUTH).status_code == 404


class RecordingUploader(FakeUploader):
    def __init__(self):
        self.stop_calls = []

    def stop(self, timeout=None):
        self.stop_calls.append(timeout)


class FailingRecorder(FakeRecorder):
    def stop(self, timeout=None):
        raise RuntimeError("stop failed")


def test_uploader_stopped_even_if_recorder_stop_raises(tmp_path):
    uploader = RecordingUploader()
    prague = FakeRecorder()
    comps = make_components(tmp_path, uploader, {"sydney": FailingRecorder(), "prague": prague})
    with pytest.raises(RuntimeError, match="stop failed"):
        with TestClient(create_app(comps.env, comps)):
            assert prague.alive
    assert uploader.stop_calls == [15]
    assert not prague.alive  # the other city's recorder is still stopped


def _publish(comps, vehicle="a"):
    now = datetime.now(tz=timezone.utc)
    sydney(comps).live.publish("buses", now, [Position(
        vehicle_id=vehicle, trip_id="t", route_id="2510_333", latitude=-33.8, longitude=151.2,
        bearing=90.0, speed=5.0, timestamp=now,
    )])


def test_map_page(ctx):
    client, _ = ctx
    r = client.get("/map", auth=AUTH)
    assert r.status_code == 200
    assert "maplibre-gl@4.7.1" in r.text and "deck.gl@9.1.14" in r.text


def test_live_snapshot(ctx):
    client, comps = ctx
    _publish(comps)
    body = client.get("/api/live", auth=AUTH).json()
    assert body["seq"] == 1
    assert body["feeds"]["buses"][0][:2] == ["a", "2510_333"]
    assert body["feeds"]["buses"][0][7:] == ["333", "bus"]


def test_live_stream_sends_batches_after_since(ctx):
    client, comps = ctx
    _publish(comps, "a")
    _publish(comps, "b")
    sydney(comps).live.close()  # ends the stream after the backlog, like shutdown
    with client.stream("GET", "/api/live/stream?since=1", auth=AUTH) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert "id: 2\n" in text and '"feed":"buses"' in text and '"b"' in text
    assert "id: 1\n" not in text


def test_live_stream_resumes_from_last_event_id(ctx):
    client, comps = ctx
    _publish(comps, "a")
    _publish(comps, "b")
    sydney(comps).live.close()
    with client.stream("GET", "/api/live/stream", auth=AUTH, headers={"Last-Event-ID": "1"}) as r:
        text = "".join(r.iter_text())
    assert "id: 2\n" in text and "id: 1\n" not in text


def test_live_stream_after_server_restart_resends_buffer(ctx):
    # The browser reconnects with the Last-Event-ID of the previous server process,
    # which is higher than anything this process has sent.
    client, comps = ctx
    _publish(comps, "a")
    sydney(comps).live.close()
    with client.stream("GET", "/api/live/stream", auth=AUTH, headers={"Last-Event-ID": "5000"}) as r:
        text = "".join(r.iter_text())
    assert "id: 1\n" in text and '"a"' in text


def test_history_window_endpoint(ctx, tmp_path):
    client, comps = ctx
    path = tmp_path / "sydney" / "complete" / "buses" / "buses_2026-10-16T18-00+1100.csv.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(
        b"fetched_at,timestamp,vehicle_id,trip_id,route_id,latitude,longitude,bearing,speed\n"
        b"2026-10-16T18:00:20+11:00,2026-10-16T18:00:10+11:00,a,t,2510_333,-33.8,151.2,90.0,5.0\n"
    ))
    start = 1792134000  # 2026-10-16 18:00 AEDT
    r = client.get(f"/api/history/window?start={start}", auth=AUTH)
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert r.json()["feeds"]["buses"][0][:3] == ["a", "2510_333", start + 10]
    assert r.json()["feeds"]["buses"][0][7:] == ["333", "bus"]
    assert client.get(f"/api/history/window?start={start + 60}", auth=AUTH).status_code == 422


def test_history_unavailable_without_component(ctx):
    client, comps = ctx
    sydney(comps).history = None
    assert client.get("/api/history/window?start=1792134000", auth=AUTH).status_code == 503


# -- build_components -------------------------------------------------------------

class FakeGtfsRtClient:
    instances = []

    def __init__(self):
        self.calls = []
        FakeGtfsRtClient.instances.append(self)

    def fetch(self, url, headers):
        self.calls.append(url)
        return []


class FakeS3:
    """Enough of boto3's S3 client for the uploader and history."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError

        if Key in self.objects:
            return {}
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        with open(Filename, "rb") as f:
            self.objects[Key] = f.read()


class NoNetworkSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        raise OSError("no network in tests")


S3 = S3Config(endpoint_url=None, region="us-east-1", bucket="b", access_key_id="id", secret_access_key="s")
CSV_HEADER = b"fetched_at,timestamp,vehicle_id,trip_id,route_id,latitude,longitude,bearing,speed\n"
W1800 = 1792134000  # 2026-10-16 18:00 AEDT


def build(tmp_path, cities, s3=None, session=None):
    FakeGtfsRtClient.instances = []
    return build_components(
        make_env(tmp_path, S3 if s3 is not None else None), cities=cities, client_factory=FakeGtfsRtClient,
        s3_client_factory=lambda _: s3, route_session=session or NoNetworkSession(),
    )


def test_build_components_records_both_cities_with_own_clients(tmp_path):
    comps = build(tmp_path, CITIES)
    assert list(comps.cities) == ["sydney", "prague"]
    recorders = [cc.recorder for cc in comps.cities.values()]
    assert all(isinstance(r, Recorder) for r in recorders)
    assert [r.city.id for r in recorders] == ["sydney", "prague"]
    assert len(FakeGtfsRtClient.instances) == 2
    assert recorders[0]._client is not recorders[1]._client  # one session per thread
    assert recorders[1]._data_dir == tmp_path / "prague"
    assert [r._settings.get() for r in recorders] == [comps.settings.get("sydney"), comps.settings.get("prague")]
    assert comps.uploader.status()["prefixes"] == {"sydney": "vehiclepos", "prague": "vehiclepos/city=prague"}
    assert comps.cities["prague"].history.tz.key == "Europe/Prague"


def test_build_components_with_sydney_disabled(tmp_path):
    key = "vehiclepos/feed=buses/date=2026-10-16/buses_2026-10-16T18-00+1100.csv.gz"
    s3 = FakeS3({key: gzip.compress(
        CSV_HEADER + b"2026-10-16T18:00:20+11:00,2026-10-16T18:00:10+11:00,a,t,2510_333,-33.8,151.2,90.0,5.0\n"
    )})
    cities = load_cities(env={})  # no TFNSW_API_KEY
    assert cities["sydney"].disabled_reason
    comps = build(tmp_path, cities, s3=s3)
    assert comps.cities["sydney"].recorder is None
    assert isinstance(comps.cities["prague"].recorder, Recorder)
    with TestClient(create_app(comps.env, comps)) as client:
        assert client.get("/healthz").status_code == 200
        r = client.get(f"/api/history/window?start={W1800}", auth=AUTH)
        assert r.status_code == 200
        assert r.json()["feeds"]["buses"] == [["a", "2510_333", W1800 + 10, 151.2, -33.8, 90.0, 5.0, "333", "bus"]]
        assert comps.cities["prague"].recorder.alive
    assert not comps.cities["prague"].recorder.alive


def test_build_components_migrates_legacy_layout_before_recovery(tmp_path):
    legacy = tmp_path / "open" / "buses" / "buses_2026-10-16T12-10+1100.csv.gz"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(gzip.compress(CSV_HEADER))
    s3 = FakeS3()
    comps = build(tmp_path, CITIES, s3=s3)
    recovered = tmp_path / "sydney" / "complete" / "buses" / legacy.name
    assert recovered.is_file() and not (tmp_path / "open").exists()
    comps.uploader.start()  # startup scan of DATA_DIR/*/complete
    comps.uploader.stop(timeout=5)
    assert f"vehiclepos/feed=buses/date=2026-10-16/{legacy.name}" in s3.objects
    assert not recovered.exists()


def test_build_components_labels_prague_lines_from_cached_routes(tmp_path):
    (tmp_path / "prague").mkdir()
    (tmp_path / "prague" / "routes.json").write_text(json.dumps(
        {"checked": time.time(), "last_modified": None, "routes": {"L992": ["B", "metro"]}}
    ))
    session = NoNetworkSession()
    comps = build(tmp_path, CITIES, session=session)
    now = datetime.now(tz=timezone.utc)
    comps.cities["prague"].live.publish("all", now, [Position(
        vehicle_id="metro-B-25-25", trip_id="t", route_id="L992", latitude=50.08, longitude=14.44,
        bearing=None, speed=None, timestamp=now,
    )])
    assert comps.cities["prague"].live.snapshot()["feeds"]["all"][0][7:] == ["B", "metro"]
    assert [r._city_id for r in comps.refreshers] == ["prague"]  # Sydney has no static GTFS
    with TestClient(create_app(comps.env, comps)):
        assert comps.refreshers[0].alive
    assert not comps.refreshers[0].alive
    assert session.calls == []  # cache is fresh: no download
