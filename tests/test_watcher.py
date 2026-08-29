from __future__ import annotations

from unittest.mock import patch

import pytest

from game_save_genie.models import Game, Platform, ProcessInfo
from game_save_genie.watcher import (
    GameWatcher,
    is_helper_executable,
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


def test_title_keywords_drops_publisher_and_edition_words() -> None:
    """Publisher words identify a folder, not a game. Leaving them in reduced
    'EA Sports FC 26' to the single word 'sports'."""
    assert title_keywords("EA Sports FC 26") == ["fc", "26"]
    assert title_keywords("The Witcher 3: Wild Hunt") == ["witcher", "3", "wild", "hunt"]
    assert title_keywords("Skyrim Special Edition Collection") == ["skyrim", "special"]


def test_title_keywords_keeps_short_words() -> None:
    """The old >=4 rule reduced these to nothing, so they could never match
    any process while still being counted as watched."""
    assert title_keywords("GTA V") == ["gta", "v"]
    assert title_keywords("F1 24") == ["f1", "24"]
    assert title_keywords("NHL 25") == ["nhl", "25"]


def test_title_keywords_never_returns_empty() -> None:
    """A title made only of ignored words must not become unmatchable."""
    assert title_keywords("The Game") == ["the", "game"]
    assert title_keywords("EA Sports") == ["ea", "sports"]


def test_match_rejects_a_sibling_title_sharing_a_publisher() -> None:
    """The regression that started this: 'sports' was the only keyword left
    for FC 26, so any path containing it matched."""
    assert (
        title_matches_process(
            "WRC.exe", r"D:\Games\EA SPORTS WRC\WRC.exe", "EA Sports FC 26"
        )
        is False
    )
    # ...and the neighbouring year must not match either.
    assert (
        title_matches_process(
            "FC25.exe", r"D:\Games\EA SPORTS FC 25\FC25.exe", "EA Sports FC 26"
        )
        is False
    )
    # The real one still does.
    assert (
        title_matches_process(
            "FC26.exe", r"D:\Games\EA SPORTS FC 26\FC26.exe", "EA Sports FC 26"
        )
        is True
    )


def test_match_uses_whole_tokens_not_substrings() -> None:
    """A substring test over the joined path matched 'portal' inside
    'PortableApps'."""
    assert (
        title_matches_process("thing.exe", r"D:\Tools\PortableApps\thing.exe", "Portal")
        is False
    )


def test_a_two_letter_title_does_not_match_program_files() -> None:
    """Caught by the existing CLI round-trip test: a game called "RA" matched
    msedge.exe, because "ra" sits inside "Program Files"."""
    assert (
        title_matches_process(
            "msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "RA",
        )
        is False
    )
    # It still matches its own install, where it is a whole path token.
    assert title_matches_process("ra.exe", r"D:\Games\RA\ra.exe", "RA") is True


def test_match_survives_a_folder_missing_part_of_the_title() -> None:
    """Install folders routinely drop the subtitle."""
    assert (
        title_matches_process(
            "witcher3.exe",
            r"D:\Games\The Witcher 3\bin\x64\witcher3.exe",
            "The Witcher 3: Wild Hunt",
        )
        is True
    )


def test_short_titles_are_matchable_again() -> None:
    assert title_matches_process("gtav.exe", r"D:\Games\GTA V\gtav.exe", "GTA V") is True
    assert title_matches_process("F1_24.exe", r"D:\Games\F1 24\F1_24.exe", "F1 24") is True


def test_helper_processes_are_recognised() -> None:
    """These are what the watcher used to learn as a game's identity."""
    assert is_helper_executable("REDEngineErrorReporter.exe") is True
    assert is_helper_executable("EpicGamesLauncher.exe") is True
    assert is_helper_executable("EasyAntiCheat.exe") is True
    assert is_helper_executable("Cyberpunk2077.exe") is False
    assert is_helper_executable("witcher3.exe") is False


def test_unmatchable_reason_flags_only_hopeless_games() -> None:
    from game_save_genie.watcher import unmatchable_reason

    assert unmatchable_reason(Game(id="a", title="Cyberpunk 2077", platform=Platform.WINDOWS)) is None
    # An explicit executable is always matchable, whatever the title.
    assert (
        unmatchable_reason(
            Game(id="b", title="???", platform=Platform.WINDOWS, executable_names=["g.exe"])
        )
        is None
    )
    assert unmatchable_reason(Game(id="c", title="!!!", platform=Platform.WINDOWS)) is not None


def test_a_learned_name_does_not_disable_title_matching() -> None:
    """A learned name is a fast path, not a narrowing rule. When it was
    treated as narrowing, one bad guess made the game undetectable forever."""
    learned = Game(
        id="cp", title="Cyberpunk 2077", platform=Platform.WINDOWS,
        executable_names=["REDEngineErrorReporter.exe"], executables_learned=True,
    )
    watcher = GameWatcher([learned])
    assert watcher._matches(
        "Cyberpunk2077.exe", r"D:\Games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe", learned
    ) is True


def test_an_explicit_exe_still_narrows_matching() -> None:
    """--exe is the user choosing; it must keep suppressing title matching."""
    explicit = Game(
        id="cp", title="Cyberpunk 2077", platform=Platform.WINDOWS,
        executable_names=["Cyberpunk2077.exe"], executables_learned=False,
    )
    watcher = GameWatcher([explicit])
    assert watcher._matches(
        "REDlauncher.exe", r"D:\Games\Cyberpunk 2077\REDlauncher.exe", explicit
    ) is False


def test_session_names_accumulate_and_clear() -> None:
    game = _make_game()
    watcher = GameWatcher([game])
    watcher._session_names["test-game"] = {"launcher.exe", "game.exe"}
    assert watcher.session_process_names("test-game") == {"launcher.exe", "game.exe"}
    watcher.clear_session_names("test-game")
    assert watcher.session_process_names("test-game") == set()


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


def test_stop_ends_the_watch_loop() -> None:
    """The tray's Quit item calls stop() from another thread; the loop must
    return instead of running until the process is killed."""
    import threading
    import time

    game = _make_game()
    watcher = _ScriptedWatcher([game])
    watcher.script = [{} for _ in range(1000)]

    finished = threading.Event()

    def run() -> None:
        watcher.watch_loop(interval=0.05)
        finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.15)
    watcher.stop()

    assert finished.wait(timeout=5), "watch_loop ignored stop()"


