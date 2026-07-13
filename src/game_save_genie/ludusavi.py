"""Ludusavi binary wrapper for Game Save Genie."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import requests

from .config import get_default_binary_dir
from .models import BackupResult, Game, GameSavePath, Platform, SaveVersion

logger = logging.getLogger(__name__)

LUDUSAVI_RELEASES_URL = "https://api.github.com/repos/mtkennerly/ludusavi/releases/latest"


def get_ludusavi_path(config_path: Path | None = None) -> Path:
    """Return the Ludusavi binary path, downloading if necessary."""
    from .config import load_config

    cfg = load_config(config_path)
    if cfg.ludusavi_path and cfg.ludusavi_path.exists():
        return cfg.ludusavi_path

    binary_dir = get_default_binary_dir()
    binary_name = "ludusavi.exe" if os.name == "nt" else "ludusavi"
    candidate = binary_dir / binary_name
    if candidate.exists():
        return candidate

    return download_ludusavi(binary_dir)


def download_ludusavi(target_dir: Path) -> Path:
    """Download the latest Ludusavi release for the current platform."""
    target_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "ludusavi.exe" if os.name == "nt" else "ludusavi"
    binary_path = target_dir / binary_name

    logger.info("Downloading Ludusavi...")
    release_info = requests.get(LUDUSAVI_RELEASES_URL, timeout=30).json()
    asset_name = _ludusavi_asset_name(release_info)
    asset_url: str | None = None
    for asset in release_info.get("assets", []):
        if asset["name"] == asset_name:
            asset_url = asset["browser_download_url"]
            break

    if asset_url is None:
        raise RuntimeError(f"Could not find Ludusavi asset: {asset_name}")

    download_path = target_dir / asset_name
    response = requests.get(asset_url, timeout=120, stream=True)
    response.raise_for_status()
    with download_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(download_path, "r") as zf:
            zf.extractall(target_dir)
    elif asset_name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(download_path, "r:gz") as tf:
            tf.extractall(target_dir)
    else:
        raise RuntimeError(f"Unsupported archive format: {asset_name}")

    if not binary_path.exists():
        raise RuntimeError(f"Ludusavi binary not found after extraction at {binary_path}")

    if os.name != "nt":
        binary_path.chmod(0o755)

    download_path.unlink(missing_ok=True)
    logger.info("Ludusavi downloaded to %s", binary_path)
    return binary_path


def _ludusavi_asset_name(release_info: dict[str, Any]) -> str:
    """Return the asset name for the current platform from the release."""
    import platform
    if os.name == "nt":
        suffix = "win64.zip"
    elif platform.system() == "Darwin":
        suffix = "mac.tar.gz"
    else:
        suffix = "linux.tar.gz"

    for asset in release_info.get("assets", []):
        name = str(asset.get("name", ""))
        if name.startswith("ludusavi-") and name.endswith(suffix):
            return name
    raise RuntimeError(f"Could not find Ludusavi asset for {suffix}")


def run_ludusavi(
    binary: Path,
    args: list[str],
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Ludusavi with the given arguments."""
    cmd = [str(binary), *args]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=capture_output,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Ludusavi failed (exit {result.returncode}): {result.stderr or result.stdout}"
        )
    return result


def _parse_json(stdout: str) -> dict[str, Any]:
    return json.loads(stdout)  # type: ignore[no-any-return]


def scan_games(binary: Path) -> dict[str, Any]:
    """Scan the system for games with Ludusavi and return raw API data."""
    result = run_ludusavi(binary, ["backup", "--api", "--preview"])
    return _parse_json(result.stdout)


def find_game_saves(binary: Path, game: Game) -> list[GameSavePath]:
    """Use Ludusavi to detect save paths for a specific game."""
    if not game.shop or not game.shop_object_id:
        return []

    args = ["backup", game.title, "--api", "--preview"]
    if game.platform == Platform.LINUX and any(p.wine_prefix_path for p in game.save_paths):
        wine_prefix = game.save_paths[0].wine_prefix_path
        if wine_prefix:
            args.extend(["--wine-prefix", str(wine_prefix)])

    result = run_ludusavi(binary, args, check=False)
    if result.returncode != 0:
        return []

    data = _parse_json(result.stdout)
    paths: list[GameSavePath] = []
    for entry in data.get("games", {}).get(game.title, {}).get("files", {}).values():
        paths.append(GameSavePath(path=Path(entry["path"])))
    return paths


def backup_game(
    binary: Path,
    game: Game,
    backup_dir: Path,
    label: str | None = None,
) -> BackupResult:
    """Back up a game's saves using Ludusavi."""
    game_backup_dir = backup_dir / game.id
    game_backup_dir.mkdir(parents=True, exist_ok=True)

    args = ["backup", game.title, "--api", "--force", "--path", str(game_backup_dir)]
    if game.platform == Platform.LINUX and any(p.wine_prefix_path for p in game.save_paths):
        wine_prefix = next(p.wine_prefix_path for p in game.save_paths if p.wine_prefix_path)
        if wine_prefix:
            args.extend(["--wine-prefix", str(wine_prefix)])

    try:
        result = run_ludusavi(binary, args)
    except RuntimeError as exc:
        return BackupResult(success=False, game_id=game.id, message=str(exc))

    data = json.loads(result.stdout)
    files_changed = data.get("overall", {}).get("changed", {}).get("files", 0)
    if files_changed == 0:
        return BackupResult(
            success=True,
            game_id=game.id,
            message="No changes detected since last backup",
            files_changed=0,
        )

    # Build version metadata from the Ludusavi output.
    file_count = 0
    size_bytes = 0
    for file_info in data.get("games", {}).get(game.title, {}).get("files", {}).values():
        file_count += 1
        size_bytes += int(file_info.get("size", 0))

    from datetime import datetime, timezone
    from .config import get_machine_id

    version_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    version = SaveVersion(
        id=version_id,
        game_id=game.id,
        created_at=datetime.now(timezone.utc),
        local_path=game_backup_dir,
        size_bytes=size_bytes,
        file_count=file_count,
        label=label or f"Backup on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        source_machine=get_machine_id(),
        platform=game.platform,
    )

    return BackupResult(
        success=True,
        game_id=game.id,
        version=version,
        message=f"Backed up {file_count} files ({_human_size(size_bytes)})",
        files_changed=files_changed,
    )


def restore_game(
    binary: Path,
    game: Game,
    version: SaveVersion,
) -> dict[str, Any]:
    """Restore a save version using Ludusavi."""
    args = ["restore", game.title, "--api", "--force", "--path", str(version.local_path)]
    if game.platform == Platform.LINUX and any(p.wine_prefix_path for p in game.save_paths):
        wine_prefix = next(p.wine_prefix_path for p in game.save_paths if p.wine_prefix_path)
        if wine_prefix:
            args.extend(["--wine-prefix", str(wine_prefix)])

    result = run_ludusavi(binary, args)
    return _parse_json(result.stdout)


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ["KiB", "MiB", "GiB", "TiB"]:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} TiB"
