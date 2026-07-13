"""Rclone-based cloud sync for Game Save Genie."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import requests

from .config import get_default_binary_dir
from .models import CloudProvider, CloudSyncResult, Game, SaveVersion

logger = logging.getLogger(__name__)

RCLONE_RELEASES_URL = "https://api.github.com/repos/rclone/rclone/releases/latest"


def get_rclone_config_path() -> Path:
    """Return the default rclone configuration file path."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    else:
        base = Path.home() / ".config"
    return base / "rclone" / "rclone.conf"


def _read_rclone_config() -> dict[str, dict[str, str]]:
    """Parse rclone.conf into a nested dict."""
    config_path = get_rclone_config_path()
    if not config_path.exists():
        return {}

    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    with config_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                name = line[1:-1]
                current = {}
                sections[name] = current
            elif current is not None and "=" in line:
                key, value = line.split("=", 1)
                current[key.strip()] = value.strip()
    return sections


def _write_rclone_config(sections: dict[str, dict[str, str]]) -> None:
    """Write rclone.conf from a nested dict."""
    config_path = get_rclone_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        for name, entries in sections.items():
            f.write(f"[{name}]\n")
            for key, value in entries.items():
                f.write(f"{key} = {value}\n")
            f.write("\n")


def write_railway_s3_config(
    remote_name: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str = "auto",
) -> Path:
    """Write an rclone S3 remote config for Railway S3-compatible storage."""
    sections = _read_rclone_config()
    sections[remote_name] = {
        "type": "s3",
        "provider": "Other",
        "env_auth": "false",
        "access_key_id": access_key,
        "secret_access_key": secret_key,
        "endpoint": endpoint,
        "region": region,
        "bucket": bucket,
        "force_path_style": "true",
    }
    _write_rclone_config(sections)
    return get_rclone_config_path()


def get_remote_size(binary: Path, remote_name: str, remote_root: str) -> tuple[int, int]:
    """Return (total_objects, total_bytes) for a remote path, or (0,0) on error."""
    remote_path = f"{remote_name}:{remote_root}"
    result = run_rclone(binary, ["size", remote_path, "--json"], check=False)
    if result.returncode != 0:
        return 0, 0
    data: dict[str, Any] = {}
    try:
        import json
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return 0, 0
    return int(data.get("count", 0)), int(data.get("bytes", 0))



def get_rclone_path(config_path: Path | None = None) -> Path:
    """Return the rclone binary path, downloading if necessary."""
    from .config import load_config

    cfg = load_config(config_path)
    if cfg.rclone_path and cfg.rclone_path.exists():
        return cfg.rclone_path

    system_path = shutil.which("rclone")
    if system_path:
        return Path(system_path)

    binary_dir = get_default_binary_dir()
    binary_name = "rclone.exe" if os.name == "nt" else "rclone"
    candidate = binary_dir / binary_name
    if candidate.exists():
        return candidate

    return download_rclone(binary_dir)


def download_rclone(target_dir: Path) -> Path:
    """Download the latest rclone release for the current platform."""
    target_dir.mkdir(parents=True, exist_ok=True)
    binary_name = "rclone.exe" if os.name == "nt" else "rclone"
    binary_path = target_dir / binary_name

    logger.info("Downloading rclone...")
    release_info = requests.get(RCLONE_RELEASES_URL, timeout=30).json()
    asset_name = _rclone_asset_name()
    asset_url: str | None = None
    for asset in release_info.get("assets", []):
        if asset["name"] == asset_name:
            asset_url = asset["browser_download_url"]
            break

    if asset_url is None:
        raise RuntimeError(f"Could not find rclone asset: {asset_name}")

    download_path = target_dir / asset_name
    response = requests.get(asset_url, timeout=180, stream=True)
    response.raise_for_status()
    with download_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(download_path, "r") as zf:
            zf.extractall(target_dir)
    elif asset_name.endswith(".tar.gz"):
        with tarfile.open(download_path, "r:gz") as tf:
            tf.extractall(target_dir)
    else:
        raise RuntimeError(f"Unsupported archive format: {asset_name}")

    extracted_dir = target_dir / asset_name.replace(".zip", "").replace(".tar.gz", "")
    extracted_binary = extracted_dir / binary_name
    if not extracted_binary.exists():
        raise RuntimeError(f"rclone binary not found after extraction at {extracted_binary}")

    shutil.move(str(extracted_binary), str(binary_path))
    shutil.rmtree(extracted_dir, ignore_errors=True)
    if os.name != "nt":
        binary_path.chmod(0o755)

    download_path.unlink(missing_ok=True)
    logger.info("rclone downloaded to %s", binary_path)
    return binary_path


