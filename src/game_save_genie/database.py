"""SQLite database for tracking save versions and sync state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Platform, SaveVersion


SCHEMA = """
CREATE TABLE IF NOT EXISTS save_versions (
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

CREATE TABLE IF NOT EXISTS sync_state (
    game_id TEXT PRIMARY KEY,
    last_synced_at TEXT,
    last_version_id TEXT,
    remote_etag TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_game_id ON save_versions(game_id);
CREATE INDEX IF NOT EXISTS idx_versions_created_at ON save_versions(created_at);
"""


class Database:
    """Simple SQLite database for save version tracking."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._ensure_schema()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def add_version(self, version: SaveVersion) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO save_versions
                (id, game_id, created_at, local_path, size_bytes, file_count, label,
                 source_machine, platform, cloud_synced, cloud_remote_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.id,
                    version.game_id,
                    version.created_at.isoformat(),
                    str(version.local_path),
                    version.size_bytes,
                    version.file_count,
                    version.label,
                    version.source_machine,
                    version.platform.value,
                    int(version.cloud_synced),
                    version.cloud_remote_path,
                ),
            )
            conn.commit()

    def get_versions(self, game_id: str) -> list[SaveVersion]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM save_versions WHERE game_id = ? ORDER BY created_at DESC",
                (game_id,),
            ).fetchall()
        return [self._row_to_version(row) for row in rows]

    def get_version(self, version_id: str) -> SaveVersion | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM save_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return self._row_to_version(row) if row else None

    def delete_version(self, version_id: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM save_versions WHERE id = ?", (version_id,))
            conn.commit()

    def mark_cloud_synced(self, version_id: str, remote_path: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE save_versions SET cloud_synced = 1, cloud_remote_path = ? WHERE id = ?",
                (remote_path, version_id),
            )
            conn.commit()

    def count_versions(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM save_versions").fetchone()
        return int(row[0]) if row else 0

    def update_sync_state(self, game_id: str, version_id: str | None) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (game_id, last_synced_at, last_version_id)
                VALUES (?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    last_synced_at = excluded.last_synced_at,
                    last_version_id = excluded.last_version_id
                """,
                (game_id, datetime.now(timezone.utc).isoformat(), version_id),
            )
            conn.commit()

    def _row_to_version(self, row: sqlite3.Row) -> SaveVersion:
        return SaveVersion(
            id=row["id"],
            game_id=row["game_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            local_path=Path(row["local_path"]),
            size_bytes=row["size_bytes"],
            file_count=row["file_count"],
            label=row["label"],
            source_machine=row["source_machine"],
            platform=Platform(row["platform"]),
            cloud_synced=bool(row["cloud_synced"]),
            cloud_remote_path=row["cloud_remote_path"],
        )
