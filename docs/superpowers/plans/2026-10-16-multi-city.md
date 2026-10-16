# Multiple Cities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One recorder deployment records, maps and replays several cities, starting with Prague next to Sydney, without disturbing Sydney's existing data.

**Architecture:** A `cities.toml` config describes each city (feeds, timezone, S3 prefix, line-name source). The service builds one recorder thread, live hub, history and route-info object per city; one uploader serves all. Every API takes `?city=`; the pages get a city picker. The static site moves to `data/<city>/`.

**Tech Stack:** Python 3.12 (`tomllib`, `requests`, `gtfs-realtime-bindings`), FastAPI, MapLibre GL JS 4.7.1 + deck.gl 9.1.14, uv, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-city-design.md`

**Level of detail:** each task lists files, interfaces, the tests to write first and the key code. Implementers write the remaining code test-first; the interfaces block is binding between tasks.

## Global Constraints

- Sydney must keep working at every commit: existing S3 keys under `vehiclepos/feed=…`, local files, `settings.json` and the playback day files stay readable.
- City ids: lowercase `[a-z0-9-]+`; Sydney's id is `sydney`, Prague's `prague`.
- Prague S3 prefix `vehiclepos/city=prague`; Sydney's prefix `vehiclepos` (or `S3_PREFIX` if set).
- File and S3 key format under a prefix unchanged: `feed=<feed_dir>/date=<YYYY-MM-DD>/<feed_dir>_<YYYY-MM-DDTHH-MM±HHMM>[_pN].csv.gz`, in the city's local time.
- Secrets only via env vars referenced as `${NAME}` in `cities.toml`; never logged or returned by the API.
- A city with an unresolved `${NAME}` is disabled (status shows why); other cities run.
- Report format everywhere (live, history, playback): `[vehicle_id, route_id, gps_epoch_s, lon, lat, bearing, speed, label, mode]`.
- Modes: `metro`, `tram`, `rail`, `bus`, `ferry`, `trolleybus`, `cable`, `other`.
- Static window format v2: vehicles `[feed_index, vehicle_id, route_id, label, mode]`; `index.json` has `"format": 2`.
- Tests: `uv run pytest -W error`, no network.

## Review Focus

1. First start on an existing Sydney volume: legacy `open/`, `complete/`, `playback/` move into `sydney/` before recovery/upload, and pending uploads still go to `vehiclepos/feed=…` (Task 5).
2. `TFNSW_API_KEY` missing: Sydney disabled, Prague still records, `/healthz` stays 200 (Task 2, Task 6).
3. Prague window labels use `+0200`/`+0100` and the Prague date for `date=` partitions, including the night of the October DST change (Task 3).
4. PID GTFS download fails or is huge/slow: recording never waits for it; labels fall back to cached or raw ids (Task 4).
5. An old browser tab of `/map` (no `city` param) keeps working against the new server (defaults to the first city) (Task 7).

---

## File Structure

| File | Change |
|---|---|
| `cities.toml` | new: Sydney (23 feeds, today's defaults) and Prague |
| `recorder/cities.py` | new: `City`, `Feed`, `load_cities()` |
| `recorder/gtfsrt.py` | new: `Position`, `GtfsRtClient`, `FetchError` |
| `recorder/routes.py` | new: `RouteInfo`, GTFS `routes.txt` cache, mode mapping |
| `recorder/windows.py` | timezone parameter |
| `recorder/config.py` | `EnvConfig` without `TFNSW_API_KEY`; per-city `SettingsStore` |
| `recorder/feeds.py` | reduced to helpers (`feed_dir_name`); feed lists move to `cities.toml` |
| `recorder/storage.py` | new: per-city paths, legacy migration |
| `recorder/recorder.py`, `writer.py`, `live.py`, `history.py`, `playback.py`, `uploader.py`, `export.py`, `static_export.py` | take a `City` (timezone, data dir, prefix) |
| `recorder/web.py` | per-city components, `?city=`, `/api/cities` |
| `recorder/static/index.html`, `map.html` | city picker |
| `pyproject.toml` | drop `tfnsw-trip-planner`, add `gtfs-realtime-bindings` |
| `../tfnsw-playback-map/` | `data/cities.json`, `data/<city>/`, city picker |

---

### Task 1: City config

**Files:** create `cities.toml`, `recorder/cities.py`, `tests/test_cities.py`

**Interfaces (produces):**

```python
@dataclass(frozen=True)
class Feed:
    id: str            # "buses", "all"
    url: str
    mode: str          # fallback mode
