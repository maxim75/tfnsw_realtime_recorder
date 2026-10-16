"""Per-feed gzipped CSV files, one per 10-minute window."""
from __future__ import annotations

import csv
import gzip
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .feeds import feed_dir_name
from .windows import window_label, window_start

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


class RotatingWriter:
    """Writes one feed's rows to ``open/<feed_dir>/<feed_dir>_<label>[_pN].csv.gz``.

    When the window changes (or on ``close()``) the file is moved to
    ``complete/<feed_dir>/`` and passed to *on_complete*. Every append opens the
    file in gzip append mode, adding a gzip member; the concatenation is one
    valid gzip stream.
    """

    def __init__(self, feed: str, data_dir: Path, on_complete: Callable[[Path], None]) -> None:
        self.feed = feed
        self.dir_name = feed_dir_name(feed)
        self.open_dir = data_dir / "open" / self.dir_name
        self.complete_dir = data_dir / "complete" / self.dir_name
        self.open_dir.mkdir(parents=True, exist_ok=True)
        self.complete_dir.mkdir(parents=True, exist_ok=True)
        self._on_complete = on_complete
        self._window: datetime | None = None
        self.current_path: Path | None = None
        self.rows_in_window = 0

    def write(self, fetched_at: datetime, rows: list[dict[str, str]]) -> None:
        start = window_start(fetched_at)
        if self._window is not None and start != self._window:
            self.close()
        if not rows:
            return
        new_file = self.current_path is None
        if new_file:
            self._window = start
            self.current_path = self._free_path(start)
        with gzip.open(self.current_path, "at", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        self.rows_in_window += len(rows)

    def rotate_if_ended(self, now: datetime) -> None:
        if self._window is not None and window_start(now) != self._window:
            self.close()

    def close(self) -> None:
        path = self.current_path
        self._window = None
        self.current_path = None
        self.rows_in_window = 0
        if path is None:
            return
        dest = self.complete_dir / path.name
        os.replace(path, dest)
        self._on_complete(dest)

    def _free_path(self, start: datetime) -> Path:
        base = f"{self.dir_name}_{window_label(start)}"
        part = 1
        while True:
            name = f"{base}.csv.gz" if part == 1 else f"{base}_p{part}.csv.gz"
            if not (self.open_dir / name).exists() and not (self.complete_dir / name).exists():
                return self.open_dir / name
            part += 1


def recover_open_files(data_dir: Path) -> list[Path]:
    """Move files left in ``open/`` by a crash to ``complete/`` (as partial windows)."""
    recovered = []
    for path in sorted((data_dir / "open").glob("*/*.csv.gz")):
        dest_dir = data_dir / "complete" / path.parent.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        os.replace(path, dest)
        recovered.append(dest)
    return recovered
