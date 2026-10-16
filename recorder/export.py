"""Combine recorded window files from S3 into one CSV for kepler.gl.

    uv run export_csv.py --date 2026-10-16 --feed buses --route 333
    uv run export_csv.py --date 2026-10-16 --from 07:00 --to 09:00 --feed sydneytrains
    uv run export_csv.py --date 2026-10-16 --feed all --from 18:00 --to 18:30
    uv run export_csv.py --city prague --date 2026-10-16 --feed all --route B

Reads S3 settings from the environment or .env (same variables as the recorder).
Output columns: ``feed``, ``time`` (GPS time as ``YYYY-MM-DD HH:MM:SS`` in the
city's local time, which kepler.gl detects as a timestamp), then the recorded columns.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import logging
import re
import sys
from datetime import date, datetime, time, timedelta
from typing import TextIO
from zoneinfo import ZoneInfo

from .cities import load_cities
from .config import ConfigError, S3Config, load_dotenv
from .feeds import feed_dir_name
from .uploader import make_s3_client, retry_denials
from .windows import SYDNEY_TZ, WINDOW
from .writer import FIELDS

log = logging.getLogger(__name__)

OUT_FIELDS = ["feed", "time", *FIELDS]
LARGE_FOR_KEPLER = 250_000  # rows; beyond this the kepler.gl demo gets sluggish

_LABEL = re.compile(r"_(?P<label>\d{4}-\d\d-\d\dT\d\d-\d\d[+-]\d{4})(?:_p\d+)?\.csv\.gz$")


def window_of(key: str) -> datetime | None:
    match = _LABEL.search(key)
    return datetime.strptime(match["label"], "%Y-%m-%dT%H-%M%z") if match else None


def list_keys(
    client, bucket: str, prefix: str, feed: str, start: datetime, end: datetime, tz: ZoneInfo = SYDNEY_TZ
) -> list[str]:
    """Keys of *feed*'s window files overlapping ``[start, end)``, in window order (dates in *tz*)."""
    keys = []
    day = start.astimezone(tz).date()
    last_day = (end - timedelta(microseconds=1)).astimezone(tz).date()
    while day <= last_day:
        day_prefix = f"feed={feed_dir_name(feed)}/date={day.isoformat()}/"
        kwargs = {"Bucket": bucket, "Prefix": f"{prefix}/{day_prefix}" if prefix else day_prefix}
        while True:
            resp = retry_denials(client.list_objects_v2, **kwargs)
            for obj in resp.get("Contents", []):
                window = window_of(obj["Key"])
                if window is not None and window < end and window + WINDOW > start:
                    keys.append(obj["Key"])
            if not resp.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        day += timedelta(days=1)
    return sorted(keys, key=lambda k: (window_of(k), k))


def read_rows(client, bucket: str, key: str) -> list[dict[str, str]]:
    body = retry_denials(client.get_object, Bucket=bucket, Key=key)["Body"].read()
    return list(csv.DictReader(io.StringIO(gzip.decompress(body).decode())))


def route_matches(route_id: str, routes: set[str]) -> bool:
    """Match ``2510_333`` by full id or by route name (the part after the last ``_``)."""
    return not routes or route_id in routes or route_id.rsplit("_", 1)[-1] in routes


