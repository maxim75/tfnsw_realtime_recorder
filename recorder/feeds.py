"""Feed helpers shared by the recorder, history and exports (feeds are in cities.toml)."""

# A feed whose newest GPS report is older than this is flagged stale.
STALE_AFTER_S = 600


def feed_dir_name(feed: str) -> str:
    """Directory/file-name form of a feed path: ``ferries/MFF`` -> ``ferries_MFF``."""
    return feed.strip("/").replace("/", "_")
