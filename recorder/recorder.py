"""Polling loop: fetch feeds, de-duplicate, write window files."""
from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tfnsw_trip_planner import TripPlannerError

from .config import Settings, SettingsStore
from .feeds import MAX_REQUESTS_PER_SECOND
from .windows import SYDNEY_TZ
from .writer import RotatingWriter

log = logging.getLogger(__name__)


def new_rows(positions, last_seen: dict, fetched_at: datetime) -> list[dict[str, str]]:
    """Rows for positions whose GPS timestamp changed since the last poll."""
    rows = []
    for p in positions:
        if p.latitude == 0.0 and p.longitude == 0.0:
            continue  # no GPS fix
        key = p.vehicle_id or p.trip_id or f"{p.latitude},{p.longitude}"
        if key in last_seen and last_seen[key] == p.timestamp:
            continue  # unchanged since last poll
        last_seen[key] = p.timestamp
        rows.append(
            {
                "fetched_at": fetched_at.astimezone(SYDNEY_TZ).isoformat(timespec="seconds"),
                "timestamp": p.timestamp.isoformat() if p.timestamp else "",
                "vehicle_id": p.vehicle_id or "",
                "trip_id": p.trip_id or "",
                "route_id": p.route_id or "",
                "latitude": f"{p.latitude:.6f}",
                "longitude": f"{p.longitude:.6f}",
                "bearing": "" if p.bearing is None else f"{p.bearing:.1f}",
                "speed": "" if p.speed is None else f"{p.speed:.2f}",
            }
        )
    return rows


@dataclass
class FeedStatus:
    last_poll_at: datetime | None = None
    vehicles: int = 0
    rows_written_last_poll: int = 0
    rows_in_window: int = 0
    current_file: str | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    total_rows: int = 0


class Recorder:
    def __init__(
        self,
        client,
        settings: SettingsStore,
        data_dir: Path,
        on_complete: Callable[[Path], None],
        *,
        request_spacing: float = 1 / MAX_REQUESTS_PER_SECOND,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=SYDNEY_TZ),
    ) -> None:
        self._client = client
        self._settings = settings
        self._data_dir = data_dir
        self._on_complete = on_complete
        self._spacing = request_spacing
        self._clock = clock
        self._writers: dict[str, RotatingWriter] = {}
        self._last_seen: dict[str, dict] = {}
        self._status: dict[str, FeedStatus] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._crashed = False
        self.started_at: datetime | None = None

    # -- thread control -------------------------------------------------

    def start(self) -> None:
        self.started_at = self._clock()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._crashed

    def status(self) -> dict[str, FeedStatus]:
        with self._lock:
            return copy.deepcopy(self._status)

    # -- work -----------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                settings = self._settings.get()
                wake = time.monotonic() + settings.interval
                self.poll_once(settings)
                while not self._stop.is_set() and time.monotonic() < wake:
                    self._stop.wait(min(1.0, max(0.0, wake - time.monotonic())))
                    self.tick(self._clock(), self._settings.get())
        except Exception:
            self._crashed = True
            log.exception("Recorder crashed")
        finally:
            for feed in list(self._writers):
                self._close(feed)

    def poll_once(self, settings: Settings) -> None:
        self._close_disabled(settings)
        for n, feed in enumerate(settings.feeds):
            if self._stop.is_set():
                return
            if n:
                self._stop.wait(self._spacing)  # stay under the rate limit
            self._poll_feed(feed)

    def tick(self, now: datetime, settings: Settings) -> None:
        """Between polls: close ended windows and feeds that were disabled."""
        self._close_disabled(settings)
        for feed, writer in self._writers.items():
            writer.rotate_if_ended(now)
            self._sync_writer_status(feed)

    def _poll_feed(self, feed: str) -> None:
        fetched_at = self._clock()
        try:
            positions = self._client.vehicle_positions(feed)
        except TripPlannerError as exc:
            log.warning("%s: %s", feed, exc)
            with self._lock:
                st = self._status.setdefault(feed, FeedStatus())
                st.last_poll_at = fetched_at
                st.last_error = str(exc)
                st.last_error_at = fetched_at
            return
        rows = new_rows(positions, self._last_seen.setdefault(feed, {}), fetched_at)
        writer = self._writers.get(feed)
        if writer is None:
            writer = self._writers[feed] = RotatingWriter(feed, self._data_dir, self._on_complete)
        writer.write(fetched_at, rows)
        log.info("%-34s vehicles=%5d new=%5d", feed, len(positions), len(rows))
        with self._lock:
            st = self._status.setdefault(feed, FeedStatus())
            st.last_poll_at = fetched_at
            st.vehicles = len(positions)
            st.rows_written_last_poll = len(rows)
            st.total_rows += len(rows)
        self._sync_writer_status(feed)

    def _close_disabled(self, settings: Settings) -> None:
        for feed in [f for f in self._writers if f not in settings.feeds]:
            self._close(feed)
            self._last_seen.pop(feed, None)

    def _close(self, feed: str) -> None:
        self._writers.pop(feed).close()
        with self._lock:
            st = self._status.get(feed)
            if st:
                st.rows_in_window = 0
                st.current_file = None

    def _sync_writer_status(self, feed: str) -> None:
        writer = self._writers.get(feed)
        with self._lock:
            st = self._status.setdefault(feed, FeedStatus())
            st.rows_in_window = writer.rows_in_window if writer else 0
            st.current_file = writer.current_path.name if writer and writer.current_path else None
