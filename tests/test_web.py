import pytest
from fastapi.testclient import TestClient

from recorder.config import EnvConfig, SettingsStore
from recorder.recorder import FeedStatus
from recorder.web import Components, create_app

AUTH = ("admin", "secret")


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


@pytest.fixture
def ctx(tmp_path):
    env = EnvConfig(
        tfnsw_api_key="k", admin_user="admin", admin_password="secret",
        data_dir=tmp_path, port=8000, s3=None,
    )
    comps = Components(
        env=env, settings=SettingsStore(tmp_path / "settings.json"),
        recorder=FakeRecorder(), uploader=FakeUploader(),
    )
    with TestClient(create_app(env, comps)) as client:
        yield client, comps


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
    comps.recorder.alive = False
    assert client.get("/healthz").status_code == 500


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
    assert body["uploader"]["pending"] == 2
    assert "secret" not in str(body) and "admin_password" not in str(body)


def test_update_settings(ctx):
    client, comps = ctx
    r = client.put("/api/settings", json={"feeds": ["metro"], "interval": 60}, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"feeds": ["metro"], "interval": 60}
    assert comps.settings.get().feeds == ("metro",)


def test_update_settings_invalid(ctx):
    client, comps = ctx
    r = client.put("/api/settings", json={"feeds": ["nope"], "interval": 60}, auth=AUTH)
    assert r.status_code == 422
    assert "unknown feeds" in r.json()["detail"]
    assert comps.settings.get().interval == 30


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
    env = EnvConfig(
        tfnsw_api_key="k", admin_user="admin", admin_password="secret",
        data_dir=tmp_path, port=8000, s3=None,
    )
    uploader = RecordingUploader()
    comps = Components(
        env=env, settings=SettingsStore(tmp_path / "settings.json"),
        recorder=FailingRecorder(), uploader=uploader,
    )
    with pytest.raises(RuntimeError, match="stop failed"):
        with TestClient(create_app(env, comps)):
            pass
    assert uploader.stop_calls == [20]
