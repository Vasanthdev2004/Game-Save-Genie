"""Fail if src/ imports a third-party package that pyproject does not declare.

CI installs `.[dev]`, so a dev tool can quietly supply a runtime dependency and
hide the gap for as long as nobody installs the project the way a user does.
That is exactly what happened with click: typer 0.26.8 dropped it, black still
pulled it in, every CI job stayed green, and the first person to run a
runtime-only install hit ModuleNotFoundError in the first-run wizard.

This checks the declaration rather than the installation, so it catches the gap
even when the import is unreachable at module scope.

    python packaging/check_deps.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# Import name differs from the distribution name on PyPI. Extend as needed.
IMPORT_TO_DISTRIBUTION = {
    "yaml": "pyyaml",
    "PIL": "pillow",
}


def declared_runtime_dependencies() -> set[str]:
    """Distribution names from [project.dependencies], normalised.

    Deliberately ignores optional-dependencies: the whole point is to check
    what a plain `pip install .` provides.
    """
    # utf-8-sig, not utf-8: an editor that leaves a BOM would otherwise make
    # this fail with a TOML parse error rather than the answer it was asked for.
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))
    names = set()
    for spec in data["project"]["dependencies"]:
        name = re.split(r"[><=!~\[;\s]", spec, maxsplit=1)[0]
        names.add(name.strip().lower().replace("-", "_"))
    return names


def imported_third_party() -> dict[str, list[str]]:
    """Top-level third-party modules imported anywhere under src/, with sites."""
    found: dict[str, list[str]] = {}
    local = {p.name for p in SRC.iterdir() if p.is_dir()}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]
            else:
                continue
            for module in modules:
                if module in sys.stdlib_module_names or module in local:
                    continue
                site = f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                found.setdefault(module, []).append(site)
    return found


def main() -> int:
    declared = declared_runtime_dependencies()
    missing: list[tuple[str, list[str]]] = []

    for module, sites in sorted(imported_third_party().items()):
        distribution = IMPORT_TO_DISTRIBUTION.get(module, module)
        if distribution.lower().replace("-", "_") not in declared:
            missing.append((module, sites))

    if not missing:
        print(f"All third-party imports in src/ are declared ({len(declared)} runtime deps).")
        return 0

    print("Undeclared third-party imports:\n", file=sys.stderr)
    for module, sites in missing:
        distribution = IMPORT_TO_DISTRIBUTION.get(module, module)
        print(f"  {module}  (add '{distribution}' to project.dependencies)", file=sys.stderr)
        for site in sites:
            print(f"      {site}", file=sys.stderr)
    print(
        "\nA dev-only package may be supplying these locally, which is why "
        "the tests still pass.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
