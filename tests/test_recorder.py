import time
from datetime import datetime, timezone

import pytest
import requests
from tfnsw_trip_planner import APIError, VehiclePosition

from recorder.config import Settings, SettingsStore
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


def wait_until(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


class BrokenSettings:
    def get(self):
        raise RuntimeError("settings bug")


def test_thread_fatal_error_marks_dead_and_calls_on_fatal(tmp_path):
    fatal = []
    rec = Recorder(
        FakeClient({}), BrokenSettings(), tmp_path, lambda p: None,
        request_spacing=0, on_fatal=lambda: fatal.append(1),
    )
    rec.start()
    assert wait_until(lambda: fatal)
    assert not rec.alive


def test_normal_stop_does_not_call_on_fatal(tmp_path):
    fatal = []
    rec, _, _, _ = make(tmp_path, {"buses": [vp("a")]})
    rec._on_fatal = lambda: fatal.append(1)
    rec.start()
    assert wait_until(lambda: rec.status().get("buses"))
    rec.stop(timeout=5)
    assert fatal == []


@pytest.mark.parametrize(
    "exc", [RuntimeError("bug"), requests.exceptions.ChunkedEncodingError("cut off"), OverflowError("ts")]
)
def test_unexpected_feed_error_is_contained(tmp_path, exc):
    rec, store, _, _ = make(
        tmp_path, {"buses": exc, "metro": [vp("m")]}, feeds=("buses", "metro")
    )
    rec.poll_once(store.get())
    status = rec.status()
    assert status["buses"].last_error and status["buses"].last_error_at
    assert status["metro"].rows_written_last_poll == 1


def test_thread_survives_unexpected_feed_errors(tmp_path):
    fatal = []
    rec, store, _, _ = make(
        tmp_path,
        {"buses": requests.exceptions.ChunkedEncodingError("x"), "metro": [vp("m")]},
        feeds=("buses", "metro"),
    )
    rec._on_fatal = lambda: fatal.append(1)
    rec.start()
    assert wait_until(lambda: rec.status().get("metro") and rec.status().get("buses"))
    assert rec.alive and fatal == []
    rec.stop(timeout=5)


def test_write_failure_does_not_kill_recorder(tmp_path, monkeypatch):
    rec, store, _, _ = make(tmp_path, {"buses": [vp("a")]})
    from recorder import writer

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(writer.gzip, "open", boom)
    rec.poll_once(store.get())
    assert "No space" in rec.status()["buses"].last_error
    monkeypatch.undo()
    rec.poll_once(store.get())  # recovers; row was not marked as seen
    assert rec.status()["buses"].rows_written_last_poll == 1


def test_new_rows_drops_vehicles_missing_from_poll():
    seen = {}
    fetched = utc(2026, 10, 16, 1, 11)
    new_rows([vp("a"), vp("b")], seen, fetched)
    new_rows([vp("a")], seen, fetched)
    assert set(seen) == {"a"}
    # documented trade-off: a vehicle that disappears and returns with the same
    # timestamp is written again.
    assert len(new_rows([vp("a"), vp("b")], seen, fetched)) == 1


def test_new_rows_keeps_dedupe_state_for_skipped_positions():
    seen = {}
    fetched = utc(2026, 10, 16, 1, 11)
    new_rows([vp("z")], seen, fetched)
    new_rows([vp("a"), vp("z", lat=0.0, lon=0.0)], seen, fetched)  # z loses its fix
    assert set(seen) == {"a", "z"}
    assert new_rows([vp("z")], seen, fetched) == []  # same timestamp: still deduped


def test_lowering_interval_applies_within_cycle(tmp_path):
    class Store:
        interval = 600

        def get(self):
            return Settings(feeds=("buses",), interval=self.interval)

    store = Store()
    client = FakeClient({"buses": [vp("a")]})
    rec = Recorder(client, store, tmp_path, lambda p: None, request_spacing=0, on_fatal=lambda: None)
    rec.start()
    assert wait_until(lambda: len(client.calls) == 1)
    store.interval = 1
    assert wait_until(lambda: len(client.calls) >= 2, timeout=5)
    rec.stop(timeout=5)


def test_stop_keeps_closing_writers_when_one_close_fails(tmp_path):
    def on_complete(path):
        if path.name.startswith("buses_"):
            raise RuntimeError("upload queue broken")

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"feeds": ["buses", "metro"], "interval": 30})
    rec = Recorder(
        FakeClient({"buses": [vp("a")], "metro": [vp("m")]}), store, tmp_path, on_complete,
        request_spacing=0, clock=lambda: utc(2026, 10, 16, 1, 11),
    )
    rec.start()
    deadline = time.monotonic() + 5
    while len(rec.status()) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    rec.stop(timeout=5)
    assert not rec.alive
    completed = {p.name for p in (tmp_path / "complete").glob("*/*.csv.gz")}
    assert "metro_2026-10-16T12-10+1100.csv.gz" in completed
