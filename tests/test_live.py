import json
from datetime import datetime, timedelta, timezone

from recorder.cities import load_cities
from recorder.gtfsrt import Position
from recorder.live import LiveHub as _LiveHub
from recorder.routes import RouteInfo

CITIES = load_cities(env={"TFNSW_API_KEY": "k"})
SYDNEY, PRAGUE = CITIES["sydney"], CITIES["prague"]


def LiveHub(city=SYDNEY, routes=None, **kwargs):
    from pathlib import Path

    return _LiveHub(city, routes or RouteInfo(city, Path("/nonexistent")), **kwargs)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


T0 = utc(2026, 10, 16, 8, 0)


def vp(vehicle="v1", ts=T0, lat=-33.8, lon=151.2, route="2510_333", bearing=90.0, speed=5.5):
    return Position(
        vehicle_id=vehicle, trip_id="t", route_id=route, latitude=lat, longitude=lon,
        bearing=bearing, speed=speed, timestamp=ts,
    )


def reports(snapshot, feed):
    return snapshot["feeds"].get(feed, [])


def test_publish_compacts_reports_and_bumps_seq():
    hub = LiveHub()
    hub.publish("buses", T0 + timedelta(seconds=10), [vp("a", lat=-33.812345678, lon=151.212345678)])
    snap = hub.snapshot()
    assert snap["seq"] == 1
    assert reports(snap, "buses") == [
        ["a", "2510_333", int(T0.timestamp()), 151.21235, -33.81235, 90.0, 5.5, "333", "bus"]
    ]


def test_skips_missing_fix_stale_and_unchanged_reports():
    hub = LiveHub()
    fetched = T0 + timedelta(seconds=10)
    hub.publish("buses", fetched, [
        vp("a"),
        vp("nofix", lat=0.0, lon=0.0),
        vp("old", ts=T0 - timedelta(hours=1)),
    ])
    hub.publish("buses", fetched + timedelta(seconds=30), [vp("a")])  # same GPS time again
    snap = hub.snapshot()
    assert [r[0] for r in reports(snap, "buses")] == ["a"]
    assert snap["seq"] == 1  # second publish had nothing new: no batch


def test_keeps_recent_history_per_vehicle():
    hub = LiveHub(history=3)
    for i in range(5):
        ts = T0 + timedelta(seconds=15 * i)
        hub.publish("buses", ts + timedelta(seconds=5), [vp("a", ts=ts, lon=151.2 + i / 1000)])
    rows = reports(hub.snapshot(), "buses")
    assert [r[2] for r in rows] == [int((T0 + timedelta(seconds=15 * i)).timestamp()) for i in (2, 3, 4)]


def test_forgets_vehicles_that_stop_reporting():
    hub = LiveHub(forget_after_s=600)
    hub.publish("buses", T0, [vp("a"), vp("b")])
    later = T0 + timedelta(minutes=11)
    hub.publish("buses", later, [vp("b", ts=later)])
    assert sorted({r[0] for r in reports(hub.snapshot(), "buses")}) == ["b"]


def test_batches_after_returns_only_newer_batches_with_new_reports():
    hub = LiveHub(max_batches=2)
    for i in range(3):
        ts = T0 + timedelta(seconds=30 * i)
        hub.publish("buses", ts, [vp("a", ts=ts)])
    batches = hub.batches_after(1)
    assert [b["seq"] for b in batches] == [2, 3]
    assert batches[-1]["feed"] == "buses"
    assert [r[0] for r in batches[-1]["reports"]] == ["a"]
    assert hub.batches_after(3) == []
    assert [b["seq"] for b in hub.batches_after(0)] == [2, 3]  # oldest batch already dropped


def test_close():
    hub = LiveHub()
    assert not hub.closed
    hub.close()
    assert hub.closed


def test_reports_carry_label_and_mode_from_the_city_route_info(tmp_path):
    (tmp_path / "routes.json").write_text(json.dumps(
        {"checked": 0, "last_modified": None, "routes": {"L992": ["B", "metro"], "L22": ["22", "tram"]}}
    ))
    hub = LiveHub(PRAGUE, RouteInfo(PRAGUE, tmp_path))
    hub.publish("all", T0 + timedelta(seconds=10), [
        vp("metro-B-25-25", route="L992", lat=50.08, lon=14.44),
        vp("bus-1", route="L9999", lat=50.07, lon=14.43),  # not in routes.txt
    ])
    rows = {r[0]: r for r in reports(hub.snapshot(), "all")}
    assert rows["metro-B-25-25"][7:] == ["B", "metro"]
    assert rows["bus-1"][7:] == ["L9999", "bus"]  # raw id, the feed's mode
    assert hub.batches_after(0)[0]["reports"][0][7:] == ["B", "metro"]


def test_sydney_feed_mode_is_used_for_live_reports():
    hub = LiveHub()
    hub.publish("ferries/MFF", T0 + timedelta(seconds=10), [vp("f", route="MFF_F99")])
    assert reports(hub.snapshot(), "ferries/MFF")[0][7:] == ["F99", "ferry"]
