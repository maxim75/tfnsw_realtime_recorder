# Recorder service: 10-minute files, S3 upload, Coolify, control page

Date: 2026-10-16
Status: approved design, pending spec review

## Goal

Turn the CLI recorder (`main.py`) into a long-running service that:

1. Writes one data file per feed per 10-minute wall-clock window
   (12:00, 12:10, 12:20, ... Sydney time).
2. Uploads each completed window file to Hetzner Object Storage
   (S3-compatible), with credentials from environment variables.
3. Runs as a single Docker container deployed on Coolify.
4. Serves a web page, behind basic auth, to enable/disable feeds, change the
   polling interval and show recorder status.

Success: a deployed container records the enabled feeds continuously; every
completed window appears in the bucket as a gzipped CSV within a minute of the
window closing; settings changes on the page take effect at the next poll
without a restart; a redeploy loses no recorded rows.

## Decisions made with the user

| Topic | Decision |
|---|---|
| Web auth | HTTP basic auth, `ADMIN_USER` / `ADMIN_PASSWORD` env vars |
| File format | gzipped CSV, same columns as today |
| Storage | Hetzner Object Storage, location `nbg1` |
| Architecture | One process: FastAPI app + recorder thread + uploader thread |
| Default feeds | the 10 feeds currently in `FEEDS`, 30 s interval |

## Architecture

One Python process started by uvicorn. FastAPI's lifespan hook starts two
daemon threads (recorder, uploader) and stops them on shutdown.

```
TfNSW feeds ──poll──▶ Recorder thread ──rows──▶ RotatingWriter (per feed)
                          ▲                          │ window closed
                          │ reads each cycle         ▼
                     Settings (settings.json)   Upload queue ──▶ Uploader thread ──▶ S3
                          ▲                                            │ success
                          │ PUT /api/settings                          ▼
                     Web app (FastAPI, basic auth) ◀── status ──  delete local file
```

### Modules (`recorder/` package)

| Module | Responsibility | Depends on |
|---|---|---|
| `config.py` | `EnvConfig` (frozen dataclass read once from env) and `SettingsStore` (load/validate/atomically save `settings.json`; thread-safe getter returning an immutable snapshot) | stdlib |
| `feeds.py` | `KNOWN_FEEDS` (the 23 feeds that answered on 2026-10-16), `DEFAULT_FEEDS` (the 10), `feed_dir_name()` (`/` → `_`) | — |
| `windows.py` | Pure time helpers: `window_start(dt)` → Sydney-local aware datetime floored to 10 min; `window_label(start)` → `2026-10-16T12-10+1100` | stdlib `zoneinfo` |
| `writer.py` | `RotatingWriter` for one feed: appends rows to the current window's `.csv.gz`, rotates on window change, `close()` for early close; calls an `on_complete(path)` callback for every closed file | `windows` |
| `uploader.py` | `Uploader`: queue + worker thread; builds object keys; retries with backoff; deletes after success; scans `DATA_DIR/complete/` on start | `boto3` (injected client) |
| `recorder.py` | `Recorder`: polling loop thread (logic moved from `main.py`), de-duplication, per-feed `FeedStatus` | `tfnsw_trip_planner`, `writer`, `config` |
| `web.py` | FastAPI app, lifespan wiring, basic auth dependency, routes, serves `static/index.html` | all of the above |
| `static/index.html` | Control page, vanilla JS, no build step | — |

`main.py` stays as a thin CLI: `uv run main.py` runs the service locally
(`uvicorn.run("recorder.web:app", ...)`).

## Files on disk

`DATA_DIR` (default `/data`, a Coolify persistent volume):

```
/data/settings.json
/data/open/<feed_dir>/<label>[_pN].csv.gz       # window being written
/data/complete/<feed_dir>/<label>[_pN].csv.gz   # closed, waiting for upload
```

- Window = `[start, start + 10 min)`, computed from the poll's `fetched_at`
  in `Australia/Sydney`. Rows go to the window of `fetched_at`, not of the
  vehicle's GPS timestamp, so a file is complete when its wall-clock window
  ends.
- Label includes the UTC offset so the repeated hour at the end of daylight
  saving produces distinct names (`...T02-10+1100` vs `...T02-10+1000`).
- A file is gzip-written by opening `gzip.open(path, "at")` per append
  batch. Each append adds a gzip member; concatenated members are a valid
  gzip stream that `gzip`, pandas and DuckDB read as one file. The CSV header
  is written only when the file is created.
- **Rotation:** when a poll's window differs from the open file's window, the
  writer moves the open file to `complete/` (atomic `os.replace`, same
  filesystem) and calls `on_complete`. Rotation is driven by polls, so a file
  closes at the first poll after its window ends (≤ one interval late).
  The recorder also checks every second while sleeping and rotates any writer
  whose window has ended, so files close within ~1 s of the boundary even with
  long intervals.
