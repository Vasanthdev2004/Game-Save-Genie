"""Wiring assertions for fixes that have already shipped.

Every fix in 0.6.1 through 0.6.3 was pinned at the helper and not at the call
site. Reverting the production wiring left the whole suite green, so a
refactor or a merge could silently undo a shipped fix and the next signal
would be another bug report (#44).

These tests assert the production path *uses* each helper. They are
deliberately structural. Behaviour is covered elsewhere; what is covered here
is that the behaviour is still reachable.
"""

from __future__ import annotations

import codecs
import inspect
import os
from pathlib import Path

import pytest

from game_save_genie import cli, cloud, ludusavi


def test_startup_script_is_written_as_utf16_bytes() -> None:
    """0.6.3. Reverting this call site to write_text(encoding="utf-8") used to
    pass 229 tests while restoring the exact crash that was reported."""
    source = inspect.getsource(cli._install_startup)
    assert "encode_vbs" in source
    assert "write_text" not in source


@pytest.mark.skipif(
    os.name != "nt",
    reason="_install_startup writes a systemd unit off Windows, not a .vbs",
)
def test_installed_startup_file_really_begins_with_a_utf16_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same fix from the other end: run the real installer and read the
    bytes, so this survives a rewrite that keeps the helper but drops it.

    Windows only, and deliberately so: on Linux this function installs a
    systemd user service into the real ~/.config, which a test has no
    business doing. The structural assertion above covers both platforms.
    """
    target = tmp_path / "Startup" / "GameSaveGenie.vbs"
    monkeypatch.setattr(cli, "_startup_vbs_path", lambda: target)
    monkeypatch.setattr(cli, "_find_gsg_exe", lambda: tmp_path / "gsg.exe")
    cli._install_startup(None)
    raw = target.read_bytes()
    assert raw.startswith(codecs.BOM_UTF16_LE)
    assert not raw.startswith(codecs.BOM_UTF8)


def test_ludusavi_is_looked_up_on_path_before_downloading() -> None:
    """0.6.3. The only escape hatch for architectures upstream does not
    build - ARM Linux and Intel macOS have no official Ludusavi binary."""
    source = inspect.getsource(ludusavi.get_ludusavi_path)
    assert "shutil.which" in source
    assert source.index("shutil.which") < source.index("download_ludusavi")


def test_unsupported_architectures_refuse_before_downloading() -> None:
    """Downloading first meant caching a binary that cannot exec, which then
    failed identically on every later run with no way out."""
    source = inspect.getsource(ludusavi._ludusavi_asset_name)
    assert "unsupported_architecture_reason" in source


def test_blob_upload_asks_for_one_recursive_listing() -> None:
    """0.6.2. Without it rclone pays a listing per directory the copy touches,
    which on Drive is an API call each."""
    args = cloud._blob_upload_args(Path("/stage"), "remote:blobs")
    assert "--fast-list" in args
    assert "--size-only" in args


def test_the_upload_path_uses_the_flag_builder() -> None:
    """Structural half: the flags exist and the caller still calls them."""
    source = inspect.getsource(cloud.upload_save_cas)
    assert "_blob_upload_args" in source


def test_s3_config_defaults_to_path_style() -> None:
    """0.6.2. Subdomain addressing cannot resolve for a self-hosted server."""
    signature = inspect.signature(cloud.write_s3_config)
    assert signature.parameters["force_path_style"].default is True


def test_s3_setup_probes_https_before_http() -> None:
    """0.6.2. A scheme-less endpoint silently became HTTPS; the fallback must
    try TLS first so an explicit https:// is never quietly downgraded."""
    assert cli._endpoint_candidates("host:9000") == [
        "https://host:9000",
        "http://host:9000",
    ]
    assert cli._endpoint_candidates("https://host") == ["https://host"]


def test_rclone_asset_name_is_derived_from_the_running_platform() -> None:
    """0.6.1. Both the extension and the architecture were hardcoded, so no
    Linux machine could install at all."""
    source = inspect.getsource(cloud._rclone_asset_name)
    assert "_RCLONE_OS" in source
    assert "_RCLONE_ARCH" in source
    assert ".tar.gz" not in source.split('"""')[2]  # not in the code body


def test_click_is_a_declared_runtime_dependency() -> None:
    """0.6.1, contributed externally. It was imported but never declared, so a
    plain pip install crashed on first run."""
    # Parsed by hand rather than with tomllib, which is 3.11+ while this
    # project supports 3.10 and CI runs it.
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    block = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "click" in block.lower()
