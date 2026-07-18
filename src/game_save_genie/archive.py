"""Archive helpers: safe extraction, directory zipping, and hashing.

All zip/tar extraction in the project goes through these helpers so that
archive members can never escape their destination directory (path
traversal via absolute paths or ``..`` components).
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _validate_member_name(name: str, dest: Path) -> None:
    member = Path(name)
    if member.is_absolute() or ".." in member.parts:
        raise RuntimeError(f"Unsafe archive member path: {name}")
    if not _is_within(dest, dest / member):
        raise RuntimeError(f"Archive member escapes destination: {name}")


def safe_extract_zip(archive: Path, dest: Path) -> None:
    """Extract a zip archive, rejecting members that escape ``dest``.

    Also verifies archive integrity (CRC) before extracting, so a truncated
    or corrupted download fails cleanly instead of half-applying.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise RuntimeError(f"Corrupt archive {archive.name}: bad CRC for {bad}")
        for info in zf.infolist():
            _validate_member_name(info.filename, dest)
        zf.extractall(dest)


def safe_extract_tar_gz(archive: Path, dest: Path) -> None:
    """Extract a .tar.gz archive, rejecting members that escape ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            # Python < 3.12 without the filter backport: validate manually.
            for member in tf.getmembers():
                _validate_member_name(member.name, dest)
                if member.issym() or member.islnk():
                    raise RuntimeError(f"Unsafe link member in archive: {member.name}")
            tf.extractall(dest)


def zip_directory(src_dir: Path, zip_path: Path) -> str:
    """Zip a directory's contents and return the archive's SHA-256 hex digest.

    Archive names are relative to ``src_dir``. The parent of ``zip_path`` is
    created if needed. Files are added in sorted order so identical content
    produces a stable layout.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in sorted(src_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(src_dir))
    return sha256_file(zip_path)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, streamed in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
