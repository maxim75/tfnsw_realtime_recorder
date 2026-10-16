import json
from pathlib import Path

import pytest

from recorder.cities import load_cities
from recorder.config import (
    ConfigError,
    EnvConfig,
    Settings,
    SettingsStore,
    derive_region,
    validate_settings,
)

BASE_ENV = {"TFNSW_API_KEY": "k", "ADMIN_USER": "admin", "ADMIN_PASSWORD": "pw"}


def test_env_without_s3_disables_uploads():
    cfg = EnvConfig.from_env(BASE_ENV)
    assert cfg.s3 is None
    assert cfg.data_dir == Path("/data")
    assert cfg.port == 8729


def test_env_missing_required_vars_lists_them():
    with pytest.raises(ConfigError) as exc:
        EnvConfig.from_env({})
    assert "ADMIN_USER" in str(exc.value) and "ADMIN_PASSWORD" in str(exc.value)
    assert "TFNSW_API_KEY" not in str(exc.value)


def test_env_without_tfnsw_key_is_fine():
    cfg = EnvConfig.from_env({"ADMIN_USER": "admin", "ADMIN_PASSWORD": "pw"})
    assert not hasattr(cfg, "tfnsw_api_key")  # cities.toml reads it per city


def test_env_bucket_without_keys_is_an_error():
    with pytest.raises(ConfigError) as exc:
        EnvConfig.from_env({**BASE_ENV, "S3_BUCKET": "b"})
    assert "S3_ACCESS_KEY_ID" in str(exc.value)


def test_env_hetzner_s3():
    cfg = EnvConfig.from_env(
        {
            **BASE_ENV,
            "S3_BUCKET": "b",
            "S3_ACCESS_KEY_ID": "id",
            "S3_SECRET_ACCESS_KEY": "secret",
            "S3_ENDPOINT_URL": "https://nbg1.your-objectstorage.com",
            "DATA_DIR": "/tmp/x",
            "PORT": "9000",
        }
    )
    assert cfg.s3.region == "nbg1"
    assert cfg.data_dir == Path("/tmp/x")
    assert cfg.port == 9000


def test_derive_region():
    assert derive_region("https://nbg1.your-objectstorage.com") == "nbg1"
    assert derive_region("https://fsn1.your-objectstorage.com/") == "fsn1"
    assert derive_region("https://s3.example.com") == "us-east-1"
    assert derive_region(None) == "us-east-1"


CITIES = load_cities(env={})

# Sydney's settings before multi-city: the defaults must stay the same.
SYDNEY_DEFAULTS = Settings(feeds=(
    "buses", "sydneytrains", "metro", "nswtrains", "ferries/sydneyferries", "ferries/MFF",
    "lightrail/cbdandsoutheast", "lightrail/innerwest", "lightrail/parramatta", "regionbuses/sydneysurrounds",
), interval=30)


def test_validate_settings_accepts_known_feeds():
    s = validate_settings({"feeds": ["metro", "buses"], "interval": 45}, CITIES["sydney"])
    assert s.feeds == ("buses", "metro")  # the city's feed order
    assert s.interval == 45


@pytest.mark.parametrize(
    "data",
    [
        {"feeds": ["nope"], "interval": 30},
        {"feeds": ["buses", "buses"], "interval": 30},
        {"feeds": ["buses"], "interval": 9},
        {"feeds": ["buses"], "interval": 3601},
        {"feeds": ["buses"], "interval": True},
        {"feeds": ["buses"], "interval": "30"},
        {"feeds": "buses", "interval": 30},
        {"interval": 30},
        [],
    ],
)
def test_validate_settings_rejects(data):
    with pytest.raises(ValueError):
        validate_settings(data, CITIES["sydney"])


def test_validate_settings_is_per_city():
    assert validate_settings({"feeds": ["all"], "interval": 30}, CITIES["prague"]).feeds == ("all",)
    with pytest.raises(ValueError, match="unknown feeds: all"):
        validate_settings({"feeds": ["all"], "interval": 30}, CITIES["sydney"])
    with pytest.raises(TypeError):
        validate_settings({"feeds": ["buses"], "interval": 30})  # no fallback feed list


