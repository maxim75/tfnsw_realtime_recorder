"""Export one recorded day as static files for the GitHub Pages playback site.

    uv run export_static.py --date 2026-10-16 --out ../tfnsw-playback-map/data

writes ``<out>/index.json`` (day bounds, windows with data, report counts) and
``<out>/w/<window start epoch>.json.gz``, one compact gzip file per 10-minute
window. Data comes from the local playback day file if one was built, else S3.

Compact window format: vehicles are listed once; their reports are stored as
per-vehicle runs in flat columns, delta-encoded so they gzip well::

    {"start": epoch, "feeds": ["buses", ...],
     "vehicles": [[feed_index, vehicle_id, route_id], ...],
     "n": [reports per vehicle, ...],
     "t": [...],  # seconds: first report relative to start, then since previous report
     "x": [...], "y": [...],  # lon/lat * 1e5: first absolute, then delta from previous
     "b": [...],  # bearing in whole degrees, -1 = none
     "s": [...]}  # speed in 0.1 m/s, -1 = none
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from .playback import day_windows, resolve_date
from .windows import SYDNEY_TZ, WINDOW

log = logging.getLogger(__name__)

WINDOW_S = int(WINDOW.total_seconds())
SCALE = 1e5  # coordinates to 5 decimals (~1 m)


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


def encode_window(start: int, feeds: dict[str, list[list]]) -> dict:
    """Compact form of ``{feed: [[vehicle_id, route_id, ts, lon, lat, bearing, speed], ...]}``."""
    out = {"start": start, "feeds": list(feeds), "vehicles": [], "n": [], "t": [], "x": [], "y": [], "b": [], "s": []}
    for fi, reports in enumerate(feeds.values()):
        by_vehicle = defaultdict(list)
        for r in reports:
            by_vehicle[(r[0], r[1])].append(r)
        for (vid, route), runs in sorted(by_vehicle.items()):
            runs.sort(key=lambda r: r[2])
            out["vehicles"].append([fi, vid, route])
            out["n"].append(len(runs))
            pt, px, py = start, 0, 0
            for _, _, ts, lon, lat, bearing, speed in runs:
                xi, yi = _round(lon * SCALE), _round(lat * SCALE)
                out["t"].append(ts - pt)
                out["x"].append(xi - px)
                out["y"].append(yi - py)
                out["b"].append(-1 if bearing is None else _round(bearing) % 360)
                out["s"].append(-1 if speed is None else _round(speed * 10))
                pt, px, py = ts, xi, yi
    return out


def decode_window(data: dict) -> dict[str, list[list]]:
    """Inverse of encode_window (the site's app.js does the same in the browser)."""
    feeds: dict[str, list[list]] = defaultdict(list)
    i = 0
    for (fi, vid, route), n in zip(data["vehicles"], data["n"]):
        pt, px, py = data["start"], 0, 0
        for _ in range(n):
            pt, px, py = pt + data["t"][i], px + data["x"][i], py + data["y"][i]
            b, s = data["b"][i], data["s"][i]
            feeds[data["feeds"][fi]].append(
                [vid, route, pt, px / SCALE, py / SCALE, None if b < 0 else b, None if s < 0 else s / 10]
            )
            i += 1
    return dict(feeds)


def export_day(history, day: date, out: Path, *, now: float | None = None) -> dict:
    """Write *day* to *out* (replacing a previous export); returns totals."""
    from .history import FINAL_AFTER_S

    now = time.time() if now is None else now
    wdir = out / "w"
    wdir.mkdir(parents=True, exist_ok=True)
    for old in wdir.glob("*.json.gz"):
        old.unlink()
    starts = day_windows(day, history.tz)
    windows, feeds_seen, total = {}, set(), 0
    for start in starts:
        if now < start + WINDOW_S + FINAL_AFTER_S:
            break  # still being recorded
        feeds = json.loads(gzip.decompress(history.window(start)))["feeds"]
        n = sum(len(r) for r in feeds.values())
        if not n:
            continue
        body = json.dumps(encode_window(start, feeds), separators=(",", ":")).encode()
        (wdir / f"{start}.json.gz").write_bytes(gzip.compress(body, 9))
        windows[str(start)] = n
        feeds_seen |= set(feeds)
        total += n
        log.info("%s: %d reports", datetime.fromtimestamp(start, tz=history.tz).strftime("%H:%M"), n)
    index = {
        "date": day.isoformat(),
        "timezone": history.tz.key,
        "start": starts[0],
        "end": starts[-1] + WINDOW_S,
        "window_seconds": WINDOW_S,
        "windows": windows,
        "feeds": sorted(feeds_seen),
        "generated": datetime.now(tz=history.tz).isoformat(timespec="seconds"),
        "source": "Transport for NSW Open Data, GTFS-Realtime vehicle positions (CC BY 4.0)",
    }
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return {"windows": len(windows), "reports": total}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export one recorded day as static files for the playback site.")
    parser.add_argument("--date", default="yesterday",
                        help="Sydney-local date: YYYY-MM-DD, 'today' or 'yesterday' (default)")
    parser.add_argument("--out", type=Path, required=True, help="output directory, e.g. ../tfnsw-playback-map/data")
    parser.add_argument("--data-dir", type=Path, help="recorder data directory with playback day files (default: DATA_DIR or ./data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from .config import ConfigError, S3Config, load_dotenv
    from .history import History
    from .uploader import make_s3_client

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    args = parse_args(argv)
    load_dotenv()
    try:
        day = resolve_date(args.date, SYDNEY_TZ)
        s3 = S3Config.from_env()
    except (ValueError, ConfigError) as exc:
        log.error("%s", exc)
        return 2
    data_dir = args.data_dir or Path(os.environ.get("DATA_DIR") or "data")
    client = make_s3_client(s3) if s3 else None
    history = History(client, s3.bucket if s3 else None, s3.prefix if s3 else "", data_dir)
    started = time.monotonic()
    summary = export_day(history, day, args.out)
    size = sum(p.stat().st_size for p in (args.out / "w").glob("*.json.gz"))
    log.info("Exported %s: %d windows, %d reports, %.1f MB to %s in %.0f s",
             day, summary["windows"], summary["reports"], size / 1e6, args.out, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
