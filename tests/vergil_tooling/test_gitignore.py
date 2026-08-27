"""Tests for the composable .gitignore fragment library.

The ground-truth check is the lossless-split invariant: the union of the base
fragment and every language fragment must equal the current monolithic
baseline's pattern set — no orphaned baseline lines, no invented fragment lines
(epic vergil-project/.github#325, spec §5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import gitignore, repo_config

if TYPE_CHECKING:
    import pytest


def test_split_is_lossless_against_baseline() -> None:
    """base ∪ all fragments == current baseline pattern set, exactly."""
    baseline = set(repo_config._gitignore_patterns(repo_config._load_gitignore_baseline()))
    fragments = set(gitignore.load_base())
    for lang in gitignore.FRAGMENT_LANGS:
        fragments.update(gitignore.load_fragment(lang))
    assert fragments == baseline


def test_fragment_langs_are_expected() -> None:
    assert gitignore.FRAGMENT_LANGS == (
        "python",
        "cpp",
        "go",
        "ruby",
        "rust",
        "java",
        "typescript",
    )


def test_load_base_is_nonempty_and_stripped() -> None:
    base = gitignore.load_base()
    assert base
    assert all(line == line.rstrip() for line in base)
    assert all(line.strip() for line in base)


def test_compose_per_language_length_is_base_plus_fragment() -> None:
    """Composed length equals base + fragment for every language (no dupes)."""
    base_len = len(gitignore.load_base())
    for lang in gitignore.FRAGMENT_LANGS:
        fragment = gitignore.load_fragment(lang)
        composed = gitignore.compose(lang)
        assert len(composed) == base_len + len(fragment)


def test_compose_python_contains_base_and_python_lines() -> None:
    composed = gitignore.compose("python")
    assert "__pycache__/" in composed
    assert "build/" in composed  # a base line


def test_compose_cpp_contains_cmakedeps_line() -> None:
    composed = gitignore.compose("cpp")
    assert "Find*.cmake" in composed  # a #2908 CMakeDeps line, cpp-only


def test_compose_none_is_base_only() -> None:
    assert gitignore.compose(None) == gitignore.load_base()


def test_compose_unknown_language_is_base_only() -> None:
    assert gitignore.compose("shell") == gitignore.load_base()


def test_empty_language_fragments_are_empty() -> None:
    assert gitignore.load_fragment("rust") == []
    assert gitignore.load_fragment("java") == []


def test_load_fragment_unknown_is_empty() -> None:
    assert gitignore.load_fragment("shell") == []
    assert gitignore.load_fragment(None) == []
    assert gitignore.load_fragment("") == []


def test_compose_order_is_base_then_fragment_no_dupes() -> None:
    base = gitignore.load_base()
    fragment = gitignore.load_fragment("python")
    composed = gitignore.compose("python")
    assert composed[: len(base)] == base
    assert composed[len(base) :] == fragment
    assert len(composed) == len(set(composed))


def test_compose_dedupes_overlap_between_base_and_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pattern shared by base and a fragment appears once, in base order."""
    monkeypatch.setattr(gitignore, "load_base", lambda: ["a", "b"])
    monkeypatch.setattr(gitignore, "load_fragment", lambda _lang: ["b", "c"])
    assert gitignore.compose("python") == ["a", "b", "c"]


def test_managed_vocabulary_contains_known_lines() -> None:
    vocab = gitignore.managed_vocabulary()
    assert "conan_toolchain.cmake" in vocab  # cpp
    assert ".mypy_cache/" in vocab  # python
    assert "build/" in vocab  # base


def test_managed_vocabulary_equals_baseline_set() -> None:
    baseline = set(repo_config._gitignore_patterns(repo_config._load_gitignore_baseline()))
    assert gitignore.managed_vocabulary() == baseline
