"""In-memory live view of the latest vehicle positions, for the /map page.

The recorder publishes every successful poll here. The hub keeps a few recent
reports per vehicle (so a freshly opened map can interpolate straight away) and
a short list of numbered batches of new reports that the SSE stream sends on.

A report is a compact list: ``[vehicle_id, route_id, gps_epoch_s, lon, lat,
bearing, speed, label, mode]`` (line name and mode from the city's `RouteInfo`).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

from .cities import City
from .feeds import STALE_AFTER_S
from .routes import RouteInfo


class LiveHub:
    def __init__(
        self, city: City, routes: RouteInfo, *, history: int = 6, max_batches: int = 50, forget_after_s: float = 900
    ) -> None:
        self._city = city
        self._routes = routes
        self._history = history
        self._forget_after_s = forget_after_s
        self._lock = threading.Lock()
        self._vehicles: dict[str, dict[str, deque[list]]] = {}  # feed -> vehicle -> reports
        self._batches: deque[dict] = deque(maxlen=max_batches)
        self._seq = 0
        self._closed = False

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Ends open SSE streams once they have sent what is queued (used at shutdown)."""
        self._closed = True

    def publish(self, feed: str, fetched_at: datetime, positions) -> None:
        now = fetched_at.timestamp()
        feed_info = self._city.feed(feed)
        new = []
        with self._lock:
            vehicles = self._vehicles.setdefault(feed, {})
            for p in positions:
                if not p.timestamp or (p.latitude == 0.0 and p.longitude == 0.0):
                    continue
                ts = int(p.timestamp.timestamp())
                if now - ts > STALE_AFTER_S:
                    continue  # frozen or replayed fix: nothing to animate
                vid = p.vehicle_id or p.trip_id or f"{p.latitude},{p.longitude}"
                history = vehicles.setdefault(vid, deque(maxlen=self._history))
                if history and history[-1][2] >= ts:
                    continue  # unchanged (or out-of-order) report
                report = [
                    vid, p.route_id or "", ts, round(p.longitude, 5), round(p.latitude, 5),
                    p.bearing, None if p.speed is None else round(p.speed, 1),
                    self._routes.label(p.route_id), self._routes.mode(feed_info, p.route_id),
                ]
                history.append(report)
                new.append(report)
            for vid in [v for v, h in vehicles.items() if now - h[-1][2] > self._forget_after_s]:
                del vehicles[vid]
            if new:
                self._seq += 1
                self._batches.append({"seq": self._seq, "feed": feed, "reports": new})

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "seq": self._seq,
                "feeds": {
                    feed: sorted((r for h in vehicles.values() for r in h), key=lambda r: r[2])
                    for feed, vehicles in self._vehicles.items()
                },
            }

    def batches_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [b for b in self._batches if b["seq"] > seq]
