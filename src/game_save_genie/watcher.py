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

_TITLE_SKIP_WORDS = {
    "the", "a", "an", "of", "and", "or", "for", "to",
    "arise", "overdrive", "collection", "edition", "remastered",
    "game", "play", "launcher",
}


def title_keywords(title: str) -> list[str]:
    """Return significant lowercase words from a game title for matching."""
    return [
        word.lower()
        for word in re.split(r"[^a-zA-Z0-9]+", title)
        if len(word) >= 4 and word.lower() not in _TITLE_SKIP_WORDS
    ]


def is_system_executable(exe: str | None) -> bool:
    """Return True for OS/system processes that should never match a game."""
    if not exe:
        return False
    normalized = exe.lower().replace("\\", "/")
    return "/windows/" in normalized


def title_matches_process(name: str, exe: str | None, title: str) -> bool:
    """Fuzzy-match a game title against a process, avoiding false positives.

    A match requires a title keyword (or the whole compacted title) to appear
    in the executable's full path. Bare process-name matches are intentionally
    rejected because generic names cause false positives. System executables
    never match.
    """
    if is_system_executable(exe):
        return False

    keywords = title_keywords(title)
    if not keywords or not exe:
        return False

    exe_compact = exe.lower().replace("\\", "/").replace(" ", "")
    compact_title = "".join(keywords)
    if compact_title and compact_title in exe_compact:
        return True

    return any(word in exe_compact for word in keywords)


