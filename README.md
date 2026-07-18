# Game Save Genie

Self-hosted cloud save sync for PC games that don't have their own — Hydra and other manual installs, GOG offline installers, emulator-adjacent titles. Wraps the open-source [Ludusavi](https://github.com/mtkennerly/ludusavi) save-detection engine (19,000+ games) and [rclone](https://rclone.org/) cloud transport. No subscription, no storage caps you don't control, your saves live in your own bucket.

## What it does

- **Fully automatic mode** (`gsg auto`): scans your machine, auto-tracks every non-launcher game, then watches processes in the background — backing up when a game closes and every 10 minutes while it runs. Newer cloud saves are pulled down automatically at startup and whenever the game isn't running, so a machine that's behind catches up before you play (never underneath a live game).
- **Real save versioning**: every backup is frozen into an immutable per-version zip with a SHA-256 checksum — roll back to any play session with `gsg restore --version`.
- **Cloud retention**: `max_versions` is enforced both locally and in the cloud, so your bucket doesn't grow forever.
- **Safety-first restores**: every restore (manual or automatic) verifies the snapshot's integrity first, then takes a safety backup of your current saves before touching anything. A failed download or restore changes nothing and retries cleanly.
- **Launcher filtering**: Steam/Epic/Xbox games are detected and skipped by default — those launchers already sync saves.
- **Run at boot**: `gsg auto --install` starts the watcher automatically on Windows startup.

Steam/Epic/Xbox already cloud-sync their own games. Game Save Genie exists for everything else.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/Vasanthdev2004/game-save-genie
cd game-save-genie
pip install -e .
```

Ludusavi and rclone are downloaded automatically on first use (or point at your own binaries with `gsg config --ludusavi / --rclone`).

## Quick start (automatic mode)

```bash
# 1. One-time cloud setup (Railway S3 — Hobby plan includes 5 GB)
#    Create a bucket in the Railway dashboard, then:
gsg setup-railway
# prompts for endpoint, access key, secret key, bucket name

# 2. Start fully automatic backup
gsg auto
# scans for non-launcher games, auto-tracks them, and starts watching

# 3. (Optional) start on boot
gsg auto --install
```

That's it. Play games; saves are backed up on close and every 10 minutes during play. On a machine that's behind, newer cloud saves are applied at watcher startup and during idle checks — if you launch a game while a newer cloud save exists, you get a notification instead of a mid-session overwrite.

## Manual commands

```bash
gsg scan                    # See detected games (default: non-launcher only; --source all)
gsg add "Elden Ring" --exe eldenring.exe   # Track a game manually
gsg list                    # Tracked games
gsg backup [game-id]        # Back up one or all games (--dry-run previews, changes nothing)
gsg versions <game-id>      # List local versions
gsg cloud-list <game-id>    # List cloud versions
gsg restore <game-id> [--version ID]       # Restore latest or a specific version
gsg status                  # Per-game overview + storage usage and quota warning
gsg usage                   # Local + remote storage totals
gsg pause / resume <game-id>  # Exclude/re-include a game from auto-backup
gsg remove <game-id> [--purge]  # Untrack (--purge also deletes local + cloud saves)
gsg watch                   # Simple watcher: backup-on-close only, no auto-restore
```

## Cloud providers

**Railway S3** is the guided path (`gsg setup-railway`). Any rclone-supported provider works:

```bash
gsg setup-rclone mydrive        # interactive rclone config
gsg config --rclone-remote mydrive --cloud-provider google_drive
```

The remote layout is `<remote>:<remote_root>/<game-id>/<version-id>.zip` — one compressed object per version.

## Configuration

Config lives at `%APPDATA%\game-save-genie\Game Save Genie\config.yaml` on Windows (`~/.config/Game Save Genie/` on Linux). View or edit with `gsg config`:

| Key | Default | Meaning |
|---|---|---|
| `backup_dir` | `<data>/backups` | Local backup root |
| `max_versions` | `10` | Versions kept per game, locally **and** in the cloud |
| `cloud_provider` | – | Default provider (`s3`, `google_drive`, …) |
| `rclone_remote_name` | – | Name of the rclone remote to upload through |
| `remote_root` | `game-save-genie` | Root folder / bucket name on the remote |
| `storage_limit_gb` | `5.0` | Warn in `gsg status` at 80% of this (0 = off) |
| `ludusavi_path` / `rclone_path` | auto-download | Custom binary paths |

## How versioning works

Each backup runs Ludusavi into a per-game working directory, then freezes that directory into `backups/_versions/<game-id>/<version-id>.zip` with a SHA-256 checksum recorded in a local SQLite database. Restores extract the chosen snapshot to a staging directory, verify it, take a safety backup of your current saves (aborting if that fails), and only then apply. Automatic cloud restore only fires when the cloud is *strictly newer* than anything this machine has seen, and only while the game is not running — offline progress is never clobbered, safety backups live in their own retention pool so they never evict real backups, and a failed download or restore changes nothing and retries at the next idle check.

## Project structure

```
src/game_save_genie/
  cli.py            # Typer CLI — all commands and orchestration
  config.py         # Config + tracked-games persistence (platformdirs + YAML)
  models.py         # Pydantic models
  database.py       # SQLite version + sync-state tracking
  ludusavi.py       # Ludusavi binary wrapper (scan/backup/restore)
  cloud.py          # rclone wrapper: upload/download/list/prune, Railway S3 setup
  archive.py        # Safe zip/tar extraction, snapshot zipping, hashing
  sync.py           # Pure restore-decision policy (unit-tested)
  watcher.py        # psutil process watcher (start/close/periodic callbacks)
  launcher.py       # Steam/Epic/Xbox detection for scan filtering
  notify.py         # Rotating file log + Windows toast notifications
  remap.py          # Cross-platform path remapping (groundwork for gsg pull)
```

## Development

```bash
pip install -e ".[dev]"
pytest            # tests
ruff check src tests
mypy src tests    # strict mode
```

## License

MIT
