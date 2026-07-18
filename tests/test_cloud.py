"""Tests for cloud path building, listing normalization, and retention policy."""

from __future__ import annotations

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
