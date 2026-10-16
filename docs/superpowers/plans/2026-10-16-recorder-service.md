# Recorder Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the CLI vehicle-position recorder into a Docker service that writes 10-minute gzipped CSV files per feed, uploads finished files to Hetzner Object Storage, and serves a basic-auth control page.

**Architecture:** One uvicorn process. A FastAPI app factory (`recorder.web:create_app`) wires a `SettingsStore` (JSON on the data volume), a `Recorder` thread (polls TfNSW, writes via one `RotatingWriter` per feed) and an `Uploader` thread (queue → S3 → delete local). Threads start and stop in the FastAPI lifespan.

**Tech Stack:** Python 3.12, uv, `tfnsw-trip-planner[realtime]` 1.4.0, FastAPI, uvicorn, boto3, pytest + httpx (`TestClient`), Docker (`python:3.12-slim`, `ghcr.io/astral-sh/uv:0.9.15`), Coolify.

**Spec:** `docs/superpowers/specs/2026-10-16-recorder-service-design.md`

## Global Constraints

- Window = 10 minutes, floored in `Australia/Sydney`; rows go to the window of the poll's `fetched_at`.
- Window label format `%Y-%m-%dT%H-%M%z`, e.g. `2026-10-16T12-10+1100`; file name `<feed_dir>_<label>[_pN].csv.gz`; `feed_dir` = feed with `/` → `_`.
- CSV columns, in order: `fetched_at,timestamp,vehicle_id,trip_id,route_id,latitude,longitude,bearing,speed`.
- Disk layout: `DATA_DIR/settings.json`, `DATA_DIR/open/<feed_dir>/`, `DATA_DIR/complete/<feed_dir>/`.
- S3 key: `{S3_PREFIX}/feed={feed_dir}/date={YYYY-MM-DD}/{file name}`; `ContentType: application/gzip`; never `ContentEncoding`.
- An existing S3 object is never overwritten: if the key exists, upload as the next free `_pN` (this realises the spec's "nothing is overwritten" across restarts, when the earlier part is already uploaded and deleted locally).
- boto3 client: `request_checksum_calculation="when_required"`, `response_checksum_validation="when_required"`.
- Upload retry backoff: start 5 s, double, cap 300 s, reset on success.
- Settings: `interval` number in `[10, 3600]` (not bool); `feeds` unique, each in `KNOWN_FEEDS`; defaults = 10 `DEFAULT_FEEDS`, 30 s.
- Rate limit spacing 0.2 s between TfNSW requests; quota constant 60,000/day.
- Env vars: `TFNSW_API_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD` required; `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_PREFIX` (default `vehiclepos`), `DATA_DIR` (default `/data`; `main.py` defaults it to `./data`), `PORT` (default `8000`).
- Region: `S3_REGION`, else `<loc>` from `https://<loc>.your-objectstorage.com`, else `us-east-1`. This deployment: `nbg1`.
- All routes except `/healthz` need basic auth (`secrets.compare_digest`).
- Shutdown: stop recorder → close writers → uploader drains ≤ 20 s.
- Secrets are never logged or returned by the API.
- Tests run with `uv run pytest`; no network in unit tests.

## Review Focus

1. Restart in the middle of a window after the first part was already uploaded → the new part must upload as `_p2`, not overwrite the S3 object (test in Task 4).
2. Feed disabled while the interval is long (e.g. 600 s) → its file is closed and queued within ~1 s, not at the next poll (test in Task 5).
3. `settings.json` hand-edited into invalid JSON or invalid values → service starts with defaults instead of crashing (test in Task 2).
4. S3 unreachable for a long time → files stay on disk, are retried, and upload once S3 is back; nothing is deleted on failure (test in Task 4).
5. End of daylight saving (repeated 02:xx hour) → two windows with the same clock time get different file names (test in Task 1).

---

## File Structure

| File | Responsibility |
|---|---|
| `recorder/__init__.py` | package marker |
| `recorder/feeds.py` | feed lists, `feed_dir_name`, API limit constants |
| `recorder/windows.py` | 10-minute window maths and labels (pure) |
| `recorder/config.py` | `.env` loader, `EnvConfig`/`S3Config` from env, `Settings` validation, `SettingsStore` |
| `recorder/writer.py` | `FIELDS`, `RotatingWriter`, `recover_open_files` |
| `recorder/uploader.py` | `object_key`, `next_part_key`, `make_s3_client`, `Uploader` thread |
| `recorder/recorder.py` | `new_rows`, `FeedStatus`, `Recorder` thread |
| `recorder/web.py` | `Components`, `build_components`, `create_app` (routes, auth, lifespan) |
| `recorder/static/index.html` | control page |
| `main.py` | local runner: loads `.env`, runs uvicorn |
| `tests/test_*.py` | one test file per module |
| `Dockerfile`, `.dockerignore` | container image |
| `README.md` | usage, env vars, Coolify deployment |

---

### Task 1: Project setup, feeds and windows

**Files:**
- Modify: `pyproject.toml`
- Create: `recorder/__init__.py`, `recorder/feeds.py`, `recorder/windows.py`
- Test: `tests/test_windows.py`

**Interfaces:**
- Produces:
  - `recorder.feeds.KNOWN_FEEDS: tuple[str, ...]` (23), `DEFAULT_FEEDS: tuple[str, ...]` (10), `feed_dir_name(feed: str) -> str`, `MAX_REQUESTS_PER_SECOND = 5`, `DAILY_REQUEST_QUOTA = 60_000`
  - `recorder.windows.SYDNEY_TZ`, `WINDOW: timedelta`, `window_start(dt: datetime) -> datetime`, `window_end(start: datetime) -> datetime`, `window_label(start: datetime) -> str`

- [ ] **Step 1: Add dependencies and pytest config**

```bash
uv add fastapi uvicorn boto3
uv add --dev pytest httpx
```

Then append to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

and set `description = "Record TfNSW live vehicle positions to 10-minute files and upload them to S3"`.

- [ ] **Step 2: Create `recorder/__init__.py` and `recorder/feeds.py`**

`recorder/__init__.py`:

```python
"""TfNSW live vehicle position recorder service."""
```

`recorder/feeds.py`:

```python
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
```

- [ ] **Step 3: Write the failing window tests**

`tests/test_windows.py`:

```python
from datetime import datetime, timezone

from recorder.windows import SYDNEY_TZ, window_end, window_label, window_start


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_floors_to_ten_minutes_in_sydney_time():
    # 01:19:59.999Z = 12:19:59.999 AEDT
    start = window_start(utc(2026, 10, 16, 1, 19, 59, 999_999))
    assert start == datetime(2026, 10, 16, 12, 10, tzinfo=SYDNEY_TZ)
    assert window_label(start) == "2026-10-16T12-10+1100"


def test_boundary_belongs_to_new_window():
    start = window_start(utc(2026, 10, 16, 1, 20))
    assert window_label(start) == "2026-10-16T12-20+1100"


def test_window_end_is_ten_minutes_later():
    start = window_start(utc(2026, 10, 16, 1, 15))
    assert window_label(window_end(start)) == "2026-10-16T12-20+1100"


def test_repeated_hour_at_end_of_daylight_saving_has_distinct_labels():
    # 2026-04-05 03:00 AEDT -> 02:00 AEST; 02:10 happens twice.
    first = window_start(utc(2026, 4, 4, 15, 12))
    second = window_start(utc(2026, 4, 4, 16, 12))
    assert window_label(first) == "2026-04-05T02-10+1100"
    assert window_label(second) == "2026-04-05T02-10+1000"


def test_window_end_across_end_of_daylight_saving():
    last_aedt = window_start(utc(2026, 4, 4, 15, 55))  # 02:50 AEDT
    assert window_label(window_end(last_aedt)) == "2026-04-05T02-00+1000"


def test_window_end_across_start_of_daylight_saving():
    # 2026-10-04 02:00 AEST -> 03:00 AEDT
    last_aest = window_start(utc(2026, 10, 3, 15, 55))  # 01:50 AEST
    assert window_label(last_aest) == "2026-10-04T01-50+1000"
    assert window_label(window_end(last_aest)) == "2026-10-04T03-00+1100"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_windows.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recorder.windows'`

- [ ] **Step 5: Implement `recorder/windows.py`**

```python
"""10-minute recording windows in Sydney local time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
WINDOW = timedelta(minutes=10)


def window_start(dt: datetime) -> datetime:
    """Start of the 10-minute window containing aware datetime *dt*, in Sydney time.

    Sydney's UTC offset only changes on the hour, so flooring the local minute
    never crosses an offset change.
    """
    local = dt.astimezone(SYDNEY_TZ)
    return local.replace(minute=local.minute - local.minute % 10, second=0, microsecond=0)


def window_end(start: datetime) -> datetime:
    """Start of the next window (arithmetic in UTC so DST changes are handled)."""
    return (start.astimezone(timezone.utc) + WINDOW).astimezone(SYDNEY_TZ)


def window_label(start: datetime) -> str:
    """File-name label, e.g. ``2026-10-16T12-10+1100``. The offset keeps the
    repeated hour at the end of daylight saving unique."""
    return start.strftime("%Y-%m-%dT%H-%M%z")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_windows.py -v`
Expected: 6 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock recorder/__init__.py recorder/feeds.py recorder/windows.py tests/test_windows.py
git commit -m "Add feed list and 10-minute window helpers"
```

---

### Task 2: Configuration and settings store

**Files:**
- Create: `recorder/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `recorder.feeds.KNOWN_FEEDS`, `DEFAULT_FEEDS`
- Produces:
  - `load_dotenv(path: Path = Path(".env")) -> None`
  - `class ConfigError(Exception)`
  - `@dataclass(frozen=True) S3Config(endpoint_url: str | None, region: str, bucket: str, access_key_id: str, secret_access_key: str, prefix: str)`
  - `@dataclass(frozen=True) EnvConfig(tfnsw_api_key: str, admin_user: str, admin_password: str, data_dir: Path, port: int, s3: S3Config | None)` with `@classmethod from_env(cls, env: Mapping[str, str] | None = None) -> EnvConfig`
  - `derive_region(endpoint_url: str | None) -> str`
  - `@dataclass(frozen=True) Settings(feeds: tuple[str, ...], interval: float)` with `to_dict() -> dict`
  - `DEFAULT_SETTINGS: Settings`, `validate_settings(data: object) -> Settings` (raises `ValueError`)
  - `class SettingsStore(path: Path)` with `get() -> Settings`, `update(data: object) -> Settings`

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
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
    assert cfg.port == 8000


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recorder.config'`

- [ ] **Step 3: Implement `recorder/config.py`**

```python
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
        s3 = None
        if get("S3_BUCKET"):
            missing += [n for n in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY") if not get(n)]
            endpoint = get("S3_ENDPOINT_URL") or None
            s3 = S3Config(
                endpoint_url=endpoint,
                region=get("S3_REGION") or derive_region(endpoint),
                bucket=get("S3_BUCKET"),
                access_key_id=get("S3_ACCESS_KEY_ID"),
                secret_access_key=get("S3_SECRET_ACCESS_KEY"),
                prefix=get("S3_PREFIX").strip("/") or "vehiclepos",
            )
        if missing:
            raise ConfigError("Missing environment variables: " + ", ".join(missing))

        return cls(
            tfnsw_api_key=get("TFNSW_API_KEY"),
            admin_user=get("ADMIN_USER"),
            admin_password=get("ADMIN_PASSWORD"),
            data_dir=Path(get("DATA_DIR") or "/data"),
            port=int(get("PORT") or 8000),
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: all passed (20 tests incl. parametrized)

- [ ] **Step 5: Commit**

```bash
git add recorder/config.py tests/test_config.py
git commit -m "Add environment config and persisted runtime settings"
```

---

### Task 3: Rotating window writer

**Files:**
- Create: `recorder/writer.py`
- Test: `tests/test_writer.py`

**Interfaces:**
- Consumes: `recorder.windows.window_start`, `window_label`; `recorder.feeds.feed_dir_name`
- Produces:
  - `FIELDS: list[str]`
  - `class RotatingWriter(feed: str, data_dir: Path, on_complete: Callable[[Path], None])` with `write(fetched_at: datetime, rows: list[dict[str, str]]) -> None`, `rotate_if_ended(now: datetime) -> None`, `close() -> None`, attributes `current_path: Path | None`, `rows_in_window: int`
  - `recover_open_files(data_dir: Path) -> list[Path]` (moves `open/*/*.csv.gz` to `complete/`, returns new paths)

- [ ] **Step 1: Write the failing tests**

`tests/test_writer.py`:

```python
import csv
import gzip
from datetime import datetime, timezone

from recorder.writer import FIELDS, RotatingWriter, recover_open_files


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def row(vehicle="v1"):
    return {f: "" for f in FIELDS} | {"vehicle_id": vehicle}


def read_rows(path):
    with gzip.open(path, "rt", newline="") as f:
        return list(csv.DictReader(f))


def test_appends_to_window_file_with_one_header(tmp_path):
    done = []
    w = RotatingWriter("ferries/MFF", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 11), [row("a")])
    w.write(utc(2026, 10, 16, 1, 12), [row("b"), row("c")])
    assert w.current_path == tmp_path / "open/ferries_MFF/ferries_MFF_2026-10-16T12-10+1100.csv.gz"
    assert w.rows_in_window == 3
    assert [r["vehicle_id"] for r in read_rows(w.current_path)] == ["a", "b", "c"]
    assert done == []


def test_rotates_on_new_window(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 19), [row("a")])
    w.write(utc(2026, 10, 16, 1, 20), [row("b")])
    assert done == [tmp_path / "complete/buses/buses_2026-10-16T12-10+1100.csv.gz"]
    assert [r["vehicle_id"] for r in read_rows(done[0])] == ["a"]
    assert w.current_path.name == "buses_2026-10-16T12-20+1100.csv.gz"
    assert w.rows_in_window == 1


def test_no_rows_in_new_window_still_closes_old_file(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 19), [row()])
    w.write(utc(2026, 10, 16, 1, 21), [])
    assert len(done) == 1
    assert w.current_path is None


def test_rotate_if_ended(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.write(utc(2026, 10, 16, 1, 19), [row()])
    w.rotate_if_ended(utc(2026, 10, 16, 1, 19, 59))
    assert done == []
    w.rotate_if_ended(utc(2026, 10, 16, 1, 20, 0, 500_000))
    assert len(done) == 1


def test_close_is_idempotent(tmp_path):
    done = []
    w = RotatingWriter("buses", tmp_path, done.append)
    w.close()
    w.write(utc(2026, 10, 16, 1, 15), [row()])
    w.close()
    w.close()
    assert len(done) == 1


def test_part_suffix_when_name_taken(tmp_path):
    done = []
    first = RotatingWriter("buses", tmp_path, done.append)
    first.write(utc(2026, 10, 16, 1, 11), [row()])
    first.close()
    second = RotatingWriter("buses", tmp_path, done.append)
    second.write(utc(2026, 10, 16, 1, 12), [row()])
    assert second.current_path.name == "buses_2026-10-16T12-10+1100_p2.csv.gz"


def test_recover_open_files(tmp_path):
    w = RotatingWriter("buses", tmp_path, lambda p: None)
    w.write(utc(2026, 10, 16, 1, 11), [row()])  # simulate crash: never closed
    recovered = recover_open_files(tmp_path)
    assert recovered == [tmp_path / "complete/buses/buses_2026-10-16T12-10+1100.csv.gz"]
    assert not list((tmp_path / "open").rglob("*.csv.gz"))
    assert len(read_rows(recovered[0])) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_writer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recorder.writer'`

- [ ] **Step 3: Implement `recorder/writer.py`**

```python
"""Per-feed gzipped CSV files, one per 10-minute window."""
from __future__ import annotations

import csv
import gzip
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .feeds import feed_dir_name
from .windows import window_label, window_start

FIELDS = [
    "fetched_at",
    "timestamp",
    "vehicle_id",
    "trip_id",
    "route_id",
    "latitude",
    "longitude",
    "bearing",
    "speed",
]


class RotatingWriter:
    """Writes one feed's rows to ``open/<feed_dir>/<feed_dir>_<label>[_pN].csv.gz``.

    When the window changes (or on ``close()``) the file is moved to
    ``complete/<feed_dir>/`` and passed to *on_complete*. Every append opens the
    file in gzip append mode, adding a gzip member; the concatenation is one
    valid gzip stream.
    """

    def __init__(self, feed: str, data_dir: Path, on_complete: Callable[[Path], None]) -> None:
        self.feed = feed
        self.dir_name = feed_dir_name(feed)
        self.open_dir = data_dir / "open" / self.dir_name
        self.complete_dir = data_dir / "complete" / self.dir_name
        self.open_dir.mkdir(parents=True, exist_ok=True)
        self.complete_dir.mkdir(parents=True, exist_ok=True)
        self._on_complete = on_complete
        self._window: datetime | None = None
        self.current_path: Path | None = None
        self.rows_in_window = 0

    def write(self, fetched_at: datetime, rows: list[dict[str, str]]) -> None:
        start = window_start(fetched_at)
        if self._window is not None and start != self._window:
            self.close()
        if not rows:
            return
        new_file = self.current_path is None
        if new_file:
            self._window = start
            self.current_path = self._free_path(start)
        with gzip.open(self.current_path, "at", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        self.rows_in_window += len(rows)

    def rotate_if_ended(self, now: datetime) -> None:
        if self._window is not None and window_start(now) != self._window:
            self.close()

    def close(self) -> None:
        path = self.current_path
        self._window = None
        self.current_path = None
        self.rows_in_window = 0
        if path is None:
            return
        dest = self.complete_dir / path.name
        os.replace(path, dest)
        self._on_complete(dest)

    def _free_path(self, start: datetime) -> Path:
        base = f"{self.dir_name}_{window_label(start)}"
        part = 1
        while True:
            name = f"{base}.csv.gz" if part == 1 else f"{base}_p{part}.csv.gz"
            if not (self.open_dir / name).exists() and not (self.complete_dir / name).exists():
                return self.open_dir / name
            part += 1


def recover_open_files(data_dir: Path) -> list[Path]:
    """Move files left in ``open/`` by a crash to ``complete/`` (as partial windows)."""
    recovered = []
    for path in sorted((data_dir / "open").glob("*/*.csv.gz")):
        dest_dir = data_dir / "complete" / path.parent.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        os.replace(path, dest)
        recovered.append(dest)
    return recovered
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_writer.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add recorder/writer.py tests/test_writer.py
git commit -m "Add rotating 10-minute gzip CSV writer"
```

---

### Task 4: S3 uploader

**Files:**
- Create: `recorder/uploader.py`
- Test: `tests/test_uploader.py`

**Interfaces:**
- Consumes: `recorder.config.S3Config`
- Produces:
  - `object_key(prefix: str, path: Path) -> str`
  - `next_part_key(key: str) -> str`
  - `make_s3_client(s3: S3Config)` → boto3 S3 client
  - `class Uploader(client, bucket: str | None, prefix: str, data_dir: Path, *, backoff_initial: float = 5.0, backoff_max: float = 300.0)` with `enqueue(path: Path) -> None`, `upload(path: Path) -> bool`, `start() -> None`, `stop(timeout: float = 20.0) -> None`, property `alive: bool`, `status() -> dict`
  - `client is None` means uploads disabled (S3 not configured).

- [ ] **Step 1: Write the failing tests**

`tests/test_uploader.py`:

```python
import time
from pathlib import Path

from botocore.exceptions import ClientError

from recorder.uploader import Uploader, next_part_key, object_key


class FakeS3:
    def __init__(self, existing=(), fail_times=0):
        self.objects = {k: b"" for k in existing}
        self.fail_times = fail_times
        self.extra_args = []

    def head_object(self, Bucket, Key):
        if Key in self.objects:
            return {}
        raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        if self.fail_times:
            self.fail_times -= 1
            raise ClientError({"Error": {"Code": "500", "Message": "boom"}}, "PutObject")
        self.objects[Key] = Path(Filename).read_bytes()
        self.extra_args.append(ExtraArgs)


def make_file(data_dir, name="buses_2026-10-16T12-10+1100.csv.gz", feed_dir="buses"):
    path = data_dir / "complete" / feed_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    return path


KEY = "vehiclepos/feed=buses/date=2026-10-16/buses_2026-10-16T12-10+1100.csv.gz"


def test_object_key(tmp_path):
    assert object_key("vehiclepos", make_file(tmp_path)) == KEY
    path = make_file(tmp_path, "ferries_MFF_2026-10-16T12-10+1100_p2.csv.gz", "ferries_MFF")
    assert object_key("", path) == (
        "feed=ferries_MFF/date=2026-10-16/ferries_MFF_2026-10-16T12-10+1100_p2.csv.gz"
    )


def test_next_part_key():
    assert next_part_key("a/b_2026-10-16T12-10+1100.csv.gz") == "a/b_2026-10-16T12-10+1100_p2.csv.gz"
    assert next_part_key("a/b_2026-10-16T12-10+1100_p2.csv.gz") == "a/b_2026-10-16T12-10+1100_p3.csv.gz"


def test_upload_success_deletes_local_file(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path)
    path = make_file(tmp_path)
    assert up.upload(path)
    assert s3.objects[KEY] == b"data"
    assert s3.extra_args == [{"ContentType": "application/gzip"}]
    assert not path.exists()
    st = up.status()
    assert st["uploaded_total"] == 1 and st["last_uploaded_key"] == KEY


def test_existing_object_is_not_overwritten(tmp_path):
    s3 = FakeS3(existing=[KEY])
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path)
    assert up.upload(make_file(tmp_path))
    assert s3.objects[KEY] == b""
    assert s3.objects[KEY.replace(".csv.gz", "_p2.csv.gz")] == b"data"


