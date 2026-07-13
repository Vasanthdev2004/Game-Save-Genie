"""Pure sync-decision helpers.

These functions contain no side effects so the restore/backup policy can be
unit-tested independently of Ludusavi, rclone, or the filesystem.

Version ids are UTC timestamps formatted as ``%Y%m%d-%H%M%S-%f`` (see
``ludusavi.backup_game``), so lexicographic string order equals chronological
order.
"""

from __future__ import annotations


def latest_version_id(version_ids: list[str]) -> str | None:
    """Return the newest version id, or ``None`` when the list is empty."""
    if not version_ids:
        return None
    return sorted(version_ids)[-1]


def should_restore_from_cloud(
    local_latest: str | None, cloud_latest: str | None
) -> bool:
    """Decide whether the cloud save should be applied over the local state.

    Restore only when the cloud holds a strictly newer save than the newest
    local version. This prevents clobbering local progress made offline and
    avoids re-restoring a save this machine just uploaded (equal ids).
    """
    if cloud_latest is None:
        return False
    if local_latest is None:
        return True
    return cloud_latest > local_latest
