from datetime import datetime, timedelta, timezone

from tfnsw_trip_planner import VehiclePosition

from recorder.live import LiveHub


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


T0 = utc(2026, 10, 16, 8, 0)


def vp(vehicle="v1", ts=T0, lat=-33.8, lon=151.2, route="2510_333", bearing=90.0, speed=5.5):
    return VehiclePosition(
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
    assert reports(snap, "buses") == [["a", "2510_333", int(T0.timestamp()), 151.21235, -33.81235, 90.0, 5.5]]


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
