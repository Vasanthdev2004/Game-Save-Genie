"""Guards on the paths that can destroy a save.

Each test names the issue it pins. These are deliberately behavioural rather
than structural: the audit that found these bugs also found that every earlier
fix was pinned at the helper and not at the call site, so a revert of the
production wiring left the suite green (#44).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_save_genie import custom
from game_save_genie.cloud import select_entries_to_prune
from game_save_genie.config import load_config
from game_save_genie.database import Database
from game_save_genie.models import (
    BackupResult,
    Game,
    GameSavePath,
    Platform,
    SaveVersion,
    SyncConfig,
)

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


# --- silent failures (#41) -------------------------------------------------


def test_a_game_with_no_save_files_is_a_failure_not_a_no_op() -> None:
    """"No changes detected" and "I found nothing to back up" are different
    outcomes. Sharing a message told a user whose saves had moved that
    everything was fine, every session, indefinitely."""
    import json
    from unittest.mock import patch

    from game_save_genie.ludusavi import backup_game

    game = Game(id="g", title="Gone Game", platform=Platform.WINDOWS,
                shop="steam", shop_object_id="1")
    empty_scan = type("R", (), {"stdout": json.dumps({"games": {"Gone Game": {"files": {}}}})})()
    with patch("game_save_genie.ludusavi.run_ludusavi", return_value=empty_scan):
        result = backup_game(Path("ludusavi"), game, Path("backups"))
    assert not result.success
    assert "no save files found" in result.message.lower()


def test_a_callback_failure_is_reported_not_only_logged() -> None:
    """The loop must survive, but a failed close-backup used to leave the tray
    on the green "Playing" state with only a stack trace in a log file."""
    from game_save_genie.watcher import GameWatcher

    watcher = GameWatcher([])
    seen: list[str] = []
    watcher.set_on_error(seen.append)

    def boom() -> None:
        raise RuntimeError("upload exploded")

    watcher._safe_callback(boom)
    assert seen and "upload exploded" in seen[0]


def test_a_watcher_without_a_reporter_still_survives_a_failing_callback() -> None:
    """on_error is optional; its absence must not turn a swallowed error into
    a crash in the loop that runs unattended from boot."""
    from game_save_genie.watcher import GameWatcher

    def boom() -> None:
        raise RuntimeError("nope")

    GameWatcher([])._safe_callback(boom)  # must not raise


def test_gsg_auto_reports_watcher_errors_to_the_tray() -> None:
    """Wiring: the reporter has to be attached, not merely available."""
    import inspect

    from game_save_genie import cli

    source = inspect.getsource(cli.auto)
    assert "set_on_error" in source
    assert "STATE_ERROR" in source


# --- discovery and first backup (#54, #55) ---------------------------------


def test_a_periodic_task_does_not_run_immediately() -> None:
    """The caller has just scanned at startup; running again on the first
    tick would repeat an expensive Ludusavi scan for nothing."""
    from game_save_genie.watcher import GameWatcher

    watcher = GameWatcher([])
    calls: list[int] = []
    watcher.set_periodic_task(3600.0, lambda: calls.append(1))
    watcher._run_periodic_task()
    assert calls == []


def test_a_periodic_task_runs_once_its_interval_has_passed() -> None:
    from game_save_genie.watcher import GameWatcher

    watcher = GameWatcher([])
    calls: list[int] = []
    watcher.set_periodic_task(3600.0, lambda: calls.append(1))
    watcher._periodic_task_last = 0.0  # pretend an hour went by
    watcher._run_periodic_task()
    assert calls == [1]


def test_a_periodic_task_that_throws_does_not_kill_the_loop() -> None:
    """It runs on the watcher thread that must survive unattended."""
    from game_save_genie.watcher import GameWatcher

    watcher = GameWatcher([])

    def boom() -> None:
        raise RuntimeError("scan exploded")

    watcher.set_periodic_task(1.0, boom)
    watcher._periodic_task_last = 0.0
    watcher._run_periodic_task()  # must not raise


def test_a_failing_periodic_task_does_not_retry_every_tick() -> None:
    """Stamping after a throw would re-run an expensive scan on every 5s
    poll for as long as it kept failing."""
    from game_save_genie.watcher import GameWatcher

    watcher = GameWatcher([])
    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("nope")

    watcher.set_periodic_task(3600.0, boom)
    watcher._periodic_task_last = 0.0
    watcher._run_periodic_task()
    watcher._run_periodic_task()
    assert calls == [1]


def test_no_periodic_task_is_a_noop() -> None:
    from game_save_genie.watcher import GameWatcher

    GameWatcher([])._run_periodic_task()


def test_newly_discovered_games_start_being_watched() -> None:
    """A rescan that finds a game is pointless if the loop never watches it."""
    from game_save_genie.watcher import GameWatcher

    first = Game(id="a", title="A", platform=Platform.WINDOWS)
    second = Game(id="b", title="B", platform=Platform.WINDOWS)
    watcher = GameWatcher([first])
    assert watcher.add_games([first, second]) == ["b"]
    assert set(watcher.games) == {"a", "b"}


def test_re_adding_a_running_game_does_not_disturb_it() -> None:
    """Replacing the entry would drop its pid set, and the next tick would
    read that as the game closing - firing a spurious backup."""
    from game_save_genie.watcher import GameWatcher

    game = Game(id="a", title="A", platform=Platform.WINDOWS)
    watcher = GameWatcher([game])
    watcher._running["a"] = {123}
    watcher.add_games([Game(id="a", title="A renamed", platform=Platform.WINDOWS)])
    assert watcher._running["a"] == {123}
    assert watcher.games["a"].title == "A"


def test_the_watch_loop_rescans_for_new_games() -> None:
    """#54: discovery happened once, before the loop."""
    import inspect

    from game_save_genie import cli

    source = inspect.getsource(cli.auto)
    assert "set_periodic_task" in source
    assert "discover_new_games" in source