@dataclass(frozen=True)
class City:
    id: str; name: str; timezone: ZoneInfo; center: tuple[float, float]; zoom: float
    s3_prefix: str; attribution: str; interval: float; max_requests_per_second: float
    daily_quota: int | None; default_feeds: tuple[str, ...]; feeds: tuple[Feed, ...]
    headers: dict[str, str]           # env already expanded
    route_label_pattern: str | None; static_gtfs: str | None
    disabled_reason: str | None       # e.g. "TFNSW_API_KEY is not set"
    def feed(self, id: str) -> Feed: ...
def load_cities(path: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, City]  # ordered as in file
```

- [ ] Tests first:
  - `test_loads_sydney_and_prague_from_repo_file` (ids, timezone names, 23 Sydney feeds, `default_feeds` = today's 10, Prague prefix `vehiclepos/city=prague`)
  - `test_expands_env_in_headers` (`apikey ${TFNSW_API_KEY}` → value)
  - `test_missing_env_disables_city_only` (Sydney disabled with reason, Prague enabled)
  - `test_s3_prefix_env_overrides_sydney_prefix` (`S3_PREFIX=x`)
  - `test_rejects_bad_config` (unknown default feed, bad timezone, duplicate feed id, bad city id) → `ValueError` naming the problem
- [ ] Write `cities.toml` with the 23 Sydney feeds as URLs (`v2` for `sydneytrains`, `metro`, `lightrail/innerwest`; modes: buses/regionbuses → bus, sydneytrains/nswtrains → rail, metro → metro, ferries/* → ferry, lightrail/* → tram) and Prague (spec §1).
- [ ] Implement `load_cities` with `tomllib`; `CITIES_FILE` env selects another file.
- [ ] Commit: "Add city config with Sydney and Prague".

### Task 2: Generic GTFS-Realtime client

**Files:** create `recorder/gtfsrt.py`, `tests/test_gtfsrt.py`; modify `pyproject.toml` (add `gtfs-realtime-bindings`; keep `tfnsw-trip-planner` until Task 6 removes its last import)

**Interfaces:**

```python
@dataclass
class Position:
    vehicle_id: str | None; trip_id: str | None; route_id: str | None
    latitude: float; longitude: float; bearing: float | None; speed: float | None
    timestamp: datetime | None        # aware UTC
class FetchError(Exception): ...
class GtfsRtClient:
    def __init__(self, timeout: float = 8, session: requests.Session | None = None): ...
    def fetch(self, url: str, headers: Mapping[str, str]) -> list[Position]: ...
```

- [ ] Tests first (fake session returning canned protobuf built with `gtfs_realtime_pb2`): positions parsed incl. missing bearing/speed; entities without position skipped; HTTP 404/429 → `FetchError` with status; timeout/connection error → `FetchError`; garbage body → `FetchError`; headers passed through.
- [ ] Implement (same field semantics as `tfnsw_trip_planner.VehiclePosition.from_entity`, so `new_rows`, the live hub and history work unchanged).
- [ ] Commit: "Add generic GTFS-Realtime vehicle position client".

### Task 3: Timezone-aware windows

**Files:** modify `recorder/windows.py`, `recorder/playback.py` (`day_windows`, `resolve_date`), their callers; tests `tests/test_windows.py`, `tests/test_playback.py`

**Interfaces:** `window_start(dt, tz)`, `window_end(start)`, `window_label(start)` (offset from `start`), `day_windows(day, tz)`, `resolve_date(value, tz, now=None)`. `SYDNEY_TZ` stays as a constant for tests and the Sydney entry.

- [ ] Tests first: Prague flooring and labels (`2026-10-16T12-10+0200`); Prague DST end 2026-10-25 (03:00 CEST → 02:00 CET, repeated 02:xx with `+0200` and `+0100`); `day_windows(2026-10-25, Prague)` has 150 windows; existing Sydney tests pass with `tz=SYDNEY_TZ`.
- [ ] Thread `tz` through `RotatingWriter`, `Recorder`, `History`, `build_day`, `export_day` (callers pass `city.timezone`; temporarily `SYDNEY_TZ` until Task 6 wires cities).
- [ ] Commit: "Make windows and day boundaries timezone-aware".

### Task 4: Line names and modes

**Files:** create `recorder/routes.py`, `tests/test_routes.py`

**Interfaces:**

```python
MODE_BY_ROUTE_TYPE: dict[int, str]   # 0 tram, 1 metro, 2 rail, 3 bus, 4 ferry, 5/6/7 cable, 11 trolleybus, 100-117 rail, 200-209 bus, 400-405 metro, 700-716 bus, 900-906 tram, 1000 ferry, 1300-1307 cable
class RouteInfo:
    def __init__(self, city: City, data_dir: Path, *, session=None, clock=time.time): ...
    def label(self, route_id: str | None) -> str
    def mode(self, feed: Feed, route_id: str | None) -> str
    def refresh(self) -> None        # download if older than 24 h; never raises
