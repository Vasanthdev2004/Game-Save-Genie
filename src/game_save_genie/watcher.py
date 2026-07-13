"""Process watcher to auto-trigger backups when games close."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .models import Game, ProcessInfo

logger = logging.getLogger(__name__)


class GameWatcher:
    """Watch for running games and trigger callbacks on start/close."""

    def __init__(self, games: list[Game], periodic_interval: float = 0) -> None:
        self.games = {g.id: g for g in games}
        self._seen_pids: set[int] = set()
        self._running: dict[str, int] = {}
        self._last_backup: dict[str, float] = {}
        self._periodic_interval = periodic_interval
        self.on_game_close: Callable[[Game, ProcessInfo], None] | None = None
        self.on_game_start: Callable[[Game, ProcessInfo], None] | None = None
        self.on_periodic_backup: Callable[[Game], None] | None = None

    def set_on_game_close(self, callback: Callable[[Game, ProcessInfo], None]) -> None:
        self.on_game_close = callback

    def set_on_game_start(self, callback: Callable[[Game, ProcessInfo], None]) -> None:
        self.on_game_start = callback

    def set_on_periodic_backup(self, callback: Callable[[Game], None]) -> None:
        self.on_periodic_backup = callback

    def scan(self) -> list[ProcessInfo]:
        """Scan for currently running game processes."""
        found: list[ProcessInfo] = []
        for game in self.games.values():
            for proc in self._iter_processes():
                if self._matches_game(proc, game):
                    found.append(proc)
                    break
        return found

    def tick(self) -> None:
        """Process one tick of the watcher loop."""
        currently_running: dict[str, int] = {}
        now = time.time()
        for game in self.games.values():
            for proc in self._iter_processes():
                if self._matches_game(proc, game):
                    currently_running[game.id] = proc.pid
                    self._seen_pids.add(proc.pid)
                    # Detect newly started games
                    if game.id not in self._running and self.on_game_start is not None:
                        proc_info = self._process_info_from_pid(proc.pid)
                        if proc_info is not None:
                            logger.info("Game started: %s (pid %d)", game.title, proc.pid)
                            self.on_game_start(game, proc_info)
                        self._last_backup[game.id] = now

                    # Periodic backup during gameplay
                    if (
                        self._periodic_interval > 0
                        and self.on_periodic_backup is not None
                        and game.id in self._last_backup
                        and (now - self._last_backup[game.id]) >= self._periodic_interval
                    ):
                        logger.info("Periodic backup: %s", game.title)
                        self.on_periodic_backup(game)
                        self._last_backup[game.id] = now

                    break

        # Detect closed games
        for game_id, pid in self._running.items():
            if game_id not in currently_running or currently_running[game_id] != pid:
                closed_game = self.games.get(game_id)
                if closed_game is not None and self.on_game_close is not None:
                    proc_info = self._process_info_from_pid(pid)
                    if proc_info is not None:
                        logger.info("Game closed: %s (pid %d)", closed_game.title, pid)
                        self.on_game_close(closed_game, proc_info)

        self._running = currently_running

    def watch_loop(self, interval: float = 5.0) -> None:
        """Run the watcher loop indefinitely."""
        logger.info("Starting game watcher with %d tracked games", len(self.games))
        while True:
            self.tick()
            time.sleep(interval)

    def _matches_game(self, proc: psutil.Process, game: Game) -> bool:
        try:
            name = proc.name()
            exe = proc.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

        # Match by executable name (explicit)
        for executable in game.executable_names:
            if executable.lower() in name.lower():
                return True
            if exe and executable.lower() in exe.lower():
                return True

        # Fallback: match by game title keywords in process name/exe path
        if not game.executable_names:
            return self._matches_by_title(name, exe, game.title)

        return False

    def _matches_by_title(self, name: str, exe: str | None, title: str) -> bool:
        """Fuzzy match a game title against process name and exe path."""
        # Extract significant words from the title (skip short/common words)
        skip_words = {"the", "a", "an", "of", "and", "or", "arise", "overdrive", "collection"}
        words = [
            w for w in re.split(r"[^a-zA-Z0-9]+", title)
            if len(w) >= 4 and w.lower() not in skip_words
        ]
        if not words:
            return False

        name_lower = name.lower()
        exe_lower = (exe or "").lower()

        # Check if the process name or exe path contains a title keyword
        for word in words:
            word_lower = word.lower()
            # Match with or without spaces/underscores
            if word_lower in name_lower or word_lower in exe_lower:
                return True
            # Also try without spaces (e.g., "SoloLeveling" matches "Solo Leveling")
            compact = word_lower.replace(" ", "")
            if compact in name_lower or compact in exe_lower:
                return True

        return False

    def _iter_processes(self) -> Any:
        for proc in psutil.process_iter(["pid", "name", "exe", "status", "create_time"]):
            try:
                yield proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _process_info_from_pid(self, pid: int) -> ProcessInfo | None:
        try:
            proc = psutil.Process(pid)
            return ProcessInfo(
                pid=pid,
                name=proc.name(),
                exe=proc.exe(),
                status=proc.status(),
                create_time=datetime.fromtimestamp(proc.create_time(), tz=timezone.utc),
                environ=proc.environ(),
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _detect_wine_prefix(self, proc: psutil.Process) -> Path | None:
        """Try to detect the Steam/Wine prefix from process environment."""
        try:
            env = proc.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        steam_path = env.get("STEAM_COMPAT_DATA_PATH")
        if steam_path:
            return Path(steam_path) / "pfx"
        return None
