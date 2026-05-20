from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import tomllib

from vergil_tooling.lib.config import _parse_raw_config
from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    detect_completed_steps,
    prompt_choice,
    prompt_free_text,
    prompt_yes_no,
    render_cd_workflow,
    render_ci_workflow,
    render_claude_md,
    render_gitignore,
    render_mkdocs_yml,
    step_clone,
    step_create_repo,
    step_generate_config,
    render_readme,
    render_vergil_toml,
)


class TestPromptChoice:
    def test_valid_selection(self) -> None:
        options = ["alpha", "beta", "gamma"]
        with patch("builtins.input", return_value="2"):
            result = prompt_choice("Pick one", options)
        assert result == "beta"

    def test_default_on_empty_input(self) -> None:
        options = ["alpha", "beta", "gamma"]
        with patch("builtins.input", return_value=""):
            result = prompt_choice("Pick one", options, default="beta")
        assert result == "beta"

    def test_invalid_then_valid(self) -> None:
        options = ["alpha", "beta"]
        with patch("builtins.input", side_effect=["0", "abc", "1"]):
            result = prompt_choice("Pick one", options)
        assert result == "alpha"


class TestPromptYesNo:
    def test_yes(self) -> None:
        with patch("builtins.input", return_value="y"):
            assert prompt_yes_no("Continue?") is True

    def test_no(self) -> None:
        with patch("builtins.input", return_value="n"):
            assert prompt_yes_no("Continue?") is False

    def test_default_yes(self) -> None:
        with patch("builtins.input", return_value=""):
            assert prompt_yes_no("Continue?", default=True) is True

    def test_default_no(self) -> None:
        with patch("builtins.input", return_value=""):
            assert prompt_yes_no("Continue?", default=False) is False


class TestPromptFreeText:
    def test_returns_input(self) -> None:
        with patch("builtins.input", return_value="hello world"):
            result = prompt_free_text("Enter text")
        assert result == "hello world"

    def test_default_on_empty(self) -> None:
        with patch("builtins.input", return_value=""):
            result = prompt_free_text("Enter text", default="fallback")
        assert result == "fallback"

    def test_required_retries(self) -> None:
        with patch("builtins.input", side_effect=["", "", "got it"]):
            result = prompt_free_text("Enter text")
        assert result == "got it"


