"""Command line interface for Game Save Genie."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .archive import safe_extract_zip, sha256_file, zip_directory
from .cloud import (
    _remote_path,
    download_save,
    get_rclone_path,
    get_remote_size,
    list_remote_versions,
    prune_remote_versions,
    run_rclone,
    upload_save_cas,
    write_railway_s3_config,
)
from .config import (
    get_config_path,
    get_data_dir,
    get_games_path,
    load_config,
    load_games,
    save_config,
    save_games,
)
from .database import Database
from .ludusavi import (
    backup_game,
    get_ludusavi_path,
    preview_backup,
    restore_from_backup,
    scan_games,
)
from .models import (
    BackupResult,
    CloudProvider,
    Game,
    Platform,
    ProcessInfo,
    SaveVersion,
    SyncConfig,
)
from .notify import notify, setup_file_logging
from .remap import _current_platform, apply_remap_to_staged_backup
from .sync import effective_local_latest, latest_version_id, should_restore_from_cloud
from .watcher import GameWatcher

app = typer.Typer(help="Game Save Genie - self-hosted cloud save sync")
console = Console()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"Game Save Genie {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit",
    ),
) -> None:
    """Global options."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config

    if ctx.invoked_subcommand is None:
        # Bare `gsg`: first run gets the guided setup; otherwise show help.
        if not _cloud_configured(config) and sys.stdin.isatty():
            if _run_setup_wizard(ctx):
                console.print(
                    "\n[green]Setup complete![/green] Run [bold]gsg auto[/bold] to start "
                    "automatic backup."
                )
                return
        console.print(ctx.get_help())


def _cloud_configured(config_path: Optional[Path]) -> bool:
    config = load_config(config_path)
    return bool(config.rclone_remote_name and config.cloud_provider)


@app.command()
def init(
    ctx: typer.Context,
    backup_dir: Optional[Path] = typer.Option(None, help="Local backup directory"),
) -> None:
    """Initialize Game Save Genie configuration."""
    config_path = ctx.obj.get("config_path") or get_config_path()
    config = load_config(config_path)
    if backup_dir:
        config.backup_dir = backup_dir
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    save_config(config, config_path)
    if not get_games_path(config_path).exists():
        save_games([], config_path)
    Database(get_data_dir() / "versions.db")
    console.print(f"[green]Initialized Game Save Genie at {config_path.parent}[/green]")
    console.print(f"Backups: {config.backup_dir}")


@app.command()
def scan(
    ctx: typer.Context,
    source: str = typer.Option(
        "hydra",
        "--source",
        help="Filter by launcher: 'hydra' (non-Steam/Epic/Xbox), 'all', 'steam', 'epic', 'xbox'",
    ),
) -> None:
    """Scan for installed games and their save locations."""
    from .launcher import detect_launcher, get_all_launcher_games

    config_path = ctx.obj.get("config_path")
    ludusavi_path = get_ludusavi_path(config_path)
    console.print("[cyan]Scanning for games with Ludusavi...[/cyan]")
    data = scan_games(ludusavi_path)
    games_data = data.get("games", {})
    if not games_data:
        console.print("[yellow]No games found.[/yellow]")
        return

    # Detect launcher for each game
    steam_games, epic_games, xbox_games = get_all_launcher_games()

    table = Table(title="Detected Games")
    table.add_column("Title")
    table.add_column("Source")
    table.add_column("Files")
    table.add_column("Size")

    source_colors = {
        "steam": "blue",
        "epic": "magenta",
        "xbox": "green",
        "other": "cyan",
    }

    for title, info in games_data.items():
        files = info.get("files", {})
        size = sum(int(f.get("bytes", 0)) for f in files.values())
        save_paths = list(files.keys())
        detected = detect_launcher(
            title, save_paths, steam_games, epic_games, xbox_games
        )

        # Filter: "hydra" shows non-Steam/Epic/Xbox games (detected as "other")
        if source == "all":
            pass
        elif source == "hydra":
            if detected != "other":
                continue
        elif detected != source:
            continue

        color = source_colors.get(detected, "white")
        table.add_row(
            title,
            f"[{color}]{detected}[/{color}]",
            str(len(files)),
            _human_size(size),
        )

    console.print(table)
    if source != "all":
        console.print(
            f"[dim]Filtered by source: {source}. Use --source all to see every game.[/dim]"
        )


@app.command()
def add(
    ctx: typer.Context,
    title: str = typer.Argument(..., help="Game title"),
    executable: Optional[str] = typer.Option(None, "--exe", help="Executable name to watch"),
    platform: Platform = typer.Option(_current_platform(), "--platform", help="Platform"),
    cloud: Optional[CloudProvider] = typer.Option(None, "--cloud", help="Cloud provider"),
    remote_path: Optional[str] = typer.Option(None, "--remote", help="Remote path/remote name"),
    no_auto_sync: bool = typer.Option(False, "--no-auto-sync", help="Disable auto-sync"),
) -> None:
    """Add a game to track."""
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    game_id = _slugify(title)
    if any(g.id == game_id for g in games):
        console.print(f"[yellow]Game '{title}' is already tracked.[/yellow]")
        raise typer.Exit(1)

    game = Game(
        id=game_id,
        title=title,
        platform=platform,
        executable_names=[executable] if executable else [],
        auto_sync=not no_auto_sync,
        cloud_provider=cloud,
        remote_path=remote_path,
    )
    games.append(game)
    save_games(games, config_path)
    console.print(f"[green]Added game: {title} ({game_id})[/green]")


@app.command(name="list")
def list_games(ctx: typer.Context) -> None:
    """List tracked games."""
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    if not games:
        console.print("[yellow]No games tracked.[/yellow]")
        return

    table = Table(title="Tracked Games")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Platform")
    table.add_column("Auto Sync")
    table.add_column("Cloud")

    for game in games:
        table.add_row(
            game.id,
            game.title,
            game.platform.value,
            "yes" if game.auto_sync else "no",
            game.cloud_provider.value if game.cloud_provider else "none",
        )
    console.print(table)


@app.command()
def remove(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to remove"),
    purge: bool = typer.Option(False, "--purge", help="Also delete local backups and cloud saves"),
) -> None:
    """Remove a game from tracking."""
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    game = next((g for g in games if g.id == game_id), None)
    if not game:
        console.print(f"[red]Game '{game_id}' not found.[/red]")
        raise typer.Exit(1)

    games = [g for g in games if g.id != game_id]
    save_games(games, config_path)
    console.print(f"[green]Removed: {game.title} ({game_id})[/green]")

    if purge:
        # Delete local backups (live backup dir + per-version snapshots)
        import shutil

        config = load_config(config_path)
        for local_dir in (
            config.backup_dir / game_id,
            config.backup_dir / "_versions" / game_id,
        ):
            if local_dir.exists():
                shutil.rmtree(local_dir, ignore_errors=True)
                console.print(f"  [dim]Deleted local backups: {local_dir}[/dim]")

        # Delete cloud saves
        if game.cloud_provider and config.rclone_remote_name:
            try:
                rclone_path = get_rclone_path(config_path)
                remote = _remote_path(config.rclone_remote_name, config.remote_root, game_id)
                run_rclone(rclone_path, ["purge", remote], check=False)
                console.print(f"  [dim]Deleted cloud saves: {remote}[/dim]")
            except RuntimeError:
                pass