def _fresh(tmp_path: Path) -> tuple[SyncConfig, Database]:
    config = load_config(tmp_path / "config.yaml")
    config.backup_dir = tmp_path / "backups"
    return config, Database(tmp_path / "versions.db")


def test_a_game_with_no_versions_is_backed_up_not_merely_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#55. It used to print advice into a hidden console and do nothing."""
    from game_save_genie import cli

    config, db = _fresh(tmp_path)
    game = Game(id="a", title="A", platform=Platform.WINDOWS)
    backed: list[str] = []
    uploaded: list[str] = []

    def fake_backup(g, cfg, database, lud, label=None, **kw):  # type: ignore[no-untyped-def]
        backed.append(g.id)
        version = SaveVersion(
            id="20260101-000000-000000", game_id=g.id,
            created_at=datetime.now(timezone.utc), local_path=tmp_path / "v.zip",
            size_bytes=1, file_count=1, platform=Platform.WINDOWS,
        )
        return BackupResult(success=True, game_id=g.id, version=version, message="ok")

    monkeypatch.setattr(cli, "_run_backup", fake_backup)
    def fake_upload(*args: object, **kwargs: object) -> bool:
        uploaded.append("x")
        return True

    monkeypatch.setattr(cli, "_cloud_upload", fake_upload)
    still = cli.protect_unbacked_games(
        [game], config, tmp_path / "config.yaml", db, Path("lud"), lambda *a: None
    )
    assert backed == ["a"]
    assert uploaded == ["x"]
    assert still == []


def test_a_game_that_already_has_versions_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from game_save_genie import cli

    config, db = _fresh(tmp_path)
    game = Game(id="a", title="A", platform=Platform.WINDOWS)
    db.add_version(SaveVersion(
        id="20260101-000000-000000", game_id="a",
        created_at=datetime.now(timezone.utc), local_path=tmp_path / "v.zip",
        size_bytes=1, file_count=1, platform=Platform.WINDOWS,
    ))

    def explode(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("backed up a game that already had versions")

    monkeypatch.setattr(cli, "_run_backup", explode)
    assert cli.protect_unbacked_games(
        [game], config, tmp_path / "config.yaml", db, Path("lud"), lambda *a: None
    ) == []


def test_one_failing_first_backup_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher must still start, and the other games must still be
    protected."""
    from game_save_genie import cli

    config, db = _fresh(tmp_path)
    games = [
        Game(id="a", title="A", platform=Platform.WINDOWS),
        Game(id="b", title="B", platform=Platform.WINDOWS),
    ]
    seen: list[str] = []

    def flaky(g, *a, **k):  # type: ignore[no-untyped-def]
        seen.append(g.id)
        if g.id == "a":
            raise RuntimeError("ludusavi exploded")
        return BackupResult(success=True, game_id=g.id, message="ok")

    monkeypatch.setattr(cli, "_run_backup", flaky)
    still = cli.protect_unbacked_games(
        games, config, tmp_path / "config.yaml", db, Path("lud"), lambda *a: None
    )
    assert seen == ["a", "b"]
    assert "A" in still


def test_the_watch_loop_actually_runs_the_periodic_task() -> None:
    """#54. Registering the task is useless if the loop never calls it.

    The loop is stopped by a timer rather than by the task itself. Letting the
    task do it means that if the loop never calls the task, the loop never
    exits - so a regression hangs the suite instead of failing it. That is not
    hypothetical: it happened while writing this, and wedged a mutation run
    for twenty minutes.
    """
    import threading

    from game_save_genie.watcher import GameWatcher

    watcher = GameWatcher([])
    calls: list[int] = []
    watcher.set_periodic_task(0.001, lambda: calls.append(1))
    watcher._periodic_task_last = 0.0

    threading.Timer(2.0, watcher.stop).start()
    watcher.watch_loop(interval=0.01)
    assert calls, "the watch loop never ran its periodic task"


def test_auto_registers_the_rescan_when_the_interval_is_positive() -> None:
    from game_save_genie.models import SyncConfig

    assert SyncConfig(backup_dir=Path("x")).rescan_interval_hours > 0
