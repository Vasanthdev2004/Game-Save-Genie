"""Tests for cloud path building, listing normalization, and retention policy."""

from __future__ import annotations

import pytest

from game_save_genie import cloud
from game_save_genie.cloud import (
    _remote_path,
    parse_lsf_entries,
    select_entries_to_prune,
)


def test_remote_path_with_root() -> None:
    assert _remote_path("railway", "bucket", "game", "v1.zip") == "railway:bucket/game/v1.zip"


def test_remote_path_without_root() -> None:
    assert _remote_path("railway", "", "game", "v1.zip") == "railway:game/v1.zip"


def test_parse_lsf_normalizes_zip_and_dirs() -> None:
    stdout = "20260101-000000-000000.zip\n20260102-000000-000000/\n"
    entries = parse_lsf_entries(stdout)
    assert entries == [
        ("20260101-000000-000000", "20260101-000000-000000.zip"),
        ("20260102-000000-000000", "20260102-000000-000000/"),
    ]


def test_parse_lsf_skips_reserved_and_dedupes() -> None:
    stdout = "_meta.json\nv1.zip\nv1/\n\n"
    entries = parse_lsf_entries(stdout)
    assert entries == [("v1", "v1.zip")]


def test_parse_lsf_legacy_nested_zip_dir() -> None:
    # Legacy uploads produced <id>.zip/<id>.zip; lsf shows the outer dir.
    entries = parse_lsf_entries("20260101-000000-000000.zip/\n")
    assert entries == [("20260101-000000-000000", "20260101-000000-000000.zip/")]


def test_prune_selection_keeps_newest() -> None:
    entries = [
        ("20260103-000000-000000", "20260103-000000-000000.zip"),
        ("20260101-000000-000000", "20260101-000000-000000.zip"),
        ("20260102-000000-000000", "20260102-000000-000000/"),
    ]
    pruned = select_entries_to_prune(entries, keep=1)
    assert [vid for vid, _ in pruned] == [
        "20260101-000000-000000",
        "20260102-000000-000000",
    ]


def test_prune_selection_noop_when_under_limit() -> None:
    entries = [("v1", "v1.zip"), ("v2", "v2.zip")]
    assert select_entries_to_prune(entries, keep=5) == []
    assert select_entries_to_prune(entries, keep=2) == []
    assert select_entries_to_prune([], keep=1) == []


def test_prune_selection_refuses_bad_keep() -> None:
    entries = [("v1", "v1.zip"), ("v2", "v2.zip")]
    assert select_entries_to_prune(entries, keep=0) == []
    assert select_entries_to_prune(entries, keep=-3) == []


# --- rclone asset selection ------------------------------------------------
# Every name here is copied from a real rclone release. The Linux entry is the
# whole bug: the code asked for linux-amd64.tar.gz, which rclone has never
# published, so `gsg` could not install itself on Linux at all.

_RCLONE_RELEASE = {
    "assets": [
        {"name": "rclone-v1.75.0-linux-amd64.deb"},
        {"name": "rclone-v1.75.0-linux-amd64.rpm"},
        {"name": "rclone-v1.75.0-linux-amd64.zip"},
        {"name": "rclone-v1.75.0-linux-arm64.zip"},
        {"name": "rclone-v1.75.0-linux-386.zip"},
        {"name": "rclone-v1.75.0-osx-amd64.zip"},
        {"name": "rclone-v1.75.0-osx-arm64.zip"},
        {"name": "rclone-v1.75.0-windows-amd64.zip"},
        {"name": "rclone-v1.75.0-windows-arm64.zip"},
    ]
}


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "rclone-v1.75.0-linux-amd64.zip"),
        ("Linux", "aarch64", "rclone-v1.75.0-linux-arm64.zip"),
        ("Linux", "i686", "rclone-v1.75.0-linux-386.zip"),
        ("Darwin", "x86_64", "rclone-v1.75.0-osx-amd64.zip"),
        ("Darwin", "arm64", "rclone-v1.75.0-osx-arm64.zip"),
        ("Windows", "AMD64", "rclone-v1.75.0-windows-amd64.zip"),
        ("Windows", "ARM64", "rclone-v1.75.0-windows-arm64.zip"),
    ],
)
def test_rclone_asset_matches_platform(
    system: str, machine: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: system)
    monkeypatch.setattr(platform_module, "machine", lambda: machine)
    assert cloud._rclone_asset_name(_RCLONE_RELEASE) == expected


def test_rclone_asset_never_asks_for_a_tarball(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original bug. rclone ships .zip on every platform including Linux."""
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: "Linux")
    monkeypatch.setattr(platform_module, "machine", lambda: "x86_64")
    assert cloud._rclone_asset_name(_RCLONE_RELEASE).endswith(".zip")


def test_rclone_asset_does_not_hand_arm_an_intel_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architecture was hardcoded to amd64, so arm64 got the wrong binary."""
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: "Linux")
    monkeypatch.setattr(platform_module, "machine", lambda: "aarch64")
    assert "arm64" in cloud._rclone_asset_name(_RCLONE_RELEASE)
    assert "amd64" not in cloud._rclone_asset_name(_RCLONE_RELEASE)


def test_rclone_asset_error_points_at_the_workaround(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported platform should say what to do, not just fail."""
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: "Haiku")
    monkeypatch.setattr(platform_module, "machine", lambda: "sparc")
    with pytest.raises(RuntimeError, match="checks PATH"):
        cloud._rclone_asset_name(_RCLONE_RELEASE)
