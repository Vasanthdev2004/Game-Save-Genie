"""Tests for cloud path building, listing normalization, and retention policy."""

from __future__ import annotations

from pathlib import Path

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


# --- S3 endpoint handling --------------------------------------------------
# rclone assumes https for a scheme-less endpoint and, with path style off,
# turns the bucket into a subdomain. Both defaults are wrong for a self-hosted
# server, and together they made every possible input fail in #24.


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://host:9000", True),
        ("https://host", True),
        ("HTTP://HOST:9000", True),
        ("host:9000", False),
        ("192.168.1.10:9000", False),
        ("localhost", False),
        ("", False),
    ],
)
def test_endpoint_scheme_detection(endpoint: str, expected: bool) -> None:
    assert cloud.endpoint_has_scheme(endpoint) is expected


def test_normalize_endpoint_strips_noise() -> None:
    assert cloud.normalize_endpoint("  http://host:9000/  ") == "http://host:9000"


def test_normalize_endpoint_applies_scheme_only_when_missing() -> None:
    assert cloud.normalize_endpoint("host:9000", "http") == "http://host:9000"
    # An explicit scheme is the user's decision and must survive.
    assert cloud.normalize_endpoint("https://host", "http") == "https://host"


def _written_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_path_style: bool = True
) -> dict[str, str]:
    conf = tmp_path / "rclone.conf"
    monkeypatch.setattr(cloud, "get_rclone_config_path", lambda: conf)
    cloud.write_s3_config(
        "homelab", "http://192.0.2.10:9000/", "k", "s", "saves",
        force_path_style=force_path_style,
    )
    return cloud._read_rclone_config()["homelab"]


def test_s3_config_defaults_to_path_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subdomain addressing cannot work self-hosted: nothing resolves
    saves.192.0.2.10, so path style is the only default that can succeed."""
    assert _written_remote(tmp_path, monkeypatch)["force_path_style"] == "true"


def test_s3_config_can_still_use_subdomain_addressing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Railway needs it off, so the choice has to stay reachable."""
    remote = _written_remote(tmp_path, monkeypatch, force_path_style=False)
    assert remote["force_path_style"] == "false"


def test_s3_config_normalizes_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trailing slash becomes a double slash in the signed URL."""
    assert _written_remote(tmp_path, monkeypatch)["endpoint"] == "http://192.0.2.10:9000"


def test_s3_config_keeps_other_remotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = tmp_path / "rclone.conf"
    monkeypatch.setattr(cloud, "get_rclone_config_path", lambda: conf)
    conf.write_text("[gdrive]\ntype = drive\n\n", encoding="utf-8")
    cloud.write_s3_config("homelab", "http://host:9000", "k", "s", "saves")
    sections = cloud._read_rclone_config()
    assert sections["gdrive"]["type"] == "drive"
    assert sections["homelab"]["type"] == "s3"


# --- blob upload flags (#26) -----------------------------------------------
# These build an argv and invoke nothing, so they must live here and not in
# test_cloud_cas.py: that module is skipped whole when rclone is missing,
# which is every CI run. Guarding the upload flags there guarded nothing.


def _upload_args(**kwargs: object) -> list[str]:
    return cloud._blob_upload_args(
        Path("/stage"), "gdrive:saves/game/blobs", **kwargs  # type: ignore[arg-type]
    )


def test_blob_upload_asks_for_one_recursive_listing() -> None:
    """Without this, rclone walks source and destination in step and pays a
    listing per directory it touches. On Drive that is an API call each, and
    blobs are spread over up to 256 shard directories."""
    assert "--fast-list" in _upload_args()


def test_blob_upload_never_asks_per_file() -> None:
    """--no-traverse looks like the alternative and measures worse at every
    size above about five blobs: a backup stages the version's whole blob set,
    so nearly every file it would ask about is already there."""
    assert "--no-traverse" not in _upload_args()


def test_blob_upload_keeps_size_only() -> None:
    """A blob is named for its own hash, so a matching name is proof of
    matching content. Dropping this re-uploads every blob every time."""
    assert "--size-only" in _upload_args()


def test_blob_upload_raises_parallelism_above_the_default_four() -> None:
    """Saves are many small files, so wall clock is round trips, not bytes."""
    args = _upload_args()
    assert args[args.index("--transfers") + 1] == "16"
    assert args[args.index("--checkers") + 1] == "16"


def test_caller_flags_come_last_so_they_win() -> None:
    """rclone takes the final occurrence of a repeated flag, so a caller's
    --transfers, or a --dry-run, has to be able to override ours."""
    args = _upload_args(extra_args=["--dry-run", "--transfers", "2"])
    assert args[-3:] == ["--dry-run", "--transfers", "2"]
    assert args.index("--dry-run") > args.index("--size-only")


def test_blob_upload_still_copies_stage_to_remote() -> None:
    """The flags are an optimisation; the operation must not have moved."""
    args = _upload_args()
    assert args[:3] == ["copy", str(Path("/stage")), "gdrive:saves/game/blobs"]


def test_blob_download_is_parallel_too() -> None:
    """A restore is the same many-small-files shape as an upload. --files-from
    already suppresses traversal there, so parallelism is what is left."""
    assert cloud._CAS_PARALLELISM == ["--transfers", "16", "--checkers", "16"]
