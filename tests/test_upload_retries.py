"""Durable offline recovery, truthful health, and writer ownership."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from game_save_genie import cli, tray
from game_save_genie.config import save_config, save_games
from game_save_genie.database import Database
from game_save_genie.health import describe_health
from game_save_genie.models import (
    CloudProvider,
    CloudSyncResult,
    Game,
    GameSavePath,
    Platform,
    SaveVersion,
    SyncConfig,
)
from game_save_genie.watcher import GameWatcher


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, SyncConfig, Game, Database]:
    monkeypatch.setattr(cli, "get_data_dir", lambda: tmp_path / "data")
    cfg = tmp_path / "config.yaml"
    config = SyncConfig(backup_dir=tmp_path / "backups", cloud_provider=CloudProvider.LOCAL,
                        rclone_remote_name="test", auto_scan=False, max_versions=1)
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "slot.sav").write_bytes(b"level 1")
    game = Game(id="my-game", title="My Game", platform=Platform.WINDOWS, custom=True,
                executable_names=["my-game.exe"], save_paths=[GameSavePath(path=saves)])
    save_config(config, cfg)
    save_games([game], cfg)
    monkeypatch.setattr(cli, "get_rclone_path", lambda *a: Path("rclone"))
    monkeypatch.setattr(cli, "prune_remote_versions", lambda *a, **kw: None)
    return cfg, config, game, Database(tmp_path / "data" / "versions.db")


def _backup(
    config: SyncConfig, game: Game, db: Database, *, queue_upload: bool = True,
) -> SaveVersion:
    result = cli._run_backup(game, config, db, None, queue_upload=queue_upload)
    assert result.success and result.version is not None, result.message
    return result.version


def _upload_ok(*args: object, **kwargs: object) -> CloudSyncResult:
    game, version = args[1:3]
    assert isinstance(game, Game) and isinstance(version, SaveVersion)
    return CloudSyncResult(success=True, direction="upload", message="ok",
                           remote_path=f"test:game-save-genie/{game.id}/manifests/{version.id}.json")


def test_upload_recovers_after_restart_without_new_save(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    version = _backup(config, game, db)
    clock = [1000.0]
    monkeypatch.setattr("game_save_genie.cli.time.time", lambda: clock[0])

    def offline(*args: object, **kwargs: object) -> CloudSyncResult:
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(cli, "upload_save_cas", offline)
    assert not cli._cloud_upload(cfg, game, version, dry_run=False)
    db = Database(db.db_path)  # Reopen, retaining no in-memory retry state.
    job = db.get_upload_jobs()[0]
    assert (job.attempts, job.next_attempt_at, job.last_error) == (1, 1060, "network unavailable")
    assert describe_health(game, config, db).state == "Waiting to upload"
    assert cli._run_backup(game, config, db, None).version is None
    assert cli.retry_pending_uploads(cfg) == (0, 0)  # Backoff respected.
    clock[0] = 1060
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    assert cli.retry_pending_uploads(cfg) == (1, 0)
    assert cli.retry_pending_uploads(cfg) == (0, 0)  # Completion is idempotent.
    assert db.get_upload_jobs() == []
    saved = db.get_version(version.id)
    assert saved and saved.cloud_synced_at is not None
    health = describe_health(game, config, db)
    assert health.state == "Protected" and health.last_upload is not None


def test_backoff_is_capped_and_enqueue_cannot_reset_or_redirect_it(
    setup: tuple[Path, SyncConfig, Game, Database],
) -> None:
    _, config, game, db = setup
    version = _backup(config, game, db)
    for attempt in range(10):
        db.fail_upload(version.id, "offline", 1000)
        job = Database(db.db_path).get_upload_jobs()[0]
        assert job.next_attempt_at == 1000 + min(3600, 60 * 2 ** attempt)
    db.enqueue_upload(version.id, "new-remote", "new-root")
    assert db.get_upload_jobs()[0] == job


def test_no_cloud_and_safety_backups_never_upload_automatically(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    version = _backup(config, game, db, queue_upload=False)
    (game.save_paths[0].path / "slot.sav").write_bytes(b"before restore")
    safety = cli._run_backup(game, config, db, None, origin="safety")
    assert safety.version and safety.version.origin == "safety"
    db.enqueue_upload(safety.version.id, "test", config.remote_root)
    assert db.get_upload_jobs() == []
    assert cli.retry_pending_uploads(cfg) == (0, 0)
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    assert cli.retry_pending_uploads(cfg, manual=True) == (1, 0)
    saved = db.get_version(version.id)
    assert saved and saved.cloud_synced
    assert not db.get_version(safety.version.id).cloud_synced  # type: ignore[union-attr]


@pytest.mark.parametrize("disabled", ["paused", "sync_disabled", "removed", "new_remote", "new_root"])
def test_automatic_retry_respects_current_user_choices(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, disabled: str,
) -> None:
    cfg, config, game, db = setup
    _backup(config, game, db)
    if disabled == "paused":
        game.auto_sync = False
    elif disabled == "sync_disabled":
        game.sync_enabled = False
    elif disabled == "new_remote":
        config.rclone_remote_name = "other"
    elif disabled == "new_root":
        config.remote_root = "other"
    save_games([] if disabled == "removed" else [game], cfg)
    save_config(config, cfg)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Retried against the user's current settings")

    monkeypatch.setattr(cli, "upload_save_cas", forbidden)
    assert cli.retry_pending_uploads(cfg) == (0, 0)
    assert len(db.get_upload_jobs()) == 1
    if disabled.startswith("new_"):
        assert describe_health(game, config, db).state == "Needs attention"


def test_retention_keeps_queued_snapshots_until_they_upload(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    first = _backup(config, game, db)
    (game.save_paths[0].path / "slot.sav").write_bytes(b"level 2")
    second = _backup(config, game, db)
    assert len(db.get_versions(game.id)) == 2  # max_versions=1, but neither may be lost.
    assert first.local_path.exists() and second.local_path.exists()
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    assert cli.retry_pending_uploads(cfg) == (2, 0)
    assert not first.local_path.exists()
    assert second.local_path.exists()
    assert [v.id for v in db.get_versions(game.id)] == [second.id]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_damaged_snapshot_never_reaches_cloud(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    cfg, config, game, db = setup
    version = _backup(config, game, db)
    if damage == "missing":
        version.local_path.unlink()
    else:
        version.local_path.write_bytes(b"corrupt")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Uploaded a damaged snapshot")

    monkeypatch.setattr(cli, "upload_save_cas", forbidden)
    assert cli.retry_pending_uploads(cfg) == (0, 1)
    assert db.get_upload_jobs()[0].last_error
    assert not db.get_version(version.id).cloud_synced  # type: ignore[union-attr]


def test_health_records_backup_failure_and_recovers_on_success(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, game, db = setup
    original = cli._snapshot_version

    def disk_full(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_snapshot_version", disk_full)
    assert not cli._run_backup(game, config, db, None).success
    health = describe_health(game, config, Database(db.db_path))
    assert health.state == "Needs attention" and "disk full" in health.detail
    monkeypatch.setattr(cli, "_snapshot_version", original)
    _backup(config, game, db)
    assert db.get_backup_issue(game.id) is None


def test_health_does_not_claim_safety_only_or_legacy_saves_are_uploaded(
    setup: tuple[Path, SyncConfig, Game, Database],
) -> None:
    _, config, game, db = setup
    result = cli._run_backup(game, config, db, None, origin="safety")
    assert result.version
    health = describe_health(game, config, db)
    assert health.state == "Needs attention" and health.last_backup is None
    db.delete_version(result.version.id)
    version = _backup(config, game, db, queue_upload=False)
    assert describe_health(game, config, db).state == "Local only"
    db.mark_cloud_synced(version.id, f"other:{config.remote_root}/{game.id}/snapshot.zip")
    health = describe_health(game, config, db)
    assert health.state == "Local only" and health.last_upload is None


def test_database_deletion_removes_pending_jobs_and_issue(
    setup: tuple[Path, SyncConfig, Game, Database],
) -> None:
    _, config, game, db = setup
    version = _backup(config, game, db)
    db.delete_version(version.id)
    assert db.get_upload_jobs() == []
    _backup(config, game, db)
    db.set_backup_issue(game.id, "failure")
    db.delete_game(game.id)
    assert db.get_upload_jobs() == [] and db.get_backup_issue(game.id) is None


def test_writer_is_held_through_snapshot_and_registration(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, game, db = setup
    snapshot = cli._snapshot_version
    registered = db.add_version

    def check_snapshot(version: SaveVersion, cfg: SyncConfig) -> None:
        assert cli._acquire_file_lock(db.db_path.parent / "writer.lock") is None
        snapshot(version, cfg)

    def check_register(version: SaveVersion, upload_target: tuple[str, str] | None = None) -> None:
        assert cli._acquire_file_lock(db.db_path.parent / "writer.lock") is None
        registered(version, upload_target)

    monkeypatch.setattr(cli, "_snapshot_version", check_snapshot)
    monkeypatch.setattr(db, "add_version", check_register)
    _backup(config, game, db)


def test_busy_writer_changes_nothing_and_releases_after_exception(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, game, db = setup
    monkeypatch.setattr(cli, "_WRITER_WAIT_SECONDS", 0.01)
    results = []
    with pytest.raises(RuntimeError, match="abort"), cli._backup_guard("restore"):
        worker = threading.Thread(target=lambda: results.append(cli._run_backup(game, config, db, None)))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert not results[0].success and "busy" in results[0].message
        assert db.get_versions(game.id) == [] and not config.backup_dir.exists()
        raise RuntimeError("abort")
    _backup(config, game, db)  # Exception did not strand the lock.


def test_other_process_can_write_with_idle_daemon_but_not_active_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "get_data_dir", lambda: tmp_path)
    script = (
        "import sys; from pathlib import Path; from game_save_genie import cli; "
        "lock = cli._acquire_file_lock(Path(sys.argv[1])); "
        "sys.exit(0 if lock is not None else 7)"
    )

    def probe() -> int:
        return subprocess.run([sys.executable, "-c", script, str(tmp_path / "writer.lock")],
                              timeout=20, check=False, capture_output=True).returncode

    daemon = cli._acquire_instance_lock()
    assert daemon is not None
    with daemon:
        assert probe() == 0
        with cli._backup_guard("backup"):
            assert probe() == 7
        assert probe() == 0


def test_restore_owns_writer_lock_during_staging_safety_and_apply(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    version = _backup(config, game, db, queue_upload=False)
    save = game.save_paths[0].path / "slot.sav"
    save.write_bytes(b"unsaved progress")
    materialize, apply = cli._materialize_version, cli._apply_staged_backup
    seen: list[str] = []

    def stage(v: SaveVersion, g: Game) -> Path | None:
        assert cli._acquire_file_lock(db.db_path.parent / "writer.lock") is None
        seen.append("staging")
        return materialize(v, g)

    def restore(g: Game, source: Path, ludusavi: Path | None) -> None:
        assert cli._acquire_file_lock(db.db_path.parent / "writer.lock") is None
        assert any(v.origin == "safety" for v in db.get_versions(game.id))
        seen.append("apply")
        apply(g, source, ludusavi)

    monkeypatch.setattr(cli, "_materialize_version", stage)
    monkeypatch.setattr(cli, "_apply_staged_backup", restore)
    ok, message = cli.restore_local_version(game, version, config, db, cfg, force=True)
    assert ok, message
    assert seen == ["staging", "apply"] and save.read_bytes() == b"level 1"


class HealthTray(tray.NullTray):
    state = ""

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state


def test_tray_uses_worst_game_health_not_last_success(
    setup: tuple[Path, SyncConfig, Game, Database],
) -> None:
    _, config, game, db = setup
    version = _backup(config, game, db)
    db.mark_cloud_synced(version.id, f"test:{config.remote_root}/{game.id}/snapshot.zip")
    broken = game.model_copy(update={"id": "broken", "title": "Broken"})
    db.set_backup_issue(broken.id, "disk full")
    icon = HealthTray()
    cli._update_tray_health(icon, [broken, game], config, db)
    assert icon.state == tray.STATE_ERROR
    cli._update_tray_health(icon, [game], config, db)
    assert icon.state == tray.STATE_OK


@pytest.mark.parametrize("scan", [False, True])
def test_auto_retries_with_periodic_backups_disabled_and_protects_rescan(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, scan: bool,
) -> None:
    cfg, config, game, db = setup
    config.auto_scan = scan
    config.rescan_interval_hours = 1
    save_config(config, cfg)
    _backup(config, game, db)  # An upload queued before the daemon starts.
    found = game.model_copy(update={"id": "new-game", "title": "New Game"})
    scans: list[bool] = []

    def discover(*args: object, quiet: bool = False) -> list[Game]:
        scans.append(quiet)
        if not quiet:
            return []
        save_games([game, found], cfg)
        return [found]

    monkeypatch.setattr(cli, "discover_new_games", discover)
    monkeypatch.setattr(cli, "get_ludusavi_path", lambda *a: Path("ludusavi"))
    monkeypatch.setattr(cli, "_auto_restore_if_idle", lambda *a: None)
    monkeypatch.setattr(cli, "notify", lambda *a: None)
    monkeypatch.setattr(cli, "setup_file_logging", lambda *a: None)
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    monkeypatch.setattr(GameWatcher, "prime", lambda self: None)
    clock = [100.0]
    monkeypatch.setattr("game_save_genie.cli.time.monotonic", lambda: clock[0])

    def watch_loop(self: GameWatcher, interval: float = 5.0) -> None:
        clock[0] += 3601
        self._periodic_task_last = 0.0
        self._run_periodic_task()
        assert db.get_upload_jobs() == []
        assert describe_health(game, config, db).state == "Protected"
        if scan:
            assert found.id in self.games
            assert db.get_versions(found.id)  # Protected before it ever runs/closes.

    monkeypatch.setattr(GameWatcher, "watch_loop", watch_loop)
    result = CliRunner().invoke(cli.app, ["--config", str(cfg), "auto", "--no-tray", "--periodic", "0"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert scans == ([False, True] if scan else [])


def test_retry_command_uploads_local_only_on_explicit_request(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    _backup(config, game, db, queue_upload=False)
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    result = CliRunner().invoke(cli.app, ["--config", str(cfg), "retry", game.id])
    assert result.exit_code == 0, result.output
    assert "Uploaded 1" in result.output and "Protected" in result.output


def test_unchanged_partial_backup_keeps_missing_root_warning(
    setup: tuple[Path, SyncConfig, Game, Database],
) -> None:
    _, config, game, db = setup
    game.save_paths.append(GameSavePath(path=config.backup_dir.parent / "missing"))
    _backup(config, game, db)
    result = cli._run_backup(game, config, db, None)
    assert result.success and result.version is None
    health = describe_health(game, config, db)
    assert health.state == "Needs attention" and "Missing save paths" in health.detail


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_backup_rebuilds_damaged_snapshot_even_with_unchanged_source(
    setup: tuple[Path, SyncConfig, Game, Database], damage: str,
) -> None:
    _, config, game, db = setup
    version = _backup(config, game, db, queue_upload=False)
    if damage == "missing":
        version.local_path.unlink()
    else:
        version.local_path.write_bytes(b"corrupt")
    repaired = _backup(config, game, db, queue_upload=False)
    assert repaired.id != version.id and repaired.local_path.exists()


def test_rclone_dry_run_must_not_clear_queue_or_run_retention(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    _backup(config, game, db)
    config.custom_rclone_args = ["--dry-run"]
    save_config(config, cfg)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A simulated transfer was treated as a real one")

    monkeypatch.setattr(cli, "upload_save_cas", forbidden)
    monkeypatch.setattr(cli, "prune_remote_versions", forbidden)
    assert cli.retry_pending_uploads(cfg) == (0, 1)
    assert "dry-run" in (db.get_upload_jobs()[0].last_error or "")


def test_auto_can_protect_local_saves_when_rclone_is_unavailable(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup

    def offline(*args: object) -> Path:
        raise OSError("offline, binary cannot download")

    monkeypatch.setattr(cli, "get_rclone_path", offline)
    monkeypatch.setattr(cli, "get_ludusavi_path", offline)  # Custom-only must never need it.
    monkeypatch.setattr(cli, "setup_file_logging", lambda *a: None)
    monkeypatch.setattr(cli, "notify", lambda *a: None)
    monkeypatch.setattr(GameWatcher, "prime", lambda self: None)
    monkeypatch.setattr(GameWatcher, "watch_loop", lambda self, **kw: None)
    result = CliRunner().invoke(cli.app, ["--config", str(cfg), "auto", "--no-tray"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert db.get_versions(game.id) and db.get_upload_jobs()
    assert describe_health(game, config, db).state == "Waiting to upload"


def test_busy_close_event_is_retried_after_restart_without_another_play_session(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    initial = _backup(config, game, db)
    db.mark_cloud_synced(initial.id, f"test:{config.remote_root}/{game.id}/snapshot.zip")
    (game.save_paths[0].path / "slot.sav").write_bytes(b"progress at game close")
    monkeypatch.setattr(cli, "_WRITER_WAIT_SECONDS", 0.01)
    watcher = GameWatcher([game])
    watcher._running = {game.id: {123}}
    monkeypatch.setattr(watcher, "_scan_running", dict)
    monkeypatch.setattr(watcher, "_first_process_info", lambda pids: None)
    results = []
    watcher.set_on_game_close(lambda g, process: results.append(
        cli._run_backup(g, config, db, None, label="close event", origin="auto")
    ))
    with cli._backup_guard("slow upload"):
        worker = threading.Thread(target=watcher.tick)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
    watcher.tick()
    assert len(results) == 1 and not results[0].success  # Close event was consumed.
    db = Database(db.db_path)
    assert db.get_deferred_backups() == [(game.id, "close event")]
    assert describe_health(game, config, db).state == "Needs attention"
    assert cli.retry_deferred_backups(cfg) == (1, 0)
    assert db.get_deferred_backups() == [] and db.get_backup_issue(game.id) is None
    latest = db.get_versions(game.id)[0]
    assert latest.id != initial.id and latest.origin == "auto"
    with zipfile.ZipFile(latest.local_path) as archive:
        save_name = next(name for name in archive.namelist() if name.endswith("slot.sav"))
        assert archive.read(save_name) == b"progress at game close"


@pytest.mark.parametrize("state", ["paused", "sync_disabled", "removed"])
def test_deferred_backup_respects_current_game_state(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    cfg, _, game, db = setup
    db.defer_backup(game.id, "close event", "Writer busy")
    if state == "paused":
        game.auto_sync = False
    elif state == "sync_disabled":
        game.sync_enabled = False
    save_games([] if state == "removed" else [game], cfg)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Backed up a paused or removed game")

    monkeypatch.setattr(cli, "_run_backup", forbidden)
    assert cli.retry_deferred_backups(cfg) == (0, 0)
    assert db.get_deferred_backups() == [(game.id, "close event")]


def test_deferred_request_survives_failure_and_safety_backup_until_regular_success(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    db.defer_backup(game.id, "close event", "Writer busy")
    assert cli._run_backup(game, config, db, None, origin="safety").success
    assert db.get_deferred_backups()
    original = cli._snapshot_version

    def disk_full(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_snapshot_version", disk_full)
    assert cli.retry_deferred_backups(cfg) == (0, 1)
    assert db.get_deferred_backups() and "disk full" in (db.get_backup_issue(game.id) or "")
    monkeypatch.setattr(cli, "_snapshot_version", original)
    assert cli.retry_deferred_backups(cfg) == (1, 0)
    assert db.get_deferred_backups() == [] and db.get_backup_issue(game.id) is None


def test_pending_close_backup_prevents_idle_cloud_restore(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, game, db = setup
    db.defer_backup(game.id, "close event", "Writer busy")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Checked/applied cloud state before securing pending local progress")

    monkeypatch.setattr(cli, "_cloud_newer_version", forbidden)
    cli._auto_restore_if_idle(game, config, db, Path("rclone"), None)
    db.delete_game(game.id)
    assert db.get_deferred_backups() == []


@pytest.mark.parametrize("change", ["paused", "removed", "new_remote"])
def test_retry_rechecks_game_choices_between_slow_transfers(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    cfg, config, first, db = setup
    second = first.model_copy(update={"id": "second-game", "title": "Second Game"})
    save_games([first, second], cfg)
    _backup(config, first, db)
    _backup(config, second, db)
    uploaded: list[str] = []

    def transfer(*args: object, **kwargs: object) -> CloudSyncResult:
        game = args[1]
        assert isinstance(game, Game)
        uploaded.append(game.id)
        if game.id == first.id:
            if change == "paused":
                second.auto_sync = False
            elif change == "new_remote":
                second.remote_path = "other"
            save_games([first] if change == "removed" else [first, second], cfg)
        return _upload_ok(*args, **kwargs)

    monkeypatch.setattr(cli, "upload_save_cas", transfer)
    assert cli.retry_pending_uploads(cfg) == (1, 0)
    assert uploaded == [first.id]
    assert len(db.get_upload_jobs(second.id)) == 1


@pytest.mark.parametrize("command", ["auto", "watch"])
def test_watcher_maintenance_replays_deferred_backup_before_upload(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    cfg, config, game, db = setup
    _backup(config, game, db)
    monkeypatch.setattr(cli, "_auto_restore_if_idle", lambda *a: None)
    monkeypatch.setattr(cli, "notify", lambda *a: None)
    monkeypatch.setattr(cli, "setup_file_logging", lambda *a: None)
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    monkeypatch.setattr(GameWatcher, "prime", lambda self: None)

    def watch_loop(self: GameWatcher, interval: float = 5.0) -> None:
        (game.save_paths[0].path / "slot.sav").write_bytes(b"deferred progress")
        db.defer_backup(game.id, "close event", "Writer busy")
        self._periodic_task_last = 0.0
        self._run_periodic_task()
        assert db.get_deferred_backups() == [] and db.get_upload_jobs() == []
        assert describe_health(game, config, db).state == "Protected"

    monkeypatch.setattr(GameWatcher, "watch_loop", watch_loop)
    args = ["--config", str(cfg), command]
    if command == "auto":
        args.append("--no-tray")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output + repr(result.exception)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_invalid_snapshot_blocks_automatic_retries_until_manual_repair(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    cfg, config, game, db = setup
    version = _backup(config, game, db)
    original_bytes = version.local_path.read_bytes()
    if damage == "missing":
        version.local_path.unlink()
    else:
        version.local_path.write_bytes(b"damaged archive")
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    assert cli.retry_pending_uploads(cfg) == (0, 1)
    db = Database(db.db_path)
    assert db.get_upload_jobs()[0].blocked
    health = describe_health(game, config, db)
    assert health.state == "Needs attention" and version.id in health.detail
    upload = cli._cloud_upload

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Automatically retried a snapshot needing manual repair")

    monkeypatch.setattr(cli, "_cloud_upload", forbidden)
    monkeypatch.setattr("game_save_genie.cli.time.time", lambda: 1e12)
    assert cli.retry_pending_uploads(cfg) == (0, 0)
    version.local_path.write_bytes(original_bytes)
    assert cli.retry_pending_uploads(cfg) == (0, 0)  # Repair still requires explicit revalidation.
    monkeypatch.setattr(cli, "_cloud_upload", upload)
    assert cli.retry_pending_uploads(cfg, manual=True) == (1, 0)
    assert db.get_upload_jobs() == [] and db.get_version(version.id) is not None
    assert describe_health(game, config, db).state == "Protected"


def test_blocked_history_is_retained_without_preventing_new_uploads(
    setup: tuple[Path, SyncConfig, Game, Database], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, config, game, db = setup
    broken = _backup(config, game, db)
    broken.local_path.write_bytes(b"damaged archive")
    monkeypatch.setattr(cli, "upload_save_cas", _upload_ok)
    assert cli.retry_pending_uploads(cfg) == (0, 1)
    (game.save_paths[0].path / "slot.sav").write_bytes(b"new healthy progress")
    latest = _backup(config, game, db)
    assert cli.retry_pending_uploads(cfg) == (1, 0)
    assert db.get_version(latest.id).cloud_synced  # type: ignore[union-attr]
    assert db.get_version(broken.id) is not None and broken.local_path.exists()
    assert db.get_upload_jobs()[0].blocked
    health = describe_health(game, config, db)
    assert health.state == "Needs attention" and broken.id in health.detail


def test_queue_schema_migrates_existing_jobs_without_resetting_state(tmp_path: Path) -> None:
    db_path = tmp_path / "old-queue.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE upload_jobs (version_id TEXT PRIMARY KEY, remote_name TEXT "
                           "NOT NULL, remote_root TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                           "next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT)")
        connection.execute("INSERT INTO upload_jobs VALUES ('version', 'remote', 'root', 3, 999, 'offline')")
    Database(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT * FROM upload_jobs").fetchone()
    assert row == ("version", "remote", "root", 3, 999, "offline", 0)
