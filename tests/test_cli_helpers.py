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
