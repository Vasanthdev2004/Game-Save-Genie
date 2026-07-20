"""Backup and restore for custom-path games (emulators, arbitrary folders).

Games added with ``gsg add --path`` bypass Ludusavi: their save locations are
user-specified directories or files (RetroArch, PCSX2, Dolphin, or any game
Ludusavi's manifest misses). Backups mirror those paths into a per-game backup
tree with a small ``gsg-custom.json`` manifest, then flow through the exact
same snapshot / CAS-upload / retention / restore-staging pipeline as Ludusavi
backups — only the produce-the-tree and apply-the-tree steps differ.

Restore writes each mirrored root back to the game's LOCALLY CONFIGURED
save_path (matched by index), never to a path taken from the manifest. That
keeps cross-machine restore working (each machine uses its own paths) and
means a tampered manifest can't redirect a restore outside the paths the user
declared with ``gsg add --path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .archive import sha256_file
from .models import BackupResult, Game, SaveVersion

MANIFEST_NAME = "gsg-custom.json"
MANIFEST_FORMAT = "gsg-custom-1"
ROOTS_DIR = "roots"


def _iter_root_files(root: Path) -> Iterator[tuple[Path, Path]]:
    """Yield (relative_path, absolute_file) for every file under a save root."""
    if root.is_file():
        yield Path(root.name), root
    elif root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                yield path.relative_to(root), path


def compute_source_digest(game: Game) -> tuple[str, int, int]:
    """Return (stable content digest, total bytes, file count) over all paths.

    The digest is order-stable (root index + posix relpath + file hash), so an
    unchanged save set yields the same digest across backups and machines.
    """
    hasher = hashlib.sha256()
    total_bytes = 0
    file_count = 0
    for index, save_path in enumerate(game.save_paths):
        for rel, absolute in _iter_root_files(save_path.path):
            digest = sha256_file(absolute)
            hasher.update(f"{index}\0{rel.as_posix()}\0{digest}\n".encode())
            total_bytes += absolute.stat().st_size
            file_count += 1
    return hasher.hexdigest(), total_bytes, file_count


def custom_backup_valid(backup_dir: Path) -> bool:
    """Whether a staged directory holds a custom-game backup."""
    return (backup_dir / MANIFEST_NAME).is_file()


def backup_custom(
    game: Game,
    backup_dir: Path,
    label: str | None = None,
    previous_digest: str | None = None,
) -> BackupResult:
    """Mirror a custom game's configured paths into a backup tree.

    Returns a version-less success when nothing changed since
    ``previous_digest`` or when no files exist at the configured paths.
    """
    if not game.save_paths:
        return BackupResult(
            success=False, game_id=game.id,
            message="No --path locations configured for this game",
        )

    digest, total_bytes, file_count = compute_source_digest(game)
    if file_count == 0:
        return BackupResult(
            success=True, game_id=game.id, files_changed=0,
            message="No save files found at the configured paths",
        )
    if digest == previous_digest:
        return BackupResult(
            success=True, game_id=game.id, files_changed=0,
            message="No changes detected since last backup",
        )

    game_backup_dir = backup_dir / game.id
    if game_backup_dir.exists():
        shutil.rmtree(game_backup_dir, ignore_errors=True)
    game_backup_dir.mkdir(parents=True, exist_ok=True)

    roots_meta: list[dict[str, object]] = []
    for index, save_path in enumerate(game.save_paths):
        root = save_path.path
        dest = game_backup_dir / ROOTS_DIR / str(index)
        if root.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root, dest / root.name)
            roots_meta.append(
                {"index": index, "original": str(root), "type": "file", "name": root.name}
            )
        elif root.is_dir():
            shutil.copytree(root, dest, dirs_exist_ok=True)
            roots_meta.append({"index": index, "original": str(root), "type": "dir"})
        else:
            roots_meta.append({"index": index, "original": str(root), "type": "missing"})

    manifest = {"format": MANIFEST_FORMAT, "game_id": game.id, "roots": roots_meta}
    (game_backup_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    version = SaveVersion(
        id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"),
        game_id=game.id,
        created_at=datetime.now(timezone.utc),
        local_path=game_backup_dir,
        size_bytes=total_bytes,
        file_count=file_count,
        label=label or f"Backup on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        source_machine=_machine_id(),
        platform=game.platform,
        content_digest=digest,
    )
    return BackupResult(
        success=True, game_id=game.id, version=version, files_changed=file_count,
        message=f"Backed up {file_count} file(s) ({_human_size(total_bytes)})",
    )


def _norm(path: object) -> str:
    """Case/separator-normalized absolute-path key for matching."""
    return os.path.normcase(os.path.normpath(str(path)))


def restore_custom(game: Game, backup_source_dir: Path) -> int:
    """Restore a custom backup tree to the game's configured save paths.

    Each mirrored root is written to a LOCALLY configured save path, never to
    the absolute path recorded in the manifest — so restores work across
    machines and a tampered manifest cannot redirect writes. Roots are matched
    to a target by exact original-path first (robust against reordering the
    --path list on the same machine), then by list position (cross-machine).

    Every root is resolved and type-checked BEFORE anything is written, so a
    config mismatch aborts with no partial restore. Directory roots are
    REPLACED, not merged: files on disk that were not in the backed-up version
    are removed, so a restore reproduces the selected version exactly. Returns
    the number of roots restored; raises RuntimeError on any problem.
    """
    manifest_path = backup_source_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError("Backup is missing gsg-custom.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unreadable custom backup manifest: {exc}") from exc

    local_by_path = {_norm(sp.path): sp.path for sp in game.save_paths}

    # Phase 1 — resolve + validate every root before touching disk.
    plan: list[tuple[Path, Path, str]] = []
    for meta in manifest.get("roots", []):
        if meta.get("type") == "missing":
            continue
        index = int(meta["index"])
        src = backup_source_dir / ROOTS_DIR / str(index)
        if not src.exists():
            continue
        original = str(meta.get("original", ""))
        matched = local_by_path.get(_norm(original))
        if matched is not None:
            target = matched
        elif index < len(game.save_paths):
            target = game.save_paths[index].path
        else:
            raise RuntimeError(
                f"'{game.id}' has no configured path matching backup root #{index} "
                f"({original}); re-add it with the same --path list before restoring"
            )
        root_type = str(meta.get("type", "dir"))
        if root_type == "file" and target.is_dir():
            raise RuntimeError(f"{target} is a directory but the backup stored a file")
        if root_type == "dir" and target.is_file():
            raise RuntimeError(f"{target} is a file but the backup stored a directory")
        plan.append((src, target, root_type))

    # Phase 2 — apply (safety backup already taken by the caller).
    restored = 0
    try:
        for src, target, root_type in plan:
            if root_type == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                stored = next((p for p in sorted(src.iterdir()) if p.is_file()), None)
                if stored is not None:
                    shutil.copy2(stored, target)
            else:
                _replace_dir(src, target)
            restored += 1
    except OSError as exc:
        raise RuntimeError(f"Restore failed writing to disk: {exc}") from exc
    return restored


def _replace_dir(src: Path, target: Path) -> None:
    """Make ``target``'s contents exactly match ``src`` (replace, not merge)."""
    target.mkdir(parents=True, exist_ok=True)
    keep = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    shutil.copytree(src, target, dirs_exist_ok=True)
    for path in list(target.rglob("*")):
        if path.is_file() and path.relative_to(target) not in keep:
            path.unlink()
    for directory in sorted(
        (p for p in target.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts), reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass  # not empty — keep it


def _machine_id() -> str:
    from .config import get_machine_id

    return get_machine_id()


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ["KiB", "MiB", "GiB", "TiB"]:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} TiB"
