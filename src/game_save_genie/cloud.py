"""Rclone-based cloud sync for Game Save Genie."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from . import cas
from .archive import safe_extract_tar_gz, safe_extract_zip, zip_directory
from .config import get_data_dir, get_default_binary_dir
from .models import CloudProvider, CloudSyncResult, Game, SaveVersion

logger = logging.getLogger(__name__)

RCLONE_RELEASES_URL = "https://api.github.com/repos/rclone/rclone/releases/latest"

# Content-addressed layout under <remote>/<game_id>/
CAS_MANIFEST_DIR = "manifests"
CAS_BLOB_DIR = "blobs"

# Blobs written more recently than this are never GC'd: they may belong to an
# upload from another machine whose manifest has not landed yet (blobs are
# uploaded before the manifest). One hour comfortably covers any real upload.
GC_GRACE_SECONDS = 3600
# Blob GC is best-effort cleanup; throttle it so heavy play (a backup every
# few minutes) does not re-list every manifest on every upload.
GC_MIN_INTERVAL_SECONDS = 1800


def entry_kind(raw_entry: str) -> str:
    """Classify a raw remote listing entry: 'cas', 'zipdir', or 'zip'."""
    if raw_entry.startswith(CAS_MANIFEST_DIR + "/"):
        return "cas"
    if raw_entry.endswith("/"):
        return "zipdir"
    return "zip"


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
        "force_path_style": "false",
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
    release_response = requests.get(RCLONE_RELEASES_URL, timeout=30)
    release_response.raise_for_status()
    release_info = release_response.json()
    asset_name = _rclone_asset_name(release_info)
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
        safe_extract_zip(download_path, target_dir)
    elif asset_name.endswith(".tar.gz"):
        safe_extract_tar_gz(download_path, target_dir)
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


def _rclone_asset_name(release_info: dict[str, Any]) -> str:
    """Return the rclone asset name for the current platform from the release."""
    import platform
    if os.name == "nt":
        suffix = "windows-amd64.zip"
    elif platform.system() == "Darwin":
        suffix = "osx-amd64.zip"
    else:
        suffix = "linux-amd64.tar.gz"

    for asset in release_info.get("assets", []):
        name = str(asset.get("name", ""))
        if name.startswith("rclone-v") and name.endswith(suffix):
            return name
    raise RuntimeError(f"Could not find rclone asset for {suffix}")



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


def _remote_path(remote_name: str, remote_root: str, *parts: str) -> str:
    """Build an rclone remote path, avoiding leading slashes when remote_root is empty."""
    suffix = "/".join(parts).lstrip("/")
    if remote_root:
        prefix = remote_root.rstrip("/")
        return f"{remote_name}:{prefix}/{suffix}"
    return f"{remote_name}:{suffix}"


def upload_save(
    binary: Path,
    game: Game,
    version: SaveVersion,
    remote_name: str,
    remote_root: str,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Upload a save version as a single zip object.

    ``version.local_path`` is normally the per-version snapshot zip; a
    directory (legacy versions) is zipped to a temporary file first. Using
    ``copyto`` (not ``copy``) makes the object land exactly at
    ``<root>/<game_id>/<version_id>.zip`` instead of nesting inside a
    directory of the same name.
    """
    remote_path = _remote_path(remote_name, remote_root, game.id, f"{version.id}.zip")
    upload_source = version.local_path
    temp_zip: Path | None = None

    try:
        if version.local_path.is_dir():
            temp_zip = version.local_path.parent / f"{version.id}.zip"
            zip_directory(version.local_path, temp_zip)
            upload_source = temp_zip

        args = ["copyto", str(upload_source), remote_path, "--progress"]
        if dry_run:
            args.append("--dry-run")
        if extra_args:
            args.extend(extra_args)

        try:
            result = run_rclone(binary, args)
        except RuntimeError as exc:
            return CloudSyncResult(
                success=False, direction="upload", message=str(exc), remote_path=remote_path
            )
    except OSError as exc:
        # e.g. disk full while zipping a legacy directory version — report
        # failure instead of raising into (and killing) the watcher loop.
        return CloudSyncResult(
            success=False, direction="upload", message=f"Could not package save: {exc}",
            remote_path=remote_path,
        )
    finally:
        if temp_zip is not None:
            temp_zip.unlink(missing_ok=True)

    return CloudSyncResult(
        success=True,
        direction="upload",
        message="Upload complete" if not result.stdout.strip() else result.stdout.strip(),
        remote_path=remote_path,
    )


