"""Pydantic models for Game Save Genie."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Platform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


class CloudProvider(str, Enum):
    GOOGLE_DRIVE = "google_drive"
    ONEDRIVE = "onedrive"
    DROPBOX = "dropbox"
    BOX = "box"
    S3 = "s3"
    LOCAL = "local"
    WEBDAV = "webdav"


class GameSavePath(BaseModel):
    """A single save path entry for a game."""

    path: Path
    is_wine_prefix: bool = False
    wine_prefix_path: Path | None = None
    description: str | None = None


class Game(BaseModel):
    """A tracked game with its save locations."""

    id: str
    title: str
    platform: Platform
    executable_names: list[str] = Field(default_factory=list)
    save_paths: list[GameSavePath] = Field(default_factory=list)
    shop: str | None = None
    shop_object_id: str | None = None
    auto_sync: bool = True
    sync_enabled: bool = True
    cloud_provider: CloudProvider | None = None
    remote_path: str | None = None
    custom: bool = False

    @field_validator("id")
    @classmethod
    def id_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Game ID cannot be empty")
        return value


class SaveVersion(BaseModel):
    """A backed-up save version.

    ``local_path`` points at this version's snapshot zip. Versions created
    before snapshot support may still point at the shared per-game backup
    directory. ``origin`` distinguishes user/auto backups from safety backups
    taken automatically before a restore ("safety"), which are excluded from
    the cloud-newer comparison so a failed restore can be retried.
    """

    id: str
    game_id: str
    created_at: datetime
    local_path: Path
    size_bytes: int
    file_count: int
    label: str | None = None
    source_machine: str | None = None
    platform: Platform
    cloud_synced: bool = False
    cloud_remote_path: str | None = None
    sha256: str | None = None
    origin: str = "user"  # "user", "auto", or "safety"
    content_digest: str | None = None  # stable source-tree hash for custom-game change detection


class SyncConfig(BaseModel):
    """Global sync configuration."""

    backup_dir: Path
    max_versions: int = 10
    auto_sync_on_game_close: bool = True
    dry_run_default: bool = False
    custom_rclone_args: list[str] = Field(default_factory=list)
    cloud_provider: CloudProvider | None = None
    rclone_remote_name: str | None = None
    remote_root: str = "game-save-genie"
    ludusavi_path: Path | None = None
    rclone_path: Path | None = None
    storage_limit_gb: float = 5.0  # warn when remote usage nears this (0 = no warning)


class BackupResult(BaseModel):
    """Result of a backup operation."""

    success: bool
    game_id: str
    version: SaveVersion | None = None
    message: str
    files_changed: int = 0


class RestoreResult(BaseModel):
    """Result of a restore operation."""

    success: bool
    game_id: str
    version_id: str
    message: str
    files_restored: int = 0


class CloudSyncResult(BaseModel):
    """Result of a cloud sync operation."""

    success: bool
    direction: str  # "upload", "download", "bidirectional"
    files_transferred: int = 0
    message: str
    remote_path: str


class ProcessInfo(BaseModel):
    """Information about a running game process."""

    pid: int
    name: str
    exe: str | None
    status: str
    create_time: datetime | None
    environ: dict[str, str] | None = None
