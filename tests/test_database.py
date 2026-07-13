"""Tests for the version database."""

from datetime import datetime, timezone
from pathlib import Path

from game_save_genie.database import Database
from game_save_genie.models import Platform, SaveVersion


def test_add_and_get_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    version = SaveVersion(
        id="20240101-120000-000000",
        game_id="test-game",
        created_at=datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
        local_path=tmp_path / "backups" / "test-game",
        size_bytes=1024,
        file_count=3,
        label="Test backup",
        source_machine="test-machine",
        platform=Platform.WINDOWS,
    )
    db.add_version(version)
    loaded = db.get_version(version.id)
    assert loaded is not None
    assert loaded.game_id == "test-game"
    assert loaded.size_bytes == 1024


def test_get_versions_ordered(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    for i in range(3):
        db.add_version(
            SaveVersion(
                id=f"v{i}",
                game_id="game",
                created_at=datetime(2024, 1, 1, 12, i, tzinfo=timezone.utc),
                local_path=tmp_path / "backups" / "game",
                size_bytes=100,
                file_count=1,
                platform=Platform.WINDOWS,
            )
        )
    versions = db.get_versions("game")
    assert len(versions) == 3
    assert versions[0].id == "v2"


def test_mark_cloud_synced(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    version = SaveVersion(
        id="v1",
        game_id="game",
        created_at=datetime.now(timezone.utc),
        local_path=tmp_path / "backups",
        size_bytes=100,
        file_count=1,
        platform=Platform.WINDOWS,
    )
    db.add_version(version)
    db.mark_cloud_synced("v1", "remote/path")
    loaded = db.get_version("v1")
    assert loaded is not None
    assert loaded.cloud_synced is True
    assert loaded.cloud_remote_path == "remote/path"
