"""Path remapping utilities for cross-platform/cross-machine restore."""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import Game, GameSavePath, Platform

# Windows profile folders that are shared, not per-user — never remapped.
_SHARED_PROFILES = {"public", "default", "default user", "all users"}


def remap_windows_user_path(path_str: str, current_user: str) -> str:
    """Rewrite ONLY the user-profile segment directly after the drive root.

    ``C:/Users/alice/Saved Games/x`` -> ``C:/Users/<current>/Saved Games/x``.
    Deeper ``users`` path segments (e.g. ``C:/Games/users/data``) and shared
    profiles (Public, Default) are left untouched. Output uses forward
    slashes, matching Ludusavi's mapping.yaml key format.
    """
    match = re.match(r"^([A-Za-z]:)[/\\](Users)[/\\]([^/\\]+)((?:[/\\].*)?)$", path_str)
    if not match:
        return path_str
    profile = match.group(3)
    if profile.lower() == current_user.lower() or profile.lower() in _SHARED_PROFILES:
        return path_str
    rest = (match.group(4) or "").replace("\\", "/")
    return f"{match.group(1)}/{match.group(2)}/{current_user}{rest}"


def current_profile_name() -> str:
    """The current machine's profile identity for path remapping.

    On Windows this must be the on-disk profile FOLDER name (from
    %USERPROFILE%), not the logon name — accounts renamed via Settings keep
    their original folder, and Ludusavi records the folder.
    """
    return Path.home().name or getpass.getuser()


def apply_remap_to_staged_backup(game_dir: Path, current_user: str | None = None) -> int:
    """Rewrite a staged Ludusavi backup so it restores onto THIS machine.

    A Ludusavi backup records literal absolute paths as mapping.yaml file
    keys and mirrors them in per-backup trees: the backup named ``.`` lives
    directly in the game dir, while retention creates ``backup-<ts>/``
    subfolders, and differential backups nest under ``children``. A backup
    made under another Windows username would restore into that user's
    (usually nonexistent) profile, so this rewrites the mapping keys AND
    moves the stored files so both stay consistent.

    Returns the number of remapped paths (0 when nothing needed changing).
    Raises RuntimeError for layouts that cannot be remapped safely (zip-
    format backups), and OSError/yaml.YAMLError on I/O failure; callers
    must treat any raise as "do not restore this staging dir".
    """
    mapping_path = game_dir / "mapping.yaml"
    if not mapping_path.is_file():
        return 0
    with mapping_path.open("r", encoding="utf-8") as f:
        mapping = yaml.safe_load(f) or {}

    user = current_user or current_profile_name()
    drives = mapping.get("drives") or {}
    folder_for_drive = {str(letter).upper(): str(folder) for folder, letter in drives.items()}

    remapped = 0
    for backup in mapping.get("backups") or []:
        remapped += _remap_backup_entry(game_dir, folder_for_drive, backup, user)

    if remapped:
        with mapping_path.open("w", encoding="utf-8") as f:
            yaml.dump(mapping, f, sort_keys=False, allow_unicode=True)
        _prune_empty_dirs(game_dir)
    return remapped


def _remap_backup_entry(
    game_dir: Path,
    folder_for_drive: dict[str, str],
    entry: dict[str, Any],
    user: str,
) -> int:
    """Remap one backups[] entry (and its differential children) in place."""
    remapped = 0
    name = str(entry.get("name") or ".")
    files = entry.get("files") or {}

    if name.endswith(".zip"):
        # Ludusavi's zip backup format stores the tree inside an archive we
        # do not unpack. Refuse rather than rewrite keys we cannot mirror.
        if any(remap_windows_user_path(str(k), user) != str(k) for k in files):
            raise RuntimeError(
                f"backup '{name}' uses Ludusavi's zip format and needs path "
                f"remapping; restore it on the original machine or switch "
                f"Ludusavi's backup format to 'simple'"
            )
    else:
        base_dir = game_dir if name == "." else game_dir / name
        new_files: dict[str, Any] = {}
        for key, meta in files.items():
            old_key = str(key)
            new_key = remap_windows_user_path(old_key, user)
            if (
                new_key != old_key
                and new_key not in files  # another entry already owns that path
                and new_key not in new_files  # two old users converging
                and _move_stored_file(base_dir, folder_for_drive, old_key, new_key)
            ):
                new_files[new_key] = meta
                remapped += 1
            else:
                new_files[old_key] = meta
        entry["files"] = new_files

    for child in entry.get("children") or []:
        remapped += _remap_backup_entry(game_dir, folder_for_drive, child, user)
    return remapped


def _move_stored_file(
    base_dir: Path,
    folder_for_drive: dict[str, str],
    old_key: str,
    new_key: str,
) -> bool:
    """Move a backup's stored file to mirror the remapped key.

    Returns True only when the file was actually moved — the mapping key is
    rewritten solely in that case. A missing stored file or a collision
    keeps the original key so the backup stays self-consistent.
    """
    old_rel = _key_to_relative(folder_for_drive, old_key)
    new_rel = _key_to_relative(folder_for_drive, new_key)
    if old_rel is None or new_rel is None:
        return False
    old_path = base_dir / old_rel
    new_path = base_dir / new_rel
    if not old_path.exists() or new_path.exists():
        return False
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    return True


def _key_to_relative(folder_for_drive: dict[str, str], key: str) -> Path | None:
    match = re.match(r"^([A-Za-z]:)[/\\](.*)$", key)
    if not match:
        return None
    folder = folder_for_drive.get(match.group(1).upper())
    if folder is None:
        return None
    return Path(folder) / match.group(2).replace("\\", "/")


def _prune_empty_dirs(root: Path) -> None:
    """Remove directories emptied by file moves (deepest first)."""
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass  # not empty (or locked) — fine


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