def test_stop_before_the_loop_starts_is_honoured() -> None:
    """A Quit click racing startup must not leave the loop running forever."""
    watcher = _ScriptedWatcher([_make_game()])
    watcher.script = [{} for _ in range(10)]
    watcher.stop()
    watcher.watch_loop(interval=30.0)  # returns immediately, does not sleep


# --- POSIX system processes and Proton (#40) -------------------------------
# The watcher was inverted on Linux: OS daemons matched as games, and no
# Proton game matched at all. Every case below was executed against the real
# functions before the fix and behaved the wrong way round.


@pytest.mark.parametrize(
    "exe",
    [
        "/usr/libexec/xdg-desktop-portal",
        "/usr/bin/gnome-control-center",
        "/usr/lib/systemd/systemd",
        "/usr/sbin/cupsd",
        "/bin/bash",
        "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder",
        "C:/Windows/System32/svchost.exe",
    ],
)
def test_operating_system_processes_are_never_games(exe: str) -> None:
    assert is_system_executable(exe)


@pytest.mark.parametrize(
    "exe",
    [
        "/home/deck/.steam/steamapps/common/Portal 2/portal2.sh",
        "/opt/games/Control/control.x86_64",
        "/mnt/sdcard/roms/game.bin",
        "D:/Games/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe",
    ],
)
def test_real_game_locations_are_not_excluded(exe: str) -> None:
    """The exclusion must be prefix-anchored. A game under /home, /opt or a
    mounted drive shares no prefix with the OS directories."""
    assert not is_system_executable(exe)


def test_xdg_desktop_portal_does_not_match_portal() -> None:
    """The reported case. It also gets learned into games.yaml, so one match
    poisons that game's config permanently and reports it forever running."""
    assert not title_matches_process(
        "xdg-desktop-portal", "/usr/libexec/xdg-desktop-portal", "Portal"
    )
    assert not title_matches_process(
        "xdg-desktop-portal", "/usr/libexec/xdg-desktop-portal", "Portal 2"
    )


def test_gnome_control_center_does_not_match_control() -> None:
    assert not title_matches_process(
        "gnome-control-center", "/usr/bin/gnome-control-center", "Control"
    )


@pytest.mark.parametrize(
    ("name", "loader", "title"),
    [
        (
            "portal2.exe",
            "/home/deck/.steam/steamapps/common/Proton - Experimental/files/bin/wine64-preloader",
            "Portal 2",
        ),
        (
            "Cyberpunk2077.exe",
            "/home/deck/.local/share/Steam/steamapps/common/Proton 8.0/files/bin/wine",
            "Cyberpunk 2077",
        ),
    ],
)
def test_a_proton_game_is_matched_by_its_process_name(
    name: str, loader: str, title: str
) -> None:
    """Under Proton the executable path is the wine loader, so the title
    appears nowhere in it. The process name is the only evidence there is,
    and it used to be discarded."""
    assert title_matches_process(name, loader, title)


def test_the_process_name_does_not_become_a_loose_match() -> None:
    """Adding the name as evidence must not weaken the whole-token rules."""
    assert not title_matches_process("python3", "/home/deck/scripts/run.sh", "Portal 2")
    assert not title_matches_process("mono", "/opt/mono/bin/mono", "Control")