def test_failure_keeps_file_and_records_error(tmp_path):
    up = Uploader(FakeS3(fail_times=1), "bucket", "vehiclepos", tmp_path)
    path = make_file(tmp_path)
    assert not up.upload(path)
    assert path.exists()
    assert "boom" in up.status()["last_error"]


def test_worker_retries_until_s3_recovers(tmp_path):
    s3 = FakeS3(fail_times=2)
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path, backoff_initial=0.01, backoff_max=0.02)
    path = make_file(tmp_path)
    up.start()  # startup scan finds the file in complete/
    deadline = time.monotonic() + 5
    while KEY not in s3.objects and time.monotonic() < deadline:
        time.sleep(0.01)
    up.stop(timeout=1)
    assert KEY in s3.objects
    assert not path.exists()
    assert up.status()["pending"] == 0


def test_stop_drains_queue(tmp_path):
    s3 = FakeS3()
    up = Uploader(s3, "bucket", "vehiclepos", tmp_path)
    up.start()
    up.enqueue(make_file(tmp_path))
    up.stop(timeout=2)
    assert KEY in s3.objects
    assert not up.alive


def test_disabled_uploader_keeps_files(tmp_path):
    up = Uploader(None, None, "vehiclepos", tmp_path)
    path = make_file(tmp_path)
    up.start()
    up.enqueue(path)
    up.stop()
    assert path.exists()
    st = up.status()
    assert st["s3_configured"] is False and st["pending"] == 1
    assert up.alive  # disabled is healthy
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_uploader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recorder.uploader'`

