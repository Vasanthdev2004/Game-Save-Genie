"""Ludusavi binary wrapper for Game Save Genie."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from .archive import safe_extract_tar_gz, safe_extract_zip
from .config import get_default_binary_dir
from .models import BackupResult, Game, GameSavePath, Platform, SaveVersion

logger = logging.getLogger(__name__)

LUDUSAVI_RELEASES_URL = "https://api.github.com/repos/mtkennerly/ludusavi/releases/latest"


def _ludusavi_manifest_path() -> Path:
    """Where Ludusavi keeps the game manifest it downloads."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ludusavi" / "manifest.yaml"


# Stores whose own cloud sync Ludusavi's manifest records. Anything outside
# this set in a manifest entry is ignored rather than guessed at.
CLOUD_PLATFORMS = ("steam", "epic", "gog", "origin", "uplay")


def _manifest_lines() -> list[str] | None:
    """The manifest as lines, or None when it cannot be read.

    Read as bytes and decoded leniently: the manifest carries game titles from
    every locale, and one undecodable byte should not cost the whole lookup.
    """
    manifest = _ludusavi_manifest_path()
    try:
        return manifest.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.debug("Could not read Ludusavi manifest at %s: %s", manifest, exc)
        return None


def _entry_blocks(lines: list[str], titles: set[str]) -> dict[str, list[str]]:
    """The manifest block for each requested title that is present."""
    wanted = {f"{title}:": title for title in titles}
    blocks: dict[str, list[str]] = {}
    index = 0
    total = len(lines)
    while index < total:
        title = wanted.get(lines[index].rstrip())
        if title is None:
            index += 1
            continue
        end = index + 1
        while end < total and (not lines[end] or lines[end][0].isspace()):
            end += 1
        blocks[title] = lines[index:end]
        index = end
    return blocks


