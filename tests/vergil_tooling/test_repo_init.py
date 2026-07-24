from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib.config import _parse_raw_config
from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    _cd_release_secrets,
    _check_remote_steps,
    _container_suffix,
    _default_ci_versions,
    _load_existing_config,
    _remote_branch_exists,
    _resolve,
    _sync_labels,
    detect_completed_steps,
    prompt_choice,
    prompt_free_text,
    prompt_language,
    prompt_multi_choice,
    prompt_yes_no,
    render_cd_workflow,
    render_ci_workflow,
    render_claude_md,
    render_claude_settings,
    render_epic_rollup_workflow,
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


class TestPromptLanguage:
    # Languages are listed sorted: go, java, python, ruby, rust (so index 3 is python).
    def test_selects_language(self) -> None:
        with patch("builtins.input", return_value="3"):
            assert prompt_language() == "python"

    def test_none_of_the_above_returns_empty(self) -> None:
        with patch("builtins.input", return_value="0"):
            assert prompt_language() == ""

    def test_empty_with_no_default_means_no_language(self) -> None:
        with patch("builtins.input", return_value=""):
            assert prompt_language() == ""

    def test_empty_with_language_default_keeps_default(self) -> None:
        with patch("builtins.input", return_value=""):
            assert prompt_language(default="python") == "python"

    def test_invalid_then_none(self) -> None:
        with patch("builtins.input", side_effect=["9", "abc", "0"]):
            assert prompt_language() == ""


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


class TestResolve:
    def test_override_wins(self) -> None:
        called = False

        def prompt() -> str:
            nonlocal called
            called = True
            return "prompted"

        result = _resolve("flag", non_interactive=False, default="dflt", prompt=prompt)
        assert result == "flag"
        assert called is False

    def test_non_interactive_uses_default(self) -> None:
        result = _resolve(None, non_interactive=True, default="dflt", prompt=lambda: "prompted")
        assert result == "dflt"

    def test_interactive_prompts_when_no_override(self) -> None:
        result = _resolve(None, non_interactive=False, default="dflt", prompt=lambda: "prompted")
        assert result == "prompted"

    def test_false_boolean_override_is_respected(self) -> None:
        # A False override must not be mistaken for "not supplied".
        result = _resolve(False, non_interactive=True, default=True, prompt=lambda: True)
        assert result is False


