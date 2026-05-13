"""Tests for vergil_tooling.bin.validate_common."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.validate_common import (
    _find_dockerfiles,
    _find_markdown_files,
    _find_shell_files,
    _find_yaml_files,
    main,
)

if TYPE_CHECKING:
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
vergil-tooling = "v2.0"

[ci]
versions = ["3.14"]
"""


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


def test_find_markdown_files_both(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "index.md").write_text("# Hello\n")
    (tmp_path / "README.md").write_text("# Project\n")
    result = _find_markdown_files(tmp_path)
    assert len(result) == 2


def test_find_markdown_files_sorted(tmp_path: Path) -> None:
    site = tmp_path / "docs" / "site"
    site.mkdir(parents=True)
    (site / "b.md").write_text("# B\n")
    (site / "a.md").write_text("# A\n")
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
    for skip in (".worktrees", ".venv", ".venv-host", "node_modules"):
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        assert main() == 1


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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
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
            "vergil_tooling.bin.validate_common.vrg_repo_profile.main",
            return_value=0,
        ),
        patch(
            "vergil_tooling.bin.validate_common.subprocess.run",
            side_effect=mock_run,
        ),
    ):
        assert main() == 1
