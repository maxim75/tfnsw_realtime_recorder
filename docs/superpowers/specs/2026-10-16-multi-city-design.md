# Multiple cities: recorder, maps and static site

Date: 2026-10-16
Status: draft for review

## Goal

Record, map and replay vehicle positions for several cities from one recorder
deployment, starting with Prague next to Sydney. Adding a later city with a
keyless GTFS-Realtime vehicle-positions feed should be a config entry, not
code.

Success:

- One container polls Sydney and Prague, each at its own interval and rate
  limit; a slow or failing city never delays the other.
- Sydney keeps working exactly as now, including its existing S3 recordings,
  local files and settings; nothing has to be re-uploaded or renamed in S3.
- Prague recordings land in S3 under their own prefix, in Prague local time.
- The status page, `/map` (live and playback) and the static GitHub Pages site
  each let the viewer pick the city; vehicles show proper line names (Prague
  `B`, `22`, `136`, not `L992`) and are grouped by mode (metro, tram, bus,
  rail, ferry …).

## Decisions (agreed)

| Topic | Decision |
|---|---|
| Deployment | One container records all configured cities, one poller thread per city |
| Static site | One site (`tfnsw-playback-map`) with a city picker, `data/<city>/` folders |
| First new city | Prague (PID via Golemio, keyless) |

## Prague feed (checked 2026-10-16)

- URL `https://api.golemio.cz/v2/vehiclepositions/gtfsrt/vehicle_positions.pb`,
  no key; 430 kB per fetch; `Cache-Control: s-maxage=40`.
- 2,267 vehicles at 22:30 CET-ish; all with `route_id`, `trip_id`,
  `vehicle.id` (stable, e.g. `metro-B-25-25`); 87 % with bearing, 33 % with
  speed. Median report age 39 s (p90 107 s): fewer reports than Sydney.
- `route_id` is GTFS (`L992`), not a line name. PID static GTFS
  (`https://data.pid.cz/PID_GTFS.zip`, 53 MB, updated daily) maps it via
  `routes.txt` to `route_short_name` (`B`) and `route_type` (1 = metro).
- Licence and attribution terms of Golemio/PID open data: **to confirm** before
  publishing Prague data on the public site (expected CC BY 4.0 with
  attribution to "Golemio / PID").

## Design

### 1. City config: `cities.toml`

Checked into the repo (and the image); `CITIES_FILE` can point elsewhere.
TOML so the stdlib (`tomllib`) reads it. Secrets are referenced by env var
name, never stored in the file.

```toml
[sydney]
name = "Sydney"
timezone = "Australia/Sydney"
center = [151.05, -33.85]            # lon, lat
zoom = 10.3
s3_prefix = "vehiclepos"             # existing layout, unchanged
attribution = "Transport for NSW (CC BY 4.0)"
interval = 30                        # default poll interval, s
max_requests_per_second = 5
daily_quota = 60000
default_feeds = ["buses", "sydneytrains", "metro", ...]   # today's 10
route_label = { pattern = '_(?P<label>[^_]+)$' }          # 2510_333 -> 333
headers = { Authorization = "apikey ${TFNSW_API_KEY}" }   # env expanded at load

  [[sydney.feeds]]
  id = "buses"
  url = "https://api.transport.nsw.gov.au/v1/gtfs/vehiclepos/buses"
  mode = "bus"
  # ... the 23 known feeds; v1/v2 is just the URL

[prague]
name = "Prague"
timezone = "Europe/Prague"
center = [14.44, 50.08]
zoom = 11
s3_prefix = "vehiclepos/city=prague"
attribution = "Golemio / PID (CC BY 4.0)"   # confirm
interval = 30
max_requests_per_second = 1
static_gtfs = "https://data.pid.cz/PID_GTFS.zip"   # line names and modes
default_feeds = ["all"]

  [[prague.feeds]]
  id = "all"
  url = "https://api.golemio.cz/v2/vehiclepositions/gtfsrt/vehicle_positions.pb"
  mode = "bus"     # fallback when a route isn't in routes.txt
```

A city whose `headers` reference an unset env var is disabled at startup with a
logged error (Sydney without `TFNSW_API_KEY` must not stop Prague). The
`TFNSW_API_KEY` requirement in `EnvConfig` goes away; `ADMIN_*` stay required.

### 2. Fetching: generic GTFS-Realtime client

`recorder/gtfsrt.py`: `GtfsRtClient(timeout=8)` with
`fetch(url, headers) -> list[Position]`; `Position` is a small dataclass with
the fields `new_rows`, the live hub and history already use (vehicle_id,
trip_id, route_id, latitude, longitude, bearing, speed, timestamp). Network,
HTTP and decode errors raise `FetchError`. The `tfnsw_trip_planner` dependency
is removed (TfNSW feeds are plain GTFS-R URLs with an `apikey` header).

### 3. Line names and modes

`recorder/routes.py`: per city, `RouteInfo.label(route_id) -> str` and
`RouteInfo.mode(feed, route_id) -> str` with modes
`metro | tram | rail | bus | ferry | trolleybus | cable | other`.

