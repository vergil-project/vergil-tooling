from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
import yaml

from vergil_tooling.lib import (
    changelog,
    dependabot,
    github,
    gitignore,
    repo_config,
    repo_init,
    typescript_tools,
    version,
)
from vergil_tooling.lib.config import _ENUMS, CiConfig, ProjectConfig, _parse_raw_config
from vergil_tooling.lib.github_config import ClassicProtectionReport
from vergil_tooling.lib.languages import CheckKind, language_commands
from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    _assert_ci_gates_producible,
    _cd_release_secrets,
    _check_remote_steps,
    _container_suffix,
    _load_existing_config,
    _remote_branch_exists,
    _resolve,
    _resolve_versions,
    check_workflows_registered,
    cwd_repo_slug,
    derive_clone_parent,
    detect_completed_steps,
    foreign_repo_refusal,
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
    restore_generated_config,
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
    step_scaffold_language,
)
from vergil_tooling.lib.versions import ResolutionError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vergil_tooling.lib.versions.resolve import Resolution


@pytest.fixture(autouse=True)
def _pin_installed_series(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the installed tooling to the v2.1 series.

    The template ``uses:`` refs and the ``vergil`` dependency default derive
    from the installed vergil-tooling (issue #3252); pinning it keeps the
    ``@v2.1`` assertions below independent of the version under test.
    """
    monkeypatch.setattr(repo_init.metadata, "version", lambda _name: "2.1.0")


_AUTOMATION_APP_ID = 9876543


@pytest.fixture(autouse=True)
def _hermetic_automation_app() -> Iterator[None]:
    """Resolve the org automation App's slug and ID without GitHub (epic
    vergil-project/.github#1064): step 9 needs the ID for the automation-branch
    ruleset's bypass actor."""
    with (
        patch(
            "vergil_tooling.lib.automation_identity.automation_app_slug",
            return_value="acme-automation",
        ),
        patch(
            "vergil_tooling.lib.automation_identity.automation_app_id",
            return_value=_AUTOMATION_APP_ID,
        ),
    ):
        yield


class TestDefaultVergilSeries:
    def test_series_from_installed_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(repo_init.metadata, "version", lambda _name: "9.4.17")
        assert repo_init.default_vergil_series() == "v9.4"

    def test_reads_the_vergil_tooling_distribution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def _version(name: str) -> str:
            seen.append(name)
            return "2.1.242"

        monkeypatch.setattr(repo_init.metadata, "version", _version)
        assert repo_init.default_vergil_series() == "v2.1"
        assert seen == ["vergil-tooling"]

    def test_context_default_tracks_tooling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(repo_init.metadata, "version", lambda _name: "3.0.1")
        assert RepoInitContext(org="o", name="r").vergil_version == "v3.0"

    def test_templates_use_context_series_not_literal(self) -> None:
        ctx = RepoInitContext(org="o", name="r")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.14"]
        ctx.release_model = "tagged-release"
        ctx.publish_docs = True
        ctx.publish_release = True
        ctx.package = True
        ctx.vergil_version = "v9.4"
        rendered = (
            render_ci_workflow(ctx)
            + render_cd_workflow(ctx)
            + repo_init.render_ops_workflow(ctx)
            + render_epic_rollup_workflow("v9.4")
        )
        assert "@v2.1\n" not in rendered
        for workflow in (
            "ci-audit",
            "ci-quality",
            "ci-security",
            "ci-docs",
            "ci-test",
            "ci-version-bump",
            "ci-package",
            "cd-docs",
            "cd-release",
            "ops-github-config",
            "ops-epic-rollup",
        ):
            assert f"{workflow}.yml@v9.4\n" in rendered, workflow


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
    # Languages are listed sorted: cpp, go, python, rust, typescript (index 3 is python).
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


class TestCwdRepoSlug:
    def test_returns_slug(self) -> None:
        with patch("vergil_tooling.lib.repo_init.github.current_repo", return_value="org/repo"):
            assert cwd_repo_slug() == "org/repo"

    def test_returns_none_when_not_a_repo(self) -> None:
        with patch(
            "vergil_tooling.lib.repo_init.github.current_repo",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            assert cwd_repo_slug() is None

    def test_returns_none_when_empty(self) -> None:
        with patch("vergil_tooling.lib.repo_init.github.current_repo", return_value=""):
            assert cwd_repo_slug() is None


class TestForeignRepoRefusal:
    def test_adopt_never_refuses(self) -> None:
        ctx = RepoInitContext(org="org", name="repo", adopt=True)
        with patch("vergil_tooling.lib.repo_init.cwd_repo_slug") as mock_slug:
            assert foreign_repo_refusal(ctx) is None
        mock_slug.assert_not_called()  # short-circuits before touching CWD

    def test_target_dir_never_refuses(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="org", name="repo", target_dir=tmp_path)
        with patch("vergil_tooling.lib.repo_init.cwd_repo_slug") as mock_slug:
            assert foreign_repo_refusal(ctx) is None
        mock_slug.assert_not_called()

    def test_none_outside_a_repo(self) -> None:
        ctx = RepoInitContext(org="org", name="repo")
        with patch("vergil_tooling.lib.repo_init.cwd_repo_slug", return_value=None):
            assert foreign_repo_refusal(ctx) is None

    def test_none_when_cwd_is_target(self) -> None:
        ctx = RepoInitContext(org="Org", name="Repo")  # case-insensitive match
        with patch("vergil_tooling.lib.repo_init.cwd_repo_slug", return_value="org/repo"):
            assert foreign_repo_refusal(ctx) is None

    def test_refuses_inside_a_foreign_repo(self) -> None:
        ctx = RepoInitContext(org="mnemosys-project", name="docs")
        with patch(
            "vergil_tooling.lib.repo_init.cwd_repo_slug",
            return_value="mnemosys-project/.github",
        ):
            msg = foreign_repo_refusal(ctx)
        assert msg is not None
        assert "mnemosys-project/docs" in msg
        assert "mnemosys-project/.github" in msg
        assert "--target-dir" in msg

    def test_none_when_org_layout_derivable_inside_foreign_repo(self, tmp_path: Path) -> None:
        # Launched from the org .github clone: cwd_repo_slug reports a foreign
        # repo, but the org-layout parent is derivable, so the clone will land
        # correctly beside sibling repos — no refusal (#2225).
        org_dir = tmp_path / "mnemosys-project"
        nested = org_dir / ".github"
        nested.mkdir(parents=True)
        ctx = RepoInitContext(org="mnemosys-project", name="docs")

        with (
            patch("vergil_tooling.lib.repo_init.Path.cwd", return_value=nested),
            patch(
                "vergil_tooling.lib.repo_init.cwd_repo_slug",
                return_value="mnemosys-project/.github",
            ),
        ):
            assert foreign_repo_refusal(ctx) is None


class TestDeriveCloneParent:
    def test_returns_org_root_when_cwd_is_org_dir(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "vergil-project"
        org_dir.mkdir()
        assert derive_clone_parent("vergil-project", org_dir) == org_dir.resolve()

    def test_walks_up_to_org_root_from_nested_cwd(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "vergil-project"
        nested = org_dir / ".github" / "workflows"
        nested.mkdir(parents=True)
        assert derive_clone_parent("vergil-project", nested) == org_dir.resolve()

    def test_returns_none_when_no_ancestor_matches_org(self, tmp_path: Path) -> None:
        stray = tmp_path / "somewhere-else"
        stray.mkdir()
        assert derive_clone_parent("vergil-project", stray) is None


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
        assert "## Memory" in content
        assert "## Git and GitHub" in content
        assert "## Validation" in content

    def test_wraps_consumer_template_in_markers(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        content = render_claude_md(ctx)
        begin = "<!-- vergil:template:claude-md:begin -->"
        end = "<!-- vergil:template:claude-md:end -->"
        assert content.count(begin) == 1
        assert content.count(end) == 1
        assert content.index(begin) < content.index("## Memory")
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

    def test_long_description_emitted_unwrapped(self) -> None:
        # The bundled markdownlint config has MD013 off, so a long description
        # is emitted as given, on one line, not reflowed (issue #4103).
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
        assert f"\n{ctx.description}\n" in content

    def test_description_paragraphs_preserved(self) -> None:
        # Blank-line-separated paragraphs survive as separate blocks.
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.description = "First paragraph.\n\nSecond paragraph."
        ctx.license_type = "MIT"
        ctx.publish_docs = True
        content = render_readme(ctx)
        assert "First paragraph.\n\nSecond paragraph.\n" in content


class TestRenderGitignore:
    def test_contains_baseline_patterns(self) -> None:
        content = render_gitignore("python")
        # Base patterns live inside the composed managed block now.
        assert ".DS_Store" in content
        assert ".worktrees/" in content
        assert ".venv/" in content
        assert ".venv-host/" not in content

    def test_contains_vergil_workflow_patterns(self) -> None:
        content = render_gitignore("python")
        assert ".vergil/" in content
        assert "build/" in content
        assert ".superpowers/" in content

    def test_flagship_managed_block_matches_render_block(self) -> None:
        """Drift guard: this repo's own managed fence equals ``render_block``.

        vergil-tooling is the first repo converted to the fenced form (dogfood,
        epic vergil-project/.github#325). Its ``.gitignore`` managed block must be
        exactly ``gitignore.render_block("python")`` — the composed base + python
        fence — so the flagship never drifts from the source of truth it ships.

        This is the only gate on the self-hosting case: after a fragment edit the
        installed ``vrg-github-repo-config update`` composes from the *installed*
        package's data and leaves this fence stale (#3774). So the comparison
        must be against this worktree's ``src/`` data, and the failure says how
        to resync from it.
        """
        repo_root = Path(__file__).resolve().parents[2]
        module_path = Path(gitignore.__file__).resolve()
        assert module_path.is_relative_to(repo_root / "src"), (
            f"vergil_tooling.lib.gitignore was imported from {module_path}, not "
            "this worktree's src/; the drift guard would compare the wrong data"
        )
        _repo_local, fence = gitignore.parse((repo_root / ".gitignore").read_text(encoding="utf-8"))
        assert fence == gitignore.render_block("python"), (
            "This repo's .gitignore managed fence does not match what "
            "src/vergil_tooling/data/gitignore/ composes. The installed "
            "vrg-github-repo-config update uses the installed package's data; "
            "resync from this worktree's source instead, at its root:\n"
            "    PYTHONPATH=src python3 -c 'from pathlib import Path; "
            "from vergil_tooling.lib.repo_config_fixes import fix_gitignore; "
            'fix_gitignore(Path("."))\''
        )

    def test_render_gitignore_returns_render_block(self) -> None:
        rendered = render_gitignore("python")
        # Base + python patterns are present, inside the fence.
        assert ".venv/" in rendered
        assert "quality-ruff.json" in rendered
        assert "docs/site/site/" in rendered
        # It IS the composed managed block, byte-for-byte.
        assert rendered == gitignore.render_block("python")

    def test_render_gitignore_base_only_for_no_fragment(self) -> None:
        # A language without a managed fragment (or None) yields a base-only
        # block — no python-specific patterns, matching compose(None).
        rendered = render_gitignore(None)
        assert rendered == gitignore.render_block(None)
        assert "quality-ruff.json" not in rendered
        assert ".venv/" in rendered


class TestRenderOpsWorkflow:
    def test_ops_cron_minute_deterministic_and_in_range(self) -> None:
        a = repo_init._ops_cron_minute("vergil-project", "vergil-tooling")
        b = repo_init._ops_cron_minute("vergil-project", "vergil-tooling")
        assert a == b
        assert 0 <= a <= 59

    def test_ops_cron_minute_varies_by_repo(self) -> None:
        minutes = {
            repo_init._ops_cron_minute("vergil-project", n)
            for n in ("vergil-tooling", "vergil-actions", "vergil-vm", "vergil-containers")
        }
        assert len(minutes) >= 2  # spread, not all identical

    def test_render_ops_workflow_wires_audit_with_staggered_cron(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-actions")
        yaml = repo_init.render_ops_workflow(ctx)
        minute = repo_init._ops_cron_minute("vergil-project", "vergil-actions")
        assert f"- cron: '{minute} 1 * * *'" in yaml
        assert "ops-github-config.yml@v2.1" in yaml
        assert "workflow_dispatch:" in yaml

    def test_scaffolded_repo_passes_new_audit_checks(self, tmp_path: Path) -> None:
        """A freshly scaffolded repo passes both new audit checks (round-trip)."""
        from vergil_tooling.lib import repo_config

        # No vergil.toml here, so the audit resolves a base-only fence (None);
        # scaffold a matching base-only .gitignore for the round-trip.
        (tmp_path / ".gitignore").write_text(repo_init.render_gitignore(None), encoding="utf-8")
        ctx = RepoInitContext(org="vergil-project", name="demo")
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "ops.yml").write_text(repo_init.render_ops_workflow(ctx), encoding="utf-8")
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        repo_config._check_required_workflows(tmp_path, items)
        assert items == []


class TestRenderCiWorkflow:
    def test_python_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        ctx.resolution = _resolved("python", ctx.ci_versions)
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "ci-quality.yml@v2.1" in content
        assert "ci-audit.yml@v2.1" in content
        assert "ci-test.yml@v2.1" in content
        assert "container-suffix: python" in content
        assert "ci-version-bump.yml@v2.1" in content
        assert "run-codeql: false" not in content

    def test_cpp_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "cpp"
        ctx.ci_versions = ["clang-20", "clang-19"]
        ctx.resolution = _resolved("cpp", ctx.ci_versions)
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        # Compiler-family-aware suffix → prod-cpp-clang image family. The tag
        # is resolved by the reusable workflow from vergil.toml, not passed by
        # the thin caller (epic vergil-project/.github#338, Task 10).
        assert "container-suffix: cpp-clang" in content
        assert "container-tag:" not in content
        # cpp is CodeQL-supported, so run-codeql must NOT be disabled.
        assert "run-codeql: false" not in content

    def test_typescript_workflow(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "typescript"
        ctx.ci_versions = ["node-24", "node-22"]
        ctx.resolution = _resolved("typescript", ctx.ci_versions)
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        # The emitted ci.yml carries the *primary-language* name for container
        # resolution — NOT the CodeQL identifier `javascript-typescript` (that
        # mapping is the reusable Action's job, epic vergil-project/.github#284 T7).
        assert "language: typescript" in content
        # Node-family suffix → prod-ts-node image family. The tag is resolved
        # by the reusable workflow from vergil.toml, not passed by the caller.
        assert "container-suffix: ts-node" in content
        assert "container-tag:" not in content
        # typescript is CodeQL-supported, so run-codeql must NOT be disabled.
        assert "run-codeql: false" not in content

    def test_rust_workflow_runs_codeql(self) -> None:
        # The CI-gates ruleset requires CodeQL for rust, so ci.yml must not skip
        # it: a skipped job leaves every PR blocked on green checks (issue #4381).
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "rust"
        ctx.resolution = _resolved("rust")
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "language: rust" in content
        assert "run-codeql: false" not in content

    @pytest.mark.parametrize("language", [*sorted(_ENUMS["primary-language"]), ""])
    def test_codeql_run_matches_ruleset_requirement(self, language: str) -> None:
        # For every primary language (and none), ci.yml runs CodeQL exactly when
        # the CI-gates ruleset requires its checks, so the ruleset never waits
        # on a check the workflow skips (issue #4381).
        from vergil_tooling.lib.github_config import desired_ci_gates_rulesets

        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = language
        ctx.ci_versions = ["latest"]
        ctx.release_model = "tagged-release"
        runs_codeql = "run-codeql: false" not in render_ci_workflow(ctx)

        project = ProjectConfig(
            repository_type="library",
            versioning_scheme="semver",
            branching_model="library-release",
            release_model="tagged-release",
            primary_language=language or None,
        )
        ruleset = desired_ci_gates_rulesets(
            project, CiConfig(versions=["latest"], integration_tests=False)
        )[0]
        required = {
            str(check["context"])
            for rule in ruleset.rules
            if rule["type"] == "required_status_checks"
            for check in cast("dict[str, list[dict[str, object]]]", rule["parameters"])[
                "required_status_checks"
            ]
        }
        requires_codeql = "security / codeql" in required
        assert requires_codeql == ("CodeQL" in required)
        assert runs_codeql == requires_codeql

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
        # quality + version; the security job passes no container-suffix.
        assert content.count("container-suffix: base") == 2
        assert "container-tag:" not in content

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
        # quality only; the security job passes no container-suffix.
        assert content.count("container-suffix: base") == 1
        assert "container-tag:" not in content

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

    @pytest.mark.parametrize("language", ["python", ""])
    def test_repo_init_ci_has_no_upload_sarif(self, language: str) -> None:
        """Every repo is public: no SARIF opt-out, and no ``actions: read`` grant.

        ci-security.yml@v2.1 no longer requests ``actions: read`` (epic
        vergil-project/.github#1476), so the security job grants only what it uses.
        """
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = language
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        content = render_ci_workflow(ctx)
        assert "upload-sarif" not in content
        security = content.split("\n  security:\n", 1)[1].split("\n\n", 1)[0]
        assert "ci-security.yml@" in security
        assert "    permissions:\n      contents: read\n      security-events: write\n" in security
        assert "actions: read" not in security

    def test_pins_are_v2_1(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "@v2.0" not in content
        assert "ci-security.yml@v2.1" in content

    def test_publish_docs_emits_ci_docs_job(self) -> None:
        """A docs-publishing repo must invoke ci-docs.yml from ci.yml so the
        `docs / docs` gate reports on pull_request (issue #2720)."""
        ctx = RepoInitContext(org="mnemosys-project", name="docs")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        ctx.publish_docs = True
        content = render_ci_workflow(ctx)
        assert "ci-docs.yml@v2.1" in content
        assert "\n  docs:\n" in content

    def test_no_publish_docs_omits_ci_docs_job(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="dotgithub")
        ctx.ci_versions = ["latest"]
        ctx.release_model = "none"
        ctx.publish_docs = False
        content = render_ci_workflow(ctx)
        assert "ci-docs.yml" not in content

    def test_thin_caller_omits_versions_and_container_tag(self) -> None:
        """The rendered ci.yml is a thin caller: it passes no ``versions:`` or
        ``container-tag:`` input under any reusable-workflow call. Both now
        resolve from ``vergil.toml`` inside the reusable workflows, so the
        caller just ``uses:`` the workflow at the pinned tag (epic
        vergil-project/.github#338, Task 10). Exercised across every job the
        renderer can emit — audit, quality, security, docs, test, version."""
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12", "3.13", "3.14"]
        ctx.resolution = _resolved("python", ctx.ci_versions)
        ctx.release_model = "tagged-release"
        ctx.integration_tests = True
        ctx.publish_docs = True
        content = render_ci_workflow(ctx)
        assert "versions:" not in content
        assert "container-tag:" not in content
        # container-suffix still selects the image family and stays emitted.
        assert "container-suffix: python" in content

    def test_security_job_omits_deprecated_run_standards(self) -> None:
        """ci-security.yml retired its standards job and documents
        ``run-standards`` as deprecated and ignored, so the generated caller
        must not pass it — a new repo would otherwise be born with dead
        config (issue #3384)."""
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.12"]
        ctx.release_model = "tagged-release"
        content = render_ci_workflow(ctx)
        assert "run-standards" not in content
        security = yaml.safe_load(content)["jobs"]["security"]["with"]
        assert security["run-security"] == "${{ inputs.run-security != 'false' }}"

    @staticmethod
    def _job_block(content: str, job: str) -> str:
        """Return the rendered text of one top-level job, header included."""
        start = content.index(f"\n  {job}:\n")
        end = content.find("\n\n", start + 1)
        return content[start:] if end == -1 else content[start:end]

    @pytest.mark.parametrize("release_model", ["tagged-release", "none"])
    @pytest.mark.parametrize("language", ["python", ""])
    def test_omits_deprecated_inputs(self, release_model: str, language: str) -> None:
        """The rendered ci.yml emits none of the inputs vergil-actions has
        deprecated to no-ops (issue #3387): ``run-release`` is neither
        declared under ``workflow_call`` nor forwarded to ci-version-bump.yml,
        ``run-standards`` is not passed to ci-security.yml, and the security
        job passes no ``container-suffix``."""
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = language
        ctx.ci_versions = ["3.14"] if language else ["latest"]
        ctx.release_model = release_model
        content = render_ci_workflow(ctx)
        assert "run-release" not in content
        assert "run-standards" not in content
        security = self._job_block(content, "security")
        assert "ci-security.yml@v2.1" in security
        assert "container-suffix" not in security
        assert "run-security: ${{ inputs.run-security != 'false' }}" in security
        if release_model != "none":
            # The version job still selects its image via container-suffix.
            version = self._job_block(content, "version")
            assert "ci-version-bump.yml@v2.1" in version
            assert "container-suffix:" in version


class TestCiGatesProducibility:
    """The generated ci.yml must emit every context the CI-gates ruleset
    requires on a pull_request — the parity #2720 restores and guards."""

    @staticmethod
    def _unproducible(ctx: RepoInitContext, *, docs: bool) -> list[str]:
        from vergil_tooling.lib.github_config import (
            desired_ci_gates_rulesets,
            required_status_contexts,
            unproducible_ci_yaml_contexts,
        )

        project = ProjectConfig(
            repository_type="documentation",
            versioning_scheme="semver",
            branching_model="gitflow",
            release_model=ctx.release_model,
            primary_language=ctx.primary_language,
        )
        ci = CiConfig(versions=ctx.ci_versions, integration_tests=ctx.integration_tests)
        ruleset = desired_ci_gates_rulesets(
            project, ci, docs=docs, integration_versions=ctx.ci_versions
        )[0]
        result: list[str] = unproducible_ci_yaml_contexts(
            render_ci_workflow(ctx), required_status_contexts(ruleset)
        )
        return result

    def test_docs_repo_is_fully_producible(self) -> None:
        """End-to-end: a python docs repo's generated ci.yml produces every
        required context, so nothing is unproducible (the mnemosys/docs case)."""
        ctx = RepoInitContext(org="mnemosys-project", name="docs")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.14"]
        ctx.release_model = "tagged-release"
        ctx.publish_docs = True
        assert self._unproducible(ctx, docs=True) == []

    def test_integration_tests_leave_an_unproducible_context(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.14"]
        ctx.release_model = "tagged-release"
        ctx.integration_tests = True
        ctx.publish_docs = True
        assert self._unproducible(ctx, docs=True) == ["test / integration / 3.14"]


class TestAssertCiGatesProducible:
    def _ctx_with_ci(self, tmp_path: Path, ctx: RepoInitContext) -> RepoInitContext:
        ctx.work_dir = tmp_path
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "ci.yml").write_text(render_ci_workflow(ctx))
        return ctx

    def _desired(self, ctx: RepoInitContext) -> Any:
        """A minimal desired-state stand-in: the guard only reads ``.rulesets``."""
        from types import SimpleNamespace

        from vergil_tooling.lib.github_config import desired_ci_gates_rulesets

        project = ProjectConfig(
            repository_type="documentation",
            versioning_scheme="semver",
            branching_model="gitflow",
            release_model=ctx.release_model,
            primary_language=ctx.primary_language,
        )
        ci = CiConfig(versions=ctx.ci_versions, integration_tests=ctx.integration_tests)
        rulesets = desired_ci_gates_rulesets(
            project,
            ci,
            docs=ctx.publish_docs,
            integration_versions=ctx.ci_versions,
        )
        return SimpleNamespace(rulesets=list(rulesets))

    def test_passes_for_a_docs_repo(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="mnemosys-project", name="docs")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.14"]
        ctx.release_model = "tagged-release"
        ctx.publish_docs = True
        self._ctx_with_ci(tmp_path, ctx)
        # Does not raise.
        _assert_ci_gates_producible(ctx, self._desired(ctx))

    def test_raises_for_integration_tests(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.primary_language = "python"
        ctx.ci_versions = ["3.14"]
        ctx.release_model = "tagged-release"
        ctx.integration_tests = True
        ctx.publish_docs = True
        self._ctx_with_ci(tmp_path, ctx)
        with pytest.raises(RuntimeError) as exc:
            _assert_ci_gates_producible(ctx, self._desired(ctx))
        message = str(exc.value)
        assert "test / integration / 3.14" in message
        assert "2721" in message
        assert "Cause of test / integration / 3.14: [ci].integration-tests = true" in message


def _resolved(language: str, versions: list[str] | None = None) -> Resolution | None:
    """The resolver's answer for a library repo, against the fixture window."""
    pin = "" if versions is None else "versions = [" + ", ".join(f'"{v}"' for v in versions) + "]"
    return _resolve_versions(
        "[project]\n"
        'repository-type = "library"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        f'primary-language = "{language}"\n'
        f"[ci]\n{pin}\n"
        "[dependencies]\n"
        'vergil = "v2.1"\n'
    )


class TestContainerHelpers:
    def test_no_resolution_is_base(self) -> None:
        assert _container_suffix(None) == "base"

    def test_python_primary_is_newest(self) -> None:
        resolution = _resolved("python")
        assert _container_suffix(resolution) == "python"

    def test_cpp_family_rides_the_suffix(self) -> None:
        # The window family is the image suffix, so clang-20 runs on
        # prod-cpp-clang (epic vergil-project/.github#207).
        resolution = _resolved("cpp", ["clang-20"])
        assert _container_suffix(resolution) == "cpp-clang"

    def test_cpp_gcc(self) -> None:
        resolution = _resolved("cpp", ["gcc-14"])
        assert _container_suffix(resolution) == "cpp-gcc"

    def test_typescript_node(self) -> None:
        resolution = _resolved("typescript", ["node-24", "node-22"])
        assert _container_suffix(resolution) == "ts-node"


class TestResolveVersions:
    def test_no_language_is_none(self) -> None:
        toml = (
            "[project]\n"
            'repository-type = "documentation"\n'
            'versioning-scheme = "semver"\n'
            'branching-model = "library-release"\n'
            'release-model = "none"\n'
            "[ci]\n"
            "[dependencies]\n"
            'vergil = "v2.1"\n'
        )
        assert _resolve_versions(toml) is None

    def test_pin_outside_window_stops_the_wizard(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(SystemExit) as exc:
            _resolved("python", ["3.11"])
        assert exc.value.code == 1
        assert re.match(r"ERROR: .*3\.11.*remove it from \[ci\]\.versions", capsys.readouterr().err)

    def test_window_error_is_a_ci_annotation_under_github_actions(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with pytest.raises(SystemExit) as exc:
            _resolved("python", ["3.11"])
        assert exc.value.code == 1
        assert capsys.readouterr().err.startswith("::error")

    def test_unpinned_family_language_stops_the_wizard(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(SystemExit) as exc:
            _resolved("cpp")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("ERROR: ")
        assert "cpp must pin [ci].versions: its window has more than one family" in err

    _APP_MULTI_PIN = (
        "[project]\n"
        'repository-type = "application"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "python"\n'
        "[ci]\n"
        'versions = ["3.13", "3.14"]\n'
        "[dependencies]\n"
        'vergil = "v2.1"\n'
    )

    def test_app_multi_pin_stops_the_wizard(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(SystemExit) as exc:
            _resolve_versions(self._APP_MULTI_PIN)
        assert exc.value.code == 1
        assert re.match(
            r"ERROR: \[ci\]\.versions pins 2 versions .*pin a single version",
            capsys.readouterr().err,
        )


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
        ctx.resolution = _resolved("python", ctx.ci_versions)
        content = render_cd_workflow(ctx)
        assert "cd-release.yml@v2.1" in content
        assert "if: github.ref == 'refs/heads/main' && !github.event.created\n" in content
        assert "language: python" in content
        # cd-release@v2.1 derives the release image tag from the resolver's
        # primary, so the caller passes no container tag (issue #3484).
        assert "container-tag" not in content
        # python publishes via OIDC trusted publishing, so only the automation
        # App secrets the tag push needs are forwarded (issue #4747), and the
        # blanket `secrets: inherit` must never be emitted (epic .github#189).
        assert "secrets: inherit" not in content
        assert yaml.safe_load(content)["jobs"]["release"]["secrets"] == {
            "APP_CLIENT_ID": "${{ secrets.APP_CLIENT_ID }}",
            "APP_PRIVATE_KEY": "${{ secrets.APP_PRIVATE_KEY }}",
        }
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

    def test_release_skips_the_push_that_creates_main(self) -> None:
        # Step 8 creates `main` by a direct push, which no release PR produced:
        # cd-release's evidence gate could only fail on it (issue #4580). The
        # job runs on every other push to main, so a real release without a
        # resolvable PR still fails the gate.
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = True
        ctx.publish_release = True
        ctx.primary_language = "go"
        ctx.resolution = _resolved("go")
        release = yaml.safe_load(render_cd_workflow(ctx))["jobs"]["release"]
        assert release["if"] == "github.ref == 'refs/heads/main' && !github.event.created"

    def test_docs_and_release(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = True
        ctx.publish_release = True
        ctx.primary_language = "go"
        ctx.resolution = _resolved("go")
        content = render_cd_workflow(ctx)
        assert "cd-docs.yml@v2.1" in content
        assert "cd-release.yml@v2.1" in content
        assert "language: go" in content
        # cd-release resolves the release image (prod-go:1.26) itself; the
        # caller passes no container tag (issue #3484).
        assert "container-tag" not in content
        assert "attestations: write" in content
        # go needs no publishing token: only the App secrets, never blanket inherit.
        assert "secrets: inherit" not in content
        assert yaml.safe_load(content)["jobs"]["release"]["secrets"] == {
            "APP_CLIENT_ID": "${{ secrets.APP_CLIENT_ID }}",
            "APP_PRIVATE_KEY": "${{ secrets.APP_PRIVATE_KEY }}",
        }
        # cd-docs reports its failures with the App's token, so the docs job
        # passes both App secrets too (issue #4965).
        assert yaml.safe_load(content)["jobs"]["docs"]["secrets"] == {
            "APP_CLIENT_ID": "${{ secrets.APP_CLIENT_ID }}",
            "APP_PRIVATE_KEY": "${{ secrets.APP_PRIVATE_KEY }}",
        }

    def test_release_rust_explicit_secrets(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        ctx.publish_release = True
        ctx.primary_language = "rust"
        content = render_cd_workflow(ctx)
        assert "secrets: inherit" not in content
        assert "    secrets:\n" in content
        assert yaml.safe_load(content)["jobs"]["release"]["secrets"] == {
            "APP_CLIENT_ID": "${{ secrets.APP_CLIENT_ID }}",
            "APP_PRIVATE_KEY": "${{ secrets.APP_PRIVATE_KEY }}",
            "CARGO_REGISTRY_TOKEN": "${{ secrets.CARGO_REGISTRY_TOKEN }}",
        }

    def test_release_without_language_forwards_app_secrets(self) -> None:
        # A repo with no primary language still releases through cd-release,
        # which pushes the tags with the App's token (issue #4747).
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.publish_docs = False
        ctx.publish_release = True
        content = render_cd_workflow(ctx)
        assert "      APP_CLIENT_ID: ${{ secrets.APP_CLIENT_ID }}\n" in content
        assert "      APP_PRIVATE_KEY: ${{ secrets.APP_PRIVATE_KEY }}\n" in content

    def test_cd_release_secrets_helper(self) -> None:
        # Every release gets the App secrets; publishers add their exact set.
        app = ["APP_CLIENT_ID", "APP_PRIVATE_KEY"]
        assert _cd_release_secrets("python") == app
        assert _cd_release_secrets("go") == app
        assert _cd_release_secrets(None) == app
        assert _cd_release_secrets("elixir") == app
        assert _cd_release_secrets("rust") == [*app, "CARGO_REGISTRY_TOKEN"]


class TestRenderMkdocsYml:
    def test_contains_site_name(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        content = render_mkdocs_yml(ctx)
        assert 'site_name: "vergil-vm"' in content
        assert "material" in content

    def test_snippets_are_confined_to_the_docs_dir(self) -> None:
        content = render_mkdocs_yml(RepoInitContext(org="vergil-project", name="vergil-vm"))
        assert _SNIPPETS_CONFIG in content
        assert "  - pymdownx.snippets\n" not in content

    def test_own_docs_site_confines_snippets_the_same_way(self) -> None:
        own = Path(__file__).resolve().parents[2] / "docs" / "site" / "mkdocs.yml"
        assert _SNIPPETS_CONFIG in own.read_text()


_SNIPPETS_CONFIG = (
    "  - pymdownx.snippets:\n"
    "      base_path: !relative $docs_dir\n"
    "      restrict_base_path: true\n"
    "      check_paths: true\n"
    "      url_download: false\n"
)


class TestStepCreateRepo:
    def test_creates_new_repo(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
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

        create = [c for c in calls if "repo" in c and "create" in c]
        assert len(create) == 1
        # Every repo is created public (epic vergil-project/.github#1476).
        assert "--public" in create[0]
        assert "--private" not in create[0]

    def test_skips_when_repo_exists_nonadopt(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project",
            name="vergil-vm",
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

    def test_derives_parent_from_org_root(self, tmp_path: Path) -> None:
        # No --target-dir: launched from the org directory itself. The clone
        # lands beside sibling repos under <projects>/<org>/ (#2225).
        org_dir = tmp_path / "vergil-project"
        org_dir.mkdir()
        target = org_dir / "vergil-vm"
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("vergil_tooling.lib.repo_init.Path.cwd", return_value=org_dir),
            patch("vergil_tooling.lib.repo_init.prompt_yes_no", return_value=True),
            patch("vergil_tooling.lib.repo_init.os.chdir"),
        ):
            step_clone(ctx)

        assert ctx.work_dir == target

    def test_derives_parent_from_nested_cwd(self, tmp_path: Path) -> None:
        # Launched from a subdirectory of the org tree (e.g. the org .github
        # clone). The clone still lands beside sibling repos, not nested in the
        # subdirectory the operator happened to be in (#2225).
        org_dir = tmp_path / "vergil-project"
        nested = org_dir / ".github"
        nested.mkdir(parents=True)
        target = org_dir / "vergil-vm"
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("vergil_tooling.lib.repo_init.Path.cwd", return_value=nested),
            patch("vergil_tooling.lib.repo_init.prompt_yes_no", return_value=True),
            patch("vergil_tooling.lib.repo_init.os.chdir"),
        ):
            step_clone(ctx)

        assert ctx.work_dir == target

    def test_fails_loud_when_org_layout_not_derivable(self, tmp_path: Path) -> None:
        # No --target-dir and no ancestor named after the org: refuse loudly
        # rather than silently cloning into an arbitrary CWD (#2225).
        stray = tmp_path / "somewhere-else"
        stray.mkdir()
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        with (
            patch("vergil_tooling.lib.repo_init.Path.cwd", return_value=stray),
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=AssertionError("must not clone when the layout is underivable"),
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            step_clone(ctx)

        assert excinfo.value.code == 1

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

    def test_refuses_occupied_non_clone_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # An existing, non-empty directory that is NOT a clone (e.g. a legitimate
        # docs/ dir) must not be clobbered — refuse loudly, naming the path (#2717).
        ctx = RepoInitContext(org="vergil-project", name="docs")
        ctx.non_interactive = True
        collision = tmp_path / "docs"
        collision.mkdir()
        (collision / "index.md").write_text("hello\n")

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=AssertionError("must not clone into an occupied path"),
            ),
            pytest.raises(SystemExit),
        ):
            step_clone(ctx, parent_dir=tmp_path)
        assert str(collision.resolve()) in capsys.readouterr().err

    def test_target_dir_used_as_clone_parent(self, tmp_path: Path) -> None:
        # With no explicit parent_dir, the clone parent comes from ctx.target_dir,
        # not CWD (#2717).
        parent = tmp_path / "sibling-root"
        parent.mkdir()
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm", target_dir=parent)
        ctx.non_interactive = True
        target = parent / "vergil-vm"

        def mock_subprocess_run(cmd: Any, **kw: Any) -> None:
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with (
            patch(
                "vergil_tooling.lib.repo_init.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("vergil_tooling.lib.repo_init.Path.cwd", return_value=tmp_path / "elsewhere"),
            patch("vergil_tooling.lib.repo_init.os.chdir"),
        ):
            step_clone(ctx)

        assert ctx.work_dir == target

        assert ctx.work_dir == target


# An adopted repo's vergil.toml, every required field set.
_ADOPTED_TOML = (
    "[project]\n"
    'repository-type = "library"\n'
    'versioning-scheme = "semver"\n'
    'branching-model = "library-release"\n'
    'release-model = "tagged-release"\n'
    'primary-language = "python"\n'
    "\n"
    "[ci]\n"
    'versions = ["3.14"]\n'
    "integration-tests = false\n"
    "\n"
    "[publish]\n"
    "release = true\n"
    "docs = false\n"
    "\n"
    "[dependencies]\n"
    'vergil = "v2.1"\n'
)


class TestStepGenerateConfig:
    def test_prompts_and_writes_vergil_toml(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        # Indices are alphabetical within each sorted enum:
        # repository-type: 5=tooling; primary-language: 3=python — sorted order
        # is cpp, go, python, rust, typescript;
        # branching-model: 3=library-release, versioning-scheme: 4=semver,
        # release-model: 4=tagged-release
        inputs = iter(
            [
                "5",  # repository-type: tooling
                "3",  # primary-language: python
                "3",  # branching-model: library-release
                "4",  # versioning-scheme: semver
                "4",  # release-model: tagged-release
                "",  # ci versions: no pin, follow the window
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
        # No pin is written: the new repo follows the version window, and an
        # app resolves to its newest version.
        assert "versions =" not in content
        assert ctx.resolution is not None
        assert ctx.resolution.versions == ("3.14",)

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
            'versions = ["3.14"]\n'
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
        # The existing pin is kept.
        assert ctx.ci_versions == ["3.14"]

    def test_non_interactive_resolves_flags_without_prompting(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.non_interactive = True
        ctx.opt_repository_type = "library"
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

        assert ctx.repository_type == "library"
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

    def test_interactive_yes_to_integration_tests_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Refused as soon as the answer is resolved: no vergil.toml is written,
        # nothing is committed and nothing pushed (#4448). run_wizard asks
        # before step 1 instead (#4462); should step 3 ever ask a new-repo run,
        # the refusal names the repository step 1 left behind.
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.completed_steps = {1, 2}
        inputs = iter(
            [
                "5",  # repository-type: tooling
                "3",  # primary-language: python
                "3",  # branching-model: library-release
                "4",  # versioning-scheme: semver
                "4",  # release-model: tagged-release
                "",  # ci versions
                "y",  # integration tests
            ]
        )

        with (
            patch("builtins.input", side_effect=lambda _="": next(inputs)),
            patch("vergil_tooling.lib.repo_init.git.run") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            step_generate_config(ctx)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith(
            "ERROR: Refusing to initialize vergil-project/vergil-vm: the answer to "
            "'Integration tests?' sets integration tests on. "
            "[ci].integration-tests = true makes the CI-gates ruleset require"
        )
        assert "vergil-project/vergil-tooling#2721" in err
        assert err.rstrip().endswith(
            "Nothing has been pushed and no GitHub settings were applied, but step 1 "
            "has created vergil-project/vergil-vm on GitHub (or found it there) and it "
            "is left in place: delete it, or re-run vrg-github-repo-init "
            "vergil-project/vergil-vm without integration tests to finish initializing it."
        )
        assert not (tmp_path / "vergil.toml").exists()
        mock_git.assert_not_called()

    def test_early_answer_is_used_without_asking_again(self, tmp_path: Path) -> None:
        # run_wizard's answer before step 1 stands in for step 3's prompt (#4462).
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.answered_integration_tests = False
        prompts: list[str] = []
        inputs = iter(
            [
                "5",  # repository-type: tooling
                "3",  # primary-language: python
                "3",  # branching-model: library-release
                "4",  # versioning-scheme: semver
                "4",  # release-model: tagged-release
                "",  # ci versions
                "y",  # publish releases
                "y",  # publish docs
                "",  # vergil version
                "1",  # license: MIT (option 1)
                "",  # initial version
            ]
        )

        def answer(label: str = "") -> str:
            prompts.append(label)
            return next(inputs)

        with (
            patch("builtins.input", side_effect=answer),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert not any("Integration tests?" in p for p in prompts)
        assert ctx.integration_tests is False
        assert "integration-tests = false\n" in (tmp_path / "vergil.toml").read_text()

    def test_adopted_toml_with_integration_tests_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = RepoInitContext(org="vergil-project", name="dormant", adopt=True)
        ctx.work_dir = tmp_path
        ctx.non_interactive = True
        existing = _ADOPTED_TOML.replace("integration-tests = false", "integration-tests = true")
        (tmp_path / "vergil.toml").write_text(existing)

        with (
            patch("builtins.input", side_effect=AssertionError("prompted")),
            patch("vergil_tooling.lib.repo_init.git.run") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            step_generate_config(ctx)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        # An adopt run is refused as adopting, as parse_args says it (#4462).
        assert err.startswith(
            "ERROR: Refusing to adopt vergil-project/dormant: "
            "the adopted vergil.toml sets integration tests on."
        )
        assert err.rstrip().endswith("Nothing has been pushed and no GitHub settings were applied.")
        assert "test / integration / <version>" in err
        # The adopted file is left as it was.
        assert (tmp_path / "vergil.toml").read_text() == existing
        mock_git.assert_not_called()

    def test_integration_tests_flag_in_the_context_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # parse_args refuses the flag first; a context built some other way
        # still never reaches the write (#4448).
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = RepoInitContext(org="vergil-project", name="dormant", adopt=True)
        ctx.work_dir = tmp_path
        ctx.non_interactive = True
        ctx.opt_integration_tests = True
        (tmp_path / "vergil.toml").write_text(_ADOPTED_TOML)

        with (
            patch("builtins.input", side_effect=AssertionError("prompted")),
            patch("vergil_tooling.lib.repo_init.git.run") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            step_generate_config(ctx)

        assert exc.value.code == 1
        assert "--integration-tests sets integration tests on." in capsys.readouterr().err
        assert (tmp_path / "vergil.toml").read_text() == _ADOPTED_TOML
        mock_git.assert_not_called()

    def test_no_integration_tests_flag_turns_off_an_adopted_setting(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="dormant", adopt=True)
        ctx.work_dir = tmp_path
        ctx.non_interactive = True
        ctx.opt_integration_tests = False
        (tmp_path / "vergil.toml").write_text(
            _ADOPTED_TOML.replace("integration-tests = false", "integration-tests = true")
        )

        with (
            patch("builtins.input", side_effect=AssertionError("prompted")),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert ctx.integration_tests is False
        assert "integration-tests = false\n" in (tmp_path / "vergil.toml").read_text()

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

    @pytest.mark.parametrize("lang", ["python", "cpp", ""])
    def test_config_files_writes_dependabot_yml(self, tmp_path: Path, lang: str) -> None:
        """Every new repo is scaffolded with the canonical report-only dependabot.yml."""
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.primary_language = lang

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        written = (tmp_path / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        assert written == dependabot.render(lang or None)
        assert dependabot.check(written, lang or None) == []

    def test_fresh_typescript_init_passes_audit(self, tmp_path: Path) -> None:
        """A new TypeScript repo is born with its managed files, without drift (.github#1174)."""
        ctx = _committed_answers()
        ctx.primary_language = "typescript"
        ctx.ci_versions = []
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        # Step 3 writes vergil.toml; the audit reads the language from it.
        (tmp_path / "vergil.toml").write_text(render_vergil_toml(ctx))

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        for entry in repo_config.LANGUAGE_MANAGED_FILES["typescript"]:
            written = (tmp_path / entry.repo_path).read_bytes()
            assert written == repo_config.read_language_master(entry)
        items = repo_config.audit_local_config(tmp_path).items
        assert [i for i in items if i.field == "local.language_files"] == []

    def _fresh_typescript_ctx(self, tmp_path: Path) -> RepoInitContext:
        ctx = _committed_answers()
        ctx.primary_language = "typescript"
        ctx.ci_versions = []
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.initial_version = "0.1.0"
        (tmp_path / "vergil.toml").write_text(render_vergil_toml(ctx))
        return ctx

    def test_fresh_typescript_init_writes_minimal_package_json(self, tmp_path: Path) -> None:
        """A new TypeScript repo's package.json holds name, version and private only (#4552)."""
        ctx = self._fresh_typescript_ctx(tmp_path)

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        text = (tmp_path / "package.json").read_text(encoding="utf-8")
        assert text == '{\n  "name": "demo",\n  "version": "0.1.0",\n  "private": true\n}\n'
        assert json.loads(text) == {"name": "demo", "version": "0.1.0", "private": True}
        assert (tmp_path / "VERSION").read_text().strip() == "0.1.0"

    def test_fresh_typescript_init_passes_version_checks(self, tmp_path: Path) -> None:
        """The new repo passes check_version_matches and vrg-version show's cross-check (#4552)."""
        ctx = self._fresh_typescript_ctx(tmp_path)

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        assert typescript_tools.check_version_matches(tmp_path) == []
        assert version.show(tmp_path) == "0.1.0"

    def test_typescript_init_keeps_existing_package_json(self, tmp_path: Path) -> None:
        """An existing package.json is the repo's: repo-init never overwrites it (#4552)."""
        ctx = self._fresh_typescript_ctx(tmp_path)
        existing = '{\n  "name": "kept",\n  "version": "0.1.0",\n  "type": "module"\n}\n'
        (tmp_path / "package.json").write_text(existing, encoding="utf-8")

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        assert (tmp_path / "package.json").read_text(encoding="utf-8") == existing

    @pytest.mark.parametrize("lang", ["python", ""])
    def test_config_files_writes_no_language_files_for_other_languages(
        self, tmp_path: Path, lang: str
    ) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.primary_language = lang

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        for name in (
            "tsconfig.vergil.json",
            "eslint.vergil.config.mjs",
            ".prettierrc.json",
            "package.json",
        ):
            assert not (tmp_path / name).exists()

    def test_no_githooks_created(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test"
        ctx.license_type = "none"
        ctx.publish_docs = True

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        assert not (tmp_path / ".githooks").exists()


class TestStepScaffoldLanguage:
    def test_invokes_scaffold_language(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = RepoInitContext(org="acme", name="demo")
        ctx.work_dir = tmp_path
        ctx.primary_language = "cpp"
        called: dict[str, Any] = {}
        monkeypatch.setattr(
            repo_init.lang_scaffold,
            "scaffold_language",
            lambda c: called.setdefault("ctx", c),
        )
        step_scaffold_language(ctx)
        assert called["ctx"] is ctx

    def test_runs_between_config_and_ci_in_wizard(self) -> None:
        # The scaffold phase is sequenced after config files and before CI/CD,
        # so its host-written skeleton + resolved lock ride into the next step's
        # ``git add -A`` commit (epic vergil-project/.github#342).
        ctx = RepoInitContext(org="acme", name="demo")
        ctx.opt_integration_tests = False  # no early integration-tests prompt (#4462)
        steps_run: list[str] = []

        def mock_step(label: str) -> Any:
            def inner(*a: Any, **kw: Any) -> None:
                steps_run.append(label)

            return inner

        with (
            patch("vergil_tooling.lib.repo_init.step_create_repo", side_effect=mock_step("create")),
            patch("vergil_tooling.lib.repo_init.step_clone", side_effect=mock_step("clone")),
            patch(
                "vergil_tooling.lib.repo_init.step_generate_config",
                side_effect=mock_step("config-toml"),
            ),
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_config_files",
                side_effect=mock_step("config-files"),
            ),
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_language",
                side_effect=mock_step("scaffold"),
            ),
            patch(
                "vergil_tooling.lib.repo_init.step_ci_cd_workflows",
                side_effect=mock_step("ci-cd"),
            ),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step("docs")),
            patch(
                "vergil_tooling.lib.repo_init.step_branch_structure",
                side_effect=mock_step("branch"),
            ),
            patch(
                "vergil_tooling.lib.repo_init.step_github_config",
                side_effect=mock_step("gh-config"),
            ),
            patch(
                "vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step("gh-pages")
            ),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            patch("vergil_tooling.lib.repo_init.cwd_repo_slug", return_value=None),
        ):
            run_wizard(ctx)

        assert steps_run.index("config-files") < steps_run.index("scaffold")
        assert steps_run.index("scaffold") < steps_run.index("ci-cd")


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
        # Daily config-audit caller ships in every repo (epic #311).
        assert (tmp_path / ".github" / "workflows" / "ops.yml").exists()


class TestRenderEpicRollupWorkflow:
    def test_is_a_thin_caller_to_the_reusable_workflow(self) -> None:
        content = render_epic_rollup_workflow("v2.1")
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


#: The suffixes of the files repo-init writes that Prettier parses with no plugin.
_PRETTIER_SUFFIXES = (".json", ".md", ".yml", ".yaml", ".js", ".mjs", ".cjs", ".ts")


def _scaffold_typescript(tmp_path: Path) -> list[str]:
    """Run repo-init's file-writing steps for a TypeScript repo; return what they wrote.

    The paths are relative to ``tmp_path``, in sorted order.
    """
    ctx = _committed_answers()
    ctx.primary_language = "typescript"
    ctx.ci_versions = []
    ctx.publish_docs = True
    ctx.work_dir = tmp_path
    ctx.description = "Test"
    ctx.license_type = "MIT"
    (tmp_path / "vergil.toml").write_text(render_vergil_toml(ctx))
    with patch("vergil_tooling.lib.repo_init.git.run"):
        step_scaffold_config_files(ctx)
        step_ci_cd_workflows(ctx)
        step_docs_site(ctx)
    return sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())


class TestTypescriptPrettierScope:
    """TypeScript LINT's Prettier check leaves out the Vergil-managed files (#4541)."""

    def test_every_audited_file_repo_init_writes_is_excluded(self, tmp_path: Path) -> None:
        """A written file whose blanking the audit reports is one Prettier leaves out.

        The repo cannot reformat such a file, so a Prettier finding in it would
        be one the repo cannot fix. The test finds them through the audit
        itself, so a managed file added later is held to the same rule.
        """
        written = _scaffold_typescript(tmp_path)
        excluded = set(repo_config.managed_paths("typescript"))
        baseline = {(i.field, i.actual) for i in repo_config.audit_local_config(tmp_path).items}
        audited = []
        for rel in written:
            if not rel.endswith(_PRETTIER_SUFFIXES):
                continue
            path = tmp_path / rel
            original = path.read_bytes()
            path.write_text("", encoding="utf-8")
            items = {(i.field, i.actual) for i in repo_config.audit_local_config(tmp_path).items}
            path.write_bytes(original)
            if items != baseline:
                audited.append(rel)
        # The three files the fixture programs found (#4541) are among them.
        assert {"CLAUDE.md", ".claude/settings.json", ".github/workflows/ops.yml"} <= set(audited)
        assert [rel for rel in audited if rel not in excluded] == []

    def test_lint_prettier_command_excludes_the_managed_and_release_files(self) -> None:
        """The managed files (#4541), then the files the release writes (#4553)."""
        prettier = language_commands("typescript", CheckKind.LINT)[0]
        negated = [arg.removeprefix("!") for arg in prettier if arg.startswith("!")]
        assert negated == [
            *repo_config.managed_paths("typescript"),
            *changelog.release_output_paths(),
        ]
        assert changelog.release_output_paths() == ("CHANGELOG.md", "releases/*.md")

    @pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier is not installed")
    def test_lint_prettier_passes_on_a_new_repo(self, tmp_path: Path) -> None:
        """Run the real command over what repo-init writes: it finds nothing."""
        _scaffold_typescript(tmp_path)
        prettier = language_commands("typescript", CheckKind.LINT)[0]
        result = subprocess.run(prettier, cwd=tmp_path, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.skipif(shutil.which("prettier") is None, reason="prettier is not installed")
    def test_lint_prettier_leaves_out_only_the_root_release_files(self, tmp_path: Path) -> None:
        """``!releases/*.md`` drops every root release note, and nothing else (#4553)."""
        _scaffold_typescript(tmp_path)
        unformatted = "\n# Release\n- **item**\ndescription\n"
        (tmp_path / "CHANGELOG.md").write_text(unformatted, encoding="utf-8")
        (tmp_path / "releases").mkdir()
        for name in ("v0.1.0.md", "v0.2.0.md"):
            (tmp_path / "releases" / name).write_text(unformatted, encoding="utf-8")
        prettier = language_commands("typescript", CheckKind.LINT)[0]
        result = subprocess.run(prettier, cwd=tmp_path, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        # A note of the same shape elsewhere in the tree is still checked.
        (tmp_path / "docs" / "releases").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs" / "releases" / "v0.1.0.md").write_text(unformatted, encoding="utf-8")
        result = subprocess.run(prettier, cwd=tmp_path, capture_output=True, text=True, check=False)
        assert result.returncode != 0
        assert "docs/releases/v0.1.0.md" in result.stdout + result.stderr


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
            patch("vergil_tooling.lib.github_config.compute_desired_state"),
            patch("vergil_tooling.lib.github_config.apply_desired_state", side_effect=mock_apply),
            patch("vergil_tooling.lib.repo_init.sync_labels", side_effect=mock_sync),
            patch("vergil_tooling.lib.config.read_config"),
            patch("vergil_tooling.lib.repo_init._assert_ci_gates_producible"),
        ):
            step_github_config(ctx)

        assert config_applied
        assert ctx.repo in labels_synced

    def test_new_repo_gets_automation_ruleset_with_both_bypass_actors(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="new-repo")
        ctx.work_dir = tmp_path
        ctx.repository_type = "tooling"
        ctx.primary_language = "python"
        ctx.branching_model = "library-release"
        ctx.versioning_scheme = "semver"
        ctx.release_model = "tagged-release"
        ctx.ci_versions = ["3.14"]
        ctx.publish_docs = True
        ctx.vergil_version = "v2.1"
        cfg = _parse_raw_config(tomllib.loads(render_vergil_toml(ctx)))

        with (
            patch(
                "vergil_tooling.lib.automation_identity.automation_app_slug",
                return_value="acme-automation",
            ) as mock_slug,
            patch(
                "vergil_tooling.lib.github_config.apply_desired_state", return_value=[]
            ) as mock_apply,
            patch("vergil_tooling.lib.repo_init.sync_labels"),
            patch("vergil_tooling.lib.config.read_config", return_value=cfg),
            patch("vergil_tooling.lib.repo_init._assert_ci_gates_producible"),
        ):
            step_github_config(ctx)

        mock_slug.assert_called_once_with("vergil-project")
        desired = mock_apply.call_args.args[1]
        ruleset = next(rs for rs in desired.rulesets if rs.name == "Automation branches")
        assert ruleset.ref_include == ["refs/heads/automation/**"]
        assert ruleset.bypass_actors == [
            {"actor_id": _AUTOMATION_APP_ID, "actor_type": "Integration", "bypass_mode": "always"},
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        ]
        assert all(rs.bypass_actors is not None for rs in desired.rulesets)
        assert desired.repo_settings.allow_auto_merge is True

    @pytest.mark.parametrize(
        ("branching_model", "expected"),
        [
            ("library-release", [("CI gates (develop)", False), ("CI gates (main)", True)]),
            ("docs-single-branch", [("CI gates (develop)", False)]),
        ],
    )
    def test_repo_init_creates_both(
        self, tmp_path: Path, branching_model: str, expected: list[tuple[str, bool]]
    ) -> None:
        """A new repo gets develop's non-strict CI-gates ruleset and, with a main
        branch, main's strict one, each checked against the generated ci.yml
        (epic vergil-project/.github#1218, spec §6.1)."""
        ctx = RepoInitContext(org="vergil-project", name="new-repo")
        ctx.work_dir = tmp_path
        ctx.repository_type = "tooling"
        ctx.primary_language = "python"
        ctx.branching_model = branching_model
        ctx.versioning_scheme = "semver"
        ctx.release_model = "tagged-release"
        ctx.ci_versions = ["3.14"]
        ctx.publish_docs = True
        ctx.vergil_version = "v2.1"
        cfg = _parse_raw_config(tomllib.loads(render_vergil_toml(ctx)))
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(render_ci_workflow(ctx))

        with (
            patch(
                "vergil_tooling.lib.automation_identity.automation_app_slug",
                return_value="acme-automation",
            ),
            patch(
                "vergil_tooling.lib.github_config.apply_desired_state", return_value=[]
            ) as mock_apply,
            patch("vergil_tooling.lib.repo_init.sync_labels"),
            patch("vergil_tooling.lib.config.read_config", return_value=cfg),
        ):
            step_github_config(ctx)

        desired = mock_apply.call_args.args[1]
        gates = [rs for rs in desired.rulesets if rs.name.startswith("CI gates")]
        assert [
            (rs.name, rs.rules[0]["parameters"]["strict_required_status_checks_policy"])
            for rs in gates
        ] == expected


_PAGES_ENDPOINT = "repos/vergil-project/vergil-vm/pages"
_HOMEPAGE_PATCH = (
    "PATCH",
    "repos/vergil-project/vergil-vm",
    {"homepage": "https://vergil-project.github.io/vergil-vm/"},
)


def _pages_site(branch: str = "gh-pages", path: str = "/", build_type: str = "legacy") -> Any:
    return {"source": {"branch": branch, "path": path}, "build_type": build_type}


def _pages_404() -> github.GitHubAPIError:
    return github.GitHubAPIError(
        1, ("gh", "api", _PAGES_ENDPOINT), "", "gh: Not Found (HTTP 404)\n"
    )


def _post_result(returncode: int, status: str, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=f"{status}\nX: y\r\n\r\n", stderr=stderr
    )


class TestStepGithubPages:
    """Step 10 creates or confirms the Pages site, then sets the homepage (#4371)."""

    def _run(
        self,
        *,
        reads: list[Any],
        post: Any = None,
        branch_exists: bool = True,
    ) -> tuple[Any, Any, Any]:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = True
        with (
            patch("vergil_tooling.lib.repo_init.github.read_json", side_effect=reads) as read,
            patch(
                "vergil_tooling.lib.repo_init.github.post_json_with_status", return_value=post
            ) as create,
            patch("vergil_tooling.lib.repo_init.github.write_json") as write,
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=branch_exists),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_github_pages(ctx)
        return read, create, write

    def test_create_succeeds_then_sets_homepage(self) -> None:
        _, create, write = self._run(
            reads=[_pages_404()], post=_post_result(0, "HTTP/2.0 201 Created")
        )
        create.assert_called_once_with(
            _PAGES_ENDPOINT, {"source": {"branch": "gh-pages", "path": "/"}}
        )
        write.assert_called_once_with(*_HOMEPAGE_PATCH)

    def test_creates_gh_pages_branch_when_missing(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = True
        with (
            patch("vergil_tooling.lib.repo_init.github.read_json", side_effect=[_pages_404()]),
            patch(
                "vergil_tooling.lib.repo_init.github.post_json_with_status",
                return_value=_post_result(0, "HTTP/2.0 201 Created"),
            ),
            patch("vergil_tooling.lib.repo_init.github.write_json"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
            patch("vergil_tooling.lib.repo_init.git.run") as git_run,
        ):
            step_github_pages(ctx)
        git_run.assert_any_call("push", "origin", "gh-pages")

    def test_existing_site_with_requested_source_is_done(self) -> None:
        # A re-run after the create landed: no POST, so no 409.
        _, create, write = self._run(reads=[_pages_site()])
        create.assert_not_called()
        write.assert_called_once_with(*_HOMEPAGE_PATCH)

    def test_post_409_already_enabled_with_requested_source_is_done(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        post = _post_result(
            1, "HTTP/2.0 409 Conflict", "gh: GitHub Pages is already enabled. (HTTP 409)\n"
        )
        read, _, write = self._run(reads=[_pages_404(), _pages_site()], post=post)
        assert read.call_count == 2
        write.assert_called_once_with(*_HOMEPAGE_PATCH)
        assert "HTTP/2.0 409 Conflict" in capsys.readouterr().err

    def test_landed_create_reported_as_empty_json_failure_warns_and_continues(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The observed failure: an error status with an empty JSON-typed body,
        # which gh reports only as Go's parse error; the GET shows the site.
        post = _post_result(
            1, "HTTP/2.0 500 Internal Server Error", "unexpected end of JSON input\n"
        )
        _, _, write = self._run(reads=[_pages_404(), _pages_site()], post=post)
        write.assert_called_once_with(*_HOMEPAGE_PATCH)
        err = capsys.readouterr().err
        assert "HTTP/2.0 500 Internal Server Error" in err
        assert "unexpected end of JSON input" in err

    def test_real_create_error_surfaces_status_and_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        post = _post_result(
            1,
            "HTTP/2.0 422 Unprocessable Entity",
            "gh: The gh-pages branch must exist before Pages can be enabled. (HTTP 422)\n",
        )
        with pytest.raises(SystemExit) as exc:
            self._run(reads=[_pages_404(), _pages_404()], post=post)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "HTTP/2.0 422 Unprocessable Entity" in err
        assert "must exist before Pages can be enabled. (HTTP 422)" in err

    def test_failed_post_without_status_still_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        post = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with pytest.raises(SystemExit):
            self._run(reads=[_pages_404(), _pages_404()], post=post)
        assert "no HTTP status: no error output" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "site",
        [
            _pages_site(branch="main"),
            _pages_site(path="/docs"),
            _pages_site(build_type="workflow"),
            {"build_type": "legacy"},
        ],
    )
    def test_existing_site_with_another_source_is_a_loud_error(
        self, site: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            self._run(reads=[site])
        assert exc.value.code == 1
        assert "already has a GitHub Pages site" in capsys.readouterr().err

    def test_landed_create_with_another_source_is_a_loud_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        post = _post_result(1, "HTTP/2.0 409 Conflict", "gh: already enabled (HTTP 409)\n")
        with pytest.raises(SystemExit):
            self._run(reads=[_pages_404(), _pages_site(branch="main")], post=post)
        assert "already has a GitHub Pages site" in capsys.readouterr().err

    def test_get_error_other_than_404_raises(self) -> None:
        denied = github.GitHubAPIError(
            1, ("gh", "api", _PAGES_ENDPOINT), "", "gh: Forbidden (HTTP 403)\n"
        )
        with pytest.raises(github.GitHubAPIError):
            self._run(reads=[denied])

    def test_get_non_object_raises(self) -> None:
        with pytest.raises(TypeError, match="not a JSON object"):
            self._run(reads=[[1]])

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

    def test_malformed_toml_fails_loudly(self, tmp_path: Path) -> None:
        # #3451: the scaffolded dependabot.yml / .gitignore follow the language
        # read from here, so an unreadable vergil.toml must stop repo-init, not
        # fall back to a language-less default.
        (tmp_path / "vergil.toml").write_text("not = [valid toml")
        with pytest.raises(tomllib.TOMLDecodeError):
            _load_existing_config(tmp_path)


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


class TestCheckRemoteSteps:
    def test_repo_and_branches_exist(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")

        with (
            patch("vergil_tooling.lib.repo_init.github.read_output", return_value="test"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=True),
        ):
            result = _check_remote_steps(ctx)

        # Branch structure is step 8 after the language-skeleton step was
        # inserted at 5 (epic vergil-project/.github#342).
        assert result == {1, 8}

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


_REPO = "vergil-project/vergil-vm"
_WF = ".github/workflows"
_ALL_WORKFLOWS = [f"{_WF}/cd.yml", f"{_WF}/ci.yml", f"{_WF}/epic-rollup.yml", f"{_WF}/ops.yml"]


def _contents(paths: list[str]) -> list[dict[str, str]]:
    return [{"path": p, "type": "file"} for p in paths]


def _registered(paths: list[str]) -> list[object]:
    return [{"path": p, "state": "active"} for p in paths]


class TestCheckWorkflowsRegistered:
    """Every workflow file on the default branch must be registered (#4376)."""

    def _run(
        self,
        *,
        registered: list[Any],
        files: Any = None,
        default: str = "develop",
        run_side_effect: Any = None,
    ) -> tuple[Any, Any, Any, Any]:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        listing = _contents(_ALL_WORKFLOWS) if files is None else files
        reads = [{"default_branch": default}, listing]
        with (
            patch("vergil_tooling.lib.repo_init.github.read_json", side_effect=reads) as read,
            patch(
                "vergil_tooling.lib.repo_init.github.read_json_paginated",
                side_effect=registered,
            ) as listed,
            patch("vergil_tooling.lib.repo_init.github.run", side_effect=run_side_effect) as gh_run,
            patch("vergil_tooling.lib.repo_init.time.sleep") as sleep,
        ):
            check_workflows_registered(ctx)
        return read, listed, gh_run, sleep

    def test_all_registered_needs_no_workaround(self, capsys: pytest.CaptureFixture[str]) -> None:
        read, listed, gh_run, _ = self._run(registered=[_registered(_ALL_WORKFLOWS)])
        read.assert_any_call("api", f"repos/{_REPO}/contents/{_WF}?ref=develop")
        listed.assert_called_once_with(f"repos/{_REPO}/actions/workflows", "workflows")
        gh_run.assert_not_called()
        out = capsys.readouterr().out
        assert f"{_WF}/epic-rollup.yml" in out
        assert "no workaround needed" in out

    def test_extra_registered_workflows_and_non_workflow_files_are_ignored(self) -> None:
        files = [
            *_contents(_ALL_WORKFLOWS),
            {"path": f"{_WF}/README.md", "type": "file"},
            {"path": f"{_WF}/shared", "type": "dir"},
        ]
        registered = _registered([*_ALL_WORKFLOWS, "dynamic/pages/pages-build-deployment"])
        _, _, gh_run, _ = self._run(registered=[registered], files=files)
        gh_run.assert_not_called()

    def test_missing_then_registered_after_toggle(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The observed failure: only one of the four registered; the toggle fixes it.
        only_cd = _registered([f"{_WF}/cd.yml"])
        _, listed, gh_run, sleep = self._run(
            registered=[only_cd, only_cd, _registered(_ALL_WORKFLOWS)]
        )
        assert gh_run.call_args_list == [
            (("repo", "edit", _REPO, "--default-branch", "main"),),
            (("repo", "edit", _REPO, "--default-branch", "develop"),),
        ]
        assert listed.call_count == 3
        sleep.assert_called_once()
        out = capsys.readouterr().out
        assert f"Not registered: {_WF}/ci.yml, {_WF}/epic-rollup.yml, {_WF}/ops.yml" in out
        assert "Applying the workaround" in out
        assert "registered after the workaround" in out

    def test_main_default_toggles_through_develop(self) -> None:
        _, _, gh_run, _ = self._run(
            default="main",
            registered=[_registered([]), _registered(_ALL_WORKFLOWS)],
        )
        assert gh_run.call_args_list == [
            (("repo", "edit", _REPO, "--default-branch", "develop"),),
            (("repo", "edit", _REPO, "--default-branch", "main"),),
        ]

    def test_still_missing_after_toggle_fails_naming_the_paths(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stuck = _registered([f"{_WF}/cd.yml", f"{_WF}/ci.yml"])
        polls = repo_init._REGISTRATION_POLLS
        with pytest.raises(SystemExit) as exc:
            self._run(registered=[stuck] * (polls + 1))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert f"{_WF}/epic-rollup.yml, {_WF}/ops.yml" in err
        assert f"{_WF}/ci.yml" not in err

    def test_bounded_wait(self) -> None:
        stuck = _registered([])
        polls = repo_init._REGISTRATION_POLLS
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        with (
            patch(
                "vergil_tooling.lib.repo_init.github.read_json",
                side_effect=[{"default_branch": "develop"}, _contents(_ALL_WORKFLOWS)],
            ),
            patch(
                "vergil_tooling.lib.repo_init.github.read_json_paginated",
                side_effect=[stuck] * (polls + 1),
            ) as listed,
            patch("vergil_tooling.lib.repo_init.github.run"),
            patch("vergil_tooling.lib.repo_init.time.sleep") as sleep,
            pytest.raises(SystemExit),
        ):
            check_workflows_registered(ctx)
        assert listed.call_count == polls + 1
        assert sleep.call_count == polls - 1
        sleep.assert_called_with(repo_init._REGISTRATION_POLL_SECS)

    def test_api_error_while_listing_workflows_surfaces(self) -> None:
        error = github.GitHubAPIError(1, ("gh", "api"), "", "gh: Server Error (HTTP 500)\n")
        with pytest.raises(github.GitHubAPIError):
            self._run(registered=[error])

    def test_api_error_while_reading_files_surfaces(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        error = github.GitHubAPIError(1, ("gh", "api"), "", "gh: Forbidden (HTTP 403)\n")
        with (
            patch(
                "vergil_tooling.lib.repo_init.github.read_json",
                side_effect=[{"default_branch": "develop"}, error],
            ),
            patch("vergil_tooling.lib.repo_init.github.read_json_paginated") as listed,
            pytest.raises(github.GitHubAPIError),
        ):
            check_workflows_registered(ctx)
        listed.assert_not_called()

    def test_no_workflows_directory_has_nothing_to_check(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing_dir = github.GitHubAPIError(1, ("gh", "api"), "", "gh: Not Found (HTTP 404)\n")
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        with (
            patch(
                "vergil_tooling.lib.repo_init.github.read_json",
                side_effect=[{"default_branch": "develop"}, missing_dir],
            ),
            patch("vergil_tooling.lib.repo_init.github.read_json_paginated") as listed,
        ):
            check_workflows_registered(ctx)
        listed.assert_not_called()
        assert "No workflow files on develop" in capsys.readouterr().out

    def test_failed_switch_back_names_the_branch_left_as_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def gh_run(*args: str) -> None:
            if args[-1] == "develop":
                raise github.GitHubAPIError(1, ("gh", *args), "", "gh: boom (HTTP 502)\n")

        with pytest.raises(SystemExit) as exc:
            self._run(registered=[_registered([])], run_side_effect=gh_run)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "it is now main" in err
        assert f"gh repo edit {_REPO} --default-branch develop" in err

    def test_failed_first_switch_surfaces(self) -> None:
        error = github.GitHubAPIError(1, ("gh",), "", "gh: boom (HTTP 502)\n")
        with pytest.raises(github.GitHubAPIError):
            self._run(registered=[_registered([])], run_side_effect=error)

    @pytest.mark.parametrize(
        ("repo_json", "listing", "registered"),
        [
            ({}, [], []),
            ({"default_branch": "develop"}, {"path": "x"}, []),
            ({"default_branch": "develop"}, [{"type": "file"}], []),
            ({"default_branch": "develop"}, _contents([f"{_WF}/ci.yml"]), [{"id": 1}]),
        ],
    )
    def test_malformed_responses_raise(
        self, repo_json: Any, listing: Any, registered: list[object]
    ) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        with (
            patch(
                "vergil_tooling.lib.repo_init.github.read_json",
                side_effect=[repo_json, listing],
            ),
            patch(
                "vergil_tooling.lib.repo_init.github.read_json_paginated",
                return_value=registered,
            ),
            pytest.raises(TypeError),
        ):
            check_workflows_registered(ctx)


class TestStepGithubConfigExtended:
    def test_classic_protection_cleanup_reported(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        def mock_apply(*a: Any, **kw: Any) -> list[ClassicProtectionReport]:
            return [
                ClassicProtectionReport(
                    branch="develop",
                    removed_contexts=["audit / dependencies / 3.12"],
                    preserved_settings=["required_pull_request_reviews"],
                ),
            ]

        with (
            patch("vergil_tooling.lib.github_config.compute_desired_state"),
            patch("vergil_tooling.lib.github_config.apply_desired_state", side_effect=mock_apply),
            patch("vergil_tooling.lib.repo_init.sync_labels"),
            patch("vergil_tooling.lib.config.read_config"),
            patch("vergil_tooling.lib.repo_init._assert_ci_gates_producible"),
        ):
            step_github_config(ctx)

    def test_version_window_error_stops_cleanly_before_apply(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A resumed run skips step 3's version check; a pin that has since left
        # the window must stop step 9 as a one-line error, not a traceback,
        # and before anything is applied (#3670).
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        failure = ResolutionError("[ci].versions pins 1.25, which is not in the go window")

        with (
            patch("vergil_tooling.lib.github_config.compute_desired_state", side_effect=failure),
            patch("vergil_tooling.lib.github_config.apply_desired_state") as mock_apply,
            patch("vergil_tooling.lib.config.read_config"),
            pytest.raises(SystemExit) as exc,
        ):
            step_github_config(ctx)
        assert exc.value.code == 1
        assert re.search(r"^ERROR: \[ci\]\.versions pins 1\.25", capsys.readouterr().err, re.M)
        mock_apply.assert_not_called()


class TestRunWizard:
    _STEPS = (
        "step_create_repo",
        "step_clone",
        "step_generate_config",
        "step_scaffold_config_files",
        "step_scaffold_language",
        "step_ci_cd_workflows",
        "step_docs_site",
        "step_branch_structure",
        "step_github_config",
        "step_github_pages",
    )

    def _run_with_fakes(
        self, ctx: RepoInitContext, answers: list[str], remote_completed: set[int]
    ) -> tuple[list[str], list[str]]:
        """Run the wizard with every step faked; return (steps run, prompts)."""
        order: list[str] = []
        prompts: list[str] = []
        inputs = iter(answers)

        def answer(label: str = "") -> str:
            prompts.append(label)
            return next(inputs)

        with contextlib.ExitStack() as stack:
            for name in self._STEPS:
                stack.enter_context(
                    patch(
                        f"vergil_tooling.lib.repo_init.{name}",
                        side_effect=lambda *a, _n=name, **kw: order.append(_n),
                    )
                )
            stack.enter_context(
                patch(
                    "vergil_tooling.lib.repo_init._check_remote_steps",
                    return_value=remote_completed,
                )
            )
            stack.enter_context(patch("vergil_tooling.lib.repo_init.check_workflows_registered"))
            stack.enter_context(
                patch("vergil_tooling.lib.repo_init.cwd_repo_slug", return_value=None)
            )
            stack.enter_context(patch("vergil_tooling.lib.repo_init.restore_generated_config"))
            stack.enter_context(patch("builtins.input", side_effect=answer))
            run_wizard(ctx)
        return order, prompts

    def test_interactive_yes_is_refused_before_step_1(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The answer is asked before step 1 creates the repository, so a yes
        # leaves nothing behind (#4462).
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = RepoInitContext(org="vergil-project", name="new-repo")

        with pytest.raises(SystemExit) as exc:
            self._run_with_fakes(ctx, ["y"], set())

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith(
            "ERROR: Refusing to initialize vergil-project/new-repo: the answer to "
            "'Integration tests?' sets integration tests on."
        )
        assert err.rstrip().endswith("Nothing was changed.")
        assert ctx.answered_integration_tests is None

    def test_interactive_no_is_asked_once_before_step_1(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="new-repo")

        order, prompts = self._run_with_fakes(ctx, ["n"], set())

        assert len(prompts) == 1
        assert prompts[0].startswith("Integration tests?")
        assert order[0] == "step_create_repo"
        assert ctx.answered_integration_tests is False

    def test_resumed_yes_names_the_repository_step_1_left(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An earlier run's step 1 created the repository: the refusal says so.
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = RepoInitContext(org="vergil-project", name="new-repo")

        with pytest.raises(SystemExit):
            self._run_with_fakes(ctx, ["y"], {1})

        err = capsys.readouterr().err
        assert "step 1 has created vergil-project/new-repo on GitHub" in err
        assert "delete it, or re-run vrg-github-repo-init vergil-project/new-repo" in err

    @pytest.mark.parametrize(
        ("adopt", "non_interactive", "flag", "remote_completed"),
        [
            (True, False, None, set()),
            (False, True, None, set()),
            (False, False, False, set()),
            (False, False, None, {1, 2, 3}),
        ],
        ids=["adopt", "non-interactive", "flag", "step-3-done"],
    )
    def test_not_asked_before_step_1(
        self,
        adopt: bool,
        non_interactive: bool,
        flag: bool | None,
        remote_completed: set[int],
    ) -> None:
        ctx = RepoInitContext(org="vergil-project", name="new-repo", adopt=adopt)
        ctx.non_interactive = non_interactive
        ctx.opt_integration_tests = flag

        order, prompts = self._run_with_fakes(ctx, [], remote_completed)

        assert prompts == []
        assert ctx.answered_integration_tests is None
        assert order

    @pytest.mark.parametrize("remote_completed", [set(), {1, 8}])
    def test_checks_workflow_registration_after_step_8_run_or_skipped(
        self, remote_completed: set[int]
    ) -> None:
        # A run stopped by the check skips step 8 when re-run, so the check
        # runs after step 8 either way, before step 9 (#4376).
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.opt_integration_tests = False  # no early integration-tests prompt (#4462)
        order: list[str] = []

        def record(name: str) -> Any:
            return lambda *a, **kw: order.append(name)

        steps = [
            "step_create_repo",
            "step_clone",
            "step_generate_config",
            "step_scaffold_config_files",
            "step_scaffold_language",
            "step_ci_cd_workflows",
            "step_docs_site",
            "step_branch_structure",
            "step_github_config",
            "step_github_pages",
        ]
        with contextlib.ExitStack() as stack:
            for name in steps:
                stack.enter_context(
                    patch(f"vergil_tooling.lib.repo_init.{name}", side_effect=record(name))
                )
            stack.enter_context(
                patch(
                    "vergil_tooling.lib.repo_init._check_remote_steps",
                    return_value=remote_completed,
                )
            )
            stack.enter_context(
                patch(
                    "vergil_tooling.lib.repo_init.check_workflows_registered",
                    side_effect=record("check"),
                )
            )
            stack.enter_context(
                patch("vergil_tooling.lib.repo_init.cwd_repo_slug", return_value=None)
            )
            run_wizard(ctx)

        assert order.count("check") == 1
        assert order.index("check") == order.index("step_github_config") - 1
        assert ("step_branch_structure" in order) == (8 not in remote_completed)

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
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_language",
                side_effect=mock_step(5),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(10)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            # Local resume state is trusted only when CWD is the target's own
            # clone (#2717); here it is, so the step 3/4 markers apply.
            patch(
                "vergil_tooling.lib.repo_init.cwd_repo_slug",
                return_value="vergil-project/test",
            ),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                return_value=(
                    "abc1234 chore(init): step 3 - vergil.toml\n"
                    "def5678 chore(init): step 4 - config files\n"
                ),
            ),
            patch("vergil_tooling.lib.repo_init.restore_generated_config") as mock_restore,
        ):
            run_wizard(ctx)

        assert 3 not in steps_run
        assert 4 not in steps_run
        assert 5 in steps_run
        # A skipped step 3 rebuilds its answers instead of leaving defaults (#3977).
        mock_restore.assert_called_once_with(ctx)

    def test_adopt_ignores_checkpoint_markers_and_regenerates(self) -> None:
        # #2795 regression: adopt is an idempotent "overwrite managed files to
        # canonical" run. A previously-init'd repo carries permanent
        # `chore(init): step N -` markers for every generation step; adopt must
        # NOT read them as completed progress, or it skips regenerating managed
        # files (notably ci.yml at step 6), defeating adopt's own contract.
        ctx = RepoInitContext(org="mnemosys-project", name="docs", adopt=True)

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
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_language",
                side_effect=mock_step(5),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(10)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            # Adopt runs in the target's own clone, whose log carries markers for
            # every past init step — the exact history that must be ignored.
            patch(
                "vergil_tooling.lib.repo_init.cwd_repo_slug",
                return_value="mnemosys-project/docs",
            ),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                return_value=(
                    "abc chore(init): step 3 - vergil.toml\n"
                    "def chore(init): step 4 - config files\n"
                    "ghi chore(init): step 5 - CI/CD workflows\n"
                    "jkl chore(init): step 6 - docs site\n"
                ),
            ) as mock_log,
        ):
            run_wizard(ctx)

        # Every generation step re-runs; step 6 (ci.yml) is the load-bearing one.
        assert {3, 4, 5, 6}.issubset(steps_run)
        # On adopt the checkpoint log is not even consulted.
        mock_log.assert_not_called()

    def test_ignores_foreign_repo_resume_state(self) -> None:
        # #2717 regression: when CWD is a DIFFERENT repo, its own bootstrap
        # commits must not be read as this repo's progress — every step runs.
        ctx = RepoInitContext(org="mnemosys-project", name="docs")
        ctx.opt_integration_tests = False  # no early integration-tests prompt (#4462)

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
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_language",
                side_effect=mock_step(5),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(10)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            # Standing inside the `.github` repo, whose log carries step 3/4/5
            # markers that must be ignored for the unrelated `docs` target.
            patch(
                "vergil_tooling.lib.repo_init.cwd_repo_slug",
                return_value="mnemosys-project/.github",
            ),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                return_value=(
                    "abc chore(init): step 3 - vergil.toml\n"
                    "def chore(init): step 4 - config files\n"
                    "ghi chore(init): step 5 - CI/CD workflows\n"
                ),
            ) as mock_log,
        ):
            run_wizard(ctx)

        assert {3, 4, 5}.issubset(steps_run)  # none skipped
        mock_log.assert_not_called()  # the foreign log is not even read

    def test_handles_git_log_failure(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.opt_integration_tests = False  # no early integration-tests prompt (#4462)

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
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_language",
                side_effect=mock_step(5),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(10)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            # CWD is the target's own clone, so the log is read — and its failure
            # degrades to "no local completions" rather than raising.
            patch(
                "vergil_tooling.lib.repo_init.cwd_repo_slug",
                return_value="vergil-project/test",
            ),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
        ):
            run_wizard(ctx)

        assert 1 in steps_run

    def test_no_docs_skips_url(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.opt_integration_tests = False  # no early integration-tests prompt (#4462)
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
            patch(
                "vergil_tooling.lib.repo_init.step_scaffold_language",
                side_effect=mock_step(5),
            ),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(10)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            # CWD is not the target's clone, so local resume state is not trusted
            # and the git log is not even read.
            patch("vergil_tooling.lib.repo_init.cwd_repo_slug", return_value=None),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
        ):
            run_wizard(ctx)


def _committed_answers() -> RepoInitContext:
    """The step 3 answers ``_committed_ctx`` writes, with no overrides."""
    return RepoInitContext(
        org="vergil-project",
        name="demo",
        repository_type="library",
        primary_language="python",
        branching_model="library-release",
        versioning_scheme="semver",
        release_model="tagged-release",
        integration_tests=False,
        publish_release=True,
        publish_docs=False,
        vergil_version="v2.1",
    )


def _committed_ctx(tmp_path: Path, **overrides: Any) -> RepoInitContext:
    """A context whose step 3 answers are committed to ``tmp_path/vergil.toml``.

    Returns a fresh context for the resumed run: only the identity, the work
    dir and the completed steps are set, as on a real resume.
    """
    answers = _committed_answers()
    for key, value in overrides.items():
        setattr(answers, key, value)
    (tmp_path / "vergil.toml").write_text(render_vergil_toml(answers))
    ctx = RepoInitContext(org="vergil-project", name="demo")
    ctx.work_dir = tmp_path
    ctx.completed_steps = {1, 3, 4}
    return ctx


class TestRestoreGeneratedConfig:
    """A resumed fresh init rebuilds step 3's answers or stops (#3977)."""

    def test_restores_every_step3_field(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(tmp_path, ci_versions=["3.14"])

        restore_generated_config(ctx)

        assert ctx.repository_type == "library"
        assert ctx.primary_language == "python"
        assert ctx.branching_model == "library-release"
        assert ctx.versioning_scheme == "semver"
        assert ctx.release_model == "tagged-release"
        assert ctx.ci_versions == ["3.14"]
        assert ctx.integration_tests is False
        assert ctx.publish_release is True
        assert ctx.publish_docs is False
        assert ctx.vergil_version == "v2.1"
        assert ctx.package is False
        assert ctx.resolution is not None
        assert ctx.resolution.versions == ("3.14",)
        # The later steps now render the language, not the defaults.
        ci = render_ci_workflow(ctx)
        assert "      language: python\n" in ci
        assert "container-suffix: python" in ci
        assert "container-suffix: base" not in ci

    def test_no_language_restores_empty(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(
            tmp_path,
            repository_type="documentation",
            primary_language="",
            release_model="none",
            publish_release=False,
        )

        restore_generated_config(ctx)

        assert ctx.primary_language == ""
        assert ctx.resolution is None

    def test_package_table_is_restored(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(tmp_path)
        with (tmp_path / "vergil.toml").open("a") as f:
            f.write(
                '\n[package]\nbuilder = "staged"\nvendor = "vergil"\nname = "demo"\n'
                'summary = "A demo"\nsmoke = "true"\n'
            )

        restore_generated_config(ctx)

        assert ctx.package is True

    def test_missing_vergil_toml_refuses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path)
        (tmp_path / "vergil.toml").unlink()

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("ERROR: cannot resume the init of this repo: ")
        assert "vergil.toml is missing" in err
        # Nothing was restored: the defaults never reach a later step.
        assert ctx.primary_language == ""

    @pytest.mark.parametrize(
        ("text", "detail"),
        [
            ("[project\n", "is not a valid vergil.toml"),
            ('[project]\nrepository-type = "library"\n', "is not a valid vergil.toml"),
        ],
    )
    def test_invalid_vergil_toml_refuses(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        text: str,
        detail: str,
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path)
        (tmp_path / "vergil.toml").write_text(text)

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        assert detail in capsys.readouterr().err

    def test_missing_vergil_pin_is_invalid(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path)
        text = (tmp_path / "vergil.toml").read_text()
        (tmp_path / "vergil.toml").write_text(text.split("[dependencies]")[0])

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "is not a valid vergil.toml" in err
        assert "[dependencies] must contain 'vergil'" in err

    def test_contradicting_flags_refuse(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path)
        ctx.opt_primary_language = "go"
        ctx.opt_publish_docs = True
        ctx.opt_ci_versions = "3.13"

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--language 'go' (vergil.toml has 'python')" in err
        assert "--publish-docs True (vergil.toml has False)" in err
        assert "--versions ['3.13'] (vergil.toml has [])" in err
        assert ctx.primary_language == ""

    @pytest.mark.parametrize(
        ("completed", "state"),
        [
            (
                {1, 3, 4},
                "Nothing has been pushed and no GitHub settings were applied, but step 1 "
                "has created vergil-project/demo on GitHub (or found it there) and it is "
                "left in place",
            ),
            (
                {1, 3, 4, 8},
                "Step 8 had already pushed; this run stops before step 9 applies any "
                "GitHub settings.",
            ),
        ],
    )
    def test_committed_integration_tests_refuses(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        completed: set[int],
        state: str,
    ) -> None:
        # A step-3 commit from before #4448 can still carry the setting: the
        # resumed run refuses it before the later steps, not at step 9.
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path, integration_tests=True)
        ctx.completed_steps = completed

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert f"the committed {tmp_path / 'vergil.toml'} sets integration tests on." in err
        assert "vergil-project/vergil-tooling#2721" in err
        assert state in err
        assert ctx.integration_tests is False

    def test_agreeing_flags_are_accepted(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(tmp_path, ci_versions=["3.14"])
        ctx.opt_repository_type = "library"
        ctx.opt_primary_language = "python"
        ctx.opt_branching_model = "library-release"
        ctx.opt_versioning_scheme = "semver"
        ctx.opt_release_model = "tagged-release"
        ctx.opt_ci_versions = " 3.14 ,"
        ctx.opt_integration_tests = False
        ctx.opt_publish_release = True
        ctx.opt_publish_docs = False
        ctx.opt_vergil_version = "v2.1"

        restore_generated_config(ctx)

        assert ctx.primary_language == "python"

    def test_moved_window_stops_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path, ci_versions=["3.11"])

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        assert re.match(r"ERROR: .*3\.11", capsys.readouterr().err)

    def test_pending_step4_non_interactive_requires_license_and_version(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        ctx = _committed_ctx(tmp_path)
        ctx.completed_steps = {1, 3}
        ctx.non_interactive = True

        with pytest.raises(SystemExit) as exc:
            restore_generated_config(ctx)

        assert exc.value.code == 1
        assert "pass --license and --initial-version." in capsys.readouterr().err

    def test_pending_step4_non_interactive_takes_the_flags(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(tmp_path)
        ctx.completed_steps = {1, 3}
        ctx.non_interactive = True
        ctx.opt_license_type = "Apache-2.0"
        ctx.opt_initial_version = "1.2.0"

        restore_generated_config(ctx)

        assert ctx.license_type == "Apache-2.0"
        assert ctx.initial_version == "1.2.0"

    def test_pending_step4_interactive_prompts_again(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(tmp_path)
        ctx.completed_steps = {1, 3}
        inputs = iter(["2", "0.3.0"])  # license: GPL-3.0; initial version

        with patch("builtins.input", side_effect=lambda _="": next(inputs)):
            restore_generated_config(ctx)

        assert ctx.license_type == "GPL-3.0"
        assert ctx.initial_version == "0.3.0"

    def test_done_step4_asks_nothing(self, tmp_path: Path) -> None:
        ctx = _committed_ctx(tmp_path)
        ctx.non_interactive = True

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            restore_generated_config(ctx)

        assert ctx.license_type == "MIT"

    def test_resumed_wizard_renders_steps_4_to_6_from_vergil_toml(self, tmp_path: Path) -> None:
        # The #3977 scenario end to end: the init stopped after step 3's
        # checkpoint; the resumed run must write the python gitignore,
        # dependabot and ci.yml, not the language-less defaults.
        ctx = _committed_ctx(tmp_path)
        ctx.completed_steps = set()
        ctx.work_dir = None
        ctx.opt_license_type = "MIT"
        ctx.opt_initial_version = "0.1.0"
        ctx.description = "Demo"

        def clone(_ctx: RepoInitContext) -> None:
            _ctx.work_dir = tmp_path

        with (
            patch("vergil_tooling.lib.repo_init.step_clone", side_effect=clone),
            patch("vergil_tooling.lib.repo_init.step_scaffold_language"),
            patch("vergil_tooling.lib.repo_init.step_docs_site"),
            patch("vergil_tooling.lib.repo_init.step_branch_structure"),
            patch("vergil_tooling.lib.repo_init.step_github_config"),
            patch("vergil_tooling.lib.repo_init.step_github_pages"),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value={1}),
            patch("vergil_tooling.lib.repo_init.check_workflows_registered"),
            patch(
                "vergil_tooling.lib.repo_init.cwd_repo_slug",
                return_value="vergil-project/demo",
            ),
            patch(
                "vergil_tooling.lib.repo_init.git.read_output",
                return_value="abc chore(init): step 3 - vergil.toml\n",
            ),
            patch("vergil_tooling.lib.repo_init.git.run"),
            patch("vergil_tooling.lib.repo_init.step_generate_config") as mock_step3,
        ):
            run_wizard(ctx)

        mock_step3.assert_not_called()
        # Resumed with {1, 3} done; the run then records each step it ran (#4462).
        assert ctx.completed_steps == set(range(1, 11))
        ci = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
        assert "      language: python\n" in ci
        assert "container-suffix: base" not in ci
        cd = (tmp_path / ".github" / "workflows" / "cd.yml").read_text()
        assert "      language: python\n" in cd
        assert (tmp_path / ".gitignore").read_text() == render_gitignore("python")
        assert (tmp_path / ".github" / "dependabot.yml").read_text() == dependabot.render("python")


def test_render_claude_settings_equals_template() -> None:
    # The template is the whole file: no marketplace entry and no seeded ref
    # (epic vergil-project/.github#558).
    text = render_claude_settings()
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "vergil_tooling"
        / "data"
        / "claude_settings.json"
    ).read_text(encoding="utf-8")
    assert json.loads(text) == json.loads(template)
    assert "extraKnownMarketplaces" not in json.loads(text)
    assert text.endswith("\n")


def test_render_claude_md_has_no_body_validation_section() -> None:
    # The template region states the validation command; the header no longer
    # adds a second ``## Validation`` section outside the markers.
    content = render_claude_md(RepoInitContext(org="vergil-project", name="demo"))
    begin = content.index("<!-- vergil:template:claude-md:begin -->")
    assert "## Validation" not in content[:begin]
    assert content.count("## Validation") == 1


def test_repo_init_output_audits_clean(tmp_path: Path) -> None:
    from vergil_tooling.lib import repo_config

    (tmp_path / "CLAUDE.md").write_text(
        render_claude_md(RepoInitContext(org="vergil-project", name="demo"))
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(render_claude_settings())
    items: list = []
    repo_config._check_claude_md(tmp_path, items)
    repo_config._check_claude_md_length(tmp_path, items)
    repo_config._check_agents_md(tmp_path, items)
    repo_config._check_claude_settings(tmp_path, items)
    assert items == []


def test_render_claude_settings_turns_off_attribution() -> None:
    # AI co-author attribution is dropped (#3200): new repos ship with Claude
    # Code's commit/PR attribution turned off.
    data = json.loads(render_claude_settings())
    assert data["attribution"] == {"commit": "", "pr": ""}


def test_render_claude_settings_turns_off_idle_compaction() -> None:
    # Idle compaction is off (#3795): the human decides when a session is
    # compacted. Context-full auto-compaction is left alone.
    data = json.loads(render_claude_settings())
    assert data["idleCompaction"] is False
    assert "autoCompactEnabled" not in data


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
