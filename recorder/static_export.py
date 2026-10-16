"""Export one recorded day as static files for the GitHub Pages playback site.

    uv run export_static.py --date 2026-10-16 --out ../tfnsw-playback-map/data
    uv run export_static.py --city prague --date 2026-10-16 --out ../tfnsw-playback-map/data

``--out`` is the site's data root. For each city it writes ``<out>/<city>/index.json``
(format 2: city metadata, day bounds, windows with data, report counts) and
``<out>/<city>/w/<window start epoch>.json.gz``, one compact gzip file per 10-minute
window, and merges the city into ``<out>/cities.json``
(``{"format": 2, "cities": [{id, name, timezone, center, zoom, attribution, date}]}``).
Data comes from the local playback day file if one was built, else S3.

Compact window format (2): vehicles are listed once; their reports are stored as
per-vehicle runs in flat columns, delta-encoded so they gzip well::

    {"start": epoch, "feeds": ["buses", ...],
     "vehicles": [[feed_index, vehicle_id, route_id, label, mode], ...],
     "n": [reports per vehicle, ...],
     "t": [...],  # seconds: first report relative to start, then since previous report
     "x": [...], "y": [...],  # lon/lat * 1e5: first absolute, then delta from previous
     "b": [...],  # bearing in whole degrees, -1 = none
     "s": [...]}  # speed in 0.1 m/s, -1 = none

Format 1 (the old flat layout) had 3-element vehicles without label and mode;
``decode_window`` reads both.
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
from .windows import WINDOW

log = logging.getLogger(__name__)

WINDOW_S = int(WINDOW.total_seconds())
SCALE = 1e5  # coordinates to 5 decimals (~1 m)


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


def encode_window(start: int, feeds: dict[str, list[list]], modes: dict[str, str] | None = None) -> dict:
    """Compact form of ``{feed: [[vehicle_id, route_id, ts, lon, lat, bearing, speed, label, mode], ...]}``.

    A report without label/mode gets ``""`` and the feed's mode from *modes*
    (``""`` if unknown); a vehicle's label and mode come from its first report.
    """
    modes = modes or {}
    out = {"start": start, "feeds": list(feeds), "vehicles": [], "n": [], "t": [], "x": [], "y": [], "b": [], "s": []}
    for fi, (feed_id, reports) in enumerate(feeds.items()):
        by_vehicle = defaultdict(list)
        for r in reports:
            by_vehicle[(r[0], r[1])].append(r)
        for (vid, route), runs in sorted(by_vehicle.items()):
            runs.sort(key=lambda r: r[2])
            first = runs[0]
            label = first[7] if len(first) > 7 else ""
            mode = (first[8] if len(first) > 8 else "") or modes.get(feed_id, "")
            out["vehicles"].append([fi, vid, route, label, mode])
            out["n"].append(len(runs))
            pt, px, py = start, 0, 0
            for ts, lon, lat, bearing, speed in (r[2:7] for r in runs):
                xi, yi = _round(lon * SCALE), _round(lat * SCALE)
                out["t"].append(ts - pt)
                out["x"].append(xi - px)
                out["y"].append(yi - py)
                out["b"].append(-1 if bearing is None else _round(bearing) % 360)
                out["s"].append(-1 if speed is None else _round(speed * 10))
                pt, px, py = ts, xi, yi
    return out


def decode_window(data: dict) -> dict[str, list[list]]:
    """Inverse of encode_window (the site's app.js does the same in the browser).

    Format 2 reports end with ``label, mode``; format 1 windows have neither.
    """
    feeds: dict[str, list[list]] = defaultdict(list)
    i = 0
    for (fi, vid, route, *extra), n in zip(data["vehicles"], data["n"]):
        pt, px, py = data["start"], 0, 0
        for _ in range(n):
            pt, px, py = pt + data["t"][i], px + data["x"][i], py + data["y"][i]
            b, s = data["b"][i], data["s"][i]
            feeds[data["feeds"][fi]].append(
                [vid, route, pt, px / SCALE, py / SCALE, None if b < 0 else b, None if s < 0 else s / 10, *extra]
            )
            i += 1
    return dict(feeds)


def export_day(history, day: date, out: Path, *, now: float | None = None) -> dict:
    """Write *day* to *out*, the city's folder (replacing its previous export); returns totals."""
    from .history import FINAL_AFTER_S

    now = time.time() if now is None else now
    wdir = out / "w"
    wdir.mkdir(parents=True, exist_ok=True)
    for old in wdir.glob("*.json.gz"):
        old.unlink()
    city = history._city
    modes = {feed.id: feed.mode for feed in city.feeds}
    starts = day_windows(day, history.tz)
    windows, feeds_seen, total = {}, set(), 0
    for start in starts:
        if now < start + WINDOW_S + FINAL_AFTER_S:
            break  # still being recorded
        feeds = json.loads(gzip.decompress(history.window(start)))["feeds"]
        n = sum(len(r) for r in feeds.values())
        if not n:
            continue
        body = json.dumps(encode_window(start, feeds, modes), separators=(",", ":")).encode()
        (wdir / f"{start}.json.gz").write_bytes(gzip.compress(body, 9))
        windows[str(start)] = n
        feeds_seen |= set(feeds)
        total += n
        log.info("%s: %d reports", datetime.fromtimestamp(start, tz=history.tz).strftime("%H:%M"), n)
    index = {
        "format": 2,
        "city": city.id,
        "name": city.name,
        "center": list(city.center),
        "zoom": city.zoom,
        "attribution": city.attribution,
        "date": day.isoformat(),
        "timezone": history.tz.key,
        "start": starts[0],
        "end": starts[-1] + WINDOW_S,
        "window_seconds": WINDOW_S,
        "windows": windows,
        "feeds": sorted(feeds_seen),
        "generated": datetime.now(tz=history.tz).isoformat(timespec="seconds"),
        "source": city.attribution,
    }
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return {"windows": len(windows), "reports": total}


def update_cities_index(root: Path, city, day: date, order: list[str]) -> None:
    """Merge *city* (exported for *day*) into ``<root>/cities.json``, keeping the other cities.

    Entries follow *order* (the ids in ``cities.toml``); unknown ids go last.
    """
    path = root / "cities.json"
    try:
        entries = {c["id"]: c for c in json.loads(path.read_text())["cities"]}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        entries = {}
    entries[city.id] = {
        "id": city.id,
        "name": city.name,
        "timezone": city.timezone.key,
        "center": list(city.center),
        "zoom": city.zoom,
        "attribution": city.attribution,
        "date": day.isoformat(),
    }
    rank = {cid: i for i, cid in enumerate(order)}
    ordered = sorted(entries.values(), key=lambda c: rank.get(c["id"], len(rank)))  # stable: unknown keep order
    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"format": 2, "cities": ordered}, indent=1))
    os.replace(tmp, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export one recorded day as static files for the playback site.")
    parser.add_argument("--city", default="sydney", help="city id from cities.toml (default: sydney)")
    parser.add_argument("--date", default="yesterday",
                        help="city-local date: YYYY-MM-DD, 'today' or 'yesterday' (default)")
    parser.add_argument("--out", type=Path, required=True, help="site data root, e.g. ../tfnsw-playback-map/data (writes <out>/<city>/ and <out>/cities.json)")
    parser.add_argument("--data-dir", type=Path, help="recorder data directory with the city folders (default: DATA_DIR or ./data)")
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
        cities = load_cities()
        city = cities[args.city]
    except KeyError:
        log.error("Unknown city %r", args.city)
        return 2
    try:
        day = resolve_date(args.date, city.timezone)
        s3 = S3Config.from_env()
    except (ValueError, ConfigError) as exc:
        log.error("%s", exc)
        return 2
    data_dir = city_dir(args.data_dir or Path(os.environ.get("DATA_DIR") or "data"), city.id)
    client = make_s3_client(s3) if s3 else None
    history = History(client, s3.bucket if s3 else None, city.s3_prefix, data_dir, city=city)
    started = time.monotonic()
    out = args.out / city.id
    summary = export_day(history, day, out)
    update_cities_index(args.out, city, day, list(cities))
    size = sum(p.stat().st_size for p in (out / "w").glob("*.json.gz"))
    log.info("Exported %s %s: %d windows, %d reports, %.1f MB to %s in %.0f s",
             city.id, day, summary["windows"], summary["reports"], size / 1e6, out, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