@app.command()
def status(ctx: typer.Context) -> None:
    """Show quick overview of tracked games, backups, and cloud sync status."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    db = Database(get_data_dir() / "versions.db")

    if not games:
        console.print("[yellow]No games tracked. Run 'gsg scan' then 'gsg add'.[/yellow]")
        return

    table = Table(title="Game Save Genie Status")
    table.add_column("Game")
    table.add_column("Versions")
    table.add_column("Last Backup")
    table.add_column("Cloud")
    table.add_column("Cloud Synced")

    for game in games:
        versions = db.get_versions(game.id)
        last_backup = "never"
        cloud_synced = "no"
        # Judge sync status by the newest real backup — safety backups are
        # local-only by design and would otherwise show as forever-pending.
        display = next((v for v in versions if v.origin != "safety"), None)
        if display:
            last_backup = display.created_at.strftime("%Y-%m-%d %H:%M")
            cloud_synced = "yes" if display.cloud_synced else "pending"

        cloud_status = game.cloud_provider.value if game.cloud_provider else "none"
        table.add_row(
            game.title,
            str(len(versions)),
            last_backup,
            cloud_status,
            cloud_synced,
        )

    console.print(table)

    # Storage summary
    local_size = sum(
        f.stat().st_size for f in config.backup_dir.rglob("*") if f.is_file()
    ) if config.backup_dir.exists() else 0

    console.print(f"\n[dim]Local backups: {len(db.get_all_versions())} versions, {_human_size(local_size)}[/dim]")
    if config.rclone_remote_name:
        try:
            rclone_path = get_rclone_path(config_path)
            objects, remote_size = get_remote_size(
                rclone_path, config.rclone_remote_name, config.remote_root
            )
            console.print(f"[dim]Cloud storage: {objects} objects, {_human_size(remote_size)}[/dim]")
            limit_bytes = int(config.storage_limit_gb * 1024**3)
            if limit_bytes > 0 and remote_size >= 0.8 * limit_bytes:
                console.print(
                    f"[yellow]Cloud storage is at {remote_size / limit_bytes:.0%} of the "
                    f"{config.storage_limit_gb:g} GB limit. Lower max_versions or run "
                    f"'gsg remove --purge' on unused games.[/yellow]"
                )
        except RuntimeError:
            console.print("[dim]Cloud storage: unable to connect[/dim]")


@app.command()
def backup(
    ctx: typer.Context,
    game_id: Optional[str] = typer.Argument(None, help="Game ID to backup (omit for all)"),
    label: Optional[str] = typer.Option(None, "--label", help="Backup label"),
    no_cloud: bool = typer.Option(False, "--no-cloud", help="Skip cloud upload"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
) -> None:
    """Back up save data for one or all games."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    db = Database(get_data_dir() / "versions.db")
    ludusavi_path = get_ludusavi_path(config_path)

    targets = [g for g in games if g.id == game_id] if game_id else games
    if game_id and not targets:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)

    for game in targets:
        if dry_run:
            preview = preview_backup(ludusavi_path, game, config.backup_dir)
            console.print(f"{'[cyan]' if preview.success else '[red]'}{game.title}: {preview.message}[/]")
            continue
        result = _run_backup(game, config, db, ludusavi_path, label)
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version and not no_cloud and game.cloud_provider:
            _cloud_upload(ctx, game, result.version, dry_run=False)


@app.command()
def restore(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to restore"),
    version_id: Optional[str] = typer.Option(None, "--version", help="Version ID (omit for latest)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
    no_safety: bool = typer.Option(
        False, "--no-safety", help="Skip the pre-restore safety backup (not recommended)"
    ),
) -> None:
    """Restore a save version for a game."""
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    game = next((g for g in games if g.id == game_id), None)
    if not game:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)

    db = Database(get_data_dir() / "versions.db")
    if version_id:
        version = db.get_version(version_id)
    else:
        versions = db.get_versions(game_id)
        version = versions[0] if versions else None

    if not version or version.game_id != game_id:
        console.print(f"[red]No local version found for {game_id}.[/red]")
        raise typer.Exit(1)

    if dry_run:
        console.print(f"[cyan]Would restore version {version.id} for {game.title}[/cyan]")
        return

    ludusavi_path = get_ludusavi_path(config_path)
    config = load_config(config_path)

    # Verify and stage the snapshot BEFORE touching anything on disk.
    restore_source = _materialize_version(version, game)
    if restore_source is None:
        raise typer.Exit(1)

    # Secure the current on-disk state so this restore can be undone.
    if not no_safety:
        safety = _run_backup(
            game, config, db, ludusavi_path,
            label="Safety backup before restore", origin="safety",
            protect_id=version.id,
        )
        if not safety.success:
            console.print(
                f"[red]Safety backup failed ({safety.message}); aborting restore. "
                f"Pass --no-safety to restore anyway.[/red]"
            )
            raise typer.Exit(1)

    try:
        restore_from_backup(ludusavi_path, game, restore_source)
    except RuntimeError as exc:
        console.print(f"[red]Restore failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Restored {game.title} from version {version.id}[/green]")


@app.command()
def versions(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID"),
) -> None:
    """List save versions for a game."""
    db = Database(get_data_dir() / "versions.db")
    versions_list = db.get_versions(game_id)
    if not versions_list:
        console.print("[yellow]No versions found.[/yellow]")
        return

    table = Table(title=f"Save Versions for {game_id}")
    table.add_column("Version ID")
    table.add_column("Created")
    table.add_column("Size")
    table.add_column("Files")
    table.add_column("Machine")
    table.add_column("Cloud")

    for v in versions_list:
        table.add_row(
            v.id,
            v.created_at.strftime("%Y-%m-%d %H:%M"),
            _human_size(v.size_bytes),
            str(v.file_count),
            v.source_machine or "unknown",
            "yes" if v.cloud_synced else "no",
        )
    console.print(table)


