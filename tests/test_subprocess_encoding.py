"""Every subprocess we decode must be told the encoding.

`text=True` alone decodes with the locale default, which on a stock Windows
install is cp1252. Ludusavi and rclone both emit UTF-8, so a game title with a
character outside cp1252 came back mangled.

Reported by @Tudzer in #60, who found it in the Ludusavi call. It was in five
other places, one of which matters more than the original: rclone's output is
parsed into the remote version listing that prune and restore work from, and
`_slugify` keeps non-ASCII, so a game id - and therefore its remote path - can
contain characters cp1252 cannot represent.

This is a whole-tree assertion rather than six separate ones, because the bug
is "somebody added a subprocess call and forgot", and that is what needs
catching.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path("src/game_save_genie")


def _decoded_runs_missing_encoding() -> list[str]:
    offenders: list[str] = []
    for file in sorted(SRC.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", "") != "run":
                continue
            keywords = {kw.arg for kw in node.keywords}
            # text=True (or universal_newlines) means Python decodes for us,
            # so it needs to be told how. Byte-mode calls are unaffected.
            decodes = "text" in keywords or "universal_newlines" in keywords
            if decodes and "encoding" not in keywords:
                offenders.append(f"{file.as_posix()}:{node.lineno}")
    return offenders


def test_no_subprocess_decodes_with_the_locale_default() -> None:
    offenders = _decoded_runs_missing_encoding()
    assert offenders == [], (
        "subprocess.run(text=True) without encoding= decodes with the locale "
        "default (cp1252 on Windows) and mangles non-ASCII output: "
        + ", ".join(offenders)
    )


def test_the_check_can_actually_see_a_violation() -> None:
    """A guard that cannot fail is not a guard. Prove the AST walk detects the
    shape it is meant to catch."""
    tree = ast.parse("import subprocess\nsubprocess.run(['x'], text=True)\n")
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "run"
        and "text" in {kw.arg for kw in node.keywords}
        and "encoding" not in {kw.arg for kw in node.keywords}
    ]
    assert len(found) == 1