- **Parts:** if a file for the same feed and label already exists in `open/`
  or `complete/` (restart mid-window, or feed re-enabled mid-window), the
  writer uses the next free `_p2`, `_p3`, ... suffix. Nothing is overwritten.
- **Startup recovery:** files left in `open/` by a crash are moved to
  `complete/` on start (they are partial windows; their names already
  identify them) and queued with everything else in `complete/`.

## S3

- Client: `boto3.client("s3", endpoint_url=S3_ENDPOINT_URL, region_name=...,
  config=Config(request_checksum_calculation="when_required",
  response_checksum_validation="when_required", retries={"max_attempts": 3}))`.
  The checksum settings avoid the default CRC headers of boto3 ≥ 1.36 that
  some S3-compatible stores reject.
- Region: `S3_REGION`; if unset and the endpoint host matches
  `<loc>.your-objectstorage.com`, use `<loc>`; else `us-east-1`.
- Object key (Hive-style partitions, date = Sydney-local date of the window):

  ```
  {S3_PREFIX}/feed={feed_dir}/date={YYYY-MM-DD}/{feed_dir}_{label}[_pN].csv.gz
  ```

  Example: `vehiclepos/feed=buses/date=2026-10-16/buses_2026-10-16T12-10+1100.csv.gz`.
  The feed and date are parsed back from the local path, so the uploader needs
  no extra metadata.
- Upload with `upload_file(path, bucket, key,
  ExtraArgs={"ContentType": "application/gzip"})`. No `ContentEncoding: gzip`:
  it makes some clients decompress transparently on download, so a `.csv.gz`
  would arrive as plain CSV under a `.gz` name.
- After a successful upload, delete the local file.
- Failure: log, record `last_error`, put the file back at the end of the queue
  and sleep with exponential backoff (5 s doubling, capped at 300 s) before the
  next attempt; reset backoff after any success.
- `S3_BUCKET` unset → uploader disabled: completed files stay in `complete/`;
  status reports `s3_configured: false`.

## Environment variables

| Variable | Required | Default |
|---|---|---|
| `TFNSW_API_KEY` | yes | — (app refuses to start) |
| `ADMIN_USER`, `ADMIN_PASSWORD` | yes | — (app refuses to start) |
| `S3_ENDPOINT_URL` | for Hetzner | AWS if unset; `https://nbg1.your-objectstorage.com` for this deployment |
| `S3_REGION` | no | derived from Hetzner endpoint, else `us-east-1` |
| `S3_BUCKET` | to enable uploads | — |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | with `S3_BUCKET` | — (start fails if bucket set without them) |
| `S3_PREFIX` | no | `vehiclepos` |
| `DATA_DIR` | no | `/data` (`./data` when run via `main.py`) |
| `PORT` | no | `8000` |

A local `.env` file is loaded if present (existing minimal loader), never
overriding real environment variables. Secrets are never logged or returned by
the API.

## Runtime settings

`settings.json`: `{"feeds": [...], "interval": 30}`.

- Created with defaults (10 feeds, 30 s) if missing.
- Validation: `interval` is a number in `[10, 3600]`; `feeds` is a list of
  unique names from `KNOWN_FEEDS` (may be empty, which pauses recording).
  Invalid file on disk → log error and use defaults (do not crash).
- Save: write `settings.json.tmp`, `fsync`, `os.replace`.
- The recorder reads a snapshot at the start of each cycle. Feeds removed from
  the set have their writer closed (file to `complete/`, queued). Feeds added
  get a writer at their first poll. Interval changes apply from the next sleep.

## Recorder

- Logic from current `main.py`: per-feed `last_seen` dedupe by GPS timestamp,
  skip `0,0`, 0.2 s spacing between requests (5 req/s limit),
  `TripPlannerError` caught per feed.
- `FeedStatus` per feed: `last_poll_at`, `vehicles`, `rows_written_last_poll`,
  `rows_in_window`, `current_file`, `last_error`, `last_error_at`,
  `total_rows`. Guarded by a lock; the web layer reads copies.
- Per-feed errors (`TripPlannerError` and any other `Exception`, e.g.
  `ChunkedEncodingError` or a disk write failure): logged (traceback for
  unexpected ones), recorded as the feed's `last_error`, other feeds continue.
  A feed's dedupe state is committed only after its write succeeds, and is
  pruned to the vehicles seen in the latest poll (bounds memory; a vehicle that
  vanishes and returns with the same timestamp is written again).
