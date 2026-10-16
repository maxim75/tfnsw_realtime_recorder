"""Generic GTFS-Realtime vehicle position client."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

USER_AGENT = "tfnsw-realtime-recorder"


@dataclass
class Position:
    vehicle_id: str | None
    trip_id: str | None
    route_id: str | None
    latitude: float
    longitude: float
    bearing: float | None
    speed: float | None
    timestamp: datetime | None  # aware UTC


class FetchError(Exception):
    """A feed could not be fetched or decoded. Never contains header values."""


def _position(entity) -> Position | None:
    if not entity.HasField("vehicle"):
        return None
    vp = entity.vehicle
    if not vp.HasField("position"):
        return None
    pos = vp.position
    return Position(
        vehicle_id=vp.vehicle.id or None,
        trip_id=vp.trip.trip_id or None,
        route_id=vp.trip.route_id or None,
        latitude=pos.latitude,
        longitude=pos.longitude,
        bearing=pos.bearing if pos.HasField("bearing") else None,
        speed=pos.speed if pos.HasField("speed") else None,
        timestamp=datetime.fromtimestamp(vp.timestamp, tz=timezone.utc) if vp.timestamp else None,
    )


class GtfsRtClient:
    def __init__(self, timeout: float = 8, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch(self, url: str, headers: Mapping[str, str]) -> list[Position]:
        sent = dict(headers)
        if not any(k.lower() == "user-agent" for k in sent):
            sent["User-Agent"] = USER_AGENT
        try:
            response = self.session.get(url, headers=sent, timeout=self.timeout)
        except requests.RequestException as e:
            raise FetchError(f"request failed: {type(e).__name__}") from None
        if not 200 <= response.status_code < 300:
            raise FetchError(f"HTTP {response.status_code}")
        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(response.content)
        except DecodeError as e:
            raise FetchError(f"invalid GTFS-Realtime payload: {e}") from None
        return [p for p in map(_position, feed.entity) if p is not None]
