"""End-to-end CAS tests against a real rclone binary and a local remote.

Skipped when rclone is not installed (e.g. minimal CI). Uses a throwaway
RCLONE_CONFIG and a `local`-type remote in a temp dir — never touches any
real cloud config.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_save_genie import cloud
from game_save_genie.archive import zip_directory
from game_save_genie.config import get_default_binary_dir
from game_save_genie.models import Game, Platform, SaveVersion


def _find_rclone() -> Path | None:
    found = shutil.which("rclone")
    if found:
        return Path(found)
    for name in ("rclone.exe", "rclone"):
        candidate = get_default_binary_dir() / name
        if candidate.exists():
            return candidate
    return None


RCLONE = _find_rclone()
pytestmark = pytest.mark.skipif(RCLONE is None, reason="rclone binary not available")

GAME = Game(id="test-game", title="Test Game", platform=Platform.WINDOWS)


@pytest.fixture()
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    conf = tmp_path / "rclone.conf"
    conf.write_text("[test]\ntype = local\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    # Isolate the GC throttle stamp from the real data dir.
    monkeypatch.setattr("game_save_genie.cloud.get_data_dir", lambda: tmp_path / "data")
    store = tmp_path / "store"
    store.mkdir()
    return "test", str(store).replace("\\", "/")


def _build_tree(root: Path, slot1: bytes) -> None:
    d = root / "Test Game" / "drive-C" / "Users" / "vasan" / "saves"
    d.mkdir(parents=True, exist_ok=True)
    (root / "Test Game" / "mapping.yaml").write_text("name: Test Game\n", encoding="utf-8")
    (d / "slot1.sav").write_bytes(slot1)
    (d / "slot2.sav").write_bytes(b"UNCHANGED" * 10000)


def _version(vid: str, tree: Path, tmp: Path) -> SaveVersion:
    zip_path = tmp / f"{vid}.zip"
    digest = zip_directory(tree, zip_path)
    return SaveVersion(
        id=vid, game_id=GAME.id, created_at=datetime.now(timezone.utc),
        local_path=zip_path, size_bytes=zip_path.stat().st_size, file_count=3,
        platform=Platform.WINDOWS, sha256=digest,
    )


def _blob_count(name: str, root: str) -> int:
    r = cloud.run_rclone(
        RCLONE,  # type: ignore[arg-type]
        ["lsf", cloud._remote_path(name, root, GAME.id, "blobs"), "-R", "--files-only"],
        check=False,
    )
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def test_cas_dedup_download_prune_gc(remote: tuple[str, str], tmp_path: Path) -> None:
    name, root = remote
    assert RCLONE is not None

    t1 = tmp_path / "t1"
    _build_tree(t1, b"V1" * 5000)
    v1 = _version("20260718-100000-000000", t1, tmp_path)
    assert cloud.upload_save_cas(RCLONE, GAME, v1, name, root).success
    n1 = _blob_count(name, root)
    assert n1 >= 3

    # Only slot1 changes; slot2 identical -> exactly one new blob.
    t2 = tmp_path / "t2"
    _build_tree(t2, b"V2-CHANGED" * 5000)
    v2 = _version("20260718-101000-000000", t2, tmp_path)
    assert cloud.upload_save_cas(RCLONE, GAME, v2, name, root).success
    assert _blob_count(name, root) == n1 + 1

    assert set(cloud.list_remote_versions(RCLONE, GAME, name, root)) == {v1.id, v2.id}

    # Download v2, byte-identical.
    dl = tmp_path / "dl"
    assert cloud.download_save(RCLONE, GAME, v2.id, dl, name, root).success
    got = dl / "Test Game" / "drive-C" / "Users" / "vasan" / "saves" / "slot1.sav"
    assert got.read_bytes() == (t2 / "Test Game" / "drive-C" / "Users" / "vasan" / "saves" / "slot1.sav").read_bytes()

    # Prune keep=1 -> v1 manifest gone. The throttled/graced GC inside prune
    # leaves the (freshly written) orphan blob in place.
    assert cloud.prune_remote_versions(RCLONE, GAME, name, root, keep=1) == [v1.id]
    assert cloud.list_remote_versions(RCLONE, GAME, name, root) == [v2.id]
    assert _blob_count(name, root) == n1 + 1  # grace period protects the fresh orphan

    # Fresh blobs are protected by the grace window.
    assert cloud.gc_blobs(RCLONE, GAME, name, root) == 0
    # Forcing grace=0 collects exactly the one orphaned blob, keeping v2's.
    assert cloud.gc_blobs(RCLONE, GAME, name, root, grace_seconds=0) == 1
    assert _blob_count(name, root) == n1

    dl2 = tmp_path / "dl2"
    assert cloud.download_save(RCLONE, GAME, v2.id, dl2, name, root).success
    assert (dl2 / "Test Game" / "mapping.yaml").is_file()


def test_legacy_zip_still_works_alongside_cas(remote: tuple[str, str], tmp_path: Path) -> None:
    """A bucket with an old flat zip AND a new CAS version: both list & download."""
    name, root = remote
    assert RCLONE is not None

    # Legacy flat-zip upload (the pre-CAS format).
    t_old = tmp_path / "old"
    _build_tree(t_old, b"OLD" * 5000)
    v_old = _version("20260717-100000-000000", t_old, tmp_path)
    assert cloud.upload_save(RCLONE, GAME, v_old, name, root).success

    # New CAS upload.
    t_new = tmp_path / "new"
    _build_tree(t_new, b"NEW" * 5000)
    v_new = _version("20260718-100000-000000", t_new, tmp_path)
    assert cloud.upload_save_cas(RCLONE, GAME, v_new, name, root).success

    assert set(cloud.list_remote_versions(RCLONE, GAME, name, root)) == {v_old.id, v_new.id}

    # The legacy zip downloads via the legacy path.
    dl_old = tmp_path / "dlold"
    assert cloud.download_save(RCLONE, GAME, v_old.id, dl_old, name, root).success
    got = dl_old / "Test Game" / "drive-C" / "Users" / "vasan" / "saves" / "slot1.sav"
    assert got.read_bytes() == b"OLD" * 5000
