"""Command line interface for Game Save Genie."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__  # noqa: F401
from .cloud import (
    get_rclone_path,
    get_remote_size,
    list_remote_versions,
    run_rclone,
    upload_save,
    write_railway_s3_config,
)
from .config import (
    get_config_path,
    get_data_dir,
    load_config,
    load_games,
    save_config,
    save_games,
)
from .database import Database
from .ludusavi import backup_game, get_ludusavi_path, restore_game, scan_games
from .models import BackupResult, CloudProvider, Game, Platform, SaveVersion, SyncConfig
from .remap import _current_platform
from .watcher import GameWatcher

app = typer.Typer(help="Game Save Genie - self-hosted cloud save sync")
console = Console()


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config file"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """Global options."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


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
        size = sum(f.get("size", 0) for f in files.values())
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
        result = _run_backup(game, config, db, ludusavi_path, label)
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version and not no_cloud and game.cloud_provider:
            _cloud_upload(ctx, game, result.version, dry_run)


@app.command()
def restore(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to restore"),
    version_id: Optional[str] = typer.Option(None, "--version", help="Version ID (omit for latest)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
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

    if not version:
        console.print(f"[red]No local version found for {game_id}.[/red]")
        raise typer.Exit(1)

    if dry_run:
        console.print(f"[cyan]Would restore version {version.id} for {game.title}[/cyan]")
        return

    ludusavi_path = get_ludusavi_path(config_path)
    restore_game(ludusavi_path, game, version)
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

    rclone_path = get_rclone_path(config_path)
    remote_name = game.remote_path or config.rclone_remote_name or config.remote_root
    version_ids = list_remote_versions(rclone_path, game, remote_name, config.remote_root)
    if not version_ids:
        console.print("[yellow]No cloud versions found.[/yellow]")
        return
    for vid in version_ids:
        console.print(vid)


@app.command()
def watch(ctx: typer.Context) -> None:
    """Watch running games and auto-backup on close."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    if not config.auto_sync_on_game_close:
        console.print("[yellow]Auto-sync on game close is disabled in config.[/yellow]")
        raise typer.Exit(1)

    db = Database(get_data_dir() / "versions.db")
    ludusavi_path = get_ludusavi_path(config_path)

    def on_close(game: Game, proc_info: object) -> None:
        console.print(f"[cyan]Game closed: {game.title}. Backing up...[/cyan]")
        result = _run_backup(game, config, db, ludusavi_path, label=f"Auto-backup on {datetime.now()}")
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version and game.cloud_provider:
            _cloud_upload(ctx, game, result.version, dry_run=False)

    watcher = GameWatcher(games)
    watcher.set_on_game_close(on_close)
    console.print("[green]Watching for games. Press Ctrl+C to stop.[/green]")
    try:
        watcher.watch_loop()
    except KeyboardInterrupt:
        console.print("[yellow]Stopped watching.[/yellow]")


@app.command()
def auto(
    ctx: typer.Context,
    install: bool = typer.Option(False, "--install", help="Add to Windows startup so it runs automatically on boot"),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove from Windows startup"),
    interval: float = typer.Option(5.0, "--interval", help="Polling interval in seconds"),
) -> None:
    """Fully automatic cloud backup. Scans for Hydra/manual games, watches them, and backs up to Railway S3 on close.

    Run with --install to make it start automatically on Windows boot.
    """
    if uninstall:
        _uninstall_startup()
        return

    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)

    # Verify Railway S3 is configured
    if not config.rclone_remote_name or not config.cloud_provider:
        console.print("[red]Cloud storage not configured. Run 'gsg setup-railway' first.[/red]")
        raise typer.Exit(1)

    if install:
        _install_startup()
        return

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
    new_games: list[Game] = []

    for title, info in games_data.items():
        files = info.get("files", {})
        save_paths = list(files.keys())
        detected = detect_launcher(title, save_paths, steam_games, epic_games, xbox_games)
        if detected != "other":
            continue

        game_id = _slugify(title)
        if game_id in existing_ids:
            continue

        game = Game(
            id=game_id,
            title=title,
            platform=_current_platform(),
            cloud_provider=CloudProvider.S3,
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
    all_tracked = load_games(config_path)
    if not all_tracked:
        console.print("[yellow]No games to watch. Play some games and run 'gsg auto' again.[/yellow]")
        raise typer.Exit(1)

    db = Database(get_data_dir() / "versions.db")

    def on_start(game: Game, proc_info: object) -> None:
        console.print(f"[green]Game started: {game.title}[/green]")

    def on_close(game: Game, proc_info: object) -> None:
        console.print(f"[cyan]Game closed: {game.title}. Backing up to Railway S3...[/cyan]")
        result = _run_backup(game, config, db, ludusavi_path, label=f"Auto-backup on {datetime.now()}")
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version and result.files_changed > 0:
            _cloud_upload(ctx, game, result.version, dry_run=False)

    watcher = GameWatcher(all_tracked)
    watcher.set_on_game_start(on_start)
    watcher.set_on_game_close(on_close)

    console.print(f"\n[green]Auto-backup active. Watching {len(all_tracked)} game(s).[/green]")
    console.print("[dim]Press Ctrl+C to stop. Run 'gsg auto --install' to start on boot.[/dim]\n")
    try:
        watcher.watch_loop(interval=interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/yellow]")


def _install_startup() -> None:
    """Install gsg auto as a Windows startup (hidden VBS wrapper)."""
    startup_dir = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)

    vbs_path = startup_dir / "GameSaveGenie.vbs"
    # Find the gsg executable
    import sys
    gsg_path = Path(sys.executable).parent / "gsg.exe"
    if not gsg_path.exists():
        # Try to find it in the venv
        project_venv = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "gsg.exe"
        gsg_path = project_venv if project_venv.exists() else gsg_path

    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """{gsg_path}"" auto", 0, False
'''
    vbs_path.write_text(vbs_content, encoding="utf-8")
    console.print(f"[green]Installed to Windows startup: {vbs_path}[/green]")
    console.print("[dim]Game Save Genie will auto-start on boot and back up saves in the background.[/dim]")


def _uninstall_startup() -> None:
    """Remove gsg auto from Windows startup."""
    startup_dir = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    vbs_path = startup_dir / "GameSaveGenie.vbs"
    if vbs_path.exists():
        vbs_path.unlink()
        console.print(f"[green]Removed from Windows startup: {vbs_path}[/green]")
    else:
        console.print("[yellow]Not found in startup.[/yellow]")


@app.command()
def config_cmd(
    ctx: typer.Context,
    backup_dir: Optional[Path] = typer.Option(None, "--backup-dir", help="Set backup directory"),
    max_versions: Optional[int] = typer.Option(None, "--max-versions", help="Max versions to keep"),
    cloud_provider: Optional[CloudProvider] = typer.Option(None, "--cloud-provider", help="Default cloud provider"),
    rclone_remote_name: Optional[str] = typer.Option(None, "--rclone-remote", help="Name of the rclone remote"),
    remote_root: Optional[str] = typer.Option(None, "--remote-root", help="Remote root path or bucket"),
    ludusavi_path: Optional[Path] = typer.Option(None, "--ludusavi", help="Path to ludusavi binary"),
    rclone_path: Optional[Path] = typer.Option(None, "--rclone", help="Path to rclone binary"),
) -> None:
    """View or edit configuration."""
    config_path = ctx.obj.get("config_path") or get_config_path()
    config = load_config(config_path)
    if backup_dir:
        config.backup_dir = backup_dir
    if max_versions is not None:
        config.max_versions = max_versions
    if cloud_provider:
        config.cloud_provider = cloud_provider
    if rclone_remote_name:
        config.rclone_remote_name = rclone_remote_name
    if remote_root:
        config.remote_root = remote_root
    if ludusavi_path:
        config.ludusavi_path = ludusavi_path
    if rclone_path:
        config.rclone_path = rclone_path
    save_config(config, config_path)
    console.print(f"[green]Configuration saved to {config_path}[/green]")
    console.print(f"backup_dir: {config.backup_dir}")
    console.print(f"max_versions: {config.max_versions}")
    console.print(f"cloud_provider: {config.cloud_provider}")
    console.print(f"rclone_remote_name: {config.rclone_remote_name}")
    console.print(f"remote_root: {config.remote_root}")


@app.command()
def setup_rclone(
    ctx: typer.Context,
    remote_name: str = typer.Argument(..., help="Name for the rclone remote"),
) -> None:
    """Launch rclone config to set up a cloud remote."""
    config_path = ctx.obj.get("config_path")
    rclone_path = get_rclone_path(config_path)
    console.print(f"[cyan]Launching rclone config for remote '{remote_name}'...[/cyan]")
    console.print("Follow the interactive prompts. When done, set the remote name with --remote.")
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
    config_path = ctx.obj.get("config_path") or get_config_path()
    config = load_config(config_path)
    config_path_obj = write_railway_s3_config(remote_name, endpoint, access_key, secret_key, bucket, region)
    config.cloud_provider = CloudProvider.S3
    config.rclone_remote_name = remote_name
    config.remote_root = bucket
    save_config(config, config_path)
    console.print(f"[green]Railway S3 remote '{remote_name}' configured.[/green]")
    console.print(f"rclone config written to: {config_path_obj}")
    console.print("Test it with: gsg backup <game-id>")


@app.command()
def usage(ctx: typer.Context) -> None:
    """Show local backup and remote storage usage."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    db = Database(get_data_dir() / "versions.db")

    local_size = sum(
        f.stat().st_size for f in config.backup_dir.rglob("*") if f.is_file()
    )
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


def _run_backup(
    game: Game,
    config: SyncConfig,
    db: Database,
    ludusavi_path: Path,
    label: Optional[str] = None,
) -> BackupResult:
    result = backup_game(ludusavi_path, game, config.backup_dir, label)
    if result.success and result.version:
        db.add_version(result.version)
        _prune_old_versions(db, game.id, config.max_versions)
    return result


def _prune_old_versions(db: Database, game_id: str, max_versions: int) -> None:
    versions = db.get_versions(game_id)
    if len(versions) <= max_versions:
        return
    for old in versions[max_versions:]:
        db.delete_version(old.id)


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
    result = upload_save(
        rclone_path,
        game,
        version,
        remote_name,
        config.remote_root,
        dry_run=dry_run,
        extra_args=config.custom_rclone_args,
    )
    console.print(f"[{'green' if result.success else 'red'}]{result.message}[/]")
    if result.success:
        db = Database(get_data_dir() / "versions.db")
        db.mark_cloud_synced(version.id, result.remote_path)


def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.strip().lower()).strip("-")


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ["KiB", "MiB", "GiB", "TiB"]:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} TiB"


def run() -> None:
    app()