def store_at(path):
    return SettingsStore(path, CITIES)


def test_store_defaults_when_missing(tmp_path):
    store = store_at(tmp_path / "settings.json")
    assert store.get("sydney") == SYDNEY_DEFAULTS
    assert store.get("sydney").interval == 30
    assert store.get("prague").feeds == CITIES["prague"].default_feeds
    assert store.get("prague").interval == CITIES["prague"].interval


def test_store_unknown_city(tmp_path):
    store = store_at(tmp_path / "settings.json")
    with pytest.raises(KeyError):
        store.get("atlantis")
    with pytest.raises(KeyError):
        store.update("atlantis", {"feeds": [], "interval": 30})
    with pytest.raises(KeyError):
        store.for_city("atlantis")


def test_store_update_persists_per_city(tmp_path):
    path = tmp_path / "settings.json"
    store_at(path).update("sydney", {"feeds": ["buses"], "interval": 60})
    assert json.loads(path.read_text()) == {"cities": {"sydney": {"feeds": ["buses"], "interval": 60}}}
    assert store_at(path).get("sydney").feeds == ("buses",)
    assert store_at(path).get("prague").feeds == CITIES["prague"].default_feeds
    assert not (tmp_path / "settings.json.tmp").exists()


def test_store_update_keeps_other_cities(tmp_path):
    path = tmp_path / "settings.json"
    store = store_at(path)
    store.update("sydney", {"feeds": ["buses"], "interval": 60})
    store.update("prague", {"feeds": ["all"], "interval": 20})
    assert set(json.loads(path.read_text())["cities"]) == {"sydney", "prague"}
    reloaded = store_at(path)
    assert reloaded.get("sydney").interval == 60
    assert reloaded.get("prague").interval == 20


def test_store_for_city_view(tmp_path):
    view = store_at(tmp_path / "settings.json").for_city("prague")
    assert view.update({"feeds": ["all"], "interval": 15}).interval == 15
    assert view.get().interval == 15


def test_store_validates_against_the_city(tmp_path):
    store = store_at(tmp_path / "settings.json")
    assert store.update("prague", {"feeds": ["all"], "interval": 30}).feeds == ("all",)
    with pytest.raises(ValueError):
        store.update("prague", {"feeds": ["buses"], "interval": 30})
    with pytest.raises(ValueError):
        store.update("sydney", {"feeds": ["all"], "interval": 30})


def test_store_orders_feeds_as_the_city_config(tmp_path):
    ids = [f.id for f in CITIES["sydney"].feeds]
    got = store_at(tmp_path / "settings.json").update("sydney", {"feeds": ids[::-1], "interval": 30})
    assert got.feeds == tuple(ids)


def test_store_reads_legacy_flat_file_and_rewrites_it(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"feeds": ["metro"], "interval": 45}')
    store = store_at(path)
    assert store.get("sydney").feeds == ("metro",)
    assert store.get("sydney").interval == 45
    assert store.get("prague").feeds == CITIES["prague"].default_feeds
    store.update("prague", {"feeds": ["all"], "interval": 20})
    assert json.loads(path.read_text()) == {
        "cities": {
            "sydney": {"feeds": ["metro"], "interval": 45},
            "prague": {"feeds": ["all"], "interval": 20},
        }
    }


def test_store_update_invalid_keeps_old(tmp_path):
    store = store_at(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update("sydney", {"feeds": ["nope"], "interval": 30})
    assert store.get("sydney") == SYDNEY_DEFAULTS


@pytest.mark.parametrize("content", ["{not json", '{"feeds": ["nope"], "interval": 30}', "[]"])
def test_store_corrupted_file_uses_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    assert store_at(path).get("sydney") == SYDNEY_DEFAULTS


def test_store_invalid_city_entry_only_affects_that_city(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"cities": {
        "sydney": {"feeds": ["nope"], "interval": 30},
        "prague": {"feeds": ["all"], "interval": 20},
    }}))
    store = store_at(path)
    assert store.get("sydney") == SYDNEY_DEFAULTS
    assert store.get("prague").interval == 20