def export(
    client,
    bucket: str,
    prefix: str,
    feeds: list[str],
    start: datetime,
    end: datetime,
    routes: set[str],
    out: TextIO,
    max_age: timedelta | None = None,
    tz: ZoneInfo = SYDNEY_TZ,
) -> tuple[int, int]:
    """Write matching rows to *out*; returns (files read, rows written). Times are in *tz*.

    Reports whose GPS time is more than *max_age* older than the poll are dropped
    (some vehicles report fixes that are hours or years old).
    """
    writer = csv.DictWriter(out, fieldnames=OUT_FIELDS)
    writer.writeheader()
    files = rows = stale = 0
    for feed in feeds:
        # A vehicle's unchanged report can be recorded twice: in a _pN part after an
        # ambiguous upload retry, or after a restart. Rows are in time order, so
        # comparing with the vehicle's previous GPS time removes those repeats.
        last_ts: dict[str, str] = {}
        for key in list_keys(client, bucket, prefix, feed, start, end, tz):
            files += 1
            for row in read_rows(client, bucket, key):
                fetched_at = datetime.fromisoformat(row["fetched_at"])
                if not start <= fetched_at < end:
                    continue
                if not route_matches(row["route_id"], routes):
                    continue
                if last_ts.get(row["vehicle_id"]) == row["timestamp"]:
                    continue
                last_ts[row["vehicle_id"]] = row["timestamp"]
                gps_time = datetime.fromisoformat(row["timestamp"] or row["fetched_at"])
                if max_age and fetched_at - gps_time > max_age:
                    stale += 1
                    continue
                writer.writerow(
                    {"feed": feed, "time": gps_time.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"), **row}
                )
                rows += 1
        log.info("%s: done, %d rows so far", feed, rows)
    if stale:
        log.info("Dropped %d stale reports (GPS time older than %s before the poll)", stale, max_age)
    return files, rows


def time_range(
    day: str, start: str | None, end: str | None, tz: ZoneInfo = SYDNEY_TZ
) -> tuple[datetime, datetime]:
    """``[start, end)`` on *tz*-local *day*; times are HH:MM, ``24:00`` = end of day."""
    d = date.fromisoformat(day)

    def at(hhmm: str) -> datetime:
        if hhmm == "24:00":
            return datetime.combine(d + timedelta(days=1), time(), tzinfo=tz)
        return datetime.combine(d, time.fromisoformat(hhmm), tzinfo=tz)

    first, last = at(start or "00:00"), at(end or "24:00")
    if last <= first:
        raise ValueError("--to must be after --from")
    return first, last


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    cities = load_cities()
    parser = argparse.ArgumentParser(
        description="Combine recorded vehicle positions from S3 into one CSV for kepler.gl."
    )
    parser.add_argument("--city", default="sydney", choices=list(cities), help="city (default: sydney)")
    parser.add_argument("--date", help="city-local date, YYYY-MM-DD (default: today)")
    parser.add_argument("--from", dest="start", metavar="HH:MM", help="start time (default 00:00)")
    parser.add_argument("--to", dest="end", metavar="HH:MM", help="end time, exclusive (default 24:00)")
    parser.add_argument(
        "--feed", action="append", metavar="FEED",
        help="feed to export, e.g. buses, sydneytrains, ferries/sydneyferries; repeat for "
        "several, or 'all' (default: the city's first feed). Feed names are in cities.toml",
    )
    parser.add_argument(
        "--route", action="append", default=[],
        help="only this route, e.g. 333 or 2510_333; repeatable",
    )
    parser.add_argument(
        "--max-age", type=float, default=15, metavar="MINUTES",
        help="drop reports whose GPS time is this much older than the poll (default 15; 0 keeps all)",
    )
    parser.add_argument("-o", "--out", help="output file (default: derived from the options)")
    args = parser.parse_args(argv)
    city = cities[args.city]
    known = [f.id for f in city.feeds]
    args.feed = args.feed or [known[0]]
    unknown = [f for f in args.feed if f not in (*known, "all")]
    if unknown:
        parser.error(f"unknown feed for {city.name}: {', '.join(unknown)} (choose from {', '.join(known)}, all)")
    args.date = args.date or datetime.now(tz=city.timezone).date().isoformat()
    args.feeds = known if "all" in args.feed else args.feed
    args.prefix, args.tz = city.s3_prefix, city.timezone
    return args


def default_output(args: argparse.Namespace) -> str:
    parts = [] if args.city == "sydney" else [args.city]  # Sydney's names stay as before
    parts.append("all" if "all" in args.feed else "+".join(feed_dir_name(f) for f in args.feed))
    parts += args.route
    parts.append(args.date)
    if args.start or args.end:
        parts.append(f"{(args.start or '00:00').replace(':', '')}-{(args.end or '24:00').replace(':', '')}")
    return "_".join(parts) + ".csv"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    args = parse_args(argv)
    try:
        start, end = time_range(args.date, args.start, args.end, args.tz)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    load_dotenv()
    try:
        s3 = S3Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if s3 is None:
        log.error("S3_BUCKET is not set (environment or .env)")
        return 2

    out_path = args.out or default_output(args)
    log.info("Exporting %s, %s to %s, from s3://%s/%s", ", ".join(args.feed), start, end, s3.bucket, args.prefix)
    with open(out_path, "w", newline="") as out:
        files, rows = export(
            make_s3_client(s3), s3.bucket, args.prefix, args.feeds, start, end, set(args.route), out,
            max_age=timedelta(minutes=args.max_age) if args.max_age else None, tz=args.tz,
        )
    log.info("Wrote %d rows from %d files to %s", rows, files, out_path)
    if rows == 0:
        log.warning("No rows matched: check the date, time range, feed and route")
    elif rows > LARGE_FOR_KEPLER:
        log.warning("%d rows may be slow in kepler.gl; narrow the time range or pick a route", rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