def cloud_platforms_for_titles(titles: set[str]) -> dict[str, set[str]]:
    """Which stores sync each title's saves natively, per Ludusavi's manifest.

    A manifest entry may carry a ``cloud:`` block naming the stores that
    provide their own save sync, e.g. ``cloud: {steam: true}``. Only stores
    recorded as true are returned; a title with no cloud block is absent from
    the result rather than mapped to an empty set, so "unknown" and "none"
    stay distinguishable.

    This says the GAME supports a store's cloud sync. It does not say YOUR
    copy is synced by it: a repack, a GOG offline installer or a Hydra install
    of a Steam-Cloud game is covered by nothing. That distinction is the whole
    reason gsg exists, so callers must treat this as information to offer and
    never as grounds to stop protecting something on their own initiative
    (#51).
    """
    if not titles:
        return {}
    lines = _manifest_lines()
    if lines is None:
        return {}

    found: dict[str, set[str]] = {}
    for title, block in _entry_blocks(lines, titles).items():
        try:
            cloud_at = next(i for i, line in enumerate(block) if line.strip() == "cloud:")
        except StopIteration:
            continue
        indent = len(block[cloud_at]) - len(block[cloud_at].lstrip())
        platforms: set[str] = set()
        for line in block[cloud_at + 1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if len(line) - len(line.lstrip()) <= indent:
                break
            key, _, value = stripped.partition(":")
            if key in CLOUD_PLATFORMS and value.strip() == "true":
                platforms.add(key)
        if platforms:
            found[title] = platforms
    return found


def titles_without_save_data(titles: set[str]) -> set[str]:
    """Of ``titles``, the ones Ludusavi knows hold no save data.

    A manifest entry tags every path it lists: ``save``, ``config``, and so
    on. Roblox's entry, for instance, is two files both tagged ``config`` -
    graphics and control preferences - because Roblox keeps progress on the
    account. Backing that up produces versions of a settings file (#47).

    The scan API does not report tags, only ``change`` and ``bytes``, so the
    tags have to come from the manifest.

    Deliberately conservative. A title is returned only when its entry lists
    file paths and NONE of them is tagged ``save``. An entry with no tags, a
    title that is absent, or an unreadable manifest all yield nothing, so the
    caller behaves exactly as it did before. This must never withhold a
    backup on a guess; it may only decline to start one we know is pointless.

    Parsed line by line rather than with a YAML loader: the manifest is over
    half a million lines, and this runs on the scan path.
    """
    if not titles:
        return set()
    lines = _manifest_lines()
    if lines is None:
        return set()

    found: set[str] = set()
    for title, block in _entry_blocks(lines, titles).items():
        has_files = any(line.strip() == "files:" for line in block)
        # An entry that tags nothing tells us nothing. Requiring at least one
        # tag is what keeps "no save tag" from meaning "no tags at all".
        has_any_tag = any(line.strip() == "tags:" for line in block)
        has_save_tag = any(line.strip() == "- save" for line in block)
        if has_files and has_any_tag and not has_save_tag:
            found.add(title)
    return found


def get_ludusavi_path(config_path: Path | None = None) -> Path:
    """Return the Ludusavi binary path, downloading if necessary."""
    from .config import load_config

    cfg = load_config(config_path)
    if cfg.ludusavi_path and cfg.ludusavi_path.exists():
        return cfg.ludusavi_path

    # A Ludusavi the user installed themselves wins over one we fetch. This is
    # what get_rclone_path already does, and it is the only route open to
    # anyone upstream publishes no build for: ARM Linux and Intel macOS both
    # have to build or package it themselves.
    system_path = shutil.which("ludusavi")
    if system_path:
        return Path(system_path)

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
    release_response = requests.get(LUDUSAVI_RELEASES_URL, timeout=30)
    release_response.raise_for_status()
    release_info = release_response.json()
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
        safe_extract_zip(download_path, target_dir)
    elif asset_name.endswith((".tar.gz", ".tgz")):
        safe_extract_tar_gz(download_path, target_dir)
    else:
        raise RuntimeError(f"Unsupported archive format: {asset_name}")

    if not binary_path.exists():
        raise RuntimeError(f"Ludusavi binary not found after extraction at {binary_path}")

    if os.name != "nt":
        binary_path.chmod(0o755)

    download_path.unlink(missing_ok=True)
    logger.info("Ludusavi downloaded to %s", binary_path)
    return binary_path


_INSTALL_IT_YOURSELF = (
    "Install Ludusavi yourself and gsg will use it: it checks PATH before "
    "downloading anything. See https://github.com/mtkennerly/ludusavi"
)


def unsupported_architecture_reason() -> str | None:
    """Why this machine cannot run an official Ludusavi build, or None.

    Ludusavi's asset names carry no architecture, so a mismatch cannot be
    caught by name the way rclone's could (#23). Upstream builds exactly one
    Linux target, x86_64, and exactly one macOS target, arm64. Everything else
    downloads successfully and then fails to exec, and because the unrunnable
    binary is cached, every later run fails the same way.
    """
    import platform

    system = platform.system()
    machine = platform.machine().lower()

    if system == "Linux" and machine not in ("x86_64", "amd64"):
        return (
            f"Ludusavi publishes no Linux build for {platform.machine()} "
            f"(upstream builds x86_64 only)."
        )
    if system == "Darwin" and machine not in ("arm64", "aarch64"):
        return (
            f"Ludusavi publishes no macOS build for {platform.machine()} "
            f"(upstream builds Apple Silicon only; Rosetta cannot run an "
            f"arm64 binary on Intel)."
        )
    return None


def _ludusavi_asset_name(release_info: dict[str, Any]) -> str:
    """Return the asset name for the current platform from the release."""
    import platform

    reason = unsupported_architecture_reason()
    if reason:
        raise RuntimeError(f"{reason} {_INSTALL_IT_YOURSELF}")

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
    raise RuntimeError(
        f"This Ludusavi release has no {suffix} build. {_INSTALL_IT_YOURSELF}"
    )


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
        encoding="utf-8",
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
    return [
        GameSavePath(path=Path(entry["path"]))
        for entry in data.get("games", {}).get(game.title, {}).get("files", {}).values()
    ]


def _wine_prefix_args(game: Game) -> list[str]:
    if game.platform == Platform.LINUX and any(p.wine_prefix_path for p in game.save_paths):
        wine_prefix = next(p.wine_prefix_path for p in game.save_paths if p.wine_prefix_path)
        if wine_prefix:
            return ["--wine-prefix", str(wine_prefix)]
    return []


def preview_backup(binary: Path, game: Game, backup_dir: Path) -> BackupResult:
    """Report what a backup would change, without writing anything."""
    game_backup_dir = backup_dir / game.id
    args = ["backup", game.title, "--api", "--preview", "--path", str(game_backup_dir)]
    args.extend(_wine_prefix_args(game))

    try:
        result = run_ludusavi(binary, args)
    except RuntimeError as exc:
        return BackupResult(success=False, game_id=game.id, message=str(exc))

    data = _parse_json(result.stdout)
    files_data = data.get("games", {}).get(game.title, {}).get("files", {})
    files_changed = sum(
        1 for f in files_data.values() if f.get("change") in ("New", "Different")
    )
    size_bytes = sum(int(f.get("bytes", 0)) for f in files_data.values())
    if files_changed == 0:
        message = "No changes to back up"
    else:
        message = (
            f"Would back up {files_changed} changed of {len(files_data)} file(s) "
            f"({_human_size(size_bytes)})"
        )
    return BackupResult(
        success=True, game_id=game.id, message=message, files_changed=files_changed
    )


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
    args.extend(_wine_prefix_args(game))

    try:
        result = run_ludusavi(binary, args)
    except RuntimeError as exc:
        return BackupResult(success=False, game_id=game.id, message=str(exc))

    data = json.loads(result.stdout)
    files_data = data.get("games", {}).get(game.title, {}).get("files", {})

    # Ludusavi marks each file as "New", "Different", or "Same".
    files_changed = sum(
        1 for f in files_data.values() if f.get("change") in ("New", "Different")
    )
    if not files_data:
        # Ludusavi found the game but no save files at all. That is not "no
        # changes" - it usually means the saves moved, the drive is not
        # mounted, or the game was uninstalled. Reporting success here told a
        # user with a relocated save that everything was fine, every session,
        # indefinitely (#41).
        logger.warning(
            "No save files found for %s; nothing was backed up", game.title
        )
        return BackupResult(
            success=False,
            game_id=game.id,
            message=(
                f"No save files found for {game.title}. The saves may have "
                f"moved, or the drive may not be mounted."
            ),
            files_changed=0,
        )

    if files_changed == 0:
        return BackupResult(
            success=True,
            game_id=game.id,
            message="No changes detected since last backup",
            files_changed=0,
        )

    # Build version metadata from the Ludusavi output.
    file_count = len(files_data)
    size_bytes = sum(int(f.get("bytes", 0)) for f in files_data.values())

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
        label=label
        or f"Backup on {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
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


def restore_from_backup(
    binary: Path,
    game: Game,
    backup_path: Path,
) -> dict[str, Any]:
    """Restore a game's saves from a Ludusavi backup directory.

    ``backup_path`` must contain the Ludusavi backup structure (``mapping.yaml``
    plus the backed-up drive folders), which is exactly what a downloaded and
    extracted cloud save provides.
    """
    args = ["restore", game.title, "--api", "--force", "--path", str(backup_path)]
    args.extend(_wine_prefix_args(game))

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
