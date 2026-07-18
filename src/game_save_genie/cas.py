"""Content-addressed storage for space-efficient cloud backups.

Instead of uploading a full zip per version, each file is stored once under
its SHA-256 (``blobs/<hh>/<hash>``) and each version is a small JSON manifest
listing the files it contains. A new backup only uploads files whose content
is not already in the cloud, so an unchanged save slot is never re-sent.

This module is pure (hashing, manifest build/parse, blob staging and
reconstruction) so it can be tested without rclone or a network.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .archive import sha256_file, validate_member_path

MANIFEST_FORMAT = "gsg-cas-1"


def blob_key(digest: str) -> str:
    """Sharded remote/local key for a blob, e.g. ``ab/abcd...``."""
    return f"{digest[:2]}/{digest}"


def build_manifest(
    root: Path,
    *,
    version_id: str,
    game_id: str,
    created_at: str,
    source_machine: str | None,
) -> dict[str, Any]:
    """Hash every file under ``root`` and return a version manifest.

    Paths are stored relative to ``root`` with forward slashes so a manifest
    written on one OS reconstructs correctly on another.
    """
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
    return {
        "format": MANIFEST_FORMAT,
        "version_id": version_id,
        "game_id": game_id,
        "created_at": created_at,
        "source_machine": source_machine,
        "files": files,
    }


def manifest_blob_keys(manifest: dict[str, Any]) -> set[str]:
    """The set of distinct blob keys referenced by one manifest."""
    return {blob_key(str(f["sha256"])) for f in manifest.get("files", [])}


def referenced_blob_keys(manifests: list[dict[str, Any]]) -> set[str]:
    """Union of blob keys referenced by all given manifests (for GC)."""
    keys: set[str] = set()
    for manifest in manifests:
        keys |= manifest_blob_keys(manifest)
    return keys


def stage_blobs(root: Path, manifest: dict[str, Any], stage_dir: Path) -> int:
    """Materialize each referenced blob under ``stage_dir`` as ``<hh>/<hash>``.

    Files are hard-linked when possible (no data duplication on the same
    volume) and copied otherwise. Duplicate content is staged once. Returns
    the number of distinct blobs staged.
    """
    staged: set[str] = set()
    for entry in manifest.get("files", []):
        digest = str(entry["sha256"])
        if digest in staged:
            continue
        src = root / Path(str(entry["path"]))
        dst = stage_dir / blob_key(digest)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            try:
                dst.hardlink_to(src)
            except (OSError, NotImplementedError):
                shutil.copy2(src, dst)
        staged.add(digest)
    return len(staged)


def reconstruct(manifest: dict[str, Any], blob_dir: Path, dest_dir: Path) -> None:
    """Rebuild the backup tree under ``dest_dir`` from downloaded blobs.

    Every file is verified against its manifest hash; a missing blob or a
    hash mismatch raises RuntimeError so a corrupt or incomplete download is
    never handed to the restore step.
    """
    for entry in manifest.get("files", []):
        # A manifest from a shared/untrusted bucket must not be able to write
        # outside dest_dir (absolute, drive-rooted, or ../ paths).
        validate_member_path(str(entry["path"]), dest_dir)
        digest = str(entry["sha256"])
        blob = blob_dir / blob_key(digest)
        if not blob.is_file():
            raise RuntimeError(f"Missing blob {digest} for {entry['path']}")
        if sha256_file(blob) != digest:
            raise RuntimeError(f"Blob {digest} failed its hash check")
        dst = dest_dir / Path(str(entry["path"]))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob, dst)
