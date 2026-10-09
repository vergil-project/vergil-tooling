"""repo-init generation for repos that declare ``[package]`` (issue #3144)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from vergil_tooling.lib.config import CiConfig, ProjectConfig
from vergil_tooling.lib.github_config import (
    desired_ci_gates_ruleset,
    required_status_contexts,
    unproducible_ci_yaml_contexts,
)
from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    _unmanaged_toml_sections,
    render_cd_workflow,
    render_ci_workflow,
    render_vergil_toml,
    step_ci_cd_workflows,
    step_generate_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INHERIT_LINE = (
    "    secrets: inherit"
    "  # nosemgrep: yaml.github-actions.security.secrets-inherit.secrets-inherit\n"
)


def _packaged_python_ctx() -> RepoInitContext:
    """A context shaped like this repo: python, docs + release, ``[package]``."""
    ctx = RepoInitContext(org="vergil-project", name="vergil-tooling")
    ctx.primary_language = "python"
    ctx.ci_versions = ["3.12", "3.13", "3.14"]
    ctx.release_model = "tagged-release"
    ctx.publish_docs = True
    ctx.publish_release = True
    ctx.package = True
    return ctx


def _hand_written(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


class TestPackagedCiWorkflow:
    def test_package_job_emitted_for_package_repo(self) -> None:
        content = render_ci_workflow(_packaged_python_ctx())
        assert (
            "  package:\n"
            "    uses: vergil-project/vergil-actions/.github/workflows/ci-package.yml@v2.1\n"
        ) in content

    def test_no_package_job_without_package(self) -> None:
        ctx = _packaged_python_ctx()
        ctx.package = False
        content = render_ci_workflow(ctx)
        assert "ci-package.yml" not in content
        assert "  package:\n" not in content

    def test_package_job_matches_hand_written_ci(self) -> None:
        generated = yaml.safe_load(render_ci_workflow(_packaged_python_ctx()))
        assert generated["jobs"]["package"] == _hand_written("ci.yml")["jobs"]["package"]

    def test_package_job_is_last(self) -> None:
        generated = yaml.safe_load(render_ci_workflow(_packaged_python_ctx()))
        assert list(generated["jobs"])[-1] == "package"


class TestPackageGateProducible:
    """The ruleset requires `package / evidence` for [package]; ci.yml must emit it."""

    @staticmethod
    def _unproducible(ctx: RepoInitContext, *, ruleset_package: bool) -> list[str]:
        project = ProjectConfig(
            repository_type="tooling",
            versioning_scheme="semver",
            branching_model="library-release",
            release_model=ctx.release_model,
            primary_language=ctx.primary_language,
        )
        ci = CiConfig(versions=ctx.ci_versions, integration_tests=ctx.integration_tests)
        ruleset = desired_ci_gates_ruleset(
            project, ci, ghas=True, docs=ctx.publish_docs, package=ruleset_package
        )
        assert ("package / evidence" in required_status_contexts(ruleset)) is ruleset_package
        result: list[str] = unproducible_ci_yaml_contexts(
            render_ci_workflow(ctx), required_status_contexts(ruleset), ghas=True
        )
        return result

    def test_package_repo_ci_produces_required_package_gate(self) -> None:
        assert self._unproducible(_packaged_python_ctx(), ruleset_package=True) == []

    def test_ci_without_package_job_cannot_satisfy_package_gate(self) -> None:
        ctx = _packaged_python_ctx()
        ctx.package = False
        assert self._unproducible(ctx, ruleset_package=True) == ["package / evidence"]


class TestPackagedCdWorkflow:
    def test_package_repo_release_inherits_secrets(self) -> None:
        content = render_cd_workflow(_packaged_python_ctx())
        assert content.endswith(_INHERIT_LINE)

    def test_package_repo_inherit_replaces_explicit_map(self) -> None:
        # A publisher with its own secrets still inherits: inherit already
        # carries CARGO_REGISTRY_TOKEN, and an explicit map would starve the
        # package-signing environment secrets.
        ctx = _packaged_python_ctx()
        ctx.primary_language = "rust"
        ctx.ci_versions = ["stable"]
        content = render_cd_workflow(ctx)
        assert _INHERIT_LINE in content
        assert "CARGO_REGISTRY_TOKEN" not in content
        assert "    secrets:\n" not in content
        assert yaml.safe_load(content)["jobs"]["release"]["secrets"] == "inherit"

    def test_no_package_keeps_least_privilege_map(self) -> None:
        ctx = _packaged_python_ctx()
        ctx.package = False
        ctx.primary_language = "rust"
        ctx.ci_versions = ["stable"]
        content = render_cd_workflow(ctx)
        assert "secrets: inherit" not in content
        assert "      CARGO_REGISTRY_TOKEN: ${{ secrets.CARGO_REGISTRY_TOKEN }}\n" in content

    def test_no_package_python_has_no_secrets_block(self) -> None:
        ctx = _packaged_python_ctx()
        ctx.package = False
        assert "secrets:" not in render_cd_workflow(ctx)

    def test_package_without_release_emits_no_release_job(self) -> None:
        ctx = _packaged_python_ctx()
        ctx.publish_release = False
        content = render_cd_workflow(ctx)
        assert "cd-release" not in content
        assert "secrets" not in content

    def test_matches_hand_written_cd(self) -> None:
        generated = yaml.safe_load(render_cd_workflow(_packaged_python_ctx()))
        assert generated == _hand_written("cd.yml")

    def test_step_writes_packaged_workflows(self, tmp_path: Path) -> None:
        ctx = _packaged_python_ctx()
        ctx.work_dir = tmp_path
        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_ci_cd_workflows(ctx)
        wf = tmp_path / ".github" / "workflows"
        assert "ci-package.yml@v2.1" in (wf / "ci.yml").read_text()
        assert _INHERIT_LINE in (wf / "cd.yml").read_text()


_BASE_TOML = (
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
    'vergil = "v2.1"\n'
)

_PACKAGE_TOML = (
    "\n"
    "[package]\n"
    'name = "demo"\n'
    'builder = "python"\n'
    "\n"
    "[package.python]\n"
    'runtime = "3.14.0"\n'
    "\n"
    "[container]\n"
    'env-prefixes = ["DEMO_"]\n'
)


class TestUnmanagedTomlSections:
    def test_extracts_only_unmanaged_tables(self) -> None:
        text = _BASE_TOML + _PACKAGE_TOML
        out = _unmanaged_toml_sections(text, tomllib.loads(text))
        assert out.startswith("[package]\n")
        assert "[package.python]\n" in out
        assert "[container]\n" in out
        assert "[project]" not in out
        assert "[ci]" not in out

    def test_empty_when_only_managed_tables(self) -> None:
        assert _unmanaged_toml_sections(_BASE_TOML, tomllib.loads(_BASE_TOML)) == ""

    def test_fails_loud_when_extraction_breaks_toml(self) -> None:
        # A header-looking line inside a multi-line string splits the string.
        text = _BASE_TOML + '\n[container]\nbuild-command = """\n[ci]\necho hi\n"""\n'
        with pytest.raises(RuntimeError, match="cannot preserve"):
            _unmanaged_toml_sections(text, tomllib.loads(text))

    def test_fails_loud_when_extraction_loses_a_value(self) -> None:
        # A table defined by a dotted key before any header cannot be lifted.
        text = 'container.env-prefixes = ["X_"]\n' + _BASE_TOML
        with pytest.raises(RuntimeError, match="cannot preserve"):
            _unmanaged_toml_sections(text, tomllib.loads(text))


def _adopt(tmp_path: Path, toml_text: str) -> RepoInitContext:
    ctx = RepoInitContext(org="vergil-project", name="demo", adopt=True)
    ctx.work_dir = tmp_path
    (tmp_path / "vergil.toml").write_text(toml_text)
    inputs = iter([""] * 12)
    with (
        patch("builtins.input", side_effect=lambda _="": next(inputs)),
        patch("vergil_tooling.lib.repo_init.git.run"),
    ):
        step_generate_config(ctx)
    return ctx


class TestAdoptPreservesPackage:
    def test_adopt_keeps_package_and_sets_ctx(self, tmp_path: Path) -> None:
        ctx = _adopt(tmp_path, _BASE_TOML + _PACKAGE_TOML)
        assert ctx.package is True
        written = tomllib.loads((tmp_path / "vergil.toml").read_text())
        assert written["package"] == {
            "name": "demo",
            "builder": "python",
            "python": {"runtime": "3.14.0"},
        }
        assert written["container"] == {"env-prefixes": ["DEMO_"]}

    def test_adopt_without_package_leaves_flag_false(self, tmp_path: Path) -> None:
        ctx = _adopt(tmp_path, _BASE_TOML)
        assert ctx.package is False
        assert (tmp_path / "vergil.toml").read_text() == render_vergil_toml(ctx)
