import json
from pathlib import Path

import pytest

from recorder.config import (
    DEFAULT_SETTINGS,
    ConfigError,
    EnvConfig,
    SettingsStore,
    derive_region,
    validate_settings,
)
from recorder.feeds import DEFAULT_FEEDS

BASE_ENV = {"TFNSW_API_KEY": "k", "ADMIN_USER": "admin", "ADMIN_PASSWORD": "pw"}


def test_env_without_s3_disables_uploads():
    cfg = EnvConfig.from_env(BASE_ENV)
    assert cfg.s3 is None
    assert cfg.data_dir == Path("/data")
    assert cfg.port == 8729


def test_env_missing_required_vars_lists_them():
    with pytest.raises(ConfigError) as exc:
        EnvConfig.from_env({"TFNSW_API_KEY": "k"})
    assert "ADMIN_USER" in str(exc.value) and "ADMIN_PASSWORD" in str(exc.value)


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
    assert cfg.s3.prefix == "vehiclepos"
    assert cfg.data_dir == Path("/tmp/x")
    assert cfg.port == 9000


def test_derive_region():
    assert derive_region("https://nbg1.your-objectstorage.com") == "nbg1"
    assert derive_region("https://fsn1.your-objectstorage.com/") == "fsn1"
    assert derive_region("https://s3.example.com") == "us-east-1"
    assert derive_region(None) == "us-east-1"


def test_validate_settings_accepts_known_feeds():
    s = validate_settings({"feeds": ["metro", "buses"], "interval": 45})
    assert s.feeds == ("buses", "metro")  # KNOWN_FEEDS order
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
        validate_settings(data)


def test_store_defaults_when_missing(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.get() == DEFAULT_SETTINGS
    assert store.get().feeds == DEFAULT_FEEDS
    assert store.get().interval == 30


def test_store_update_persists(tmp_path):
    path = tmp_path / "settings.json"
    SettingsStore(path).update({"feeds": ["buses"], "interval": 60})
    assert json.loads(path.read_text()) == {"feeds": ["buses"], "interval": 60}
    assert SettingsStore(path).get().feeds == ("buses",)
    assert not (tmp_path / "settings.json.tmp").exists()


def test_store_update_invalid_keeps_old(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.update({"feeds": ["nope"], "interval": 30})
    assert store.get() == DEFAULT_SETTINGS


@pytest.mark.parametrize("content", ["{not json", '{"feeds": ["nope"], "interval": 30}'])
def test_store_corrupted_file_uses_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    assert SettingsStore(path).get() == DEFAULT_SETTINGS