@app.command()
def cloud_list(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID"),
) -> None:
    """List versions available in the cloud for a game."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    game = next((g for g in games if g.id == game_id), None)
    if not game or not game.cloud_provider:
        console.print("[red]Game has no cloud provider configured.[/red]")
        raise typer.Exit(1)

    remote_name = game.remote_path or config.rclone_remote_name
    if not remote_name:
        console.print("[red]No rclone remote configured. Run 'gsg setup-railway' first.[/red]")
        raise typer.Exit(1)
    rclone_path = get_rclone_path(config_path)
    try:
        version_ids = list_remote_versions(rclone_path, game, remote_name, config.remote_root)
    except RuntimeError as exc:
        console.print(f"[red]Cloud listing failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    if not version_ids:
        console.print("[yellow]No cloud versions found.[/yellow]")
        return
    for vid in version_ids:
        console.print(vid)


@app.command()
def pull(
    ctx: typer.Context,
    game_id: Optional[str] = typer.Argument(None, help="Game ID to pull (omit with --all)"),
    version_id: Optional[str] = typer.Option(
        None, "--version", help="Cloud version ID (omit for latest; see 'gsg cloud-list')"
    ),
    all_games: bool = typer.Option(
        False, "--all", help="Catch up every tracked cloud game that is behind"
    ),
    force: bool = typer.Option(
        False, "--force", help="Restore even when the local save is newer than the cloud"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
) -> None:
    """Pull a save down from the cloud and apply it — the cross-machine restore.

    On a machine that is behind (or a fresh machine), this downloads the
    cloud save, remaps paths recorded under another username to this
    machine, takes a safety backup, and applies it.
    """
    if bool(game_id) == all_games:
        console.print("[red]Specify a game ID, or use --all.[/red]")
        raise typer.Exit(1)
    if all_games and version_id:
        console.print("[red]--version needs a specific game ID, not --all.[/red]")
        raise typer.Exit(1)

    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    db = Database(get_data_dir() / "versions.db")

    targets = games if all_games else [g for g in games if g.id == game_id]
    if not all_games and not targets:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)
    if all_games:
        # --all respects pause; an explicitly named game does not.
        paused = [g for g in targets if not (g.auto_sync and g.sync_enabled)]
        for g in paused:
            console.print(f"[dim]{g.title}: paused — skipped (pull it by id to override).[/dim]")
        targets = [g for g in targets if g.auto_sync and g.sync_enabled]
    cloud_targets = [
        g for g in targets
        if g.cloud_provider and (g.remote_path or config.rclone_remote_name)
    ]
    if not cloud_targets:
        console.print(
            "[red]No cloud-enabled game to pull. Configure cloud storage with 'gsg' "
            "or set a provider with 'gsg add --cloud'.[/red]"
        )
        raise typer.Exit(1)

    rclone_path = get_rclone_path(config_path)
    ludusavi_path = get_ludusavi_path(config_path)

    # Never restore underneath a live process.
    probe = GameWatcher(cloud_targets)
    probe.prime()

    pulled = 0
    failed = 0
    skipped_running = 0
    for game in cloud_targets:
        if probe.is_running(game.id):
            proc_info = probe.running_process_info(game.id)
            matched = f" (matched: {proc_info.exe or proc_info.name})" if proc_info else ""
            if force and not all_games:
                console.print(
                    f"[yellow]{game.title}: appears to be running{matched} — "
                    f"proceeding anyway (--force).[/yellow]"
                )
            else:
                console.print(
                    f"[yellow]{game.title}: game is running{matched} — close it and "
                    f"retry, or use --force if this match is wrong. Skipped.[/yellow]"
                )
                skipped_running += 1
                continue

        remote_name = game.remote_path or config.rclone_remote_name
        assert remote_name is not None  # filtered above
        cloud_latest: Optional[str] = None
        if not (version_id and force):
            try:
                cloud_ids = list_remote_versions(
                    rclone_path, game, remote_name, config.remote_root
                )
            except RuntimeError as exc:
                console.print(f"[red]{game.title}: cloud listing failed: {exc}[/red]")
                failed += 1
                continue
            cloud_latest = latest_version_id(cloud_ids)
        if version_id:
            target_version = version_id
        else:
            if cloud_latest is None:
                console.print(f"[dim]{game.title}: no cloud versions.[/dim]")
                continue
            if not force:
                local_latest = db.get_latest_version_id(game.id, exclude_safety=True)
                effective = effective_local_latest(local_latest, db.get_sync_state(game.id))
                if not should_restore_from_cloud(effective, cloud_latest):
                    hint = "" if all_games else " Use --force or --version <id> to restore anyway."
                    console.print(f"[dim]{game.title}: already up to date.{hint}[/dim]")
                    continue
            target_version = cloud_latest

        if dry_run:
            console.print(
                f"[cyan]Would restore {game.title} from cloud version {target_version}[/cyan]"
            )
            continue
        if _apply_cloud_version(
            game, config, db, rclone_path, ludusavi_path, target_version
        ):
            pulled += 1
            if version_id and cloud_latest and cloud_latest > target_version:
                # Deliberately pulling an OLD version is a decision over
                # everything currently in the cloud — record the newest id
                # as seen so gsg auto does not immediately overwrite the
                # user's choice with the latest.
                _record_applied_cloud_version(db, game.id, cloud_latest)
        else:
            failed += 1

    if all_games and not dry_run:
        console.print(f"\n[green]Pulled {pulled} game(s).[/green]" + (
            f" [red]{failed} failed.[/red]" if failed else ""
        ))
    if failed or (skipped_running and not all_games):
        raise typer.Exit(1)


@app.command()
def watch(ctx: typer.Context) -> None:
    """Watch running games and auto-backup on close."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = _watchable_games(load_games(config_path))
    if not config.auto_sync_on_game_close:
        console.print("[yellow]Auto-sync on game close is disabled in config.[/yellow]")
        raise typer.Exit(1)
    if not games:
        console.print("[yellow]No games with auto-sync enabled. Run 'gsg add' or 'gsg resume'.[/yellow]")
        raise typer.Exit(1)

    lock = _acquire_instance_lock()
    if lock is None:
        console.print("[red]Another gsg watcher is already running.[/red]")
        raise typer.Exit(1)

    db = Database(get_data_dir() / "versions.db")
    ludusavi_path = get_ludusavi_path(config_path)

    def on_close(game: Game, proc_info: object) -> None:
        console.print(f"[cyan]Game closed: {game.title}. Backing up...[/cyan]")
        result = _run_backup(
            game, config, db, ludusavi_path,
            label=f"Auto-backup on {datetime.now()}", origin="auto",
        )
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version and game.cloud_provider:
            _cloud_upload(ctx, game, result.version, dry_run=False)

    watcher = GameWatcher(games)
    watcher.set_on_game_close(on_close)
    watcher.prime()
    console.print("[green]Watching for games. Press Ctrl+C to stop.[/green]")
    try:
        watcher.watch_loop()
    except KeyboardInterrupt:
        console.print("[yellow]Stopped watching.[/yellow]")
    finally:
        lock.close()


