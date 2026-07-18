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
