"""Tests for vergil_tooling.bin.validate_common."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from vergil_tooling.bin.validate_common import (
    _ansible_lint_overrides,
    _check_runner_labels,
    _drop_gitignored,
    _find_ansible_lint_config,
    _find_dockerfiles,
    _find_markdown_files,
    _find_repo_sh_files,
    _find_shell_files,
    _find_workflow_files,
    _find_yaml_files,
    _has_ansible_content,
    main,
)
from vergil_tooling.lib import dependabot

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MINIMAL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


@pytest.fixture(autouse=True)
def _no_gitignore_filter() -> Iterator[None]:
    """Keep every discovered file: most tests use a plain ``tmp_path`` that is
    not a git repository, so ``git check-ignore`` would fail there. The
    gitignore filter itself is tested against a real repo below (#3594)."""
    with patch(
        "vergil_tooling.bin.validate_common._drop_gitignored",
        side_effect=lambda _root, paths: paths,
    ):
        yield


@pytest.fixture(autouse=True)
def _no_repo_sh_listing() -> Iterator[None]:
    """List no repo-wide ``*.sh`` files: ``git ls-files`` needs a real
    repository, which most tests' ``tmp_path`` is not. The listing itself is
    tested against a real repo below, through the name imported at module
    load, which this patch does not replace (#3830)."""
    with patch(
        "vergil_tooling.bin.validate_common._find_repo_sh_files",
        return_value=[],
    ):
        yield


@pytest.fixture(autouse=True)
def _no_workflow_listing() -> Iterator[None]:
    """List workflow files from the directory, not ``git ls-files``, which
    needs a real repository that most tests' ``tmp_path`` is not. The listing
    itself is tested against a real repo below, through the name imported at
    module load, which this patch does not replace (#4129)."""

    def _listing(repo_root: Path) -> list[str]:
        workflows = repo_root / ".github" / "workflows"
        if not workflows.is_dir():
            return []
        return sorted(
            str(path)
            for path in workflows.iterdir()
            if path.is_file() and path.suffix in {".yml", ".yaml"}
        )

    with patch(
        "vergil_tooling.bin.validate_common._find_workflow_files",
        side_effect=_listing,
    ):
        yield


def _git_repo(path: Path, gitignore: str) -> Path:
    """Initialise a real git repository at *path* with *gitignore*."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    (path / ".gitignore").write_text(gitignore)
    return path


# -- _drop_gitignored ---------------------------------------------------------


def test_drop_gitignored_drops_ignored_keeps_rest(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", "docs/site/docs/releases/\n")
    releases = repo / "docs" / "site" / "docs" / "releases"
    releases.mkdir(parents=True)
    generated = str(releases / "v1.0.md")
    source = str(repo / "docs" / "site" / "docs" / "index.md")
    (releases / "v1.0.md").write_text("# Generated\n")
    (repo / "docs" / "site" / "docs" / "index.md").write_text("# Source\n")

    assert _drop_gitignored(repo, [source, generated]) == [source]


def test_drop_gitignored_keeps_tracked_file_matching_pattern(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", "*.md\n")
    readme = repo / "README.md"
    readme.write_text("# Hello\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "README.md"], check=True, capture_output=True
    )

    assert _drop_gitignored(repo, [str(readme)]) == [str(readme)]


def test_drop_gitignored_empty_list(tmp_path: Path) -> None:
    assert _drop_gitignored(tmp_path, []) == []


def test_drop_gitignored_outside_repo_raises(tmp_path: Path) -> None:
    """Outside a repository check-ignore fails: an error, never 'lint less'."""
    (tmp_path / "x.md").write_text("# X\n")
    with pytest.raises(subprocess.CalledProcessError):
        _drop_gitignored(tmp_path, [str(tmp_path / "x.md")])


@pytest.mark.skipif(
    shutil.which("markdownlint") is None,
    reason="markdownlint binary not on PATH (only present in the dev container)",
)
def test_main_skips_gitignored_markdown(tmp_path: Path) -> None:
    """A gitignored build artifact under docs/site/ that would FAIL lint is
    skipped, while the committed docs still lint clean (issue #3594)."""
    repo = _git_repo(tmp_path / "repo", "docs/site/docs/changelog.md\n")
    (repo / "vergil.toml").write_text(_MINIMAL_TOML)
    docs = repo / "docs" / "site" / "docs"
    docs.mkdir(parents=True)
    (docs / "index.md").write_text("# Hello\n")
    (docs / "changelog.md").write_text("no heading   \n\n\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common._drop_gitignored",
            side_effect=_drop_gitignored,
        ),
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=repo,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
    ):
        assert main() == 0
        # Un-ignoring the same file makes the step fail: the skip, not a
        # lenient config, is what let it pass.
        (repo / ".gitignore").write_text("")
        assert main() != 0


# -- _find_shell_files --------------------------------------------------------


def test_find_shell_files_none(tmp_path: Path) -> None:
    assert _find_shell_files(tmp_path) == []


def test_find_shell_files_discovers_sh(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts" / "dev"
    scripts.mkdir(parents=True)
    (scripts / "lint.sh").write_text("#!/bin/bash\n")
    result = _find_shell_files(tmp_path)
    assert len(result) == 1
    assert result[0].endswith("lint.sh")


def test_find_shell_files_discovers_bin(tmp_path: Path) -> None:
    scripts_bin = tmp_path / "scripts" / "bin"
    scripts_bin.mkdir(parents=True)
    (scripts_bin / "my-script").write_text("#!/bin/bash\n")
    result = _find_shell_files(tmp_path)
    assert len(result) == 1
    assert "my-script" in result[0]


def test_find_shell_files_discovers_git_hooks(tmp_path: Path) -> None:
    hooks = tmp_path / "scripts" / "lib" / "git-hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/bash\n")
    result = _find_shell_files(tmp_path)
    assert len(result) == 1
    assert "pre-commit" in result[0]


def test_find_shell_files_skips_non_matching(tmp_path: Path) -> None:
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "README.md").write_text("# Not a shell file\n")
    result = _find_shell_files(tmp_path)
    assert result == []


