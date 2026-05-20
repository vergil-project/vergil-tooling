from __future__ import annotations

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
