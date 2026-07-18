"""Tests for CLI helper functions."""

from __future__ import annotations

from game_save_genie.cli import _slugify


def test_slugify_matches_legacy_scheme() -> None:
    """Ids must stay byte-identical to those in existing games.yaml files —
    a changed scheme would re-add every tracked game under a new id."""
    assert _slugify("Solo Leveling: Arise") == "solo-leveling--arise"
    assert _slugify("Elden Ring") == "elden-ring"
    assert _slugify("  Trimmed  ") == "trimmed"


def test_slugify_keeps_unicode_titles() -> None:
    assert _slugify("Ведьмак") == "ведьмак"
    assert _slugify("エルデンリング") == "エルデンリング"


def test_slugify_never_returns_empty() -> None:
    slug = _slugify("!!!")
    assert slug.startswith("game-")
    assert len(slug) > 5
    # Deterministic: same title always maps to the same id.
    assert _slugify("!!!") == slug