- [ ] **Step 3: Implement `recorder/uploader.py`**

```python
"""Upload completed window files to S3-compatible storage."""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import S3Config
from .windows import SYDNEY_TZ

log = logging.getLogger(__name__)

_PART = re.compile(r"^(?P<base>.*?)(?:_p(?P<part>\d+))?\.csv\.gz$")


def object_key(prefix: str, path: Path) -> str:
    """``{prefix}/feed={feed_dir}/date={YYYY-MM-DD}/{name}`` for a file in ``complete/<feed_dir>/``."""
    feed_dir = path.parent.name
    date = path.name[len(feed_dir) + 1 :][:10]
    key = f"feed={feed_dir}/date={date}/{path.name}"
    return f"{prefix}/{key}" if prefix else key


def next_part_key(key: str) -> str:
    match = _PART.match(key)
    part = int(match["part"] or 1) + 1
    return f"{match['base']}_p{part}.csv.gz"


def make_s3_client(s3: S3Config):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=s3.endpoint_url,
        region_name=s3.region,
        aws_access_key_id=s3.access_key_id,
        aws_secret_access_key=s3.secret_access_key,
        config=Config(
            # boto3 >= 1.36 adds CRC checksum headers that some S3-compatible stores reject.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3},
        ),
    )


class Uploader:
    """Queue + worker thread. A file is deleted locally only after a successful upload."""

    def __init__(
        self,
        client,
        bucket: str | None,
        prefix: str,
        data_dir: Path,
        *,
        backoff_initial: float = 5.0,
        backoff_max: float = 300.0,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix
        self._data_dir = data_dir
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._queue: queue.Queue[Path] = queue.Queue()
        self._stop = threading.Event()
        self._deadline = 0.0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._uploaded_total = 0
        self._last_uploaded_key: str | None = None
        self._last_uploaded_at: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def alive(self) -> bool:
        return not self.enabled or (self._thread is not None and self._thread.is_alive())

    def enqueue(self, path: Path) -> None:
        if self.enabled:
            self._queue.put(path)

    def start(self) -> None:
        if not self.enabled:
            log.warning("S3 not configured: completed files stay in %s", self._data_dir / "complete")
            return
        for path in sorted(self._data_dir.glob("complete/*/*.csv.gz")):
            self._queue.put(path)
        self._thread = threading.Thread(target=self._run, name="uploader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 20.0) -> None:
        """Upload what is queued for up to *timeout* seconds, then stop."""
        self._deadline = time.monotonic() + timeout
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout + 1)

    def upload(self, path: Path) -> bool:
        from botocore.exceptions import ClientError

        try:
            key = object_key(self._prefix, path)
            while self._exists(key, ClientError):
                key = next_part_key(key)
            self._client.upload_file(
                str(path), self._bucket, key, ExtraArgs={"ContentType": "application/gzip"}
            )
        except Exception as exc:  # boto3/botocore raise many unrelated types
            log.warning("Upload of %s failed: %s", path.name, exc)
            with self._lock:
                self._last_error = str(exc)
                self._last_error_at = datetime.now(tz=SYDNEY_TZ)
            return False
        path.unlink(missing_ok=True)
        log.info("Uploaded %s", key)
        with self._lock:
            self._uploaded_total += 1
            self._last_uploaded_key = key
            self._last_uploaded_at = datetime.now(tz=SYDNEY_TZ)
        return True

    def _exists(self, key: str, client_error: type[Exception]) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except client_error as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def _run(self) -> None:
        backoff = self._backoff_initial
        while True:
            if self._stop.is_set() and (self._queue.empty() or time.monotonic() > self._deadline):
                return
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not path.exists():
                continue
            if self.upload(path):
                backoff = self._backoff_initial
                continue
            self._queue.put(path)
            if self._stop.is_set():
                return  # shutting down: leave it for the next start
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    def status(self) -> dict:
        pending = (
            self._queue.qsize()
            if self.enabled
            else len(list(self._data_dir.glob("complete/*/*.csv.gz")))
        )
        with self._lock:
            return {
                "s3_configured": self.enabled,
                "bucket": self._bucket,
                "prefix": self._prefix,
                "pending": pending,
                "uploaded_total": self._uploaded_total,
                "last_uploaded_key": self._last_uploaded_key,
                "last_uploaded_at": self._last_uploaded_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_uploader.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add recorder/uploader.py tests/test_uploader.py
git commit -m "Add S3 uploader with retry and no-overwrite parts"
```

