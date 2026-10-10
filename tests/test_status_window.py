"""The tray's status process is independent of the background watcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from game_save_genie import cli


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("custom_config", [False, True])
def test_status_window_has_its_own_frozen_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    platform: str, frozen: bool, custom_config: bool,
) -> None:
    exe = tmp_path / "Game Save Genie" / "gsg.exe"
    config_path = tmp_path / "My settings" / "config.yaml" if custom_config else None
    monkeypatch.setattr(cli, "os", SimpleNamespace(name=platform, environ=os.environ))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(cli, "_find_gsg_exe", lambda: exe)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/xterm" if name == "xterm" else None)
    monkeypatch.setenv("PYINSTALLER_RESET_ENVIRONMENT", "0")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "watcher-extraction-directory")
    monkeypatch.setenv("GSG_STATUS_TEST_SENTINEL", "preserve-child-settings")
    spawn = Mock()
    monkeypatch.setattr(subprocess, "Popen", spawn)

    cli._open_status_window(config_path)

    assert spawn.call_count == 1
    args = [str(exe), "status"]
    if config_path:
        args = [str(exe), "--config", str(config_path), "status"]
    if platform == "nt":
        assert spawn.call_args.args[0] == ["cmd", "/k", *args]
        assert spawn.call_args.kwargs["creationflags"] == cli._CREATE_NEW_CONSOLE
    else:
        assert spawn.call_args.args[0] == ["xterm", "-e", *args]

    child_env = spawn.call_args.kwargs.get("env")
    if frozen:
        assert child_env is not None
        assert child_env is not os.environ
        assert child_env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert child_env["GSG_STATUS_TEST_SENTINEL"] == "preserve-child-settings"
        # Let the bootloader reset its private state; do not hand-edit it.
        assert child_env["_PYI_APPLICATION_HOME_DIR"] == "watcher-extraction-directory"
    else:
        assert child_env is None
    # A menu click must not alter the watcher's environment or other children.
    assert os.environ["PYINSTALLER_RESET_ENVIRONMENT"] == "0"
    assert os.environ["_PYI_APPLICATION_HOME_DIR"] == "watcher-extraction-directory"


def test_status_spawn_failure_is_logged_without_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli, "_find_gsg_exe", lambda: tmp_path / "gsg.exe")
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=OSError("terminal unavailable")))

    cli._open_status_window(None)

    assert "Could not show status: terminal unavailable" in caplog.text
