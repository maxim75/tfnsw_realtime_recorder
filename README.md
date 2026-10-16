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