---

### Task 5: Recorder thread

**Files:**
- Create: `recorder/recorder.py`
- Test: `tests/test_recorder.py`

**Interfaces:**
- Consumes: `SettingsStore.get() -> Settings`, `RotatingWriter`, `FIELDS`, `MAX_REQUESTS_PER_SECOND`, `SYDNEY_TZ`; client with `vehicle_positions(mode: str) -> list[VehiclePosition]`
- Produces:
  - `new_rows(positions, last_seen: dict, fetched_at: datetime) -> list[dict[str, str]]`
  - `@dataclass FeedStatus(last_poll_at, vehicles, rows_written_last_poll, rows_in_window, current_file, last_error, last_error_at, total_rows)`
  - `class Recorder(client, settings: SettingsStore, data_dir: Path, on_complete: Callable[[Path], None], *, request_spacing: float = 1 / MAX_REQUESTS_PER_SECOND, clock: Callable[[], datetime] = ...)` with `poll_once(settings: Settings) -> None`, `tick(now: datetime, settings: Settings) -> None`, `start()`, `stop(timeout: float = 10.0)`, property `alive: bool`, attribute `started_at: datetime | None`, `status() -> dict[str, FeedStatus]`

- [ ] **Step 1: Write the failing tests**

`tests/test_recorder.py`:

