"""Optimized local day files for map playback.

    uv run build_playback.py --date 2026-10-16

downloads one day of recordings from S3 and writes ``DATA_DIR/<city>/playback/<date>.sqlite``
(``--city``, default sydney):
one row per recorded 10-minute window holding the gzip JSON body that
``/api/history/window`` serves, so playback of that day needs no S3 reads and
each window loads instantly. Windows missing from the file (e.g. the rest of
today, if built early) are still read from S3 by the server.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from .windows import SYDNEY_TZ, WINDOW

log = logging.getLogger(__name__)

WINDOW_S = int(WINDOW.total_seconds())


def day_file(data_dir: Path, day: date) -> Path:
    return data_dir / "playback" / f"{day.isoformat()}.sqlite"


def day_windows(day: date, tz: ZoneInfo) -> list[int]:
    """Window starts (epoch) of a *tz*-local day: 144, or 138/150 on DST change days."""
    first = datetime.combine(day, dt_time(), tzinfo=tz).timestamp()
    end = datetime.combine(day + timedelta(days=1), dt_time(), tzinfo=tz).timestamp()
    return list(range(int(first), int(end), WINDOW_S))


def read_window(data_dir: Path, start: int, tz: ZoneInfo = SYDNEY_TZ) -> bytes | None:
    """The stored body for the window at *start*, or None if no built day file has it."""
    day = datetime.fromtimestamp(start, tz=tz).date()
    path = day_file(data_dir, day)
    if not path.is_file():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute("SELECT body FROM windows WHERE start = ?", (start,)).fetchone()
    return row[0] if row else None


def build_day(history, day: date, path: Path, *, now: float | None = None) -> tuple[int, int]:
    """Write *day*'s recorded windows to *path*; returns (windows written, reports).

    Windows that may still change (recent ones, when building today) are left out,
    so the server keeps reading those from S3.
    """
    from .history import FINAL_AFTER_S, encode_window

    now = time.time() if now is None else now

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    windows = reports = 0
    with sqlite3.connect(tmp) as db:
        db.execute("CREATE TABLE windows (start INTEGER PRIMARY KEY, reports INTEGER, body BLOB)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        for start in day_windows(day, history.tz):
            if now < start + WINDOW_S + FINAL_AFTER_S:
                break
            feeds = history.collect(start)
            n = sum(len(r) for r in feeds.values())
            if n:
                db.execute("INSERT INTO windows VALUES (?, ?, ?)", (start, n, encode_window(start, feeds)))
                windows += 1
                reports += n
            log.info("%s: %d reports", datetime.fromtimestamp(start, tz=history.tz).strftime("%H:%M"), n)
        db.execute("INSERT INTO meta VALUES ('built_at', ?)", (datetime.now(tz=history.tz).isoformat(timespec="seconds"),))
    os.replace(tmp, path)
    return windows, reports


def resolve_date(value: str, tz: ZoneInfo, now: float | None = None) -> date:
    """YYYY-MM-DD, or 'today' / 'yesterday' in *tz* (for scheduled builds)."""
    today = datetime.fromtimestamp(time.time() if now is None else now, tz=tz).date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an optimized local playback file for one day from S3.")
    parser.add_argument("--city", default="sydney", help="city id from cities.toml (default: sydney)")
    parser.add_argument("--date", default="today",
                        help="city-local date: YYYY-MM-DD, 'today' (default) or 'yesterday'")
    parser.add_argument("--data-dir", type=Path,
                        help="data directory holding the city folders (default: DATA_DIR, or ./data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from .cities import load_cities
    from .config import ConfigError, S3Config, load_dotenv
    from .history import History
    from .storage import city_dir
    from .uploader import make_s3_client

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    args = parse_args(argv)
    load_dotenv()
    try:
        city = load_cities()[args.city]
    except KeyError:
        log.error("Unknown city %r", args.city)
        return 2
    try:
        day = resolve_date(args.date, city.timezone)
        s3 = S3Config.from_env()
    except (ValueError, ConfigError) as exc:
        log.error("%s", exc)
        return 2
    if s3 is None:
        log.error("S3_BUCKET is not set (environment or .env)")
        return 2
    data_dir = city_dir(args.data_dir or Path(os.environ.get("DATA_DIR") or "data"), city.id)
    path = day_file(data_dir, day)
    started = time.monotonic()
    # data_dir is also read for recordings not uploaded yet (when run next to the recorder).
    history = History(make_s3_client(s3), s3.bucket, city.s3_prefix, data_dir, city=city)
    windows, reports = build_day(history, day, path)
    log.info("Wrote %d windows, %d reports, %.1f MB to %s in %.0f s",
             windows, reports, path.stat().st_size / 1e6, path, time.monotonic() - started)
    if not windows:
        log.warning("No recorded data on %s", day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
