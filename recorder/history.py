"""Recorded positions for map playback, served one 10-minute window at a time.

A window's data is the union of its S3 objects (all ``_pN`` parts) and any local
files for it that are not uploaded yet (``complete/``, and ``open/`` for the
window being written), so playback reaches right up to the present.

Response (gzip-compressed JSON), in the live view's report format::

    {"start": <epoch>, "feeds": {"buses": [[vehicle_id, route_id, gps_epoch_s,
                                             lon, lat, bearing, speed], ...], ...}}
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from .export import window_of
from .feeds import KNOWN_FEEDS, STALE_AFTER_S, feed_dir_name
from .playback import read_window
from .uploader import retry_denials
from .windows import SYDNEY_TZ, WINDOW, window_label

log = logging.getLogger(__name__)

WINDOW_S = int(WINDOW.total_seconds())
FINAL_AFTER_S = 900  # after the window ends: rotated, uploaded (with retries); cache for good
LIVE_TTL_S = 30  # cache lifetime for a window that may still change
CACHE_WINDOWS = 24
S3_WORKERS = 8  # feeds read in parallel; a cold day listing is 23 S3 requests


def _decode(data: bytes) -> list[dict[str, str]]:
    """CSV rows from gzip data that may end in a truncated member (file being written)."""
    out = bytearray()
    while data:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out += d.decompress(data)
        except zlib.error:
            break
        if not d.eof:
            break  # truncated member: keep what decoded
        data = d.unused_data
    text = out.decode("utf-8", errors="ignore")
    text = text[: text.rfind("\n") + 1]  # drop a partial last line
    return list(csv.DictReader(io.StringIO(text)))


def _report(row: dict[str, str]) -> list | None:
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if lat == 0.0 and lon == 0.0 or not row["timestamp"]:
            return None
        ts = int(datetime.fromisoformat(row["timestamp"]).timestamp())
        if datetime.fromisoformat(row["fetched_at"]).timestamp() - ts > STALE_AFTER_S:
            return None
        bearing = float(row["bearing"]) if row["bearing"] else None
        speed = round(float(row["speed"]), 1) if row["speed"] else None
    except (KeyError, ValueError):
        return None
    return [row["vehicle_id"], row["route_id"], ts, round(lon, 5), round(lat, 5), bearing, speed]


def encode_window(start: int, feeds: dict[str, list[list]]) -> bytes:
    return gzip.compress(json.dumps({"start": start, "feeds": feeds}, separators=(",", ":")).encode(), 5)


class History:
    def __init__(
        self,
        client,
        bucket: str | None,
        prefix: str,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        tz: ZoneInfo = SYDNEY_TZ,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._data_dir = data_dir
        self._clock = clock
        self.tz = tz
        self._lock = threading.Lock()
        self._windows: OrderedDict[int, tuple[float | None, bytes]] = OrderedDict()  # start -> (expires, body)
        self._listings: dict[tuple[str, str], tuple[float | None, list[str]]] = {}

    def window(self, start: int) -> bytes:
        if start % WINDOW_S:
            raise ValueError("start must be a 10-minute window boundary (epoch seconds)")
        now = self._clock()
        with self._lock:
            cached = self._windows.get(start)
            if cached and (cached[0] is None or cached[0] > now):
                self._windows.move_to_end(start)
                return cached[1]
        final = now >= start + WINDOW_S + FINAL_AFTER_S
        # A day built with build_playback.py is served from that local file.
        body = read_window(self._data_dir, start, self.tz)
        if body is None:
            body = encode_window(start, self.collect(start))
        with self._lock:
            self._windows[start] = (None if final else now + LIVE_TTL_S, body)
            self._windows.move_to_end(start)
            while len(self._windows) > CACHE_WINDOWS:
                self._windows.popitem(last=False)
        return body

    def collect(self, start: int) -> dict[str, list[list]]:
        """Reports per feed in the window starting at *start*, from S3 and local recordings."""
        window = datetime.fromtimestamp(start, tz=self.tz)
        with ThreadPoolExecutor(max_workers=S3_WORKERS) as pool:
            results = pool.map(lambda feed: (feed, self._feed_reports(feed, window)), KNOWN_FEEDS)
            feeds = {feed: reports for feed, reports in results if reports}
        log.debug("history %s: %s", window_label(window), {f: len(r) for f, r in feeds.items()})
        return feeds

    def _feed_reports(self, feed: str, window: datetime) -> list[list]:
        """*feed*'s reports in *window* from S3 and local files, without repeats, by GPS time."""
        feed_dir, label = feed_dir_name(feed), window_label(window)
        sources = [
            retry_denials(self._client.get_object, Bucket=self._bucket, Key=key)["Body"].read()
            for key in self._keys(feed, window)
        ]
        for kind in ("complete", "open"):
            sources += [p.read_bytes() for p in sorted((self._data_dir / kind / feed_dir).glob(f"{feed_dir}_{label}*.csv.gz"))]
        seen, reports = set(), []
        for data in sources:
            for row in _decode(data):
                report = _report(row)
                if report and (report[0], report[2]) not in seen:
                    seen.add((report[0], report[2]))
                    reports.append(report)
        return sorted(reports, key=lambda r: r[2])

    def _keys(self, feed: str, window: datetime) -> list[str]:
        """S3 keys of *feed*'s parts for *window*; day listings are cached."""
        if self._client is None:
            return []
        day = window.date().isoformat()
        now = self._clock()
        with self._lock:
            cached = self._listings.get((feed, day))
        if not cached or (cached[0] is not None and cached[0] <= now):
            day_prefix = f"feed={feed_dir_name(feed)}/date={day}/"
            kwargs = {"Bucket": self._bucket, "Prefix": f"{self._prefix}/{day_prefix}" if self._prefix else day_prefix}
            keys = []
            while True:
                resp = retry_denials(self._client.list_objects_v2, **kwargs)
                keys += [o["Key"] for o in resp.get("Contents", [])]
                if not resp.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = resp["NextContinuationToken"]
            day_end = datetime.combine(window.date() + timedelta(days=1), dt_time(), tzinfo=self.tz).timestamp()
            cached = (None if now >= day_end + FINAL_AFTER_S else now + LIVE_TTL_S, keys)  # None = never expires
            with self._lock:
                self._listings[(feed, day)] = cached
        return [k for k in cached[1] if window_of(k) == window]
