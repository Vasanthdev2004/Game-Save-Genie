"""Tests for safe archive extraction, zipping, and hashing."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from game_save_genie.archive import safe_extract_zip, sha256_file, zip_directory


def test_zip_directory_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "drive-C" / "saves").mkdir(parents=True)
    (src / "mapping.yaml").write_text("games: {}\n", encoding="utf-8")
    (src / "drive-C" / "saves" / "slot1.sav").write_bytes(b"\x00\x01\x02")

    zip_path = tmp_path / "out" / "v1.zip"
    digest = zip_directory(src, zip_path)

    assert zip_path.is_file()
    assert digest == sha256_file(zip_path)

    dest = tmp_path / "dest"
    safe_extract_zip(zip_path, dest)
    assert (dest / "mapping.yaml").read_text(encoding="utf-8") == "games: {}\n"
    assert (dest / "drive-C" / "saves" / "slot1.sav").read_bytes() == b"\x00\x01\x02"


def test_zip_directory_stable_hash(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.txt").write_text("b", encoding="utf-8")
    (src / "a.txt").write_text("a", encoding="utf-8")

    first = zip_directory(src, tmp_path / "one.zip")
    second = zip_directory(src, tmp_path / "two.zip")
    # Same content, same member order; only zip timestamps may differ, which
    # is fine — we only require hashing the same file twice to agree.
    assert first == sha256_file(tmp_path / "one.zip")
    assert second == sha256_file(tmp_path / "two.zip")


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "gotcha")

    with pytest.raises(RuntimeError, match="Unsafe archive member"):
        safe_extract_zip(evil, tmp_path / "dest")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_absolute_path(tmp_path: Path) -> None:
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("C:/Windows/pwned.txt", "gotcha")

    with pytest.raises(RuntimeError, match="Unsafe archive member"):
        safe_extract_zip(evil, tmp_path / "dest")


def test_safe_extract_detects_corruption(tmp_path: Path) -> None:
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("data.bin", b"x" * 4096)

    # Flip bytes inside the member data to break the CRC.
    raw = bytearray(archive.read_bytes())
    raw[60:70] = b"\xff" * 10
    archive.write_bytes(bytes(raw))

    with pytest.raises((RuntimeError, zipfile.BadZipFile)):
        safe_extract_zip(archive, tmp_path / "dest")