def _rclone_asset_name() -> str:
    """Return the rclone asset name for the current platform."""
    if os.name == "nt":
        return "rclone-v1.68.2-windows-amd64.zip"
    import platform
    if platform.system() == "Darwin":
        return "rclone-v1.68.2-osx-amd64.zip"
    return "rclone-v1.68.2-linux-amd64.tar.gz"


def run_rclone(
    binary: Path,
    args: list[str],
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run rclone with the given arguments."""
    cmd = [str(binary), *args]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=capture_output, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"rclone failed (exit {result.returncode}): {result.stderr or result.stdout}"
        )
    return result


def configure_remote(
    binary: Path,
    provider: CloudProvider,
    remote_name: str,
) -> CloudSyncResult:
    """Configure an rclone remote interactively."""
    if provider == CloudProvider.LOCAL:
        return CloudSyncResult(
            success=True,
            direction="config",
            message="Local provider does not need rclone config.",
            remote_path="",
        )

    result = run_rclone(binary, ["config", "create", remote_name, provider.value], check=False)
    success = result.returncode == 0
    return CloudSyncResult(
        success=success,
        direction="config",
        message=result.stdout if success else result.stderr,
        remote_path=remote_name,
    )


def upload_save(
    binary: Path,
    game: Game,
    version: SaveVersion,
    remote_name: str,
    remote_root: str,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Upload a save version to the configured cloud provider."""
    remote_path = f"{remote_name}:{remote_root}/{game.id}/{version.id}"
    args = ["copy", str(version.local_path), remote_path, "--progress"]
    if dry_run:
        args.append("--dry-run")
    if extra_args:
        args.extend(extra_args)

    try:
        result = run_rclone(binary, args)
    except RuntimeError as exc:
        return CloudSyncResult(success=False, direction="upload", message=str(exc), remote_path=remote_path)

    return CloudSyncResult(
        success=True,
        direction="upload",
        message=result.stdout.strip() or "Upload complete",
        remote_path=remote_path,
    )


def download_save(
    binary: Path,
    game: Game,
    version_id: str,
    local_dir: Path,
    remote_name: str,
    remote_root: str,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Download a save version from the cloud."""
    remote_path = f"{remote_name}:{remote_root}/{game.id}/{version_id}"
    local_dir.mkdir(parents=True, exist_ok=True)
    args = ["copy", remote_path, str(local_dir), "--progress"]
    if dry_run:
        args.append("--dry-run")
    if extra_args:
        args.extend(extra_args)

    try:
        result = run_rclone(binary, args)
    except RuntimeError as exc:
        return CloudSyncResult(success=False, direction="download", message=str(exc), remote_path=remote_path)

    return CloudSyncResult(
        success=True,
        direction="download",
        message=result.stdout.strip() or "Download complete",
        remote_path=remote_path,
    )


def sync_bidirectional(
    binary: Path,
    game: Game,
    local_dir: Path,
    remote_name: str,
    remote_root: str,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Bidirectional sync between local and cloud for a game."""
    remote_path = f"{remote_name}:{remote_root}/{game.id}"
    args = ["bisync", str(local_dir), remote_path, "--resync", "--progress"]
    if dry_run:
        args.append("--dry-run")
    if extra_args:
        args.extend(extra_args)

    try:
        result = run_rclone(binary, args)
    except RuntimeError as exc:
        return CloudSyncResult(success=False, direction="bidirectional", message=str(exc), remote_path=remote_path)

    return CloudSyncResult(
        success=True,
        direction="bidirectional",
        message=result.stdout.strip() or "Bidirectional sync complete",
        remote_path=remote_path,
    )


def list_remote_versions(
    binary: Path,
    game: Game,
    remote_name: str,
    remote_root: str,
) -> list[str]:
    """List available version IDs stored in the cloud for a game."""
    remote_path = f"{remote_name}:{remote_root}/{game.id}"
    result = run_rclone(binary, ["lsf", remote_path, "--dirs-only"], check=False)
    if result.returncode != 0:
        return []
    return [line.strip("/\r\n") for line in result.stdout.splitlines() if line.strip()]