```python
import time
from datetime import datetime, timezone

from tfnsw_trip_planner import APIError, VehiclePosition

from recorder.config import SettingsStore
from recorder.recorder import Recorder, new_rows
from recorder.windows import SYDNEY_TZ


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def vp(vehicle="v1", ts=utc(2026, 10, 16, 1, 10), lat=-33.8, lon=151.2):
    return VehiclePosition(
        vehicle_id=vehicle, trip_id="t", route_id="2510_992", latitude=lat, longitude=lon,
        bearing=90.0, speed=5.5, timestamp=ts.astimezone(SYDNEY_TZ),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def vehicle_positions(self, mode):
        self.calls.append(mode)
        r = self.responses[mode]
        if isinstance(r, Exception):
            raise r
        return r


def make(tmp_path, responses, feeds=("buses",), interval=30, now=utc(2026, 10, 16, 1, 11)):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"feeds": list(feeds), "interval": interval})
    clock = [now]
    done = []
    rec = Recorder(
        FakeClient(responses), store, tmp_path, done.append,
        request_spacing=0, clock=lambda: clock[0],
    )
    return rec, store, clock, done


def test_new_rows_dedupes_and_skips_missing_fix():
    seen = {}
    fetched = utc(2026, 10, 16, 1, 11)
    rows = new_rows([vp("a"), vp("b", lat=0.0, lon=0.0)], seen, fetched)
    assert [r["vehicle_id"] for r in rows] == ["a"]
    assert rows[0]["latitude"] == "-33.800000" and rows[0]["speed"] == "5.50"
    assert rows[0]["fetched_at"] == "2026-10-16T12:11:00+11:00"
    assert new_rows([vp("a")], seen, fetched) == []
    assert len(new_rows([vp("a", ts=utc(2026, 10, 16, 1, 11))], seen, fetched)) == 1


def test_poll_once_writes_and_updates_status(tmp_path):
    rec, store, _, _ = make(tmp_path, {"buses": [vp("a"), vp("b")]})
    rec.poll_once(store.get())
    st = rec.status()["buses"]
    assert st.vehicles == 2 and st.rows_written_last_poll == 2 and st.rows_in_window == 2
    assert st.current_file == "buses_2026-10-16T12-10+1100.csv.gz"
    assert st.last_error is None


def test_error_on_one_feed_does_not_stop_others(tmp_path):
    rec, store, _, _ = make(
        tmp_path,
        {"buses": APIError("API error 429"), "metro": [vp("m")]},
        feeds=("buses", "metro"),
    )
    rec.poll_once(store.get())
    status = rec.status()
    assert "429" in status["buses"].last_error
    assert status["metro"].rows_written_last_poll == 1


def test_tick_rotates_ended_window(tmp_path):
    rec, store, clock, done = make(tmp_path, {"buses": [vp("a")]})
    rec.poll_once(store.get())
    rec.tick(utc(2026, 10, 16, 1, 20, 1), store.get())
    assert [p.name for p in done] == ["buses_2026-10-16T12-10+1100.csv.gz"]
    assert rec.status()["buses"].current_file is None


def test_disabled_feed_is_closed_on_next_tick(tmp_path):
    rec, store, _, done = make(tmp_path, {"buses": [vp("a")]}, interval=600)
    rec.poll_once(store.get())
    store.update({"feeds": ["metro"], "interval": 600})
    rec.tick(utc(2026, 10, 16, 1, 11, 1), store.get())  # same window
    assert len(done) == 1


def test_thread_stop_closes_open_files(tmp_path):
    rec, _, _, done = make(tmp_path, {"buses": [vp("a")]})
    rec.start()
    deadline = time.monotonic() + 5
    while not rec.status().get("buses") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rec.alive
    rec.stop(timeout=5)
    assert not rec.alive
    assert len(done) == 1


def test_unexpected_exception_marks_recorder_dead(tmp_path):
    rec, _, _, _ = make(tmp_path, {"buses": RuntimeError("bug")})
    rec.start()
    deadline = time.monotonic() + 5
    while rec.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not rec.alive
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recorder.recorder'`

- [ ] **Step 3: Implement `recorder/recorder.py`**

```python
"""Polling loop: fetch feeds, de-duplicate, write window files."""
from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tfnsw_trip_planner import TripPlannerError

from .config import Settings, SettingsStore
from .feeds import MAX_REQUESTS_PER_SECOND
from .windows import SYDNEY_TZ
from .writer import RotatingWriter

log = logging.getLogger(__name__)


def new_rows(positions, last_seen: dict, fetched_at: datetime) -> list[dict[str, str]]:
    """Rows for positions whose GPS timestamp changed since the last poll."""
    rows = []
    for p in positions:
        if p.latitude == 0.0 and p.longitude == 0.0:
            continue  # no GPS fix
        key = p.vehicle_id or p.trip_id or f"{p.latitude},{p.longitude}"
        if key in last_seen and last_seen[key] == p.timestamp:
            continue  # unchanged since last poll
        last_seen[key] = p.timestamp
        rows.append(
            {
                "fetched_at": fetched_at.astimezone(SYDNEY_TZ).isoformat(timespec="seconds"),
                "timestamp": p.timestamp.isoformat() if p.timestamp else "",
                "vehicle_id": p.vehicle_id or "",
                "trip_id": p.trip_id or "",
                "route_id": p.route_id or "",
                "latitude": f"{p.latitude:.6f}",
                "longitude": f"{p.longitude:.6f}",
                "bearing": "" if p.bearing is None else f"{p.bearing:.1f}",
                "speed": "" if p.speed is None else f"{p.speed:.2f}",
            }
        )
    return rows


@dataclass
class FeedStatus:
    last_poll_at: datetime | None = None
    vehicles: int = 0
    rows_written_last_poll: int = 0
    rows_in_window: int = 0
    current_file: str | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    total_rows: int = 0


class Recorder:
    def __init__(
        self,
        client,
        settings: SettingsStore,
        data_dir: Path,
        on_complete: Callable[[Path], None],
        *,
        request_spacing: float = 1 / MAX_REQUESTS_PER_SECOND,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=SYDNEY_TZ),
    ) -> None:
        self._client = client
        self._settings = settings
        self._data_dir = data_dir
        self._on_complete = on_complete
        self._spacing = request_spacing
        self._clock = clock
        self._writers: dict[str, RotatingWriter] = {}
        self._last_seen: dict[str, dict] = {}
        self._status: dict[str, FeedStatus] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._crashed = False
        self.started_at: datetime | None = None

    # -- thread control -------------------------------------------------

    def start(self) -> None:
        self.started_at = self._clock()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._crashed

    def status(self) -> dict[str, FeedStatus]:
        with self._lock:
            return copy.deepcopy(self._status)

    # -- work -----------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                settings = self._settings.get()
                wake = time.monotonic() + settings.interval
                self.poll_once(settings)
                while not self._stop.is_set() and time.monotonic() < wake:
                    self._stop.wait(min(1.0, max(0.0, wake - time.monotonic())))
                    self.tick(self._clock(), self._settings.get())
        except Exception:
            self._crashed = True
            log.exception("Recorder crashed")
        finally:
            for feed in list(self._writers):
                self._close(feed)

    def poll_once(self, settings: Settings) -> None:
        self._close_disabled(settings)
        for n, feed in enumerate(settings.feeds):
            if self._stop.is_set():
                return
            if n:
                self._stop.wait(self._spacing)  # stay under the rate limit
            self._poll_feed(feed)

    def tick(self, now: datetime, settings: Settings) -> None:
        """Between polls: close ended windows and feeds that were disabled."""
        self._close_disabled(settings)
        for feed, writer in self._writers.items():
            writer.rotate_if_ended(now)
            self._sync_writer_status(feed)

    def _poll_feed(self, feed: str) -> None:
        fetched_at = self._clock()
        try:
            positions = self._client.vehicle_positions(feed)
        except TripPlannerError as exc:
            log.warning("%s: %s", feed, exc)
            with self._lock:
                st = self._status.setdefault(feed, FeedStatus())
                st.last_poll_at = fetched_at
                st.last_error = str(exc)
                st.last_error_at = fetched_at
            return
        rows = new_rows(positions, self._last_seen.setdefault(feed, {}), fetched_at)
        writer = self._writers.get(feed)
        if writer is None:
            writer = self._writers[feed] = RotatingWriter(feed, self._data_dir, self._on_complete)
        writer.write(fetched_at, rows)
        log.info("%-34s vehicles=%5d new=%5d", feed, len(positions), len(rows))
        with self._lock:
            st = self._status.setdefault(feed, FeedStatus())
            st.last_poll_at = fetched_at
            st.vehicles = len(positions)
            st.rows_written_last_poll = len(rows)
            st.total_rows += len(rows)
        self._sync_writer_status(feed)

    def _close_disabled(self, settings: Settings) -> None:
        for feed in [f for f in self._writers if f not in settings.feeds]:
            self._close(feed)
            self._last_seen.pop(feed, None)

    def _close(self, feed: str) -> None:
        self._writers.pop(feed).close()
        with self._lock:
            st = self._status.get(feed)
            if st:
                st.rows_in_window = 0
                st.current_file = None

    def _sync_writer_status(self, feed: str) -> None:
        writer = self._writers.get(feed)
        with self._lock:
            st = self._status.setdefault(feed, FeedStatus())
            st.rows_in_window = writer.rows_in_window if writer else 0
            st.current_file = writer.current_path.name if writer and writer.current_path else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_recorder.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add recorder/recorder.py tests/test_recorder.py
git commit -m "Add recorder thread with window rotation and per-feed status"
```