class TestRepoInitContext:
    def test_construction(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        assert ctx.org == "vergil-project"
        assert ctx.name == "vergil-vm"
        assert ctx.repo == "vergil-project/vergil-vm"
        assert ctx.completed_steps == set()

    def test_adopt_mode(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
            adopt=True,
        )
        assert ctx.adopt is True


class TestDetectCompletedSteps:
    def test_no_commits_returns_empty(self) -> None:
        log_output = ""
        result = detect_completed_steps(log_output)
        assert result == set()

    def test_parses_checkpoint_markers(self) -> None:
        log_output = (
            "abc1234 chore(init): step 3 - vergil.toml\n"
            "def5678 chore(init): step 4 - config files\n"
        )
        result = detect_completed_steps(log_output)
        assert result == {3, 4}

    def test_ignores_non_marker_commits(self) -> None:
        log_output = (
            "abc1234 feat: something else\n"
            "def5678 chore(init): step 3 - vergil.toml\n"
            "ghi9012 docs: readme\n"
        )
        result = detect_completed_steps(log_output)
        assert result == {3}


class TestRenderVergilToml:
    def test_output_is_valid_toml(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test-repo")
        ctx.repository_type = "tooling"
        ctx.primary_language = "shell"
        ctx.branching_model = "library-release"
        ctx.versioning_scheme = "semver"
        ctx.release_model = "tagged-release"
        ctx.ci_versions = ["latest"]
        ctx.integration_tests = False
        ctx.publish_release = True
        ctx.publish_docs = True
        ctx.vergil_version = "v2.0"

        content = render_vergil_toml(ctx)
        raw = tomllib.loads(content)
        assert raw["project"]["repository-type"] == "tooling"
        assert raw["project"]["primary-language"] == "shell"
        assert raw["dependencies"]["vergil"] == "v2.0"

    def test_passes_config_validation(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test-repo")
        ctx.repository_type = "tooling"
        ctx.primary_language = "python"
        ctx.branching_model = "library-release"
        ctx.versioning_scheme = "semver"
        ctx.release_model = "tagged-release"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        ctx.integration_tests = False
        ctx.publish_release = True
        ctx.publish_docs = True
        ctx.vergil_version = "v2.0"

        content = render_vergil_toml(ctx)
        raw = tomllib.loads(content)
        config = _parse_raw_config(raw)
        assert config.project.primary_language == "python"
        assert config.ci.versions == ["3.12", "3.13", "3.14"]


class TestRenderClaudeMd:
    def test_contains_project_name(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        content = render_claude_md(ctx)
        assert "# CLAUDE.md" in content
        assert "vergil-vm" in content

    def test_contains_consumer_template(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        content = render_claude_md(ctx)
        assert "## Memory management" in content
        assert "## Shell command policy" in content
        assert "## Validation" in content


class TestRenderReadme:
    def test_contains_project_name(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "Lima VM image definitions"
        ctx.license_type = "GPL-3.0"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert "# vergil-vm" in content
        assert "Lima VM image definitions" in content
        assert "GPL-3.0" in content

    def test_pages_url(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "test"
        ctx.license_type = "MIT"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert "vergil-project.github.io/vergil-vm" in content


class TestRenderGitignore:
    def test_contains_baseline_patterns(self) -> None:
        content = render_gitignore()
        assert ".DS_Store" in content
        assert ".worktrees/" in content
        assert ".venv-host/" in content


class TestRenderCiWorkflow:
    def test_python_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "ci-quality.yml@v2.0" in content
        assert "container-suffix: python" in content
        assert "ci-version-bump.yml@v2.0" in content

    def test_shell_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "shell"
        ctx.ci_versions = ["latest"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "container-suffix: base" in content

    def test_no_version_bump_when_release_none(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "shell"
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "version-bump" not in content


class TestRenderCdWorkflow:
    def test_docs_job(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = True
        ctx.publish_release = False
        content = render_cd_workflow(ctx)
        assert "cd-docs.yml@v2.0" in content
        assert "cd-release" not in content


class TestRenderMkdocsYml:
    def test_contains_site_name(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        content = render_mkdocs_yml(ctx)
        assert 'site_name: "vergil-vm"' in content
        assert "material" in content


class TestStepCreateRepo:
    def test_creates_new_repo(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
            visibility="public",
            description="Test repo",
        )
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str) -> None:
            calls.append(args)

        with (
            patch(
                "vergil_tooling.lib.repo_init.github.read_output",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
            patch("vergil_tooling.lib.repo_init.github.run", side_effect=mock_run),
        ):
            step_create_repo(ctx)

        assert any("repo" in c and "create" in c for c in calls)

    def test_skips_when_repo_exists_adopt(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
            adopt=True,
        )

        with patch(
            "vergil_tooling.lib.repo_init.github.read_output",
            return_value="vergil-project/vergil-vm",
        ):
            step_create_repo(ctx)


class TestStepClone:
    def test_clones_to_work_dir(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        target = tmp_path / "vergil-vm"

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with patch("vergil_tooling.lib.repo_init.subprocess.run", side_effect=mock_subprocess_run):
            step_clone(ctx, parent_dir=tmp_path)

        assert ctx.work_dir == target

    def test_skips_when_already_cloned(self, tmp_path: Path) -> None:
        target = tmp_path / "vergil-vm"
        target.mkdir()
        (target / ".git").mkdir()
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        step_clone(ctx, parent_dir=tmp_path)
        assert ctx.work_dir == target


class TestStepGenerateConfig:
    def test_prompts_and_writes_vergil_toml(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        # Indices are alphabetical within each sorted enum:
        # repository-type: 5=tooling, primary-language: 8=shell,
        # branching-model: 3=library-release, versioning-scheme: 4=semver,
        # release-model: 4=tagged-release
        inputs = iter([
            "5",       # repository-type: tooling
            "8",       # primary-language: shell
            "3",       # branching-model: library-release
            "4",       # versioning-scheme: semver
            "4",       # release-model: tagged-release
            "latest",  # ci versions
            "n",       # integration tests
            "y",       # publish releases
            "y",       # publish docs
            "",        # vergil version (default v2.0)
            "1",       # license: GPL-3.0
        ])

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)

        with (
            patch("builtins.input", side_effect=lambda _="": next(inputs)),
            patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run),
        ):
            step_generate_config(ctx)

        toml_path = tmp_path / "vergil.toml"
        assert toml_path.exists()
        content = toml_path.read_text()
        assert 'primary-language = "shell"' in content

        assert any("commit" in c for c in calls)

    def test_prefills_from_existing_toml_in_adopt_mode(
        self,
        tmp_path: Path,
    ) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
            adopt=True,
        )
        ctx.work_dir = tmp_path

        existing = (
            "[project]\n"
            'repository-type = "tooling"\n'
            'versioning-scheme = "semver"\n'
            'branching-model = "library-release"\n'
            'release-model = "tagged-release"\n'
            'primary-language = "shell"\n'
            "\n"
            "[ci]\n"
            'versions = ["latest"]\n'
            "integration-tests = false\n"
            "\n"
            "[publish]\n"
            "release = true\n"
            "docs = true\n"
            "\n"
            "[dependencies]\n"
            'vergil = "v2.0"\n'
        )
        (tmp_path / "vergil.toml").write_text(existing)

        # All defaults accepted (empty input)
        inputs = iter([""] * 11)

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)

        with (
            patch("builtins.input", side_effect=lambda _="": next(inputs)),
            patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run),
        ):
            step_generate_config(ctx)

        assert ctx.primary_language == "shell"
        assert ctx.repository_type == "tooling"
