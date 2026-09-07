"""Timestamps are shown in the reader's time zone, not UTC.

Backups are stored with timezone-aware UTC timestamps, which is right: version
ids have to sort identically on every machine. But rendering that raw meant
every time in the product was wrong by the reader's offset - five and a half
hours, on the machine where this was noticed - with nothing to signal it.

Reported by @Tudzer in #58, who fixed the dashboard. These cover all four
render sites, because half-converting is worse than not converting: `gsg ui`
and `gsg status` would then disagree about the same backup.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_save_genie.cli import app
from game_save_genie.database import Database
from game_save_genie.models import Platform, SaveVersion

runner = CliRunner()

# Deliberately an hour that changes date in some zones, so a conversion that
# silently does nothing cannot coincidentally match.
_STORED_UTC = datetime(2026, 8, 31, 22, 45, 0, tzinfo=timezone.utc)


def _expected_local() -> str:
    return _STORED_UTC.astimezone().strftime("%Y-%m-%d %H:%M")


def _seed(tmp_path: Path) -> tuple[str, str]:
    """A tracked game with one backup at a known UTC instant."""
    config_path = tmp_path / "config.yaml"
    runner.invoke(app, ["--config", str(config_path), "add", "Zone Test", "--exe", "z.exe"])
    db = Database(tmp_path / "data" / "versions.db")
    db.add_version(SaveVersion(
        id="20260831-224500-000000",
        game_id="zone-test",
        created_at=_STORED_UTC,
        local_path=tmp_path / "v.zip",
        size_bytes=1,
        file_count=1,
        platform=Platform.WINDOWS,
    ))
    return str(config_path), "zone-test"


def test_utc_and_local_differ_here_or_the_test_proves_nothing() -> None:
    """On a UTC machine this suite cannot detect the bug. Say so rather than
    reporting a false pass."""
    if _STORED_UTC.astimezone().utcoffset() == timedelta(0):
        pytest.skip("machine is on UTC; conversion is unobservable")


def test_gsg_versions_shows_local_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if _STORED_UTC.astimezone().utcoffset() == timedelta(0):
        pytest.skip("machine is on UTC")
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    config_path, game_id = _seed(tmp_path)
    result = runner.invoke(app, ["--config", config_path, "versions", game_id])
    assert _expected_local() in _flatten(result.output)
    assert "2026-08-31 22:45" not in _flatten(result.output)


def test_gsg_status_shows_local_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if _STORED_UTC.astimezone().utcoffset() == timedelta(0):
        pytest.skip("machine is on UTC")
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    config_path, _ = _seed(tmp_path)
    result = runner.invoke(app, ["--config", config_path, "status"])
    assert _expected_local() in _flatten(result.output)


def _flatten(output: str) -> str:
    """Rich wraps table cells across lines; join them for substring checks."""
    return re.sub(r"\s+", " ", output.replace("\n", " "))


def test_every_render_site_converts() -> None:
    """The dashboard's history timestamp site is pinned structurally alongside
    the two CLI sites. If a fourth render site appears,
    this fails and asks for it to be covered properly."""
    sources = [
        Path("src/game_save_genie/cli.py").read_text(encoding="utf-8"),
        Path("src/game_save_genie/ui.py").read_text(encoding="utf-8"),
    ]
    joined = "\n".join(sources)
    raw = re.findall(r"created_at\.strftime", joined)
    converted = re.findall(r"created_at\.astimezone\(\)\.strftime", joined)
    assert raw == [], f"{len(raw)} timestamp(s) still rendered as UTC"
    assert len(converted) == 3, f"expected 3 render sites, found {len(converted)}"
