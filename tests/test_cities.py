from pathlib import Path

import pytest

from recorder.cities import City, Feed, load_cities

from repo_env import REPO_ENV

# Sydney's feeds and defaults before multi-city (every gtfs/vehiclepos feed that answered
# on 2026-10-16); cities.toml must keep them.
KNOWN_FEEDS = (
    "buses", "sydneytrains", "metro", "nswtrains", "ferries/sydneyferries", "ferries/MFF",
    "lightrail/cbdandsoutheast", "lightrail/innerwest", "lightrail/newcastle", "lightrail/parramatta",
    "regionbuses/centralwestandorana", "regionbuses/centralwestandorana2", "regionbuses/farwest",
    "regionbuses/newcastlehunter", "regionbuses/newenglandnorthwest", "regionbuses/northcoast",
    "regionbuses/northcoast2", "regionbuses/northcoast3", "regionbuses/riverinamurray",
    "regionbuses/riverinamurray2", "regionbuses/southeasttablelands", "regionbuses/southeasttablelands2",
    "regionbuses/sydneysurrounds",
)
DEFAULT_FEEDS = (
    "buses", "sydneytrains", "metro", "nswtrains", "ferries/sydneyferries", "ferries/MFF",
    "lightrail/cbdandsoutheast", "lightrail/innerwest", "lightrail/parramatta", "regionbuses/sydneysurrounds",
)
# Read from /v2/: sydneytrains and lightrail/innerwest 404 on v1, v1 metro is frozen.
V2_FEEDS = {"sydneytrains", "metro", "lightrail/innerwest"}


def feed_version(feed):
    return "v2" if feed in V2_FEEDS else "v1"

KEY_ENV = {"TFNSW_API_KEY": "secret"}


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cities.toml"
    path.write_text(text)
    return path


MINIMAL = """
[{id}]
name = "X"
timezone = "{tz}"
center = [1.0, 2.0]
zoom = 5
s3_prefix = "p"
attribution = "a"
interval = 10
max_requests_per_second = 1
default_feeds = {default}

  [[{id}.feeds]]
  id = "{feed}"
  url = "https://example.test/a"
  mode = "bus"
{extra}
"""


def _minimal(id="x", tz="Europe/Prague", default='["a"]', feed="a", extra=""):
    return MINIMAL.format(id=id, tz=tz, default=default, feed=feed, extra=extra)


REPO_CITIES = load_cities(env=REPO_ENV)


def test_repo_file_keeps_sydney_first_and_prague():
    cities = REPO_CITIES
    assert list(cities)[0] == "sydney"  # the default city (first in the picker)
    sydney, prague = cities["sydney"], cities["prague"]
    assert sydney.timezone.key == "Australia/Sydney"
    assert prague.timezone.key == "Europe/Prague"
    assert [f.id for f in sydney.feeds] == list(KNOWN_FEEDS)
    assert sydney.default_feeds == DEFAULT_FEEDS
    assert sydney.s3_prefix == "vehiclepos"
    assert sydney.daily_quota == 60000 and prague.daily_quota is None
    assert sydney.max_requests_per_second == 5 and prague.max_requests_per_second == 1
    assert sydney.center == (151.05, -33.85)
    assert sydney.route_label_pattern == r"_(?P<label>[^_]+)$"
    assert prague.s3_prefix == "vehiclepos/city=prague"
    assert prague.static_gtfs == "https://data.pid.cz/PID_GTFS.zip"
    assert prague.default_feeds == ("all",)
    assert prague.feed("all").mode == "bus"
    assert prague.headers == {}
    assert prague.disabled_reason is None


def test_repo_file_kyiv():
    kyiv = REPO_CITIES["kyiv"]
    assert kyiv.timezone.key == "Europe/Kyiv"
    assert kyiv.s3_prefix == "vehiclepos/city=kyiv"
    assert kyiv.static_gtfs.startswith("https://data.kyivcity.gov.ua/")
    assert kyiv.feed("all").url.startswith("https://data.kyivcity.gov.ua/")
    assert kyiv.default_feeds == ("all",) and kyiv.headers == {} and kyiv.disabled_reason is None


