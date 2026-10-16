"""Record TfNSW live vehicle positions (GTFS-Realtime) for later analysis.

Polls one or more ``gtfs/vehiclepos`` feeds via ``tfnsw_trip_planner`` and
appends every *new* position report to a CSV file per feed per day:

    data/<feed>/<YYYY-MM-DD>.csv      e.g. data/buses/2026-10-16.csv

A vehicle report is only written when its GPS timestamp changes, so polling
faster than the feed refreshes does not create duplicate rows.

Usage:
    export TFNSW_API_KEY=...
    uv run main.py                              # all FEEDS, every 30s, until Ctrl+C
    uv run main.py -m buses -m sydneytrains -i 15
    uv run main.py -m ferries/sydneyferries --duration 3600
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tfnsw_trip_planner import TripPlannerClient, TripPlannerError

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# Feeds recorded by default (Sydney-area services).
FEEDS = [
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
]

# TfNSW Open Data default limits per API key.
MAX_REQUESTS_PER_SECOND = 5
DAILY_REQUEST_QUOTA = 60_000

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

log = logging.getLogger("recorder")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so the key can live next to the script."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class FeedRecorder:
    """Appends new positions for one feed to daily CSV files."""

    def __init__(self, mode: str, out_dir: Path) -> None:
        self.mode = mode
        self.dir = out_dir / mode.strip("/").replace("/", "_")
        self.dir.mkdir(parents=True, exist_ok=True)
        # Last GPS timestamp written per vehicle, for de-duplication.
        self.last_seen: dict[str, datetime | None] = {}

    def record(self, client: TripPlannerClient) -> tuple[int, int]:
        fetched_at = datetime.now(tz=SYDNEY_TZ)
        positions = client.vehicle_positions(self.mode)

        rows = []
        for p in positions:
            if p.latitude == 0.0 and p.longitude == 0.0:
                continue  # no GPS fix
            key = p.vehicle_id or p.trip_id or f"{p.latitude},{p.longitude}"
            if key in self.last_seen and self.last_seen[key] == p.timestamp:
                continue  # unchanged since last poll
            self.last_seen[key] = p.timestamp
            rows.append(
                {
                    "fetched_at": fetched_at.isoformat(timespec="seconds"),
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

        if rows:
            path = self.dir / f"{fetched_at:%Y-%m-%d}.csv"
            new_file = not path.exists()
            with path.open("a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerows(rows)
        return len(positions), len(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "-m",
        "--mode",
        action="append",
        help=(
            "Feed to record; repeat for several (default: all of "
            + ", ".join(FEEDS)
            + ")"
        ),
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=30.0, help="Seconds between polls (default 30)"
    )
    parser.add_argument(
        "-d", "--duration", type=float, help="Stop after this many seconds (default: run forever)"
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=Path("data"), help="Output directory (default ./data)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_dotenv()
    api_key = os.environ.get("TFNSW_API_KEY")
    if not api_key:
        log.error("Set TFNSW_API_KEY (environment or .env file)")
        return 2

    recorders = [FeedRecorder(m, args.out) for m in (args.mode or FEEDS)]

    per_day = len(recorders) * 86400 / args.interval
    if per_day > DAILY_REQUEST_QUOTA:
        log.warning(
            "%d feeds every %gs is ~%d requests/day, above the default quota of %d; "
            "use --interval %d or more for continuous recording",
            len(recorders), args.interval, per_day, DAILY_REQUEST_QUOTA,
            len(recorders) * 86400 // DAILY_REQUEST_QUOTA + 1,
        )

    stop = False

    def handle_signal(*_: object) -> None:
        nonlocal stop
        stop = True
        log.info("Stopping after current poll...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    deadline = time.monotonic() + args.duration if args.duration else None
    with TripPlannerClient(api_key=api_key) as client:
        while not stop:
            started = time.monotonic()
            for n, rec in enumerate(recorders):
                if stop:
                    break
                if n:
                    time.sleep(1 / MAX_REQUESTS_PER_SECOND)  # stay under the rate limit
                try:
                    total, written = rec.record(client)
                    log.info("%-34s vehicles=%5d new=%5d", rec.mode, total, written)
                except TripPlannerError as exc:
                    # Transient API/network errors shouldn't kill a long recording.
                    log.warning("%s: %s", rec.mode, exc)

            if deadline and time.monotonic() >= deadline:
                break
            # Sleep in small steps so Ctrl+C is responsive.
            wake = started + args.interval
            while not stop and time.monotonic() < wake:
                time.sleep(min(0.5, wake - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
