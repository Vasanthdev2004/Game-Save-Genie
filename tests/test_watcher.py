from __future__ import annotations

from unittest.mock import patch

from game_save_genie.models import Game, Platform, ProcessInfo
from game_save_genie.watcher import (
    GameWatcher,
    is_system_executable,
    title_keywords,
    title_matches_process,
)


def _make_game(game_id: str = "test-game") -> Game:
    return Game(id=game_id, title="Test Game", platform=Platform.WINDOWS)


def _stub_proc_info(pid: int) -> ProcessInfo:
    return ProcessInfo(
        pid=pid, name="game.exe", exe="D:/Games/game.exe",
        status="running", create_time=None, environ={},
    )


class _ScriptedWatcher(GameWatcher):
    """GameWatcher whose process scan is driven by a script of tick results."""

    def __init__(
        self,
        games: list[Game],
        periodic_interval: float = 0,
        idle_interval: float = 0,
    ) -> None:
        super().__init__(
            games, periodic_interval=periodic_interval, idle_interval=idle_interval
        )
        self.script: list[dict[str, set[int]]] = []

    def _scan_running(self) -> dict[str, set[int]]:
        return self.script.pop(0) if self.script else {}


def test_title_keywords_skips_short_and_common() -> None:
    assert title_keywords("Solo Leveling: ARISE") == ["solo", "leveling"]
    assert title_keywords("The Witcher 3") == ["witcher"]


def test_system_executable_detected() -> None:
    assert is_system_executable(r"C:\Windows\System32\svchost.exe") is True
    assert is_system_executable(None) is False
    assert (
        is_system_executable(r"D:\Games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe")
        is False
    )


def test_match_requires_exe_path_keyword() -> None:
    assert (
        title_matches_process(
            "Cyberpunk2077.exe",
            r"D:\Games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe",
            "Cyberpunk 2077",
        )
        is True
    )


def test_no_match_on_bare_process_name_without_path() -> None:
    assert title_matches_process("witcher.exe", None, "The Witcher 3") is False


def test_system_process_never_matches() -> None:
    assert (
        title_matches_process(
            "svchost.exe", r"C:\Windows\System32\svchost.exe", "Svchost Adventure"
        )
        is False
    )


def test_tick_fires_start_and_close() -> None:
    game = _make_game()
    watcher = _ScriptedWatcher([game])
    events: list[str] = []
    watcher.set_on_game_start(lambda g, p: events.append(f"start:{g.id}"))
    watcher.set_on_game_close(lambda g, p: events.append(f"close:{g.id}"))

    watcher.script = [{game.id: {100}}, {game.id: {100}}, {}]
    with patch.object(GameWatcher, "_process_info_from_pid", return_value=_stub_proc_info(100)):
        watcher.tick()  # game appears -> start
        watcher.tick()  # still running -> nothing
        watcher.tick()  # gone -> close

    assert events == ["start:test-game", "close:test-game"]


def test_tick_close_fires_with_stub_when_process_gone() -> None:
    game = _make_game()
    watcher = _ScriptedWatcher([game])
    closed: list[ProcessInfo] = []
    watcher.set_on_game_close(lambda g, p: closed.append(p))

    watcher.script = [{game.id: {100}}, {}]
    with patch.object(GameWatcher, "_process_info_from_pid", return_value=None):
        watcher.tick()
        watcher.tick()

    assert len(closed) == 1
    assert closed[0].status == "terminated"


def test_launcher_exit_is_not_a_close() -> None:
    """A helper process (launcher/anti-cheat) exiting while the game keeps
    running must not fire a close; close fires only when NO matching
    process remains."""
    game = _make_game()
    watcher = _ScriptedWatcher([game])
    events: list[str] = []
    watcher.set_on_game_start(lambda g, p: events.append("start"))
    watcher.set_on_game_close(lambda g, p: events.append("close"))

    watcher.script = [{game.id: {100, 200}}, {game.id: {200}}, {}]
    with patch.object(GameWatcher, "_process_info_from_pid", return_value=_stub_proc_info(100)):
        watcher.tick()  # launcher (100) + game (200) -> start
        watcher.tick()  # launcher exited, game still running -> nothing
        watcher.tick()  # all gone -> close

    assert events == ["start", "close"]


def test_prime_suppresses_start_but_not_close() -> None:
    """A game already running at watcher startup must not fire on_start,
    but must still get its close backup."""
    game = _make_game()
    watcher = _ScriptedWatcher([game])
    events: list[str] = []
    watcher.set_on_game_start(lambda g, p: events.append("start"))
    watcher.set_on_game_close(lambda g, p: events.append("close"))

    watcher.script = [{game.id: {100}}, {game.id: {100}}, {}]
    with patch.object(GameWatcher, "_process_info_from_pid", return_value=_stub_proc_info(100)):
        watcher.prime()  # consumes first scan: game already running
        watcher.tick()   # still running -> no start
        watcher.tick()   # gone -> close

    assert events == ["close"]


def test_periodic_fires_without_start_callback() -> None:
    """The periodic timer must be seeded even when no start callback is set."""
    game = _make_game()
    watcher = _ScriptedWatcher([game], periodic_interval=0.0001)
    fired: list[str] = []
    watcher.set_on_periodic_backup(lambda g: fired.append(g.id))

    watcher.script = [{game.id: {100}}, {game.id: {100}}]
    watcher.tick()  # seeds the timer
    import time

    time.sleep(0.001)
    watcher.tick()  # interval elapsed -> periodic backup

    assert fired == [game.id]


def test_idle_check_fires_only_when_not_running() -> None:
    game = _make_game()
    watcher = _ScriptedWatcher([game], idle_interval=0.0001)
    idle: list[str] = []
    watcher.set_on_idle_check(lambda g: idle.append(g.id))

    import time

    watcher.script = [{}, {}, {game.id: {100}}]
    watcher.tick()          # seeds the idle timer
    time.sleep(0.001)
    watcher.tick()          # elapsed while idle -> fires
    watcher.tick()          # game running -> never fires
    assert idle == [game.id]


def test_callback_exception_does_not_kill_tick() -> None:
    """A raising callback must be swallowed so the watcher daemon survives."""
    game = _make_game()
    watcher = _ScriptedWatcher([game])
    events: list[str] = []

    def bad_start(g: Game, p: ProcessInfo) -> None:
        raise OSError("disk full")

    watcher.set_on_game_start(bad_start)
    watcher.set_on_game_close(lambda g, p: events.append("close"))

    watcher.script = [{game.id: {100}}, {}]
    with patch.object(GameWatcher, "_process_info_from_pid", return_value=_stub_proc_info(100)):
        watcher.tick()  # on_start raises -> swallowed
        watcher.tick()  # close still fires

    assert events == ["close"]


def test_running_pids_reflects_current_tick() -> None:
    game = _make_game()
    watcher = _ScriptedWatcher([game])
    seen: list[int] = []
    watcher.set_on_game_start(lambda g, p: seen.append(len(watcher.running_pids(g.id))))

    watcher.script = [{game.id: {100, 200}}]
    with patch.object(GameWatcher, "_process_info_from_pid", return_value=_stub_proc_info(100)):
        watcher.tick()

    assert seen == [2]
    assert watcher.is_running(game.id) is True
