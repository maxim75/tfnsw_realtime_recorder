import logging
from pathlib import Path

import pytest

from recorder.storage import city_dir, city_of, migrate_legacy_layout


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_city_dir(tmp_path):
    assert city_dir(tmp_path, "prague") == tmp_path / "prague"


def test_city_of(tmp_path):
    assert city_of(tmp_path / "prague" / "complete" / "feed=all" / "f.csv.gz", tmp_path) == "prague"
    with pytest.raises(ValueError):
        city_of(tmp_path / "settings.json", tmp_path)
    with pytest.raises(ValueError):
        city_of(Path("/elsewhere/sydney/complete/f"), tmp_path)


def test_migrate_moves_legacy_folders(tmp_path):
    touch(tmp_path / "open" / "feed=buses" / "a.csv")
    touch(tmp_path / "complete" / "feed=buses" / "b.csv.gz")
    touch(tmp_path / "playback" / "2026-10-16.json")
    touch(tmp_path / "settings.json", "{}")
    assert migrate_legacy_layout(tmp_path) == ["open", "complete", "playback"]
    assert (tmp_path / "sydney" / "open" / "feed=buses" / "a.csv").read_text() == "x"
    assert (tmp_path / "sydney" / "complete" / "feed=buses" / "b.csv.gz").exists()
    assert (tmp_path / "sydney" / "playback" / "2026-10-16.json").exists()
    assert not any((tmp_path / n).exists() for n in ("open", "complete", "playback"))
    assert (tmp_path / "settings.json").read_text() == "{}"


def test_migrate_is_idempotent(tmp_path):
    touch(tmp_path / "complete" / "f.csv.gz")
    migrate_legacy_layout(tmp_path)
    assert migrate_legacy_layout(tmp_path) == []
    assert (tmp_path / "sydney" / "complete" / "f.csv.gz").exists()


def test_migrate_other_first_city(tmp_path):
    touch(tmp_path / "open" / "a.csv")
    migrate_legacy_layout(tmp_path, first_city="prague")
    assert (tmp_path / "prague" / "open" / "a.csv").exists()


def test_migrate_merges_into_existing_city_folder(tmp_path, caplog):
    touch(tmp_path / "complete" / "feed=buses" / "old.csv.gz", "old")
    touch(tmp_path / "complete" / "feed=buses" / "clash.csv.gz", "legacy")
    touch(tmp_path / "sydney" / "complete" / "feed=buses" / "clash.csv.gz", "kept")
    touch(tmp_path / "sydney" / "complete" / "feed=buses" / "new.csv.gz", "new")
    with caplog.at_level(logging.WARNING):
        assert migrate_legacy_layout(tmp_path) == ["complete"]
    base = tmp_path / "sydney" / "complete" / "feed=buses"
    assert (base / "old.csv.gz").read_text() == "old"
    assert (base / "new.csv.gz").read_text() == "new"
    assert (base / "clash.csv.gz").read_text() == "kept"
    # both are kept: the legacy clash stays where it was
    assert (tmp_path / "complete" / "feed=buses" / "clash.csv.gz").read_text() == "legacy"
    assert not (tmp_path / "complete" / "feed=buses" / "old.csv.gz").exists()
    assert "clash.csv.gz" in caplog.text
