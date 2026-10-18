import time
from datetime import datetime, timezone

import pytest
import requests

from recorder.cities import load_cities
from recorder.config import Settings, SettingsStore
from recorder.gtfsrt import FetchError, Position
from recorder.recorder import Recorder, new_rows
from recorder.routes import RouteInfo

CITIES = load_cities(env={"TFNSW_API_KEY": "k"})
SYDNEY, PRAGUE = CITIES["sydney"], CITIES["prague"]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def vp(vehicle="v1", ts=utc(2026, 10, 16, 1, 10), lat=-33.8, lon=151.2):
    return Position(
        vehicle_id=vehicle, trip_id="t", route_id="2510_992", latitude=lat, longitude=lon,
        bearing=90.0, speed=5.5, timestamp=ts,
    )


class FakeClient:
    """Answers ``fetch(url, headers)`` from *responses*, keyed by the city's feed ids."""

    def __init__(self, responses, city=SYDNEY):
        self.responses = {city.feed(feed).url: r for feed, r in responses.items()}
        self.ids = {city.feed(feed).url: feed for feed in responses}
        self.calls = []  # feed ids, in order
        self.headers = []

    def fetch(self, url, headers):
        self.calls.append(self.ids[url])
        self.headers.append(dict(headers))
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r


def recorder(tmp_path, client, settings, on_complete=lambda p: None, city=SYDNEY, **kwargs):
    kwargs.setdefault("request_spacing", 0)
    return Recorder(client, city, settings, tmp_path, on_complete,
                    routes=RouteInfo(city, tmp_path / city.id), **kwargs)


def store_for(tmp_path, feeds, interval=30, city=SYDNEY):
    store = SettingsStore(tmp_path / "settings.json", CITIES)
    store.update(city.id, {"feeds": list(feeds), "interval": interval})
    return store


def make(tmp_path, responses, feeds=("buses",), interval=30, now=utc(2026, 10, 16, 1, 11), city=SYDNEY):
    store = store_for(tmp_path, feeds, interval, city).for_city(city.id)
    clock = [now]
    done = []
    rec = recorder(tmp_path, FakeClient(responses, city), store, done.append, city=city, clock=lambda: clock[0])
    return rec, store, clock, done


def test_new_rows_dedupes_and_skips_missing_fix():
    seen = {}
    fetched = utc(2026, 10, 16, 1, 11)
    rows = new_rows([vp("a"), vp("b", lat=0.0, lon=0.0)], seen, fetched)
    assert [r["vehicle_id"] for r in rows] == ["a"]
    assert rows[0]["latitude"] == "-33.800000" and rows[0]["speed"] == "5.50"
    assert rows[0]["fetched_at"] == "2026-10-16T12:11:00+11:00"
    assert rows[0]["timestamp"] == "2026-10-16T12:10:00+11:00"  # UTC from the feed, written in local time
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
        {"buses": FetchError("HTTP 429"), "metro": [vp("m")]},
        feeds=("buses", "metro"),
    )
    rec.poll_once(store.get())
    status = rec.status()
    assert status["buses"].last_error == "HTTP 429" and status["buses"].last_error_at
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
    rec = recorder(tmp_path, FakeClient({}), BrokenSettings(), on_fatal=lambda: fatal.append(1))
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
    rec = recorder(tmp_path, client, store, on_fatal=lambda: None)
    rec.start()
    assert wait_until(lambda: len(client.calls) == 1)
    store.interval = 1
    assert wait_until(lambda: len(client.calls) >= 2, timeout=5)
    rec.stop(timeout=5)


def test_stop_keeps_closing_writers_when_one_close_fails(tmp_path):
    def on_complete(path):
        if path.name.startswith("buses_"):
            raise RuntimeError("upload queue broken")

    store = store_for(tmp_path, ["buses", "metro"]).for_city("sydney")
    rec = recorder(
        tmp_path, FakeClient({"buses": [vp("a")], "metro": [vp("m")]}), store, on_complete,
        clock=lambda: utc(2026, 10, 16, 1, 11),
    )
    rec.start()
    deadline = time.monotonic() + 5
    while len(rec.status()) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    rec.stop(timeout=5)
    assert not rec.alive
    completed = {p.name for p in (tmp_path / "sydney" / "complete").glob("*/*.csv.gz")}
    assert "metro_2026-10-16T12-10+1100.csv.gz" in completed


