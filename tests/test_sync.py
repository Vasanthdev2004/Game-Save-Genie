from game_save_genie.sync import (
    effective_local_latest,
    latest_version_id,
    should_restore_from_cloud,
)


def test_latest_version_id_empty() -> None:
    assert latest_version_id([]) is None


def test_latest_version_id_picks_newest() -> None:
    ids = [
        "20260101-000000-000000",
        "20260713-120000-000000",
        "20260201-000000-000000",
    ]
    assert latest_version_id(ids) == "20260713-120000-000000"


def test_restore_when_no_local_but_cloud_exists() -> None:
    assert should_restore_from_cloud(None, "20260713-120000-000000") is True


def test_no_restore_when_cloud_missing() -> None:
    assert should_restore_from_cloud("20260713-120000-000000", None) is False
    assert should_restore_from_cloud(None, None) is False


def test_restore_only_when_cloud_strictly_newer() -> None:
    local = "20260713-120000-000000"
    newer = "20260713-130000-000000"
    older = "20260713-110000-000000"
    assert should_restore_from_cloud(local, newer) is True
    assert should_restore_from_cloud(local, older) is False
    assert should_restore_from_cloud(local, local) is False


def test_effective_local_latest_combines_sources() -> None:
    local = "20260713-120000-000000"
    restored = "20260713-130000-000000"
    assert effective_local_latest(local, restored) == restored
    assert effective_local_latest(restored, local) == restored
    assert effective_local_latest(local, None) == local
    assert effective_local_latest(None, restored) == restored
    assert effective_local_latest(None, None) is None


def test_no_re_restore_after_cloud_restore() -> None:
    """The cloud version just applied must not be restored again next launch."""
    cloud_latest = "20260713-130000-000000"
    local_latest = "20260713-120000-000000"  # pre-restore local backup
    effective = effective_local_latest(local_latest, cloud_latest)
    assert should_restore_from_cloud(effective, cloud_latest) is False


def test_failed_restore_retries_next_launch() -> None:
    """Without a sync-state record (failed restore), the gate stays open."""
    cloud_latest = "20260713-130000-000000"
    local_latest = "20260713-120000-000000"  # safety backups excluded upstream
    effective = effective_local_latest(local_latest, None)
    assert should_restore_from_cloud(effective, cloud_latest) is True


# --- clock skew (#39) ------------------------------------------------------
# Version ids are wall clock and sync orders them as strings, so one machine
# booting with a dead RTC used to pin every other machine's "newest seen" to a
# date that never arrives.

from datetime import datetime, timedelta, timezone  # noqa: E402

from game_save_genie.sync import (  # noqa: E402
    is_implausibly_future,
    parse_version_id,
)


def _stamp(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y%m%d-%H%M%S-%f")


def test_a_version_stamped_years_ahead_is_implausible() -> None:
    assert is_implausibly_future(_stamp(timedelta(days=3650)))


def test_an_ordinary_recent_version_is_plausible() -> None:
    assert not is_implausibly_future(_stamp(timedelta(hours=-1)))
    assert not is_implausibly_future(_stamp(timedelta(hours=1)))


def test_a_modest_clock_disagreement_is_tolerated() -> None:
    """Two machines an hour or two apart is normal; refusing those would
    break ordinary sync to guard against a rare one."""
    assert not is_implausibly_future(_stamp(timedelta(hours=12)))


def test_an_unparseable_id_is_not_treated_as_future() -> None:
    """Legacy and third-party ids exist; they get the ordinary comparison."""
    assert not is_implausibly_future("not-a-timestamp")
    assert parse_version_id("not-a-timestamp") is None


def test_a_future_stamped_cloud_version_is_never_restored() -> None:
    """The bug: it used to compare as newest forever, so `gsg pull` reported
    'already up to date' on every machine with no way to recover."""
    from game_save_genie.sync import should_restore_from_cloud

    local = _stamp(timedelta(days=-2))
    assert not should_restore_from_cloud(local, _stamp(timedelta(days=3650)))


def test_a_genuinely_newer_cloud_version_still_restores() -> None:
    from game_save_genie.sync import should_restore_from_cloud

    assert should_restore_from_cloud(_stamp(timedelta(days=-2)), _stamp(timedelta(hours=-1)))
