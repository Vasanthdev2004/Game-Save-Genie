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
    source = inspect.getsource(cli.discover_new_games)
    assert "titles_without_save_data" in source
    assert "auto_add_skip_reason" in source


def test_the_scan_path_rechecks_ownership_of_tracked_games() -> None:
    """#48: ownership was decided once and never revisited, so an Epic game
    added while Epic had no manifest for it stayed tracked forever."""
    source = inspect.getsource(cli.discover_new_games)
    assert "for game in existing_games:" in source
    assert "syncs its own saves" in source


def test_the_recheck_only_reports() -> None:
    """Removing a tracked game would delete backups on a heuristic that has
    already proven fragile, and some people keep a deliberate second copy."""
    source = inspect.getsource(cli.discover_new_games)
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


# --- native cloud support (#51) --------------------------------------------
# Ludusavi's manifest records which stores sync a game's saves themselves.
# This is about what the GAME supports, never about what YOUR copy is covered
# by - a repack of a Steam Cloud game is synced by nothing.

_CLOUD_MANIFEST = """\
Cyberpunk 2077:
  cloud:
    epic: true
    gog: true
    steam: true
  files:
    "<winLocalAppData>/CD Projekt Red/Cyberpunk 2077":
      tags:
        - save
Half Cloud Game:
  cloud:
    steam: true
    gog: false
  files:
    "<home>/x":
      tags:
        - save
No Cloud Game:
  files:
    "<home>/y":
      tags:
        - save
Unknown Store Game:
  cloud:
    nintendo: true
  files:
    "<home>/z":
      tags:
        - save
Indent Trap Game:
  cloud:
    gog: true
  installDir:
    steam: true
  files:
    "<home>/w":
      tags:
        - save
"""


@pytest.fixture()
def cloud_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(_CLOUD_MANIFEST, encoding="utf-8")
    monkeypatch.setattr(
        "game_save_genie.ludusavi._ludusavi_manifest_path", lambda: path
    )
    return path


def test_every_true_store_is_reported(cloud_manifest: Path) -> None:
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    assert cloud_platforms_for_titles({"Cyberpunk 2077"}) == {
        "Cyberpunk 2077": {"epic", "gog", "steam"}
    }


def test_a_store_recorded_false_is_not_reported(cloud_manifest: Path) -> None:
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    assert cloud_platforms_for_titles({"Half Cloud Game"}) == {
        "Half Cloud Game": {"steam"}
    }


def test_a_game_with_no_cloud_block_is_absent_not_empty(cloud_manifest: Path) -> None:
    """Absent and empty must stay distinguishable: "we do not know" is not
    the same claim as "no store syncs this"."""
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    assert cloud_platforms_for_titles({"No Cloud Game"}) == {}


def test_an_unrecognised_store_is_ignored(cloud_manifest: Path) -> None:
    """Only stores we actually know about are reported, rather than passing
    through whatever the manifest happens to contain."""
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    assert cloud_platforms_for_titles({"Unknown Store Game"}) == {}


def test_the_cloud_block_stops_at_its_own_indent(cloud_manifest: Path) -> None:
    """A later key inside the same entry can carry a name that matches a
    store. Only the cloud block may be read, so this game is gog-only despite
    an installDir called steam."""
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    assert cloud_platforms_for_titles({"Indent Trap Game"}) == {
        "Indent Trap Game": {"gog"}
    }


def test_a_missing_manifest_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    monkeypatch.setattr(
        "game_save_genie.ludusavi._ludusavi_manifest_path",
        lambda: tmp_path / "nope.yaml",
    )
    assert cloud_platforms_for_titles({"Cyberpunk 2077"}) == {}


def test_no_titles_reads_no_manifest_for_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    from game_save_genie.ludusavi import cloud_platforms_for_titles

    def explode() -> Path:
        raise AssertionError("manifest read for an empty title set")

    monkeypatch.setattr("game_save_genie.ludusavi._ludusavi_manifest_path", explode)
    assert cloud_platforms_for_titles(set()) == {}


def test_scan_rejects_an_unknown_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently filter nothing.

    Checked with no games found, because that is where the original ordering
    was wrong: validation ran after the scan, so a machine with nothing
    installed returned 0 and never mentioned the bad flag. CI has no games,
    which is how this surfaced.
    """
    from typer.testing import CliRunner

    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", lambda p: Path("lud"))
    monkeypatch.setattr("game_save_genie.cli.scan_games", lambda p: {"games": {}})

    result = CliRunner().invoke(cli.app, ["scan", "--skip-cloud-synced", "nintendo"])
    assert result.exit_code == 1
    assert "Unknown store" in result.output


def test_a_bad_store_is_rejected_before_the_scan_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo should not cost a full Ludusavi scan first."""
    from typer.testing import CliRunner

    def explode(_: object) -> None:
        raise AssertionError("scanned before validating the flag")

    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", explode)
    result = CliRunner().invoke(cli.app, ["scan", "--skip-cloud-synced", "nintendo"])
    assert result.exit_code == 1


def _run_scan(
    monkeypatch: pytest.MonkeyPatch, manifest: Path, *args: str
) -> str:
    """Drive the real scan command over a controlled manifest and game list."""
    from typer.testing import CliRunner

    monkeypatch.setattr(
        "game_save_genie.ludusavi._ludusavi_manifest_path", lambda: manifest
    )
    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", lambda p: Path("lud"))
    monkeypatch.setattr(
        "game_save_genie.cli.scan_games",
        lambda p: {
            "games": {
                "Cyberpunk 2077": {"files": {"a": {"bytes": 1}}},
                "No Cloud Game": {"files": {"b": {"bytes": 1}}},
            }
        },
    )
    monkeypatch.setattr(
        "game_save_genie.launcher.get_all_launcher_games",
        lambda: (set(), set(), set()),
    )
    result = CliRunner().invoke(cli.app, ["scan", "--source", "all", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_a_cloud_synced_game_is_shown_when_you_do_not_ask_to_hide_it(
    cloud_manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter must never be a default. gsg exists for copies no store
    syncs - a repack of a Steam Cloud game - and hiding on capability would
    silently drop exactly those."""
    output = _run_scan(monkeypatch, cloud_manifest)
    assert "Cyberpunk 2077" in output
    assert "No Cloud Game" in output


def test_asking_to_hide_steam_hides_only_the_steam_synced_game(
    cloud_manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _run_scan(monkeypatch, cloud_manifest, "--skip-cloud-synced", "steam")
    assert "Cyberpunk 2077" not in output
    assert "No Cloud Game" in output


def test_hiding_a_store_the_game_does_not_use_hides_nothing(
    cloud_manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cyberpunk is on epic/gog/steam but not uplay."""
    output = _run_scan(monkeypatch, cloud_manifest, "--skip-cloud-synced", "uplay")
    assert "Cyberpunk 2077" in output


def test_native_cloud_is_reported_without_being_asked(
    cloud_manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Knowing Steam already covers a game is useful even when you want gsg
    to cover it too."""
    output = _run_scan(monkeypatch, cloud_manifest)
    assert "Native Cloud" in output
    assert "steam" in output
