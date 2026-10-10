"""Build and exercise the production tray-status launcher without opening windows.

Run on Windows with PyInstaller installed: python packaging/check_frozen_status.py
Add --legacy-control to reproduce the pre-fix inherited environment (expected failure).
All reports/build products stay in a fresh ignored build/issue69-* directory.
The frozen entry intercepts `status` before Typer: no real settings or saves are read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SMOKE_DIR = "GSG_FROZEN_STATUS_SMOKE_DIR"


def write_report(path: Path, **fields: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(fields), encoding="utf-8")
    temporary.replace(path)


def wait_for(path: Path, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {path.name}; inspect {path.parent}")
        time.sleep(0.05)


def frozen_parent(directory: Path, *, legacy: bool) -> None:
    from game_save_genie import cli

    os.environ[SMOKE_DIR] = str(directory)
    original_popen = subprocess.Popen

    def hidden_terminal(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        # Retain the production cmd intermediary/arguments/environment. Only
        # hide its window and let the test terminal exit when status finishes.
        command = ["/c" if arg.lower() == "/k" else arg for arg in command]
        if legacy:
            kwargs["env"] = dict(kwargs.get("env", os.environ))
            kwargs["env"].pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with (directory / "terminal.log").open("ab") as output:
            kwargs["stdout"] = output
            kwargs["stderr"] = output
            return original_popen(command, **kwargs)

    subprocess.Popen = hidden_terminal  # type: ignore[misc,assignment]
    try:
        cli._open_status_window(None)
    finally:
        subprocess.Popen = original_popen  # type: ignore[misc]
    write_report(directory / "parent.json", pid=os.getpid(), meipass=sys._MEIPASS)  # type: ignore[attr-defined]
    wait_for(directory / "release-parent", timeout=90)


def frozen_status(directory: Path) -> None:
    extraction = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    write_report(directory / "child.json", pid=os.getpid(), meipass=str(extraction))
    wait_for(directory / "release-child", timeout=90)
    # Read a bundled file after the first onefile parent has fully exited and
    # cleaned up. Merely keeping an already-loaded Python process alive is weaker.
    library = extraction / "base_library.zip"
    available = library.is_file() and library.read_bytes()[:2] == b"PK"
    write_report(directory / "child-finished.json", survived=True, bundle_available=available)


def check_frozen_launch() -> None:
    if os.name != "nt":
        raise RuntimeError("This packaging regression requires Windows")
    root = Path(__file__).resolve().parents[1]
    (root / "build").mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="issue69-", dir=root / "build"))
    print(f"Frozen status smoke artifacts: {directory}", flush=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--clean",
        "--name", "gsg status smoke", "--distpath", str(directory / "dist"),
        "--workpath", str(directory / "work"), "--specpath", str(directory / "spec"),
        "--paths", str(root / "src"), str(Path(__file__).resolve()),
    ]
    with (directory / "build.log").open("wb") as output:
        subprocess.run(command, check=True, cwd=root, stdout=output, stderr=output,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    executable = directory / "dist" / "gsg status smoke.exe"
    with (directory / "parent.log").open("wb") as output:
        parent = subprocess.Popen(
            [str(executable), "legacy-parent" if "--legacy-control" in sys.argv else "smoke-parent",
             str(directory)], stdout=output, stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            wait_for(directory / "parent.json")
            wait_for(directory / "child.json")
            parent_info = json.loads((directory / "parent.json").read_text(encoding="utf-8"))
            child_info = json.loads((directory / "child.json").read_text(encoding="utf-8"))
            parent_alive = parent.poll() is None
            independent = parent_info["meipass"] != child_info["meipass"]
            (directory / "release-parent").touch()
            parent.wait(timeout=45)
            (directory / "release-child").touch()
            wait_for(directory / "child-finished.json")
            finished = json.loads((directory / "child-finished.json").read_text(encoding="utf-8"))
            report = {
                "parent_alive_after_status_launch": parent_alive,
                "independent_extraction": independent,
                "parent_exit_code": parent.returncode,
                **finished,
            }
            print(json.dumps(report, indent=2), flush=True)
            write_report(directory / "result.json", **report)
            if not (parent_alive and independent and parent.returncode == 0
                    and finished["survived"] and finished["bundle_available"]):
                raise RuntimeError("Frozen status launch is not independent of its parent")
        except Exception:
            for name in ("parent.log", "terminal.log"):
                log = directory / name
                if log.exists():
                    print(f"{name}:\n{log.read_text(encoding='utf-8', errors='replace')}", flush=True)
            raise
        finally:
            # Release only this harness's processes, including on assertion failure.
            (directory / "release-parent").touch()
            (directory / "release-child").touch()
            try:
                parent.wait(timeout=15)
            except subprocess.TimeoutExpired:
                parent.kill()
                parent.wait(timeout=15)


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        if sys.argv[-1] == "status":
            frozen_status(Path(os.environ[SMOKE_DIR]))
        else:
            frozen_parent(Path(sys.argv[2]), legacy=sys.argv[1] == "legacy-parent")
    else:
        check_frozen_launch()