@app.command()
def auto(
    ctx: typer.Context,
    install: bool = typer.Option(False, "--install", help="Add to Windows startup so it runs automatically on boot"),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove from Windows startup"),
    interval: float = typer.Option(5.0, "--interval", help="Polling interval in seconds"),
    periodic: float = typer.Option(600.0, "--periodic", help="Periodic backup interval in seconds during gameplay (0=off)"),
    no_wizard: bool = typer.Option(
        False, "--no-wizard",
        help="Exit with an error instead of launching setup when unconfigured (used by autostart)",
    ),
) -> None:
    """Fully automatic cloud backup: scans for Hydra/manual games, watches them, and backs up to your configured cloud storage.

    Run with --install to make it start automatically on Windows boot.
    """
    if uninstall:
        _uninstall_startup()
        return

    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)

    # First run: walk through cloud setup instead of erroring out. The
    # autostart entry passes --no-wizard because its console is hidden —
    # isatty() is still True there, and a wizard prompt would block forever
    # on a console nobody can see.
    if not config.rclone_remote_name or not config.cloud_provider:
        message = (
            "Cloud storage not configured. Run 'gsg' for guided setup, "
            "or 'gsg setup-drive' / 'gsg setup-railway'."
        )
        if no_wizard or not sys.stdin.isatty():
            try:
                setup_file_logging(get_data_dir() / "logs")
            except OSError:
                pass
            logging.getLogger(__name__).error(message)
            console.print(f"[red]{message}[/red]")
            raise typer.Exit(1)
        if not _run_setup_wizard(ctx):
            console.print(
                "[yellow]Cloud storage is required for gsg auto. "
                "Run 'gsg' again to set it up.[/yellow]"
            )
            raise typer.Exit(1)
        config = load_config(config_path)

    if install:
        _install_startup(config_path)
        return

    setup_file_logging(get_data_dir() / "logs")

    # Scan for non-launcher (Hydra/manual) games
    from .launcher import detect_launcher, get_all_launcher_games

    console.print("[cyan]Scanning for Hydra/manual games...[/cyan]")
    ludusavi_path = get_ludusavi_path(config_path)
    data = scan_games(ludusavi_path)
    games_data = data.get("games", {})
    steam_games, epic_games, xbox_games = get_all_launcher_games()

    # Build game list: only non-Steam/Epic/Xbox games
    existing_games = load_games(config_path)
    existing_ids = {g.id for g in existing_games}
    existing_titles = {g.title for g in existing_games}
    new_games: list[Game] = []

    for title, info in games_data.items():
        files = info.get("files", {})
        save_paths = list(files.keys())
        detected = detect_launcher(title, save_paths, steam_games, epic_games, xbox_games)
        if detected != "other":
            continue

        game_id = _slugify(title)
        if not game_id or game_id in existing_ids or title in existing_titles:
            continue

        game = Game(
            id=game_id,
            title=title,
            platform=_current_platform(),
            cloud_provider=config.cloud_provider or CloudProvider.S3,
            auto_sync=True,
        )
        new_games.append(game)

    if new_games:
        all_games = existing_games + new_games
        save_games(all_games, config_path)
        console.print(f"[green]Auto-added {len(new_games)} game(s):[/green]")
        for g in new_games:
            console.print(f"  - {g.title}")
    else:
        console.print("[dim]No new games found. Using existing tracked games.[/dim]")

    # Load all tracked games for watching
    all_tracked = _watchable_games(load_games(config_path))
    if not all_tracked:
        console.print("[yellow]No games to watch. Play some games and run 'gsg auto' again.[/yellow]")
        raise typer.Exit(1)

    lock = _acquire_instance_lock()
    if lock is None:
        console.print("[red]Another gsg watcher is already running.[/red]")
        raise typer.Exit(1)

    db = Database(get_data_dir() / "versions.db")
    rclone_path = get_rclone_path(config_path)

    def on_start(game: Game, proc_info: ProcessInfo) -> None:
        console.print(f"[green]Game started: {game.title}[/green]")
        notify("Game started", game.title)
        # Only learn the executable when exactly one process matches —
        # otherwise a transient launcher/anti-cheat helper could be
        # persisted and the real game would never match again.
        if len(watcher.running_pids(game.id)) == 1:
            _remember_executable(game, proc_info, config_path)
        # Never restore under a live process; just tell the user.
        if _cloud_newer_version(game, config, db, rclone_path) is not None:
            notify(
                "Newer cloud save exists",
                f"{game.title}: not applied because the game is running. "
                f"It will be restored after you quit.",
            )

    def on_close(game: Game, proc_info: ProcessInfo) -> None:
        console.print(f"[cyan]Game closed: {game.title}. Backing up to cloud...[/cyan]")
        result = _run_backup(
            game, config, db, ludusavi_path,
            label=f"Auto-backup on {datetime.now()}", origin="auto",
        )
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version and result.files_changed > 0:
            _cloud_upload(ctx, game, result.version, dry_run=False)
            notify("Save backed up", game.title)

    def on_periodic(game: Game) -> None:
        console.print(f"[cyan]Periodic backup: {game.title}...[/cyan]")
        result = _run_backup(
            game, config, db, ludusavi_path,
            label=f"Periodic backup on {datetime.now()}", origin="auto",
        )
        if result.success and result.version and result.files_changed > 0:
            console.print(f"[green]{result.message}[/green]")
            _cloud_upload(ctx, game, result.version, dry_run=False)
            notify("Save backed up", game.title)
        else:
            console.print(f"[dim]{result.message}[/dim]")

    def on_idle(game: Game) -> None:
        _auto_restore_if_idle(game, config, db, rclone_path, ludusavi_path)

    # Cloud restores only ever run for games that are NOT running: once at
    # startup, then at every idle check. Restoring on game start would race
    # the live process (it loads the old save, then overwrites the restored
    # files on exit).
    idle_interval = periodic if periodic > 0 else 600.0
    watcher = GameWatcher(all_tracked, periodic_interval=periodic, idle_interval=idle_interval)
    watcher.set_on_game_start(on_start)
    watcher.set_on_game_close(on_close)
    if periodic > 0:
        watcher.set_on_periodic_backup(on_periodic)
    watcher.set_on_idle_check(on_idle)
    watcher.prime()

    console.print("[cyan]Checking cloud for newer saves...[/cyan]")
    for game in all_tracked:
        if not watcher.is_running(game.id):
            _auto_restore_if_idle(game, config, db, rclone_path, ludusavi_path)

    console.print(f"\n[green]Auto-backup active. Watching {len(all_tracked)} game(s).[/green]")
    if periodic > 0:
        console.print(f"[dim]Periodic backup every {int(periodic)}s during gameplay.[/dim]")
    console.print("[dim]Press Ctrl+C to stop. Run 'gsg auto --install' to start on boot.[/dim]\n")
    try:
        watcher.watch_loop(interval=interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/yellow]")
    finally:
        lock.close()


def _startup_vbs_path() -> Path:
    startup_dir = (
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs" / "Startup"
    )
    return startup_dir / "GameSaveGenie.vbs"


def _find_gsg_exe() -> Optional[Path]:
    """Locate the gsg executable for autostart, or None."""
    if getattr(sys, "frozen", False):
        # Running as a PyInstaller bundle: this process IS gsg.exe.
        return Path(sys.executable)
    candidate = Path(sys.executable).parent / "gsg.exe"
    if candidate.exists():
        return candidate
    import shutil

    which = shutil.which("gsg")
    if which:
        return Path(which)
    project_venv = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "gsg.exe"
    if project_venv.exists():
        return project_venv
    return None


