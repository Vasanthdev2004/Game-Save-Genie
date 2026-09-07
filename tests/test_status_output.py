"""Readable, literal status output for ordinary terminal widths."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from game_save_genie import cli
from game_save_genie.config import save_config, save_games
from game_save_genie.database import Database
from game_save_genie.models import CloudProvider, Game, Platform, SaveVersion, SyncConfig

runner = CliRunner()


def test_status_is_scannable_at_eighty_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    backup_at = datetime(2026, 9, 7, 6, 5, tzinfo=timezone.utc)
    uploaded_at = datetime(2026, 9, 7, 6, 8, tzinfo=timezone.utc)
    game = Game(
        id="long-game",
        title="A Long Game Title That Still Has Readable Status",
        platform=Platform.WINDOWS,
    )
    config = SyncConfig(
        backup_dir=tmp_path / "backups",
        cloud_provider=CloudProvider.GOOGLE_DRIVE,
        rclone_remote_name="gdrive",
    )
    snapshot = tmp_path / "snapshot.zip"
    snapshot.write_bytes(b"snapshot")

    monkeypatch.setattr(cli, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(cli, "console", Console(width=80, color_system=None))
    monkeypatch.setattr(cli, "get_rclone_path", lambda _path: Path("rclone"))
    monkeypatch.setattr(cli, "get_remote_size", lambda *_args: (1, 8))
    save_config(config, config_path)
    save_games([game], config_path)
    Database(data_dir / "versions.db").add_version(
        SaveVersion(
            id="20260907-060500-000000",
            game_id=game.id,
            created_at=backup_at,
            local_path=snapshot,
            size_bytes=8,
            file_count=1,
            platform=Platform.WINDOWS,
            cloud_synced=True,
            cloud_remote_path=(
                "gdrive:game-save-genie/long-game/manifests/"
                "20260907-060500-000000.json"
            ),
            cloud_synced_at=uploaded_at,
        )
    )

    result = runner.invoke(cli.app, ["--config", str(config_path), "status"])

    assert result.exit_code == 0, result.output
    assert all(len(line) <= 80 for line in result.output.splitlines())
    assert "Game" in result.output
    assert "Health" in result.output
    assert "Backups" in result.output
    assert "Last backup" in result.output
    assert "Cloud Synced" not in result.output
    assert "Cloud Target" not in result.output
    assert any(
        backup_at.astimezone().strftime("%Y-%m-%d %H:%M") in line
        for line in result.output.splitlines()
    )
    assert "Cloud target: gdrive:game-save-genie" in result.output
    assert (
        f"Last upload: {uploaded_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
        in result.output
    )


def test_status_treats_game_titles_and_errors_as_literal_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    game = Game(
        id="literal-game",
        title="[red]Literal title[/red]",
        platform=Platform.WINDOWS,
    )

    monkeypatch.setattr(cli, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(cli, "console", Console(width=80, color_system=None))
    save_config(SyncConfig(backup_dir=tmp_path / "backups"), config_path)
    save_games([game], config_path)
    Database(data_dir / "versions.db").set_backup_issue(
        game.id, "Disk returned [bold]broken markup[/bold]"
    )

    result = runner.invoke(cli.app, ["--config", str(config_path), "status"])

    assert result.exit_code == 0, result.output
    assert "[red]Literal title[/red]" in result.output
    assert "[bold]broken markup[/bold]" in " ".join(result.output.split())
    assert "literal-game" in result.output
    assert "never been backed up" in result.output