class TestRepoInitContext:
    def test_construction(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        assert ctx.org == "vergil-project"
        assert ctx.name == "vergil-vm"
        assert ctx.repo == "vergil-project/vergil-vm"
        assert ctx.completed_steps == set()

    def test_default_license_is_mit(self) -> None:
        # Standing decision (2026-06-30): new repos default to MIT, not GPL-3.0.
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        assert ctx.license_type == "MIT"

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
        ctx.primary_language = "python"
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
        assert raw["project"]["primary-language"] == "python"
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

    def test_wraps_consumer_template_in_markers(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        content = render_claude_md(ctx)
        begin = "<!-- vergil:template:claude-md:begin -->"
        end = "<!-- vergil:template:claude-md:end -->"
        assert content.count(begin) == 1
        assert content.count(end) == 1
        assert content.index(begin) < content.index("## Memory management")
        assert content.rstrip().endswith(end)


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

    def test_long_description_reflowed_under_md013(self) -> None:
        # A long one-paragraph description must be wrapped to <=100-char lines so
        # the generated README passes the bundled markdownlint MD013 limit — the
        # first vrg-validate of every new repo lints it (issue #2393).
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = (
            "A resilience and observability toolkit for message queues that "
            "provides retry policies, dead-letter handling, structured tracing, "
            "and Prometheus metrics across RabbitMQ and Kafka backends, with "
            "sensible defaults for production deployments."
        )
        ctx.license_type = "MIT"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert all(len(line) <= 100 for line in content.splitlines())
        # Content is preserved — every word from the description survives the wrap.
        collapsed = " ".join(content.split())
        for word in ctx.description.split():
            assert word in collapsed

    def test_description_paragraphs_preserved(self) -> None:
        # Blank-line-separated paragraphs survive the reflow as separate blocks.
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "First paragraph.\n\nSecond paragraph."
        ctx.license_type = "MIT"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert "First paragraph.\n\nSecond paragraph.\n" in content


class TestRenderGitignore:
    def test_contains_baseline_patterns(self) -> None:
        content = render_gitignore()
        assert ".DS_Store" in content
        assert ".worktrees/" in content
        assert ".venv/" in content
        assert ".venv-host/" not in content

    def test_contains_vergil_workflow_patterns(self) -> None:
        content = render_gitignore()
        assert ".vergil/" in content
        assert "build/" in content
        assert ".superpowers/" in content

    def test_baseline_is_subset_of_flagship_gitignore(self) -> None:
        """Drift guard: every baseline entry must exist in this repo's .gitignore.

        The flagship repo carries repo-specific entries beyond the
        baseline, but the baseline itself must never contain an entry
        the flagship lacks (vergil-tooling#1425).
        """
        flagship = Path(__file__).resolve().parents[2] / ".gitignore"
        flagship_entries = {
            line.strip()
            for line in flagship.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        baseline_entries = [
            line.strip()
            for line in render_gitignore().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        missing = [entry for entry in baseline_entries if entry not in flagship_entries]
        assert not missing, f"baseline entries missing from flagship .gitignore: {missing}"


class TestRenderCiWorkflow:
    def test_python_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "ci-quality.yml@v2.1" in content
        assert "ci-audit.yml@v2.1" in content
        assert "ci-test.yml@v2.1" in content
        assert "container-suffix: python" in content
        assert "ci-version-bump.yml@v2.1" in content
        assert "run-codeql: false" not in content

    def test_no_language_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "container-suffix: base" in content
        assert "run-codeql: false" in content
        assert "ci-audit.yml" not in content
        assert "ci-test.yml" not in content
        assert "ci-quality.yml@v2.1" in content
        assert "ci-security.yml@v2.1" in content
        assert "ci-version-bump.yml@v2.1" in content
        assert content.count("container-suffix: base") == 3
        assert content.count("container-tag: 'latest'") == 3

    def test_no_language_omits_trailing_space(self) -> None:
        """A no-primary-language repo must not scaffold `language: ` with a
        trailing space — yamllint trailing-spaces rejects it (issue #1993)."""
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "language: \n" not in content
        trailing = [line for line in content.splitlines() if line != line.rstrip()]
        assert not trailing, f"lines with trailing whitespace: {trailing!r}"

    def test_no_language_no_release_minimal_jobs(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "ci-quality.yml@v2.1" in content
        assert "ci-security.yml@v2.1" in content
        assert "ci-audit.yml" not in content
        assert "ci-test.yml" not in content
        assert "version-bump" not in content
        assert content.count("container-suffix: base") == 2
        assert content.count("container-tag: 'latest'") == 2

    def test_no_language_skips_audit_and_test(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "run-codeql: false" in content
        assert "ci-audit.yml" not in content
        assert "ci-test.yml" not in content
        assert "ci-quality.yml@v2.1" in content
        assert "ci-security.yml@v2.1" in content

    def test_no_version_bump_when_release_none(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "version-bump" not in content

    def test_integration_tests_emit_test_job_without_language(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        ctx.integration_tests = True
        content = render_ci_workflow(ctx)
        assert "ci-test.yml@v2.1" in content

    def test_private_repo_disables_sarif_upload(self) -> None:
        ctx = RepoInitContext(org="logical-minds-foundry", name="test", visibility="private")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "upload-sarif: false" in content
        assert "actions: read" in content

    def test_public_repo_keeps_sarif_upload(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "upload-sarif: false" not in content
        assert "actions: read" in content

    def test_pins_are_v2_1(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "@v2.0" not in content
        assert "ci-security.yml@v2.1" in content


class TestContainerHelpers:
    def test_container_suffix_none_returns_base(self) -> None:
        assert _container_suffix(None) == "base"

    def test_default_ci_versions_none_returns_latest(self) -> None:
        assert _default_ci_versions(None) == "latest"


class TestRenderCdWorkflow:
    def test_docs_job(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = True
        ctx.publish_release = False
        content = render_cd_workflow(ctx)
        assert "cd-docs.yml@v2.1" in content
        assert "cd-release" not in content
        assert "attestations: write" not in content

    def test_no_docs_job(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        content = render_cd_workflow(ctx)
        assert "cd-docs" not in content

    def test_release_job(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        ctx.publish_release = True
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        content = render_cd_workflow(ctx)
        assert "cd-release.yml@v2.1" in content
        assert "if: github.ref == 'refs/heads/main'" in content
        assert "language: python" in content
        assert 'container-tag: "3.14"' in content
        # python publishes via OIDC trusted publishing — no repo secret, and
        # the blanket `secrets: inherit` must never be emitted (epic .github#189).
        assert "secrets: inherit" not in content
        assert "secrets:" not in content
        assert "attestations: write" in content
        assert "id-token: write" in content
        assert "pull-requests: write" in content
        # cd-release.yml@v2.1 requests actions:read; without it the reusable
        # workflow startup-fails at parse time on every CD run (issue #2392).
        assert "actions: read" in content
        assert "cd-docs" not in content

    def test_release_omits_actions_read_when_no_release(self) -> None:
        # actions:read is only needed by the release job; a docs-only CD must
        # not carry it (no cd-release call to satisfy).
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = True
        ctx.publish_release = False
        content = render_cd_workflow(ctx)
        assert "actions: read" not in content

    def test_docs_and_release(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = True
        ctx.publish_release = True
        ctx.primary_language = "go"
        ctx.ci_versions = ["1.22"]
        content = render_cd_workflow(ctx)
        assert "cd-docs.yml@v2.1" in content
        assert "cd-release.yml@v2.1" in content
        assert "language: go" in content
        assert 'container-tag: "latest"' in content
        assert "attestations: write" in content
        # go needs no publishing token — no secrets block, never blanket inherit.
        assert "secrets: inherit" not in content
        assert "secrets:" not in content

    def test_release_rust_explicit_secrets(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        ctx.publish_release = True
        ctx.primary_language = "rust"
        content = render_cd_workflow(ctx)
        assert "secrets: inherit" not in content
        assert "    secrets:\n" in content
        assert "      CARGO_REGISTRY_TOKEN: ${{ secrets.CARGO_REGISTRY_TOKEN }}\n" in content

    def test_release_ruby_explicit_secrets(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        ctx.publish_release = True
        ctx.primary_language = "ruby"
        content = render_cd_workflow(ctx)
        assert "secrets: inherit" not in content
        assert "    secrets:\n" in content
        assert "      RUBYGEMS_API_KEY: ${{ secrets.RUBYGEMS_API_KEY }}\n" in content

    def test_release_java_explicit_secrets(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        ctx.publish_release = True
        ctx.primary_language = "java"
        content = render_cd_workflow(ctx)
        assert "secrets: inherit" not in content
        assert "    secrets:\n" in content
        assert "      CENTRAL_USERNAME: ${{ secrets.CENTRAL_USERNAME }}\n" in content
        assert "      CENTRAL_TOKEN: ${{ secrets.CENTRAL_TOKEN }}\n" in content
        assert "      GPG_PRIVATE_KEY: ${{ secrets.GPG_PRIVATE_KEY }}\n" in content
        assert "      GPG_PASSPHRASE: ${{ secrets.GPG_PASSPHRASE }}\n" in content

    def test_cd_release_secrets_helper(self) -> None:
        # python/go/unlisted → no secrets; publishers → their exact secret set.
        assert _cd_release_secrets("python") == []
        assert _cd_release_secrets("go") == []
        assert _cd_release_secrets(None) == []
        assert _cd_release_secrets("elixir") == []
        assert _cd_release_secrets("rust") == ["CARGO_REGISTRY_TOKEN"]
        assert _cd_release_secrets("ruby") == ["RUBYGEMS_API_KEY"]
        assert _cd_release_secrets("java") == [
            "CENTRAL_USERNAME",
            "CENTRAL_TOKEN",
            "GPG_PRIVATE_KEY",
            "GPG_PASSPHRASE",
        ]


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

    def test_non_interactive_skips_clone_confirmation(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.non_interactive = True
        target = tmp_path / "vergil-vm"

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch(
                "vergil_tooling.lib.repo_init.prompt_yes_no",
                side_effect=AssertionError("prompted in non-interactive mode"),
            ),
            patch("vergil_tooling.lib.repo_init.os.chdir"),
        ):
            step_clone(ctx, parent_dir=tmp_path)

        assert ctx.work_dir == target


class TestStepGenerateConfig:
    def test_prompts_and_writes_vergil_toml(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        # Indices are alphabetical within each sorted enum:
        # repository-type: 5=tooling, primary-language: 3=python,
        # branching-model: 3=library-release, versioning-scheme: 4=semver,
        # release-model: 4=tagged-release
        inputs = iter(
            [
                "5",  # repository-type: tooling
                "3",  # primary-language: python
                "3",  # branching-model: library-release
                "4",  # versioning-scheme: semver
                "4",  # release-model: tagged-release
                "latest",  # ci versions
                "n",  # integration tests
                "y",  # publish releases
                "y",  # publish docs
                "",  # vergil version (default v2.0)
                "1",  # license: MIT (option 1)
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
        assert 'primary-language = "python"' in content

        assert any("commit" in c for c in calls)

    def test_none_of_the_above_omits_primary_language(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="dot-github")
        ctx.work_dir = tmp_path

        inputs = iter(
            [
                "2",  # repository-type: documentation
                "0",  # primary-language: none of the above
                "3",  # branching-model: docs-single-branch
                "2",  # versioning-scheme: none
                "3",  # release-model: none
                "latest",  # ci versions
                "n",  # integration tests
                "n",  # publish releases
                "y",  # publish docs
                "",  # vergil version
                "1",  # license: MIT (option 1)
                "",  # initial version
            ]
        )

        with (
            patch("builtins.input", side_effect=lambda _="": next(inputs)),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert ctx.primary_language == ""
        content = (tmp_path / "vergil.toml").read_text()
        assert "primary-language" not in content
        # Round-trips through config validation as a language-less repo.
        assert _parse_raw_config(tomllib.loads(content)).project.primary_language is None

    def test_adopt_language_less_toml_round_trips(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="dot-github", adopt=True)
        ctx.work_dir = tmp_path

        existing = (
            "[project]\n"
            'repository-type = "documentation"\n'
            'versioning-scheme = "none"\n'
            'branching-model = "docs-single-branch"\n'
            'release-model = "none"\n'
            "\n"
            "[ci]\n"
            'versions = ["latest"]\n'
            "integration-tests = false\n"
            "\n"
            "[publish]\n"
            "release = false\n"
            "docs = true\n"
            "\n"
            "[dependencies]\n"
            'vergil = "v2.1"\n'
        )
        (tmp_path / "vergil.toml").write_text(existing)

        # All defaults accepted (empty input); the language default is "no language".
        inputs = iter([""] * 12)

        with (
            patch("builtins.input", side_effect=lambda _="": next(inputs)),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert ctx.primary_language == ""
        content = (tmp_path / "vergil.toml").read_text()
        assert "primary-language" not in content

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
            'primary-language = "python"\n'
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

        assert ctx.primary_language == "python"
        assert ctx.repository_type == "tooling"

    def test_non_interactive_resolves_flags_without_prompting(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.non_interactive = True
        ctx.opt_repository_type = "tooling"
        ctx.opt_primary_language = "python"
        ctx.opt_branching_model = "library-release"
        ctx.opt_versioning_scheme = "semver"
        ctx.opt_release_model = "tagged-release"
        ctx.opt_ci_versions = "3.12, 3.13"
        ctx.opt_integration_tests = False
        ctx.opt_publish_release = True
        ctx.opt_publish_docs = False
        ctx.opt_vergil_version = "v2.1"
        ctx.opt_license_type = "Apache-2.0"
        ctx.opt_initial_version = "1.0.0"

        with (
            patch(
                "builtins.input",
                side_effect=AssertionError("prompted in non-interactive mode"),
            ),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert ctx.repository_type == "tooling"
        assert ctx.primary_language == "python"
        assert ctx.branching_model == "library-release"
        assert ctx.versioning_scheme == "semver"
        assert ctx.release_model == "tagged-release"
        assert ctx.ci_versions == ["3.12", "3.13"]
        assert ctx.integration_tests is False
        assert ctx.publish_release is True
        assert ctx.publish_docs is False
        assert ctx.license_type == "Apache-2.0"
        assert ctx.initial_version == "1.0.0"
        content = (tmp_path / "vergil.toml").read_text()
        assert 'primary-language = "python"' in content
        assert _parse_raw_config(tomllib.loads(content)).ci.versions == ["3.12", "3.13"]

    def test_non_interactive_uses_documented_defaults_for_optional_flags(
        self, tmp_path: Path
    ) -> None:
        # Required enums supplied; every optional flag omitted falls back to its
        # documented default (matching the interactive default exactly).
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.non_interactive = True
        ctx.opt_repository_type = "tooling"
        ctx.opt_branching_model = "library-release"
        ctx.opt_versioning_scheme = "semver"
        ctx.opt_release_model = "tagged-release"

        with (
            patch(
                "builtins.input",
                side_effect=AssertionError("prompted in non-interactive mode"),
            ),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert ctx.primary_language == ""  # no --language → language-less
        assert ctx.integration_tests is False
        assert ctx.publish_release is True  # release-model != none
        assert ctx.publish_docs is True
        assert ctx.vergil_version == "v2.1"
        assert ctx.license_type == "MIT"
        assert ctx.initial_version == "0.1.0"

    def test_non_interactive_adopt_missing_required_enum_fails_loud(self, tmp_path: Path) -> None:
        # Adopt with no existing vergil.toml and no flags: the required enums
        # resolve empty, so the wizard must fail loud rather than write an
        # invalid config (issue #2382, no silent failure).
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm", adopt=True)
        ctx.work_dir = tmp_path
        ctx.non_interactive = True

        with (
            patch(
                "builtins.input",
                side_effect=AssertionError("prompted in non-interactive mode"),
            ),
            patch("vergil_tooling.lib.repo_init.git.run"),
            pytest.raises(SystemExit) as exc,
        ):
            step_generate_config(ctx)

        message = str(exc.value)
        assert "--repository-type" in message
        assert "--branching-model" in message
        assert not (tmp_path / "vergil.toml").exists()


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
        assert (tmp_path / ".claude" / "hooks" / "guard.sh").exists()
        assert (tmp_path / ".claude" / "hooks" / "guard.sh").stat().st_mode & 0o111
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

    def test_no_githooks_created(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.publish_docs = True

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        assert not (tmp_path / ".githooks").exists()


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

    def test_creates_cd_for_release_only(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.work_dir = tmp_path
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.14"]
        ctx.release_model = "tagged-release"
        ctx.publish_docs = False
        ctx.publish_release = True

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_ci_cd_workflows(ctx)

        assert (tmp_path / ".github" / "workflows" / "cd.yml").exists()
        content = (tmp_path / ".github" / "workflows" / "cd.yml").read_text()
        assert "cd-release.yml@v2.1" in content
        assert "cd-docs" not in content

    def test_skips_cd_when_no_docs_or_release(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.work_dir = tmp_path
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        ctx.publish_docs = False
        ctx.publish_release = False

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_ci_cd_workflows(ctx)

        assert (tmp_path / ".github" / "workflows" / "ci.yml").exists()
        assert not (tmp_path / ".github" / "workflows" / "cd.yml").exists()
        # Event-driven rollup ships in every repo, even without docs/release.
        assert (tmp_path / ".github" / "workflows" / "epic-rollup.yml").exists()


class TestRenderEpicRollupWorkflow:
    def test_is_a_thin_caller_to_the_reusable_workflow(self) -> None:
        content = render_epic_rollup_workflow()
        assert "on:\n  issues:\n    types: [closed]" in content
        assert "ops-epic-rollup.yml@v2.1" in content
        assert "APP_CLIENT_ID: ${{ secrets.APP_CLIENT_ID }}" in content
        assert "APP_PRIVATE_KEY: ${{ secrets.APP_PRIVATE_KEY }}" in content


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


def test_render_claude_settings_seeds_main() -> None:
    text = render_claude_settings()
    data = json.loads(text)
    src = data["extraKnownMarketplaces"]["vergil-marketplace"]["source"]
    assert src["ref"] == "main"
    assert text.endswith("\n")


def test_multi_choice_numbers() -> None:
    with patch("builtins.input", return_value="1,3"):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [0, 2]


def test_multi_choice_all() -> None:
    with patch("builtins.input", return_value="all"):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [0, 1, 2]


def test_multi_choice_space_separated_and_dedup_sorted() -> None:
    with patch("builtins.input", return_value="3 1 1"):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [0, 2]


def test_multi_choice_reprompts_on_out_of_range(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("builtins.input", side_effect=["9", "2"]):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [1]
    assert "between 1 and 3" in capsys.readouterr().out


def test_multi_choice_reprompts_on_non_integer(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("builtins.input", side_effect=["x", "2"]):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [1]
    assert "between 1 and 3" in capsys.readouterr().out
