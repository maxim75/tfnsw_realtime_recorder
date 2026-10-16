from datetime import datetime, timezone

import pytest
import requests
from google.transit import gtfs_realtime_pb2

from recorder.gtfsrt import FetchError, GtfsRtClient


def feed_bytes(entities):
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    for i, e in enumerate(entities):
        ent = msg.entity.add()
        ent.id = str(i)
        if e is None:
            continue
        v = ent.vehicle
        if "id" in e:
            v.vehicle.id = e["id"]
        if "trip" in e:
            v.trip.trip_id = e["trip"]
        if "route" in e:
            v.trip.route_id = e["route"]
        if "ts" in e:
            v.timestamp = e["ts"]
        if "lat" in e:
            v.position.latitude = e["lat"]
            v.position.longitude = e["lon"]
            if "bearing" in e:
                v.position.bearing = e["bearing"]
            if "speed" in e:
                v.position.speed = e["speed"]
    return msg.SerializeToString()


class FakeResponse:
    def __init__(self, status=200, content=b""):
        self.status_code = status
        self.content = content


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        if self.exc:
            raise self.exc
        return self.response


def client(response=None, exc=None, timeout=8):
    session = FakeSession(response, exc)
    return GtfsRtClient(timeout=timeout, session=session), session


def test_parses_positions():
    body = feed_bytes([
        {"id": "v1", "trip": "t1", "route": "r1", "ts": 1760000000,
         "lat": -33.5, "lon": 151.25, "bearing": 90.0, "speed": 4.5},
    ])
    c, _ = client(FakeResponse(200, body))
    [p] = c.fetch("http://x", {})
    assert (p.vehicle_id, p.trip_id, p.route_id) == ("v1", "t1", "r1")
    assert (p.latitude, p.longitude) == (-33.5, 151.25)
    assert (p.bearing, p.speed) == (90.0, 4.5)
    assert p.timestamp == datetime.fromtimestamp(1760000000, tz=timezone.utc)
    assert p.timestamp.utcoffset().total_seconds() == 0


def test_missing_optional_fields_are_none():
    body = feed_bytes([{"lat": 50.0, "lon": 14.0}])
    c, _ = client(FakeResponse(200, body))
    [p] = c.fetch("http://x", {})
    assert p.vehicle_id is None and p.trip_id is None and p.route_id is None
    assert p.bearing is None and p.speed is None and p.timestamp is None


def test_skips_entities_without_vehicle_or_position():
    body = feed_bytes([None, {"id": "nopos"}, {"id": "ok", "lat": 1.0, "lon": 2.0}])
    c, _ = client(FakeResponse(200, body))
    assert [p.vehicle_id for p in c.fetch("http://x", {})] == ["ok"]


@pytest.mark.parametrize("status", [404, 429, 500])
def test_http_error_status(status):
    c, _ = client(FakeResponse(status, b"nope"))
    with pytest.raises(FetchError, match=str(status)):
        c.fetch("http://x", {})


@pytest.mark.parametrize("exc", [requests.Timeout("t"), requests.ConnectionError("c")])
def test_request_exceptions_wrapped(exc):
    c, _ = client(exc=exc)
    with pytest.raises(FetchError):
        c.fetch("http://x", {})


def test_garbage_body():
    c, _ = client(FakeResponse(200, b"\xff\xff\xffnot protobuf"))
    with pytest.raises(FetchError):
        c.fetch("http://x", {})


def test_headers_timeout_and_user_agent():
    c, s = client(FakeResponse(200, feed_bytes([])), timeout=3)
    c.fetch("http://x", {"Authorization": "secret"})
    url, headers, timeout = s.calls[0]
    assert url == "http://x" and timeout == 3
    assert headers["Authorization"] == "secret"
    assert "User-Agent" in headers


def test_caller_user_agent_wins():
    c, s = client(FakeResponse(200, feed_bytes([])))
    c.fetch("http://x", {"user-agent": "mine"})
    assert s.calls[0][1] == {"user-agent": "mine"}


def test_errors_do_not_leak_headers():
    c, _ = client(exc=requests.ConnectionError("boom"))
    with pytest.raises(FetchError) as ei:
        c.fetch("http://x", {"Authorization": "SECRETKEY"})
    assert "SECRETKEY" not in str(ei.value)


def test_absurd_timestamp_only_loses_that_vehicles_time():
    ok = {"id": "a", "lat": 1.0, "lon": 2.0, "ts": 1760000000}
    ms = {"id": "b", "lat": 1.0, "lon": 2.0, "ts": 1760000000000}  # milliseconds
    huge = {"id": "c", "lat": 1.0, "lon": 2.0, "ts": 2**63 - 1}
    c, _ = client(FakeResponse(200, feed_bytes([ok, ms, huge])))
    got = {p.vehicle_id: p.timestamp for p in c.fetch("http://x", {})}
    assert got["a"] is not None and got["b"] is None and got["c"] is None
