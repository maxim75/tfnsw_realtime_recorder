import io
import json
import logging
import zipfile
from pathlib import Path

import pytest
import requests

from recorder.cities import load_cities
from recorder.routes import MODE_BY_ROUTE_TYPE, RouteInfo

ROUTES_TXT = (
    "route_id,agency_id,route_short_name,route_long_name,route_type\n"
    "L992,1,B,\"Metro, line B\",1\n"
    "L22,1,22,Tram 22,0\n"
    "L100,1,,\"Long only\",3\n"
    "L200,1,,,2\n"
    "L300,1,S1,Rail,109\n"
    "L400,1,X,Weird,9999\n"
)


def _zip(routes_txt: str = ROUTES_TXT, bom: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("stops.txt", "stop_id\n1\n")
        zf.writestr("routes.txt", ("﻿" if bom else "") + routes_txt)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), 7):
            yield self._body[i : i + 7]

    def close(self):
        pass


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def cities():
    return load_cities(env={})


@pytest.fixture
def prague(cities):
    return cities["prague"]


@pytest.fixture
def sydney(cities):
    return cities["sydney"]


def _ok(body=None, last_modified="Wed, 01 Jan 2026 00:00:00 GMT"):
    return FakeResponse(200, _zip() if body is None else body, {"Last-Modified": last_modified})


def test_mode_table():
    assert MODE_BY_ROUTE_TYPE[0] == "tram"
    assert MODE_BY_ROUTE_TYPE[1] == "metro"
    assert MODE_BY_ROUTE_TYPE[2] == "rail"
    assert MODE_BY_ROUTE_TYPE[3] == "bus"
    assert MODE_BY_ROUTE_TYPE[4] == "ferry"
    assert MODE_BY_ROUTE_TYPE[6] == "cable"
    assert MODE_BY_ROUTE_TYPE[11] == "trolleybus"
    assert MODE_BY_ROUTE_TYPE[117] == "rail"
    assert MODE_BY_ROUTE_TYPE[209] == "bus"
    assert MODE_BY_ROUTE_TYPE[405] == "metro"
    assert MODE_BY_ROUTE_TYPE[716] == "bus"
    assert MODE_BY_ROUTE_TYPE[906] == "tram"
    assert MODE_BY_ROUTE_TYPE[1000] == "ferry"
    assert MODE_BY_ROUTE_TYPE[1307] == "cable"


def test_refresh_builds_labels_and_modes(prague, tmp_path):
    session = FakeSession(_ok())
    info = RouteInfo(prague, tmp_path, session=session)
    info.refresh()
    feed = prague.feeds[0]
    assert info.label("L992") == "B"
    assert info.mode(feed, "L992") == "metro"
    assert info.label("L22") == "22"
    assert info.mode(feed, "L22") == "tram"
    assert info.label("L100") == "Long only"
    assert info.mode(feed, "L100") == "bus"
    assert info.label("L200") == "L200"
    assert info.mode(feed, "L200") == "rail"
    assert info.mode(feed, "L300") == "rail"
    assert info.mode(feed, "L400") == "other"
    assert session.calls[0][0] == prague.static_gtfs
    assert session.calls[0][1]["stream"] is True
    assert list(tmp_path.iterdir()) == [tmp_path / "routes.json"]


def test_refresh_without_bom(prague, tmp_path):
    info = RouteInfo(prague, tmp_path, session=FakeSession(_ok(_zip(bom=False))))
    info.refresh()
    assert info.label("L992") == "B"


def test_unknown_route_uses_raw_id_and_feed_mode(prague, tmp_path):
    info = RouteInfo(prague, tmp_path, session=FakeSession())
    feed = prague.feeds[0]
    assert info.label("nope") == "nope"
    assert info.label(None) == ""
    assert info.mode(feed, "nope") == feed.mode
    assert info.mode(feed, None) == feed.mode


