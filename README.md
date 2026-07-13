# Game Save Genie

Self-hosted cloud save sync for games. Replaces paid cloud save services by wrapping the open-source [Ludusavi](https://github.com/mtkennerly/ludusavi) save backup engine with [rclone](https://rclone.org/) cloud sync.

## Features

- **Automatic save detection** for 19,000+ games via Ludusavi
- **Cloud sync** to any rclone-supported provider (Google Drive, OneDrive, Dropbox, S3, etc.)
- **Save versioning & rollback** with automatic pruning
- **Auto-backup on game close** via process watcher
- **Cross-platform & cross-machine restore** with path remapping
- **Dry-run support** to preview changes before they happen
- **Self-hosted & free** - no subscription required

## Install

```bash
pip install -e .
```

Or install from the latest release.

## Quick Start

```bash
# Initialize configuration and data directories
gsg init

# Scan your installed games and their save locations
gsg scan

# Add a game to track (the auto-watcher will use the executable name)
gsg add "Elden Ring" --exe "eldenring.exe"

# Back up a specific game (or omit ID to back up all tracked games)
gsg backup elden-ring

# List backed-up versions
gsg versions elden-ring

# Restore the latest version
gsg restore elden-ring

# Watch for games and auto-backup when they close
gsg watch
```

## Cloud Setup

### Generic rclone provider

```bash
# Configure rclone interactively for your provider
gsg setup-rclone gdrive

# Set it as the default provider and remote name
gsg config --cloud-provider google_drive --remote-root gdrive

# Back up a game and upload to cloud automatically
gsg backup elden-ring
```

### Railway S3 (Hobby Plan includes 5 GB)

```bash
# Install Railway CLI locally (already included if you cloned this repo)
npm install @railway/cli

# Create a bucket in the Railway dashboard and get S3 credentials
# Then run the interactive setup
gsg setup-railway

# It will ask for: endpoint, access key, secret key, bucket name
# After setup, backups go straight to your Railway bucket
gsg backup elden-ring

# Check how much of your 5 GB is used
gsg usage
```

## Configuration

Config lives in the platform config directory (e.g. `%APPDATA%\Game Save Genie\config.yaml` on Windows, `~/.config/Game Save Genie/config.yaml` on Linux).

Key settings:

- `backup_dir`: Local backup directory
- `max_versions`: Number of versions to keep per game
- `cloud_provider`: Default cloud provider
- `remote_root`: rclone remote name / root folder name
- `ludusavi_path`: Path to custom Ludusavi binary
- `rclone_path`: Path to custom rclone binary

## Project Structure

```
src/game_save_genie/
  __init__.py       # Package metadata
  cli.py            # Typer CLI
  config.py         # Config + game list persistence
  database.py       # SQLite version tracking
  models.py         # Pydantic models
  ludusavi.py       # Ludusavi binary wrapper
  cloud.py          # rclone cloud sync wrapper
  remap.py          # Cross-platform path remapping
  watcher.py        # Process watcher for auto-backup
```

## License

MIT
