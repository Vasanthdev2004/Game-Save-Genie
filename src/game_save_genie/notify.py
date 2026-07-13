"""Persistent logging and best-effort desktop notifications."""

from __future__ import annotations

import logging
import os
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = 0x08000000


def setup_file_logging(log_dir: Path) -> Path:
    """Attach a rotating file handler to the root logger (idempotent)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "game-save-genie.log"

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(
            getattr(handler, "baseFilename", "")
        ) == log_file.resolve():
            return log_file

    handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return log_file


def notify(title: str, message: str) -> None:
    """Log an event and show a best-effort desktop notification."""
    logger.info("%s: %s", title, message)
    if os.name == "nt":
        _windows_toast(title, message)


def _windows_toast(title: str, message: str) -> None:
    """Show a Windows balloon notification without blocking or raising."""
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        f"$n.ShowBalloonTip(5000,'{safe_title}','{safe_message}',"
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Milliseconds 6000;$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, ValueError) as exc:
        logger.debug("Toast notification failed: %s", exc)
