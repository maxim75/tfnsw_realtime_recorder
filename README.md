# tfnsw-realtime-recorder

Records Transport for NSW **live vehicle positions** (GTFS-Realtime) to CSV files
for later analysis and visualisation, using
[tfnsw_trip_planner](https://github.com/maxim75/tfnsw_trip_planner).

## Setup

You need a TfNSW Open Data API key with the **Public Transport – Realtime Vehicle
Positions** product enabled. Put it in the environment or a `.env` file:

```
TFNSW_API_KEY=your_key
```

## Usage

```bash
uv run main.py                                   # the 10 default feeds every 30s until Ctrl+C
uv run main.py -m buses -m sydneytrains -m ferries/sydneyferries
uv run main.py -m metro -i 10 --duration 3600    # one hour, 10s polling
```

Options: `-m/--mode` (repeatable; any `gtfs/vehiclepos/` feed path, default
all feeds below), `-i/--interval` seconds (default 30), `-d/--duration`
seconds, `-o/--out` directory.

Default feeds (`FEEDS` in `main.py`):

| feed | service |
|---|---|
| `buses` | Sydney metro buses |
| `sydneytrains` | Sydney Trains |
| `metro` | Sydney Metro |
| `nswtrains` | NSW TrainLink |
| `ferries/sydneyferries` | Sydney Ferries |
| `ferries/MFF` | Manly Fast Ferry |
| `lightrail/cbdandsoutheast` | Sydney light rail L2/L3 |
| `lightrail/innerwest` | Inner West light rail L1 |
| `lightrail/parramatta` | Parramatta light rail |
| `regionbuses/sydneysurrounds` | Sydney Surrounds regional buses |

Other feeds that answered on 2026-10-16 and can be added with `-m`:
`lightrail/newcastle` and `regionbuses/` + `centralwestandorana`,
`centralwestandorana2`, `farwest`, `newcastlehunter`, `newenglandnorthwest`,
`northcoast`, `northcoast2`, `northcoast3`, `riverinamurray`, `riverinamurray2`,
`southeasttablelands`, `southeasttablelands2`.

## Output

One CSV per feed per day (Sydney time), appended to as the recorder runs:

```
data/buses/2026-10-16.csv
data/sydneytrains/2026-10-16.csv
data/ferries_sydneyferries/2026-10-16.csv
```

| column | notes |
|---|---|
| `fetched_at` | when the recorder polled (Sydney time, ISO 8601) |
| `timestamp` | GPS report time from the vehicle |
| `vehicle_id`, `trip_id`, `route_id` | GTFS identifiers (`route_id` is `<prefix>_<shortName>`, e.g. `2510_992`) |
| `latitude`, `longitude` | WGS84 |
| `bearing`, `speed` | degrees / m/s; empty when the feed doesn't provide them |

A row is written only when a vehicle's `timestamp` changes, so polling faster
than the feed refreshes doesn't create duplicates. Vehicles with no GPS fix
(`0,0`) are skipped.

Load for analysis:

```python
import pandas as pd
df = pd.read_csv("data/buses/2026-10-16.csv", parse_dates=["fetched_at", "timestamp"])
track = df[df.vehicle_id == df.vehicle_id.iloc[0]].sort_values("timestamp")
```

## Feed characteristics (measured 2026-10-16, 11:22 AEDT)

| feed | vehicles | report interval | lag behind real time | speed/bearing |
|---|---|---|---|---|
| buses | ~1,550 | ~15 s | ~12 s | yes |
| sydneytrains (v2) | ~225 | ~40 s | ~25 s | no |
| ferries/sydneyferries | ~20 | ~15 s | ~13 s | yes |

Storage: buses at 15 s polling is ~0.9 MB/min, roughly **1.3 GB/day** of CSV
(about 5× smaller gzipped); at the default 30 s it's roughly half that. The
other feeds are much smaller.

## API limits

Two TfNSW API key limits apply:

- **Rate:** ~5 requests/second. The recorder spaces requests 0.2 s apart; going
  faster returns HTTP 429.
- **Daily quota:** 60,000 requests by default. Requests per day =
  `feeds × 86400 / interval`, so the 10 default feeds at 30 s use 28,800
  requests/day. The recorder warns at startup if you'd go over the quota.
  For finer detail on one feed, run it in its own process (e.g. `-m buses -i 15`,
  5,760 requests/day) and the others with `-m ...` at a longer interval. Don't
  let two processes record the same feed: each de-duplicates only its own rows.
