"""Process watcher to auto-trigger backups when games close."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .models import Game, ProcessInfo

logger = logging.getLogger(__name__)

# Words that identify a publisher, a storefront, or an edition rather than the
# game. They are dropped so a title matches an install folder that omits them —
# and, more importantly, so a game is never identified by them: "EA Sports FC
# 26" reduced to the single word "sports" matched "EA SPORTS WRC".
_IGNORED_TITLE_WORDS = {
    # articles and connectives
    "the", "a", "an", "of", "and", "or", "for", "to",
    # publishers and storefronts that show up as parent folders
    "ea", "sports", "ubisoft", "activision", "bethesda", "rockstar",
    "square", "enix", "bandai", "namco", "sega", "capcom", "konami",
    "studios", "studio", "interactive", "entertainment", "games", "game",
    # edition and packaging markers
    "edition", "remastered", "definitive", "collection", "goty", "deluxe",
    "complete", "directors", "cut", "launcher", "play",
}

# A single keyword this long, matched as a whole path token, is specific
# enough to identify a game on its own ("witcher", "cyberpunk").
_DISTINCTIVE_KEYWORD_LENGTH = 5

# Below this length, a run-together title is only accepted as a whole token,
# never as a substring: a game called "RA" would otherwise match every path
# containing "Program Files".
_MIN_SUBSTRING_LENGTH = 4


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric words, split on anything else."""
    return [word.lower() for word in re.split(r"[^a-zA-Z0-9]+", text) if word]


