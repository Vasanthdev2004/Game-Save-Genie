"""Pure sync-decision helpers.

These functions contain no side effects so the restore/backup policy can be
unit-tested independently of Ludusavi, rclone, or the filesystem.

Version ids are UTC timestamps formatted as ``%Y%m%d-%H%M%S-%f`` (see
``ludusavi.backup_game``), so lexicographic string order equals chronological
order.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_VERSION_ID_FORMAT = "%Y%m%d-%H%M%S-%f"

# How far ahead of this machine's clock a version id may be stamped before it
# is treated as a broken clock rather than a real save. Generous enough to
# absorb an ordinary timezone or NTP disagreement between two machines, far
# short of the years-ahead stamps a dead RTC produces.
_FUTURE_TOLERANCE = timedelta(hours=24)


def parse_version_id(version_id: str) -> datetime | None:
    """The UTC timestamp encoded in a version id, or None if unparseable."""
    try:
        return datetime.strptime(version_id, _VERSION_ID_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def is_implausibly_future(version_id: str, now: datetime | None = None) -> bool:
    """Whether a version id is stamped so far ahead that it cannot be real.

    Version ids are wall clock, and the whole sync policy orders them as
    strings. A machine that boots once with a dead RTC writes an id years
    ahead; every other machine then treats it as the newest save forever, so
    `gsg pull` reports "already up to date" and cross-machine sync is dead
    with no way to recover short of editing the bucket by hand (#39).

    An id we cannot parse is not implausible - it may be a legacy or
    third-party id - and is left to the ordinary comparison.
    """
    stamped = parse_version_id(version_id)
    if stamped is None:
        return False
    reference = now or datetime.now(timezone.utc)
    return stamped - reference > _FUTURE_TOLERANCE


def latest_version_id(version_ids: list[str]) -> str | None:
    """Return the newest version id, or ``None`` when the list is empty."""
    if not version_ids:
        return None
    return max(version_ids)


def effective_local_latest(
    local_latest: str | None, last_restored: str | None
) -> str | None:
    """Combine the newest local version with the last cloud version applied.

    A cloud restore does not create a local version row for the restored id,
    so the sync-state record of the last applied cloud version must also count
    as "local knowledge" — otherwise the same cloud save would be re-restored
    on every launch.
    """
    candidates = [v for v in (local_latest, last_restored) if v is not None]
    if not candidates:
        return None
    return max(candidates)


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
    if is_implausibly_future(cloud_latest):
        # Adopting this would pin every machine's "newest seen" to a date
        # that never arrives. Refusing keeps sync working with the versions
        # that are real.
        logger.warning(
            "Ignoring cloud version %s: stamped far in the future, which "
            "means the machine that wrote it had a wrong clock.",
            cloud_latest,
        )
        return False
    if local_latest is None:
        return True
    return cloud_latest > local_latest
