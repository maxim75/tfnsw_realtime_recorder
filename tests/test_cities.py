from pathlib import Path

import pytest

from recorder.cities import City, Feed, load_cities
from recorder.feeds import DEFAULT_FEEDS, KNOWN_FEEDS, feed_version

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


def test_loads_sydney_and_prague_from_repo_file():
    cities = load_cities(env=KEY_ENV)
    assert list(cities) == ["sydney", "prague"]
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


def test_sydney_feed_urls_and_modes_match_feeds_module():
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