def _install_startup(config_path: Optional[Path] = None) -> None:
    """Install gsg auto to run hidden at logon via the user's Startup folder.

    A Startup-folder script needs no elevation and no console window.
    (A Task Scheduler ONLOGON task was considered and rejected: schtasks
    requires elevation and pins a visible console window to every logon.)
    The script passes --no-wizard so an unconfigured boot run exits with a
    logged error instead of blocking forever on an invisible prompt, and it
    checks the executable still exists so a moved exe fails silently rather
    than popping an error dialog at every logon.
    """
    if os.name != "nt":
        console.print("[yellow]Autostart install is currently Windows-only.[/yellow]")
        raise typer.Exit(1)

    gsg_path = _find_gsg_exe()
    if gsg_path is None:
        console.print(
            "[red]Could not locate the gsg executable; autostart not installed. "
            "Install the package (pip install .) or use the standalone gsg.exe.[/red]"
        )
        raise typer.Exit(1)

    temp_dir = os.environ.get("TEMP", "")
    if temp_dir and str(gsg_path).lower().startswith(temp_dir.lower()):
        console.print(
            "[yellow]gsg.exe is running from a temporary folder. Move it somewhere "
            "permanent and re-run 'gsg auto --install', or autostart will stop "
            "working when the folder is cleaned up.[/yellow]"
        )

    # Inside a VBS double-quoted string a literal quote is doubled ("").
    command = f'""{gsg_path}""'
    if config_path is not None:
        command += f' --config ""{config_path}""'
    command += " auto --no-wizard"

    vbs_path = _startup_vbs_path()
    vbs_path.parent.mkdir(parents=True, exist_ok=True)
    vbs_content = (
        "On Error Resume Next\n"
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        f'If fso.FileExists("{gsg_path}") Then\n'
        '    Set WshShell = CreateObject("WScript.Shell")\n'
        f'    WshShell.Run "{command}", 0, False\n'
        "End If\n"
    )
    vbs_path.write_text(vbs_content, encoding="utf-8")
    console.print(f"[green]Installed to Windows startup: {vbs_path}[/green]")
    console.print(
        f"[dim]Runs '{gsg_path}' hidden at logon. Moving the executable breaks "
        f"autostart — re-run 'gsg auto --install' after moving it.[/dim]"
    )


def _uninstall_startup() -> None:
    """Remove gsg auto from Windows startup."""
    vbs_path = _startup_vbs_path()
    if vbs_path.exists():
        vbs_path.unlink()
        console.print(f"[green]Removed from Windows startup: {vbs_path}[/green]")
    else:
        console.print("[yellow]No autostart entry found.[/yellow]")


@app.command(name="config")
def config_cmd(
    ctx: typer.Context,
    backup_dir: Optional[Path] = typer.Option(None, "--backup-dir", help="Set backup directory"),
    max_versions: Optional[int] = typer.Option(None, "--max-versions", help="Max versions to keep"),
    cloud_provider: Optional[CloudProvider] = typer.Option(None, "--cloud-provider", help="Default cloud provider"),
    rclone_remote_name: Optional[str] = typer.Option(None, "--rclone-remote", help="Name of the rclone remote"),
    remote_root: Optional[str] = typer.Option(None, "--remote-root", help="Remote root path or bucket"),
    ludusavi_path: Optional[Path] = typer.Option(None, "--ludusavi", help="Path to ludusavi binary"),
    rclone_path: Optional[Path] = typer.Option(None, "--rclone", help="Path to rclone binary"),
    storage_limit: Optional[float] = typer.Option(
        None, "--storage-limit", help="Cloud storage limit in GB for usage warnings (0 = off)"
    ),
) -> None:
    """View configuration, or edit it by passing options."""
    config_path = ctx.obj.get("config_path") or get_config_path()
    config = load_config(config_path)
    changed = False
    if backup_dir:
        config.backup_dir = backup_dir
        changed = True
    if max_versions is not None:
        config.max_versions = max_versions
        changed = True
    if cloud_provider:
        config.cloud_provider = cloud_provider
        changed = True
    if rclone_remote_name:
        config.rclone_remote_name = rclone_remote_name
        changed = True
    if remote_root:
        config.remote_root = remote_root
        changed = True
    if ludusavi_path:
        config.ludusavi_path = ludusavi_path
        changed = True
    if rclone_path:
        config.rclone_path = rclone_path
        changed = True
    if storage_limit is not None:
        config.storage_limit_gb = storage_limit
        changed = True

    if changed:
        save_config(config, config_path)
        console.print(f"[green]Configuration saved to {config_path}[/green]")
    else:
        console.print(f"[dim]Configuration at {config_path}[/dim]")
    console.print(f"backup_dir: {config.backup_dir}")
    console.print(f"max_versions: {config.max_versions}")
    console.print(f"cloud_provider: {config.cloud_provider}")
    console.print(f"rclone_remote_name: {config.rclone_remote_name}")
    console.print(f"remote_root: {config.remote_root}")
    console.print(f"storage_limit_gb: {config.storage_limit_gb:g}")


@app.command()
def pause(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to pause"),
) -> None:
    """Exclude a game from watching/auto-backup without removing it."""
    _set_auto_sync(ctx, game_id, enabled=False)


@app.command()
def resume(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to resume"),
) -> None:
    """Re-enable watching/auto-backup for a paused game."""
    _set_auto_sync(ctx, game_id, enabled=True)


def _set_auto_sync(ctx: typer.Context, game_id: str, enabled: bool) -> None:
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    game = next((g for g in games if g.id == game_id), None)
    if not game:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)
    game.auto_sync = enabled
    save_games(games, config_path)
    state = "resumed" if enabled else "paused"
    console.print(f"[green]{game.title}: auto-sync {state}.[/green]")


@app.command()
def setup_rclone(
    ctx: typer.Context,
    remote_name: str = typer.Argument(..., help="Name for the rclone remote"),
) -> None:
    """Launch rclone config to set up a cloud remote."""
    config_path = ctx.obj.get("config_path")
    rclone_path = get_rclone_path(config_path)
    console.print(f"[cyan]Launching rclone config for remote '{remote_name}'...[/cyan]")
    console.print(
        f"Follow the interactive prompts. When done, run: gsg config --rclone-remote {remote_name}"
    )
    run_rclone(rclone_path, ["config"], capture_output=False, check=False)


@app.command()
def setup_railway(
    ctx: typer.Context,
    remote_name: str = typer.Argument(default="railway", help="Name for the rclone remote"),
    endpoint: str = typer.Option(..., prompt=True, help="Railway S3 endpoint URL"),
    access_key: str = typer.Option(..., prompt=True, hide_input=True, help="Access key"),
    secret_key: str = typer.Option(..., prompt=True, hide_input=True, help="Secret key"),
    bucket: str = typer.Option(..., prompt=True, help="Bucket name"),
    region: str = typer.Option("auto", help="Region"),
) -> None:
    """Configure rclone for Railway S3-compatible storage."""
    _setup_s3_endpoint(ctx, remote_name, endpoint, access_key, secret_key, bucket, region)


