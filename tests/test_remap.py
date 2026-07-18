"""Tests for path remapping."""

from pathlib import Path
from typing import Any

import yaml

from game_save_genie.models import Game, Platform
from game_save_genie.remap import (
    _remap_single_path,
    apply_remap_to_staged_backup,
    remap_paths,
    remap_windows_user_path,
)


def test_user_path_remap_basic() -> None:
    assert (
        remap_windows_user_path("C:/Users/alice/Saved Games/x.sav", "bob")
        == "C:/Users/bob/Saved Games/x.sav"
    )


def test_user_path_remap_backslashes_normalized() -> None:
    assert (
        remap_windows_user_path(r"C:\Users\alice\AppData\Roaming\game", "bob")
        == "C:/Users/bob/AppData/Roaming/game"
    )


def test_user_path_remap_noop_cases() -> None:
    # Same user (case-insensitive), shared profiles, non-profile paths.
    assert remap_windows_user_path("C:/Users/Bob/x", "bob") == "C:/Users/Bob/x"
    assert remap_windows_user_path("C:/Users/Public/x", "bob") == "C:/Users/Public/x"
    assert remap_windows_user_path("C:/Users/Default/x", "bob") == "C:/Users/Default/x"
    assert remap_windows_user_path("D:/Games/Skyrim/saves", "bob") == "D:/Games/Skyrim/saves"
    assert remap_windows_user_path("/home/alice/.config/game", "bob") == "/home/alice/.config/game"


def test_user_path_remap_only_touches_profile_segment() -> None:
    # A deeper 'users' directory must not be rewritten.
    assert (
        remap_windows_user_path("C:/Games/users/data.sav", "bob")
        == "C:/Games/users/data.sav"
    )
    assert (
        remap_windows_user_path("C:/Users/alice/game/users/slot1", "bob")
        == "C:/Users/bob/game/users/slot1"
    )


def _make_staged_backup(root: Path, user: str = "olduser") -> Path:
    """Build a synthetic staged Ludusavi backup matching the real layout."""
    game_dir = root / "Test Game"
    save_rel = Path("drive-C") / "Users" / user / "Saved Games" / "slot1.sav"
    (game_dir / save_rel.parent).mkdir(parents=True)
    (game_dir / save_rel).write_bytes(b"savedata")
    mapping = {
        "name": "Test Game",
        "drives": {"drive-C": "C:"},
        "backups": [
            {
                "name": ".",
                "when": "2026-07-13T09:37:44Z",
                "os": "windows",
                "files": {
                    f"C:/Users/{user}/Saved Games/slot1.sav": {"hash": "abc", "size": 8},
                },
                "registry": {"hash": None},
                "children": [],
            }
        ],
    }
    with (game_dir / "mapping.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(mapping, f, sort_keys=False)
    return game_dir


def test_staged_backup_remap_moves_files_and_rewrites_keys(tmp_path: Path) -> None:
    game_dir = _make_staged_backup(tmp_path)

    count = apply_remap_to_staged_backup(game_dir, current_user="newuser")

    assert count == 1
    moved = game_dir / "drive-C" / "Users" / "newuser" / "Saved Games" / "slot1.sav"
    assert moved.read_bytes() == b"savedata"
    assert not (game_dir / "drive-C" / "Users" / "olduser").exists()  # pruned
    with (game_dir / "mapping.yaml").open(encoding="utf-8") as f:
        mapping = yaml.safe_load(f)
    files = mapping["backups"][0]["files"]
    assert list(files) == ["C:/Users/newuser/Saved Games/slot1.sav"]
    assert files["C:/Users/newuser/Saved Games/slot1.sav"]["hash"] == "abc"


def test_staged_backup_remap_noop_for_same_user(tmp_path: Path) -> None:
    game_dir = _make_staged_backup(tmp_path, user="sameuser")
    before = (game_dir / "mapping.yaml").read_text(encoding="utf-8")

    assert apply_remap_to_staged_backup(game_dir, current_user="sameuser") == 0
    assert (game_dir / "mapping.yaml").read_text(encoding="utf-8") == before
    assert (
        game_dir / "drive-C" / "Users" / "sameuser" / "Saved Games" / "slot1.sav"
    ).exists()


def test_staged_backup_remap_keeps_key_on_collision(tmp_path: Path) -> None:
    game_dir = _make_staged_backup(tmp_path)
    # A file already exists where the remapped save would land.
    blocker = game_dir / "drive-C" / "Users" / "newuser" / "Saved Games" / "slot1.sav"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"already-here")

    count = apply_remap_to_staged_backup(game_dir, current_user="newuser")

    assert count == 0
    # Original key and file are intact; the backup stays self-consistent.
    assert (
        game_dir / "drive-C" / "Users" / "olduser" / "Saved Games" / "slot1.sav"
    ).read_bytes() == b"savedata"
    with (game_dir / "mapping.yaml").open(encoding="utf-8") as f:
        mapping = yaml.safe_load(f)
    assert list(mapping["backups"][0]["files"]) == ["C:/Users/olduser/Saved Games/slot1.sav"]


