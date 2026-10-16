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

## Feeds

Recorded by default (switch any feed on or off on the web page):

`buses`, `sydneytrains`, `metro`, `nswtrains`, `ferries/sydneyferries`,
`ferries/MFF`, `lightrail/cbdandsoutheast`, `lightrail/innerwest`,
`lightrail/parramatta`, `regionbuses/sydneysurrounds`

Also available:

`lightrail/newcastle`, `regionbuses/centralwestandorana`,
`regionbuses/centralwestandorana2`, `regionbuses/farwest`,
`regionbuses/newcastlehunter`, `regionbuses/newenglandnorthwest`,
`regionbuses/northcoast`, `regionbuses/northcoast2`, `regionbuses/northcoast3`,
`regionbuses/riverinamurray`, `regionbuses/riverinamurray2`,
`regionbuses/southeasttablelands`, `regionbuses/southeasttablelands2`

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
An upload retried after an ambiguous failure (e.g. a timeout after S3 stored
the object) can occasionally create a `_pN` duplicate; deduplicate on
`vehicle_id` + `timestamp` downstream.

Hetzner Object Storage rejects a share of valid requests with HTTP 403
(about 15% in testing); an immediate retry succeeds. The uploader retries a
403 up to 3 times within the same upload before treating it as a failure, so
occasional `403`/`AccessDenied` lines in the logs are expected. A persistent
403 on every upload means the credentials or bucket name are wrong.

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

## Map in kepler.gl

`export_csv.py` downloads the recorded files for a date and time range from
the bucket and combines them into one CSV for <https://kepler.gl/demo>. It uses
the `S3_*` variables from the environment or `.env`.

```bash
uv run export_csv.py --date 2026-10-16 --feed buses --route 333     # one route, whole day
uv run export_csv.py --date 2026-10-16 --from 07:00 --to 09:00 --feed sydneytrains
uv run export_csv.py --date 2026-10-16 --from 18:00 --to 18:30 --feed all
```

Options: `--date` (Sydney date, default today), `--from`/`--to` (`HH:MM`,
`--to` exclusive, `24:00` allowed), `--feed` (repeatable, or `all`; default
`buses`), `--route` (route name such as `333`, or the full `route_id`;
repeatable), `--max-age` (minutes, default 15: drops reports whose GPS time is
that much older than the poll; some vehicles send fixes years old), `-o` (output
file; by default it's named after the options, e.g. `buses_333_2026-10-16.csv`).

The CSV has the recorded columns plus `feed` and `time` (GPS time as
`YYYY-MM-DD HH:MM:SS`, Sydney local). Repeated reports of the same vehicle
position are written once.

In kepler.gl: **Add Data** → drop the CSV (points are placed from `latitude`/
`longitude`) → **Filters** → add a filter on `time` and press play to animate.
Colour the layer by `route_id` or `speed` if you like. Keep the export to a
few hundred thousand rows at most: all buses for a whole day is about 2.5
million rows, too many for the browser, so pick a route or a shorter time
range.

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

5. **Health check:** leave Coolify's own health check disabled. It runs
   inside the container with curl/wget, which the slim image does not have.
   The image's Docker `HEALTHCHECK` (Python, hits `/healthz` every 30 s)
   reports container health instead.
6. Docker's default stop timeout is 10 s, but shutdown can take up to ~25 s
   (recorder stop up to 10 s, then the upload drain up to 15 s). Set the
   container stop timeout to 30 s if Coolify offers it (e.g. via custom Docker
   run options: `--stop-timeout 30`). Otherwise the last partial window is
   uploaded after the next start, so nothing is lost.
7. Run **one** instance only; two would record every feed twice.
8. Use a long, random `ADMIN_PASSWORD` and expose the page only over the
   HTTPS domain (basic auth sends the password with every request).
9. Logs: one INFO summary line per poll cycle (feeds, vehicles, new rows,
   errors); per-feed lines are DEBUG. Docker's default `json-file` log driver
   does not rotate, so configure log rotation in Coolify/Docker if needed.

If the recorder or uploader thread dies, the process terminates itself so the
restart policy restarts the container (`/healthz` reports 500 meanwhile).
Errors on a single feed or file never stop recording; they show on the page.