def _version_source_tree(version: SaveVersion, work_dir: Path) -> Path:
    """Return a directory holding this version's backup tree.

    Uses the snapshot zip (immutable, hash-matched) when available, extracting
    it under ``work_dir``; falls back to a directory ``local_path`` for
    versions whose snapshot zipping failed.
    """
    if version.local_path.is_dir():
        return version.local_path
    tree = work_dir / "tree"
    safe_extract_zip(version.local_path, tree)
    return tree


def upload_save_cas(
    binary: Path,
    game: Game,
    version: SaveVersion,
    remote_name: str,
    remote_root: str,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Upload a version as content-addressed blobs plus a manifest.

    Only files whose content is not already in the cloud are transferred
    (``rclone copy --size-only`` skips blobs already present by hash-name).
    The manifest is uploaded LAST, so a version becomes visible only once
    all its blobs are present; an interrupted upload leaves reusable blobs
    and no dangling version.
    """
    manifest_remote = _remote_path(
        remote_name, remote_root, game.id, CAS_MANIFEST_DIR, f"{version.id}.json"
    )
    work = Path(tempfile.mkdtemp(prefix="gsg-cas-"))
    try:
        source_tree = _version_source_tree(version, work)
        manifest = cas.build_manifest(
            source_tree,
            version_id=version.id,
            game_id=game.id,
            created_at=version.created_at.isoformat(),
            source_machine=version.source_machine,
        )
        stage = work / "blobs"
        cas.stage_blobs(source_tree, manifest, stage)

        blobs_remote = _remote_path(remote_name, remote_root, game.id, CAS_BLOB_DIR)
        blob_args = ["copy", str(stage), blobs_remote, "--size-only"]
        if extra_args:
            blob_args.extend(extra_args)
        run_rclone(binary, blob_args)

        manifest_file = work / "manifest.json"
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        man_args = ["copyto", str(manifest_file), manifest_remote]
        if extra_args:
            man_args.extend(extra_args)
        run_rclone(binary, man_args)
    except (RuntimeError, OSError) as exc:
        return CloudSyncResult(
            success=False, direction="upload",
            message=f"CAS upload failed: {exc}", remote_path=manifest_remote,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return CloudSyncResult(
        success=True, direction="upload", message="Upload complete",
        remote_path=manifest_remote,
    )


def download_save_cas(
    binary: Path,
    game: Game,
    version_id: str,
    local_dir: Path,
    remote_name: str,
    remote_root: str,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Reconstruct a content-addressed version into ``local_dir``, verified."""
    manifest_remote = _remote_path(
        remote_name, remote_root, game.id, CAS_MANIFEST_DIR, f"{version_id}.json"
    )
    work = Path(tempfile.mkdtemp(prefix="gsg-casdl-"))
    try:
        manifest_file = work / "manifest.json"
        man_args = ["copyto", manifest_remote, str(manifest_file)]
        if extra_args:
            man_args.extend(extra_args)
        result = run_rclone(binary, man_args, check=False)
        if result.returncode != 0 or not manifest_file.is_file():
            return CloudSyncResult(
                success=False, direction="download",
                message=f"Manifest for {version_id} not found", remote_path="",
            )
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        blob_dir = work / "blobs"
        blob_dir.mkdir()
        keys = sorted(cas.manifest_blob_keys(manifest))
        if keys:
            files_from = work / "files.txt"
            files_from.write_text("\n".join(keys) + "\n", encoding="utf-8")
            blobs_remote = _remote_path(remote_name, remote_root, game.id, CAS_BLOB_DIR)
            blob_args = ["copy", blobs_remote, str(blob_dir), "--files-from", str(files_from)]
            if extra_args:
                blob_args.extend(extra_args)
            blob_result = run_rclone(binary, blob_args, check=False)
            if blob_result.returncode != 0:
                return CloudSyncResult(
                    success=False, direction="download",
                    message=f"Blob download failed (exit {blob_result.returncode})",
                    remote_path=manifest_remote,
                )

        local_dir.mkdir(parents=True, exist_ok=True)
        cas.reconstruct(manifest, blob_dir, local_dir)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        return CloudSyncResult(
            success=False, direction="download",
            message=f"CAS download failed: {exc}", remote_path=manifest_remote,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return CloudSyncResult(
        success=True, direction="download", message="Download complete",
        remote_path=manifest_remote,
    )


def _parse_rfc3339(value: str) -> datetime | None:
    """Parse an rclone ModTime (RFC3339, possibly nanosecond precision)."""
    s = value.strip()
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # trim over-long fractional seconds
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def gc_blobs(
    binary: Path,
    game: Game,
    remote_name: str,
    remote_root: str,
    extra_args: list[str] | None = None,
    grace_seconds: int = GC_GRACE_SECONDS,
) -> int:
    """Delete blobs no longer referenced by any surviving manifest.

    Two guards keep this from ever deleting live save data:

    1. The referenced set is computed from ALL current manifests; if any
       manifest cannot be downloaded or parsed the GC aborts without deleting
       anything (a partial reference set could orphan a real version's blob).
    2. Blobs written within ``grace_seconds`` are never deleted. Uploads write
       blobs before their manifest, so a freshly uploaded blob is briefly
       referenced by no manifest; the grace window protects a concurrent
       upload (e.g. from another machine) whose manifest has not landed yet.

    Returns the number of blobs deleted.
    """
    manifests_remote = _remote_path(remote_name, remote_root, game.id, CAS_MANIFEST_DIR)
    listing = run_rclone(binary, ["lsf", manifests_remote], check=False)
    if listing.returncode == 3:
        return 0
    if listing.returncode != 0:
        logger.warning("GC: cannot list manifests for %s; skipping", game.id)
        return 0
    manifest_names = [
        line.strip().strip("/")
        for line in listing.stdout.splitlines()
        if line.strip().endswith(".json")
    ]

    work = Path(tempfile.mkdtemp(prefix="gsg-gc-"))
    try:
        manifests: list[dict[str, Any]] = []
        for name in manifest_names:
            local = work / name
            args = [
                "copyto",
                _remote_path(remote_name, remote_root, game.id, CAS_MANIFEST_DIR, name),
                str(local),
            ]
            if extra_args:
                args.extend(extra_args)
            if run_rclone(binary, args, check=False).returncode != 0 or not local.is_file():
                logger.warning("GC abort: could not read manifest %s for %s", name, game.id)
                return 0
            try:
                manifests.append(json.loads(local.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                logger.warning("GC abort: bad manifest %s for %s", name, game.id)
                return 0

        referenced = cas.referenced_blob_keys(manifests)
        blobs_remote = _remote_path(remote_name, remote_root, game.id, CAS_BLOB_DIR)
        blob_listing = run_rclone(
            binary, ["lsjson", blobs_remote, "-R", "--files-only"], check=False
        )
        if blob_listing.returncode == 3:
            return 0
        if blob_listing.returncode != 0:
            logger.warning("GC: cannot list blobs for %s; skipping", game.id)
            return 0
        try:
            blobs = json.loads(blob_listing.stdout or "[]")
        except json.JSONDecodeError:
            logger.warning("GC: unparsable blob listing for %s; skipping", game.id)
            return 0

        now = datetime.now(timezone.utc)
        grace = timedelta(seconds=grace_seconds)
        deleted = 0
        for obj in blobs:
            key = str(obj.get("Path", "")).replace("\\", "/")
            if not key or key in referenced:
                continue
            mtime = _parse_rfc3339(str(obj.get("ModTime", "")))
            # Keep a blob whose age can't be determined, or that is too new.
            if mtime is None or (now - mtime) < grace:
                continue
            target = _remote_path(remote_name, remote_root, game.id, CAS_BLOB_DIR, key)
            if run_rclone(binary, ["deletefile", target], check=False).returncode == 0:
                deleted += 1
        return deleted
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _gc_stamp(game_id: str) -> Path:
    return get_data_dir() / "cas_gc" / f"{game_id}.stamp"


def _gc_due(game_id: str) -> bool:
    stamp = _gc_stamp(game_id)
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < GC_MIN_INTERVAL_SECONDS:
            return False
    except OSError:
        pass
    return True


def _mark_gc(game_id: str) -> None:
    stamp = _gc_stamp(game_id)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


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
    """Download a save version from the cloud, verified.

    The remote layout for the requested version is resolved from an actual
    listing rather than probed with ``rclone copy`` exit codes — on bucket
    remotes (S3), copying a nonexistent prefix exits 0 having transferred
    nothing, which would otherwise report success with an empty directory.
    Handles all three historical layouts: flat ``<id>.zip`` objects (current),
    legacy nested ``<id>.zip/<id>.zip`` directories, and legacy uncompressed
    ``<id>/`` directories.
    """
    try:
        entries = list_remote_version_entries(binary, game, remote_name, remote_root)
    except RuntimeError as exc:
        return CloudSyncResult(
            success=False, direction="download", message=str(exc), remote_path=""
        )
    raw_entry = next((raw for vid, raw in entries if vid == version_id), None)
    if raw_entry is None:
        return CloudSyncResult(
            success=False,
            direction="download",
            message=f"Could not find version {version_id} in cloud",
            remote_path="",
        )

    if entry_kind(raw_entry) == "cas":
        if dry_run:
            return CloudSyncResult(
                success=True, direction="download", message="Dry run", remote_path=raw_entry
            )
        return download_save_cas(
            binary, game, version_id, local_dir, remote_name, remote_root, extra_args
        )

    local_dir.mkdir(parents=True, exist_ok=True)
    if raw_entry.endswith("/"):
        # Directory layout (legacy uncompressed, or legacy nested zip dir).
        remote_path = _remote_path(remote_name, remote_root, game.id, raw_entry.rstrip("/"))
        args = ["copy", remote_path, str(local_dir), "--progress"]
    else:
        # Flat zip object: copyto fails properly if the object is missing.
        remote_path = _remote_path(remote_name, remote_root, game.id, raw_entry)
        args = ["copyto", remote_path, str(local_dir / raw_entry), "--progress"]
    if dry_run:
        args.append("--dry-run")
    if extra_args:
        args.extend(extra_args)

    result = run_rclone(binary, args, check=False)
    if result.returncode != 0:
        return CloudSyncResult(
            success=False,
            direction="download",
            message=f"Download failed (exit {result.returncode}): "
            f"{result.stderr or result.stdout}".strip(),
            remote_path=remote_path,
        )

    if dry_run:
        return CloudSyncResult(
            success=True, direction="download", message="Dry run", remote_path=remote_path
        )

    # Extract any downloaded zip files (integrity-checked; a corrupt
    # download fails here instead of half-applying at restore time).
    try:
        for zip_file in local_dir.glob("*.zip"):
            safe_extract_zip(zip_file, local_dir)
            zip_file.unlink(missing_ok=True)
    except RuntimeError as exc:
        return CloudSyncResult(
            success=False,
            direction="download",
            message=f"Downloaded archive failed verification: {exc}",
            remote_path=remote_path,
        )

    if not any(local_dir.iterdir()):
        return CloudSyncResult(
            success=False,
            direction="download",
            message=f"Download of {version_id} produced no files",
            remote_path=remote_path,
        )
    return CloudSyncResult(
        success=True,
        direction="download",
        message="Download complete",
        remote_path=remote_path,
    )


def parse_lsf_entries(stdout: str) -> list[tuple[str, str]]:
    """Parse ``rclone lsf`` output into ``(version_id, raw_entry)`` pairs.

    Raw entries keep their trailing ``/`` for directories (legacy uncompressed
    uploads) so callers can tell files from directories. Entries starting with
    ``_`` are reserved for non-version objects and skipped. The ``.zip``
    suffix is stripped from the version id so compressed and uncompressed
    uploads share one id namespace; duplicates keep the first entry seen.
    """
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        raw = line.strip("\r\n")
        version_id = raw.strip("/")
        if not version_id or version_id.startswith("_"):
            continue
        if version_id in (CAS_MANIFEST_DIR, CAS_BLOB_DIR):
            continue  # CAS structure dirs, not versions
        if version_id.endswith(".zip"):
            version_id = version_id[:-4]
        if version_id not in seen:
            seen.add(version_id)
            entries.append((version_id, raw))
    return entries


def list_remote_version_entries(
    binary: Path,
    game: Game,
    remote_name: str,
    remote_root: str,
) -> list[tuple[str, str]]:
    """List cloud versions for a game as ``(version_id, raw_entry)`` pairs.

    Returns [] only when the listing genuinely holds no versions (including
    rclone exit 3, "directory not found" — a game never uploaded). Any other
    rclone failure raises RuntimeError so callers can tell an unreachable
    remote apart from an empty one — treating "cloud unreachable" as "no
    cloud versions" would silently disable restores.
    """
    remote_path = _remote_path(remote_name, remote_root, game.id)
    result = run_rclone(binary, ["lsf", remote_path], check=False)
    if result.returncode == 3:
        legacy: list[tuple[str, str]] = []
    elif result.returncode != 0:
        raise RuntimeError(
            f"rclone lsf failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    else:
        legacy = parse_lsf_entries(result.stdout)

    # Content-addressed versions live under manifests/<id>.json.
    manifests_remote = _remote_path(remote_name, remote_root, game.id, CAS_MANIFEST_DIR)
    man_result = run_rclone(binary, ["lsf", manifests_remote], check=False)
    cas_entries: list[tuple[str, str]] = []
    if man_result.returncode == 0:
        for line in man_result.stdout.splitlines():
            name = line.strip().strip("/")
            if name.endswith(".json"):
                cas_entries.append((name[:-5], f"{CAS_MANIFEST_DIR}/{name}"))
    elif man_result.returncode != 3:
        raise RuntimeError(
            f"rclone lsf manifests failed (exit {man_result.returncode}): "
            f"{(man_result.stderr or man_result.stdout or '').strip()}"
        )

    # CAS wins over a same-id legacy zip (shouldn't co-occur, but be safe).
    merged: dict[str, str] = {}
    for vid, raw in legacy:
        merged[vid] = raw
    for vid, raw in cas_entries:
        merged[vid] = raw
    return sorted(merged.items(), key=lambda pair: pair[0])


def list_remote_versions(
    binary: Path,
    game: Game,
    remote_name: str,
    remote_root: str,
) -> list[str]:
    """List available version IDs stored in the cloud for a game."""
    return [vid for vid, _ in list_remote_version_entries(binary, game, remote_name, remote_root)]


def select_entries_to_prune(
    entries: list[tuple[str, str]], keep: int
) -> list[tuple[str, str]]:
    """Pick the oldest remote entries beyond ``keep``, newest always retained.

    Pure so the retention policy is unit-testable. Version ids are UTC
    timestamps, so lexicographic order equals chronological order.
    """
    if keep < 1 or len(entries) <= keep:
        return []
    ordered = sorted(entries, key=lambda pair: pair[0])
    return ordered[: len(ordered) - keep]


def prune_remote_versions(
    binary: Path,
    game: Game,
    remote_name: str,
    remote_root: str,
    keep: int,
) -> list[str]:
    """Delete the oldest cloud versions beyond ``keep`` for a game.

    Fail-safe by design: if the remote listing fails nothing is deleted, the
    newest version is never deleted, and individual delete failures are
    logged but never raised. Returns the version ids actually deleted.
    """
    try:
        entries = list_remote_version_entries(binary, game, remote_name, remote_root)
    except RuntimeError as exc:
        logger.warning("Skipping cloud prune for %s: %s", game.id, exc)
        return []
    deleted: list[str] = []
    removed_cas = False
    for version_id, raw_entry in select_entries_to_prune(entries, keep):
        kind = entry_kind(raw_entry)
        if kind == "zipdir":
            # Legacy uncompressed/nested upload: a directory tree.
            target = _remote_path(remote_name, remote_root, game.id, raw_entry.rstrip("/"))
            result = run_rclone(binary, ["purge", target], check=False)
        else:
            # Flat zip object, or a CAS manifest file (blobs GC'd below).
            target = _remote_path(remote_name, remote_root, game.id, raw_entry)
            result = run_rclone(binary, ["deletefile", target], check=False)
        if result.returncode == 0:
            deleted.append(version_id)
            if kind == "cas":
                removed_cas = True
        else:
            logger.warning("Failed to prune cloud version %s for %s", version_id, game.id)

    # Reclaim blobs orphaned by deleted manifests. Version retention (above)
    # is exact every time; blob GC is best-effort and throttled, so it may
    # lag by GC_MIN_INTERVAL_SECONDS — orphaned blobs simply wait one cycle.
    if removed_cas and _gc_due(game.id):
        freed = gc_blobs(binary, game, remote_name, remote_root)
        _mark_gc(game.id)
        if freed:
            logger.info("GC freed %d blob(s) for %s", freed, game.id)
    return deleted


def download_latest_save(
    binary: Path,
    game: Game,
    local_dir: Path,
    remote_name: str,
    remote_root: str,
    extra_args: list[str] | None = None,
) -> CloudSyncResult:
    """Download the latest save version from the cloud for a game."""
    versions = list_remote_versions(binary, game, remote_name, remote_root)
    if not versions:
        return CloudSyncResult(
            success=False,
            direction="download",
            message="No cloud saves found for this game",
            remote_path="",
        )

    latest = sorted(versions)[-1]
    return download_save(
        binary, game, latest, local_dir, remote_name, remote_root,
        dry_run=False, extra_args=extra_args,
    )
