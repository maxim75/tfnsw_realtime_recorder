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

# Feeds read from /v2/gtfs/vehiclepos/; every other feed uses /v1/. Checked 2026-10-16:
# sydneytrains and lightrail/innerwest 404 on v1, and v1 metro had not updated in ~2
# years while v2 metro is live. No other feed exists on v2 (ferries/sydneyferries
# answers there but empty and weeks old).
V2_FEEDS: frozenset[str] = frozenset({"sydneytrains", "metro", "lightrail/innerwest"})

# A feed whose newest GPS report is older than this is flagged stale.
STALE_AFTER_S = 600

# TfNSW Open Data default limits per API key.
MAX_REQUESTS_PER_SECOND = 5
DAILY_REQUEST_QUOTA = 60_000


def feed_version(feed: str) -> str:
    """API version (``v1``/``v2``) serving *feed*'s live vehicle positions."""
    return "v2" if feed in V2_FEEDS else "v1"


def feed_dir_name(feed: str) -> str:
    """Directory/file-name form of a feed path: ``ferries/MFF`` -> ``ferries_MFF``."""
    return feed.strip("/").replace("/", "_")
