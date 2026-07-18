"""Detect which launcher installed a game (Steam, Epic, Xbox, or other)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _normalize_title(title: str) -> str:
    """Normalize a game title for comparison."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def get_steam_games() -> set[str]:
    """Return normalized titles of games installed via Steam."""
    games: set[str] = set()
    steam_path = _find_steam_path()
    if not steam_path:
        return games

    steam_apps = steam_path / "steamapps"
    if not steam_apps.exists():
        return games

    # Check all library folders
    library_folders = [steam_apps]
    lib_file = steam_apps / "libraryfolders.vdf"
    if lib_file.exists():
        content = lib_file.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
            p = match.group(1).replace("\\\\", "\\")
            lib_apps = Path(p) / "steamapps"
            if lib_apps.exists():
                library_folders.append(lib_apps)

    for lib in library_folders:
        for manifest in lib.glob("appmanifest_*.acf"):
            content = manifest.read_text(encoding="utf-8", errors="ignore")
            name_match = re.search(r'"name"\s+"([^"]+)"', content)
            if name_match:
                games.add(_normalize_title(name_match.group(1)))

    return games


def _find_steam_path() -> Path | None:
    """Find the Steam installation directory."""
    # sys.platform (not os.name) so mypy skips the winreg block on non-Windows.
    if sys.platform != "win32":
        return None

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"
        ) as key:
            steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
            return Path(steam_path)
    except (OSError, ImportError):
        pass

    # Fallback to common paths
    for candidate in [
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
    ]:
        if candidate.exists():
            return candidate
    return None


def get_epic_games() -> set[str]:
    """Return normalized titles of games installed via Epic Games Launcher."""
    games: set[str] = set()
    manifest_dir = Path("C:/ProgramData/Epic/EpicGamesLauncher/Data/Manifests")
    if not manifest_dir.exists():
        return games

    for manifest_file in manifest_dir.glob("*.item"):
        try:
            data: Any = json.loads(manifest_file.read_text(encoding="utf-8"))
            display_name = data.get("DisplayName", "")
            if display_name:
                games.add(_normalize_title(display_name))
        except (json.JSONDecodeError, OSError):
            continue

    return games


def get_xbox_games() -> set[str]:
    """Return normalized titles of Xbox/UWP games installed on the system."""
    games: set[str] = set()
    if os.name != "nt":
        return games

    try:
        import subprocess

        result = subprocess.run(
            ["powershell", "-Command", "Get-AppxPackage | Select-Object -ExpandProperty Name"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return games

        for line in result.stdout.strip().splitlines():
            name = line.strip()
            # Xbox/UWP game packages typically start with Microsoft. and contain game-related names
            if name.startswith("Microsoft."):
                games.add(_normalize_title(name))
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return games


def detect_launcher(
    game_title: str,
    save_paths: list[str] | None = None,
    steam_games: set[str] | None = None,
    epic_games: set[str] | None = None,
    xbox_games: set[str] | None = None,
) -> str:
    """Detect which launcher installed a game.

    Returns one of: "steam", "epic", "xbox", "other".
    """
    normalized = _normalize_title(game_title)

    # Check Xbox first via save path pattern (most reliable)
    if save_paths:
        for path in save_paths:
            if "Packages/Microsoft." in path or "WindowsApps" in path:
                return "xbox"

    # Use cached sets or fetch fresh
    if steam_games is None:
        steam_games = get_steam_games()
    if epic_games is None:
        epic_games = get_epic_games()
    if xbox_games is None:
        xbox_games = get_xbox_games()

    if normalized in steam_games:
        return "steam"
    if normalized in epic_games:
        return "epic"
    if normalized in xbox_games:
        return "xbox"

    return "other"


def get_all_launcher_games() -> tuple[set[str], set[str], set[str]]:
    """Return (steam_games, epic_games, xbox_games) as normalized title sets."""
    return get_steam_games(), get_epic_games(), get_xbox_games()
