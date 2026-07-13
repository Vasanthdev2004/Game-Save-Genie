from game_save_genie.watcher import (
    is_system_executable,
    title_keywords,
    title_matches_process,
)


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