def test_find_shell_files_sorted(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts" / "dev"
    scripts.mkdir(parents=True)
    (scripts / "b.sh").write_text("#!/bin/bash\n")
    (scripts / "a.sh").write_text("#!/bin/bash\n")
    result = _find_shell_files(tmp_path)
    assert result[0] < result[1]


def test_find_shell_files_adds_repo_sh_files_once(tmp_path: Path) -> None:
    """The repo-wide ``*.sh`` listing joins the scripts/ scan, and a file both
    find (a ``scripts/**/*.sh``) appears once, in sorted order (#3830)."""
    scripts = tmp_path / "scripts" / "dev"
    scripts.mkdir(parents=True)
    (scripts / "lint.sh").write_text("#!/bin/bash\n")
    hook = str(tmp_path / "hooks" / "scripts" / "gate.sh")
    with patch(
        "vergil_tooling.bin.validate_common._find_repo_sh_files",
        return_value=[hook, str(scripts / "lint.sh")],
    ):
        result = _find_shell_files(tmp_path)
    assert result == sorted([hook, str(scripts / "lint.sh")])


# -- _find_repo_sh_files ------------------------------------------------------


def test_find_repo_sh_files_lists_tracked_and_untracked(tmp_path: Path) -> None:
    """``*.sh`` at any depth is found, committed or not, while a gitignored
    one, a non-``.sh`` file and a tracked file deleted from disk are not."""
    repo = _git_repo(tmp_path / "repo", ".worktrees/\n")
    (repo / "hooks" / "scripts" / "lib").mkdir(parents=True)
    (repo / "hooks" / "scripts" / "gate.sh").write_text("#!/bin/bash\n")
    (repo / "hooks" / "scripts" / "lib" / "util.sh").write_text("#!/bin/bash\n")
    (repo / "hooks" / "hooks.json").write_text("{}\n")
    (repo / "top.sh").write_text("#!/bin/bash\n")
    (repo / "gone.sh").write_text("#!/bin/bash\n")
    (repo / ".worktrees" / "wt").mkdir(parents=True)
    (repo / ".worktrees" / "wt" / "copy.sh").write_text("#!/bin/bash\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "hooks/scripts/gate.sh", "gone.sh"],
        check=True,
        capture_output=True,
    )
    (repo / "gone.sh").unlink()

    assert sorted(_find_repo_sh_files(repo)) == [
        str(repo / "hooks" / "scripts" / "gate.sh"),
        str(repo / "hooks" / "scripts" / "lib" / "util.sh"),
        str(repo / "top.sh"),
    ]


def test_find_repo_sh_files_none(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", "")
    (repo / "README.md").write_text("# Hi\n")
    assert _find_repo_sh_files(repo) == []


def test_find_repo_sh_files_outside_repo_raises(tmp_path: Path) -> None:
    """Outside a repository ls-files fails: an error, never 'lint less'."""
    (tmp_path / "x.sh").write_text("#!/bin/bash\n")
    with pytest.raises(subprocess.CalledProcessError):
        _find_repo_sh_files(tmp_path)


# -- _find_markdown_files ----------------------------------------------------


def test_find_markdown_files_none(tmp_path: Path) -> None:
    assert _find_markdown_files(tmp_path) == []


def test_find_markdown_files_site(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    result = _find_markdown_files(tmp_path)
    assert len(result) == 1
    assert result[0].endswith("index.md")


def test_find_markdown_files_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Hello\n")
    result = _find_markdown_files(tmp_path)
    assert len(result) == 1
    assert result[0].endswith("README.md")


def test_find_markdown_files_epics_nested(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    (epics / "epic-1").mkdir(parents=True)
    (epics / "epic-2").mkdir(parents=True)
    (epics / "epic-1" / "spec.md").write_text("# Spec\n")
    (epics / "epic-1" / "plan.md").write_text("# Plan\n")
    (epics / "epic-2" / "retrospective.md").write_text("# Retro\n")
    result = _find_markdown_files(tmp_path)
    assert len(result) == 3


def test_find_markdown_files_all_scopes(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (tmp_path / "README.md").write_text("# Project\n")
    epic = tmp_path / "epics" / "epic-1"
    epic.mkdir(parents=True)
    (epic / "spec.md").write_text("# Spec\n")
    result = _find_markdown_files(tmp_path)
    assert len(result) == 3


def test_find_markdown_files_outside_scope_skipped(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text("# Notes\n")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")
    assert _find_markdown_files(tmp_path) == []


def test_find_markdown_files_sorted(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "b.md").write_text("# B\n")
    (site / "a.md").write_text("# A\n")
    epics = tmp_path / "epics"
    epics.mkdir()
    (epics / "b.md").write_text("# B\n")
    (epics / "a.md").write_text("# A\n")
    result = _find_markdown_files(tmp_path)
    assert result == sorted(result)


def test_find_markdown_files_ignore_directory(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    research = site / "docs" / "research"
    research.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (research / "report.md").write_text("# Report\n")
    result = _find_markdown_files(tmp_path, ignore=["docs/site/docs/research"])
    assert len(result) == 1
    assert result[0].endswith("index.md")


def test_find_markdown_files_ignore_nested(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    deep = site / "docs" / "research" / "2026" / "output"
    deep.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (deep / "report.md").write_text("# Report\n")
    result = _find_markdown_files(tmp_path, ignore=["docs/site/docs/research"])
    assert len(result) == 1
    assert result[0].endswith("index.md")


def test_find_markdown_files_ignore_multiple(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    research = site / "docs" / "research"
    archive = site / "docs" / "archive"
    research.mkdir(parents=True)
    archive.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (research / "report.md").write_text("# Report\n")
    (archive / "old.md").write_text("# Old\n")
    result = _find_markdown_files(
        tmp_path,
        ignore=["docs/site/docs/research", "docs/site/docs/archive"],
    )
    assert len(result) == 1
    assert result[0].endswith("index.md")


def test_find_markdown_files_ignore_epics(tmp_path: Path) -> None:
    epics = tmp_path / "epics"
    (epics / "epic-1").mkdir(parents=True)
    (epics / "archived").mkdir(parents=True)
    (epics / "epic-1" / "spec.md").write_text("# Spec\n")
    (epics / "archived" / "old.md").write_text("# Old\n")
    result = _find_markdown_files(tmp_path, ignore=["epics/archived"])
    assert len(result) == 1
    assert result[0].endswith("spec.md")


def test_find_markdown_files_ignore_does_not_affect_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Hello\n")
    result = _find_markdown_files(tmp_path, ignore=["docs/site/docs/research"])
    assert len(result) == 1
    assert result[0].endswith("README.md")


def test_find_markdown_files_ignore_empty_list(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    result = _find_markdown_files(tmp_path, ignore=[])
    assert len(result) == 1


# -- main --------------------------------------------------------------------


def test_main_all_pass(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
    ):
        assert main() == 0


def test_main_repo_profile_fails(tmp_path: Path) -> None:
    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=1,
        ),
    ):
        assert main() == 1


def test_main_markdownlint_uses_bundled_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "markdownlint"
    assert call_args[1] == "--config"
    assert call_args[2].endswith("markdownlint.yaml")
    assert "vergil_tooling" in call_args[2]


def test_main_markdownlint_ignores_repo_local_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (tmp_path / ".markdownlint.yaml").write_text("default: true\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    ml_call = mock_run.call_args_list[0][0][0]
    assert ml_call[0] == "markdownlint"
    assert ml_call[1] == "--config"
    assert str(tmp_path) not in ml_call[2]
    assert "vergil_tooling" in ml_call[2]


def test_main_markdownlint_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / "README.md").write_text("# Hello\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


def test_main_markdownlint_honors_ignore(tmp_path: Path) -> None:
    toml = _MINIMAL_TOML + '\n[markdownlint]\nignore = ["docs/site/docs/research"]\n'
    (tmp_path / "vergil.toml").write_text(toml)
    site = tmp_path / "docs" / "site"
    research = site / "docs" / "research"
    research.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (research / "report.md").write_text("# Report\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    ml_call = mock_run.call_args_list[0][0][0]
    assert ml_call[0] == "markdownlint"
    md_args = ml_call[3:]
    assert len(md_args) == 1
    assert md_args[0].endswith("index.md")
    assert not any("research" in a for a in md_args)


def test_main_markdownlint_one_pass_covers_every_scope(tmp_path: Path) -> None:
    """docs/site, README and epics/ lint in one markdownlint invocation, against
    the bundled config."""
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (tmp_path / "README.md").write_text("# Project\n")
    epic = tmp_path / "epics" / "epic-1"
    epic.mkdir(parents=True)
    (epic / "spec.md").write_text("# Spec\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0

    md_calls = [c[0][0] for c in mock_run.call_args_list if c[0][0][0] == "markdownlint"]
    assert len(md_calls) == 1
    call = md_calls[0]
    assert call[1] == "--config"
    assert call[2].endswith("markdownlint.yaml")
    md_args = call[3:]
    assert any(a.endswith("index.md") for a in md_args)
    assert any(a.endswith("README.md") for a in md_args)
    assert any(a.endswith("spec.md") for a in md_args)


def test_main_markdownlint_epics_violation_fails_step(tmp_path: Path) -> None:
    """A violation in epics/ fails the step when it is the only scope with files."""
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    epic = tmp_path / "epics" / "epic-1"
    epic.mkdir(parents=True)
    (epic / "spec.md").write_text("# Spec\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


# A prose line well over 100 columns, otherwise clean (leading heading, blank
# line, trailing newline) so line length is the only thing it could trip.
_LONG_LINE = (
    "This is a long line of prose that intentionally runs well past one "
    "hundred columns, which the bundled config no longer limits, for testing."
)
_LONG_DOC = f"# Title\n\n{_LONG_LINE}\n"


@pytest.mark.skipif(
    shutil.which("markdownlint") is None,
    reason="markdownlint binary not on PATH (only present in the dev container)",
)
@pytest.mark.parametrize("scope", ["epics/epic-1", "docs/site"])
def test_main_long_line_passes_with_md013_off(tmp_path: Path, scope: str) -> None:
    """A >100-char line passes in every scope because the bundled config turns
    MD013 off (issue #4103), proven end to end against the real binary."""
    assert len(_LONG_LINE) > 100
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    folder = tmp_path / scope
    folder.mkdir(parents=True)
    (folder / "long.md").write_text(_LONG_DOC)

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
    ):
        assert main() == 0


def test_main_shellcheck_runs(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    scripts = tmp_path / "scripts" / "dev"
    scripts.mkdir(parents=True)
    (scripts / "lint.sh").write_text("#!/bin/bash\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "shellcheck"


def test_main_shellcheck_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    scripts = tmp_path / "scripts" / "dev"
    scripts.mkdir(parents=True)
    (scripts / "lint.sh").write_text("#!/bin/bash\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


# -- _find_yaml_files --------------------------------------------------------


def test_find_yaml_files_none(tmp_path: Path) -> None:
    assert _find_yaml_files(tmp_path) == []


def test_find_yaml_files_repo_root(tmp_path: Path) -> None:
    (tmp_path / ".markdownlint.yaml").write_text("default: true\n")
    (tmp_path / ".yamllint").write_text("extends: default\n")  # no .yml/.yaml suffix
    result = _find_yaml_files(tmp_path)
    assert len(result) == 1
    assert result[0].endswith(".markdownlint.yaml")


def test_find_yaml_files_github_workflows(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")
    (workflows / "release.yaml").write_text("name: Release\n")
    result = _find_yaml_files(tmp_path)
    assert len(result) == 2


def test_find_yaml_files_github_issue_templates(tmp_path: Path) -> None:
    templates = tmp_path / ".github" / "ISSUE_TEMPLATE"
    templates.mkdir(parents=True)
    (templates / "issue.yml").write_text("name: Issue\n")
    result = _find_yaml_files(tmp_path)
    assert any(p.endswith("issue.yml") for p in result)


def test_find_yaml_files_mkdocs(tmp_path: Path) -> None:
    docs_site = tmp_path / "docs" / "site"
    docs_site.mkdir(parents=True)
    (docs_site / "mkdocs.yml").write_text("site_name: docs\n")
    result = _find_yaml_files(tmp_path)
    assert len(result) == 1
    assert result[0].endswith("mkdocs.yml")


def test_find_yaml_files_skips_worktrees_and_venv(tmp_path: Path) -> None:
    for skip in (".worktrees", ".venv", "node_modules"):
        nested = tmp_path / skip / ".github" / "workflows"
        nested.mkdir(parents=True)
        (nested / "ci.yml").write_text("name: CI\n")
    real = tmp_path / ".github" / "workflows"
    real.mkdir(parents=True)
    (real / "ci.yml").write_text("name: CI\n")
    result = _find_yaml_files(tmp_path)
    assert len(result) == 1
    assert ".worktrees" not in result[0]
    assert ".venv" not in result[0]


def test_find_yaml_files_sorted_and_deduped(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "b.yml").write_text("name: b\n")
    (workflows / "a.yml").write_text("name: a\n")
    result = _find_yaml_files(tmp_path)
    assert result == sorted(result)
    assert len(result) == len(set(result))


# -- main: yamllint path -----------------------------------------------------


def test_main_yamllint_uses_bundled_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    yamllint_call = mock_run.call_args_list[0][0][0]
    assert yamllint_call[0] == "yamllint"
    assert yamllint_call[1] == "--config-file"
    assert yamllint_call[2].endswith("yamllint.yaml")
    assert "vergil_tooling" in yamllint_call[2]


def test_main_yamllint_ignores_repo_local_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")
    (tmp_path / ".yamllint").write_text("extends: default\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    yamllint_call = mock_run.call_args_list[0][0][0]
    assert yamllint_call[0] == "yamllint"
    assert yamllint_call[1] == "--config-file"
    assert str(tmp_path) not in yamllint_call[2]
    assert "vergil_tooling" in yamllint_call[2]


def test_main_yamllint_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


# -- main: dependabot limit-0 guard (epic vergil-project/.github#420) ---------


def _run_main_with_dependabot(tmp_path: Path, text: str | None) -> tuple[int, list[list[str]]]:
    """Run ``main`` with every subprocess passing and an optional dependabot.yml."""
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    if text is not None:
        github_dir = tmp_path / ".github"
        github_dir.mkdir(parents=True, exist_ok=True)
        (github_dir / "dependabot.yml").write_text(text)
    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        rc = main()
    return rc, [c[0][0] for c in mock_run.call_args_list]


def test_validate_dependabot_absent_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _calls = _run_main_with_dependabot(tmp_path, None)
    assert rc == 0
    assert "dependabot-limit" not in capsys.readouterr().out


def test_validate_dependabot_limit_zero_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _calls = _run_main_with_dependabot(tmp_path, dependabot.render("python"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Running: dependabot-limit (.github/dependabot.yml)" in out


def test_validate_dependabot_nonzero_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "version: 2\nupdates:\n"
        "  - package-ecosystem: npm\n    directory: /\n    open-pull-requests-limit: 5\n"
    )
    rc, calls = _run_main_with_dependabot(tmp_path, text)
    assert rc == 1
    err = capsys.readouterr().err
    assert "dependabot.yml: updates entry npm has open-pull-requests-limit: 5" in err
    # The guard stops the pipeline: actionlint etc. never run after it.
    assert all(c[0] != "actionlint" for c in calls)


def test_validate_dependabot_missing_limit_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "version: 2\nupdates:\n  - package-ecosystem: pip\n    directory: /\n"
    rc, _calls = _run_main_with_dependabot(tmp_path, text)
    assert rc == 1
    assert "dependabot.yml: updates entry pip is missing" in capsys.readouterr().err


def test_validate_dependabot_unparseable_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _calls = _run_main_with_dependabot(tmp_path, "updates: [\n")
    assert rc == 1
    assert "dependabot.yml: unparseable: " in capsys.readouterr().err


def test_validate_dependabot_does_not_require_exact_match(tmp_path: Path) -> None:
    """The guard checks only the limit; a non-canonical but limit-0 file passes."""
    text = "version: 2\nupdates:\n  - package-ecosystem: npm\n    open-pull-requests-limit: 0\n"
    rc, _calls = _run_main_with_dependabot(tmp_path, text)
    assert rc == 0


# -- _find_dockerfiles -------------------------------------------------------


def test_find_dockerfiles_none(tmp_path: Path) -> None:
    assert _find_dockerfiles(tmp_path) == []


def test_find_dockerfiles_discovers_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    result = _find_dockerfiles(tmp_path)
    assert len(result) == 1
    assert result[0].endswith("Dockerfile")


def test_find_dockerfiles_discovers_variants(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    (tmp_path / "Dockerfile.dev").write_text("FROM python:3.12\n")
    result = _find_dockerfiles(tmp_path)
    assert len(result) == 2


def test_find_dockerfiles_sorted(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile.dev").write_text("FROM python:3.12\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    result = _find_dockerfiles(tmp_path)
    assert result == sorted(result)


def test_find_dockerfiles_ignores_non_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Project\n")
    (tmp_path / "Makefile").write_text("build:\n")
    assert _find_dockerfiles(tmp_path) == []


# -- main: hadolint path ----------------------------------------------------


def test_main_hadolint_runs(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    hadolint_call = mock_run.call_args_list[0][0][0]
    assert hadolint_call[0] == "hadolint"


def test_main_hadolint_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


# -- main: actionlint path --------------------------------------------------


def test_main_actionlint_runs(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    tool_names = [call[0][0][0] for call in mock_run.call_args_list]
    assert "actionlint" in tool_names


def test_main_actionlint_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")

    calls = []

    def mock_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        if cmd[0] == "actionlint":
            return subprocess.CompletedProcess(args=cmd, returncode=1)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            side_effect=mock_run,
        ),
    ):
        assert main() == 1


# -- _check_runner_labels (epic vergil-project/.github#649) -------------------


def _workflow(repo_root: Path, text: str, name: str = "ci.yml") -> Path:
    """Write *text* as ``.github/workflows/<name>`` under *repo_root*."""
    workflows = repo_root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    path = workflows / name
    path.write_text(text)
    return path


_JOB_HEAD = "name: CI\non: push\njobs:\n  build:\n"
_STEPS = "    steps:\n      - run: true\n"


def test_runner_label_scalar_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(tmp_path, _JOB_HEAD + "    runs-on: ubuntu-latest\n" + _STEPS)
    assert _check_runner_labels(tmp_path) == 1
    err = capsys.readouterr().err
    assert ".github/workflows/ci.yml:5: ubuntu-latest is not allowed; use ubuntu-24.04" in err


def test_runner_label_list_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(tmp_path, _JOB_HEAD + "    runs-on: [self-hosted, ubuntu-latest]\n" + _STEPS)
    assert _check_runner_labels(tmp_path) == 1
    assert ".github/workflows/ci.yml:5: ubuntu-latest is not allowed" in capsys.readouterr().err


def test_runner_label_pinned_passes(tmp_path: Path) -> None:
    text = (
        _JOB_HEAD
        + "    runs-on: ubuntu-24.04\n"
        + _STEPS
        + "  arm:\n    runs-on: ubuntu-24.04-arm\n"
        + _STEPS
    )
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 0


def test_runner_label_comment_ignored(tmp_path: Path) -> None:
    text = _JOB_HEAD + "    # runs-on: ubuntu-latest\n    runs-on: ubuntu-24.04\n" + _STEPS
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 0


def test_runner_label_expression_passes(tmp_path: Path) -> None:
    text = (
        _JOB_HEAD
        + "    strategy:\n      matrix:\n        os: [ubuntu-24.04]\n"
        + "    runs-on: ${{ matrix.os }}\n"
        + _STEPS
    )
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 0


def test_runner_label_matrix_value_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        _JOB_HEAD
        + "    strategy:\n      matrix:\n        os: [ubuntu-latest]\n"
        + "    runs-on: ${{ matrix.os }}\n"
        + _STEPS
    )
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 1
    assert ".github/workflows/ci.yml:7: ubuntu-latest is not allowed" in capsys.readouterr().err


def test_runner_label_with_input_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "name: CI\non: push\njobs:\n  call:\n"
        "    uses: org/repo/.github/workflows/x.yml@v1\n"
        "    with: {runner: ubuntu-latest}\n"
    )
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 1
    assert ".github/workflows/ci.yml:6: ubuntu-latest is not allowed" in capsys.readouterr().err


def test_runner_label_no_workflows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check_runner_labels(tmp_path) == 0
    assert "runner-labels" not in capsys.readouterr().out


def test_runner_label_every_hit_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every hit in every workflow is named, ``*.yaml`` included."""
    _workflow(tmp_path, _JOB_HEAD + "    runs-on: ubuntu-latest\n" + _STEPS)
    _workflow(
        tmp_path,
        _JOB_HEAD + "    runs-on: ubuntu-latest\n" + _STEPS + "  b:\n    runs-on: ubuntu-latest\n",
        name="release.yaml",
    )
    assert _check_runner_labels(tmp_path) == 1
    err = capsys.readouterr().err
    assert ".github/workflows/ci.yml:5:" in err
    assert ".github/workflows/release.yaml:5:" in err
    assert ".github/workflows/release.yaml:9:" in err


def test_runner_label_quoted_refused(tmp_path: Path) -> None:
    _workflow(tmp_path, _JOB_HEAD + '    runs-on: "ubuntu-latest"\n' + _STEPS)
    assert _check_runner_labels(tmp_path) == 1


def test_runner_label_substring_passes(tmp_path: Path) -> None:
    """Only a value that is exactly ``ubuntu-latest`` is refused."""
    text = _JOB_HEAD + "    runs-on: ubuntu-24.04\n    steps:\n      - run: echo ubuntu-latest\n"
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 0


def test_runner_label_alias_reported_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An aliased node is one value in the file, so it is named once, at its anchor."""
    text = (
        "name: CI\non: push\nenv:\n  RUNNER: &r ubuntu-latest\njobs:\n  build:\n"
        "    runs-on: *r\n" + _STEPS
    )
    _workflow(tmp_path, text)
    assert _check_runner_labels(tmp_path) == 1
    err = capsys.readouterr().err
    assert err.count("ubuntu-latest is not allowed") == 1
    assert ".github/workflows/ci.yml:4:" in err


def test_runner_label_unparseable_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A workflow that is not YAML fails the check rather than passing unread."""
    _workflow(tmp_path, "jobs: [\n")
    assert _check_runner_labels(tmp_path) == 1
    assert ".github/workflows/ci.yml: unparseable: " in capsys.readouterr().err


def test_main_runner_labels_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    _workflow(tmp_path, _JOB_HEAD + "    runs-on: ubuntu-latest\n" + _STEPS)
    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ),
    ):
        assert main() == 1
    captured = capsys.readouterr()
    assert "Running: runner-labels" in captured.out
    assert "ubuntu-latest is not allowed; use ubuntu-24.04" in captured.err


# -- _find_workflow_files -----------------------------------------------------


def test_find_workflow_files_lists_top_level_yaml(tmp_path: Path) -> None:
    """Top-level ``*.yml``/``*.yaml`` workflows are found, committed or not,
    while a gitignored one, a nested one, a non-YAML file and a tracked file
    deleted from disk are not."""
    repo = _git_repo(tmp_path / "repo", "local.yml\n")
    workflows = repo / ".github" / "workflows"
    (workflows / "sub").mkdir(parents=True)
    for name in ("ci.yml", "release.yaml", "local.yml", "gone.yml", "README.md"):
        (workflows / name).write_text("name: x\n")
    (workflows / "sub" / "nested.yml").write_text("name: x\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".github/workflows/ci.yml", ".github/workflows/gone.yml"],
        check=True,
        capture_output=True,
    )
    (workflows / "gone.yml").unlink()

    assert _find_workflow_files(repo) == [
        str(workflows / "ci.yml"),
        str(workflows / "release.yaml"),
    ]


def test_find_workflow_files_none(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", "")
    assert _find_workflow_files(repo) == []


def test_find_workflow_files_outside_repo_raises(tmp_path: Path) -> None:
    """Outside a repository ls-files fails: an error, never 'check less'."""
    _workflow(tmp_path, "name: x\n")
    with pytest.raises(subprocess.CalledProcessError):
        _find_workflow_files(tmp_path)


# -- _has_ansible_content ----------------------------------------------------


def test_has_ansible_content_none(tmp_path: Path) -> None:
    assert _has_ansible_content(tmp_path) is False


def test_has_ansible_content_ansible_cfg(tmp_path: Path) -> None:
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_ansible_lint_config(tmp_path: Path) -> None:
    (tmp_path / ".ansible-lint").write_text("profile: production\n")
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_ansible_lint_config_yml(tmp_path: Path) -> None:
    (tmp_path / ".ansible-lint.yml").write_text("profile: production\n")
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_galaxy(tmp_path: Path) -> None:
    (tmp_path / "galaxy.yml").write_text("namespace: acme\n")
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_meta_main(tmp_path: Path) -> None:
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "main.yml").write_text("galaxy_info: {}\n")
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_playbooks_dir(tmp_path: Path) -> None:
    (tmp_path / "playbooks").mkdir()
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_roles_dir(tmp_path: Path) -> None:
    (tmp_path / "roles").mkdir()
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_ansible_dir(tmp_path: Path) -> None:
    # A top-level ``ansible/`` directory is the common convention for
    # standardizing Ansible layout (issue #1906).
    (tmp_path / "ansible").mkdir()
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_nested_ansible_roles(tmp_path: Path) -> None:
    (tmp_path / "ansible" / "roles").mkdir(parents=True)
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_nested_ansible_playbooks(tmp_path: Path) -> None:
    (tmp_path / "ansible" / "playbooks").mkdir(parents=True)
    assert _has_ansible_content(tmp_path) is True


def test_has_ansible_content_signal_file_must_be_file(tmp_path: Path) -> None:
    # A directory named like a file signal must not count as content.
    (tmp_path / "ansible.cfg").mkdir()
    assert _has_ansible_content(tmp_path) is False


def test_has_ansible_content_dir_signal_must_be_dir(tmp_path: Path) -> None:
    # A file named like a directory signal must not count as content.
    (tmp_path / "roles").write_text("not a dir\n")
    assert _has_ansible_content(tmp_path) is False


# -- main: ansible-lint path -------------------------------------------------


def test_main_ansible_lint_runs_with_bundled_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    mock_run.assert_called_once()
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "ansible-lint"
    assert call_args[1] == "-c"
    assert call_args[2].endswith("ansible-lint.yaml")
    assert "vergil_tooling" in call_args[2]


def test_main_ansible_lint_skipped_without_content(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    tool_names = [call[0][0][0] for call in mock_run.call_args_list]
    assert "ansible-lint" not in tool_names


def test_main_ansible_lint_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


# -- _find_ansible_lint_config -----------------------------------------------


def test_find_ansible_lint_config_none(tmp_path: Path) -> None:
    assert _find_ansible_lint_config(tmp_path) is None


def test_find_ansible_lint_config_prefers_discovery_order(tmp_path: Path) -> None:
    (tmp_path / ".ansible-lint.yml").write_text("skip_list: [name]\n")
    (tmp_path / ".ansible-lint").write_text("skip_list: [yaml]\n")
    assert _find_ansible_lint_config(tmp_path) == tmp_path / ".ansible-lint"


def test_find_ansible_lint_config_yaml_suffix(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yaml"
    cfg.write_text("skip_list: [name]\n")
    assert _find_ansible_lint_config(tmp_path) == cfg


# -- _ansible_lint_overrides -------------------------------------------------


def test_ansible_lint_overrides_skip_list(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("skip_list:\n  - name[casing]\n  - yaml[line-length]\n")
    assert _ansible_lint_overrides(cfg) == ["-x", "name[casing],yaml[line-length]"]


def test_ansible_lint_overrides_exclude_paths(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("exclude_paths:\n  - config/app.yml\n  - vendor/\n")
    assert _ansible_lint_overrides(cfg) == [
        "--exclude",
        "config/app.yml",
        "--exclude",
        "vendor/",
    ]


def test_ansible_lint_overrides_both_keys(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("skip_list: [name]\nexclude_paths: [config/app.yml]\n")
    assert _ansible_lint_overrides(cfg) == [
        "-x",
        "name",
        "--exclude",
        "config/app.yml",
    ]


def test_ansible_lint_overrides_empty_config(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("")
    assert _ansible_lint_overrides(cfg) == []


def test_ansible_lint_overrides_only_baseline_keys(tmp_path: Path) -> None:
    # Keys we do not translate are ignored; only skip_list / exclude_paths
    # become CLI flags.
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("profile: min\nwarn_list:\n  - experimental\n")
    assert _ansible_lint_overrides(cfg) == []


def test_ansible_lint_overrides_empty_lists(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("skip_list: []\nexclude_paths: []\n")
    assert _ansible_lint_overrides(cfg) == []


def test_ansible_lint_overrides_malformed_yaml_raises(tmp_path: Path) -> None:
    cfg = tmp_path / ".ansible-lint.yml"
    cfg.write_text("skip_list: [unterminated\n")
    with pytest.raises(yaml.YAMLError):
        _ansible_lint_overrides(cfg)


# -- main: ansible-lint local-config integration -----------------------------


def test_main_ansible_lint_passes_local_overrides(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / ".ansible-lint.yml").write_text(
        "skip_list:\n  - name[casing]\nexclude_paths:\n  - config/app.yml\n"
    )

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "ansible-lint"
    assert call_args[1] == "-c"
    assert call_args[2].endswith("ansible-lint.yaml")
    # The bundled baseline is preserved and the repo's levers are layered on.
    assert call_args[3:] == ["-x", "name[casing]", "--exclude", "config/app.yml"]


def test_main_ansible_lint_no_local_config_no_overrides(tmp_path: Path) -> None:
    # Ansible content present (roles/) but no local ansible-lint config: the
    # command is exactly the bundled baseline, unchanged.
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / "roles").mkdir()

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 0
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "ansible-lint"
    assert len(call_args) == 3  # ansible-lint, -c, <bundled config>


def test_main_ansible_lint_malformed_local_config_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MINIMAL_TOML)
    (tmp_path / ".ansible-lint.yml").write_text("skip_list: [unterminated\n")

    with (
        patch(
            "vergil_tooling.bin.validate_common.git.repo_root",
            return_value=tmp_path,
        ),
        patch(
            "vergil_tooling.bin.validate_common.repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        ) as mock_run,
    ):
        assert main() == 1
    # We fail before invoking ansible-lint rather than silently dropping skips.
    tool_names = [call[0][0][0] for call in mock_run.call_args_list]
    assert "ansible-lint" not in tool_names
