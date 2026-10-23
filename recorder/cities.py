"""City configuration: feeds, limits, timezone and map defaults, read from cities.toml."""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CITIES_FILE = Path(__file__).with_name("cities.toml")

_CITY_ID = re.compile(r"[a-z0-9-]+")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Feed:
    id: str
    url: str
    mode: str  # fallback mode for vehicles of this feed


@dataclass(frozen=True)
class City:
    id: str
    name: str
    timezone: ZoneInfo
    center: tuple[float, float]  # lon, lat
    zoom: float
    s3_prefix: str
    attribution: str
    interval: float
    max_requests_per_second: float
    daily_quota: int | None
    default_feeds: tuple[str, ...]
    feeds: tuple[Feed, ...]
    header_sets: tuple[dict[str, str], ...]  # one per API key, env expanded; requests rotate through them
    route_label_pattern: str | None = None
    static_gtfs: str | None = None
    disabled_reason: str | None = None  # e.g. "TFNSW_API_KEY is not set"
    _by_id: dict[str, Feed] = field(default_factory=dict, repr=False, compare=False)

    @property
    def headers(self) -> dict[str, str]:
        """The first header set (``{}`` when the city sends none)."""
        return self.header_sets[0] if self.header_sets else {}

    def feed(self, id: str) -> Feed:
        """The feed called *id*; ``KeyError`` when the city has none."""
        return self._by_id[id]


def load_cities(
    path: Path | None = None, env: Mapping[str, str] | None = None
) -> dict[str, City]:
    """Read the city config, ordered as in the file.

    *path* defaults to ``$CITIES_FILE`` or the ``cities.toml`` next to this module.
    ``headers`` is a table or a list of tables (one per API key; requests rotate
    through them). ``max_requests_per_second``/``daily_quota`` are for the city as
    a whole, whatever the number of keys: TfNSW keys of one account share them.
    A header set referencing an unset environment variable is skipped; a city
    left with none loads with ``disabled_reason`` set. Bad config raises ``ValueError``.
    """
    env = os.environ if env is None else env
    if path is None:
        path = Path(env["CITIES_FILE"]) if env.get("CITIES_FILE") else DEFAULT_CITIES_FILE
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return {cid: _parse_city(cid, section, env) for cid, section in raw.items()}


def _parse_city(cid: str, section: dict, env: Mapping[str, str]) -> City:
    if not _CITY_ID.fullmatch(cid):
        raise ValueError(f"bad city id {cid!r}: use lowercase letters, digits and '-'")
    try:
        try:
            tz = ZoneInfo(section["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {section['timezone']!r}") from exc
        feeds = tuple(Feed(f["id"], f["url"], f["mode"]) for f in section["feeds"])
        by_id: dict[str, Feed] = {}
        for feed in feeds:
            if feed.id in by_id:
                raise ValueError(f"duplicate feed id {feed.id!r}")
            by_id[feed.id] = feed
        default_feeds = tuple(section["default_feeds"])
        for feed_id in default_feeds:
            if feed_id not in by_id:
                raise ValueError(f"default feed {feed_id!r} is not a configured feed")
        header_sets, disabled_reason = _expand_header_sets(section.get("headers", {}), env)
        daily_quota = section.get("daily_quota")
        s3_prefix = section["s3_prefix"]
        if cid == "sydney" and env.get("S3_PREFIX"):
            s3_prefix = env["S3_PREFIX"]
        pattern = section.get("route_label", {}).get("pattern")
        if pattern is not None:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"bad route_label pattern {pattern!r}: {exc}") from exc
            if "label" not in compiled.groupindex:
                raise ValueError(f"route_label pattern {pattern!r} needs a named group 'label'")
        lon, lat = section["center"]
        return City(
            id=cid,
            name=section["name"],
            timezone=tz,
            center=(float(lon), float(lat)),
            zoom=float(section["zoom"]),
            s3_prefix=s3_prefix.strip("/"),
            attribution=section["attribution"],
            interval=float(section["interval"]),
            max_requests_per_second=float(section["max_requests_per_second"]),
            daily_quota=daily_quota,
            default_feeds=default_feeds,
            feeds=feeds,
            header_sets=header_sets,
            route_label_pattern=pattern,
            static_gtfs=section.get("static_gtfs"),
            disabled_reason=disabled_reason,
            _by_id=by_id,
        )
    except KeyError as exc:
        raise ValueError(f"city {cid!r}: missing setting {exc.args[0]!r}") from exc
    except ValueError as exc:
        raise ValueError(f"city {cid!r}: {exc}") from exc


def _expand_header_sets(
    headers: Mapping[str, str] | list[Mapping[str, str]], env: Mapping[str, str]
) -> tuple[tuple[dict[str, str], ...], str | None]:
    """Expanded header sets, skipping those with an unset env var; the reason when none is left."""
    if isinstance(headers, Mapping):
        if not headers:
            return (), None
        headers = [headers]
    sets: list[dict[str, str]] = []
    first_missing = None
    for headers_ in headers:
        missing = next((v for value in headers_.values() for v in _ENV_REF.findall(value) if not env.get(v)), None)
        if missing:
            first_missing = first_missing or missing
            continue
        sets.append({name: _ENV_REF.sub(lambda m: env[m.group(1)], value) for name, value in headers_.items()})
    if headers and not sets:
        return (), f"{first_missing} is not set"
    return tuple(sets), None