---

### Task 6: Web app and control page

**Files:**
- Create: `recorder/web.py`, `recorder/static/index.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `EnvConfig`, `SettingsStore`, `Recorder`, `Uploader`, `make_s3_client`, `recover_open_files`, `KNOWN_FEEDS`, `DAILY_REQUEST_QUOTA`, `TripPlannerClient`
- Produces:
  - `@dataclass Components(env: EnvConfig, settings: SettingsStore, recorder, uploader)`
  - `build_components(env: EnvConfig) -> Components`
  - `create_app(env: EnvConfig | None = None, components: Components | None = None) -> FastAPI` (uvicorn: `recorder.web:create_app --factory`)
  - Routes: `GET /`, `GET /api/status`, `PUT /api/settings`, `GET /healthz`

- [ ] **Step 1: Write the failing tests**

`tests/test_web.py`:

```python
import pytest
from fastapi.testclient import TestClient

from recorder.config import EnvConfig, SettingsStore
from recorder.recorder import FeedStatus
from recorder.web import Components, create_app

AUTH = ("admin", "secret")


class FakeRecorder:
    def __init__(self):
        self.alive = False
        self.started_at = None

    def start(self):
        self.alive = True

    def stop(self, timeout=None):
        self.alive = False

    def status(self):
        return {"buses": FeedStatus(vehicles=5, last_error="boom")}


class FakeUploader:
    alive = True

    def start(self):
        pass

    def stop(self, timeout=None):
        pass

    def status(self):
        return {"s3_configured": False, "pending": 2}


@pytest.fixture
def ctx(tmp_path):
    env = EnvConfig(
        tfnsw_api_key="k", admin_user="admin", admin_password="secret",
        data_dir=tmp_path, port=8000, s3=None,
    )
    comps = Components(
        env=env, settings=SettingsStore(tmp_path / "settings.json"),
        recorder=FakeRecorder(), uploader=FakeUploader(),
    )
    with TestClient(create_app(env, comps)) as client:
        yield client, comps


