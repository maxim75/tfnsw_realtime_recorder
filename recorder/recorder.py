"""Polling loop: fetch feeds, de-duplicate, write window files."""
from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .cities import City
from .config import CitySettings, Settings, SettingsStore
from .feeds import STALE_AFTER_S
from .gtfsrt import FetchError, GtfsRtClient
from .routes import RouteInfo
from .storage import city_dir
from .windows import SYDNEY_TZ
from .writer import RotatingWriter

log = logging.getLogger(__name__)


def new_rows(positions, last_seen: dict, fetched_at: datetime, tz: ZoneInfo = SYDNEY_TZ) -> list[dict[str, str]]:
    """Rows for positions whose GPS timestamp changed since the last poll."""
    rows = []
    present = set()
    for p in positions:
        key = p.vehicle_id or p.trip_id or f"{p.latitude},{p.longitude}"
        present.add(key)
        if p.latitude == 0.0 and p.longitude == 0.0:
            continue  # no GPS fix
        if key in last_seen and last_seen[key] == p.timestamp:
            continue  # unchanged since last poll
        last_seen[key] = p.timestamp
        rows.append(
            {
                "fetched_at": fetched_at.astimezone(tz).isoformat(timespec="seconds"),
                "timestamp": p.timestamp.astimezone(tz).isoformat() if p.timestamp else "",
                "vehicle_id": p.vehicle_id or "",
                "trip_id": p.trip_id or "",
                "route_id": p.route_id or "",
                "latitude": f"{p.latitude:.6f}",
                "longitude": f"{p.longitude:.6f}",
                "bearing": "" if p.bearing is None else f"{p.bearing:.1f}",
                "speed": "" if p.speed is None else f"{p.speed:.2f}",
            }
        )
    # Bound memory: forget vehicles absent from this poll (ids may be trip-scoped).
    # Trade-off: one that disappears and returns with the same timestamp is written again.
    for key in [k for k in last_seen if k not in present]:
        del last_seen[key]
    return rows


def terminate_process() -> None:
    """Ask the process to shut down gracefully (uvicorn handles SIGTERM) so the restart policy acts."""
    os.kill(os.getpid(), signal.SIGTERM)


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
    data_age_s: float | None = None  # poll time minus the newest GPS report
    stale: bool = False


