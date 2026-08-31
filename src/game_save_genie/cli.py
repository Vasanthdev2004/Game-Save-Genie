"""Command line interface for Game Save Genie."""

from __future__ import annotations

import codecs
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

import click
import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__, custom
from . import tray as tray_mod
from .archive import safe_extract_zip, sha256_file, zip_directory
from .cloud import (
    _remote_path,
    download_save,
    endpoint_has_scheme,
    get_rclone_path,
    get_remote_size,
    list_remote_versions,
    normalize_endpoint,
    prune_remote_versions,
    run_rclone,
    upload_save_cas,
    write_s3_config,
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
    CLOUD_PLATFORMS,
    backup_game,
    cloud_platforms_for_titles,
    get_ludusavi_path,
    preview_backup,
    restore_from_backup,
    scan_games,
    titles_without_save_data,
)
from .models import (
    BackupResult,
    CloudProvider,
    Game,
    GameSavePath,
    Platform,
    ProcessInfo,
    SaveVersion,
    SyncConfig,
)
from .notify import notify, setup_file_logging
from .remap import _current_platform, apply_remap_to_staged_backup
from .sync import effective_local_latest, latest_version_id, should_restore_from_cloud
from .watcher import GameWatcher, is_helper_executable, unmatchable_reason

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
    config: Path | None = typer.Option(None, "--config", help="Path to config file"),
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
        # Bare `gsg`: first run gets the guided setup, after that the
        # dashboard. The Windows Start Menu shortcut runs a bare `gsg`, so
        # this is what someone sees when they launch the app by clicking it —
        # a wall of command-line help was the wrong answer to that.
        # The wizard's outcome is terminal for this invocation either way. A
        # declined or failed setup used to fall through, and the full-screen
        # dashboard covered the explanation before it could be read — leaving
        # someone looking at a professional-looking app that backs up nothing.
        if not _cloud_configured(config) and _is_interactive():
            if _run_setup_wizard(ctx):
                console.print(
                    "\n[green]Setup complete![/green] Run [bold]gsg auto[/bold] to start "
                    "automatic backup, or [bold]gsg[/bold] to open the dashboard."
                )
            else:
                console.print(
                    "\n[yellow]Cloud storage is not set up, so nothing is being backed "
                    "up yet.[/yellow] [dim]Run 'gsg' again when you are ready.[/dim]"
                )
            return
        if _open_dashboard(config):
            return
        console.print(ctx.get_help())


def _is_interactive() -> bool:
    """Whether there is a human at a terminal to prompt.

    A named seam so the entry point's branches are testable — it is the code
    path the Start Menu shortcut takes, so it is worth pinning down.
    """
    return sys.stdin.isatty()


def _dashboard_available() -> bool:
    """Whether the TUI can run here: a real terminal and Textual installed."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Piped or redirected: help is readable and scriptable, a TUI is not.
        return False
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        logging.getLogger(__name__).info("Dashboard unavailable: %s", exc)
        return False
    return True


def _open_dashboard(config_path: Path | None) -> bool:
    """Run the dashboard. Returns False if it could not be opened or crashed.

    A False return is a signal to fall back to help, never an error — a bare
    `gsg` in a pipe, on a machine without Textual, or after a dashboard that
    died on startup must still do something sensible.
    """
    if not _dashboard_available():
        return False
    from . import ui

    # Textual owns the terminal for the duration, so nothing else may write to
    # it. The root logger's console handler otherwise paints over the running
    # app — "Downloading rclone..." straight across the layout on first run.
    # FileHandler subclasses StreamHandler, so it must be excluded explicitly —
    # the file log should keep recording while the dashboard is up.
    root = logging.getLogger()
    detached = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    for handler in detached:
        root.removeHandler(handler)
    try:
        return ui.run(config_path)
    finally:
        for handler in detached:
            root.addHandler(handler)


def _now_label() -> str:
    """Local wall-clock time for a human-readable backup label.

    Aware-but-local on purpose: the label is for reading, while a version's
    ``created_at`` stays UTC.
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _cloud_configured(config_path: Path | None) -> bool:
    config = load_config(config_path)
    return bool(config.rclone_remote_name and config.cloud_provider)


# --- Cloud resolution -------------------------------------------------------
# A game's cloud settings are an *override* of the global config, never a
# separate source of truth. Every caller must agree on how they combine, or
# the same game ends up cloud-enabled for one command and invisible to
# another (uploads that can never be pulled back down, purges that miss).


def _effective_provider(game: Game, config: SyncConfig) -> CloudProvider | None:
    """The provider actually in force for this game: per-game, else global."""
    return game.cloud_provider or config.cloud_provider


def _effective_remote(game: Game, config: SyncConfig) -> str | None:
    """The rclone remote *name* actually in force for this game."""
    return game.remote_path or config.rclone_remote_name


def _cloud_target(game: Game, config: SyncConfig) -> str | None:
    """Where this game's saves really go (``remote:root``), or None if nowhere.

    Derived on every call so it cannot drift from the config the way a stored
    label does — `game.cloud_provider` is only ever a hint, never a destination.
    """
    remote = _effective_remote(game, config)
    if not _effective_provider(game, config) or not remote:
        return None
    return f"{remote}:{config.remote_root}"


def _sync_display(version: SaveVersion, game: Game, config: SyncConfig) -> str:
    """How a version's cloud state should read *today*.

    ``cloud_synced`` is only ever set, never cleared, so after a provider
    switch every old row still claims "yes" while the configured remote holds
    nothing. Compare the remote it was uploaded to against the one in force.
    """
    if not version.cloud_synced:
        return "pending"
    remote = _effective_remote(game, config)
    stored = version.cloud_remote_path or ""
    if remote and ":" in stored:
        uploaded_to = stored.split(":", 1)[0]
        if uploaded_to != remote:
            return f"stale ({uploaded_to})"
    return "yes"


@app.command()
def init(
    ctx: typer.Context,
    backup_dir: Path | None = typer.Option(None, help="Local backup directory"),
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
    skip_cloud_synced: str | None = typer.Option(
        None,
        "--skip-cloud-synced",
        metavar="STORES",
        help=(
            "Hide games the given stores sync themselves, e.g. 'steam' or "
            "'steam,gog'. This is about what the GAME supports, not what YOUR "
            "copy is covered by: a repack or an offline installer of a "
            "Steam Cloud game is synced by nothing."
        ),
    ),
) -> None:
    """Scan for installed games and their save locations.

    The Native Cloud column reports which stores provide their own save sync
    for each game, from Ludusavi's manifest. It is shown whether or not you
    filter on it, because knowing Steam already covers a game is useful even
    when you want gsg to cover it too.
    """
    from .launcher import detect_launcher, get_all_launcher_games

    skip_stores: set[str] = set()
    if skip_cloud_synced:
        skip_stores = {s.strip().lower() for s in skip_cloud_synced.split(",") if s.strip()}
        unknown = skip_stores - set(CLOUD_PLATFORMS)
        if unknown:
            console.print(
                f"[red]Unknown store(s): {', '.join(sorted(unknown))}.[/red] "
                f"[dim]Known: {', '.join(CLOUD_PLATFORMS)}.[/dim]"
            )
            raise typer.Exit(1)

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

    native_cloud = cloud_platforms_for_titles(set(games_data))
    skipped_for_cloud = 0

    table = Table(title="Detected Games")
    table.add_column("Title")
    table.add_column("Source")
    table.add_column("Native Cloud")
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

        synced_by = native_cloud.get(title, set())
        if skip_stores & synced_by:
            skipped_for_cloud += 1
            continue

        color = source_colors.get(detected, "white")
        table.add_row(
            title,
            f"[{color}]{detected}[/{color}]",
            ", ".join(sorted(synced_by)) if synced_by else "[dim]-[/dim]",
            str(len(files)),
            _human_size(size),
        )

    console.print(table)
    if skipped_for_cloud:
        console.print(
            f"[dim]Hid {skipped_for_cloud} game(s) synced by "
            f"{', '.join(sorted(skip_stores))}. This reflects what the game "
            f"supports, not what your copy is covered by.[/dim]"
        )
    if source != "all":
        console.print(
            f"[dim]Filtered by source: {source}. Use --source all to see every game.[/dim]"
        )


