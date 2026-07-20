"""Tests for Linux tier-1 support (systemd autostart, notify-send, Steam paths)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from game_save_genie.cli import _systemd_unit_content
from game_save_genie.launcher import _linux_steam_candidates
from game_save_genie.notify import _linux_notify


def test_systemd_unit_content_default_config() -> None:
    unit = _systemd_unit_content(Path("/usr/local/bin/gsg"), None)
    assert "ExecStart=/usr/local/bin/gsg auto --no-wizard\n" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    # --no-wizard is load-bearing: a headless service must never block on a
    # first-run prompt nobody can see.
    assert "--no-wizard" in unit


def test_systemd_unit_content_custom_config() -> None:
    unit = _systemd_unit_content(Path("/home/deck/.local/bin/gsg"), Path("/home/deck/gsg.yaml"))
    assert 'ExecStart=/home/deck/.local/bin/gsg --config "/home/deck/gsg.yaml" auto --no-wizard' in unit


def test_linux_notify_invokes_notify_send() -> None:
    with patch("game_save_genie.notify.subprocess.Popen") as popen:
        _linux_notify("Save backed up", "Elden Ring")
    args = popen.call_args[0][0]
    assert args[0] == "notify-send"
    assert "Save backed up" in args
    assert "Elden Ring" in args


def test_linux_notify_swallows_missing_binary() -> None:
    with patch(
        "game_save_genie.notify.subprocess.Popen",
        side_effect=FileNotFoundError("notify-send"),
    ):
        _linux_notify("t", "m")  # must not raise — headless boxes are normal


def test_linux_steam_candidates_cover_native_and_flatpak(tmp_path: Path) -> None:
    candidates = _linux_steam_candidates(tmp_path)
    joined = [str(c) for c in candidates]
    assert any(".local" in c and "share" in c for c in joined)  # native / Deck
    assert any(".steam" in c for c in joined)  # classic symlink
    assert any("com.valvesoftware.Steam" in c for c in joined)  # Flatpak
    assert all(str(c).startswith(str(tmp_path)) for c in candidates)