class Recorder:
    """Polls one city's enabled feeds and writes their window files.

    *settings* is the `SettingsStore` (or a one-city view of it with ``get()``);
    files go to ``DATA_DIR/<city>/`` under *data_dir*. *client* is used only by
    this recorder's thread.
    """

    def __init__(
        self,
        client: GtfsRtClient,
        city: City,
        settings: SettingsStore | CitySettings,
        data_dir: Path,
        on_complete: Callable[[Path], None],
        *,
        routes: RouteInfo,
        request_spacing: float | None = None,
        clock: Callable[[], datetime] | None = None,
        on_fatal: Callable[[], None] = terminate_process,
        on_positions: Callable[[str, datetime, list], None] | None = None,
    ) -> None:
        self._client = client
        self.city = city
        self.routes = routes  # the city's line names and modes (for callers of this recorder)
        self._settings = settings.for_city(city.id) if isinstance(settings, SettingsStore) else settings
        self._data_dir = city_dir(data_dir, city.id)
        self._on_complete = on_complete
        # stay under the city's rate limit
        self._requests = 0  # rotates through the city's API keys
        self._spacing = 1 / city.max_requests_per_second if request_spacing is None else request_spacing
        self._tz = tz = city.timezone
        self._clock = clock or (lambda: datetime.now(tz=tz))
        self._on_fatal = on_fatal
        self._on_positions = on_positions  # live map; must never affect recording
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
        self._thread = threading.Thread(target=self._run, name=f"recorder-{self.city.id}", daemon=True)
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
                cycle_start = time.monotonic()
                self.poll_once(self._settings.get())
                while not self._stop.is_set():
                    # Re-read each second so a lowered interval applies promptly.
                    remaining = cycle_start + self._settings.get().interval - time.monotonic()
                    if remaining <= 0:
                        break
                    self._stop.wait(min(1.0, remaining))
                    self.tick(self._clock(), self._settings.get())
        except Exception:
            self._crashed = True
            log.exception("%s: recorder crashed", self.city.id)
        finally:
            for feed in list(self._writers):
                try:
                    self._close(feed)
                except Exception:
                    log.exception("%s: failed to close window file", feed)
        if self._crashed:
            try:
                self._on_fatal()
            except Exception:
                log.exception("on_fatal failed")

    def poll_once(self, settings: Settings) -> None:
        self._close_disabled(settings)
        polled = vehicles = new = errors = 0
        for n, feed in enumerate(settings.feeds):
            if self._stop.is_set():
                break
            if n:
                self._stop.wait(self._spacing)  # stay under the rate limit
            v, r = self._poll_feed(feed)
            polled += 1
            if v is None:
                errors += 1
            else:
                vehicles += v
                new += r
        if polled:
            log.info("%s cycle: feeds=%d vehicles=%d new_rows=%d errors=%d", self.city.id, polled, vehicles, new, errors)

    def tick(self, now: datetime, settings: Settings) -> None:
        """Between polls: close ended windows and feeds that were disabled."""
        self._close_disabled(settings)
        for feed, writer in self._writers.items():
            writer.rotate_if_ended(now)
            self._sync_writer_status(feed)

    def _record_error(self, feed: str, fetched_at: datetime, exc: Exception) -> None:
        with self._lock:
            st = self._status.setdefault(feed, FeedStatus())
            st.last_poll_at = fetched_at
            st.last_error = str(exc) or type(exc).__name__
            st.last_error_at = fetched_at

    def _next_headers(self) -> dict[str, str]:
        sets = self.city.header_sets
        if not sets:
            return {}
        self._requests += 1
        return sets[(self._requests - 1) % len(sets)]

    def _poll_feed(self, feed: str) -> tuple[int | None, int]:
        """Poll one feed. Returns (vehicles, new rows), or (None, 0) on error."""
        fetched_at = self._clock()
        try:
            positions = self._client.fetch(self.city.feed(feed).url, self._next_headers())
            seen = dict(self._last_seen.get(feed, {}))
            rows = new_rows(positions, seen, fetched_at, self._tz)
            writer = self._writers.get(feed)
            if writer is None:
                writer = self._writers[feed] = RotatingWriter(feed, self._data_dir, self._on_complete, tz=self._tz)
            writer.write(fetched_at, rows)
            self._last_seen[feed] = seen  # only after a successful write
        except FetchError as exc:
            log.warning("%s/%s: %s", self.city.id, feed, exc)
            self._record_error(feed, fetched_at, exc)
            return None, 0
        except Exception as exc:  # disk errors, bad data, bugs
            log.exception("%s/%s: unexpected error", self.city.id, feed)
            self._record_error(feed, fetched_at, exc)
            return None, 0
        log.debug("%-34s vehicles=%5d new=%5d", feed, len(positions), len(rows))
        newest = max((p.timestamp for p in positions if p.timestamp), default=None)
        data_age_s = (fetched_at - newest).total_seconds() if newest else None
        stale = data_age_s is not None and data_age_s > STALE_AFTER_S
        with self._lock:
            st = self._status.setdefault(feed, FeedStatus())
            if stale and not st.stale:
                log.warning("%s/%s: newest GPS report is %.0f s old; the feed looks stale", self.city.id, feed, data_age_s)
            st.data_age_s = data_age_s
            st.stale = stale
            st.last_poll_at = fetched_at
            st.vehicles = len(positions)
            st.rows_written_last_poll = len(rows)
            st.total_rows += len(rows)
        self._sync_writer_status(feed)
        if self._on_positions is not None:
            try:
                self._on_positions(feed, fetched_at, positions)
            except Exception:
                log.exception("%s: live view update failed", feed)
        return len(positions), len(rows)

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
