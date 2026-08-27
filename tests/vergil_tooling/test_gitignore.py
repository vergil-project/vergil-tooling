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


# --- Managed-block render/parse/check (Task 2) ------------------------------


def test_render_block_python_shape() -> None:
    block = gitignore.render_block("python")
    lines = block.splitlines()
    assert lines[0].startswith(gitignore.MANAGED_BEGIN_PREFIX)
    assert "base + python" in lines[0]
    assert lines[-1] == gitignore.MANAGED_END
    assert block.endswith("\n")
    assert "build/" in lines  # a base line
    assert "__pycache__/" in lines  # a python line


def test_render_block_none_is_base_descriptor() -> None:
    block = gitignore.render_block(None)
    lines = block.splitlines()
    descriptor_line = lines[0]
    assert descriptor_line.startswith(gitignore.MANAGED_BEGIN_PREFIX)
    assert "+" not in descriptor_line
    assert lines[-1] == gitignore.MANAGED_END
    assert "build/" in lines  # base line present
    assert "__pycache__/" not in lines  # no python fragment lines


def test_render_block_empty_fragment_lang_is_base_descriptor() -> None:
    """A known lang with an empty fragment (rust) renders a base-only block."""
    block = gitignore.render_block("rust")
    assert "+" not in block.splitlines()[0]


def test_parse_round_trips_repo_local_around_fence() -> None:
    fence_block = gitignore.render_block("python")
    text = "repo-local-1\n" + fence_block + "repo-local-2\n"
    repo_local, fence = gitignore.parse(text)
    assert repo_local == ["repo-local-1", "repo-local-2"]
    assert fence is not None
    assert fence.rstrip("\n") == fence_block.rstrip("\n")


def test_parse_no_fence_returns_all_lines_and_none() -> None:
    text = "alpha\nbeta\n"
    repo_local, fence = gitignore.parse(text)
    assert repo_local == ["alpha", "beta"]
    assert fence is None


def test_parse_malformed_begin_without_end_is_treated_as_no_fence() -> None:
    text = "alpha\n" + gitignore.MANAGED_BEGIN_PREFIX + " base (x) >>>\nbeta\n"
    repo_local, fence = gitignore.parse(text)
    assert fence is None
    assert repo_local == text.splitlines()


def test_check_compliant_for_rendered_python_block() -> None:
    result = gitignore.check(gitignore.render_block("python"), "python")
    assert result.compliant is True
    assert result.reasons == []


def test_check_compliant_for_base_only_block() -> None:
    result = gitignore.check(gitignore.render_block(None), None)
    assert result.compliant is True
    assert result.reasons == []


def test_check_flags_missing_fence() -> None:
    result = gitignore.check("just-a-local-line\n", "python")
    assert result.compliant is False
    assert any("fence" in reason.lower() for reason in result.reasons)


def test_check_flags_mangled_fence_body() -> None:
    block = gitignore.render_block("python")
    mangled = block.replace("__pycache__/", "__pycache__/EDITED")
    result = gitignore.check(mangled, "python")
    assert result.compliant is False
    assert any("match" in reason.lower() for reason in result.reasons)


def test_check_flags_stray_managed_line_outside_fence() -> None:
    block = gitignore.render_block("python")
    text = "my-local-thing/\n*.pyc\n" + block
    result = gitignore.check(text, "python")
    assert result.compliant is False
    assert any("outside" in reason.lower() for reason in result.reasons)