class GameWatcher:
    """Watch for running games and trigger callbacks on start/close.

    Each game maps to the SET of processes matching it — many games run a
    launcher, anti-cheat, or crash handler beside the main executable, so a
    game counts as closed only when no matching process remains. Callback
    exceptions are logged and swallowed: one failing backup must never kill
    a watcher that runs unattended from boot.
    """

    def __init__(
        self,
        games: list[Game],
        periodic_interval: float = 0,
        idle_interval: float = 0,
    ) -> None:
        self.games = {g.id: g for g in games}
        self._running: dict[str, set[int]] = {}
        self._last_backup: dict[str, float] = {}
        self._last_idle_check: dict[str, float] = {}
        self._periodic_interval = periodic_interval
        self._idle_interval = idle_interval
        self.on_game_close: Callable[[Game, ProcessInfo], None] | None = None
        self.on_game_start: Callable[[Game, ProcessInfo], None] | None = None
        self.on_periodic_backup: Callable[[Game], None] | None = None
        self.on_idle_check: Callable[[Game], None] | None = None

    def set_on_game_close(self, callback: Callable[[Game, ProcessInfo], None]) -> None:
        self.on_game_close = callback

    def set_on_game_start(self, callback: Callable[[Game, ProcessInfo], None]) -> None:
        self.on_game_start = callback

    def set_on_periodic_backup(self, callback: Callable[[Game], None]) -> None:
        self.on_periodic_backup = callback

    def set_on_idle_check(self, callback: Callable[[Game], None]) -> None:
        self.on_idle_check = callback

    def is_running(self, game_id: str) -> bool:
        """Whether any process currently matches this game."""
        return bool(self._running.get(game_id))

    def running_pids(self, game_id: str) -> set[int]:
        """The set of pids currently matched to this game."""
        return set(self._running.get(game_id, set()))

    def scan(self) -> list[ProcessInfo]:
        """Scan for currently running game processes."""
        found: list[ProcessInfo] = []
        for pids in self._scan_running().values():
            proc_info = self._first_process_info(pids)
            if proc_info is not None:
                found.append(proc_info)
        return found

    def prime(self) -> None:
        """Seed running-state for games already running, without firing callbacks.

        Called once before the watch loop so that a game running when the
        watcher starts (e.g. at boot) is not treated as freshly launched.
        Close/periodic callbacks still fire normally afterwards.
        """
        self._running = self._scan_running()
        now = time.time()
        for game_id in self._running:
            self._last_backup[game_id] = now

    def _scan_running(self) -> dict[str, set[int]]:
        """One pass over the process table, mapping game ids to matching pids."""
        found: dict[str, set[int]] = {}
        for proc in self._iter_processes():
            try:
                name = proc.name()
                exe = proc.exe()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            for game in self.games.values():
                if self._matches(name, exe, game):
                    found.setdefault(game.id, set()).add(proc.pid)
        return found

    def tick(self) -> None:
        """Process one tick of the watcher loop."""
        now = time.time()
        current = self._scan_running()
        previous = self._running
        self._running = current

        for game_id, pids in current.items():
            game = self.games[game_id]
            # Detect newly started games
            if not previous.get(game_id):
                self._last_backup[game_id] = now
                if self.on_game_start is not None:
                    proc_info = self._first_process_info(pids)
                    if proc_info is not None:
                        logger.info("Game started: %s (pids %s)", game.title, sorted(pids))
                        self._safe_callback(self.on_game_start, game, proc_info)

            # Periodic backup during gameplay
            if (
                self._periodic_interval > 0
                and self.on_periodic_backup is not None
                and game_id in self._last_backup
                and (now - self._last_backup[game_id]) >= self._periodic_interval
            ):
                logger.info("Periodic backup: %s", game.title)
                self._safe_callback(self.on_periodic_backup, game)
                self._last_backup[game_id] = now

        # Detect closed games: closed only when NO matching process remains
        # (a launcher exiting while the game runs is not a close).
        for game_id, pids in previous.items():
            if pids and not current.get(game_id):
                closed_game = self.games.get(game_id)
                if closed_game is not None and self.on_game_close is not None:
                    proc_info = self._first_process_info(pids)
                    if proc_info is None:
                        # Processes already exited; create a stub so the
                        # close callback (which does the backup) still fires.
                        proc_info = ProcessInfo(
                            pid=next(iter(pids)),
                            name="",
                            exe="",
                            status="terminated",
                            create_time=datetime.now(tz=timezone.utc),
                            environ={},
                        )
                    logger.info("Game closed: %s", closed_game.title)
                    self._safe_callback(self.on_game_close, closed_game, proc_info)

        # Idle checks for games that are NOT running (e.g. safe moments to
        # pull newer cloud saves without racing a live process).
        if self._idle_interval > 0 and self.on_idle_check is not None:
            for game_id, game in self.games.items():
                if current.get(game_id):
                    self._last_idle_check.pop(game_id, None)
                    continue
                last = self._last_idle_check.setdefault(game_id, now)
                if (now - last) >= self._idle_interval:
                    self._safe_callback(self.on_idle_check, game)
                    self._last_idle_check[game_id] = now

    def watch_loop(self, interval: float = 5.0) -> None:
        """Run the watcher loop indefinitely."""
        logger.info("Starting game watcher with %d tracked games", len(self.games))
        while True:
            self.tick()
            time.sleep(interval)

    def _safe_callback(self, callback: Callable[..., None], *args: Any) -> None:
        """Run a callback without letting its failure kill the watch loop."""
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - the watcher must survive anything
            logger.exception("Watcher callback failed")

    def _first_process_info(self, pids: set[int]) -> ProcessInfo | None:
        for pid in sorted(pids):
            proc_info = self._process_info_from_pid(pid)
            if proc_info is not None:
                return proc_info
        return None

    def _matches(self, name: str, exe: str | None, game: Game) -> bool:
        # Match by executable name (explicit)
        for executable in game.executable_names:
            if executable.lower() in name.lower():
                return True
            if exe and executable.lower() in exe.lower():
                return True

        # Fallback: match by game title keywords in process name/exe path
        if not game.executable_names:
            return title_matches_process(name, exe, game.title)

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
            name = proc.name()
            exe = proc.exe()
            status = proc.status()
            create_time = datetime.fromtimestamp(proc.create_time(), tz=timezone.utc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

        # environ() commonly raises AccessDenied on Windows; don't let it
        # prevent us from returning the info we already have.
        try:
            environ = proc.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            environ = {}

        return ProcessInfo(
            pid=pid,
            name=name,
            exe=exe,
            status=status,
            create_time=create_time,
            environ=environ,
        )

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
