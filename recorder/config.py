"""Environment configuration and runtime settings (settings.json)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .feeds import DEFAULT_FEEDS, KNOWN_FEEDS

log = logging.getLogger(__name__)

MIN_INTERVAL = 10
MAX_INTERVAL = 3600

_HETZNER_ENDPOINT = re.compile(r"^https?://([a-z0-9-]+)\.your-objectstorage\.com/?$")


class ConfigError(Exception):
    """Missing or inconsistent environment configuration."""


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader; never overrides variables already set."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def derive_region(endpoint_url: str | None) -> str:
    """Hetzner's region is the location in the endpoint host (e.g. ``nbg1``)."""
    match = _HETZNER_ENDPOINT.match(endpoint_url or "")
    return match.group(1) if match else "us-east-1"


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str | None
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "S3Config | None":
        """S3 settings alone (for tools that don't run the recorder); None if no bucket."""
        s3, missing = _s3_from_env(os.environ if env is None else env)
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))
        return s3


def _s3_from_env(env: Mapping[str, str]) -> tuple[S3Config | None, list[str]]:
    get = lambda name: env.get(name, "").strip()  # noqa: E731
    if not get("S3_BUCKET"):
        return None, []
    missing = [n for n in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY") if not get(n)]
    endpoint = get("S3_ENDPOINT_URL") or None
    s3 = S3Config(
        endpoint_url=endpoint,
        region=get("S3_REGION") or derive_region(endpoint),
        bucket=get("S3_BUCKET"),
        access_key_id=get("S3_ACCESS_KEY_ID"),
        secret_access_key=get("S3_SECRET_ACCESS_KEY"),
        prefix=get("S3_PREFIX").strip("/") or "vehiclepos",
    )
    return s3, missing


@dataclass(frozen=True)
class EnvConfig:
    tfnsw_api_key: str
    admin_user: str
    admin_password: str
    data_dir: Path
    port: int
    s3: S3Config | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EnvConfig":
        env = os.environ if env is None else env
        get = lambda name: env.get(name, "").strip()  # noqa: E731

        missing = [n for n in ("TFNSW_API_KEY", "ADMIN_USER", "ADMIN_PASSWORD") if not get(n)]
        s3, s3_missing = _s3_from_env(env)
        missing += s3_missing
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))

        return cls(
            tfnsw_api_key=get("TFNSW_API_KEY"),
            admin_user=get("ADMIN_USER"),
            admin_password=get("ADMIN_PASSWORD"),
            data_dir=Path(get("DATA_DIR") or "/data"),
            port=int(get("PORT") or 8729),
            s3=s3,
        )


@dataclass(frozen=True)
class Settings:
    feeds: tuple[str, ...]
    interval: float

    def to_dict(self) -> dict:
        return {"feeds": list(self.feeds), "interval": self.interval}


DEFAULT_SETTINGS = Settings(feeds=DEFAULT_FEEDS, interval=30)


def validate_settings(data: object) -> Settings:
    """Validate a settings mapping; raises ``ValueError`` with a user-facing message."""
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    feeds = data.get("feeds")
    interval = data.get("interval")
    if not isinstance(feeds, list) or not all(isinstance(f, str) for f in feeds):
        raise ValueError("feeds must be a list of feed names")
    unknown = sorted(set(feeds) - set(KNOWN_FEEDS))
    if unknown:
        raise ValueError("unknown feeds: " + ", ".join(unknown))
    if len(set(feeds)) != len(feeds):
        raise ValueError("feeds must not repeat")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise ValueError("interval must be a number of seconds")
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ValueError(f"interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds")
    return Settings(feeds=tuple(f for f in KNOWN_FEEDS if f in feeds), interval=interval)


class SettingsStore:
    """Thread-safe holder of the current settings, persisted to a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> Settings:
        if not self._path.exists():
            return DEFAULT_SETTINGS
        try:
            return validate_settings(json.loads(self._path.read_text()))
        except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
            log.error("Ignoring invalid %s (%s); using defaults", self._path, exc)
            return DEFAULT_SETTINGS

    def get(self) -> Settings:
        with self._lock:
            return self._settings

    def update(self, data: object) -> Settings:
        settings = validate_settings(data)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            with tmp.open("w") as f:
                json.dump(settings.to_dict(), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            self._settings = settings
        return settings
