"""Tests for configuration management."""

from pathlib import Path

from game_save_genie.config import load_config, save_config, save_games, load_games, get_machine_id
from game_save_genie.models import Game, Platform, CloudProvider


def test_default_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config = load_config(config_path)
    assert config.max_versions == 10
    assert config.backup_dir.name == "backups"


def test_save_and_load_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config = load_config(config_path)
    config.backup_dir = tmp_path / "custom_backups"
    config.max_versions = 5
    config.cloud_provider = CloudProvider.GOOGLE_DRIVE
    save_config(config, config_path)
    loaded = load_config(config_path)
    assert loaded.max_versions == 5
    assert loaded.backup_dir == tmp_path / "custom_backups"
    assert loaded.cloud_provider == CloudProvider.GOOGLE_DRIVE


def test_save_and_load_games(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    games = [
        Game(id="elden-ring", title="Elden Ring", platform=Platform.WINDOWS),
        Game(id="hades", title="Hades", platform=Platform.LINUX, auto_sync=False),
    ]
    save_games(games, config_path)
    loaded = load_games(config_path)
    assert len(loaded) == 2
    assert loaded[0].id == "elden-ring"
    assert loaded[1].auto_sync is False


def test_machine_id() -> None:
    machine_id = get_machine_id()
    assert isinstance(machine_id, str)
    assert machine_id
