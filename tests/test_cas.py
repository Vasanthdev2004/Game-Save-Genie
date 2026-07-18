"""Tests for content-addressed storage (manifest/blob) core logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from game_save_genie import cas
from game_save_genie.archive import sha256_file


def _make_backup_tree(root: Path) -> None:
    """A realistic Ludusavi backup tree: mapping.yaml + mirrored drive files."""
    (root / "Test Game" / "drive-C" / "Users" / "vasan" / "saves").mkdir(parents=True)
    (root / "Test Game" / "mapping.yaml").write_text("name: Test Game\n", encoding="utf-8")
    (root / "Test Game" / "drive-C" / "Users" / "vasan" / "saves" / "slot1.sav").write_bytes(
        b"\x00\x01\x02" * 1000
    )
    (root / "Test Game" / "drive-C" / "Users" / "vasan" / "saves" / "slot2.sav").write_bytes(
        b"\xaa\xbb" * 500
    )


def test_manifest_lists_every_file_with_hash(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    _make_backup_tree(root)
    manifest = cas.build_manifest(
        root, version_id="v1", game_id="test-game", created_at="t", source_machine="pc"
    )
    paths = {f["path"] for f in manifest["files"]}
    assert paths == {
        "Test Game/mapping.yaml",
        "Test Game/drive-C/Users/vasan/saves/slot1.sav",
        "Test Game/drive-C/Users/vasan/saves/slot2.sav",
    }
    # Paths are posix even on Windows.
    assert all("\\" not in p for p in paths)
    assert manifest["format"] == cas.MANIFEST_FORMAT


def test_round_trip_is_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    _make_backup_tree(root)
    manifest = cas.build_manifest(
        root, version_id="v1", game_id="g", created_at="t", source_machine=None
    )
    stage = tmp_path / "stage"
    cas.stage_blobs(root, manifest, stage)
    dest = tmp_path / "restored"
    cas.reconstruct(manifest, stage, dest)

    for entry in manifest["files"]:
        original = root / Path(entry["path"])
        restored = dest / Path(entry["path"])
        assert restored.read_bytes() == original.read_bytes()
        assert sha256_file(restored) == entry["sha256"]


def test_identical_files_stage_one_blob(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.sav").write_bytes(b"same-content")
    (root / "a" / "y.sav").write_bytes(b"same-content")  # duplicate content
    (root / "a" / "z.sav").write_bytes(b"different")
    manifest = cas.build_manifest(
        root, version_id="v", game_id="g", created_at="t", source_machine=None
    )
    stage = tmp_path / "stage"
    staged = cas.stage_blobs(root, manifest, stage)
    assert staged == 2  # two distinct contents despite three files
    assert len(manifest["files"]) == 3


def test_incremental_reuses_unchanged_blobs(tmp_path: Path) -> None:
    """The whole point: a second version only adds blobs for changed files."""
    root = tmp_path / "backup"
    _make_backup_tree(root)
    m1 = cas.build_manifest(root, version_id="v1", game_id="g", created_at="t", source_machine=None)
    stage = tmp_path / "cloudblobs"  # simulates the accumulated remote blob store
    cas.stage_blobs(root, m1, stage)
    blobs_after_v1 = {p for p in stage.rglob("*") if p.is_file()}

    # One save slot changes; everything else identical.
    (root / "Test Game" / "drive-C" / "Users" / "vasan" / "saves" / "slot1.sav").write_bytes(
        b"NEW-PROGRESS"
    )
    m2 = cas.build_manifest(root, version_id="v2", game_id="g", created_at="t", source_machine=None)
    cas.stage_blobs(root, m2, stage)
    blobs_after_v2 = {p for p in stage.rglob("*") if p.is_file()}

    # Exactly one new blob added for the changed slot; the rest reused.
    assert len(blobs_after_v2) == len(blobs_after_v1) + 1


def test_reconstruct_rejects_missing_blob(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    _make_backup_tree(root)
    manifest = cas.build_manifest(root, version_id="v", game_id="g", created_at="t", source_machine=None)
    empty_blobs = tmp_path / "empty"
    empty_blobs.mkdir()
    with pytest.raises(RuntimeError, match="Missing blob"):
        cas.reconstruct(manifest, empty_blobs, tmp_path / "out")


def test_reconstruct_rejects_corrupt_blob(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    _make_backup_tree(root)
    manifest = cas.build_manifest(root, version_id="v", game_id="g", created_at="t", source_machine=None)
    stage = tmp_path / "stage"
    cas.stage_blobs(root, manifest, stage)
    # Corrupt one staged blob in place.
    a_blob = next(p for p in stage.rglob("*") if p.is_file())
    a_blob.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="failed its hash check"):
        cas.reconstruct(manifest, stage, tmp_path / "out")


@pytest.mark.parametrize(
    "evil_path",
    [
        "../escape.sav",
        "../../escape.sav",
        "C:/Windows/System32/evil.dll",
        "/etc/passwd",
        "sub/../../escape.sav",
    ],
)
def test_reconstruct_rejects_path_traversal(tmp_path: Path, evil_path: str) -> None:
    """A tampered manifest must not write outside the destination directory."""
    manifest = {
        "format": cas.MANIFEST_FORMAT,
        "files": [{"path": evil_path, "sha256": "0" * 64, "size": 0}],
    }
    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError, match="Unsafe archive member|escapes destination"):
        cas.reconstruct(manifest, tmp_path / "blobs", dest)
    assert not (tmp_path / "escape.sav").exists()


def test_referenced_blob_keys_union(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    (root / "d").mkdir(parents=True)
    (root / "d" / "keep.sav").write_bytes(b"keep")
    m_keep = cas.build_manifest(root, version_id="v2", game_id="g", created_at="t", source_machine=None)
    (root / "d" / "old.sav").write_bytes(b"old-only")
    (root / "d" / "keep.sav").unlink()
    m_old = cas.build_manifest(root, version_id="v1", game_id="g", created_at="t", source_machine=None)

    keep_keys = cas.manifest_blob_keys(m_keep)
    old_keys = cas.manifest_blob_keys(m_old)
    # A blob only in the old manifest is not referenced once that manifest is gone.
    orphaned = old_keys - cas.referenced_blob_keys([m_keep])
    assert orphaned  # the 'old-only' blob would be GC'd
    assert cas.referenced_blob_keys([m_keep, m_old]) == keep_keys | old_keys
