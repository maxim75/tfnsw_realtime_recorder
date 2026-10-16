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
uv run main.py                                   # buses every 15s until Ctrl+C
uv run main.py -m buses -m sydneytrains -m ferries/sydneyferries
uv run main.py -m metro -i 10 --duration 3600    # one hour, 10s polling
```

Options: `-m/--mode` (repeatable; any `gtfs/vehiclepos/` feed path),
`-i/--interval` seconds, `-d/--duration` seconds, `-o/--out` directory.

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
(about 5× smaller gzipped). Trains and ferries are much smaller.