@app.command(name="setup-s3")
def setup_s3(
    ctx: typer.Context,
    remote_name: str = typer.Argument(default="s3", help="Name for the rclone remote"),
    endpoint: str = typer.Option(..., prompt=True, help="S3 endpoint URL (e.g. http://homelab:9000)"),
    access_key: str = typer.Option(..., prompt=True, hide_input=True, help="Access key"),
    secret_key: str = typer.Option(..., prompt=True, hide_input=True, help="Secret key"),
    bucket: str = typer.Option(..., prompt=True, help="Bucket name"),
    region: str = typer.Option("auto", help="Region"),
) -> None:
    """Connect any S3-compatible storage: self-hosted MinIO, Garage, Backblaze B2, AWS...

    See docker/README.md for running your own save server with docker compose.
    """
    _setup_s3_endpoint(ctx, remote_name, endpoint, access_key, secret_key, bucket, region)


def _setup_s3_endpoint(
    ctx: typer.Context,
    remote_name: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
) -> None:
    config_path = ctx.obj.get("config_path") or get_config_path()
    conf = _configure_railway(
        config_path, remote_name, endpoint, access_key, secret_key, bucket, region
    )
    if not _verify_railway_or_revert(config_path, remote_name, bucket):
        raise typer.Exit(1)
    console.print(f"rclone config written to: {conf}")
    console.print("Test it with: gsg backup <game-id>")


def _configure_railway(
    config_path: Path,
    remote_name: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
) -> Path:
    config = load_config(config_path)
    conf_path = write_railway_s3_config(
        remote_name, endpoint, access_key, secret_key, bucket, region
    )
    config.cloud_provider = CloudProvider.S3
    config.rclone_remote_name = remote_name
    config.remote_root = bucket
    save_config(config, config_path)
    return conf_path


@app.command(name="setup-drive")
def setup_drive(
    ctx: typer.Context,
    remote_name: str = typer.Argument(default="gdrive", help="Name for the rclone remote"),
) -> None:
    """Set up Google Drive via browser sign-in (no keys to copy)."""
    config_path = ctx.obj.get("config_path") or get_config_path()
    if not _setup_oauth_remote(
        config_path, remote_name, "drive", CloudProvider.GOOGLE_DRIVE, "Google Drive"
    ):
        raise typer.Exit(1)


@app.command(name="setup-onedrive")
def setup_onedrive(
    ctx: typer.Context,
    remote_name: str = typer.Argument(default="onedrive", help="Name for the rclone remote"),
) -> None:
    """Set up OneDrive via browser sign-in (no keys to copy)."""
    config_path = ctx.obj.get("config_path") or get_config_path()
    if not _setup_oauth_remote(
        config_path, remote_name, "onedrive", CloudProvider.ONEDRIVE, "OneDrive"
    ):
        raise typer.Exit(1)


def _list_rclone_remotes(rclone_path: Path) -> list[str]:
    """Names of configured rclone remotes (exact, without trailing colon)."""
    result = run_rclone(rclone_path, ["listremotes"], check=False)
    if result.returncode != 0:
        return []
    return [
        line.strip().rstrip(":")
        for line in (result.stdout or "").splitlines()
        if line.strip()
    ]


def _save_cloud_choice(config_path: Path, provider: CloudProvider, remote_name: str) -> None:
    config = load_config(config_path)
    config.cloud_provider = provider
    config.rclone_remote_name = remote_name
    save_config(config, config_path)


