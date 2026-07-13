"""Tests for path remapping."""

from pathlib import Path

from game_save_genie.models import Game, Platform
from game_save_genie.remap import remap_paths, _remap_single_path


def test_windows_user_profile_remap() -> None:
    path = Path("C:/Users/OldUser/Saved Games/Game/save.sav")
    remapped = _remap_single_path(path, Platform.WINDOWS, Game(id="g", title="Game", platform=Platform.WINDOWS))
    assert "OldUser" not in str(remapped)
    assert "Saved Games" in str(remapped)


def test_wine_to_windows_remap() -> None:
    mapping = {
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
