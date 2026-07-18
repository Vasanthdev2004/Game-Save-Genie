"""Tests for CLI helper functions and onboarding entry points."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from game_save_genie.cli import _slugify, app

runner = CliRunner()


def test_bare_gsg_shows_help_when_not_a_tty(tmp_path: Path) -> None:
    """Non-interactive bare invocation must print help, never hang on a wizard."""
    result = runner.invoke(app, ["--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 0
    assert "Commands" in result.output


def test_auto_unconfigured_non_tty_exits_with_hint(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    # Keep the error path from touching the real user profile / root logger.
    monkeypatch.setattr("game_save_genie.cli.setup_file_logging", lambda p: p)
    result = runner.invoke(app, ["--config", str(tmp_path / "config.yaml"), "auto"])
    assert result.exit_code == 1
    assert "guided setup" in result.output


def test_auto_no_wizard_never_prompts(tmp_path: Path, monkeypatch: object) -> None:
    """The autostart entry passes --no-wizard: unconfigured must exit, not prompt."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("game_save_genie.cli.setup_file_logging", lambda p: p)
    result = runner.invoke(
        app, ["--config", str(tmp_path / "config.yaml"), "auto", "--no-wizard"]
    )
    assert result.exit_code == 1
    assert "not configured" in result.output


def test_version_flag(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Game Save Genie" in result.output


def test_pull_requires_game_or_all(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "c.yaml"), "pull"])
    assert result.exit_code == 1
    assert "--all" in result.output


class _IdleWatcher:
    """GameWatcher stand-in: nothing is ever running (keeps tests hermetic)."""

    def __init__(self, games: object, **kwargs: object) -> None:
        pass

    def prime(self) -> None:
        pass

    def is_running(self, game_id: str) -> bool:
        return False

    def running_process_info(self, game_id: str) -> None:
        return None


def test_pull_dry_run_reports_newer_cloud_version(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "add", "Pull Wiring Test", "--cloud", "s3", "--remote", "r"],
    )
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.cli.GameWatcher", _IdleWatcher)
    monkeypatch.setattr(
        "game_save_genie.cli.list_remote_versions",
        lambda *a, **k: ["20990101-000000-000000"],
    )
    monkeypatch.setattr("game_save_genie.cli.get_rclone_path", lambda p: Path("rclone"))
    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", lambda p: Path("ludusavi"))

    result = runner.invoke(
        app, ["--config", cfg, "pull", "pull-wiring-test", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "Would restore" in result.output
    assert "20990101-000000-000000" in result.output


def test_pull_listing_failure_exits_nonzero(tmp_path: Path, monkeypatch: object) -> None:
    """An unreachable cloud must be an error, not 'no cloud versions'."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "add", "Pull Err Test", "--cloud", "s3", "--remote", "r"],
    )

    def boom(*a: object, **k: object) -> list[str]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.cli.GameWatcher", _IdleWatcher)
    monkeypatch.setattr("game_save_genie.cli.list_remote_versions", boom)
    monkeypatch.setattr("game_save_genie.cli.get_rclone_path", lambda p: Path("rclone"))
    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", lambda p: Path("ludusavi"))

    result = runner.invoke(app, ["--config", cfg, "pull", "pull-err-test"])
    assert result.exit_code == 1
    assert "cloud listing failed" in result.output


def test_slugify_matches_legacy_scheme() -> None:
    """Ids must stay byte-identical to those in existing games.yaml files —
    a changed scheme would re-add every tracked game under a new id."""
    assert _slugify("Solo Leveling: Arise") == "solo-leveling--arise"
    assert _slugify("Elden Ring") == "elden-ring"
    assert _slugify("  Trimmed  ") == "trimmed"


def test_slugify_keeps_unicode_titles() -> None:
    assert _slugify("Ведьмак") == "ведьмак"
    assert _slugify("エルデンリング") == "エルデンリング"


def test_slugify_never_returns_empty() -> None:
    slug = _slugify("!!!")
    assert slug.startswith("game-")
    assert len(slug) > 5
    # Deterministic: same title always maps to the same id.
    assert _slugify("!!!") == slug
