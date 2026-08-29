"""Tests for the composable .gitignore fragment library.

The ground-truth check is the lossless-split invariant: the union of the base
fragment and every language fragment must equal the current monolithic
baseline's pattern set — no orphaned baseline lines, no invented fragment lines
(epic vergil-project/.github#325, spec §5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import gitignore

if TYPE_CHECKING:
    import pytest


#: The 62 pattern lines of the pre-Task-10 monolith
#: (``src/vergil_tooling/data/gitignore.baseline``), frozen verbatim before that
#: file was deleted (epic vergil-project/.github#325, Task 10). The lossless-split
#: invariant below proves ``base ∪ all-fragments`` still reconstitutes exactly
#: this set, so a future fragment edit that silently drops or invents a pattern
#: fails CI. This is the regression guard the monolith used to be.
_LEGACY_GITIGNORE_PATTERNS: tuple[str, ...] = (
    "*.swp",
    "*.swo",
    "*~",
    "*.bak",
    ".idea/",
    ".vscode/",
    ".DS_Store",
    "Thumbs.db",
    ".env",
    ".env.*",
    "*.log",
    ".venv/",
    ".worktrees/",
    ".vergil/",
    ".superpowers/",
    ".claude/scheduled_tasks.lock",
    ".claude/settings.local.json",
    "build/",
    "build-sanitize/",
    "dist/",
    "*.egg-info/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "coverage.xml",
    "junit.xml",
    "pip-audit.json",
    "licenses.json",
    "quality-ruff.json",
    "quality-mypy.xml",
    "docs/site/site/",
    "node_modules/",
    "*.tsbuildinfo",
    "*.test",
    "*.out",
    ".bundle/",
    "vendor/bundle/",
    "*.o",
    "*.obj",
    "*.a",
    "*.so",
    "CMakePresets.json",
    "CMakeUserPresets.json",
    "cmakedeps_macros.cmake",
    "conan_toolchain.cmake",
    "conandeps_legacy.cmake",
    "conanbuild*.sh",
    "conanrun*.sh",
    "conanbuildenv-*.sh",
    "conanrunenv-*.sh",
    "deactivate_conanbuild*.sh",
    "deactivate_conanrun*.sh",
    "Find*.cmake",
    "*Config.cmake",
    "*ConfigVersion.cmake",
    "*Targets.cmake",
    "*-Target-*.cmake",
    "*-data.cmake",
    "module-*.cmake",
)


def _legacy_commented_monolith(extra: list[str] | None = None) -> str:
    """Reconstruct the pre-Task-10 fully-commented monolith from frozen data.

    The monolith file was deleted in Task 10; sync's scaffolding-stripping
    behavior (issue #2939) is still exercised by feeding an equivalent blob —
    every managed comment plus every managed pattern — optionally with genuine
    ``extra`` repo-local lines appended.
    """
    comments = sorted(gitignore._MANAGED_COMMENTS)
    patterns = sorted(gitignore.managed_vocabulary())
    return "\n".join([*comments, *patterns, *(extra or [])]) + "\n"


def test_split_is_lossless_against_baseline() -> None:
    """base ∪ all fragments == the frozen 62 legacy monolith patterns, exactly."""
    fragments = set(gitignore.load_base())
    for lang in gitignore.FRAGMENT_LANGS:
        fragments.update(gitignore.load_fragment(lang))
    assert fragments == set(_LEGACY_GITIGNORE_PATTERNS)


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
    assert gitignore.managed_vocabulary() == set(_LEGACY_GITIGNORE_PATTERNS)


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


def test_check_allows_foreign_language_pattern_outside_base_only_fence() -> None:
    """A base-only repo may keep a foreign-language managed pattern repo-local.

    ``__pycache__/`` belongs to the Python fragment, not this base-only repo's
    own composed fence, so it is legitimately repo-local and must not be flagged
    as stray (issue #2966: vergil-containers' Python build tooling needs it).
    """
    block = gitignore.render_block(None)
    text = block + "\n# python build tooling writes bytecode here\n__pycache__/\n"
    result = gitignore.check(text, None)
    assert result.compliant is True
    assert result.reasons == []


# --- sync() bootstrap/update (Task 3) ---------------------------------------


def test_sync_bootstrap_filters_loose_and_foreign_lines() -> None:
    """A loose python repo whose .gitignore also carries cpp (foreign) lines.

    Bootstrap fences base+python, drops every managed line (python's own and the
    foreign cpp lines) from the repo-local section, preserves the genuine
    repo-local line, and reports the managed lines it removed.
    """
    base = gitignore.load_base()
    python = gitignore.load_fragment("python")
    cpp = gitignore.load_fragment("cpp")
    text = "\n".join(base + python + cpp + ["secrets.json"]) + "\n"

    result = gitignore.sync(text, "python")

    assert result.changed is True
    repo_local, fence = gitignore.parse(result.text)
    assert fence == gitignore.render_block("python")
    assert "secrets.json" in repo_local
    # Every cpp (foreign-language) line was dropped and reported as removed.
    for line in cpp:
        assert line in result.removed
    # The genuine repo-local line survives outside the fence.
    assert "secrets.json" not in gitignore.managed_vocabulary()


def test_sync_bootstrap_base_only_for_github_style_file() -> None:
    """A `.github`-style file (lang None) holding the full monolith.

    Bootstrap writes a base-only fence and reports every language-fragment line
    as removed; nothing repo-local remains.
    """
    monolith = "\n".join(sorted(gitignore.managed_vocabulary())) + "\n"

    result = gitignore.sync(monolith, None)

    assert result.changed is True
    repo_local, fence = gitignore.parse(result.text)
    assert fence == gitignore.render_block(None)
    assert fence is not None
    assert "+" not in fence.splitlines()[0]  # base-only descriptor
    assert repo_local == []
    for lang in gitignore.FRAGMENT_LANGS:
        for line in gitignore.load_fragment(lang):
            assert line in result.removed


def test_sync_is_idempotent() -> None:
    """A second sync is a no-op: no change, identical text, nothing removed."""
    base = gitignore.load_base()
    python = gitignore.load_fragment("python")
    cpp = gitignore.load_fragment("cpp")
    text = "\n".join(base + python + cpp + ["secrets.json"]) + "\n"

    first = gitignore.sync(text, "python")
    second = gitignore.sync(first.text, "python")

    assert second.changed is False
    assert second.text == first.text
    assert second.removed == []


def test_sync_update_rewrites_stale_fence_body() -> None:
    """An already-fenced file with a hand-added stale line inside the fence.

    Update rewrites the fence to the canonical rendered block (dropping the
    stale line) and leaves the repo-local section untouched.
    """
    block = gitignore.render_block("python")
    stale = block.replace(gitignore.MANAGED_END, "STALE_LINE\n" + gitignore.MANAGED_END)
    text = stale + "\nkeep-me/\n"

    result = gitignore.sync(text, "python")

    assert result.changed is True
    assert "STALE_LINE" not in result.text
    assert result.removed == []
    repo_local, fence = gitignore.parse(result.text)
    assert fence == gitignore.render_block("python")
    assert "keep-me/" in repo_local


def test_sync_update_preserves_repo_local_ordering() -> None:
    """Update keeps the repo-local lines in their original order."""
    block = gitignore.render_block("python")
    text = block + "\nzeta/\nalpha/\nmiddle/\n"

    result = gitignore.sync(text, "python")

    assert result.changed is False  # already canonical
    repo_local, _fence = gitignore.parse(result.text)
    assert [line for line in repo_local if line] == ["zeta/", "alpha/", "middle/"]


# --- sync() bootstrap: stale baseline comment scaffolding (issue #2939) ------


def test_managed_comments_includes_322_append_header() -> None:
    """The #322 sweep append header is a managed comment too."""
    header = (
        "# --- Vergil baseline sync (vergil-project/.github#322): "
        "lines added to match gitignore.baseline ---"
    )
    assert header in gitignore._MANAGED_COMMENTS


def test_sync_bootstrap_strips_full_commented_baseline_scaffolding() -> None:
    """A docs-style file = the FULL commented monolith baseline, no repo-local.

    Bootstrap (lang None) must land JUST the base fence: every baseline comment
    and pattern is dropped, with no orphaned section headers and no
    'single source of truth' preamble surviving below the fence.
    """
    monolith = _legacy_commented_monolith()

    result = gitignore.sync(monolith, None)

    assert result.changed is True
    assert result.text == gitignore.render_block(None)
    repo_local, fence = gitignore.parse(result.text)
    assert fence == gitignore.render_block(None)
    # Nothing repo-local survives — no orphaned comments, no preamble.
    assert repo_local == []
    for comment in gitignore._MANAGED_COMMENTS:
        assert comment not in result.text
    assert "single source of truth" not in result.text


def test_sync_bootstrap_keeps_genuine_repo_local_comment_and_pattern() -> None:
    """A genuine repo-local comment + pattern mixed into the commented baseline.

    The genuine comment and pattern survive outside the fence; the baseline
    comment scaffolding and patterns do not.
    """
    text = _legacy_commented_monolith(["# my project data", "data/big.bin"])

    result = gitignore.sync(text, None)

    assert result.changed is True
    repo_local, fence = gitignore.parse(result.text)
    assert fence == gitignore.render_block(None)
    assert "# my project data" in repo_local
    assert "data/big.bin" in repo_local
    # Baseline scaffolding is gone.
    assert "# Editors" not in repo_local
    assert "single source of truth" not in result.text


def test_sync_bootstrap_normalizes_blank_lines() -> None:
    """Runs of blank lines around dropped content collapse and trim.

    The surviving repo-local section has no leading, trailing, or consecutive
    blank lines.
    """
    text = "\n\n# Editors\n*.swp\n\n\n\n# keep-a\n\n\nkeep-b/\n\n\n"

    result = gitignore.sync(text, None)

    repo_local, _fence = gitignore.parse(result.text)
    # Drop the single blank separator the assembler inserts after the fence.
    body = repo_local[1:] if repo_local and repo_local[0] == "" else repo_local
    # Interior blank run collapses to a single blank; no leading/trailing blanks.
    assert body == ["# keep-a", "", "keep-b/"]
    assert body[0].strip()
    assert body[-1].strip()
    assert "\n\n\n" not in result.text


def test_sync_bootstrap_strips_322_append_header() -> None:
    """The #322 sweep append header is dropped on bootstrap."""
    header = (
        "# --- Vergil baseline sync (vergil-project/.github#322): "
        "lines added to match gitignore.baseline ---"
    )
    text = "keep-me/\n" + header + "\nbuild/\n"

    result = gitignore.sync(text, None)

    repo_local, _fence = gitignore.parse(result.text)
    assert header not in result.text
    assert "keep-me/" in repo_local


def test_sync_bootstrap_comment_strip_is_idempotent() -> None:
    """A second sync on a scaffolding-stripped bootstrap is a no-op."""
    text = _legacy_commented_monolith(["# my project data", "data/big.bin"])

    first = gitignore.sync(text, None)
    second = gitignore.sync(first.text, None)

    assert second.changed is False
    assert second.text == first.text
    assert second.removed == []