def _slug(text: str) -> str:
    """Lowercase alphanumerics only — 'EA SPORTS FC 26' -> 'easportsfc26'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def title_keywords(title: str) -> list[str]:
    """Significant lowercase words from a game title, for matching.

    Short words are kept: version numbers are frequently the only thing
    distinguishing one title from its neighbours ("FC 26" from "FC 25"), and
    the old rule of dropping anything under four characters reduced "GTA V"
    and "F1 24" to nothing at all, making them permanently undetectable.

    Filtering never returns an empty list — a title made entirely of ignored
    words falls back to its raw words rather than becoming unmatchable.
    """
    words = _tokens(title)
    keywords = [word for word in words if word not in _IGNORED_TITLE_WORDS]
    return keywords or words


# Substrings of process names that belong to something running *beside* the
# game — a crash handler, launcher, updater, or anti-cheat. These are poor
# identities: Cyberpunk was learned as REDEngineErrorReporter.exe, so the game
# was thereafter detected only when its crash reporter happened to be up.
_HELPER_PROCESS_MARKERS = (
    "crashreport", "crashhandler", "crashpad", "errorreport",
    "launcher", "updater", "installer", "setup", "redistributable",
    "anticheat", "easyanticheat", "battleye", "eacl",
    "overlay", "webhelper", "helper", "service", "bootstrapper",
)


def is_helper_executable(name: str) -> bool:
    """Whether a process name looks like a companion process, not the game."""
    slug = _slug(name)
    return any(marker in slug for marker in _HELPER_PROCESS_MARKERS)


# Directories that hold the operating system rather than anything anyone
# plays. Checked as path prefixes so a game installed under /home, /opt, a
# mounted drive or a Steam library is never caught by them.
#
# This list existed only for Windows, which meant that off Windows NOTHING was
# excluded: /usr/libexec/xdg-desktop-portal matched the title "Portal", the
# name was learned into games.yaml, and the game then read as permanently
# running - which silently disabled its cloud restore (#40).
_SYSTEM_PATH_PREFIXES = (
    "c:/windows/",
    "/usr/bin/",
    "/usr/sbin/",
    "/usr/lib/",
    "/usr/libexec/",
    "/usr/share/",
    "/bin/",
    "/sbin/",
    "/lib/",
    "/lib64/",
    "/etc/",
    "/proc/",
    "/sys/",
    "/system/",              # macOS /System
    "/library/",             # macOS /Library, not ~/Library
    "/applications/utilities/",
    "/private/var/",
)


def is_system_executable(exe: str | None) -> bool:
    """Return True for OS/system processes that should never match a game.

    A false negative here is expensive in a way that is easy to miss: a
    matched process name is *learned* and written to games.yaml, so one OS
    daemon matching a game title poisons that game's configuration
    permanently and reports it as forever running.
    """
    if not exe:
        return False
    normalized = exe.lower().replace("\\", "/")
    # Windows is matched anywhere in the path because a drive letter varies
    # and Ludusavi paths embed it as "drive-C/Windows/...".
    if "/windows/" in normalized:
        return True
    return normalized.startswith(_SYSTEM_PATH_PREFIXES)


def title_matches_process(name: str, exe: str | None, title: str) -> bool:
    """Fuzzy-match a game title against a process, avoiding false positives.

    Evidence is weighed per path *segment*, and keywords must match whole
    tokens rather than appear anywhere in the path. A substring test over the
    joined path is far too loose: "portal" matches ".../portable/...", and one
    generic word matched anywhere was enough to claim a process.

    A false positive is the expensive failure. It is not just a wrong backup:
    the matched name is learned as the game's executable, so the game is
    thereafter identified by a process belonging to something else. A false
    negative only means the game is never detected, which `gsg auto` and
    `gsg status` now report so it can be fixed with `--exe`.

    System executables never match. The reported process *name* is weighed as
    one more segment, under the same whole-token rules as the path: under
    Proton the executable path is the wine loader inside the Proton runtime,
    so the game's title appears nowhere in it and the name is the only
    evidence there is (#40). The name alone was previously discarded, which
    made every Proton title undetectable while native Linux games worked -
    a failure that looks random rather than systematic.
    """
    if is_system_executable(exe) or not exe:
        return False

    keywords = title_keywords(title)
    if not keywords:
        return False

    joined = "".join(keywords)
    # The name is appended, not substituted: a path segment is still the
    # stronger signal, and the per-segment rules below are what keep a short
    # or generic name from claiming a game.
    segments = exe.replace("\\", "/").split("/")
    if name:
        segments.append(name)
    for segment in segments:
        if not segment:
            continue
        segment_tokens = set(_tokens(segment))
        # 1. The whole title, run together, inside one segment. Covers both
        #    "EA SPORTS FC 26\" and "Cyberpunk2077.exe" — but only once it is
        #    long enough that an accidental substring is implausible.
        if len(joined) >= _MIN_SUBSTRING_LENGTH and joined in _slug(segment):
            return True
        # 1b. Short titles must land on a whole token instead.
        if joined and joined in segment_tokens:
            return True

        # 2. One distinctive keyword as a whole token: ".../The Witcher 3/".
        if any(
            word in segment_tokens and len(word) >= _DISTINCTIVE_KEYWORD_LENGTH
            for word in keywords
        ):
            return True
        # 3. Two different keywords in the SAME segment. Short words like
        #    "fc" and "26" only count together, and only when they appear in
        #    one place — which is what separates "FC 26" from "WRC".
        if len(segment_tokens.intersection(keywords)) >= 2:
            return True

    return False


def unmatchable_reason(game: Game) -> str | None:
    """Why this game can never be detected, or None if it can.

    A game with no usable keywords and no explicit executable produces no
    watcher events at all — it is counted in "Watching N game(s)" and then
    silently never backed up.
    """
    if game.executable_names:
        return None
    if not title_keywords(game.title):
        return f"'{game.title}' has no matchable words"
    return None


class GameWatcher:
    """Watch for running games and trigger callbacks on start/close.

    Each game maps to the SET of processes matching it — many games run a
    launcher, anti-cheat, or crash handler beside the main executable, so a
    game counts as closed only when no matching process remains. Callback
    exceptions are logged and swallowed: one failing backup must never kill
    a watcher that runs unattended from boot -- but they are also reported
    through ``on_error``, because a watcher that survives silently is
    indistinguishable from one that is working.
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
        self._stop = threading.Event()
        # Every process name seen for a game during the current session, so a
        # name can be learned from the whole session rather than from whatever
        # happened to be running first.
        self._session_names: dict[str, set[str]] = {}
        self.on_game_close: Callable[[Game, ProcessInfo], None] | None = None
        self.on_game_start: Callable[[Game, ProcessInfo], None] | None = None
        self.on_periodic_backup: Callable[[Game], None] | None = None
        self.on_idle_check: Callable[[Game], None] | None = None
        # Called when a callback raises. Without it a failed close-backup was
        # a log line nobody reads while the tray still showed "Playing" (#41).
        self.on_error: Callable[[str], None] | None = None
        self._periodic_task: Callable[[], None] | None = None
        self._periodic_task_interval = 0.0
        self._periodic_task_last = 0.0

    def set_on_error(self, callback: Callable[[str], None]) -> None:
        self.on_error = callback

    def set_periodic_task(
        self, interval_seconds: float, task: Callable[[], None]
    ) -> None:
        """Run ``task`` from the loop, at most every ``interval_seconds``.

        For work that belongs to the watcher's lifetime but is far too
        expensive for the poll interval - rescanning for newly installed
        games costs a full Ludusavi scan, against a 5s tick (#54).

        The first run happens one interval from now, not immediately: the
        caller has just done this work at startup.
        """
        self._periodic_task = task
        self._periodic_task_interval = interval_seconds
        self._periodic_task_last = time.time()

    def add_games(self, games: list[Game]) -> list[str]:
        """Start watching any of ``games`` not already tracked.

        Returns the ids actually added. Existing entries are left alone rather
        than replaced, so a game currently detected as running keeps its pid
        set and does not spuriously fire a close event.
        """
        added: list[str] = []
        for game in games:
            if game.id in self.games:
                continue
            self.games[game.id] = game
            added.append(game.id)
        return added

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

    def running_process_info(self, game_id: str) -> ProcessInfo | None:
        """Info for one process currently matched to this game, if any."""
        return self._first_process_info(self._running.get(game_id, set()))

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
                    if name:
                        self._session_names.setdefault(game.id, set()).add(name)
        return found

    def session_process_names(self, game_id: str) -> set[str]:
        """Process names seen for this game since it was last closed."""
        return set(self._session_names.get(game_id, set()))

    def clear_session_names(self, game_id: str) -> None:
        self._session_names.pop(game_id, None)

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

    def stop(self) -> None:
        """Ask :meth:`watch_loop` to return after the current tick.

        Safe to call from another thread — the tray's Quit item does exactly
        that. Waiting on an Event rather than sleeping means shutdown is
        immediate instead of taking up to ``interval`` seconds.
        """
        self._stop.set()

    def watch_loop(self, interval: float = 5.0) -> None:
        """Run the watcher loop until :meth:`stop` is called."""
        logger.info("Starting game watcher with %d tracked games", len(self.games))
        while not self._stop.is_set():
            self.tick()
            self._run_periodic_task()
            if self._stop.wait(interval):
                break

    def _run_periodic_task(self) -> None:
        """Run the registered periodic task if its interval has elapsed."""
        if self._periodic_task is None or self._periodic_task_interval <= 0:
            return
        if time.time() - self._periodic_task_last < self._periodic_task_interval:
            return
        # Stamp before running, so a task that takes a while - or throws -
        # cannot queue itself up to run again immediately.
        self._periodic_task_last = time.time()
        self._safe_callback(self._periodic_task)

    def _safe_callback(self, callback: Callable[..., None], *args: Any) -> None:
        """Run a callback without letting its failure kill the watch loop.

        The loop must survive, but the user must hear about it. An exception
        here means the close-backup never ran, and the tray was still showing
        the green "Playing" state set when the game started - weeks of
        failures looking exactly like everything working (#41). ``on_error``
        is what surfaces it; a watcher constructed without one keeps the old
        log-only behaviour.
        """
        try:
            callback(*args)
        except Exception as exc:
            logger.exception("Watcher callback failed")
            if self.on_error is not None:
                try:
                    self.on_error(f"Backup step failed: {exc}")
                except Exception:
                    # The reporter itself failing must not kill the loop.
                    logger.exception("Watcher error reporter failed")

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

        # Fall back to the title. An explicit --exe is the user narrowing
        # matching on purpose, so it suppresses this — but a LEARNED name must
        # not, or a single bad guess (a crash handler caught on the first tick)
        # makes the game undetectable for good with no way to notice.
        if not game.executable_names or game.executables_learned:
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