- If the recorder thread itself dies: log, close writers, mark
  `recorder.alive = False`, then terminate the process (injectable `on_fatal`,
  default `SIGTERM` to self) so uvicorn shuts down gracefully and the restart
  policy restarts the container. `/healthz` returns 500 meanwhile. The
  uploader does the same if its thread dies; an unlink failure after a
  successful upload is non-fatal and the file is not re-uploaded.
- Stop: an `Event`; the sleep loop wakes every ≤1 s to check it and to rotate
  ended windows.

## Web app

All routes except `/healthz` require basic auth (constant-time comparison with
`secrets.compare_digest`; `401` with `WWW-Authenticate: Basic`).

| Route | Purpose |
|---|---|
| `GET /` | `static/index.html` |
| `GET /api/status` | JSON: recorder alive, started_at, settings, estimated requests/day (`len(feeds) * 86400 / interval`) and quota (60,000), per-feed status for all `KNOWN_FEEDS` (with `enabled` flag), uploader status (`s3_configured`, bucket, endpoint, `pending`, `uploaded_total`, `last_uploaded_key`, `last_uploaded_at`, `last_error`, `last_error_at`) |
| `PUT /api/settings` | Body `{"feeds": [...], "interval": n}`; validate, save, return new settings; `422` with message on invalid input |
| `GET /healthz` | `200 ok` if recorder and uploader threads are alive, else `500` |

Page: header (running/stopped, uptime, interval input, requests/day vs quota,
red when over), feeds table (checkbox, feed, last poll, vehicles, rows in
current window, last error), uploads panel. Polls `/api/status` every 5 s.
Checkbox and interval changes are batched behind a "Save" button so a half-made
edit is never applied. Times shown in Sydney local time.

## Shutdown

uvicorn handles `SIGTERM` and runs the lifespan shutdown:

1. Stop the recorder (finish the in-flight request, no new polls).
2. Close all open writers → files to `complete/`, queued.
3. Uploader drains the queue for up to 15 s, then stops (S3 client timeouts:
   connect 5 s, read 30 s). Whatever remains in
   `complete/` is uploaded after the next start.

Worst case is about 25 s (recorder stop 10 s + uploader drain 15 s). Docker's
default stop timeout is 10 s; the README tells the user to set the stop timeout
to 30 s. If killed early, files remain on the
volume and nothing is lost.

## Docker and Coolify

- `Dockerfile`: `python:3.12.12-slim-bookworm`, copy `uv` binary from
  `ghcr.io/astral-sh/uv` (pinned tag), `uv sync --frozen --no-dev`, non-root
  user `app` owning `/data`, `EXPOSE 8000`, `HEALTHCHECK` with Python's
  `urllib` hitting `/healthz` (no curl in slim image), `CMD` runs uvicorn via
  the venv.
- `.dockerignore`: `.venv`, `data`, `.env`, `.git`, `__pycache__`.
- README "Deploy to Coolify" section: new resource from the Git repo, build
  pack Dockerfile, port 8000, domain, persistent storage mounted at `/data`,
  env vars (with the `nbg1` endpoint), stop grace 30 s, health check path
  `/healthz`.

## Dependencies

Add `fastapi`, `uvicorn` (plain, no `[standard]`), `boto3`, `tzdata` (zoneinfo
data for the slim image). Dev group:
`pytest`, `httpx` (for `TestClient`).

## Testing

Unit (pytest, no network):

- `windows`: flooring at :09:59.999/:10:00, label format, both sides of the
  April DST change (repeated 02:xx hour gets distinct labels) and the October
  change.
- `writer`: rows land in the right window file; rotation moves the file to
  `complete/` and calls `on_complete`; part suffix when a name exists;
  multi-member gzip reads back as one CSV with one header.
- `config`: defaults on missing file, rejection of unknown feeds / out-of-range
  interval, atomic save, corrupted file → defaults; Hetzner region derivation.
- `uploader`: key building from paths; success deletes the file; failure keeps
  the file and retries; startup scan of `open/` and `complete/`; disabled mode.
  Uses a fake client object with `upload_file`.
- `web`: 401 without/with wrong credentials, `/healthz` unauthenticated,
  `PUT /api/settings` validation and persistence, status shape. Recorder and
  uploader replaced by fakes via app factory parameters.

Live (run during implementation, reported with output):

- Run the service locally against real TfNSW feeds across one window boundary
  (~11 min; window length is fixed at 10 min). With Hetzner credentials in
  `.env` (if the user provides them), verify the uploaded object with
  `head_object`, download it and check it reads back as gzip CSV with the
  expected header.
- `docker build` and `docker run` locally with `/data` mounted; check
  `/healthz`, the page with basic auth, and a clean stop on `docker stop`.

## Out of scope

- Multiple replicas (one container only; two would double-record).
- Retention/lifecycle rules in the bucket (configure in Hetzner if needed).
- Selecting v1/v2 endpoint per feed.
- Parquet output.
