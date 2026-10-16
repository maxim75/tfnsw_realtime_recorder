"""10-minute recording windows in a city's local time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WINDOW = timedelta(minutes=10)


def window_start(dt: datetime, tz: ZoneInfo) -> datetime:
    """Start of the 10-minute window containing aware datetime *dt*, in zone *tz*.

    UTC offsets change on whole hours (true for Sydney and Prague), so flooring
    the local minute never crosses an offset change.
    """
    local = dt.astimezone(tz)
    return local.replace(minute=local.minute - local.minute % 10, second=0, microsecond=0)


def window_end(start: datetime) -> datetime:
    """Start of the next window, in *start*'s zone (arithmetic in UTC so DST changes are handled)."""
    return (start.astimezone(timezone.utc) + WINDOW).astimezone(start.tzinfo)


def window_label(start: datetime) -> str:
    """File-name label, e.g. ``2026-10-16T12-10+1100``. The offset keeps the
    repeated hour at the end of daylight saving unique."""
    return start.strftime("%Y-%m-%dT%H-%M%z")
