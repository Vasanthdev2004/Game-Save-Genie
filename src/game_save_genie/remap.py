"""Path remapping utilities for cross-platform/cross-machine restore."""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path
from typing import Any

from .models import Game, GameSavePath, Platform


def remap_paths(
    game: Game,
    mapping: dict[str, Any],
    target_platform: Platform | None = None,
) -> list[GameSavePath]:
    """Remap save paths from a backup's mapping.yaml to the current system."""
    if target_platform is None:
        target_platform = _current_platform()

    remapped: list[GameSavePath] = []
    for original, _mapped in mapping.get("games", {}).get(game.title, {}).get("files", {}).items():
        original_path = Path(original)
        remapped_path = _remap_single_path(original_path, target_platform, game)
        is_wine = game.platform == Platform.LINUX and "pfx" in remapped_path.parts
        wine_prefix = game.save_paths[0].wine_prefix_path if game.save_paths else None
        remapped.append(
            GameSavePath(
                path=remapped_path,
                is_wine_prefix=is_wine,
                wine_prefix_path=wine_prefix,
            )
        )
    return remapped


def _remap_single_path(path: Path, target_platform: Platform, game: Game) -> Path:
    """Remap one path from backup source to current system."""
    path_str = str(path)

    # Cross-platform: Wine prefixes on Linux
    if target_platform == Platform.WINDOWS and "drive_c" in path_str:
        # If restoring a Linux Wine backup to Windows, strip the prefix and remap to C:\
        match = re.search(r"drive_c[\\/](.*)", path_str, re.IGNORECASE)
        if match:
            return Path("C:/") / match.group(1).replace("/", "\\")
        return Path("C:/") / path_str.split("drive_c", 1)[1].lstrip("/\\")

    if target_platform == Platform.LINUX and game.save_paths:
        # Use the configured Wine prefix if available.
        wine_prefix = game.save_paths[0].wine_prefix_path
        if wine_prefix and path_str.startswith("C:"):
            relative = path_str[2:].lstrip("/\\")
            return wine_prefix / "drive_c" / relative.replace("\\", "/")

    # User profile remapping (Windows -> Windows or Linux -> Linux)
    current_user = getpass.getuser()
    if "Users" in path_str or "users" in path_str:
        # Replace old username with current username
        path_str = re.sub(r"[Uu]sers[/\\][^/\\]+", f"Users/{current_user}", path_str)

    # HOME remapping (Linux/Mac)
    home = Path.home()
    if path_str.startswith("~/"):
        path_str = str(home / path_str[2:])
    elif path_str.startswith("/home/") or path_str.startswith("/Users/"):
        parts = Path(path_str).parts
        if len(parts) >= 3:
            path_str = str(home / Path(*parts[3:]))

    return Path(path_str)


def _current_platform() -> Platform:
    if os.name == "nt":
        return Platform.WINDOWS
    import platform
    if platform.system() == "Darwin":
        return Platform.MACOS
    return Platform.LINUX
