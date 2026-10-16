import time
from datetime import datetime, timezone

from tfnsw_trip_planner import APIError, VehiclePosition

from recorder.config import SettingsStore
from recorder.recorder import Recorder, new_rows
from recorder.windows import SYDNEY_TZ


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def vp(vehicle="v1", ts=utc(2026, 10, 16, 1, 10), lat=-33.8, lon=151.2):
    return VehiclePosition(
        vehicle_id=vehicle, trip_id="t", route_id="2510_992", latitude=lat, longitude=lon,
        bearing=90.0, speed=5.5, timestamp=ts.astimezone(SYDNEY_TZ),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def vehicle_positions(self, mode):
        self.calls.append(mode)
        r = self.responses[mode]
        if isinstance(r, Exception):
            raise r
        return r


def make(tmp_path, responses, feeds=("buses",), interval=30, now=utc(2026, 10, 16, 1, 11)):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"feeds": list(feeds), "interval": interval})
    clock = [now]
    done = []
    rec = Recorder(
        FakeClient(responses), store, tmp_path, done.append,
        request_spacing=0, clock=lambda: clock[0],
    )
    return rec, store, clock, done


def test_new_rows_dedupes_and_skips_missing_fix():
    seen = {}
    fetched = utc(2026, 10, 16, 1, 11)
    rows = new_rows([vp("a"), vp("b", lat=0.0, lon=0.0)], seen, fetched)
    assert [r["vehicle_id"] for r in rows] == ["a"]
    assert rows[0]["latitude"] == "-33.800000" and rows[0]["speed"] == "5.50"
    assert rows[0]["fetched_at"] == "2026-10-16T12:11:00+11:00"
    assert new_rows([vp("a")], seen, fetched) == []
    assert len(new_rows([vp("a", ts=utc(2026, 10, 16, 1, 11))], seen, fetched)) == 1


def test_poll_once_writes_and_updates_status(tmp_path):
    rec, store, _, _ = make(tmp_path, {"buses": [vp("a"), vp("b")]})
    rec.poll_once(store.get())
    st = rec.status()["buses"]
    assert st.vehicles == 2 and st.rows_written_last_poll == 2 and st.rows_in_window == 2
    assert st.current_file == "buses_2026-10-16T12-10+1100.csv.gz"
    assert st.last_error is None


def test_error_on_one_feed_does_not_stop_others(tmp_path):
    rec, store, _, _ = make(
        tmp_path,
        {"buses": APIError("API error 429"), "metro": [vp("m")]},
        feeds=("buses", "metro"),
    )
    rec.poll_once(store.get())
    status = rec.status()
    assert "429" in status["buses"].last_error
    assert status["metro"].rows_written_last_poll == 1


def test_tick_rotates_ended_window(tmp_path):
    rec, store, clock, done = make(tmp_path, {"buses": [vp("a")]})
    rec.poll_once(store.get())
    rec.tick(utc(2026, 10, 16, 1, 20, 1), store.get())
    assert [p.name for p in done] == ["buses_2026-10-16T12-10+1100.csv.gz"]
    assert rec.status()["buses"].current_file is None


def test_disabled_feed_is_closed_on_next_tick(tmp_path):
    rec, store, _, done = make(tmp_path, {"buses": [vp("a")]}, interval=600)
    rec.poll_once(store.get())
    store.update({"feeds": ["metro"], "interval": 600})
    rec.tick(utc(2026, 10, 16, 1, 11, 1), store.get())  # same window
    assert len(done) == 1


def test_thread_stop_closes_open_files(tmp_path):
    rec, _, _, done = make(tmp_path, {"buses": [vp("a")]})
    rec.start()
    deadline = time.monotonic() + 5
    while not rec.status().get("buses") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rec.alive
    rec.stop(timeout=5)
    assert not rec.alive
    assert len(done) == 1


def test_unexpected_exception_marks_recorder_dead(tmp_path):
    rec, _, _, _ = make(tmp_path, {"buses": RuntimeError("bug")})
    rec.start()
    deadline = time.monotonic() + 5
    while rec.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not rec.alive
