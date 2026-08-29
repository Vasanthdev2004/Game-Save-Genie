"""Tests for CLI helper functions and onboarding entry points."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from game_save_genie.cli import _cloud_target, _slugify, _sync_display, app
from game_save_genie.models import CloudProvider, Game, Platform, SaveVersion, SyncConfig

runner = CliRunner()


def _recent_cloud_version() -> str:
    """A cloud version id that is newer than anything local but not absurd.

    A hardcoded far-future id used to serve here, until #39 started refusing
    ids stamped more than a day ahead as evidence of a broken clock.
    """
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y%m%d-%H%M%S-%f"
    )


def test_bare_gsg_shows_help_when_not_a_tty(tmp_path: Path) -> None:
    """Non-interactive bare invocation must print help, never hang on a wizard
    and never launch the dashboard into a pipe."""
    result = runner.invoke(app, ["--config", str(tmp_path / "config.yaml")])
    assert result.exit_code == 0
    assert "Commands" in result.output


def test_bare_gsg_opens_the_dashboard_when_configured(
    tmp_path: Path, monkeypatch: object
) -> None:
    """The Start Menu shortcut runs a bare `gsg`. Clicking the app must open
    the dashboard, not print command-line help at a gamer."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    opened: list[object] = []
    monkeypatch.setattr("game_save_genie.cli._dashboard_available", lambda: True)
    def fake_run(cfg: object) -> bool:
        opened.append(cfg)
        return True  # ui.run reports whether the dashboard exited cleanly

    monkeypatch.setattr("game_save_genie.ui.run", fake_run)

    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "config", "--cloud-provider", "google_drive",
         "--rclone-remote", "gdrive"],
    )
    result = runner.invoke(app, ["--config", cfg])
    assert result.exit_code == 0, result.output
    assert len(opened) == 1, "bare gsg did not open the dashboard"
    assert "Commands" not in result.output


def test_bare_gsg_falls_back_to_help_without_textual(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A missing optional dependency must degrade, not crash the entry point."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("game_save_genie.cli._dashboard_available", lambda: False)

    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "config", "--cloud-provider", "google_drive",
         "--rclone-remote", "gdrive"],
    )
    result = runner.invoke(app, ["--config", cfg])
    assert result.exit_code == 0
    assert "Commands" in result.output


def test_a_crashed_dashboard_falls_back_to_help(tmp_path: Path, monkeypatch: object) -> None:
    """Textual swallows exceptions raised after its loop starts, so `run()`
    returns normally on a crash. Reporting that as success would exit 0 on a
    screen full of traceback and skip the fallback entirely."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("game_save_genie.cli._dashboard_available", lambda: True)
    monkeypatch.setattr("game_save_genie.ui.run", lambda cfg: False)

    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "config", "--cloud-provider", "google_drive",
         "--rclone-remote", "gdrive"],
    )
    result = runner.invoke(app, ["--config", cfg])
    assert "Commands" in result.output


def test_a_declined_wizard_is_not_buried_by_the_dashboard(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A failed or declined setup used to fall straight through to the
    full-screen dashboard, hiding the reason nothing is being backed up."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    opened: list[object] = []

    def fake_run(cfg: object) -> bool:
        opened.append(cfg)
        return True

    monkeypatch.setattr("game_save_genie.cli._run_setup_wizard", lambda ctx: False)
    monkeypatch.setattr("game_save_genie.cli._is_interactive", lambda: True)
    monkeypatch.setattr("game_save_genie.cli._dashboard_available", lambda: True)
    monkeypatch.setattr("game_save_genie.ui.run", fake_run)

    result = runner.invoke(app, ["--config", str(tmp_path / "c.yaml")])
    assert result.exit_code == 0, result.output
    assert "not set up" in result.output
    assert opened == [], "the dashboard covered the wizard's explanation"


def test_help_flag_never_opens_the_dashboard(tmp_path: Path, monkeypatch: object) -> None:
    """`gsg --help` must always be help, even on a configured tty."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    opened: list[object] = []
    monkeypatch.setattr("game_save_genie.cli._dashboard_available", lambda: True)
    def fake_run(cfg: object) -> bool:
        opened.append(cfg)
        return True  # ui.run reports whether the dashboard exited cleanly

    monkeypatch.setattr("game_save_genie.ui.run", fake_run)

    result = runner.invoke(app, ["--config", str(tmp_path / "c.yaml"), "--help"])
    assert result.exit_code == 0
    assert "Commands" in result.output
    assert opened == [], "--help opened the dashboard"


