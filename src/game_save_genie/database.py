"""SQLite database for tracking save versions and sync state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Platform, SaveVersion, UploadJob

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

CREATE TABLE IF NOT EXISTS upload_jobs (
    version_id TEXT PRIMARY KEY,
    remote_name TEXT NOT NULL,
    remote_root TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backup_issues (
    game_id TEXT PRIMARY KEY,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_requests (
    game_id TEXT PRIMARY KEY,
    label TEXT
);
"""

# Columns added after the initial release; applied via ALTER TABLE so
# existing databases migrate in place.
_MIGRATION_COLUMNS = {
    "sha256": "sha256 TEXT",
    "origin": "origin TEXT NOT NULL DEFAULT 'user'",
    "content_digest": "content_digest TEXT",
    "cloud_synced_at": "cloud_synced_at TEXT",
}


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
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(save_versions)").fetchall()
            }
            for column, definition in _MIGRATION_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE save_versions ADD COLUMN {definition}")
            upload_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(upload_jobs)").fetchall()
            }
            if "blocked" not in upload_columns:
                conn.execute("ALTER TABLE upload_jobs ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    def add_version(
        self, version: SaveVersion, upload_target: tuple[str, str] | None = None
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO save_versions
                (id, game_id, created_at, local_path, size_bytes, file_count, label,
                 source_machine, platform, cloud_synced, cloud_remote_path, sha256, origin,
                 content_digest, cloud_synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    version.sha256,
                    version.origin,
                    version.content_digest,
                    version.cloud_synced_at.isoformat() if version.cloud_synced_at else None,
                ),
            )
            if upload_target is not None and version.origin != "safety":
                conn.execute(
                    "INSERT INTO upload_jobs (version_id, remote_name, remote_root) VALUES (?, ?, ?)",
                    (version.id, *upload_target),
                )
            conn.commit()

    def get_versions(self, game_id: str) -> list[SaveVersion]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM save_versions WHERE game_id = ? ORDER BY created_at DESC",
                (game_id,),
            ).fetchall()
        return [self._row_to_version(row) for row in rows]

    def get_latest_version_id(self, game_id: str, exclude_safety: bool = False) -> str | None:
        """Return the newest version id for a game, or None.

        With ``exclude_safety``, versions with origin 'safety' (pre-restore
        snapshots) are ignored so they never outrank a cloud save in the
        auto-restore comparison.
        """
        query = "SELECT id FROM save_versions WHERE game_id = ?"
        if exclude_safety:
            query += " AND origin != 'safety'"
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._connection() as conn:
            row = conn.execute(query, (game_id,)).fetchone()
        return str(row["id"]) if row else None

    def get_version(self, version_id: str) -> SaveVersion | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM save_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return self._row_to_version(row) if row else None

    def delete_version(self, version_id: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM upload_jobs WHERE version_id = ?", (version_id,))
            conn.execute("DELETE FROM save_versions WHERE id = ?", (version_id,))
            conn.commit()

    def delete_game(self, game_id: str) -> int:
        """Forget every row for a game. Returns the number of versions removed.

        Retention only ever prunes games loaded from games.yaml, so rows left
        behind by an untracked game are unreachable forever — and re-adding the
        same title resurrects them, pointing at snapshots that no longer exist.
        """
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM upload_jobs WHERE version_id IN "
                "(SELECT id FROM save_versions WHERE game_id = ?)", (game_id,),
            )
            conn.execute("DELETE FROM backup_issues WHERE game_id = ?", (game_id,))
            conn.execute("DELETE FROM backup_requests WHERE game_id = ?", (game_id,))
            cursor = conn.execute("DELETE FROM save_versions WHERE game_id = ?", (game_id,))
            removed = cursor.rowcount
            conn.execute("DELETE FROM sync_state WHERE game_id = ?", (game_id,))
            conn.commit()
        return int(removed)

    def mark_cloud_synced(self, version_id: str, remote_path: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE save_versions SET cloud_synced = 1, cloud_remote_path = ?, "
                "cloud_synced_at = ? WHERE id = ?",
                (remote_path, datetime.now(timezone.utc).isoformat(), version_id),
            )
            conn.execute("DELETE FROM upload_jobs WHERE version_id = ?", (version_id,))
            conn.commit()

    def enqueue_upload(self, version_id: str, remote_name: str, remote_root: str) -> None:
        """Queue an existing snapshot without resetting a failed attempt's backoff."""
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO upload_jobs (version_id, remote_name, remote_root) "
                "SELECT id, ?, ? FROM save_versions WHERE id = ? AND origin != 'safety'",
                (remote_name, remote_root, version_id),
            )

    def get_upload_jobs(self, game_id: str | None = None) -> list[UploadJob]:
        query = "SELECT j.* FROM upload_jobs j JOIN save_versions v ON v.id = j.version_id"
        params: tuple[str, ...] = ()
        if game_id is not None:
            query += " WHERE v.game_id = ?"
            params = (game_id,)
        query += " ORDER BY j.next_attempt_at, v.created_at"
        with self._connection() as conn:
            return [UploadJob.model_validate(dict(row)) for row in conn.execute(query, params)]

    def fail_upload(self, version_id: str, error: str, now: float, *, blocked: bool = False) -> None:
        """Retry after one minute, doubling up to an hour; persist across restarts."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM upload_jobs WHERE version_id = ?", (version_id,)
            ).fetchone()
            if row is not None:
                delay = min(3600, 60 * 2 ** min(int(row["attempts"]), 6))
                conn.execute(
                    "UPDATE upload_jobs SET attempts = attempts + 1, "
                    "next_attempt_at = ?, last_error = ?, blocked = ? WHERE version_id = ?",
                    (now + delay, error, int(blocked), version_id),
                )

    def set_backup_issue(self, game_id: str, message: str | None) -> None:
        with self._connection() as conn:
            if message is None:
                conn.execute("DELETE FROM backup_issues WHERE game_id = ?", (game_id,))
            else:
                conn.execute(
                    "INSERT INTO backup_issues (game_id, message) VALUES (?, ?) "
                    "ON CONFLICT(game_id) DO UPDATE SET message = excluded.message",
                    (game_id, message),
                )

    def get_backup_issue(self, game_id: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT message FROM backup_issues WHERE game_id = ?", (game_id,)
            ).fetchone()
        return str(row["message"]) if row else None

    def defer_backup(self, game_id: str, label: str | None, message: str) -> None:
        """Retain an automatic close/periodic backup when another writer is busy."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO backup_requests (game_id, label) VALUES (?, ?) "
                "ON CONFLICT(game_id) DO UPDATE SET label = excluded.label",
                (game_id, label),
            )
            conn.execute(
                "INSERT INTO backup_issues (game_id, message) VALUES (?, ?) "
                "ON CONFLICT(game_id) DO UPDATE SET message = excluded.message",
                (game_id, message),
            )

    def get_deferred_backups(self, game_id: str | None = None) -> list[tuple[str, str | None]]:
        query = "SELECT game_id, label FROM backup_requests"
        params: tuple[str, ...] = ()
        if game_id is not None:
            query += " WHERE game_id = ?"
            params = (game_id,)
        with self._connection() as conn:
            return [(row["game_id"], row["label"]) for row in conn.execute(query, params)]

    def clear_deferred_backup(self, game_id: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM backup_requests WHERE game_id = ?", (game_id,))

    def count_versions(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM save_versions").fetchone()
        return int(row[0]) if row else 0

    def get_all_versions(self) -> list[SaveVersion]:
        """Return all versions across all games, newest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM save_versions ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_version(row) for row in rows]

    def get_sync_state(self, game_id: str) -> str | None:
        """Return the last cloud version id applied/synced for a game, or None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT last_version_id FROM sync_state WHERE game_id = ?", (game_id,)
            ).fetchone()
        return row["last_version_id"] if row and row["last_version_id"] else None

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
            sha256=row["sha256"],
            origin=row["origin"] or "user",
            content_digest=row["content_digest"],
            cloud_synced_at=(
                datetime.fromisoformat(row["cloud_synced_at"]) if row["cloud_synced_at"] else None
            ),
        )