@app.command()
def add(
    ctx: typer.Context,
    title: str = typer.Argument(..., help="Game title"),
    executable: str | None = typer.Option(None, "--exe", help="Executable name to watch"),
    paths: list[Path] = typer.Option(
        [], "--path",
        help="Save folder or file to back up directly, bypassing Ludusavi "
        "(repeatable; for emulators / games Ludusavi doesn't know)",
    ),
    platform: Platform = typer.Option(_current_platform(), "--platform", help="Platform"),
    cloud: CloudProvider | None = typer.Option(None, "--cloud", help="Cloud provider"),
    remote_path: str | None = typer.Option(None, "--remote", help="Remote path/remote name"),
    no_auto_sync: bool = typer.Option(False, "--no-auto-sync", help="Disable auto-sync"),
) -> None:
    """Add a game to track.

    With one or more --path options the game is backed up by copying those
    exact locations (custom mode), instead of relying on Ludusavi's save
    database — the way to protect emulator saves and anything Ludusavi misses.
    """
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    game_id = _slugify(title)
    if any(g.id == game_id for g in games):
        console.print(f"[yellow]Game '{title}' is already tracked.[/yellow]")
        raise typer.Exit(1)

    # Resolve to absolute at add time so the stored location is unambiguous
    # regardless of the working directory a later backup/watcher runs from.
    save_paths = [GameSavePath(path=p.expanduser().resolve()) for p in paths]
    for sp in save_paths:
        if not sp.path.exists():
            console.print(
                f"[yellow]Note: {sp.path} does not exist yet — backups will find "
                f"nothing there until it does.[/yellow]"
            )
    game = Game(
        id=game_id,
        title=title,
        platform=platform,
        executable_names=[executable] if executable else [],
        save_paths=save_paths,
        custom=bool(save_paths),
        auto_sync=not no_auto_sync,
        cloud_provider=cloud,
        remote_path=remote_path,
    )
    games.append(game)
    save_games(games, config_path)
    mode = f"custom, {len(save_paths)} path(s)" if save_paths else "Ludusavi"
    console.print(f"[green]Added game: {title} ({game_id}) — {mode}[/green]")


@app.command(name="list")
def list_games(ctx: typer.Context) -> None:
    """List tracked games."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    if not games:
        console.print("[yellow]No games tracked.[/yellow]")
        return

    table = Table(title="Tracked Games")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Platform")
    table.add_column("Auto Sync")
    table.add_column("Cloud Target")

    for game in games:
        table.add_row(
            game.id,
            game.title,
            game.platform.value,
            "yes" if game.auto_sync else "no",
            _cloud_target(game, config) or "off",
        )
    console.print(table)


@app.command(name="set")
def set_game(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to change"),
    exe: list[str] | None = typer.Option(
        None, "--exe", help="Executable name to match (repeat for several)"
    ),
    clear_exe: bool = typer.Option(
        False, "--clear-exe", help="Forget the executables and match by title again"
    ),
) -> None:
    """Change how a tracked game is detected.

    The watcher learns a game's executable by watching it run, and can learn
    the wrong one — a launcher or crash handler that started first. This is
    the repair, so fixing it never means hand-editing games.yaml or removing
    and re-adding the game (one flag away from deleting its saves).
    """
    config_path = ctx.obj.get("config_path")
    games = load_games(config_path)
    game = next((g for g in games if g.id == game_id), None)
    if not game:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)
    if not exe and not clear_exe:
        console.print("[yellow]Nothing to change. Pass --exe or --clear-exe.[/yellow]")
        console.print(
            f"[dim]{game.title} currently matches: "
            f"{', '.join(game.executable_names) or 'by title'}[/dim]"
        )
        raise typer.Exit(1)

    if clear_exe:
        game.executable_names = []
        game.executables_learned = False
        console.print(f"[green]{game.title} will be matched by title again.[/green]")
    if exe:
        # Given explicitly, so it is a deliberate narrowing, not a guess:
        # title matching stops for this game.
        game.executable_names = list(exe)
        game.executables_learned = False
        console.print(f"[green]{game.title} now matches: {', '.join(exe)}[/green]")
    save_games(games, config_path)
    console.print("[dim]Restart 'gsg auto' for this to take effect.[/dim]")


@app.command(name="ui")
def ui_command(ctx: typer.Context) -> None:
    """Open the interactive dashboard: browse games and versions, restore by arrow key.

    Everything here calls the same code as the commands — including the
    pre-restore safety backup — so there is no second, weaker path to your
    save files.
    """
    config_path = ctx.obj.get("config_path")
    # Asked for explicitly, so failures are errors here — unlike a bare `gsg`,
    # which quietly falls back to help.
    #
    # The import is checked BEFORE the terminal check so the two failures are
    # distinguishable from a non-interactive shell. That is what lets the
    # packaging CI assert that Textual really is inside the frozen exe: a
    # bundle missing it reports the dependency, not the terminal.
    try:
        from . import ui
    except ImportError as exc:
        console.print(
            f"[red]The dashboard needs Textual, which is not installed ({exc}).[/red]\n"
            "[dim]Install it with: pip install textual[/dim]"
        )
        raise typer.Exit(1) from exc
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(
            "[red]'gsg ui' needs an interactive terminal. "
            "Use 'gsg status' / 'gsg versions' when piping output.[/red]"
        )
        raise typer.Exit(1)
    ui.run(config_path)


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

    config = load_config(config_path)
    remote_name = _effective_remote(game, config)

    if purge:
        # Confirm before untracking, while the game's own settings still
        # resolve — and name the exact remote path, because this deletes the
        # only remaining copy of every save for this game.
        targets = [
            str(config.backup_dir / game_id),
            str(config.backup_dir / "_versions" / game_id),
        ]
        if remote_name:
            targets.append(_remote_path(remote_name, config.remote_root, game_id))
        console.print(f"[red]--purge will permanently delete every save for {game.title}:[/red]")
        for target in targets:
            console.print(f"  {target}")
        if not remote_name:
            console.print(
                "  [yellow](no rclone remote configured — cloud copies, if any, will remain)"
                "[/yellow]"
            )
        if sys.stdin.isatty() and not typer.confirm("Delete these permanently?"):
            console.print("[yellow]Cancelled — nothing was removed.[/yellow]")
            raise typer.Exit(1)

    games = [g for g in games if g.id != game_id]
    save_games(games, config_path)
    console.print(f"[green]Removed: {game.title} ({game_id})[/green]")

    if purge:
        # Delete local backups (live backup dir + per-version snapshots)
        import shutil

        for local_dir in (
            config.backup_dir / game_id,
            config.backup_dir / "_versions" / game_id,
        ):
            if local_dir.exists():
                shutil.rmtree(local_dir, ignore_errors=True)
                console.print(f"  [dim]Deleted local backups: {local_dir}[/dim]")

        # Forget this game's rows too, or re-adding the same title resurrects
        # version history pointing at snapshots that were just deleted.
        db = Database(get_data_dir() / "versions.db")
        db.delete_game(game_id)

        # Delete cloud saves. Resolve the remote the same way uploads do —
        # using the global remote here would purge a *different* remote than
        # the one this game's saves were actually written to.
        if not remote_name:
            console.print(
                "  [yellow]No rclone remote configured — cloud saves (if any) were NOT "
                "deleted.[/yellow]"
            )
        else:
            remote = _remote_path(remote_name, config.remote_root, game_id)
            try:
                rclone_path = get_rclone_path(config_path)
                result = run_rclone(rclone_path, ["purge", remote], check=False)
                if result.returncode == 0:
                    console.print(f"  [dim]Deleted cloud saves: {remote}[/dim]")
                elif result.returncode == 3:
                    console.print(f"  [dim]No cloud saves found at {remote}[/dim]")
                else:
                    console.print(
                        f"  [yellow]Could not delete cloud saves at {remote} — they may "
                        f"still exist:[/yellow] {(result.stderr or '').strip()}"
                    )
            except RuntimeError as exc:
                console.print(
                    f"  [yellow]rclone unavailable — cloud saves at {remote} were NOT "
                    f"deleted ({exc}).[/yellow]"
                )


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
    table.add_column("Cloud Target")
    table.add_column("Cloud Synced")

    never_backed_up: list[str] = []
    for game in games:
        versions = db.get_versions(game.id)
        last_backup = "[yellow]never[/yellow]"
        cloud_synced = "no"
        # Judge sync status by the newest real backup — safety backups are
        # local-only by design and would otherwise show as forever-pending.
        display = next((v for v in versions if v.origin != "safety"), None)
        if display:
            last_backup = display.created_at.strftime("%Y-%m-%d %H:%M")
            cloud_synced = _sync_display(display, game, config)
        else:
            never_backed_up.append(game.id)

        table.add_row(
            game.title,
            # Count real backups only, so this agrees with Last Backup: a game
            # holding nothing but pre-restore safety snapshots is not "3 versions".
            str(len([v for v in versions if v.origin != "safety"])),
            last_backup,
            _cloud_target(game, config) or "off",
            cloud_synced,
        )

    console.print(table)

    if never_backed_up:
        console.print(
            f"\n[yellow]{len(never_backed_up)} game(s) have never been backed up:[/yellow] "
            f"{', '.join(never_backed_up)}\n"
            "[dim]Run 'gsg backup <game-id>' to protect them now. If a game never backs up "
            "on its own, its process is not being matched — set it with "
            "'gsg add <title> --exe <name.exe>'.[/dim]"
        )

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
    game_id: str | None = typer.Argument(None, help="Game ID to backup (omit for all)"),
    label: str | None = typer.Option(None, "--label", help="Backup label"),
    no_cloud: bool = typer.Option(False, "--no-cloud", help="Skip cloud upload"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
) -> None:
    """Back up save data for one or all games."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    games = load_games(config_path)
    db = Database(get_data_dir() / "versions.db")

    targets = [g for g in games if g.id == game_id] if game_id else games
    if game_id and not targets:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)

    # Resolve Ludusavi only if a non-custom game needs it (custom-only users
    # shouldn't trigger a Ludusavi download).
    _lud: dict[str, Path] = {}

    def ludusavi() -> Path:
        if "path" not in _lud:
            _lud["path"] = get_ludusavi_path(config_path)
        return _lud["path"]

    for game in targets:
        if dry_run:
            if game.custom:
                message = _custom_preview_message(game, db)
            else:
                message = preview_backup(ludusavi(), game, config.backup_dir).message
            console.print(f"[cyan]{game.title}: {message}[/cyan]")
            continue
        result = _run_backup(game, config, db, None if game.custom else ludusavi(), label)
        if not result.success:
            color = "red"
        elif result.version is None:
            color = "dim"  # succeeded but nothing to back up (no changes / no files)
        else:
            color = "green"
        console.print(f"[{color}]{result.message}[/]")
        if result.success and result.version and not no_cloud:
            # No provider gate here: `_cloud_upload` resolves the effective
            # provider itself, so `gsg backup` uploads exactly what `gsg auto`
            # would. They used to disagree.
            _cloud_upload(config_path, game, result.version, dry_run=False)