# -- checks every city in the repo file, so a newly added city is covered too ------

def test_repo_cities_have_distinct_s3_prefixes():
    prefixes = [c.s3_prefix for c in REPO_CITIES.values()]
    assert len(set(prefixes)) == len(prefixes)


@pytest.mark.parametrize("city", list(REPO_CITIES.values()), ids=list(REPO_CITIES))
def test_repo_city_is_well_formed(city):
    if city.id != "sydney":  # Sydney keeps the pre-multi-city prefix
        assert city.s3_prefix == f"vehiclepos/city={city.id}"
    assert city.name and city.attribution
    lon, lat = city.center
    assert -180 <= lon <= 180 and -90 <= lat <= 90
    assert 3 <= city.zoom <= 16
    assert city.interval >= 10 and city.max_requests_per_second > 0
    assert city.feeds and city.default_feeds
    for feed in city.feeds:
        assert feed.url.startswith(("https://", "http://")) and feed.mode
    if city.static_gtfs:
        assert city.static_gtfs.startswith(("https://", "http://"))
    assert city.disabled_reason is None


def test_sydney_feed_urls_and_modes_match_the_legacy_feed_list():
    sydney = load_cities(env=KEY_ENV)["sydney"]
    for feed in sydney.feeds:
        assert feed.url == (
            f"https://api.transport.nsw.gov.au/{feed_version(feed.id)}/gtfs/vehiclepos/{feed.id}"
        )
    modes = {f.id: f.mode for f in sydney.feeds}
    assert modes["buses"] == "bus"
    assert modes["regionbuses/farwest"] == "bus"
    assert modes["sydneytrains"] == modes["nswtrains"] == "rail"
    assert modes["metro"] == "metro"
    assert modes["ferries/MFF"] == "ferry"
    assert modes["lightrail/innerwest"] == "tram"
    assert isinstance(sydney.feed("buses"), Feed)
    with pytest.raises(KeyError):
        sydney.feed("nope")


def test_expands_env_in_headers():
    sydney = load_cities(env=KEY_ENV)["sydney"]
    assert isinstance(sydney, City)
    assert sydney.headers == {"Authorization": "apikey secret"}
    assert sydney.disabled_reason is None


@pytest.mark.parametrize("env", [{}, {"TFNSW_API_KEY": ""}])
def test_missing_env_disables_city_only(env):
    cities = load_cities(env=env)
    assert cities["sydney"].disabled_reason == "TFNSW_API_KEY is not set"
    assert cities["sydney"].headers == {}
    assert cities["prague"].disabled_reason is None


def test_s3_prefix_env_overrides_sydney_prefix():
    cities = load_cities(env={**KEY_ENV, "S3_PREFIX": "/x/"})
    assert cities["sydney"].s3_prefix == "x"
    assert cities["prague"].s3_prefix == "vehiclepos/city=prague"


def test_cities_file_env_selects_file(tmp_path):
    path = _write(tmp_path, _minimal())
    cities = load_cities(env={"CITIES_FILE": str(path)})
    assert list(cities) == ["x"]
    assert cities["x"].route_label_pattern is None and cities["x"].static_gtfs is None
    assert cities["x"].daily_quota is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_minimal(default='["zzz"]'), "zzz"),
        (_minimal(tz="Mars/Base"), "Mars/Base"),
        (_minimal(extra='  [[x.feeds]]\n  id = "a"\n  url = "u"\n  mode = "bus"\n'), "duplicate"),
        (_minimal(id="Bad_City"), "Bad_City"),
    ],
)
def test_rejects_bad_config(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_cities(_write(tmp_path, text), env={})


@pytest.mark.parametrize("pattern", ["(unclosed", "_(?P<name>[^_]+)$", "_([^_]+)$"])
def test_route_label_pattern_must_compile_with_a_label_group(tmp_path, pattern):
    path = _write(tmp_path, _minimal(extra=f"[x.route_label]\npattern = '{pattern}'\n"))
    with pytest.raises(ValueError, match="city 'x'"):
        load_cities(path, env={})