```

- [ ] Tests first: fake GTFS zip (only `routes.txt`, incl. a BOM and quoted names) → `label("L992") == "B"`, `mode(..., "L992") == "metro"`, tram/bus/rail; unknown route → raw id and feed mode; pattern labels (`2510_333` → `333`); cache file reused when download fails; `refresh` skips when cache < 24 h and uses `If-Modified-Since`.
- [ ] Implement: stream the zip to a temp file, read `routes.txt` with `zipfile`, store `{route_id: [short_name or long_name, mode]}` in `DATA_DIR/<city>/routes.json`. `refresh` runs in a background thread at startup and daily (Task 6); lookups never block on it.
- [ ] Commit: "Add line names and modes from static GTFS".

### Task 5: Per-city storage and settings

**Files:** create `recorder/storage.py`, `tests/test_storage.py`; modify `recorder/config.py` (`SettingsStore`), `tests/test_config.py`

**Interfaces:**

```python
def city_dir(data_dir: Path, city_id: str) -> Path                 # DATA_DIR/<city>
def migrate_legacy_layout(data_dir: Path, first_city: str = "sydney") -> list[str]   # moved dirs
def city_of(path: Path, data_dir: Path) -> str                      # from DATA_DIR/<city>/complete/...
class SettingsStore:
    def __init__(self, path: Path, cities: dict[str, City]): ...
    def get(self, city_id: str) -> Settings
    def update(self, city_id: str, data: object) -> Settings
```

- [ ] Tests first: legacy `open/`, `complete/`, `playback/` move under `sydney/` and the call is idempotent; existing `sydney/` contents are kept (merge, no overwrite); legacy flat `settings.json` read as Sydney's and rewritten as `{"cities": {...}}` on save; unknown city id → `KeyError`; per-city validation (Prague accepts `all`, rejects `buses`); cities missing from the file get config defaults.
- [ ] Implement; `EnvConfig` drops `TFNSW_API_KEY` from required vars (S3 and admin unchanged).
- [ ] Commit: "Store recordings and settings per city".

### Task 6: Multi-city service

**Files:** modify `recorder/recorder.py`, `recorder/writer.py`, `recorder/live.py`, `recorder/history.py`, `recorder/uploader.py`, `recorder/web.py` (components only), tests

**Interfaces:**

```python
class Recorder:   # existing API, plus:
    def __init__(self, client: GtfsRtClient, city: City, settings: SettingsStore, data_dir: Path,
                 on_complete, *, routes: RouteInfo, clock=..., on_fatal=..., on_positions=None): ...
class Uploader:   # prefix now per city
    def __init__(self, client, bucket, prefixes: dict[str, str], data_dir: Path, ...): ...
@dataclass
class CityComponents:
    city: City; recorder: Recorder | None; live: LiveHub; history: History; routes: RouteInfo
@dataclass
class Components:
    env: EnvConfig; settings: SettingsStore; uploader: Uploader; cities: dict[str, CityComponents]