@app.command()
def restore(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID to restore"),
    version_id: str | None = typer.Option(None, "--version", help="Version ID (omit for latest)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
    no_safety: bool = typer.Option(
        False, "--no-safety", help="Skip the pre-restore safety backup (not recommended)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Restore even if the game looks like it is running"
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

    config = load_config(config_path)
    ok, message = restore_local_version(
        game, version, config, db, config_path, no_safety, force=force
    )
    console.print(f"[{'green' if ok else 'red'}]{message}[/]")
    if not ok:
        raise typer.Exit(1)


def _refuse_if_running(game: Game, force: bool) -> str | None:
    """Why a restore must not proceed right now, or None if it may.

    Writing save files underneath a live game loses whatever that process
    flushes on exit, and can leave a half-applied tree. This costs one
    process-table scan, which is why it lives on the restore path only — but
    EVERY restore path has to take it. A front end that skips this check is
    strictly more dangerous than the CLI, however nice it looks.
    """
    if force:
        return None
    probe = GameWatcher([game])
    probe.prime()
    if not probe.is_running(game.id):
        return None
    info = probe.running_process_info(game.id)
    matched = f" (matched: {info.exe or info.name})" if info else ""
    return (
        f"{game.title} is running{matched} — close it and try again. "
        f"Restoring under a live game loses whatever it writes on exit."
    )


def restore_local_version(
    game: Game,
    version: SaveVersion,
    config: SyncConfig,
    db: Database,
    config_path: Path | None,
    no_safety: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """Verify, safety-backup, and apply a local snapshot.

    Returns ``(ok, message)`` instead of printing and raising ``typer.Exit``,
    so a non-CLI caller (the TUI) runs this exact code path rather than a
    reimplementation of it — the safety rules must not have two versions.
    """
    blocked = _refuse_if_running(game, force)
    if blocked:
        return False, blocked

    ludusavi_path = None if game.custom else get_ludusavi_path(config_path)

    # Verify and stage the snapshot BEFORE touching anything on disk.
    restore_source = _materialize_version(version, game)
    if restore_source is None:
        return False, f"Snapshot for {version.id} is missing or failed verification."

    # Secure the current on-disk state so this restore can be undone.
    if not no_safety:
        safety = _run_backup(
            game, config, db, ludusavi_path,
            label="Safety backup before restore", origin="safety",
            protect_id=version.id,
        )
        if not safety.success:
            return False, (
                f"Safety backup failed ({safety.message}); nothing was restored. "
                f"Pass --no-safety to restore anyway."
            )

    try:
        _apply_staged_backup(game, restore_source, ludusavi_path)
    except RuntimeError as exc:
        return False, f"Restore failed: {exc}"
    return True, f"Restored {game.title} from version {version.id}"


def _apply_staged_backup(
    game: Game, staged_dir: Path, ludusavi_path: Path | None
) -> None:
    """Apply a staged backup tree to disk, dispatching by game type.

    Raises RuntimeError on failure so callers can abort without side effects.
    """
    if game.custom:
        custom.restore_custom(game, staged_dir)
    else:
        if ludusavi_path is None:
            raise RuntimeError("Ludusavi path was not resolved for a non-custom restore")
        restore_from_backup(ludusavi_path, game, staged_dir)


def _staged_backup_has_content(game: Game, staged_dir: Path) -> bool:
    """Whether a downloaded/staged dir holds a restorable backup for this game."""
    if game.custom:
        return custom.custom_backup_valid(staged_dir)
    return any(staged_dir.rglob("mapping.yaml"))


@app.command()
def versions(
    ctx: typer.Context,
    game_id: str = typer.Argument(..., help="Game ID"),
) -> None:
    """List save versions for a game."""
    config_path = ctx.obj.get("config_path")
    config = load_config(config_path)
    game = next((g for g in load_games(config_path) if g.id == game_id), None)
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
            # An untracked game (removed, rows left behind) has no cloud
            # settings to judge against — report the raw flag rather than lie.
            _sync_display(v, game, config) if game else ("yes" if v.cloud_synced else "no"),
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
    if not game:
        console.print(f"[red]Game not found: {game_id}[/red]")
        raise typer.Exit(1)
    if not _effective_provider(game, config):
        console.print("[red]Game has no cloud provider configured.[/red]")
        raise typer.Exit(1)

    remote_name = _effective_remote(game, config)
    if not remote_name:
        console.print("[red]No rclone remote configured. Run 'gsg' to set up cloud storage.[/red]")
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
    game_id: str | None = typer.Argument(None, help="Game ID to pull (omit with --all)"),
    version_id: str | None = typer.Option(
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
    cloud_targets = [g for g in targets if _cloud_target(g, config)]
    if not cloud_targets:
        console.print(
            "[red]No cloud-enabled game to pull. Configure cloud storage with 'gsg'.[/red]"
        )
        raise typer.Exit(1)

    rclone_path = get_rclone_path(config_path)
    # Only resolve (and maybe download) Ludusavi if a non-custom game needs it.
    ludusavi_path = (
        get_ludusavi_path(config_path)
        if any(not g.custom for g in cloud_targets)
        else None
    )

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

        remote_name = _effective_remote(game, config)
        assert remote_name is not None  # filtered above
        cloud_latest: str | None = None
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
            game, config, db, rclone_path, ludusavi_path, target_version, force=force
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
    _LOCK_STATE["held"] = True  # this process is now the sole backup writer

    db = Database(get_data_dir() / "versions.db")
    ludusavi_path = (
        get_ludusavi_path(config_path)
        if any(not g.custom for g in games)
        else None
    )

    def on_close(game: Game, proc_info: object) -> None:
        console.print(f"[cyan]Game closed: {game.title}. Backing up...[/cyan]")
        result = _run_backup(
            game, config, db, ludusavi_path,
            label=f"Auto-backup on {_now_label()}", origin="auto",
        )
        console.print(f"{'[green]' if result.success else '[red]'}{result.message}[/]")
        if result.success and result.version:
            _cloud_upload(config_path, game, result.version, dry_run=False)

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


def protect_unbacked_games(
    games: list[Game],
    config: SyncConfig,
    config_path: Path | None,
    db: Database,
    ludusavi_path: Path | None,
    report: Callable[[Game, BackupResult, bool | None], None],
) -> list[str]:
    """Back up every game that has no versions yet. Returns those still without.

    A game gsg has just discovered has saves on disk right now, and neither
    backup trigger reaches them: backups fire when a game closes, or
    periodically while one runs. So a newly tracked game stayed unprotected
    until the user happened to play it again, while `gsg status` printed
    "run gsg backup yourself" into a console that `gsg auto --install` hides
    and the tray stayed green (#55).

    One game failing must not leave the rest unprotected, and must not stop
    the watcher starting, so each is attempted independently.
    """
    unprotected = [g for g in games if not db.get_versions(g.id)]
    if not unprotected:
        return []

    console.print(
        f"[cyan]Protecting {len(unprotected)} game(s) that have never been "
        f"backed up...[/cyan]"
    )
    still: list[str] = []
    for index, game in enumerate(unprotected, 1):
        console.print(f"[dim]  ({index}/{len(unprotected)}) {game.title}[/dim]")
        try:
            result = _run_backup(
                game, config, db, None if game.custom else ludusavi_path,
                label="First backup",
            )
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "First backup failed for %s", game.title
            )
            console.print(f"[red]  {game.title}: {exc}[/red]")
            still.append(game.title)
            continue
        if result.success and result.version:
            uploaded = _cloud_upload(config_path, game, result.version, dry_run=False)
            report(game, result, uploaded)
        else:
            report(game, result, None)
            still.append(game.title)
    return still


def discover_new_games(
    config: SyncConfig,
    config_path: Path | None,
    ludusavi_path: Path,
    *,
    quiet: bool = False,
) -> list[Game]:
    """Scan for games gsg is not tracking yet, add them, and return them.

    Extracted from `gsg auto`, which used to do this once at startup and never
    again. A game installed while the watcher ran was invisible until the next
    reboot, so on a machine that stays up for a week that is a week of saves
    nobody was protecting (#54). The watch loop now calls this on a timer, and
    `quiet` keeps a routine rescan that finds nothing from printing anything.

    Also re-checks launcher ownership of games already tracked (#48), and
    skips titles whose only known paths are settings (#47).
    """
    from .launcher import detect_launcher, get_all_launcher_games

    def say(message: str) -> None:
        if not quiet:
            console.print(message)

    say("[cyan]Scanning for Hydra/manual games...[/cyan]")
    data = scan_games(ludusavi_path)
    games_data = data.get("games", {})
    steam_games, epic_games, xbox_games = get_all_launcher_games()

    existing_games = load_games(config_path)
    existing_ids = {g.id for g in existing_games}
    existing_titles = {g.title for g in existing_games}
    new_games: list[Game] = []

    # Ludusavi's manifest tags each path save/config/etc, but the scan API
    # does not report tags - so ask the manifest which candidates hold no save
    # data at all. Roblox is two files, both tagged config, because progress
    # lives on the account; tracking it produced ten versions of a settings
    # file (#47).
    candidate_titles = {
        title
        for title, info in games_data.items()
        if detect_launcher(
            title, list(info.get("files", {}).keys()),
            steam_games, epic_games, xbox_games,
        ) == "other"
    }
    config_only = titles_without_save_data(candidate_titles)
    if config_only:
        say(
            f"[dim]Skipped {len(config_only)} game(s) with no save data of "
            f"their own: {', '.join(sorted(config_only))}.[/dim]"
        )

    for title, info in games_data.items():
        files = info.get("files", {})
        save_paths = list(files.keys())
        detected = detect_launcher(title, save_paths, steam_games, epic_games, xbox_games)
        if auto_add_skip_reason(title, detected, config_only) is not None:
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

    # Ownership was decided when each game was first seen and never revisited.
    # Epic writes a manifest per INSTALLED game, so a title that was not
    # installed during the first scan is invisible to detect_launcher and stays
    # tracked forever, duplicating a launcher's own cloud sync (#48). Report
    # rather than act: removing a game deletes backups on the strength of a
    # heuristic that has already proven fragile, and keeping a second copy of a
    # launcher-synced game is a legitimate choice.
    for game in existing_games:
        owner = detect_launcher(
            game.title, [str(sp.path) for sp in game.save_paths],
            steam_games, epic_games, xbox_games,
        )
        if owner != "other":
            say(
                f"[yellow]{game.title} is now installed through "
                f"{owner.capitalize()}, which syncs its own saves.[/yellow] "
                f"[dim]'gsg remove {game.id}' to stop duplicating it.[/dim]"
            )

    if new_games:
        save_games(existing_games + new_games, config_path)
        # Always announced, even on a quiet rescan: finding a new game is the
        # whole point of running one.
        console.print(f"[green]Auto-added {len(new_games)} game(s):[/green]")
        for added in new_games:
            console.print(f"  - {added.title}")
        logger_names = ", ".join(g.title for g in new_games)
        logging.getLogger(__name__).info("Auto-added games: %s", logger_names)
    else:
        say("[dim]No new games found. Using existing tracked games.[/dim]")

    return new_games


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
    no_tray: bool = typer.Option(
        False, "--no-tray", help="Do not show the system tray icon"
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

    # Take the single-instance lock BEFORE the (expensive) scan: a second
    # instance should be rejected immediately, not after a full disk scan.
    # In service context (--no-wizard) a held lock is a benign outcome —
    # another watcher is doing the job — so exit 0, or Restart=on-failure
    # would relaunch the service every 30 seconds forever.
    lock = _acquire_instance_lock()
    if lock is None:
        message = "Another gsg watcher is already running."
        if no_wizard:
            logging.getLogger(__name__).info(message)
            console.print(f"[dim]{message}[/dim]")
            raise typer.Exit(0)
        console.print(f"[red]{message}[/red]")
        raise typer.Exit(1)
    _LOCK_STATE["held"] = True  # this process is now the sole backup writer

    ludusavi_path = get_ludusavi_path(config_path)
    discover_new_games(config, config_path, ludusavi_path)

    # Load all tracked games for watching
    all_tracked = _watchable_games(load_games(config_path))
    if not all_tracked:
        message = "No games to watch. Play some games and run 'gsg auto' again."
        if no_wizard:
            # Benign in service context (fresh machine, nothing installed
            # yet): exit clean so systemd does not crash-loop; the next
            # login rescans.
            logging.getLogger(__name__).info(message)
            console.print(f"[dim]{message}[/dim]")
            lock.close()
            raise typer.Exit(0)
        console.print(f"[yellow]{message}[/yellow]")
        lock.close()
        raise typer.Exit(1)

    db = Database(get_data_dir() / "versions.db")
    rclone_path = get_rclone_path(config_path)

    # The tray and the watcher can both start a backup, so everything that
    # touches save files takes this first. Watcher callbacks are already
    # serialised with each other; the tray's menu runs on its own thread.
    backup_lock = threading.Lock()
    log = logging.getLogger(__name__)

    def action_backup_now() -> None:
        with backup_lock:
            for game in all_tracked:
                backup_and_report(game, f"Manual backup on {_now_label()}")

    def action_status() -> None:
        _open_status_window(config_path)

    def action_open_logs() -> None:
        _open_path(get_data_dir() / "logs")

    def action_quit() -> None:
        # Stop the loop first: the watcher owns the main thread, and once it
        # returns the `finally` below tears the tray down and frees the lock.
        watcher.stop()
        tray.stop()

    tray: tray_mod.NullTray | tray_mod.Tray = (
        tray_mod.NullTray()
        if no_tray
        else tray_mod.create_tray(
            {
                "backup_now": action_backup_now,
                "status": action_status,
                "open_logs": action_open_logs,
                "quit": action_quit,
            }
        )
    )

    def alert(title: str, message: str) -> None:
        """Tell the user, wherever they can actually see it.

        Under autostart the console is hidden, so a print reaches nobody. The
        tray balloon is preferred (it carries our icon and costs no process);
        the platform notifier is the fallback.
        """
        log.info("%s: %s", title, message)
        if not tray.notify(title, message):
            notify(title, message)

    def report_backup(game: Game, result: BackupResult, uploaded: bool | None) -> None:
        """Surface one backup outcome to console, log, tray, and notification.

        Failures used to print to a hidden console and nothing else — no
        toast, no ERROR line, no status change. Weeks of them looked
        identical to everything working.
        """
        if not result.success:
            console.print(f"[red]{result.message}[/red]")
            log.error("Backup failed for %s: %s", game.title, result.message)
            tray.escalate(tray_mod.STATE_ERROR, f"{game.title}: backup failed")
            alert("Backup FAILED", f"{game.title}: {result.message}")
            return
        if uploaded is False:
            console.print(f"[red]{game.title}: save is backed up locally but not uploaded.[/red]")
            tray.escalate(tray_mod.STATE_ERROR, f"{game.title}: upload failed")
            alert(
                "Cloud upload FAILED",
                f"{game.title}: backed up locally, but the upload did not complete.",
            )
            return
        if result.version is None:
            console.print(f"[dim]{result.message}[/dim]")
            return
        console.print(f"[green]{result.message}[/green]")
        tray.set_state(tray_mod.STATE_OK, f"{game.title} backed up")
        alert("Save backed up", game.title)

    def on_start(game: Game, proc_info: ProcessInfo) -> None:
        console.print(f"[green]Game started: {game.title}[/green]")
        tray.set_state(tray_mod.STATE_OK, f"Playing {game.title}")
        alert("Game started", game.title)
        # Executables are learned on close, from the whole session — see
        # _remember_executables. Learning here would see only whichever
        # process of the tree started first, which is usually a launcher.
        # Never restore under a live process; just tell the user.
        if _cloud_newer_version(game, config, db, rclone_path) is not None:
            alert(
                "Newer cloud save exists",
                f"{game.title}: not applied because the game is running. "
                f"It will be restored after you quit.",
            )

    def backup_and_report(game: Game, label: str) -> None:
        result = _run_backup(
            game, config, db, ludusavi_path, label=label, origin="auto",
        )
        uploaded: bool | None = None
        if result.success and result.version and result.files_changed > 0:
            uploaded = _cloud_upload(config_path, game, result.version, dry_run=False)
        report_backup(game, result, uploaded)

    def on_close(game: Game, proc_info: ProcessInfo) -> None:
        # Every process this game ran is known now, so a real executable can
        # be picked out of the launcher/anti-cheat noise around it.
        _remember_executables(game, watcher.session_process_names(game.id), config_path)
        watcher.clear_session_names(game.id)
        console.print(f"[cyan]Game closed: {game.title}. Backing up to cloud...[/cyan]")
        with backup_lock:
            backup_and_report(game, f"Auto-backup on {_now_label()}")

    def on_periodic(game: Game) -> None:
        console.print(f"[cyan]Periodic backup: {game.title}...[/cyan]")
        with backup_lock:
            backup_and_report(game, f"Periodic backup on {_now_label()}")

    def on_idle(game: Game) -> None:
        with backup_lock:
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

    def on_watch_error(message: str) -> None:
        """A callback raised. The loop survives; the user must still hear it.

        Without this a failed close-backup left the tray on the green
        "Playing" state set when the game started, and the only record was a
        stack trace in a log file nobody opens (#41).
        """
        log.error("%s", message)
        console.print(f"[red]{message}[/red]")
        tray.escalate(tray_mod.STATE_ERROR, message)
        alert("Backup FAILED", message)

    watcher.set_on_error(on_watch_error)
    watcher.prime()

    console.print("[cyan]Checking cloud for newer saves...[/cyan]")
    for game in all_tracked:
        if not watcher.is_running(game.id):
            _auto_restore_if_idle(game, config, db, rclone_path, ludusavi_path)

    console.print(f"\n[green]Auto-backup active. Watching {len(all_tracked)} game(s).[/green]")
    if periodic > 0:
        console.print(f"[dim]Periodic backup every {int(periodic)}s during gameplay.[/dim]")
    console.print("[dim]Press Ctrl+C to stop. Run 'gsg auto --install' to start on boot.[/dim]\n")

    tray.start()
    if tray.available:
        console.print("[dim]Tray icon active — right-click it for status and manual backup.[/dim]")

    # A game the watcher has never matched produces no events at all, so the
    # tray would sit green forever while that save is unprotected. Say it up
    # front, once, where the user can see it.
    # A game whose title reduces to nothing can never be matched, yet it is
    # still counted in "Watching N game(s)" above — the most misleading state
    # the watcher can be in.
    undetectable = [
        (g.id, reason)
        for g in all_tracked
        if (reason := unmatchable_reason(g)) is not None
    ]
    if undetectable:
        tray.escalate(
            tray_mod.STATE_WARN, f"{len(undetectable)} game(s) cannot be detected"
        )
        console.print(
            f"\n[yellow]{len(undetectable)} game(s) cannot be detected automatically:"
            f"[/yellow]"
        )
        for game_id, reason in undetectable:
            console.print(f"  [dim]{reason} — set it with "
                          f"'gsg set {game_id} --exe <name.exe>'[/dim]")

    # Names learned before this was fixed were stored without the "learned"
    # flag, so they read as a deliberate --exe and still suppress title
    # matching. Point at the repair rather than guessing which is which.
    suspect = [
        g
        for g in all_tracked
        if g.executable_names
        and not g.executables_learned
        and all(is_helper_executable(name) for name in g.executable_names)
    ]
    if suspect:
        console.print(
            f"\n[yellow]{len(suspect)} game(s) are identified only by a launcher or "
            f"crash-handler process, which may not run every session:[/yellow]"
        )
        for game in suspect:
            console.print(
                f"  [dim]{game.title}: {', '.join(game.executable_names)} — "
                f"'gsg set {game.id} --clear-exe' to detect it by title instead[/dim]"
            )

    # A game gsg has just discovered has saves on disk right now, and neither
    # trigger reaches them: backups fire on game close, or periodically while
    # a game runs. Printing "run gsg backup yourself" was advice into a
    # console that `gsg auto --install` hides, so the saves stayed unprotected
    # while the tray stayed green (#55). Protect them instead.
    still_unprotected = protect_unbacked_games(
        all_tracked, config, config_path, db, ludusavi_path, report_backup
    )
    if still_unprotected:
        tray.escalate(
            tray_mod.STATE_WARN,
            f"{len(still_unprotected)} game(s) still never backed up",
        )
        console.print(
            f"[yellow]Still unprotected: {', '.join(still_unprotected)}[/yellow]"
        )
        console.print(
            "[dim]If these never back up on their own, their process isn't being "
            "matched - set it with 'gsg add <title> --exe <name.exe>'.[/dim]"
        )

    # Discovery used to happen once, before the loop. A game installed while
    # the watcher ran stayed invisible until the next reboot (#54), which on a
    # machine that stays up for a week is a week of unprotected saves. A
    # Ludusavi scan is far too expensive for the 5s poll, so it runs on its own
    # long timer and stays quiet unless it actually finds something.
    def rescan_for_new_games() -> None:
        found = discover_new_games(config, config_path, ludusavi_path, quiet=True)
        if not found:
            return
        watcher.add_games(_watchable_games(load_games(config_path)))
        for game in found:
            alert("New game found", f"{game.title} is now being backed up.")

    if config.rescan_interval_hours > 0:
        watcher.set_periodic_task(
            config.rescan_interval_hours * 3600.0, rescan_for_new_games
        )

    try:
        watcher.watch_loop(interval=interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/yellow]")
    finally:
        tray.stop()
        lock.close()


_CREATE_NEW_CONSOLE = 0x00000010


def _open_path(path: Path) -> None:
    """Open a folder in the desktop's file manager, best effort."""
    import subprocess

    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            # Fetched dynamically: os.startfile exists only on Windows, so a
            # direct call is an attr-defined error when CI type-checks on Linux
            # and an unused-ignore error when it type-checks on Windows.
            startfile = getattr(os, "startfile", None)
            if startfile is not None:
                startfile(path)
        else:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Could not open %s: %s", path, exc)


def _open_status_window(config_path: Path | None) -> None:
    """Show `gsg status` in a window the user can actually read.

    The watcher's own console is hidden under autostart, so status has to run
    somewhere new rather than printing into the void.
    """
    import subprocess

    exe = _find_gsg_exe()
    if exe is None:
        logging.getLogger(__name__).warning("Cannot show status: gsg executable not found.")
        return
    args = [str(exe), "status"]
    if config_path:
        args = [str(exe), "--config", str(config_path), "status"]
    try:
        if os.name == "nt":
            # /k keeps the window open after status has printed.
            subprocess.Popen(["cmd", "/k", *args], creationflags=_CREATE_NEW_CONSOLE)
            return
        import shutil

        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", *args])
                return
        # No terminal to borrow: the log folder is the next best thing.
        _open_path(get_data_dir() / "logs")
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Could not show status: %s", exc)


def encode_vbs(script: str) -> bytes:
    """Serialize a VBScript the way Windows Script Host actually reads one.

    WSH accepts exactly two forms: the system ANSI codepage, or UTF-16 with a
    byte order mark. It does not accept UTF-8. A UTF-8 file gets read as ANSI,
    which is harmless while every character is ASCII and wrong the moment one
    is not — a user whose Windows profile is ``C:\\Users\\José`` got a startup
    script pointing at a path that does not exist, silently. Verified: a UTF-8
    file containing "café" reports Len 5 to WSH; UTF-16 reports 4.

    A UTF-8 *BOM* is worse still. WSH refuses the file outright with
    "Invalid character" (800A0408) at line 1, char 1, before running anything.
    That is not a hypothetical either: writing one and running it under cscript
    reproduces exactly that error, which is how this was found.

    UTF-16LE with a BOM is the form that survives both.
    """
    return codecs.BOM_UTF16_LE + script.encode("utf-16-le")


def _startup_vbs_path() -> Path:
    startup_dir = (
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs" / "Startup"
    )
    return startup_dir / "GameSaveGenie.vbs"


def _find_gsg_exe() -> Path | None:
    """Locate the gsg executable for autostart, or None."""
    if getattr(sys, "frozen", False):
        # Running as a PyInstaller bundle: this process IS the gsg binary.
        return Path(sys.executable)
    binary = "gsg.exe" if os.name == "nt" else "gsg"
    candidate = Path(sys.executable).parent / binary
    if candidate.exists():
        return candidate
    import shutil

    which = shutil.which("gsg")
    if which:
        return Path(which)
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    project_venv = Path(__file__).resolve().parents[2] / ".venv" / scripts_dir / binary
    if project_venv.exists():
        return project_venv
    return None


def _install_startup(config_path: Path | None = None) -> None:
    """Install gsg auto to run hidden at logon via the user's Startup folder.

    A Startup-folder script needs no elevation and no console window.
    (A Task Scheduler ONLOGON task was considered and rejected: schtasks
    requires elevation and pins a visible console window to every logon.)
    The script passes --no-wizard so an unconfigured boot run exits with a
    logged error instead of blocking forever on an invisible prompt, and it
    checks the executable still exists so a moved exe fails silently rather
    than popping an error dialog at every logon.
    """
    gsg_path = _find_gsg_exe()
    if gsg_path is None:
        console.print(
            "[red]Could not locate the gsg executable; autostart not installed. "
            "Install the package (pip install .) or use the standalone gsg.exe.[/red]"
        )
        raise typer.Exit(1)

    if sys.platform.startswith("linux"):
        _install_systemd_unit(gsg_path, config_path)
        return
    if os.name != "nt":
        console.print("[yellow]Autostart install is not yet supported on this OS.[/yellow]")
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
        "On Error Resume Next\r\n"
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        f'If fso.FileExists("{gsg_path}") Then\r\n'
        '    Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'    WshShell.Run "{command}", 0, False\r\n'
        "End If\r\n"
    )
    vbs_path.write_bytes(encode_vbs(vbs_content))
    console.print(f"[green]Installed to Windows startup: {vbs_path}[/green]")
    console.print(
        f"[dim]Runs '{gsg_path}' hidden at logon. Moving the executable breaks "
        f"autostart — re-run 'gsg auto --install' after moving it.[/dim]"
    )


def _uninstall_startup() -> None:
    """Remove gsg auto from startup (Windows VBS or Linux systemd unit)."""
    if os.name != "nt":
        _uninstall_systemd_unit()
        return
    vbs_path = _startup_vbs_path()
    if vbs_path.exists():
        vbs_path.unlink()
        console.print(f"[green]Removed from Windows startup: {vbs_path}[/green]")
    else:
        console.print("[yellow]No autostart entry found.[/yellow]")


_SYSTEMD_UNIT_NAME = "game-save-genie.service"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _systemd_escape(path: Path) -> str:
    """Quote a path for a systemd ExecStart token (spaces + % specifiers)."""
    return '"' + path.as_posix().replace("%", "%%") + '"'


def _systemd_unit_content(gsg_path: Path, config_path: Path | None) -> str:
    """The systemd user unit that runs the watcher at login (pure, testable).

    Benign outcomes (lock already held, nothing to watch yet) exit 0 so
    Restart=on-failure does not loop on them; the StartLimit settings bound
    retries for genuine failures — without them, RestartSec=30 spaces starts
    so far apart that systemd's default rate limiter never trips.
    """
    command = _systemd_escape(gsg_path)
    if config_path is not None:
        command += f" --config {_systemd_escape(config_path)}"
    command += " auto --no-wizard"
    return (
        "[Unit]\n"
        "Description=Game Save Genie automatic save backup\n"
        "StartLimitIntervalSec=600\n"
        "StartLimitBurst=5\n"
        "\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _install_systemd_unit(gsg_path: Path, config_path: Path | None) -> None:
    """Install and start the watcher as a systemd user service (Linux)."""
    import shutil
    import subprocess

    if shutil.which("systemctl") is None:
        console.print(
            "[yellow]systemctl not found — autostart install currently supports "
            "systemd. Run 'gsg auto' from your session startup instead.[/yellow]"
        )
        raise typer.Exit(1)

    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_systemd_unit_content(gsg_path, config_path), encoding="utf-8")

    for args in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT_NAME],
    ):
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            console.print(
                f"[red]{' '.join(args)} failed: "
                f"{(result.stderr or result.stdout or '').strip()}[/red]"
            )
            raise typer.Exit(1)
    console.print(f"[green]Installed systemd user service: {unit_path}[/green]")
    console.print(
        "[dim]Starts at login. To run without an active session (headless), "
        "enable lingering: loginctl enable-linger $USER[/dim]"
    )


def _uninstall_systemd_unit() -> None:
    import shutil
    import subprocess

    have_systemctl = shutil.which("systemctl") is not None
    unit_path = _systemd_unit_path()
    removed = False
    if have_systemctl:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT_NAME],
            capture_output=True, text=True, check=False,
        )
    if unit_path.exists():
        unit_path.unlink()
        if have_systemctl:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, text=True, check=False,
            )
        console.print(f"[green]Removed systemd user service: {unit_path}[/green]")
        removed = True
    if not removed:
        console.print("[yellow]No autostart entry found.[/yellow]")