def test_requires_auth(ctx):
    client, _ = ctx
    for path in ("/", "/api/status"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Basic"
    assert client.get("/api/status", auth=("admin", "wrong")).status_code == 401
    assert client.put("/api/settings", json={"feeds": [], "interval": 30}).status_code == 401


def test_healthz_is_public_and_reflects_threads(ctx):
    client, comps = ctx
    assert client.get("/healthz").status_code == 200
    comps.recorder.alive = False
    assert client.get("/healthz").status_code == 500


def test_index_page(ctx):
    client, _ = ctx
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "Vehicle position recorder" in r.text


def test_status(ctx):
    client, _ = ctx
    body = client.get("/api/status", auth=AUTH).json()
    assert body["recorder"]["alive"] is True
    assert body["settings"]["interval"] == 30
    assert body["quota"] == {"requests_per_day": 28800, "daily_quota": 60000}
    feeds = {f["feed"]: f for f in body["feeds"]}
    assert len(feeds) == 23
    assert feeds["buses"]["enabled"] is True and feeds["buses"]["vehicles"] == 5
    assert feeds["lightrail/newcastle"]["enabled"] is False
    assert body["uploader"]["pending"] == 2
    assert "secret" not in str(body) and "admin_password" not in str(body)


def test_update_settings(ctx):
    client, comps = ctx
    r = client.put("/api/settings", json={"feeds": ["metro"], "interval": 60}, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"feeds": ["metro"], "interval": 60}
    assert comps.settings.get().feeds == ("metro",)


def test_update_settings_invalid(ctx):
    client, comps = ctx
    r = client.put("/api/settings", json={"feeds": ["nope"], "interval": 60}, auth=AUTH)
    assert r.status_code == 422
    assert "unknown feeds" in r.json()["detail"]
    assert comps.settings.get().interval == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'recorder.web'`

- [ ] **Step 3: Implement `recorder/web.py`**

```python
"""FastAPI app: control page, status API, settings API, health check."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from tfnsw_trip_planner import TripPlannerClient

from .config import EnvConfig, SettingsStore
from .feeds import DAILY_REQUEST_QUOTA, KNOWN_FEEDS
from .recorder import Recorder
from .uploader import Uploader, make_s3_client
from .writer import recover_open_files

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class Components:
    env: EnvConfig
    settings: SettingsStore
    recorder: Any
    uploader: Any


def build_components(env: EnvConfig) -> Components:
    env.data_dir.mkdir(parents=True, exist_ok=True)
    recovered = recover_open_files(env.data_dir)
    if recovered:
        log.info("Recovered %d unfinished files from a previous run", len(recovered))
    settings = SettingsStore(env.data_dir / "settings.json")
    s3 = env.s3
    uploader = Uploader(
        make_s3_client(s3) if s3 else None,
        s3.bucket if s3 else None,
        s3.prefix if s3 else "",
        env.data_dir,
    )
    recorder = Recorder(
        TripPlannerClient(api_key=env.tfnsw_api_key), settings, env.data_dir, uploader.enqueue
    )
    return Components(env=env, settings=settings, recorder=recorder, uploader=uploader)


def create_app(env: EnvConfig | None = None, components: Components | None = None) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = env or EnvConfig.from_env()
    comps = components or build_components(env)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        comps.uploader.start()
        comps.recorder.start()
        yield
        log.info("Shutting down: closing open files and uploading")
        comps.recorder.stop()
        comps.uploader.stop(timeout=20)

    app = FastAPI(title="TfNSW vehicle position recorder", lifespan=lifespan, docs_url=None, redoc_url=None)
    security = HTTPBasic()

    def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
        user_ok = secrets.compare_digest(creds.username.encode(), env.admin_user.encode())
        pass_ok = secrets.compare_digest(creds.password.encode(), env.admin_password.encode())
        if not (user_ok and pass_ok):
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> PlainTextResponse:
        ok = comps.recorder.alive and comps.uploader.alive
        return PlainTextResponse("ok" if ok else "unhealthy", status_code=200 if ok else 500)

    @app.get("/", dependencies=[Depends(require_auth)])
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status() -> dict:
        settings = comps.settings.get()
        feed_status = comps.recorder.status()
        feeds = []
        for feed in KNOWN_FEEDS:
            st = feed_status.get(feed)
            feeds.append({"feed": feed, "enabled": feed in settings.feeds, **(asdict(st) if st else {})})
        return {
            "recorder": {"alive": comps.recorder.alive, "started_at": comps.recorder.started_at},
            "settings": settings.to_dict(),
            "quota": {
                "requests_per_day": round(len(settings.feeds) * 86400 / settings.interval),
                "daily_quota": DAILY_REQUEST_QUOTA,
            },
            "feeds": feeds,
            "uploader": comps.uploader.status(),
        }

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    def update_settings(payload: Any = Body(...)) -> dict:
        try:
            return comps.settings.update(payload).to_dict()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app
```

- [ ] **Step 4: Create `recorder/static/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vehicle position recorder</title>
<style>
  :root { --fg:#1d1d1f; --muted:#6e6e73; --bg:#fff; --line:#e5e5ea; --ok:#1a7f37; --bad:#c62828; --accent:#0a58ca; }
  @media (prefers-color-scheme: dark) { :root { --fg:#f2f2f7; --muted:#a1a1a6; --bg:#151517; --line:#2c2c2e; --ok:#3fb950; --bad:#ff6b6b; --accent:#6ea8fe; } }
  body { margin:0; padding:16px; font:14px/1.45 system-ui, sans-serif; color:var(--fg); background:var(--bg); }
  main { max-width:1100px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .muted { color:var(--muted); }
  .ok { color:var(--ok); } .bad { color:var(--bad); }
  header, section { margin-bottom:20px; }
  .bar { display:flex; flex-wrap:wrap; gap:16px; align-items:center; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .wrap { overflow-x:auto; }
  input[type=number] { width:80px; }
  button { padding:6px 14px; }
  .err { color:var(--bad); max-width:320px; overflow-wrap:anywhere; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Vehicle position recorder</h1>
    <div class="bar">
      <span id="state" class="muted">Loading…</span>
      <span id="uptime" class="muted"></span>
      <label>Interval <input id="interval" type="number" min="10" max="3600" step="1"> s</label>
      <span id="quota"></span>
      <button id="save" disabled>Save</button>
      <span id="msg" class="muted"></span>
    </div>
  </header>

  <section>
    <h2>Feeds</h2>
    <div class="wrap">
      <table>
        <thead><tr>
          <th>On</th><th>Feed</th><th>Last poll</th><th class="num">Vehicles</th>
          <th class="num">Rows in window</th><th>Current file</th><th>Last error</th>
        </tr></thead>
        <tbody id="feeds"></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Uploads</h2>
    <div id="uploads" class="muted">Loading…</div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let dirty = false;

const fmtTime = (iso) => iso
  ? new Date(iso).toLocaleString("en-AU", { timeZone: "Australia/Sydney", hour12: false })
  : "—";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function quotaText(feeds, interval) {
  const perDay = Math.round(feeds * 86400 / interval);
  return { perDay, text: `${perDay.toLocaleString()} requests/day of 60,000` };
}

function updateQuota() {
  const feeds = document.querySelectorAll("#feeds input:checked").length;
  const interval = Number($("interval").value) || 1;
  const q = quotaText(feeds, interval);
  $("quota").textContent = q.text;
  $("quota").className = q.perDay > 60000 ? "bad" : "muted";
}

function markDirty() { dirty = true; $("save").disabled = false; updateQuota(); }

function render(s) {
  $("state").textContent = s.recorder.alive ? "● Recording" : "● Stopped";
  $("state").className = s.recorder.alive ? "ok" : "bad";
  $("uptime").textContent = s.recorder.started_at ? `since ${fmtTime(s.recorder.started_at)}` : "";
  if (!dirty) $("interval").value = s.settings.interval;

  const checked = dirty
    ? new Set([...document.querySelectorAll("#feeds input:checked")].map((i) => i.value))
    : new Set(s.settings.feeds);
  $("feeds").innerHTML = s.feeds.map((f) => `
    <tr>
      <td><input type="checkbox" value="${esc(f.feed)}" ${checked.has(f.feed) ? "checked" : ""}></td>
      <td>${esc(f.feed)}</td>
      <td>${fmtTime(f.last_poll_at)}</td>
      <td class="num">${f.vehicles ?? "—"}</td>
      <td class="num">${f.rows_in_window ?? "—"}</td>
      <td class="muted">${esc(f.current_file ?? "—")}</td>
      <td class="err">${f.last_error ? `${esc(f.last_error)}<br><span class="muted">${fmtTime(f.last_error_at)}</span>` : ""}</td>
    </tr>`).join("");
  document.querySelectorAll("#feeds input").forEach((i) => i.addEventListener("change", markDirty));
  updateQuota();

  const u = s.uploader;
  $("uploads").innerHTML = u.s3_configured
    ? `Bucket <b>${esc(u.bucket)}</b> · pending <b>${u.pending}</b> · uploaded <b>${u.uploaded_total}</b><br>
       Last upload: ${esc(u.last_uploaded_key ?? "—")} ${u.last_uploaded_at ? "at " + fmtTime(u.last_uploaded_at) : ""}
       ${u.last_error ? `<div class="err">Last error (${fmtTime(u.last_error_at)}): ${esc(u.last_error)}</div>` : ""}`
    : `<span class="bad">S3 not configured</span> — ${u.pending} completed files kept locally.`;
}

async function refresh() {
  try {
    const r = await fetch("/api/status");
    if (r.ok) render(await r.json());
  } catch (e) { $("msg").textContent = "Status unavailable"; }
}

$("interval").addEventListener("input", markDirty);
$("save").addEventListener("click", async () => {
  const feeds = [...document.querySelectorAll("#feeds input:checked")].map((i) => i.value);
  const r = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feeds, interval: Number($("interval").value) }),
  });
  if (r.ok) {
    dirty = false; $("save").disabled = true;
    $("msg").textContent = "Saved — applies at the next poll"; $("msg").className = "ok";
    refresh();
  } else {
    const body = await r.json().catch(() => ({}));
    $("msg").textContent = body.detail || `Error ${r.status}`; $("msg").className = "bad";
  }
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_web.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add recorder/web.py recorder/static/index.html tests/test_web.py
git commit -m "Add control page, status and settings API with basic auth"
```

---

### Task 7: Local runner, Docker image and README

**Files:**
- Modify: `main.py` (replace CLI loop with service runner)
- Create: `Dockerfile`, `.dockerignore`
- Modify: `README.md` (rewrite Usage/Output/limits; add Coolify)

**Interfaces:**
- Consumes: `recorder.config.load_dotenv`, `recorder.web:create_app`

- [ ] **Step 1: Replace `main.py`**

```python
"""Run the recorder service locally (web page on http://127.0.0.1:8000).

    uv run main.py

Reads configuration from the environment or a .env file; see README.md.
Data goes to ./data unless DATA_DIR is set.
"""
import os

import uvicorn

from recorder.config import load_dotenv


def main() -> None:
    load_dotenv()
    os.environ.setdefault("DATA_DIR", "data")
    uvicorn.run(
        "recorder.web:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        timeout_graceful_shutdown=25,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create `Dockerfile` and `.dockerignore`**

`Dockerfile`:

```dockerfile
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.15 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY recorder ./recorder

RUN useradd --system --uid 1000 --no-create-home app \
    && mkdir -p /data && chown app /data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    PORT=8000
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

CMD ["sh", "-c", "exec uvicorn recorder.web:create_app --factory --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 25"]
```

`.dockerignore`:

```
.venv
.git
.env
data
docs
tests
**/__pycache__
```

- [ ] **Step 3: Rewrite `README.md`**

Replace the whole file with:

````markdown
# tfnsw-realtime-recorder

Records Transport for NSW **live vehicle positions** (GTFS-Realtime) into
10-minute gzipped CSV files per feed and uploads each finished file to
S3-compatible storage (Hetzner Object Storage). A small web page shows the
recorder's status and lets you switch feeds on/off and change the polling
interval. Uses [tfnsw_trip_planner](https://github.com/maxim75/tfnsw_trip_planner).

## Configuration

| Variable | Required | Notes |
|---|---|---|
| `TFNSW_API_KEY` | yes | TfNSW Open Data key with *Realtime Vehicle Positions* enabled |
| `ADMIN_USER`, `ADMIN_PASSWORD` | yes | basic-auth login for the web page |
| `S3_BUCKET` | to upload | without it, finished files stay in `DATA_DIR/complete/` |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | with `S3_BUCKET` | Hetzner Console → Security → S3 credentials |
| `S3_ENDPOINT_URL` | for Hetzner | `https://nbg1.your-objectstorage.com` |
| `S3_REGION` | no | derived from the Hetzner endpoint (`nbg1`) |
| `S3_PREFIX` | no | default `vehiclepos` |
| `DATA_DIR` | no | default `/data` in Docker, `./data` with `main.py` |
| `PORT` | no | default `8000` |

Feeds and interval are set on the web page and stored in
`DATA_DIR/settings.json` (default: 10 Sydney-area feeds every 30 s).

## Run locally

Put the variables in `.env`, then:

```bash
uv run main.py
```

Open <http://127.0.0.1:8000> and log in with `ADMIN_USER` / `ADMIN_PASSWORD`.

Tests:

```bash
uv run pytest
```

## Output

Each feed gets one file per 10-minute window (Sydney time), e.g. 12:10–12:20:

```
vehiclepos/feed=buses/date=2026-10-16/buses_2026-10-16T12-10+1100.csv.gz
```

The `+1100`/`+1000` offset keeps the repeated hour at the end of daylight
saving unique. A window cut short by a restart, or a feed switched off and on
again, continues in `..._p2.csv.gz`; existing objects are never overwritten.

| column | notes |
|---|---|
| `fetched_at` | when the recorder polled (Sydney time) |
| `timestamp` | GPS report time from the vehicle |
| `vehicle_id`, `trip_id`, `route_id` | GTFS identifiers |
| `latitude`, `longitude` | WGS84 |
| `bearing`, `speed` | degrees / m/s; empty when the feed doesn't provide them |

A row is written only when a vehicle's GPS timestamp changes. Read with DuckDB:

```sql
SELECT * FROM read_csv('s3://<bucket>/vehiclepos/feed=buses/date=2026-10-16/*.csv.gz',
                       hive_partitioning = true);
```

## API limits

- **Rate:** ~5 requests/second; the recorder spaces requests 0.2 s apart.
- **Daily quota:** 60,000 requests by default. Requests/day =
  `feeds × 86400 / interval`; the page shows the estimate and turns red above
  the quota. 10 feeds at 30 s = 28,800/day.

## Deploy to Coolify

1. **New Resource → Public/Private Repository**, pick this repo, build pack
   **Dockerfile**.
2. **Ports exposed:** `8000`; set a domain for the web page.
3. **Persistent storage:** add a volume mounted at `/data` (use a Docker
   volume, not a host bind mount, so the image's `app` user can write to it).
4. **Environment variables:** everything from the table above, e.g.

   ```
   TFNSW_API_KEY=...
   ADMIN_USER=...
   ADMIN_PASSWORD=...
   S3_ENDPOINT_URL=https://nbg1.your-objectstorage.com
   S3_BUCKET=...
   S3_ACCESS_KEY_ID=...
   S3_SECRET_ACCESS_KEY=...
   ```

5. **Health check:** path `/healthz`, port `8000` (the image also has a
   Docker `HEALTHCHECK`).
6. Set the container stop/grace timeout to **30 s** so the last partial
   window is uploaded on redeploy (if it isn't, it's uploaded after the next
   start).
7. Run **one** instance only; two would record every feed twice.
````

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests from Tasks 1–6 pass.

- [ ] **Step 5: Build and smoke-test the image**

```bash
docker build -t tfnsw-recorder .
docker run -d --name tfnsw-recorder-test -p 8000:8000 --env-file .env -v tfnsw-recorder-data:/data tfnsw-recorder
sleep 15
curl -fsS http://127.0.0.1:8000/healthz
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/status        # expect 401
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" http://127.0.0.1:8000/api/status | head -c 400
docker stop -t 30 tfnsw-recorder-test && docker logs tfnsw-recorder-test | tail -20
docker rm tfnsw-recorder-test
```

Expected: `ok`; `401`; JSON with `"alive":true` and feed vehicle counts; logs end with "Shutting down" and, if S3 is configured, "Uploaded ..." lines for the partial window.

`.env` for this step needs `TFNSW_API_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD` (copy the TfNSW key from `~/code/tfnsw_trip_planner/.env` without printing it; generate a random admin password) and optionally the Hetzner `S3_*` values.

- [ ] **Step 6: Commit**

```bash
git add main.py Dockerfile .dockerignore README.md
git commit -m "Run recorder as a web service in Docker; document Coolify deploy"
```

---

### Task 8: Live verification across a window boundary

**Files:** none (verification only)

- [ ] **Step 1: Run the service locally for ~11 minutes spanning a :x0 boundary**

```bash
uv run main.py   # in the background, with .env containing the TfNSW key and admin login
```

During the run, check the page with basic auth and toggle one feed off and back on.

- [ ] **Step 2: Verify the files**

- `data/complete/` (or S3) contains one file per enabled feed for the window that closed, named `<feed_dir>_<YYYY-MM-DD>T<HH>-<M0>+1100.csv.gz`.
- `python -c "import gzip,csv,sys; r=list(csv.DictReader(gzip.open(sys.argv[1],'rt'))); print(len(r), r[0])" <file>` prints rows with the expected columns, all `fetched_at` inside the window.
- The toggled feed produced a closed file at the toggle and a `_p2` file afterwards.

- [ ] **Step 3: Verify S3 (only if the user put Hetzner credentials in `.env`)**

```bash
uv run python - <<'EOF'
import os, gzip, io
from recorder.config import EnvConfig, load_dotenv
from recorder.uploader import make_s3_client
load_dotenv(); cfg = EnvConfig.from_env()
s3 = make_s3_client(cfg.s3)
objs = s3.list_objects_v2(Bucket=cfg.s3.bucket, Prefix=cfg.s3.prefix + "/")["Contents"]
key = objs[-1]["Key"]; print(key, s3.head_object(Bucket=cfg.s3.bucket, Key=key)["ContentType"])
body = s3.get_object(Bucket=cfg.s3.bucket, Key=key)["Body"].read()
print(gzip.decompress(body).decode().splitlines()[0])
EOF
```

Expected: a key under `vehiclepos/feed=.../date=.../`, `application/gzip`, and the CSV header line.

- [ ] **Step 4: Report** results with command output; note anything not verified (e.g. S3 if no credentials, Coolify itself).
