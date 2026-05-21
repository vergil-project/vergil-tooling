from __future__ import annotations

import subprocess
import tomllib
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib.config import _parse_raw_config
from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    _check_remote_steps,
    _load_existing_config,
    _remote_branch_exists,
    _sync_labels,
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
    run_wizard,
    step_branch_structure,
    step_ci_cd_workflows,
    step_clone,
    step_create_repo,
    step_docs_site,
    step_generate_config,
    step_github_config,
    step_github_pages,
    step_scaffold_config_files,
)

if TYPE_CHECKING:
    from pathlib import Path


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

    def test_invalid_then_valid(self) -> None:
        with patch("builtins.input", side_effect=["maybe", "y"]):
            assert prompt_yes_no("Continue?") is True


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

    def test_not_required_returns_empty(self) -> None:
        with patch("builtins.input", return_value=""):
            result = prompt_free_text("Optional", required=False)
        assert result == ""


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
        assert "## Table of Contents" in content

    def test_pages_url(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "test"
        ctx.license_type = "MIT"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert "vergil-project.github.io/vergil-vm" in content

    def test_no_docs_local_fallback(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "test"
        ctx.license_type = "MIT"
        ctx.publish_docs = False
        content = render_readme(ctx)
        assert "`docs/` directory" in content

    def test_license_none(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "test"
        ctx.license_type = "none"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert "See [LICENSE](LICENSE).\n" in content
        assert "GPL" not in content


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
        assert "run-codeql: false" not in content

    def test_shell_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "shell"
        ctx.ci_versions = ["latest"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "container-suffix: base" in content
        assert "run-codeql: false" in content
        assert content.count("container-suffix: base") == 5
        assert content.count("container-tag: 'latest'") == 5

    def test_codeql_disabled_for_claude_plugin(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "claude-plugin"
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "run-codeql: false" in content

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

    def test_no_docs_job(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        content = render_cd_workflow(ctx)
        assert "cd-docs" not in content


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

    def test_skips_when_repo_exists_nonadopt(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
            visibility="public",
        )

        with patch(
            "vergil_tooling.lib.repo_init.github.read_output",
            return_value='{"name":"vergil-vm"}',
        ):
            step_create_repo(ctx)

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

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("vergil_tooling.lib.repo_init.prompt_yes_no", return_value=True),
            patch("vergil_tooling.lib.repo_init.os.chdir"),
        ):
            step_clone(ctx, parent_dir=tmp_path)

        assert ctx.work_dir == target

    def test_skips_when_already_cloned(self, tmp_path: Path) -> None:
        target = tmp_path / "vergil-vm"
        target.mkdir()
        (target / ".git").mkdir()
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        with patch("vergil_tooling.lib.repo_init.os.chdir"):
            step_clone(ctx, parent_dir=tmp_path)
        assert ctx.work_dir == target

    def test_adopt_uses_cwd(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm", adopt=True)

        step_clone(ctx)
        assert ctx.work_dir is not None

    def test_default_parent_dir(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        target = tmp_path / "vergil-vm"

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("vergil_tooling.lib.repo_init.Path.cwd", return_value=tmp_path),
            patch("vergil_tooling.lib.repo_init.prompt_yes_no", return_value=True),
            patch("vergil_tooling.lib.repo_init.os.chdir"),
        ):
            step_clone(ctx)

        assert ctx.work_dir == target

    def test_clone_changes_cwd(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        target = tmp_path / "vergil-vm"

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("vergil_tooling.lib.repo_init.prompt_yes_no", return_value=True),
            patch("vergil_tooling.lib.repo_init.os.chdir") as mock_chdir,
        ):
            step_clone(ctx, parent_dir=tmp_path)

        mock_chdir.assert_called_once_with(target)

    def test_already_cloned_changes_cwd(self, tmp_path: Path) -> None:
        target = tmp_path / "vergil-vm"
        target.mkdir()
        (target / ".git").mkdir()
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        with patch("vergil_tooling.lib.repo_init.os.chdir") as mock_chdir:
            step_clone(ctx, parent_dir=tmp_path)

        mock_chdir.assert_called_once_with(target)

    def test_clone_aborted_by_user(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        with (
            patch("vergil_tooling.lib.repo_init.prompt_yes_no", return_value=False),
            pytest.raises(SystemExit),
        ):
            step_clone(ctx, parent_dir=tmp_path)


class TestStepGenerateConfig:
    def test_prompts_and_writes_vergil_toml(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        # Indices are alphabetical within each sorted enum:
        # repository-type: 5=tooling, primary-language: 8=shell,
        # branching-model: 3=library-release, versioning-scheme: 4=semver,
        # release-model: 4=tagged-release
        inputs = iter(
            [
                "5",  # repository-type: tooling
                "8",  # primary-language: shell
                "3",  # branching-model: library-release
                "4",  # versioning-scheme: semver
                "4",  # release-model: tagged-release
                "latest",  # ci versions
                "n",  # integration tests
                "y",  # publish releases
                "y",  # publish docs
                "",  # vergil version (default v2.0)
                "1",  # license: GPL-3.0
                "",  # initial version (default 0.1.0)
            ]
        )

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
        inputs = iter([""] * 12)

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


class TestStepScaffoldConfigFiles:
    def test_creates_all_files(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test repo"
        ctx.license_type = "MIT"
        ctx.publish_docs = True

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)

        with patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run):
            step_scaffold_config_files(ctx)

        assert (tmp_path / "VERSION").exists()
        assert (tmp_path / "VERSION").read_text() == "0.1.0\n"
        assert (tmp_path / ".githooks" / "pre-commit").exists()
        assert (tmp_path / ".githooks" / "pre-commit").stat().st_mode & 0o111
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / ".claude" / "settings.json").exists()
        assert (tmp_path / "LICENSE").exists()
        assert (tmp_path / "README.md").exists()
        assert (tmp_path / ".gitignore").exists()

        assert any("commit" in c for c in calls)

    def test_skips_license_when_none(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.publish_docs = True

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        assert not (tmp_path / "LICENSE").exists()

    def test_hooks_path_set(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.publish_docs = True

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)

        with patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run):
            step_scaffold_config_files(ctx)

        assert ("config", "core.hooksPath", ".githooks") in calls


class TestStepCiCdWorkflows:
    def test_creates_ci_yml(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.work_dir = tmp_path
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        ctx.release_model = "tagged-release"
        ctx.publish_docs = True
        ctx.publish_release = False

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_ci_cd_workflows(ctx)

        assert (tmp_path / ".github" / "workflows" / "ci.yml").exists()
        assert (tmp_path / ".github" / "workflows" / "cd.yml").exists()

    def test_skips_cd_when_no_docs(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.work_dir = tmp_path
        ctx.primary_language = "shell"
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        ctx.publish_docs = False
        ctx.publish_release = False

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_ci_cd_workflows(ctx)

        assert (tmp_path / ".github" / "workflows" / "ci.yml").exists()
        assert not (tmp_path / ".github" / "workflows" / "cd.yml").exists()


class TestStepDocsSite:
    def test_creates_docs_skeleton(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.publish_docs = True

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_docs_site(ctx)

        assert (tmp_path / "docs" / "site" / "mkdocs.yml").exists()
        assert (tmp_path / "docs" / "site" / "docs" / "index.md").exists()

    def test_skips_when_docs_disabled(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.work_dir = tmp_path
        ctx.publish_docs = False

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_docs_site(ctx)

        assert not (tmp_path / "docs" / "site" / "mkdocs.yml").exists()


class TestStepBranchStructure:
    def test_pushes_develop_and_creates_main(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)

        with (
            patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run),
            patch("vergil_tooling.lib.repo_init.github.run"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
        ):
            step_branch_structure(ctx)

        assert any("push" in c for c in calls)


class TestStepGithubConfig:
    def test_applies_config_and_labels(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        config_applied: list[bool] = []
        labels_synced: list[str] = []

        def mock_apply(*a: Any, **kw: Any) -> list[str]:
            config_applied.append(True)
            return []

        def mock_sync(repo: str) -> None:
            labels_synced.append(repo)

        with (
            patch("vergil_tooling.lib.github_config.fetch_actual_state") as mock_fetch,
            patch("vergil_tooling.lib.github_config.compute_desired_state"),
            patch("vergil_tooling.lib.github_config.apply_desired_state", side_effect=mock_apply),
            patch("vergil_tooling.lib.repo_init._sync_labels", side_effect=mock_sync),
            patch("vergil_tooling.lib.config.read_config"),
        ):
            mock_fetch.return_value.owner_type = "Organization"
            mock_fetch.return_value.visibility = "public"
            step_github_config(ctx)

        assert config_applied
        assert ctx.repo in labels_synced


class TestStepGithubPages:
    def test_creates_gh_pages_branch_and_configures(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = True

        write_json_calls: list[tuple[str, str, dict[str, object]]] = []

        def mock_write_json(method: str, endpoint: str, body: dict[str, object]) -> None:
            write_json_calls.append((method, endpoint, body))

        with (
            patch("vergil_tooling.lib.repo_init.github.run"),
            patch("vergil_tooling.lib.repo_init.github.write_json", side_effect=mock_write_json),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_github_pages(ctx)

        assert any("repos/vergil-project/vergil-vm/pages" in c[1] for c in write_json_calls)

    def test_skips_when_docs_disabled(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = False

        step_github_pages(ctx)


class TestLoadExistingConfig:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        result = _load_existing_config(tmp_path)
        assert result is None

    def test_returns_parsed_toml(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text('[project]\nrepository-type = "tooling"\n')
        result = _load_existing_config(tmp_path)
        assert result is not None
        assert result["project"]["repository-type"] == "tooling"


class TestRemoteBranchExists:
    def test_returns_true_when_exists(self) -> None:
        with patch(
            "vergil_tooling.lib.repo_init.github.read_output",
            return_value="develop",
        ):
            assert _remote_branch_exists("org/repo", "develop") is True

    def test_returns_false_when_missing(self) -> None:
        with patch(
            "vergil_tooling.lib.repo_init.github.read_output",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            assert _remote_branch_exists("org/repo", "develop") is False


class TestSyncLabels:
    def test_provisions_labels(self) -> None:
        mock_registry = {
            "labels": [
                {"name": "bug", "color": "d73a4a", "description": "Something is broken"},
                {"name": "docs"},
            ]
        }

        calls: list[tuple[str, ...]] = []

        def mock_gh_run(*args: str) -> None:
            calls.append(args)

        with (
            patch("vergil_tooling.lib.repo_init.github.run", side_effect=mock_gh_run),
            patch("vergil_tooling.lib.labels.load_labels", return_value=mock_registry),
        ):
            _sync_labels("org/repo")

        assert len(calls) == 2
        assert any("bug" in c for c in calls)
        assert any("--color" in c for c in calls)


class TestCheckRemoteSteps:
    def test_repo_and_branches_exist(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")

        with (
            patch("vergil_tooling.lib.repo_init.github.read_output", return_value="test"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=True),
        ):
            result = _check_remote_steps(ctx)

        assert result == {1, 7}

    def test_nothing_exists(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")

        with (
            patch(
                "vergil_tooling.lib.repo_init.github.read_output",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
        ):
            result = _check_remote_steps(ctx)

        assert result == set()

    def test_repo_exists_no_branches(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")

        with (
            patch("vergil_tooling.lib.repo_init.github.read_output", return_value="test"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
        ):
            result = _check_remote_steps(ctx)

        assert result == {1}


class TestStepBranchStructureExtended:
    def test_rename_fails_creates_branch(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)
            if args == ("branch", "-m", "main", "develop"):
                raise subprocess.CalledProcessError(1, "git")

        with (
            patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run),
            patch("vergil_tooling.lib.repo_init.github.run"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
        ):
            step_branch_structure(ctx)

        assert ("checkout", "-b", "develop") in calls

    def test_both_branches_exist(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        with (
            patch("vergil_tooling.lib.repo_init.github.run"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=True),
        ):
            step_branch_structure(ctx)


class TestStepGithubConfigExtended:
    def test_legacy_protection_removed(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        def mock_apply(*a: Any, **kw: Any) -> list[str]:
            return ["develop", "main"]

        with (
            patch("vergil_tooling.lib.github_config.fetch_actual_state") as mock_fetch,
            patch("vergil_tooling.lib.github_config.compute_desired_state"),
            patch("vergil_tooling.lib.github_config.apply_desired_state", side_effect=mock_apply),
            patch("vergil_tooling.lib.repo_init._sync_labels"),
            patch("vergil_tooling.lib.config.read_config"),
        ):
            mock_fetch.return_value.owner_type = "Organization"
            mock_fetch.return_value.visibility = "public"
            step_github_config(ctx)


class TestStepGithubPagesExtended:
    def test_skips_branch_creation_when_exists(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = True

        with (
            patch(
                "vergil_tooling.lib.repo_init.github.write_json",
            ),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=True),
        ):
            step_github_pages(ctx)


class TestRunWizard:
    def test_skips_completed_local_steps(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.completed_steps = {3, 4}

        steps_run: list[int] = []

        def mock_step(step_num: int) -> Any:
            def inner(*a: Any, **kw: Any) -> None:
                steps_run.append(step_num)

            return inner

        with (
            patch("vergil_tooling.lib.repo_init.step_create_repo", side_effect=mock_step(1)),
            patch("vergil_tooling.lib.repo_init.step_clone", side_effect=mock_step(2)),
            patch("vergil_tooling.lib.repo_init.step_generate_config", side_effect=mock_step(3)),
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_config_files",
                side_effect=mock_step(4),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(5)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                return_value=(
                    "abc1234 chore(init): step 3 - vergil.toml\n"
                    "def5678 chore(init): step 4 - config files\n"
                ),
            ),
        ):
            run_wizard(ctx)

        assert 3 not in steps_run
        assert 4 not in steps_run
        assert 5 in steps_run

    def test_handles_git_log_failure(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")

        steps_run: list[int] = []

        def mock_step(step_num: int) -> Any:
            def inner(*a: Any, **kw: Any) -> None:
                steps_run.append(step_num)

            return inner

        with (
            patch("vergil_tooling.lib.repo_init.step_create_repo", side_effect=mock_step(1)),
            patch("vergil_tooling.lib.repo_init.step_clone", side_effect=mock_step(2)),
            patch("vergil_tooling.lib.repo_init.step_generate_config", side_effect=mock_step(3)),
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_config_files",
                side_effect=mock_step(4),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(5)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
        ):
            run_wizard(ctx)

        assert 1 in steps_run

    def test_no_docs_skips_url(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False

        def mock_step(_step_num: int) -> Any:
            def inner(*a: Any, **kw: Any) -> None:
                pass

            return inner

        with (
            patch("vergil_tooling.lib.repo_init.step_create_repo", side_effect=mock_step(1)),
            patch("vergil_tooling.lib.repo_init.step_clone", side_effect=mock_step(2)),
            patch("vergil_tooling.lib.repo_init.step_generate_config", side_effect=mock_step(3)),
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_config_files",
                side_effect=mock_step(4),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(5)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
        ):
            run_wizard(ctx)
