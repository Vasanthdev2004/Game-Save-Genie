"""Configuration management for Game Save Genie."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir, user_data_dir

from .models import CloudProvider, Game, SyncConfig


APP_NAME = "Game Save Genie"
APP_AUTHOR = "game-save-genie"
DEFAULT_CONFIG_FILE = "config.yaml"


def get_config_dir() -> Path:
    """Return the user configuration directory."""
    return Path(user_config_dir(APP_NAME, APP_AUTHOR))


def get_data_dir() -> Path:
    """Return the user data directory for backups and state."""
    return Path(user_data_dir(APP_NAME, APP_AUTHOR))


def get_config_path() -> Path:
    """Return the default configuration file path."""
    return get_config_dir() / DEFAULT_CONFIG_FILE


def get_default_backup_dir() -> Path:
    """Return the default local backup directory."""
    return get_data_dir() / "backups"


def get_default_binary_dir() -> Path:
    """Return the directory for downloaded binaries."""
    return get_data_dir() / "bin"


def load_config(config_path: Path | None = None) -> SyncConfig:
    """Load configuration from file, creating defaults if missing."""
    path = config_path or get_config_path()
    if not path.exists():
        return _default_config()

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return _build_config(data)


def save_config(config: SyncConfig, config_path: Path | None = None) -> None:
    """Save configuration to file."""
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(config.model_dump(mode="json"), f, sort_keys=False, allow_unicode=True)


def load_games(config_path: Path | None = None) -> list[Game]:
    """Load the tracked games list from file."""
    path = _games_path(config_path)
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []

    return [Game.model_validate(item) for item in data]


def save_games(games: list[Game], config_path: Path | None = None) -> None:
    """Save the tracked games list to file."""
    path = _games_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(
            [game.model_dump(mode="json") for game in games],
            f,
            sort_keys=False,
            allow_unicode=True,
        )


def _games_path(config_path: Path | None = None) -> Path:
    base = config_path.parent if config_path else get_config_dir()
    return base / "games.yaml"


def _default_config() -> SyncConfig:
    return SyncConfig(
        backup_dir=get_default_backup_dir(),
        max_versions=10,
        auto_sync_on_game_close=True,
        dry_run_default=False,
        custom_rclone_args=[],
        cloud_provider=None,
        remote_root="game-save-genie",
        ludusavi_path=None,
        rclone_path=None,
    )


def _build_config(data: dict[str, Any]) -> SyncConfig:
    """Build a SyncConfig from raw YAML data."""
    if "backup_dir" in data and data["backup_dir"]:
        data["backup_dir"] = Path(data["backup_dir"]).expanduser()
    if "ludusavi_path" in data and data["ludusavi_path"]:
        data["ludusavi_path"] = Path(data["ludusavi_path"]).expanduser()
    if "rclone_path" in data and data["rclone_path"]:
        data["rclone_path"] = Path(data["rclone_path"]).expanduser()
    if "cloud_provider" in data and data["cloud_provider"]:
        data["cloud_provider"] = CloudProvider(data["cloud_provider"])
    return SyncConfig.model_validate(data)


def get_machine_id() -> str:
    """Return a machine identifier for sync tracking."""
    return os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown"))
