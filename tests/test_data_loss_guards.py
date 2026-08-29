"""Guards on the paths that can destroy a save.

Each test names the issue it pins. These are deliberately behavioural rather
than structural: the audit that found these bugs also found that every earlier
fix was pinned at the helper and not at the call site, so a revert of the
production wiring left the suite green (#44).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_save_genie import custom
from game_save_genie.cloud import select_entries_to_prune
from game_save_genie.models import Game, GameSavePath, Platform

# Three versions written by a correctly-clocked machine, and one written by a
# machine six hours behind. The slow machine's id sorts oldest even though it
# is the most recent thing uploaded.
_ENTRIES = [
    ("20260829-130000-000000", "manifests/20260829-130000-000000.json"),
    ("20260829-120000-000000", "manifests/20260829-120000-000000.json"),
    ("20260829-110000-000000", "manifests/20260829-110000-000000.json"),
    ("20260829-070500-000000", "manifests/20260829-070500-000000.json"),
]
_SLOW_CLOCK_UPLOAD = "20260829-070500-000000"


def test_prune_would_delete_the_upload_without_the_guard() -> None:
    """The bug itself, kept as a test so the guard has something to guard."""
    doomed = [vid for vid, _ in select_entries_to_prune(_ENTRIES, keep=3)]
    assert doomed == [_SLOW_CLOCK_UPLOAD]


def test_prune_never_deletes_the_version_just_uploaded() -> None:
    doomed = select_entries_to_prune(_ENTRIES, keep=3, protect_id=_SLOW_CLOCK_UPLOAD)
    assert doomed == []


def test_protecting_one_version_does_not_spare_the_rest() -> None:
    """The guard must not turn into a general retention escape hatch."""
    doomed = [
        vid
        for vid, _ in select_entries_to_prune(_ENTRIES, keep=1, protect_id=_SLOW_CLOCK_UPLOAD)
    ]
    assert _SLOW_CLOCK_UPLOAD not in doomed
    assert "20260829-110000-000000" in doomed
    assert "20260829-120000-000000" in doomed


def test_upload_passes_the_new_version_as_protected() -> None:
    """Wiring, not behaviour. The guard exists; this asserts the production
    path actually uses it, which is the class of coverage #44 is about."""
    import inspect

    from game_save_genie import cli

    source = inspect.getsource(cli._cloud_upload)
    assert "prune_remote_versions" in source
    assert "protect_id=version.id" in source


# --- missing custom save roots (#37) ---------------------------------------


def _two_root_game(tmp_path: Path, second_exists: bool) -> tuple[Game, Path]:
    present = tmp_path / "root0"
    present.mkdir()
    (present / "slot.sav").write_bytes(b"x" * 100)
    second = tmp_path / "removable-drive"
    if second_exists:
        second.mkdir()
        (second / "other.sav").write_bytes(b"y" * 50)
    game = Game(
        id="two-root", title="Two Root", platform=Platform.WINDOWS, custom=True,
        save_paths=[GameSavePath(path=present), GameSavePath(path=second)],
    )
    return game, tmp_path / "backups"


def test_a_backup_covering_every_root_reports_nothing_missing(tmp_path: Path) -> None:
    game, backup_dir = _two_root_game(tmp_path, second_exists=True)
    result = custom.backup_custom(game, backup_dir)
    assert result.success
    assert result.missing_roots == []
    assert "missing" not in result.message


def test_an_unreachable_root_is_named_rather_than_recorded_silently(
    tmp_path: Path,
) -> None:
    """It still succeeds - the other root is worth keeping - but the message
    has to say the backup covered less than was asked for, because retention
    will eventually prune the last version that did cover it."""
    game, backup_dir = _two_root_game(tmp_path, second_exists=False)
    result = custom.backup_custom(game, backup_dir)
    assert result.success
    assert result.missing_roots == [str(tmp_path / "removable-drive")]
    assert "missing" in result.message.lower()


def test_the_missing_root_is_logged_as_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Under `gsg auto --install` the console is hidden, so the log line is
    the only durable record."""
    game, backup_dir = _two_root_game(tmp_path, second_exists=False)
    with caplog.at_level("WARNING"):
        custom.backup_custom(game, backup_dir)
    assert any("removable-drive" in r.getMessage() for r in caplog.records)


# --- concurrent backups (#38) ----------------------------------------------


def test_a_failed_snapshot_records_no_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storing the row anyway left local_path aliasing the shared live
    directory, so several versions all restored the newest content."""
    from game_save_genie import cli
    from game_save_genie.config import load_config
    from game_save_genie.database import Database

    game, backup_dir = _two_root_game(tmp_path, second_exists=True)
    config = load_config(tmp_path / "config.yaml")
    config.backup_dir = backup_dir
    db = Database(tmp_path / "versions.db")

    def boom(version: object, cfg: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_snapshot_version", boom)
    result = cli._run_backup(game, config, db, None)

    assert not result.success
    assert "Snapshot failed" in result.message
    assert db.get_versions(game.id) == []


def test_every_backup_goes_through_the_cross_process_guard() -> None:
    """Wiring. Guarding _run_backup rather than each command is what stops a
    future caller forgetting; assert that is where the guard lives."""
    import inspect

    from game_save_genie import cli

    assert "_backup_guard" in inspect.getsource(cli._run_backup)


def test_the_guard_is_a_noop_for_a_daemon_that_already_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gsg auto` holds the instance lock for its whole run. Re-acquiring it
    per backup would make the watcher warn about itself every time."""
    from game_save_genie import cli

    monkeypatch.setitem(cli._LOCK_STATE, "held", True)

    def explode() -> None:
        raise AssertionError("tried to re-acquire a lock this process holds")

    monkeypatch.setattr(cli, "_acquire_instance_lock", explode)
    with cli._backup_guard("backup"):
        pass