def test_polls_each_enabled_feed_url_with_city_headers_spaced_by_rate_limit(tmp_path):
    feeds = ("buses", "sydneytrains", "metro")
    store = store_for(tmp_path, feeds).for_city("sydney")
    client = FakeClient({f: [vp(f)] for f in feeds})
    rec = Recorder(client, SYDNEY, store, tmp_path, lambda p: None,
                   routes=RouteInfo(SYDNEY, tmp_path / "sydney"), clock=lambda: utc(2026, 10, 16, 1, 11))
    waits = []
    rec._stop.wait = waits.append  # record the spacing instead of sleeping
    rec.poll_once(store.get())
    assert client.calls == list(feeds)  # in the city's feed order
    assert client.headers == [{"Authorization": "apikey k"}] * 3
    assert waits == [1 / SYDNEY.max_requests_per_second] * 2  # 0.2 s between requests


def test_rotates_api_keys_across_requests(tmp_path):
    city = load_cities(env={"TFNSW_API_KEY": "k", "TFNSW_API_KEY_2": "k2"})["sydney"]
    feeds = ("buses", "sydneytrains", "metro")
    store = store_for(tmp_path, feeds).for_city("sydney")
    client = FakeClient({f: [vp(f)] for f in feeds}, city=city)
    rec = Recorder(client, city, store, tmp_path, lambda p: None,
                   routes=RouteInfo(city, tmp_path / "sydney"), clock=lambda: utc(2026, 10, 16, 1, 11))
    waits = []
    rec._stop.wait = waits.append
    rec.poll_once(store.get())
    rec.poll_once(store.get())
    keys = [h["Authorization"] for h in client.headers]
    assert keys == ["apikey k", "apikey k2"] * 3
    assert waits[0] == 1 / 10  # two keys: twice the per-key rate


def test_recorder_accepts_the_settings_store(tmp_path):
    store = store_for(tmp_path, ["metro"])
    client = FakeClient({"metro": [vp("m")]})
    rec = recorder(tmp_path, client, store, clock=lambda: utc(2026, 10, 16, 1, 11))
    rec.poll_once(store.get("sydney"))
    assert client.calls == ["metro"]


def test_prague_recorder_writes_prague_local_window_files(tmp_path):
    rec, store, _, _ = make(
        tmp_path, {"all": [vp("metro-B-25-25", lat=50.08, lon=14.44)]}, feeds=("all",),
        now=utc(2026, 10, 16, 10, 11), city=PRAGUE,
    )
    rec.poll_once(store.get())
    path = tmp_path / "prague" / "open" / "all" / "all_2026-10-16T12-10+0200.csv.gz"
    assert path.is_file()
    import gzip

    text = gzip.decompress(path.read_bytes()).decode()
    assert "2026-10-16T12:11:00+02:00" in text and "metro-B-25-25" in text
    assert rec.status()["all"].current_file == path.name


def test_feed_with_old_gps_data_is_flagged_stale(tmp_path):
    # poll at 01:11Z; newest report 2 years old (the frozen v1 metro feed looked like this)
    rec, store, _, _ = make(
        tmp_path, {"metro": [vp("m", ts=utc(2024, 10, 16, 1, 10))], "buses": [vp("a", ts=utc(2026, 10, 16, 1, 10, 30))]},
        feeds=("buses", "metro"),
    )
    rec.poll_once(store.get())
    status = rec.status()
    assert status["metro"].stale is True and status["metro"].data_age_s > 600
    assert status["buses"].stale is False and status["buses"].data_age_s == 30


def test_empty_feed_has_no_data_age(tmp_path):
    rec, store, _, _ = make(tmp_path, {"buses": []})
    rec.poll_once(store.get())
    st = rec.status()["buses"]
    assert st.data_age_s is None and st.stale is False


def test_positions_are_passed_to_live_view_after_each_poll(tmp_path):
    seen = []
    store = store_for(tmp_path, ["buses", "metro"]).for_city("sydney")
    rec = recorder(
        tmp_path, FakeClient({"buses": [vp("a")], "metro": FetchError("HTTP 500")}), store,
        clock=lambda: utc(2026, 10, 16, 1, 11),
        on_positions=lambda feed, fetched_at, positions: seen.append((feed, fetched_at, len(positions))),
    )
    rec.poll_once(store.get())
    assert seen == [("buses", utc(2026, 10, 16, 1, 11), 1)]  # not called for the failed feed


def test_live_view_errors_do_not_affect_recording(tmp_path):
    def broken(*_):
        raise RuntimeError("live view bug")

    store = store_for(tmp_path, ["buses"]).for_city("sydney")
    rec = recorder(
        tmp_path, FakeClient({"buses": [vp("a")]}), store,
        clock=lambda: utc(2026, 10, 16, 1, 11), on_positions=broken,
    )
    rec.poll_once(store.get())
    st = rec.status()["buses"]
    assert st.rows_written_last_poll == 1 and st.last_error is None
