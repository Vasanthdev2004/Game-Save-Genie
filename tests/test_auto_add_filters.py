"""What gsg declines to start tracking, and what it re-checks.

Both filters exist because a real install ended up tracking two games it
should not have: Roblox, whose only known paths are settings, and an Epic
title that was not installed at the moment of the first scan.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from game_save_genie import cli
from game_save_genie.ludusavi import titles_without_save_data

_MANIFEST = """\
Roblox:
  files:
    "<winLocalAppData>/Roblox/GlobalBasicSettings_13.xml":
      tags:
        - config
      when:
        - os: windows
  id:
    lutris: roblox
Hogwarts Legacy:
  files:
    "<winLocalAppData>/Hogwarts Legacy/Saved/Config/WindowsNoEditor":
      tags:
        - config
      when:
        - os: windows
    "<winLocalAppData>/Hogwarts Legacy/Saved/SaveGames/<storeUserId>":
      tags:
        - save
      when:
        - os: windows
Untagged Game:
  files:
    "<winLocalAppData>/Untagged/whatever":
      when:
        - os: windows
No Files Game:
  steam:
    id: 1
"""


@pytest.fixture()
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(_MANIFEST, encoding="utf-8")
    monkeypatch.setattr(
        "game_save_genie.ludusavi._ludusavi_manifest_path", lambda: path
    )
    return path


def test_a_config_only_game_is_identified(manifest: Path) -> None:
    """Roblox: two files, both tagged config, because progress is on the
    account. Ten versions of a settings file is not a backup."""
    assert titles_without_save_data({"Roblox"}) == {"Roblox"}


def test_a_game_with_any_save_path_is_kept(manifest: Path) -> None:
    """Hogwarts Legacy has both config and save paths. One save tag is
    enough - the config paths alongside it are irrelevant."""
    assert titles_without_save_data({"Hogwarts Legacy"}) == set()


def test_an_untagged_entry_is_never_skipped(manifest: Path) -> None:
    """Absence of tags is not evidence of absence of saves. This filter may
    only decline what we positively know is config-only."""
    assert titles_without_save_data({"Untagged Game"}) == set()


def test_an_entry_with_no_files_is_never_skipped(manifest: Path) -> None:
    assert titles_without_save_data({"No Files Game"}) == set()


def test_an_unknown_title_is_never_skipped(manifest: Path) -> None:
    assert titles_without_save_data({"Some Game Not In The Manifest"}) == set()


def test_a_missing_manifest_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the manifest cannot be read, behave exactly as before the filter
    existed rather than withholding backups."""
    monkeypatch.setattr(
        "game_save_genie.ludusavi._ludusavi_manifest_path",
        lambda: tmp_path / "does-not-exist.yaml",
    )
    assert titles_without_save_data({"Roblox"}) == set()


def test_no_titles_reads_no_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan path should not pay for a half-million-line read when there
    is nothing to ask about."""
    def explode() -> Path:
        raise AssertionError("manifest read for an empty title set")

    monkeypatch.setattr("game_save_genie.ludusavi._ludusavi_manifest_path", explode)
    assert titles_without_save_data(set()) == set()


def test_the_scan_path_applies_the_config_only_filter() -> None:
    """Wiring: the helper exists and auto-add actually consults it."""
    source = inspect.getsource(cli.auto)
    assert "titles_without_save_data" in source
    assert "auto_add_skip_reason" in source


def test_the_scan_path_rechecks_ownership_of_tracked_games() -> None:
    """#48: ownership was decided once and never revisited, so an Epic game
    added while Epic had no manifest for it stayed tracked forever."""
    source = inspect.getsource(cli.auto)
    assert "for game in existing_games:" in source
    assert "syncs its own saves" in source


def test_the_recheck_only_reports() -> None:
    """Removing a tracked game would delete backups on a heuristic that has
    already proven fragile, and some people keep a deliberate second copy."""
    source = inspect.getsource(cli.auto)
    recheck = source.split("for game in existing_games:", 1)[1].split("if new_games:", 1)[0]
    assert "remove" not in recheck.replace("'gsg remove", "").replace("gsg remove", "")


# --- the decision itself, not its wiring -----------------------------------


def test_a_launcher_managed_game_is_skipped() -> None:
    assert cli.auto_add_skip_reason("Anything", "steam", set()) == "managed by steam"
    assert cli.auto_add_skip_reason("Anything", "epic", set()) == "managed by epic"


def test_a_config_only_game_is_skipped() -> None:
    reason = cli.auto_add_skip_reason("Roblox", "other", {"Roblox"})
    assert reason == "no save data of its own"


def test_an_ordinary_game_is_tracked() -> None:
    assert cli.auto_add_skip_reason("Cyberpunk 2077", "other", {"Roblox"}) is None


def test_a_config_only_title_owned_by_a_launcher_reports_the_launcher() -> None:
    """Both reasons apply; the launcher one is the more useful thing to say."""
    assert cli.auto_add_skip_reason("Roblox", "steam", {"Roblox"}) == "managed by steam"
