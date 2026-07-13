"""Process watcher to auto-trigger backups when games close."""

from __future__ import annotations

import logging
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

    def __init__(self, games: list[Game]) -> None:
        self.games = {g.id: g for g in games}
        self._seen_pids: set[int] = set()
        self._running: dict[str, int] = {}
        self.on_game_close: Callable[[Game, ProcessInfo], None] | None = None

    def set_on_game_close(self, callback: Callable[[Game, ProcessInfo], None]) -> None:
        self.on_game_close = callback

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
        for game in self.games.values():
            for proc in self._iter_processes():
                if self._matches_game(proc, game):
                    currently_running[game.id] = proc.pid
                    self._seen_pids.add(proc.pid)
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

        for executable in game.executable_names:
            if executable.lower() in name.lower():
                return True
            if exe and executable.lower() in exe.lower():
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
