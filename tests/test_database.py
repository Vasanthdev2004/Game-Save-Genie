"""Tests for the version database."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from game_save_genie.database import Database
from game_save_genie.models import Platform, SaveVersion


def _make_version(
    version_id: str,
    tmp_path: Path,
    minute: int = 0,
    origin: str = "user",
    sha256: str | None = None,
) -> SaveVersion:
    return SaveVersion(
        id=version_id,
        game_id="game",
        created_at=datetime(2026, 1, 1, 12, minute, tzinfo=timezone.utc),
        local_path=tmp_path / "backups" / "game",
        size_bytes=100,
        file_count=1,
        platform=Platform.WINDOWS,
        origin=origin,
        sha256=sha256,
    )


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


def test_sha256_and_origin_round_trip(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.add_version(_make_version("v1", tmp_path, origin="safety", sha256="ab" * 32))
    loaded = db.get_version("v1")
    assert loaded is not None
    assert loaded.origin == "safety"
    assert loaded.sha256 == "ab" * 32


def test_migrates_legacy_schema(tmp_path: Path) -> None:
    """A pre-snapshot database (no sha256/origin columns) migrates in place."""
    db_path = tmp_path / "legacy.db"
    legacy_schema = """
    CREATE TABLE save_versions (
        id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        local_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        file_count INTEGER NOT NULL,
        label TEXT,
        source_machine TEXT,
        platform TEXT NOT NULL,
        cloud_synced INTEGER NOT NULL DEFAULT 0,
        cloud_remote_path TEXT
    );
    """
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO save_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "v-old", "game", "2026-01-01T12:00:00+00:00", "C:/backups/game",
                100, 1, None, None, "windows", 0, None,
            ),
        )
    conn.close()

    db = Database(db_path)
    loaded = db.get_version("v-old")
    assert loaded is not None
    assert loaded.origin == "user"
    assert loaded.sha256 is None

    db.add_version(_make_version("v-new", tmp_path, minute=1, origin="auto"))
    reloaded = db.get_version("v-new")
    assert reloaded is not None
    assert reloaded.origin == "auto"


def test_latest_version_id_excludes_safety(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.add_version(_make_version("v1", tmp_path, minute=0, origin="user"))
    db.add_version(_make_version("v2", tmp_path, minute=1, origin="safety"))
    assert db.get_latest_version_id("game") == "v2"
    assert db.get_latest_version_id("game", exclude_safety=True) == "v1"
    assert db.get_latest_version_id("missing") is None


def test_sync_state_round_trip(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    assert db.get_sync_state("game") is None
    db.update_sync_state("game", "20260101-120000-000000")
    assert db.get_sync_state("game") == "20260101-120000-000000"
    db.update_sync_state("game", "20260102-120000-000000")
    assert db.get_sync_state("game") == "20260102-120000-000000"


def test_delete_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.add_version(_make_version("v1", tmp_path))
    db.delete_version("v1")
    assert db.get_version("v1") is None