- From static GTFS when `static_gtfs` is set: download the zip at most once a
  day (ETag/Last-Modified), extract only `routes.txt`, cache
  `DATA_DIR/<city>/routes.json`. `route_type` maps to mode (0 tram, 1 metro,
  2 rail, 3 bus, 4 ferry, 5/6/7 cable, 11 trolleybus, extended 100–1700 by
  range). A failed download keeps the cached file; with none, labels fall back
  to the raw `route_id` and mode to the feed's `mode`.
- Else `route_label.pattern` (Sydney) and the feed's `mode`.

Labels and modes are added when serving (live view, history, static export);
recorded CSV files stay as they are.

### 4. Storage layout

Local `DATA_DIR`:

```
DATA_DIR/settings.json                     # all cities
DATA_DIR/<city>/open/<feed_dir>/...        # window being written
DATA_DIR/<city>/complete/<feed_dir>/...    # waiting for upload
DATA_DIR/<city>/playback/<date>.sqlite     # built day files
DATA_DIR/<city>/routes.json                # cached line names
```

On first start with the new code, legacy `DATA_DIR/{open,complete,playback}`
are moved into `DATA_DIR/sydney/` (same filesystem, `os.replace`), before
recovery and the uploader scan.

S3: each city's `s3_prefix` replaces today's `S3_PREFIX` env var (which, if
set, keeps overriding Sydney's prefix for compatibility). Under a prefix the
layout is unchanged: `feed=<feed_dir>/date=<YYYY-MM-DD>/<feed_dir>_<label>.csv.gz`
with the date and label in that city's local time (Prague labels carry
`+0200`/`+0100`).

### 5. Settings

`settings.json` becomes `{"cities": {"sydney": {"feeds": [...], "interval": 30},
"prague": {...}}}`. A legacy flat file `{"feeds", "interval"}` is read as
Sydney's settings and rewritten in the new form on the next save. Cities
missing from the file get their config defaults. Validation is per city
(feeds from that city's config, interval 10–3600).

### 6. Service structure

`build_components` creates, per enabled city: a `Recorder` (existing class,
parameterised with the city's feeds, timezone, request spacing, data dir), a
`LiveHub`, a `History` and `RouteInfo`. One shared `Uploader` maps a file's
city (from its path) to the city's S3 prefix. `/healthz` is healthy only if
every city's recorder thread and the uploader are alive; a dead thread still
terminates the process (existing behaviour).

Windows module functions take the city's timezone (`window_start(dt, tz)`,
`window_label`, `day_windows(day, tz)`), defaulting to Sydney only inside
tests' compatibility helpers.

### 7. Web API and pages

- `GET /api/cities`: id, name, timezone, center, zoom, attribution, the modes
  present, quota info.
- Existing endpoints take `?city=<id>` (default: first city in config):
  `/api/status`, `PUT /api/settings`, `/api/live`, `/api/live/stream`,
  `/api/history/window`.
- Reports gain two fields, label and mode:
  `[vehicle_id, route_id, ts, lon, lat, bearing, speed, label, mode]`
  (old clients ignore extra fields; the pages are updated together).
- Status page: a city tab bar; feed table and quota per city.
- `/map`: city picker (also `#prague` in the URL; last choice remembered in
  `localStorage`), map moves to the city's centre; mode checkboxes built from
  the city's modes; times shown in the city's timezone; playback date/time
  inputs in the city's timezone.

### 8. Static site

- `export_static.py --city prague --date ... --out ../tfnsw-playback-map/data`
  writes `data/<city>/index.json` and `data/<city>/w/*.json.gz`, and updates
  `data/cities.json` (id, name, center, zoom, timezone, attribution, dates
  available). Window format v2: vehicles become
  `[feed_index, vehicle_id, route_id, label, mode]`; `index.json` gains
  `"format": 2`.
- The page reads `data/cities.json`, shows a city picker (`#prague/17:30`),
  uses the city's timezone and attribution. Existing `data/index.json` and
  `data/w/` move to `data/sydney/` (re-exported in format v2).
- The repo name stays for now. Renaming it to a city-neutral name is optional
  and would change the public Pages URL.

## Error handling

- City-level isolation: each city's recorder thread catches per-feed errors as
  today; a city's thread dying terminates the process (restart), as today.
- Missing secrets for a city: city disabled with a status-page notice.
- Static GTFS unavailable: cached `routes.json`, else raw route ids.
- Prague's feed caches ~40 s: polling every 30 s sometimes returns the same
  snapshot; de-duplication by GPS time already handles it.

## Testing

Unit (no network): config loading and env expansion; disabled city on missing
env; route info from a fake GTFS zip and from a pattern; mode mapping;
generic client against a fake HTTP session (OK, HTTP error, bad protobuf);
legacy data dir and settings migration; per-city settings validation;
recorder with a non-Sydney timezone (window labels `+0200`); uploader prefix
per city; web endpoints with `?city=`; history and static export per city and
format v2 round trip.

Live checks: run locally with Sydney and Prague for one window boundary;
confirm Prague objects in S3 under `vehiclepos/city=prague/` with `+0200`
labels; play both cities in `/map`; export a Prague day to the site and check
the city picker on GitHub Pages.

## Out of scope

- Cities without vehicle positions (Berlin: trip updates only).
- Feeds needing per-request signing or OAuth.
- Per-city separate containers (the code allows it via config, not tested).
- Snapping to road shapes.