```

- [ ] Tests first:
  - recorder polls each enabled feed URL with the city's headers, spaced by `1 / max_requests_per_second`; `FetchError` per feed recorded as `last_error`
  - a Prague recorder writes `DATA_DIR/prague/open/all/all_2026-10-16T12-10+0200.csv.gz`
  - uploader key for a Prague file = `vehiclepos/city=prague/feed=all/date=2026-10-16/…`; Sydney's unchanged
  - live hub and history reports carry `label` and `mode` as the last two fields
  - `build_components` with Sydney disabled (no key): no Sydney recorder, history still serves Sydney's S3 data, Prague recorder created; `/healthz` 200
  - `/healthz` 500 when any city's recorder thread dies
- [ ] Implement; remove `tfnsw_trip_planner` imports and the dependency (`uv remove tfnsw-trip-planner`).
- [ ] Commit: "Record every configured city in one service".

### Task 7: Web API and pages

**Files:** modify `recorder/web.py`, `recorder/static/index.html`, `recorder/static/map.html`, `tests/test_web.py`

**Interfaces:** `GET /api/cities` → `[{id, name, timezone, center, zoom, attribution, modes, enabled, disabled_reason, quota}]`; `?city=` on `/api/status`, `PUT /api/settings`, `/api/live`, `/api/live/stream`, `/api/history/window` (default: first enabled city; unknown → 404).

- [ ] Tests first: `/api/cities` content and auth; each endpoint with `?city=prague`; missing `city` defaults to the first city; unknown city 404; every route still requires auth except `/healthz`.
- [ ] Status page: city tabs; per-city feed table, interval, quota line (hidden when a city has no quota).
- [ ] `/map`: city picker in the panel (and `#prague` / `#prague/17:30`, remembered in `localStorage`); `flyTo` the city's centre; mode checkboxes from `/api/cities` modes (colours by mode: metro teal, tram red, rail orange, bus blue, ferry green, trolleybus purple, cable brown, other grey); labels from the report's `label`; all clocks, date/time inputs and the playback slider in the city's timezone (generalise `sydParts`/`sydEpoch` to take a zone); switching city resets live and playback stores.
- [ ] Browser check both cities live and in playback (screenshots in the report).
- [ ] Commit: "Add city picker to status page and map".

### Task 8: Playback day files and static export per city

**Files:** modify `recorder/playback.py`, `recorder/static_export.py`, `export_static.py`, `build_playback.py`, tests

**Interfaces:** `build_playback.py --city prague --date …`; `export_static.py --city prague --date … --out ../tfnsw-playback-map/data` writes `data/prague/index.json` (`"format": 2`, city metadata) and `data/prague/w/*.json.gz`, and updates `data/cities.json` (`[{id, name, timezone, center, zoom, attribution, date}]`, merged with existing entries).

- [ ] Tests first: format v2 round trip incl. label/mode; `cities.json` merge keeps other cities; per-city `day_file` path; Prague day bounds in Prague time.
- [ ] Commit: "Export playback data per city".

### Task 9: Static site city picker

**Files (site repo):** `index.html`, `app.js`, `README.md`, `data/` (move `index.json`, `w/` → `data/sydney/`, re-export format v2)

- [ ] `app.js`: load `data/cities.json`; picker (`#prague/17:30`); load `data/<city>/index.json`; city timezone for clock/slider/coverage; modes and colours from window data; attribution per city; read format 2 (and format 1 for safety).
- [ ] Re-export Sydney's latest day into `data/sydney/` (format 2) and export Prague's first recorded day.
- [ ] Local check with `python3 -m http.server`; then commit and push; verify on GitHub Pages.
- [ ] README: per-city export commands.
- [ ] Commit (site repo): "Add city picker and Prague data".

### Task 10: Rollout and docs

- [ ] Confirm Golemio/PID licence and attribution text; update `cities.toml` and the site.
- [ ] README (recorder): `cities.toml`, adding a city, `?city=`, per-city tools; Coolify: no new env vars needed for Prague; volume migration happens automatically on first start.
- [ ] Deploy to Coolify; check: status page shows both cities; Sydney uploads continue under `vehiclepos/feed=…`; Prague objects appear under `vehiclepos/city=prague/` after the first window; `/map` shows Prague live; quota warnings only for Sydney.
- [ ] Commit: "Document multiple cities".

---

## Order and effort

| Task | Depends on | Effort |
|---|---|---|
| 1 City config | — | 0.5 day |
| 2 GTFS-R client | — | 0.5 day |
| 3 Timezone-aware windows | — | 0.5 day |
| 4 Line names and modes | 1 | 0.5 day |
| 5 Per-city storage and settings | 1 | 0.5 day |
| 6 Multi-city service | 1–5 | 1 day |
| 7 Web API and pages | 6 | 1 day |
| 8 Day files and static export | 6 | 0.5 day |
| 9 Static site city picker | 8 | 0.5 day |
| 10 Rollout and docs | 7, 9 | 0.5 day |

Tasks 1, 2 and 3 are independent and can run in parallel; 4 and 5 after 1. Sydney keeps recording on the old code until Task 6 is deployed; Tasks 1–5 don't change runtime behaviour on their own.