def test_staged_backup_remap_missing_mapping_returns_zero(tmp_path: Path) -> None:
    assert apply_remap_to_staged_backup(tmp_path / "nope", current_user="x") == 0


def test_staged_backup_remap_named_backup_folder(tmp_path: Path) -> None:
    """Retention layouts store each backup under backup-<ts>/; the stored
    file must be resolved and moved inside that folder, not the game root."""
    game_dir = tmp_path / "Test Game"
    backup_name = "backup-20260701T000000Z"
    rel = Path(backup_name) / "drive-C" / "Users" / "olduser" / "Saved Games" / "s.sav"
    (game_dir / rel.parent).mkdir(parents=True)
    (game_dir / rel).write_bytes(b"x")
    mapping = {
        "name": "Test Game",
        "drives": {"drive-C": "C:"},
        "backups": [
            {
                "name": backup_name,
                "when": "2026-07-01T00:00:00Z",
                "os": "windows",
                "files": {"C:/Users/olduser/Saved Games/s.sav": {"hash": "h", "size": 1}},
                "registry": {"hash": None},
                "children": [],
            }
        ],
    }
    with (game_dir / "mapping.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(mapping, f, sort_keys=False)

    assert apply_remap_to_staged_backup(game_dir, current_user="newuser") == 1
    moved = game_dir / backup_name / "drive-C" / "Users" / "newuser" / "Saved Games" / "s.sav"
    assert moved.read_bytes() == b"x"
    with (game_dir / "mapping.yaml").open(encoding="utf-8") as f:
        remapped = yaml.safe_load(f)
    assert list(remapped["backups"][0]["files"]) == ["C:/Users/newuser/Saved Games/s.sav"]


def test_staged_backup_remap_recurses_into_children(tmp_path: Path) -> None:
    """Differential backups nest under children with their own folders."""
    game_dir = tmp_path / "Test Game"
    child_name = "backup-20260702T000000Z-diff"
    for name in (".", child_name):
        base = game_dir if name == "." else game_dir / name
        rel = Path("drive-C") / "Users" / "olduser" / "s.sav"
        (base / rel.parent).mkdir(parents=True)
        (base / rel).write_bytes(b"x")
    mapping = {
        "name": "Test Game",
        "drives": {"drive-C": "C:"},
        "backups": [
            {
                "name": ".",
                "files": {"C:/Users/olduser/s.sav": {"hash": "h1", "size": 1}},
                "children": [
                    {
                        "name": child_name,
                        "files": {"C:/Users/olduser/s.sav": {"hash": "h2", "size": 1}},
                        "children": [],
                    }
                ],
            }
        ],
    }
    with (game_dir / "mapping.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(mapping, f, sort_keys=False)

    assert apply_remap_to_staged_backup(game_dir, current_user="newuser") == 2
    assert (game_dir / "drive-C" / "Users" / "newuser" / "s.sav").exists()
    assert (game_dir / child_name / "drive-C" / "Users" / "newuser" / "s.sav").exists()
    with (game_dir / "mapping.yaml").open(encoding="utf-8") as f:
        remapped = yaml.safe_load(f)
    child = remapped["backups"][0]["children"][0]
    assert list(child["files"]) == ["C:/Users/newuser/s.sav"]


def test_staged_backup_remap_refuses_zip_format(tmp_path: Path) -> None:
    """Zip-format backups cannot be remapped safely; must raise, not half-rewrite."""
    import pytest

    game_dir = tmp_path / "Test Game"
    game_dir.mkdir(parents=True)
    mapping = {
        "name": "Test Game",
        "drives": {"drive-C": "C:"},
        "backups": [
            {
                "name": "backup-20260701T000000Z.zip",
                "files": {"C:/Users/olduser/s.sav": {"hash": "h", "size": 1}},
                "children": [],
            }
        ],
    }
    with (game_dir / "mapping.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(mapping, f, sort_keys=False)

    with pytest.raises(RuntimeError, match="zip format"):
        apply_remap_to_staged_backup(game_dir, current_user="newuser")
    # Same-user zip backups need no remapping and must not raise.
    assert apply_remap_to_staged_backup(game_dir, current_user="olduser") == 0


def test_staged_backup_remap_missing_stored_file_keeps_key(tmp_path: Path) -> None:
    game_dir = tmp_path / "Test Game"
    game_dir.mkdir(parents=True)
    mapping = {
        "name": "Test Game",
        "drives": {"drive-C": "C:"},
        "backups": [
            {
                "name": ".",
                "files": {"C:/Users/olduser/ghost.sav": {"hash": "h", "size": 1}},
                "children": [],
            }
        ],
    }
    with (game_dir / "mapping.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(mapping, f, sort_keys=False)

    assert apply_remap_to_staged_backup(game_dir, current_user="newuser") == 0
    with (game_dir / "mapping.yaml").open(encoding="utf-8") as f:
        remapped = yaml.safe_load(f)
    assert list(remapped["backups"][0]["files"]) == ["C:/Users/olduser/ghost.sav"]


def test_staged_backup_remap_converging_keys_keep_original(tmp_path: Path) -> None:
    """Keys from two old users mapping to one new path must not merge."""
    game_dir = tmp_path / "Test Game"
    for user in ("alice", "carol"):
        rel = Path("drive-C") / "Users" / user / "s.sav"
        (game_dir / rel.parent).mkdir(parents=True)
        (game_dir / rel).write_bytes(user.encode())
    mapping = {
        "name": "Test Game",
        "drives": {"drive-C": "C:"},
        "backups": [
            {
                "name": ".",
                "files": {
                    "C:/Users/alice/s.sav": {"hash": "ha", "size": 5},
                    "C:/Users/carol/s.sav": {"hash": "hc", "size": 5},
                },
                "children": [],
            }
        ],
    }
    with (game_dir / "mapping.yaml").open("w", encoding="utf-8") as f:
        yaml.dump(mapping, f, sort_keys=False)

    count = apply_remap_to_staged_backup(game_dir, current_user="bob")

    with (game_dir / "mapping.yaml").open(encoding="utf-8") as f:
        remapped = yaml.safe_load(f)
    files = remapped["backups"][0]["files"]
    # Exactly one converges; the other keeps its original key and file.
    assert count == 1
    assert len(files) == 2
    assert "C:/Users/bob/s.sav" in files
    assert (game_dir / "drive-C" / "Users" / "bob" / "s.sav").exists()


def test_windows_user_profile_remap() -> None:
    path = Path("C:/Users/OldUser/Saved Games/Game/save.sav")
    remapped = _remap_single_path(path, Platform.WINDOWS, Game(id="g", title="Game", platform=Platform.WINDOWS))
    assert "OldUser" not in str(remapped)
    assert "Saved Games" in str(remapped)


def test_wine_to_windows_remap() -> None:
    mapping: dict[str, Any] = {
        "games": {
            "Game": {
                "files": {
                    "/home/olduser/.steam/steam/steamapps/compatdata/123/pfx/drive_c/users/olduser/Saved Games/Game/save.sav": {}
                }
            }
        }
    }
    game = Game(id="g", title="Game", platform=Platform.LINUX)
    remapped = remap_paths(game, mapping, target_platform=Platform.WINDOWS)
    assert len(remapped) == 1
    assert str(remapped[0].path).startswith("C:")