def test_auto_unconfigured_non_tty_exits_with_hint(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    # Keep the error path from touching the real user profile / root logger.
    monkeypatch.setattr("game_save_genie.cli.setup_file_logging", lambda p: p)
    result = runner.invoke(app, ["--config", str(tmp_path / "config.yaml"), "auto"])
    assert result.exit_code == 1
    assert "guided setup" in result.output


def test_auto_no_wizard_never_prompts(tmp_path: Path, monkeypatch: object) -> None:
    """The autostart entry passes --no-wizard: unconfigured must exit, not prompt."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("game_save_genie.cli.setup_file_logging", lambda p: p)
    result = runner.invoke(
        app, ["--config", str(tmp_path / "config.yaml"), "auto", "--no-wizard"]
    )
    assert result.exit_code == 1
    assert "not configured" in result.output


def test_version_flag(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Game Save Genie" in result.output


def test_add_path_creates_custom_game(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    from game_save_genie.config import load_games

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    cfg = tmp_path / "c.yaml"
    saves = tmp_path / "saves"
    saves.mkdir()
    result = runner.invoke(
        app, ["--config", str(cfg), "add", "RetroArch", "--path", str(saves)]
    )
    assert result.exit_code == 0
    assert "custom" in result.output
    games = load_games(cfg)
    assert len(games) == 1
    assert games[0].custom is True
    assert games[0].save_paths[0].path == saves


def test_custom_backup_restore_via_cli(tmp_path: Path, monkeypatch: object) -> None:
    """Full CLI round-trip: add --path, backup, corrupt, restore."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    cfg = tmp_path / "c.yaml"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "hero.srm").write_bytes(b"level-50")

    runner.invoke(app, ["--config", str(cfg), "config", "--backup-dir", str(tmp_path / "bk")])
    assert runner.invoke(app, ["--config", str(cfg), "add", "RA", "--path", str(saves)]).exit_code == 0
    b = runner.invoke(app, ["--config", str(cfg), "backup", "ra", "--no-cloud"])
    assert b.exit_code == 0 and "Backed up" in b.output

    (saves / "hero.srm").write_bytes(b"WIPED")
    r = runner.invoke(app, ["--config", str(cfg), "restore", "ra"])
    assert r.exit_code == 0, r.output
    assert (saves / "hero.srm").read_bytes() == b"level-50"


def test_pull_requires_game_or_all(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "c.yaml"), "pull"])
    assert result.exit_code == 1
    assert "--all" in result.output


class _IdleWatcher:
    """GameWatcher stand-in: nothing is ever running (keeps tests hermetic)."""

    def __init__(self, games: object, **kwargs: object) -> None:
        pass

    def prime(self) -> None:
        pass

    def is_running(self, game_id: str) -> bool:
        return False

    def running_process_info(self, game_id: str) -> None:
        return None


