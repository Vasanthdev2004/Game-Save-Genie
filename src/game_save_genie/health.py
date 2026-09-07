"""Local evidence of save protection, shared by the CLI, dashboard and tray.

This does not poll the remote or claim that an uploaded object still exists.
It reports what this machine last completed and what still needs attention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .database import Database
from .models import Game, SaveVersion, SyncConfig


def upload_target(game: Game, config: SyncConfig) -> tuple[str, str] | None:
    remote = game.remote_path or config.rclone_remote_name
    if not (game.cloud_provider or config.cloud_provider) or not remote:
        return None
    return remote, config.remote_root


def uploaded_to(version: SaveVersion, target: tuple[str, str]) -> bool:
    remote, root = target
    prefix = f"{remote}:{root.rstrip('/') + '/' if root else ''}{version.game_id}/"
    return version.cloud_synced and (version.cloud_remote_path or "").startswith(prefix)


@dataclass(frozen=True)
class SaveHealth:
    state: str
    detail: str
    last_backup: datetime | None
    last_upload: datetime | None
    pending: int

    @property
    def color(self) -> str:
        return {"Protected": "green", "Needs attention": "red", "Paused": "dim"}.get(
            self.state, "yellow"
        )


def describe_health(game: Game, config: SyncConfig, db: Database) -> SaveHealth:
    versions = [v for v in db.get_versions(game.id) if v.origin != "safety"]
    latest = versions[0] if versions else None
    jobs = db.get_upload_jobs(game.id)
    target = upload_target(game, config)
    uploaded = [v.cloud_synced_at for v in versions if target and uploaded_to(v, target)
                and v.cloud_synced_at is not None]
    last_upload = max(uploaded) if uploaded else None

    def result(state: str, detail: str) -> SaveHealth:
        return SaveHealth(state, detail, latest.created_at if latest else None, last_upload, len(jobs))

    issue = db.get_backup_issue(game.id)
    if db.get_deferred_backups(game.id):
        return result("Needs attention", f"{issue or 'Automatic backup is waiting.'} "
                      "gsg auto and gsg watch retry it automatically when protection is active.")
    if issue:
        return result("Needs attention", f"{issue} Retry a backup after fixing this.")
    blocked = [job.version_id for job in jobs if job.blocked]
    if blocked:
        return result("Needs attention", f"Upload blocked for snapshot(s): {', '.join(blocked)}. "
                      "Restore each snapshot's original local file, then press u or run gsg retry "
                      "to revalidate it. Files and upload intent are retained; creating a new "
                      "backup does not repair these historical snapshots.")
    if latest and not latest.local_path.exists():
        return result("Needs attention", "The latest local snapshot is missing. Create a new backup.")
    if not game.auto_sync or not game.sync_enabled:
        return result("Paused", "Upload retries are paused. Restart any running watcher to apply "
                      "backup-watching changes; resume to continue.")
    if jobs:
        if any((job.remote_name, job.remote_root) != target for job in jobs):
            return result("Needs attention", "Queued uploads belong to another cloud destination. "
                          "Restore the previous cloud settings to retry them.")
        error = next((job.last_error for job in jobs if job.last_error), None)
        detail = f"{len(jobs)} upload(s) queued. gsg auto retries automatically; press u or run gsg retry."
        if error:
            detail += f" Last error: {error}"
        return result("Waiting to upload", detail)
    if latest is None:
        return result("Needs attention", "No backup yet. Press b or run gsg backup to protect this game.")
    if target is None:
        return result("Local only", "A local snapshot exists. Configure cloud storage for a second copy.")
    if not uploaded_to(latest, target):
        return result("Local only", "The latest snapshot has not been uploaded to this destination. "
                      "Press u or run gsg retry to upload it.")
    return result("Protected", "Latest snapshot saved locally and uploaded successfully. "
                  "Cloud availability has not been rechecked.")
