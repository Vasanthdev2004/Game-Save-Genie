"""Tests for Ludusavi backup result parsing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from game_save_genie.ludusavi import backup_game
from game_save_genie.models import Game, Platform


def _sample_ludusavi_output(files: dict[str, dict[str, object]]) -> str:
    """Build a JSON string matching Ludusavi's --api backup response shape."""
    changed_new = sum(1 for f in files.values() if f.get("change") == "New")
    changed_diff = sum(1 for f in files.values() if f.get("change") == "Different")
    changed_same = sum(1 for f in files.values() if f.get("change") == "Same")
    total_bytes = sum(int(str(f.get("bytes", 0))) for f in files.values())
    return json.dumps({
        "overall": {
            "totalGames": 1,
            "totalBytes": total_bytes,
            "processedGames": 1,
            "processedBytes": total_bytes,
            "changedGames": {
                "new": 1 if changed_new else 0,
                "different": 1 if changed_diff else 0,
                "same": 1 if changed_same and not changed_new and not changed_diff else 0,
            },
        },
        "games": {
            "Test Game": {
                "decision": "Processed",
                "change": "New" if changed_new else ("Different" if changed_diff else "Same"),
                "files": files,
                "registry": {},
            }
        },
    })


def _make_game() -> Game:
    return Game(
        id="test-game",
        title="Test Game",
        platform=Platform.WINDOWS,
        save_paths=[],
    )


def test_backup_detects_new_files(tmp_path: Path) -> None:
    """First backup with all-new files should create a version."""
    files = {
        "C:/saves/sav.dat": {"change": "New", "bytes": 1500000},
        "C:/saves/meta.json": {"change": "New", "bytes": 2664},
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=_sample_ludusavi_output(files), stderr=""
    )
    with patch("game_save_genie.ludusavi.run_ludusavi", return_value=completed):
        result = backup_game(Path("fake"), _make_game(), tmp_path, label="test")

    assert result.success is True
    assert result.files_changed == 2
    assert result.version is not None
    assert result.version.file_count == 2
    assert result.version.size_bytes == 1502664


def test_backup_no_changes(tmp_path: Path) -> None:
    """All-same files should return 'no changes' without a version."""
    files = {
        "C:/saves/sav.dat": {"change": "Same", "bytes": 1500000},
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=_sample_ludusavi_output(files), stderr=""
    )
    with patch("game_save_genie.ludusavi.run_ludusavi", return_value=completed):
        result = backup_game(Path("fake"), _make_game(), tmp_path, label="test")

    assert result.success is True
    assert result.files_changed == 0
    assert result.version is None
    assert "No changes" in result.message


def test_backup_mixed_changes(tmp_path: Path) -> None:
    """Mix of New, Different, and Same should count only changed files."""
    files = {
        "C:/saves/new.sav": {"change": "New", "bytes": 500},
        "C:/saves/changed.sav": {"change": "Different", "bytes": 800},
        "C:/saves/unchanged.sav": {"change": "Same", "bytes": 300},
    }
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=_sample_ludusavi_output(files), stderr=""
    )
    with patch("game_save_genie.ludusavi.run_ludusavi", return_value=completed):
        result = backup_game(Path("fake"), _make_game(), tmp_path, label="test")

    assert result.success is True
    assert result.files_changed == 2  # New + Different, not Same
    assert result.version is not None
    assert result.version.file_count == 3  # total files
    assert result.version.size_bytes == 1600  # 500 + 800 + 300