def test_pull_dry_run_reports_newer_cloud_version(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "add", "Pull Wiring Test", "--cloud", "s3", "--remote", "r"],
    )
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.cli.GameWatcher", _IdleWatcher)
    monkeypatch.setattr(
        "game_save_genie.cli.list_remote_versions",
        lambda *a, **k: [_recent_cloud_version()],
    )
    monkeypatch.setattr("game_save_genie.cli.get_rclone_path", lambda p: Path("rclone"))
    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", lambda p: Path("ludusavi"))

    result = runner.invoke(
        app, ["--config", cfg, "pull", "pull-wiring-test", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "Would restore" in result.output
    assert _recent_cloud_version()[:8] in result.output


def test_pull_listing_failure_exits_nonzero(tmp_path: Path, monkeypatch: object) -> None:
    """An unreachable cloud must be an error, not 'no cloud versions'."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "add", "Pull Err Test", "--cloud", "s3", "--remote", "r"],
    )

    def boom(*a: object, **k: object) -> list[str]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.cli.GameWatcher", _IdleWatcher)
    monkeypatch.setattr("game_save_genie.cli.list_remote_versions", boom)
    monkeypatch.setattr("game_save_genie.cli.get_rclone_path", lambda p: Path("rclone"))
    monkeypatch.setattr("game_save_genie.cli.get_ludusavi_path", lambda p: Path("ludusavi"))

    result = runner.invoke(app, ["--config", cfg, "pull", "pull-err-test"])
    assert result.exit_code == 1
    assert "cloud listing failed" in result.output


def test_slugify_matches_legacy_scheme() -> None:
    """Ids must stay byte-identical to those in existing games.yaml files —
    a changed scheme would re-add every tracked game under a new id."""
    assert _slugify("Solo Leveling: Arise") == "solo-leveling--arise"
    assert _slugify("Elden Ring") == "elden-ring"
    assert _slugify("  Trimmed  ") == "trimmed"


def test_slugify_keeps_unicode_titles() -> None:
    assert _slugify("Ведьмак") == "ведьмак"
    assert _slugify("エルデンリング") == "エルデンリング"


def test_slugify_never_returns_empty() -> None:
    slug = _slugify("!!!")
    assert slug.startswith("game-")
    assert len(slug) > 5
    # Deterministic: same title always maps to the same id.
    assert _slugify("!!!") == slug


# --- Cloud destination resolution -------------------------------------------
# `game.cloud_provider` is a stored hint that nothing routes on. These pin the
# rule that the destination is always *derived* from the live config.


def _drive_config(tmp_path: Path) -> SyncConfig:
    return SyncConfig(
        backup_dir=tmp_path / "bk",
        cloud_provider=CloudProvider.GOOGLE_DRIVE,
        rclone_remote_name="gdrive",
        remote_root="game-save-genie",
    )


def test_cloud_target_ignores_a_stale_per_game_provider(tmp_path: Path) -> None:
    """A game stamped with a dead provider resolves to the remote in force.

    Regression: games added while on Railway S3 kept `cloud_provider: s3`
    forever, so `gsg list` reported "s3" for saves that were sitting in
    Google Drive the whole time.
    """
    game = Game(
        id="cyberpunk-2077",
        title="Cyberpunk 2077",
        platform=Platform.WINDOWS,
        cloud_provider=CloudProvider.S3,
    )
    assert _cloud_target(game, _drive_config(tmp_path)) == "gdrive:game-save-genie"


def test_cloud_target_falls_back_to_the_global_provider(tmp_path: Path) -> None:
    """No per-game provider means "use the global one", not "cloud is off"."""
    game = Game(id="g", title="G", platform=Platform.WINDOWS)
    assert _cloud_target(game, _drive_config(tmp_path)) == "gdrive:game-save-genie"


def test_cloud_target_prefers_a_per_game_remote(tmp_path: Path) -> None:
    game = Game(id="g", title="G", platform=Platform.WINDOWS, remote_path="homelab")
    assert _cloud_target(game, _drive_config(tmp_path)) == "homelab:game-save-genie"


def test_cloud_target_is_none_without_a_remote(tmp_path: Path) -> None:
    game = Game(id="g", title="G", platform=Platform.WINDOWS, cloud_provider=CloudProvider.S3)
    config = SyncConfig(backup_dir=tmp_path / "bk", cloud_provider=CloudProvider.S3)
    assert _cloud_target(game, config) is None


def _version(remote_path: str | None, synced: bool = True) -> SaveVersion:
    return SaveVersion(
        id="20260725-174814-438884",
        game_id="cyberpunk-2077",
        created_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        local_path=Path("snap.zip"),
        size_bytes=1,
        file_count=1,
        platform=Platform.WINDOWS,
        cloud_synced=synced,
        cloud_remote_path=remote_path,
    )


def test_sync_display_flags_a_version_stranded_on_an_old_remote(tmp_path: Path) -> None:
    """`cloud_synced` is never cleared, so a switch leaves rows claiming "yes"
    while the configured remote holds nothing. Say so instead."""
    game = Game(id="cyberpunk-2077", title="C", platform=Platform.WINDOWS)
    version = _version("railway:old-bucket/cyberpunk-2077/manifests/v.json")
    assert _sync_display(version, game, _drive_config(tmp_path)) == "stale (railway)"


def test_sync_display_reads_yes_when_the_remote_still_matches(tmp_path: Path) -> None:
    game = Game(id="cyberpunk-2077", title="C", platform=Platform.WINDOWS)
    version = _version("gdrive:game-save-genie/cyberpunk-2077/manifests/v.json")
    assert _sync_display(version, game, _drive_config(tmp_path)) == "yes"


def test_sync_display_reads_pending_when_never_uploaded(tmp_path: Path) -> None:
    game = Game(id="g", title="G", platform=Platform.WINDOWS)
    assert _sync_display(_version(None, synced=False), game, _drive_config(tmp_path)) == "pending"


def test_sync_display_does_not_cry_stale_on_legacy_rows(tmp_path: Path) -> None:
    """Pre-CAS rows have no remote recorded — fail toward "yes", not a scare."""
    game = Game(id="g", title="G", platform=Platform.WINDOWS)
    assert _sync_display(_version(None), game, _drive_config(tmp_path)) == "yes"


def test_list_shows_the_real_destination_not_the_stored_label(
    tmp_path: Path, monkeypatch: object
) -> None:
    """End-to-end regression for the report that started this: `gsg list`
    must never print a provider the saves do not actually go to."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    # Rich truncates columns to terminal width; give it room to print in full.
    monkeypatch.setenv("COLUMNS", "200")
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app,
        ["--config", cfg, "config", "--cloud-provider", "google_drive",
         "--rclone-remote", "gdrive"],
    )
    runner.invoke(app, ["--config", cfg, "add", "Stale Label Test", "--cloud", "s3"])

    result = runner.invoke(app, ["--config", cfg, "list"])
    assert result.exit_code == 0, result.output
    assert "gdrive:game-save-genie" in result.output
    assert "s3" not in result.output


def test_status_calls_out_games_that_have_never_backed_up(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(app, ["--config", cfg, "add", "Never Backed Up", "--exe", "nbu.exe"])

    result = runner.invoke(app, ["--config", cfg, "status"])
    assert result.exit_code == 0, result.output
    assert "never been backed up" in result.output
    assert "never-backed-up" in result.output


# --- remove --purge ---------------------------------------------------------
# Purge is the only irreversible command. It must delete exactly the remote the
# game's saves were written to, and never claim success it did not verify.


def _flat(output: str) -> str:
    """Collapse Rich's terminal wrapping so message assertions are stable."""
    return " ".join(output.split())


def _record_rclone(monkeypatch: object, tmp_path: Path, returncode: int = 0) -> list[list[str]]:
    import subprocess

    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    calls: list[list[str]] = []

    def fake_run_rclone(
        binary: Path, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout="", stderr="denied"
        )

    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.cli.get_rclone_path", lambda p: Path("rclone"))
    monkeypatch.setattr("game_save_genie.cli.run_rclone", fake_run_rclone)
    return calls


def _isolated_config(tmp_path: Path, *extra: str) -> str:
    """A config whose backup_dir is inside tmp_path.

    Purge deletes directories — a test must never resolve `backup_dir` to the
    developer's real backup root.
    """
    cfg = str(tmp_path / "c.yaml")
    runner.invoke(
        app, ["--config", cfg, "config", "--backup-dir", str(tmp_path / "bk"), *extra]
    )
    return cfg


def test_purge_deletes_from_the_games_own_remote(tmp_path: Path, monkeypatch: object) -> None:
    """A per-game remote must not be purged from the *global* remote — that
    deletes an unrelated path and leaves the real saves untouched."""
    cfg = _isolated_config(tmp_path, "--rclone-remote", "gdrive")
    runner.invoke(
        app, ["--config", cfg, "add", "Purge Test", "--cloud", "s3", "--remote", "homelab"]
    )
    calls = _record_rclone(monkeypatch, tmp_path)

    result = runner.invoke(app, ["--config", cfg, "remove", "purge-test", "--purge"])
    assert result.exit_code == 0, result.output
    purges = [a for a in calls if a and a[0] == "purge"]
    assert len(purges) == 1, calls
    assert purges[0][1].startswith("homelab:"), purges[0][1]


def test_purge_still_deletes_cloud_without_a_per_game_provider(
    tmp_path: Path, monkeypatch: object
) -> None:
    """`gsg auto` uploads these games, so `--purge` must clean them up too —
    it used to skip them silently while reporting the local deletions."""
    cfg = _isolated_config(
        tmp_path, "--cloud-provider", "google_drive", "--rclone-remote", "gdrive"
    )
    runner.invoke(app, ["--config", cfg, "add", "No Provider", "--exe", "np.exe"])
    calls = _record_rclone(monkeypatch, tmp_path)

    result = runner.invoke(app, ["--config", cfg, "remove", "no-provider", "--purge"])
    assert result.exit_code == 0, result.output
    purges = [a for a in calls if a and a[0] == "purge"]
    assert len(purges) == 1, calls
    assert purges[0][1].startswith("gdrive:"), purges[0][1]


def test_purge_does_not_claim_success_when_rclone_fails(
    tmp_path: Path, monkeypatch: object
) -> None:
    """`check=False` swallowed the exit code, so every purge printed
    "Deleted cloud saves" — including the ones that deleted nothing."""
    cfg = _isolated_config(tmp_path, "--rclone-remote", "gdrive")
    runner.invoke(app, ["--config", cfg, "add", "Fail Test", "--cloud", "s3"])
    _record_rclone(monkeypatch, tmp_path, returncode=1)

    result = runner.invoke(app, ["--config", cfg, "remove", "fail-test", "--purge"])
    assert result.exit_code == 0, result.output
    assert "Deleted cloud saves" not in result.output
    assert "may still exist" in _flat(result.output)


def test_purge_warns_when_no_remote_is_configured(
    tmp_path: Path, monkeypatch: object
) -> None:
    cfg = _isolated_config(tmp_path)
    runner.invoke(app, ["--config", cfg, "add", "Local Only", "--exe", "lo.exe"])
    _record_rclone(monkeypatch, tmp_path)

    result = runner.invoke(app, ["--config", cfg, "remove", "local-only", "--purge"])
    assert result.exit_code == 0, result.output
    assert "were NOT deleted" in _flat(result.output)


# --- S3 endpoint setup (#24) -----------------------------------------------
# A self-hosted MinIO speaks plain HTTP. rclone assumes https for a scheme-less
# endpoint, so "myserver:9000" produced a TLS error the reporter could not act
# on. Setup now tries the stated interpretation first and falls back.


def test_endpoint_with_a_scheme_is_taken_at_its_word() -> None:
    from game_save_genie.cli import _endpoint_candidates

    assert _endpoint_candidates("http://host:9000") == ["http://host:9000"]
    assert _endpoint_candidates("https://s3.example.com") == ["https://s3.example.com"]


def test_scheme_less_endpoint_tries_tls_before_plaintext() -> None:
    from game_save_genie.cli import _endpoint_candidates

    assert _endpoint_candidates("host:9000") == ["https://host:9000", "http://host:9000"]


def test_empty_endpoint_has_nothing_to_try() -> None:
    from game_save_genie.cli import _endpoint_candidates

    assert _endpoint_candidates("   ") == []


def _fake_probe(succeed_on: str) -> object:
    """A _probe_s3_remote stand-in that only accepts one endpoint scheme."""
    import game_save_genie.cloud as cloud_module

    def probe(config_path: Path, remote_name: str, bucket: str) -> tuple[bool, str]:
        written = cloud_module._read_rclone_config()[remote_name]["endpoint"]
        if written.startswith(succeed_on):
            return True, ""
        return False, "server gave HTTP response to HTTPS client"

    return probe


def _run_setup(
    tmp_path: Path, monkeypatch: object, succeed_on: str, endpoint: str
) -> tuple[bool, dict[str, str]]:
    import pytest

    import game_save_genie.cli as cli_module
    import game_save_genie.cloud as cloud_module

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    conf = tmp_path / "rclone.conf"
    monkeypatch.setattr(cloud_module, "get_rclone_config_path", lambda: conf)
    monkeypatch.setattr(cli_module, "_probe_s3_remote", _fake_probe(succeed_on))

    ok = cli_module._configure_and_verify_s3(
        tmp_path / "config.yaml", "homelab", endpoint, "k", "s", "saves", "auto",
    )
    return ok, cloud_module._read_rclone_config().get("homelab", {})


def test_plain_http_server_is_reached_after_https_fails(
    tmp_path: Path, monkeypatch: object
) -> None:
    """The reported case: MinIO on http, endpoint typed without a scheme."""
    ok, remote = _run_setup(tmp_path, monkeypatch, "http://", "192.0.2.10:9000")
    assert ok
    assert remote["endpoint"] == "http://192.0.2.10:9000"


def test_https_server_is_not_downgraded(tmp_path: Path, monkeypatch: object) -> None:
    """https answers first, so plaintext must never be attempted."""
    ok, remote = _run_setup(tmp_path, monkeypatch, "https://", "s3.example.com")
    assert ok
    assert remote["endpoint"] == "https://s3.example.com"


def test_explicit_https_is_never_retried_as_http(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Asking for TLS and silently getting plaintext would be a downgrade."""
    ok, remote = _run_setup(tmp_path, monkeypatch, "http://", "https://s3.example.com")
    assert not ok
    assert remote["endpoint"] == "https://s3.example.com"


def test_a_remote_that_never_answers_reverts_the_config(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Otherwise the wizard never runs again and every upload fails silently."""
    from game_save_genie.config import load_config

    config_path = tmp_path / "config.yaml"
    ok, _ = _run_setup(tmp_path, monkeypatch, "ftp://", "192.0.2.10:9000")
    assert not ok
    config = load_config(config_path)
    assert config.cloud_provider is None
    assert config.rclone_remote_name is None


def test_setup_writes_path_style_by_default(
    tmp_path: Path, monkeypatch: object
) -> None:
    _, remote = _run_setup(tmp_path, monkeypatch, "http://", "192.0.2.10:9000")
    assert remote["force_path_style"] == "true"
