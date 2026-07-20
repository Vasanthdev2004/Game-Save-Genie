"""Tests for custom-path (arbitrary directory) backup and restore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_save_genie import custom
from game_save_genie.models import Game, GameSavePath, Platform


def _game(tmp_path: Path, *roots: Path) -> Game:
    return Game(
        id="retroarch",
        title="RetroArch",
        platform=Platform.WINDOWS,
        save_paths=[GameSavePath(path=r) for r in roots],
        custom=True,
    )


def _make_saves(root: Path) -> None:
    (root / "saves").mkdir(parents=True)
    (root / "saves" / "game.srm").write_bytes(b"battery-save")
    (root / "states").mkdir()
    (root / "states" / "game.state").write_bytes(b"savestate-blob")


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "retroarch"
    _make_saves(src)
    game = _game(tmp_path, src)
    backup_dir = tmp_path / "backups"

    result = custom.backup_custom(game, backup_dir, label="test")
    assert result.success and result.version is not None
    assert result.version.file_count == 2
    assert result.version.content_digest

    # Wipe the live saves, then restore.
    (src / "saves" / "game.srm").write_bytes(b"CORRUPTED")
    (src / "states" / "game.state").unlink()

    restored = custom.restore_custom(game, backup_dir / game.id)
    assert restored == 1
    assert (src / "saves" / "game.srm").read_bytes() == b"battery-save"
    assert (src / "states" / "game.state").read_bytes() == b"savestate-blob"


def test_change_detection_skips_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "ra"
    _make_saves(src)
    game = _game(tmp_path, src)
    backup_dir = tmp_path / "backups"

    first = custom.backup_custom(game, backup_dir, previous_digest=None)
    assert first.version is not None
    digest = first.version.content_digest

    # Nothing changed -> no new version.
    again = custom.backup_custom(game, backup_dir, previous_digest=digest)
    assert again.success and again.version is None
    assert "No changes" in again.message

    # Change one file -> new version with a different digest.
    (src / "saves" / "game.srm").write_bytes(b"new-progress")
    changed = custom.backup_custom(game, backup_dir, previous_digest=digest)
    assert changed.version is not None
    assert changed.version.content_digest != digest


def test_multiple_roots_restore_to_configured_paths(tmp_path: Path) -> None:
    saves = tmp_path / "saves_dir"
    saves.mkdir()
    (saves / "a.srm").write_bytes(b"aaa")
    cfg = tmp_path / "retroarch.cfg"
    cfg.write_text("config", encoding="utf-8")
    game = _game(tmp_path, saves, cfg)
    backup_dir = tmp_path / "backups"

    result = custom.backup_custom(game, backup_dir)
    assert result.version is not None and result.version.file_count == 2

    saves_content = (saves / "a.srm").read_bytes()
    (saves / "a.srm").unlink()
    cfg.unlink()

    custom.restore_custom(game, backup_dir / game.id)
    assert (saves / "a.srm").read_bytes() == saves_content
    assert cfg.read_text(encoding="utf-8") == "config"


def test_restore_uses_local_paths_not_manifest(tmp_path: Path) -> None:
    """Cross-machine + anti-traversal: restore targets the LOCAL configured
    path, ignoring whatever absolute path the manifest recorded."""
    src = tmp_path / "machineA" / "saves"
    src.mkdir(parents=True)
    (src / "s.srm").write_bytes(b"data")
    backup_dir = tmp_path / "backups"
    game_a = _game(tmp_path, src)
    custom.backup_custom(game_a, backup_dir)

    # Tamper: point the manifest's "original" at a sensitive absolute path.
    manifest_path = backup_dir / game_a.id / custom.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["roots"][0]["original"] = "C:/Windows/System32/evil"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Restore on "machine B" whose configured path is different and safe.
    dest = tmp_path / "machineB" / "saves"
    game_b = _game(tmp_path, dest)
    custom.restore_custom(game_b, backup_dir / game_a.id)

    assert (dest / "s.srm").read_bytes() == b"data"
    assert not Path("C:/Windows/System32/evil").exists()


def test_restore_missing_configured_path_raises(tmp_path: Path) -> None:
    src = tmp_path / "saves"
    src.mkdir()
    (src / "s.srm").write_bytes(b"x")
    backup_dir = tmp_path / "backups"
    game = _game(tmp_path, src)
    custom.backup_custom(game, backup_dir)

    # Restoring game has NO configured paths -> index 0 is out of range.
    bare = Game(id="retroarch", title="RetroArch", platform=Platform.WINDOWS, custom=True)
    with pytest.raises(RuntimeError, match="no configured path"):
        custom.restore_custom(bare, backup_dir / game.id)


def test_backup_no_files_returns_no_version(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    game = _game(tmp_path, empty)
    result = custom.backup_custom(game, tmp_path / "backups")
    assert result.success and result.version is None
    assert "No save files" in result.message


def test_restore_replaces_directory_removing_stale_files(tmp_path: Path) -> None:
    """A rollback must reproduce the version exactly — files created after the
    backup must NOT survive the restore (replace, not merge)."""
    src = tmp_path / "saves"
    src.mkdir()
    (src / "slot1.sav").write_bytes(b"v1")
    game = _game(tmp_path, src)
    backup_dir = tmp_path / "backups"
    custom.backup_custom(game, backup_dir)  # backup captures {slot1}

    # Game later corrupts slot1 and creates slot2; roll back to the backup.
    (src / "slot1.sav").write_bytes(b"CORRUPT")
    (src / "slot2.sav").write_bytes(b"NEW-AFTER-BACKUP")

    custom.restore_custom(game, backup_dir / game.id)
    assert (src / "slot1.sav").read_bytes() == b"v1"
    assert not (src / "slot2.sav").exists()  # stale file removed


def test_restore_matches_by_path_after_reorder(tmp_path: Path) -> None:
    """Reordering the --path list on the same machine must not misroute a
    restore — roots are matched by their original path, not list position."""
    a = tmp_path / "dirA"
    b = tmp_path / "dirB"
    a.mkdir()
    b.mkdir()
    (a / "a.sav").write_bytes(b"AAA")
    (b / "b.sav").write_bytes(b"BBB")
    backup_dir = tmp_path / "backups"
    custom.backup_custom(_game(tmp_path, a, b), backup_dir)  # roots 0=A, 1=B

    # Wipe, then restore with the paths listed in the OPPOSITE order.
    (a / "a.sav").unlink()
    (b / "b.sav").unlink()
    reordered = _game(tmp_path, b, a)  # now index 0=B, 1=A
    custom.restore_custom(reordered, backup_dir / "retroarch")

    assert (a / "a.sav").read_bytes() == b"AAA"  # A-data landed in A, not B
    assert (b / "b.sav").read_bytes() == b"BBB"


def test_restore_type_mismatch_aborts_without_writing(tmp_path: Path) -> None:
    """A file-vs-directory mismatch aborts before any root is applied."""
    f = tmp_path / "save.dat"
    f.write_bytes(b"data")
    game = _game(tmp_path, f)  # file-type root
    backup_dir = tmp_path / "backups"
    custom.backup_custom(game, backup_dir)

    # On restore the configured target is now a DIRECTORY.
    f.unlink()
    f.mkdir()
    (f / "sentinel").write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="directory but the backup stored a file"):
        custom.restore_custom(game, backup_dir / game.id)
    assert (f / "sentinel").exists()  # nothing was written/destroyed


def test_backup_no_paths_configured_fails(tmp_path: Path) -> None:
    game = Game(id="x", title="X", platform=Platform.WINDOWS, custom=True)
    result = custom.backup_custom(game, tmp_path / "backups")
    assert not result.success
    assert "No --path" in result.message