@app.command(name="config")
def config_cmd(
    ctx: typer.Context,
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Set backup directory"),
    max_versions: int | None = typer.Option(None, "--max-versions", help="Max versions to keep"),
    cloud_provider: CloudProvider | None = typer.Option(None, "--cloud-provider", help="Default cloud provider"),
    rclone_remote_name: str | None = typer.Option(None, "--rclone-remote", help="Name of the rclone remote"),
    remote_root: str | None = typer.Option(None, "--remote-root", help="Remote root path or bucket"),
    ludusavi_path: Path | None = typer.Option(None, "--ludusavi", help="Path to ludusavi binary"),
    rclone_path: Path | None = typer.Option(None, "--rclone", help="Path to rclone binary"),
    storage_limit: float | None = typer.Option(
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
    # Railway addresses buckets as subdomains, so it is the one caller that
    # wants path style off.
    _setup_s3_endpoint(
        ctx, remote_name, endpoint, access_key, secret_key, bucket, region,
        force_path_style=False,
    )


@app.command(name="setup-s3")
def setup_s3(
    ctx: typer.Context,
    remote_name: str = typer.Argument(default="s3", help="Name for the rclone remote"),
    endpoint: str = typer.Option(..., prompt=True, help="S3 endpoint URL (e.g. http://homelab:9000)"),
    access_key: str = typer.Option(..., prompt=True, hide_input=True, help="Access key"),
    secret_key: str = typer.Option(..., prompt=True, hide_input=True, help="Secret key"),
    bucket: str = typer.Option(..., prompt=True, help="Bucket name"),
    region: str = typer.Option("auto", help="Region"),
    path_style: bool = typer.Option(
        True,
        "--path-style/--no-path-style",
        help="Address the bucket as a path (http://host/bucket). Turn off only "
             "for providers that require bucket.host subdomains.",
    ),
) -> None:
    """Connect any S3-compatible storage: self-hosted MinIO, Garage, Backblaze B2, AWS...

    See docker/README.md for running your own save server with docker compose.
    """
    _setup_s3_endpoint(
        ctx, remote_name, endpoint, access_key, secret_key, bucket, region,
        force_path_style=path_style,
    )


def _setup_s3_endpoint(
    ctx: typer.Context,
    remote_name: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    force_path_style: bool = True,
) -> None:
    config_path = ctx.obj.get("config_path") or get_config_path()
    if not _configure_and_verify_s3(
        config_path, remote_name, endpoint, access_key, secret_key, bucket, region,
        force_path_style,
    ):
        raise typer.Exit(1)


def _endpoint_candidates(endpoint: str) -> list[str]:
    """The endpoint URLs to try, in order.

    A typed endpoint that already names its scheme is taken at its word. One
    that does not is ambiguous, and rclone resolves that ambiguity by assuming
    https — which is wrong for the common case, a self-hosted server on a LAN
    with no certificate. Try the safe interpretation first and fall back
    rather than guessing once and failing with a TLS error nobody can read.
    """
    cleaned = normalize_endpoint(endpoint)
    if not cleaned:
        return []
    if endpoint_has_scheme(cleaned):
        return [cleaned]
    return [f"https://{cleaned}", f"http://{cleaned}"]


def _configure_and_verify_s3(
    config_path: Path,
    remote_name: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    force_path_style: bool = True,
) -> bool:
    """Write the remote, prove the bucket answers, and revert if it does not."""
    candidates = _endpoint_candidates(endpoint)
    if not candidates:
        console.print("[red]No endpoint given.[/red]")
        return False

    last_error = ""
    for candidate in candidates:
        if len(candidates) > 1:
            console.print(f"[dim]Trying {candidate} ...[/dim]")
        conf_path = _configure_s3_remote(
            config_path, remote_name, candidate, access_key, secret_key, bucket,
            region, force_path_style,
        )
        ok, last_error = _probe_s3_remote(config_path, remote_name, bucket)
        if ok:
            if candidate.startswith("http://"):
                console.print(
                    "[yellow]Connected over plain HTTP.[/yellow] Your access key and "
                    "your saves cross this network unencrypted. Fine on a LAN you "
                    "trust; put TLS in front of the server before using it over the "
                    "internet."
                )
            console.print(f"[green]{remote_name}: S3 storage configured and verified.[/green]")
            console.print(f"rclone config written to: {conf_path}")
            console.print("Test it with: gsg backup <game-id>")
            return True

    _revert_cloud_config(config_path)
    console.print(
        f"[red]Could not access the bucket with those credentials:[/red]\n{last_error}"
    )
    console.print(
        "[yellow]Check that:[/yellow]\n"
        "  the endpoint names scheme, host and port, e.g. http://192.168.1.10:9000\n"
        f"  the bucket '{bucket}' exists on that server\n"
        "  the access key and secret belong to a user allowed to list it\n"
        "  the server is reachable from this machine (try it in a browser)"
    )
    return False


def _configure_s3_remote(
    config_path: Path,
    remote_name: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
    force_path_style: bool = True,
) -> Path:
    config = load_config(config_path)
    conf_path = write_s3_config(
        remote_name, endpoint, access_key, secret_key, bucket, region, force_path_style
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
        ok = _configure_and_verify_s3(
            config_path, "railway", endpoint, access_key, secret_key, bucket, "auto",
            force_path_style=False,
        )
    else:
        console.print("[dim]Skipped cloud setup. Run 'gsg' again any time.[/dim]")
        return False

    can_autostart = os.name == "nt" or sys.platform.startswith("linux")
    if ok and can_autostart and typer.confirm(
        "Start Game Save Genie automatically at boot?", default=True
    ):
        try:
            _install_startup(ctx.obj.get("config_path"))
        except typer.Exit:
            pass  # install failed; the specific message was already printed
    return ok


# A config check is not a transfer, so it must not inherit transfer-grade
# patience. rclone defaults to ten attempts with backoff, which turned a typo
# into a 2.5-minute wait before the error appeared (#24) — long enough that
# nobody iterates, which is exactly what setup requires.
_S3_PROBE_ARGS = [
    "--retries", "1",
    "--low-level-retries", "1",
    "--contimeout", "10s",
    "--timeout", "30s",
]


def _probe_s3_remote(config_path: Path, remote_name: str, bucket: str) -> tuple[bool, str]:
    """Ask the remote to list the bucket. Returns (worked, error text)."""
    rclone_path = get_rclone_path(config_path)
    result = run_rclone(
        rclone_path, ["lsd", f"{remote_name}:{bucket}", *_S3_PROBE_ARGS], check=False
    )
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout or "").strip()


def _revert_cloud_config(config_path: Path) -> None:
    """Undo a cloud provider selection that failed to verify.

    Without this, a pasted typo would be declared 'configured', the wizard
    would never run again, and every upload would fail invisibly at runtime.
    """
    config = load_config(config_path)
    config.cloud_provider = None
    config.rclone_remote_name = None
    save_config(config, config_path)


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


# Set once a daemon has taken the instance lock for its lifetime, so the
# per-backup guard below knows this process is already the sole writer and
# does not warn about itself. A dict rather than a bare name because the two
# daemons set it from their own scopes and rebinding a module global from
# there is easy to misread.
_LOCK_STATE = {"held": False}


def _acquire_instance_lock() -> IO[str] | None:
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


@contextmanager
def _backup_guard(action: str) -> Iterator[None]:
    """Hold the instance lock for a one-shot command that writes backups.

    `gsg auto` and `gsg watch` hold this for their whole lifetime. Everything
    else -- `gsg backup`, `gsg restore`, `gsg pull`, and the dashboard, which
    runs in its own process -- took nothing, so pressing 'b' in `gsg ui` while
    the watcher ran its periodic backup had both writing the same
    backup_dir/<game_id> while one of them zipped it. The resulting snapshot
    passes its own hash check, because the digest is taken of the zip after it
    is written and certifies the archive rather than the consistency of what
    went into it (#38).

    Yields whether or not the lock was free: refusing to back up because a
    watcher is running would be worse than the race. The warning is the point,
    and it names the other holder so the user can act on it.
    """
    if _LOCK_STATE["held"]:
        # A daemon already owns the lock for its whole run; it is the writer.
        yield
        return
    handle = _acquire_instance_lock()
    if handle is None:
        console.print(
            f"[yellow]Another Game Save Genie process is writing backups. "
            f"Doing this {action} anyway, but if it looks wrong, stop "
            f"'gsg auto' and retry.[/yellow]"
        )
        yield
        return
    try:
        yield
    finally:
        handle.close()


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


def _custom_preview_message(game: Game, db: Database) -> str:
    previous = db.get_versions(game.id)
    prev_digest = previous[0].content_digest if previous else None
    digest, size, count = custom.compute_source_digest(game)
    if count == 0:
        return "No save files found at the configured paths"
    if digest == prev_digest:
        return "No changes to back up"
    return f"Would back up {count} file(s) ({_human_size(size)})"


def _run_backup(
    game: Game,
    config: SyncConfig,
    db: Database,
    ludusavi_path: Path | None,
    label: str | None = None,
    origin: str = "user",
    protect_id: str | None = None,
) -> BackupResult:
    # Every writer goes through here - the backup command, the safety backup
    # taken before a restore, the dashboard in its own process, and the
    # watcher. Guarding here rather than at each call site means no future
    # caller can forget (#38).
    with _backup_guard("backup"):
        if game.custom:
            previous = db.get_versions(game.id)
            prev_digest = previous[0].content_digest if previous else None
            result = custom.backup_custom(game, config.backup_dir, label, prev_digest)
        else:
            if ludusavi_path is None:
                raise RuntimeError(
                    "Ludusavi path was not resolved for a non-custom backup"
                )
            result = backup_game(ludusavi_path, game, config.backup_dir, label)
    if result.success and result.version:
        try:
            _snapshot_version(result.version, config)
        except OSError as exc:
            # Storing the row anyway used to leave local_path pointing at the
            # shared live directory with sha256 None. Every such row then
            # aliased the same directory: `gsg versions` listed them as
            # distinct restore points that all restored the newest content,
            # and an upload sent whatever the directory held at upload time
            # (#38). A backup we cannot snapshot is a failed backup.
            logging.getLogger(__name__).error(
                "Snapshot failed for %s: %s", game.title, exc
            )
            console.print(
                f"[red]Snapshot failed for {game.title}: {exc}. "
                f"This backup was NOT recorded.[/red]"
            )
            return BackupResult(
                success=False,
                game_id=game.id,
                message=f"Snapshot failed: {exc}",
            )
        result.version.origin = origin
        db.add_version(result.version)
        _prune_old_versions(db, game.id, config.max_versions, protect_id=protect_id)
    return result


def _prune_old_versions(
    db: Database,
    game_id: str,
    max_versions: int,
    protect_id: str | None = None,
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


def _materialize_version(version: SaveVersion, game: Game) -> Path | None:
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
    config_path: Path | None,
    game: Game,
    version: SaveVersion,
    dry_run: bool,
) -> bool:
    """Upload a version to the game's effective remote.

    Returns False only when an upload was actually attempted and failed, so a
    caller can tell "nothing to do" apart from "your save did not reach the
    cloud" — the tray and its notifications depend on that distinction.

    Takes a config path rather than a Typer context so callers that are not
    commands (the TUI) can reach it without inventing one.
    """
    config = load_config(config_path)
    if not _effective_provider(game, config):
        return True
    rclone_path = get_rclone_path(config_path)
    remote_name = _effective_remote(game, config)
    if not remote_name:
        console.print("[red]No rclone remote configured.[/red]")
        return False
    if dry_run:
        console.print(f"[cyan]Would upload {version.id} for {game.title}[/cyan]")
        return True
    result = upload_save_cas(
        rclone_path,
        game,
        version,
        remote_name,
        config.remote_root,
        extra_args=config.custom_rclone_args,
    )
    console.print(f"[{'green' if result.success else 'red'}]{result.message}[/]")
    if not result.success:
        logging.getLogger(__name__).error(
            "Cloud upload failed for %s: %s", game.title, result.message
        )
        return False

    db = Database(get_data_dir() / "versions.db")
    db.mark_cloud_synced(version.id, result.remote_path)
    # protect_id matters when this machine's clock disagrees with another's.
    # Version ids are wall-clock strings and retention sorts them, so an
    # upload from a slow-clocked machine sorts oldest and would be deleted by
    # the prune that immediately follows its own upload (#36).
    pruned = prune_remote_versions(
        rclone_path, game, remote_name, config.remote_root,
        keep=config.max_versions, protect_id=version.id,
    )
    if pruned:
        console.print(f"[dim]Pruned {len(pruned)} old cloud version(s).[/dim]")
    return True


def _cloud_restore_dir(game_id: str) -> Path:
    """Staging directory for downloaded cloud saves (outside the backup tree)."""
    return get_data_dir() / "cloud_restore" / game_id


def _reset_dir(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


_MAX_LEARNED_EXECUTABLES = 5


def _remember_executables(
    game: Game, seen_names: set[str], config_path: Path | None
) -> None:
    """Record the process names seen across a whole play session.

    Learning used to happen on the *start* transition, guarded by "exactly one
    process matches" — which is precisely the tick where only the earliest
    process of the tree exists. That selected FOR launchers and crash
    handlers rather than against them, and the single name it stored was
    permanent. Cyberpunk ended up identified by REDEngineErrorReporter.exe.

    So: learn on close, from every name seen during the session, skipping
    obvious companion processes, and additively — the game's real executable
    is in that set even when a launcher started first.
    """
    candidates = sorted(
        name for name in seen_names if name and not is_helper_executable(name)
    )
    if not candidates:
        return

    games = load_games(config_path)
    for tracked in games:
        if tracked.id != game.id:
            continue
        # An explicit --exe is the user's choice; never append to it.
        if tracked.executable_names and not tracked.executables_learned:
            return
        known = {name.lower() for name in tracked.executable_names}
        added = [name for name in candidates if name.lower() not in known]
        if not added:
            return
        tracked.executable_names = (tracked.executable_names + added)[
            :_MAX_LEARNED_EXECUTABLES
        ]
        tracked.executables_learned = True
        game.executable_names = list(tracked.executable_names)
        game.executables_learned = True
        save_games(games, config_path)
        console.print(
            f"[dim]Learned executable(s) for {game.title}: {', '.join(added)}[/dim]"
        )
        return


def _cloud_newer_version(
    game: Game,
    config: SyncConfig,
    db: Database,
    rclone_path: Path,
) -> str | None:
    """Return the cloud's latest version id when it is strictly newer than
    anything this machine has produced or applied; None otherwise.

    Safety backups are excluded from the local side so a pre-restore snapshot
    can never lock the cloud out, and the last applied cloud version (from
    sync_state) counts as local knowledge so the same save is not re-restored.
    """
    if not _effective_provider(game, config):
        return None
    remote_name = _effective_remote(game, config)
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
    ludusavi_path: Path | None,
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
    ludusavi_path: Path | None,
    version_id: str,
    force: bool = False,
) -> bool:
    """Download, verify, remap, and restore one cloud version.

    Ordering is deliberate: download and verify FIRST, then remap paths for
    this machine, then the safety backup, then apply — and the restore is
    aborted if the safety backup fails, so local progress is never
    overwritten without a recoverable copy. A failed step changes no state.
    The applied cloud version is recorded in sync_state so automatic
    restore never re-applies it.
    """
    blocked = _refuse_if_running(game, force)
    if blocked:
        console.print(f"[yellow]{blocked}[/yellow]")
        return False

    remote_name = _effective_remote(game, config)
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
    if not _staged_backup_has_content(game, restore_dir):
        console.print(
            f"[red]{game.title}: downloaded save is not a recognizable backup; not restoring.[/red]"
        )
        return False

    # Cross-machine remap of Ludusavi backups: a backup made under another
    # Windows username records that user's profile paths — rewrite them (and
    # the mirrored files) for this machine before applying. Custom-path games
    # restore to their own configured paths, so they need no remap here.
    if not game.custom:
        try:
            remapped = sum(
                apply_remap_to_staged_backup(mp.parent)
                for mp in restore_dir.rglob("mapping.yaml")
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
        _apply_staged_backup(game, restore_dir, ludusavi_path)
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


def auto_add_skip_reason(
    title: str, detected_launcher: str, config_only: set[str]
) -> str | None:
    """Why auto-add should leave this game alone, or None to track it.

    A predicate rather than two inline conditions so the rule can be tested
    directly. Both reasons are things gsg knows and the user does not, and
    both used to be silent: a launcher-managed game is already synced by its
    launcher, and a config-only game has no save data to protect (#47, #48).

    Neither reason applies to an explicit ``gsg add``. Someone who wants their
    settings versioned, or a second copy of a Steam save, is entitled to it.
    """
    if detected_launcher != "other":
        return f"managed by {detected_launcher}"
    if title in config_only:
        return "no save data of its own"
    return None


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