def _setup_oauth_remote(
    config_path: Path,
    remote_name: str,
    rclone_type: str,
    provider: CloudProvider,
    pretty: str,
) -> bool:
    """Create an rclone OAuth remote (browser consent flow) and save config."""
    rclone_path = get_rclone_path(config_path)

    if remote_name in _list_rclone_remotes(rclone_path):
        # Never silently clobber an existing remote (it may hold another
        # tool's credentials, or be a different provider entirely).
        if typer.confirm(
            f"rclone remote '{remote_name}' already exists. Use it as configured?",
            default=True,
        ):
            _save_cloud_choice(config_path, provider, remote_name)
            console.print(f"[green]Using existing rclone remote '{remote_name}'.[/green]")
            return True
        console.print(
            f"[yellow]Pick a different name, e.g.: gsg setup-{rclone_type} gsg-{rclone_type}[/yellow]"
        )
        return False

    console.print(
        f"[cyan]Setting up {pretty}. A browser window will open — "
        f"sign in and click Allow.[/cyan]"
    )
    result = run_rclone(
        rclone_path,
        ["config", "create", remote_name, rclone_type],
        capture_output=False,
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[red]{pretty} setup failed (rclone exit {result.returncode}).[/red]")
        return False
    if remote_name not in _list_rclone_remotes(rclone_path):
        console.print(f"[red]Remote '{remote_name}' was not created; setup incomplete.[/red]")
        return False

    _save_cloud_choice(config_path, provider, remote_name)
    config = load_config(config_path)
    console.print(
        f"[green]{pretty} configured.[/green] Backups will be stored in the "
        f"'{config.remote_root}' folder of your {pretty}."
    )
    return True


def _run_setup_wizard(ctx: typer.Context) -> bool:
    """Guided first-run setup. Returns True once cloud storage is configured."""
    config_path = ctx.obj.get("config_path") or get_config_path()
    console.print("\n[bold cyan]Welcome to Game Save Genie![/bold cyan]")
    console.print(
        "Your game saves will be backed up automatically to cloud storage you own.\n"
    )
    console.print("Where should backups go?")
    console.print("  [bold]1[/bold]  Google Drive  (free 15 GB, sign in via browser)")
    console.print("  [bold]2[/bold]  OneDrive      (free 5 GB, sign in via browser)")
    console.print("  [bold]3[/bold]  Railway S3    (advanced: endpoint + keys from railway.app)")
    console.print("  [bold]4[/bold]  Not now")
    import click

    choice = typer.prompt(
        "Choice", default="1", type=click.Choice(["1", "2", "3", "4"]),
        show_choices=False,
    )

    if choice == "1":
        ok = _setup_oauth_remote(
            config_path, "gdrive", "drive", CloudProvider.GOOGLE_DRIVE, "Google Drive"
        )
    elif choice == "2":
        ok = _setup_oauth_remote(
            config_path, "onedrive", "onedrive", CloudProvider.ONEDRIVE, "OneDrive"
        )
    elif choice == "3":
        endpoint = typer.prompt("Railway S3 endpoint URL")
        access_key = typer.prompt("Access key", hide_input=True)
        secret_key = typer.prompt("Secret key", hide_input=True)
        bucket = typer.prompt("Bucket name")
        _configure_railway(
            config_path, "railway", endpoint, access_key, secret_key, bucket, "auto"
        )
        ok = _verify_railway_or_revert(config_path, "railway", bucket)
    else:
        console.print("[dim]Skipped cloud setup. Run 'gsg' again any time.[/dim]")
        return False

    if ok and os.name == "nt" and typer.confirm(
        "Start Game Save Genie automatically at boot?", default=True
    ):
        try:
            _install_startup(ctx.obj.get("config_path"))
        except typer.Exit:
            pass  # could not locate gsg.exe; the message was already printed
    return ok


def _verify_railway_or_revert(config_path: Path, remote_name: str, bucket: str) -> bool:
    """Confirm the S3 credentials actually work; revert config if they don't.

    Without this, a pasted typo would be declared 'configured', the wizard
    would never run again, and every upload would fail invisibly at runtime.
    """
    rclone_path = get_rclone_path(config_path)
    result = run_rclone(rclone_path, ["lsd", f"{remote_name}:{bucket}"], check=False)
    if result.returncode == 0:
        console.print("[green]Railway S3 configured and verified.[/green]")
        return True
    console.print(
        f"[red]Could not access the bucket with those credentials:[/red]\n"
        f"{(result.stderr or result.stdout or '').strip()}\n"
        f"[yellow]Check the endpoint URL, keys, and bucket name, then try again.[/yellow]"
    )
    config = load_config(config_path)
    config.cloud_provider = None
    config.rclone_remote_name = None
    save_config(config, config_path)
    return False


@app.command()
def usage(ctx: typer.Context) -> None:
    """Show local backup and remote storage usage."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    db = Database(get_data_dir() / "versions.db")

    local_size = sum(
        f.stat().st_size for f in config.backup_dir.rglob("*") if f.is_file()
    ) if config.backup_dir.exists() else 0
    version_count = db.count_versions()

    table = Table(title="Storage Usage")
    table.add_column("Location")
    table.add_column("Objects")
    table.add_column("Size")
    table.add_row("Local backups", str(version_count), _human_size(local_size))

    if config.cloud_provider and config.rclone_remote_name:
        try:
            rclone_path = get_rclone_path(config_path)
            objects, remote_size = get_remote_size(
                rclone_path, config.rclone_remote_name, config.remote_root
            )
            table.add_row("Remote storage", str(objects), _human_size(remote_size))
        except RuntimeError as exc:
            table.add_row("Remote storage", "error", str(exc))

    console.print(table)


def _watchable_games(games: list[Game]) -> list[Game]:
    """Filter to games the watcher should act on, honoring per-game flags."""
    watched = [g for g in games if g.auto_sync and g.sync_enabled]
    skipped = len(games) - len(watched)
    if skipped:
        console.print(f"[dim]{skipped} game(s) excluded (auto-sync paused).[/dim]")
    return watched


def _acquire_instance_lock() -> Optional[IO[str]]:
    """Take an exclusive watcher lock; None if another instance holds it.

    The returned handle must stay open for the watcher's lifetime — the OS
    releases the lock when the process exits, so a crashed watcher never
    leaves a stale lock behind.
    """
    lock_path = get_data_dir() / "gsg.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _snapshot_version(version: SaveVersion, config: SyncConfig) -> None:
    """Freeze the live backup dir into an immutable per-version zip.

    This is what makes 'gsg restore --version' real: without it every
    version row would point at the same directory, which each new backup
    overwrites in place.
    """
    zip_path = config.backup_dir / "_versions" / version.game_id / f"{version.id}.zip"
    digest = zip_directory(version.local_path, zip_path)
    version.local_path = zip_path
    version.sha256 = digest


# Safety backups are kept in their own small pool so they never evict real
# user/auto backups from the max_versions budget.
_MAX_SAFETY_VERSIONS = 3


def _run_backup(
    game: Game,
    config: SyncConfig,
    db: Database,
    ludusavi_path: Path,
    label: Optional[str] = None,
    origin: str = "user",
    protect_id: Optional[str] = None,
) -> BackupResult:
    result = backup_game(ludusavi_path, game, config.backup_dir, label)
    if result.success and result.version:
        try:
            _snapshot_version(result.version, config)
        except OSError as exc:
            console.print(
                f"[yellow]Snapshot failed for {game.title}: {exc}. "
                f"Version will reference the live backup directory.[/yellow]"
            )
        result.version.origin = origin
        db.add_version(result.version)
        _prune_old_versions(db, game.id, config.max_versions, protect_id=protect_id)
    return result


def _prune_old_versions(
    db: Database,
    game_id: str,
    max_versions: int,
    protect_id: Optional[str] = None,
) -> None:
    if max_versions < 1:
        return
    versions = db.get_versions(game_id)
    regular = [v for v in versions if v.origin != "safety"]
    safety = [v for v in versions if v.origin == "safety"]
    for old in regular[max_versions:] + safety[_MAX_SAFETY_VERSIONS:]:
        if old.id == protect_id:
            continue
        # Snapshot zips are per-version and safe to delete; legacy directory
        # paths are the shared live backup dir and must never be removed here.
        if old.local_path.suffix == ".zip" and old.local_path.is_file():
            try:
                old.local_path.unlink()
            except OSError as exc:
                # Locked by AV/indexer/another process: keep the DB row so
                # the next prune retries, and never crash the watcher.
                logging.getLogger(__name__).warning(
                    "Could not delete snapshot %s: %s", old.local_path, exc
                )
                continue
        db.delete_version(old.id)


def _materialize_version(version: SaveVersion, game: Game) -> Optional[Path]:
    """Stage a version's Ludusavi backup structure for restore, verified.

    Returns a directory ready to hand to ``ludusavi restore --path``, or
    None if the snapshot is missing or fails verification. Never touches
    live save files.
    """
    staging = get_data_dir() / "restore_staging" / version.game_id

    if version.local_path.is_dir():
        # Legacy pre-snapshot version: all such versions share the live
        # backup dir, which only holds the newest backup's content. Copy it
        # to staging so the following safety backup can't overwrite it.
        console.print(
            "[yellow]This version predates snapshot storage; restoring the newest "
            "backed-up content, which may be newer than the selected version.[/yellow]"
        )
        import shutil

        _reset_dir(staging)
        shutil.copytree(version.local_path, staging, dirs_exist_ok=True)
        return staging

    if not version.local_path.is_file():
        console.print(f"[red]Snapshot not found: {version.local_path}[/red]")
        return None
    if version.sha256 and sha256_file(version.local_path) != version.sha256:
        console.print(
            "[red]Snapshot failed its integrity check (sha256 mismatch); not restoring.[/red]"
        )
        return None

    _reset_dir(staging)
    try:
        safe_extract_zip(version.local_path, staging)
    except RuntimeError as exc:
        console.print(f"[red]Snapshot extraction failed: {exc}[/red]")
        return None
    return staging


def _cloud_upload(
    ctx: typer.Context,
    game: Game,
    version: SaveVersion,
    dry_run: bool,
) -> None:
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    provider = game.cloud_provider or config.cloud_provider
    if not provider:
        return
    rclone_path = get_rclone_path(config_path)
    remote_name = game.remote_path or config.rclone_remote_name
    if not remote_name:
        console.print("[red]No rclone remote configured.[/red]")
        return
    if dry_run:
        console.print(f"[cyan]Would upload {version.id} for {game.title}[/cyan]")
        return
    result = upload_save_cas(
        rclone_path,
        game,
        version,
        remote_name,
        config.remote_root,
        extra_args=config.custom_rclone_args,
    )
    console.print(f"[{'green' if result.success else 'red'}]{result.message}[/]")
    if result.success:
        db = Database(get_data_dir() / "versions.db")
        db.mark_cloud_synced(version.id, result.remote_path)
        pruned = prune_remote_versions(
            rclone_path, game, remote_name, config.remote_root, keep=config.max_versions
        )
        if pruned:
            console.print(f"[dim]Pruned {len(pruned)} old cloud version(s).[/dim]")


def _cloud_restore_dir(game_id: str) -> Path:
    """Staging directory for downloaded cloud saves (outside the backup tree)."""
    return get_data_dir() / "cloud_restore" / game_id


def _reset_dir(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _remember_executable(
    game: Game, proc_info: ProcessInfo, config_path: Optional[Path]
) -> None:
    """Persist the matched process name so future matches are exact, not fuzzy."""
    if game.executable_names or not proc_info.name:
        return
    games = load_games(config_path)
    for tracked in games:
        if tracked.id == game.id and not tracked.executable_names:
            tracked.executable_names = [proc_info.name]
            game.executable_names = [proc_info.name]
            save_games(games, config_path)
            console.print(f"[dim]Learned executable for {game.title}: {proc_info.name}[/dim]")
            break


def _cloud_newer_version(
    game: Game,
    config: SyncConfig,
    db: Database,
    rclone_path: Path,
) -> Optional[str]:
    """Return the cloud's latest version id when it is strictly newer than
    anything this machine has produced or applied; None otherwise.

    Safety backups are excluded from the local side so a pre-restore snapshot
    can never lock the cloud out, and the last applied cloud version (from
    sync_state) counts as local knowledge so the same save is not re-restored.
    """
    if not game.cloud_provider:
        return None
    remote_name = game.remote_path or config.rclone_remote_name
    if not remote_name:
        return None
    try:
        cloud_ids = list_remote_versions(rclone_path, game, remote_name, config.remote_root)
    except RuntimeError as exc:
        console.print(f"[dim]Cloud check failed for {game.title}: {exc}[/dim]")
        return None
    cloud_latest = latest_version_id(cloud_ids)
    local_latest = db.get_latest_version_id(game.id, exclude_safety=True)
    effective_local = effective_local_latest(local_latest, db.get_sync_state(game.id))
    if cloud_latest is None or not should_restore_from_cloud(effective_local, cloud_latest):
        return None
    return cloud_latest


def _auto_restore_if_idle(
    game: Game,
    config: SyncConfig,
    db: Database,
    rclone_path: Path,
    ludusavi_path: Path,
) -> None:
    """Apply the latest cloud save when it is newer — ONLY for a game that is
    not currently running (callers guarantee that; restoring under a live
    process would race the game's own save writes).
    """
    cloud_latest = _cloud_newer_version(game, config, db, rclone_path)
    if cloud_latest is None:
        return
    console.print(f"[cyan]{game.title}: cloud has a newer save. Downloading...[/cyan]")
    _apply_cloud_version(game, config, db, rclone_path, ludusavi_path, cloud_latest)


def _apply_cloud_version(
    game: Game,
    config: SyncConfig,
    db: Database,
    rclone_path: Path,
    ludusavi_path: Path,
    version_id: str,
) -> bool:
    """Download, verify, remap, and restore one cloud version.

    Ordering is deliberate: download and verify FIRST, then remap paths for
    this machine, then the safety backup, then apply — and the restore is
    aborted if the safety backup fails, so local progress is never
    overwritten without a recoverable copy. A failed step changes no state.
    The applied cloud version is recorded in sync_state so automatic
    restore never re-applies it.
    """
    remote_name = game.remote_path or config.rclone_remote_name
    if not remote_name:
        return False

    restore_dir = _cloud_restore_dir(game.id)
    _reset_dir(restore_dir)
    result = download_save(
        rclone_path, game, version_id, restore_dir, remote_name, config.remote_root
    )
    if not result.success:
        console.print(f"[red]{result.message}[/red]")
        return False
    mapping_files = list(restore_dir.rglob("mapping.yaml"))
    if not mapping_files:
        console.print(
            f"[red]{game.title}: downloaded save has no Ludusavi mapping.yaml; not restoring.[/red]"
        )
        return False

    # Cross-machine: a backup made under another Windows username records
    # that user's profile paths — rewrite them (and the mirrored files) for
    # this machine before Ludusavi applies them.
    try:
        remapped = sum(
            apply_remap_to_staged_backup(mp.parent) for mp in mapping_files
        )
    except (OSError, RuntimeError, yaml.YAMLError) as exc:
        console.print(
            f"[red]{game.title}: could not remap save paths for this machine "
            f"({exc}); not restoring.[/red]"
        )
        return False
    if remapped:
        console.print(f"[dim]Remapped {remapped} save path(s) for this machine.[/dim]")

    # Download verified — secure the current on-disk state before applying.
    # If that fails, DO NOT restore: overwriting the only copy of local
    # progress without a recoverable backup is the one unforgivable failure.
    safety = _run_backup(
        game, config, db, ludusavi_path,
        label="Safety backup before cloud restore", origin="safety",
    )
    if not safety.success:
        console.print(
            f"[red]{game.title}: safety backup failed ({safety.message}); "
            f"cloud restore skipped.[/red]"
        )
        return False

    try:
        restore_from_backup(ludusavi_path, game, restore_dir)
    except RuntimeError as exc:
        console.print(f"[red]Restore failed for {game.title}: {exc}[/red]")
        return False

    _record_applied_cloud_version(db, game.id, version_id)
    notify("Cloud save restored", game.title)
    console.print(f"[green]Restored {game.title} from cloud version {version_id}[/green]")
    return True


def _record_applied_cloud_version(db: Database, game_id: str, version_id: str) -> None:
    """Advance sync_state to the applied version — never backwards, so
    deliberately pulling an old version cannot make auto-restore loop."""
    current = db.get_sync_state(game_id)
    if current is None or version_id > current:
        db.update_sync_state(game_id, version_id)


def _slugify(text: str) -> str:
    # Per-character replacement (not run-collapsing) keeps ids byte-identical
    # to those already stored in existing games.yaml files — changing the
    # scheme would re-add every tracked game under a new id. isalnum() keeps
    # Unicode titles (CJK/Cyrillic) working; the hash fallback covers titles
    # with no alphanumerics at all.
    slug = "".join(c if c.isalnum() else "-" for c in text.strip().lower()).strip("-")
    if not slug:
        import hashlib

        slug = "game-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return slug


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ["KiB", "MiB", "GiB", "TiB"]:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} TiB"
