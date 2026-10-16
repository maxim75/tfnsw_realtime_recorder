"""Per-city layout of the data directory: DATA_DIR/<city>/{open,complete,playback}."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

LEGACY_DIRS = ("open", "complete", "playback")


def city_dir(data_dir: Path, city_id: str) -> Path:
    return data_dir / city_id


def city_of(path: Path, data_dir: Path) -> str:
    """The city a file under ``DATA_DIR/<city>/...`` belongs to; ``ValueError`` if outside."""
    parts = path.relative_to(data_dir).parts
    if len(parts) < 2:
        raise ValueError(f"{path} is not inside a city folder of {data_dir}")
    return parts[0]


def migrate_legacy_layout(data_dir: Path, first_city: str = "sydney") -> list[str]:
    """Move the pre-multi-city ``open``/``complete``/``playback`` folders under *first_city*.

    Returns the names of the folders found. Safe to repeat: when the target folder already
    exists, only files missing there are moved; a clash keeps both files and is logged.
    """
    moved = []
    for name in LEGACY_DIRS:
        src = data_dir / name
        if not src.is_dir():
            continue
        dst = city_dir(data_dir, first_city) / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            src.rename(dst)
        else:
            _merge(src, dst)
        moved.append(name)
    return moved


def _merge(src: Path, dst: Path) -> None:
    for item in sorted(src.iterdir()):
        target = dst / item.name
        if item.is_dir() and not item.is_symlink():
            if target.exists() and not target.is_dir():
                log.warning("Keeping %s: %s exists and is not a folder", item, target)
                continue
            target.mkdir(exist_ok=True)
            _merge(item, target)
        elif target.exists():
            log.warning("Keeping legacy %s: %s already exists", item, target)
        else:
            shutil.move(str(item), str(target))
    try:
        src.rmdir()  # only succeeds once everything has moved
    except OSError:
        pass
