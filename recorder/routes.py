"""Line names and modes for a city, from its static GTFS feed (routes.txt)."""

import csv
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import requests

from recorder.cities import City, Feed
from recorder.gtfsrt import USER_AGENT

log = logging.getLogger(__name__)

REFRESH_INTERVAL = 24 * 3600  # s between checks of the static feed
CHECK_INTERVAL = 3600  # s between RouteRefresher calls; refresh() itself skips a fresh cache
DOWNLOAD_TIMEOUT = (10, 120)  # connect, read (s)


def _build_mode_table() -> dict[int, str]:
    table = {0: "tram", 1: "metro", 2: "rail", 3: "bus", 4: "ferry",
             5: "cable", 6: "cable", 7: "cable", 11: "trolleybus"}
    for lo, hi, mode in (
        (100, 117, "rail"), (200, 209, "bus"), (400, 405, "metro"),
        (700, 716, "bus"), (900, 906, "tram"), (1000, 1000, "ferry"),
        (1300, 1307, "cable"),
    ):
        table.update({t: mode for t in range(lo, hi + 1)})
    return table


# GTFS route_type (basic and extended) -> mode
MODE_BY_ROUTE_TYPE: dict[int, str] = _build_mode_table()


class RouteInfo:
    """Label and mode lookups for one city.

    The mapping comes from ``routes.json`` in *data_dir* (loaded at construction,
    no network) and is replaced wholesale by ``refresh()``. Lookups never block.
    """

    def __init__(self, city: City, data_dir: Path, *, session=None, clock=time.time):
        self._city = city
        self._dir = Path(data_dir)
        self._session = session
        self._clock = clock
        self._pattern = re.compile(city.route_label_pattern) if city.route_label_pattern else None
        self._lock = threading.Lock()  # serialises refreshes
        self._routes: dict[str, tuple[str, str]] = {}
        self._checked = 0.0
        self._last_modified: str | None = None
        self._load_cache()

    @property
    def _cache_file(self) -> Path:
        return self._dir / "routes.json"

    def _load_cache(self) -> None:
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            routes = {rid: (v[0], v[1]) for rid, v in data["routes"].items()}
            checked = float(data.get("checked", 0))
            last_modified = data.get("last_modified")
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            log.warning("ignoring unreadable route cache %s: %s", self._cache_file, exc)
            return
        self._routes, self._checked, self._last_modified = routes, checked, last_modified

    def _save_cache(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        data = {
            "checked": self._checked,
            "last_modified": self._last_modified,
            "routes": {rid: list(v) for rid, v in self._routes.items()},
        }
        tmp = self._cache_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self._cache_file)

    def label(self, route_id: str | None) -> str:
        """Display name of a line: GTFS short/long name, else the pattern, else the id."""
        if route_id is None:
            return ""
        entry = self._routes.get(route_id)
        if entry:
            return entry[0]
        if self._pattern:
            m = self._pattern.search(route_id)
            if m:
                return m.group("label")
        return route_id

    def mode(self, feed: Feed, route_id: str | None) -> str:
        """Mode of a line from GTFS; the feed's own mode when the line is unknown."""
        entry = self._routes.get(route_id) if route_id is not None else None
        return entry[1] if entry else feed.mode

    def modes(self) -> set[str]:
        """Every mode that occurs in the loaded GTFS routes (empty without a mapping)."""
        return {mode for _, mode in self._routes.values()}

    def refresh(self) -> None:
        """Re-download the static feed if the cache is over 24 h old. Never raises."""
        url = self._city.static_gtfs
        if not url:
            return
        try:
            with self._lock:
                self._refresh(url)
        except Exception as exc:  # background job: must not take the service down
            log.warning("route refresh for %s failed: %s", self._city.id, exc)

    def _refresh(self, url: str) -> None:
        now = self._clock()
        if now - self._checked < REFRESH_INTERVAL:
            return
        headers = {"User-Agent": USER_AGENT}
        if self._last_modified and self._routes:
            headers["If-Modified-Since"] = self._last_modified
        session = self._session or requests
        resp = session.get(url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT)
        try:
            if resp.status_code == 304:
                self._checked = now
                self._save_cache()
                return
            resp.raise_for_status()
            self._dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix="gtfs-", suffix=".zip.tmp")
            try:
                with os.fdopen(fd, "wb") as tmp:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        tmp.write(chunk)
                routes = _read_routes(Path(tmp_name))
            finally:
                Path(tmp_name).unlink(missing_ok=True)
            last_modified = resp.headers.get("Last-Modified")
        finally:
            resp.close()
        self._routes = routes  # atomic swap; readers see the old or the new dict
        self._checked = now
        self._last_modified = last_modified
        self._save_cache()
        log.info("loaded %d routes for %s", len(routes), self._city.id)


class RouteRefresher:
    """Daemon thread that calls ``routes.refresh()`` at start and then every *interval* s.

    Recording never waits for it. ``stop()`` does not wait for a download in progress
    (the thread is a daemon and the cached file is replaced atomically).
    """

    def __init__(self, routes: RouteInfo, city_id: str, *, interval: float = CHECK_INTERVAL) -> None:
        self._routes = routes
        self._city_id = city_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"routes-{self._city_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._routes.refresh()  # never raises
            self._stop.wait(self._interval)


def _read_routes(zip_path: Path) -> dict[str, tuple[str, str]]:
    with zipfile.ZipFile(zip_path) as zf, zf.open("routes.txt") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        routes: dict[str, tuple[str, str]] = {}
        for row in csv.DictReader(text):
            rid = (row.get("route_id") or "").strip()
            if not rid:
                continue
            name = (row.get("route_short_name") or "").strip() or (
                row.get("route_long_name") or ""
            ).strip() or rid
            try:
                mode = MODE_BY_ROUTE_TYPE.get(int(row.get("route_type") or ""), "other")
            except ValueError:
                mode = "other"
            routes[rid] = (name, mode)
    return routes