def test_pattern_label_without_gtfs(sydney, tmp_path):
    info = RouteInfo(sydney, tmp_path, session=FakeSession())
    assert info.label("2510_333") == "333"
    assert info.label("plain") == "plain"
    assert info.label(None) == ""
    assert info.mode(sydney.feeds[0], "2510_333") == sydney.feeds[0].mode


def test_refresh_noop_without_static_gtfs(sydney, tmp_path):
    session = FakeSession()
    RouteInfo(sydney, tmp_path, session=session).refresh()
    assert session.calls == []
    assert not (tmp_path / "routes.json").exists()


def test_cache_loaded_in_init_and_reused_when_download_fails(prague, tmp_path):
    now = [1_700_000_000.0]
    RouteInfo(prague, tmp_path, session=FakeSession(_ok()), clock=lambda: now[0]).refresh()
    now[0] += 48 * 3600
    session = FakeSession(requests.ConnectionError("boom"))
    info = RouteInfo(prague, tmp_path, session=session, clock=lambda: now[0])
    assert info.label("L992") == "B"
    assert session.calls == []  # nothing fetched at construction
    info.refresh()  # fails, does not raise
    assert len(session.calls) == 1
    assert info.label("L992") == "B"


def test_refresh_failure_logs_warning(prague, tmp_path, caplog):
    info = RouteInfo(prague, tmp_path, session=FakeSession(FakeResponse(500)))
    with caplog.at_level(logging.WARNING):
        info.refresh()
    assert caplog.records
    assert info.label("L992") == "L992"


def test_bad_zip_does_not_raise_or_leave_temp(prague, tmp_path):
    info = RouteInfo(prague, tmp_path, session=FakeSession(_ok(b"not a zip")))
    info.refresh()
    assert list(tmp_path.iterdir()) == []


def test_corrupt_cache_is_ignored(prague, tmp_path):
    (tmp_path / "routes.json").write_text("{nope")
    info = RouteInfo(prague, tmp_path, session=FakeSession())
    assert info.label("x") == "x"


def test_refresh_skipped_when_cache_fresh(prague, tmp_path):
    now = [1_700_000_000.0]
    RouteInfo(prague, tmp_path, session=FakeSession(_ok()), clock=lambda: now[0]).refresh()
    now[0] += 23 * 3600
    session = FakeSession()
    RouteInfo(prague, tmp_path, session=session, clock=lambda: now[0]).refresh()
    assert session.calls == []


def test_refresh_uses_if_modified_since_and_304_updates_check_time(prague, tmp_path):
    now = [1_700_000_000.0]
    RouteInfo(prague, tmp_path, session=FakeSession(_ok()), clock=lambda: now[0]).refresh()
    now[0] += 25 * 3600
    session = FakeSession(FakeResponse(304))
    info = RouteInfo(prague, tmp_path, session=session, clock=lambda: now[0])
    info.refresh()
    headers = session.calls[0][1]["headers"]
    assert headers["If-Modified-Since"] == "Wed, 01 Jan 2026 00:00:00 GMT"
    assert info.label("L992") == "B"
    saved = json.loads((tmp_path / "routes.json").read_text())
    assert saved["checked"] == now[0]
    assert saved["last_modified"] == "Wed, 01 Jan 2026 00:00:00 GMT"
    # checked again within 24 h: no request
    now[0] += 3600
    session2 = FakeSession()
    RouteInfo(prague, tmp_path, session=session2, clock=lambda: now[0]).refresh()
    assert session2.calls == []


def test_refresh_replaces_mapping(prague, tmp_path):
    now = [1_700_000_000.0]
    info = RouteInfo(prague, tmp_path, session=FakeSession(_ok(), _ok(_zip("route_id,route_short_name,route_type\nL1,Z,3\n"))), clock=lambda: now[0])
    info.refresh()
    now[0] += 25 * 3600
    info.refresh()
    assert info.label("L1") == "Z"
    assert info.label("L992") == "L992"
    assert isinstance(Path(tmp_path / "routes.json").read_text(), str)
