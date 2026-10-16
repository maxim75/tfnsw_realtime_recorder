"""Known GTFS-Realtime vehicle position feeds and TfNSW API limits."""

# Every gtfs/vehiclepos feed that answered on 2026-10-16.
KNOWN_FEEDS: tuple[str, ...] = (
    "buses",
    "sydneytrains",
    "metro",
    "nswtrains",
    "ferries/sydneyferries",
    "ferries/MFF",
    "lightrail/cbdandsoutheast",
    "lightrail/innerwest",
    "lightrail/newcastle",
    "lightrail/parramatta",
    "regionbuses/centralwestandorana",
    "regionbuses/centralwestandorana2",
    "regionbuses/farwest",
    "regionbuses/newcastlehunter",
    "regionbuses/newenglandnorthwest",
    "regionbuses/northcoast",
    "regionbuses/northcoast2",
    "regionbuses/northcoast3",
    "regionbuses/riverinamurray",
    "regionbuses/riverinamurray2",
    "regionbuses/southeasttablelands",
    "regionbuses/southeasttablelands2",
    "regionbuses/sydneysurrounds",
)

# Recorded when no settings file exists yet (Sydney-area services).
DEFAULT_FEEDS: tuple[str, ...] = (
    "buses",
    "sydneytrains",
    "metro",
    "nswtrains",
    "ferries/sydneyferries",
    "ferries/MFF",
    "lightrail/cbdandsoutheast",
    "lightrail/innerwest",
    "lightrail/parramatta",
    "regionbuses/sydneysurrounds",
)

# TfNSW Open Data default limits per API key.
MAX_REQUESTS_PER_SECOND = 5
DAILY_REQUEST_QUOTA = 60_000


def feed_dir_name(feed: str) -> str:
    """Directory/file-name form of a feed path: ``ferries/MFF`` -> ``ferries_MFF``."""
    return feed.strip("/").replace("/", "_")
