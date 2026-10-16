"""10-minute recording windows in Sydney local time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WINDOW = timedelta(minutes=10)


def window_start(dt: datetime) -> datetime:
    """Start of the 10-minute window containing aware datetime *dt*, in Sydney time.

    Sydney's UTC offset only changes on the hour, so flooring the local minute
    never crosses an offset change.
    """
    local = dt.astimezone(SYDNEY_TZ)
    return local.replace(minute=local.minute - local.minute % 10, second=0, microsecond=0)


def window_end(start: datetime) -> datetime:
    """Start of the next window (arithmetic in UTC so DST changes are handled)."""
    return (start.astimezone(timezone.utc) + WINDOW).astimezone(SYDNEY_TZ)


def window_label(start: datetime) -> str:
    """File-name label, e.g. ``2026-10-16T12-10+1100``. The offset keeps the
    repeated hour at the end of daylight saving unique."""
    return start.strftime("%Y-%m-%dT%H-%M%z")
