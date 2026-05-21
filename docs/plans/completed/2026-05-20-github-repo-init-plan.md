# vrg-github-repo-init — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an interactive wizard that bootstraps a new
VERGIL-managed repository from zero to "ready for PRs" — including
GitHub repo creation, local file scaffolding, branch structure, CI/CD
workflows, docs site, GitHub config, labels, and Pages setup.

**Architecture:** A single CLI entry point
(`vrg-github-repo-init`) delegates to a step-runner library
(`repo_init.py`) containing nine sequential steps. Each local-file
step commits a checkpoint; remote-only steps check actual state.
This makes the wizard idempotent — re-running after a failure skips
completed steps. Template files (pre-commit hook, CLAUDE.md consumer
template, license texts, settings.json) live in
`src/vergil_tooling/data/`. The tool runs on the host (not in a
container) and calls `git`/`gh` directly via subprocess, following the
same pattern as `vrg-commit` and `vrg-github-repo-config`.

**Tech Stack:** Python 3.12+, `subprocess` (git, gh), existing
`vergil_tooling.lib.{git,github,config,github_config}` libraries

**Spec:**
`docs/specs/2026-05-20-github-repo-init-design.md` (#807)

---

## File Map

| File | Responsibility |
|------|---------------|
| `src/vergil_tooling/bin/vrg_github_repo_init.py` | CLI entry point: argparse, dispatch to `run_wizard()` |
| `src/vergil_tooling/lib/repo_init.py` | Wizard orchestrator, step functions, prompt helpers, template rendering |
| `src/vergil_tooling/data/githooks_pre_commit.sh` | Pre-commit gate template (copied from `.githooks/pre-commit`) |
| `src/vergil_tooling/data/claude_settings.json` | Canonical `.claude/settings.json` template |
| `src/vergil_tooling/data/licenses/gpl-3.0.txt` | GPL-3.0 license text |
| `src/vergil_tooling/data/licenses/mit.txt` | MIT license text |
| `src/vergil_tooling/data/licenses/apache-2.0.txt` | Apache-2.0 license text |
| `tests/vergil_tooling/test_vrg_github_repo_init.py` | Tests for CLI argument parsing |
| `tests/vergil_tooling/test_repo_init.py` | Tests for wizard steps, prompts, template rendering, idempotency |
| `pyproject.toml` | Add `vrg-github-repo-init` entry point |

**Existing files modified:**
- `pyproject.toml` — add one line to `[project.scripts]`

**Existing code reused (not modified):**
- `vergil_tooling.lib.git` — `run()`, `read_output()` for git operations
- `vergil_tooling.lib.github` — `run()`, `read_output()`, `read_json()`, `write_json()` for GitHub API
- `vergil_tooling.lib.config` — `_ENUMS` for validation, `_parse_raw_config()` for adopt-mode pre-fill
- `vergil_tooling.lib.github_config` — `apply_desired_state()`, `compute_desired_state()`, `fetch_actual_state()` for step 8
- `vergil_tooling.lib.labels` — `load_labels()` for step 8 label sync
- `vergil_tooling.data.claude_md_consumer.md` — already exists, used in CLAUDE.md generation

---

## Task 1: Template Data Files

**Files:**
- Create: `src/vergil_tooling/data/githooks_pre_commit.sh`
- Create: `src/vergil_tooling/data/claude_settings.json`
- Create: `src/vergil_tooling/data/licenses/gpl-3.0.txt`
- Create: `src/vergil_tooling/data/licenses/mit.txt`
- Create: `src/vergil_tooling/data/licenses/apache-2.0.txt`

- [ ] **Step 1: Create pre-commit hook template**

Copy the content of `.githooks/pre-commit` into a distributable
template. This is the env-var gate that admits `VRG_COMMIT_CONTEXT=1`
and derived workflows, rejects raw `git commit`.

```bash
cp .githooks/pre-commit src/vergil_tooling/data/githooks_pre_commit.sh
```

- [ ] **Step 2: Create canonical `.claude/settings.json` template**

Create `src/vergil_tooling/data/claude_settings.json`:

```json
{
  "permissions": {
    "allow": ["Bash(vrg-*)"],
    "deny": [
      "Bash(git *)",
      "Bash(*/git *)",
      "Bash(gh *)",
      "Bash(*/gh *)"
    ]
  },
  "extraKnownMarketplaces": {
    "vergil-marketplace": {
      "source": {
        "source": "github",
        "repo": "vergil-project/vergil-claude-plugin"
      }
    }
  },
  "enabledPlugins": {
    "vergil@vergil-marketplace": true
  }
}
```

- [ ] **Step 3: Create license text files**

Create `src/vergil_tooling/data/licenses/` directory with three
files. Each contains the full license text with `{year}` and
`{copyright_holder}` placeholders for MIT and Apache-2.0. GPL-3.0
is the standard FSF text (no placeholders — it uses a preamble
convention).

Download canonical texts:
- `gpl-3.0.txt` — full GPL-3.0-or-later text from
  https://www.gnu.org/licenses/gpl-3.0.txt
- `mit.txt` — standard MIT text with `{year}` and
  `{copyright_holder}` placeholders
- `apache-2.0.txt` — standard Apache-2.0 text from
  https://www.apache.org/licenses/LICENSE-2.0.txt

- [ ] **Step 4: Verify data files are included in package**

Check that `pyproject.toml` already includes the data directory.
The existing config at `[tool.setuptools.package-data]` has:

```toml
vergil_tooling = ["data/*.json", "data/*.md", "configs/*.yaml", "configs/*.toml", "configs/ruby/*.yml"]
```

This needs to be extended to include `data/*.sh` and
`data/licenses/*.txt`:

```toml
vergil_tooling = ["data/*.json", "data/*.md", "data/*.sh", "data/licenses/*.txt", "configs/*.yaml", "configs/*.toml", "configs/ruby/*.yml"]
```

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add template data files for repo init"
```

---

## Task 2: Prompt Helpers and RepoInitContext

**Files:**
- Create: `src/vergil_tooling/lib/repo_init.py`
- Create: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for prompt helpers**

Create `tests/vergil_tooling/test_repo_init.py`:

```python
from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    prompt_choice,
    prompt_yes_no,
    prompt_free_text,
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
            org="vergil-project", name="vergil-vm", adopt=True,
        )
        assert ctx.adopt is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -v
```

Expected: `ModuleNotFoundError` — `repo_init` doesn't exist yet.

- [ ] **Step 3: Implement prompt helpers and context dataclass**

Create `src/vergil_tooling/lib/repo_init.py`:

```python
"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from vergil_tooling.lib import git, github
from vergil_tooling.lib.config import _ENUMS


@dataclass
class RepoInitContext:
    """Mutable state carried through the wizard."""

    org: str
    name: str
    adopt: bool = False
    visibility: str = "public"
    description: str = ""
    work_dir: Path | None = None
    completed_steps: set[int] = field(default_factory=set)

    # vergil.toml fields (populated by step 3 prompts)
    repository_type: str = ""
    primary_language: str = ""
    branching_model: str = ""
    versioning_scheme: str = ""
    release_model: str = ""
    ci_versions: list[str] = field(default_factory=list)
    integration_tests: bool = False
    publish_release: bool = False
    publish_docs: bool = True
    vergil_version: str = "v2.0"
    license_type: str = "GPL-3.0"

    @property
    def repo(self) -> str:
        return f"{self.org}/{self.name}"


def prompt_choice(label: str, options: list[str], *, default: str = "") -> str:
    """Present a numbered list of options and return the chosen value."""
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"  {i}. {opt}{marker}")

    while True:
        hint = f" [{default}]" if default else ""
        raw = input(f"  Choice{hint}: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(options)}.")


def prompt_yes_no(label: str, *, default: bool | None = None) -> bool:
    """Prompt for a yes/no answer."""
    hint_map = {True: " [Y/n]", False: " [y/N]", None: " [y/n]"}
    hint = hint_map[default]
    while True:
        raw = input(f"{label}{hint}: ").strip().lower()
        if not raw and default is not None:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Enter y or n.")


def prompt_free_text(
    label: str, *, default: str = "", required: bool = True,
) -> str:
    """Prompt for free-text input."""
    while True:
        hint = f" [{default}]" if default else ""
        raw = input(f"{label}{hint}: ").strip()
        if not raw and default:
            return default
        if raw:
            return raw
        if not required:
            return ""
        print("  This field is required.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add prompt helpers and RepoInitContext dataclass"
```

---

## Task 3: Checkpoint Detection and Idempotency

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for checkpoint detection**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
from vergil_tooling.lib.repo_init import detect_completed_steps


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestDetectCompletedSteps -v
```

Expected: `ImportError` — `detect_completed_steps` doesn't exist.

- [ ] **Step 3: Implement checkpoint detection**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
import re

_CHECKPOINT_RE = re.compile(r"chore\(init\): step (\d+) -")


def detect_completed_steps(log_output: str) -> set[int]:
    """Parse git log output for checkpoint markers."""
    steps: set[int] = set()
    for line in log_output.splitlines():
        m = _CHECKPOINT_RE.search(line)
        if m:
            steps.add(int(m.group(1)))
    return steps
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestDetectCompletedSteps -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add checkpoint detection for idempotent resume"
```

---

## Task 4: Template Rendering Functions

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for vergil.toml generation**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
import tomllib

from vergil_tooling.lib.repo_init import (
    render_vergil_toml,
    render_claude_md,
    render_readme,
    render_ci_workflow,
    render_cd_workflow,
    render_mkdocs_yml,
    render_gitignore,
)
from vergil_tooling.lib.config import _parse_raw_config


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "Render" -v
```

Expected: `ImportError` — render functions don't exist.

- [ ] **Step 3: Implement template rendering functions**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
from importlib import resources


def _load_data_file(filename: str) -> str:
    """Load a file from the vergil_tooling.data package."""
    return resources.files("vergil_tooling.data").joinpath(filename).read_text(encoding="utf-8")


def _load_license(license_type: str) -> str:
    """Load a license template from vergil_tooling.data.licenses."""
    filename_map = {
        "GPL-3.0": "gpl-3.0.txt",
        "MIT": "mit.txt",
        "Apache-2.0": "apache-2.0.txt",
    }
    filename = filename_map[license_type]
    return (
        resources.files("vergil_tooling.data")
        .joinpath("licenses")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def render_vergil_toml(ctx: RepoInitContext) -> str:
    """Render vergil.toml from wizard answers."""
    ci_versions = ", ".join(f'"{v}"' for v in ctx.ci_versions)
    return (
        "[project]\n"
        f'repository-type = "{ctx.repository_type}"\n'
        f'versioning-scheme = "{ctx.versioning_scheme}"\n'
        f'branching-model = "{ctx.branching_model}"\n'
        f'release-model = "{ctx.release_model}"\n'
        f'primary-language = "{ctx.primary_language}"\n'
        "\n"
        "[ci]\n"
        f"versions = [{ci_versions}]\n"
        f"integration-tests = {'true' if ctx.integration_tests else 'false'}\n"
        "\n"
        "[publish]\n"
        f"release = {'true' if ctx.publish_release else 'false'}\n"
        f"docs = {'true' if ctx.publish_docs else 'false'}\n"
        "\n"
        "[dependencies]\n"
        f'vergil = "{ctx.vergil_version}"\n'
    )


def render_claude_md(ctx: RepoInitContext) -> str:
    """Render CLAUDE.md with project header + consumer template."""
    consumer = _load_data_file("claude_md_consumer.md")
    header = (
        "# CLAUDE.md\n"
        "\n"
        f"This file provides guidance to Claude Code when working in this repository.\n"
        "\n"
        f"**Project name**: {ctx.name}\n"
        "\n"
        "## Validation\n"
        "\n"
        "```bash\n"
        "vrg-docker-run -- vrg-validate\n"
        "```\n"
        "\n"
    )
    return header + consumer


def render_readme(ctx: RepoInitContext) -> str:
    """Render README.md from wizard context."""
    lines = [
        f"# {ctx.name}\n",
        "\n",
        f"{ctx.description}\n",
        "\n",
        "## Status\n",
        "\n",
        "Early development\n",
        "\n",
        "## Overview\n",
        "\n",
        "TODO\n",
        "\n",
        "## Getting Started\n",
        "\n",
    ]
    if ctx.publish_docs:
        lines.append(
            f"See the [documentation](https://{ctx.org}.github.io/{ctx.name}/).\n"
        )
    else:
        lines.append("See the documentation in the `docs/` directory.\n")
    lines.extend([
        "\n",
        "## License\n",
        "\n",
    ])
    if ctx.license_type != "none":
        lines.append(f"{ctx.license_type} — see [LICENSE](LICENSE).\n")
    else:
        lines.append("See [LICENSE](LICENSE).\n")
    return "".join(lines)


def render_gitignore() -> str:
    """Render baseline .gitignore."""
    return (
        "# Editors\n"
        "*.swp\n"
        "*.swo\n"
        "*~\n"
        ".idea/\n"
        ".vscode/\n"
        "\n"
        "# OS\n"
        ".DS_Store\n"
        "Thumbs.db\n"
        "\n"
        "# Vergil\n"
        ".venv-host/\n"
        ".worktrees/\n"
    )


def _container_suffix(language: str) -> str:
    """Map primary language to dev container image suffix."""
    suffix_map = {
        "python": "python",
        "go": "go",
        "java": "java",
        "ruby": "ruby",
        "rust": "rust",
        "shell": "base",
        "none": "base",
        "claude-plugin": "base",
    }
    return suffix_map.get(language, "base")


def _container_tag(language: str, versions: list[str]) -> str:
    """Derive the container tag from language and versions.

    For Python, uses the highest version. For others, uses 'latest'.
    """
    if language == "python" and versions:
        return versions[-1]
    return "latest"


def render_ci_workflow(ctx: RepoInitContext) -> str:
    """Render .github/workflows/ci.yml."""
    suffix = _container_suffix(ctx.primary_language)
    tag = _container_tag(ctx.primary_language, ctx.ci_versions)
    versions_json = json.dumps(ctx.ci_versions)

    lines = [
        "name: CI\n",
        "\n",
        "on:\n",
        "  pull_request:\n",
        "  workflow_call:\n",
        "    inputs:\n",
        "      run-security:\n",
        "        type: boolean\n",
        "        default: true\n",
        "      run-release:\n",
        "        type: boolean\n",
        "        default: true\n",
        "\n",
        "permissions:\n",
        "  contents: read\n",
        "\n",
        "concurrency:\n",
        "  group: ${{ github.workflow }}-${{ github.ref }}\n",
        "  cancel-in-progress: true\n",
        "\n",
        "jobs:\n",
        "  audit:\n",
        "    uses: vergil-project/vergil-actions/.github/workflows/ci-audit.yml@v2.0\n",
        "    with:\n",
        f"      language: {ctx.primary_language}\n",
        f"      versions: '{versions_json}'\n",
        "\n",
        "  quality:\n",
        "    uses: vergil-project/vergil-actions/.github/workflows/ci-quality.yml@v2.0\n",
        "    with:\n",
        f"      language: {ctx.primary_language}\n",
        f"      versions: '{versions_json}'\n",
        f"      container-tag: '{tag}'\n",
        f"      container-suffix: {suffix}\n",
        "\n",
        "  security:\n",
        "    uses: vergil-project/vergil-actions/.github/workflows/ci-security.yml@v2.0\n",
        "    permissions:\n",
        "      contents: read\n",
        "      security-events: write\n",
        "    with:\n",
        f"      language: {ctx.primary_language}\n",
        "      run-standards: ${{ inputs.run-release != 'false' }}\n",
        "      run-security: ${{ inputs.run-security != 'false' }}\n",
        f"      container-tag: '{tag}'\n",
        f"      container-suffix: {suffix}\n",
        "\n",
        "  test:\n",
        "    uses: vergil-project/vergil-actions/.github/workflows/ci-test.yml@v2.0\n",
        "    with:\n",
        f"      language: {ctx.primary_language}\n",
        f"      versions: '{versions_json}'\n",
    ]

    if ctx.release_model != "none":
        lines.extend([
            "\n",
            "  version:\n",
            "    uses: vergil-project/vergil-actions/.github/workflows/ci-version-bump.yml@v2.0\n",
            "    with:\n",
            f"      language: {ctx.primary_language}\n",
            "      run-release: ${{ inputs.run-release != 'false' }}\n",
            f"      container-tag: '{tag}'\n",
            f"      container-suffix: {suffix}\n",
        ])

    return "".join(lines)


def render_cd_workflow(ctx: RepoInitContext) -> str:
    """Render .github/workflows/cd.yml."""
    lines = [
        "name: CD\n",
        "\n",
        "on:\n",
        "  push:\n",
        "    branches: [develop, main]\n",
        "  workflow_dispatch:\n",
        "\n",
        "permissions:\n",
        "  contents: write\n",
        "\n",
        "jobs:\n",
    ]

    if ctx.publish_docs:
        lines.extend([
            "  docs:\n",
            "    uses: vergil-project/vergil-actions/.github/workflows/cd-docs.yml@v2.0\n",
            "    permissions:\n",
            "      contents: write\n",
        ])

    return "".join(lines)


def render_mkdocs_yml(ctx: RepoInitContext) -> str:
    """Render docs/site/mkdocs.yml."""
    return (
        f'site_name: "{ctx.name}"\n'
        f"repo_url: https://github.com/{ctx.org}/{ctx.name}\n"
        "docs_dir: docs\n"
        "strict: true\n"
        'edit_uri: ""\n'
        "\n"
        "theme:\n"
        "  name: material\n"
        "  palette:\n"
        "    - scheme: default\n"
        "      primary: indigo\n"
        "      accent: indigo\n"
        "      toggle:\n"
        "        icon: material/brightness-7\n"
        "        name: Switch to dark mode\n"
        "    - scheme: slate\n"
        "      primary: indigo\n"
        "      accent: indigo\n"
        "      toggle:\n"
        "        icon: material/brightness-4\n"
        "        name: Switch to light mode\n"
        "  features:\n"
        "    - navigation.tabs\n"
        "    - navigation.sections\n"
        "    - navigation.indexes\n"
        "    - navigation.top\n"
        "    - content.code.copy\n"
        "    - search.highlight\n"
        "    - search.suggest\n"
        "\n"
        "plugins:\n"
        "  - search\n"
        "\n"
        "markdown_extensions:\n"
        "  - admonition\n"
        "  - pymdownx.details\n"
        "  - pymdownx.highlight\n"
        "  - pymdownx.superfences\n"
        "  - pymdownx.tabbed:\n"
        "      alternate_style: true\n"
        "  - pymdownx.snippets\n"
        "  - tables\n"
        "  - toc:\n"
        "      permalink: true\n"
        "\n"
        "nav:\n"
        "  - Home: index.md\n"
        "  - Getting Started: getting-started.md\n"
    )


def render_docs_index(ctx: RepoInitContext) -> str:
    """Render docs/site/docs/index.md."""
    return f"# {ctx.name}\n\nWelcome to the {ctx.name} documentation.\n"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "Render" -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add template rendering functions for all generated files"
```

---

## Task 5: Wizard Steps 1-2 (Repo Creation and Clone)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for step 1 (repo creation)**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
from unittest.mock import call


class TestStepCreateRepo:
    def test_creates_new_repo(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project", name="vergil-vm",
            visibility="public", description="Test repo",
        )
        calls: list[tuple[str, ...]] = []

        def mock_run(*args: str) -> None:
            calls.append(args)

        with patch("vergil_tooling.lib.repo_init.github.run", side_effect=mock_run):
            step_create_repo(ctx)

        assert any("repo" in c and "create" in c for c in calls)

    def test_skips_when_repo_exists_adopt(self) -> None:
        ctx = RepoInitContext(
            org="vergil-project", name="vergil-vm", adopt=True,
        )

        def mock_read_output(*args: str) -> str:
            return "vergil-project/vergil-vm"

        with patch(
            "vergil_tooling.lib.repo_init.github.read_output",
            side_effect=mock_read_output,
        ):
            step_create_repo(ctx)
        # No exception means it verified successfully


class TestStepClone:
    def test_clones_to_work_dir(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        target = tmp_path / "vergil-vm"

        calls: list[tuple[str, ...]] = []

        def mock_subprocess_run(cmd: tuple[str, ...], **kw: Any) -> None:
            calls.append(cmd)
            target.mkdir(exist_ok=True)
            (target / ".git").mkdir()

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            step_clone(ctx, parent_dir=tmp_path)

        assert ctx.work_dir == target

    def test_skips_when_already_cloned(self, tmp_path: Path) -> None:
        target = tmp_path / "vergil-vm"
        target.mkdir()
        (target / ".git").mkdir()
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        step_clone(ctx, parent_dir=tmp_path)
        assert ctx.work_dir == target
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "StepCreate or StepClone" -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement steps 1 and 2**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
def step_create_repo(ctx: RepoInitContext) -> None:
    """Step 1: Create the GitHub repo or verify it exists."""
    if ctx.adopt:
        print(f"Step 1: Verifying {ctx.repo} exists...")
        github.read_output("repo", "view", ctx.repo, "--json", "name")
        print(f"  {ctx.repo} exists.")
        return

    print(f"Step 1: Creating {ctx.repo}...")
    try:
        github.read_output("repo", "view", ctx.repo, "--json", "name")
        print(f"  {ctx.repo} already exists, skipping creation.")
        return
    except subprocess.CalledProcessError:
        pass

    cmd = [
        "repo", "create", ctx.repo,
        f"--{ctx.visibility}",
        "--description", ctx.description,
    ]
    github.run(*cmd)
    print(f"  Created {ctx.repo}.")


def step_clone(ctx: RepoInitContext, *, parent_dir: Path | None = None) -> None:
    """Step 2: Clone the repo locally or verify an existing clone."""
    if parent_dir is None:
        parent_dir = Path.cwd()

    target = parent_dir / ctx.name

    if ctx.adopt:
        ctx.work_dir = Path.cwd()
        print(f"Step 2: Using current directory as working directory.")
        return

    if (target / ".git").is_dir():
        ctx.work_dir = target
        print(f"Step 2: {target} already cloned, skipping.")
        return

    print(f"Step 2: Cloning {ctx.repo}...")
    subprocess.run(
        ("git", "clone", f"git@github.com:{ctx.repo}.git", str(target)),
        check=True,
    )
    ctx.work_dir = target
    print(f"  Cloned to {target}.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "StepCreate or StepClone" -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add wizard steps 1-2: repo creation and clone"
```

---

## Task 6: Wizard Step 3 (Interactive Config Generation)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for step 3**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
class TestStepGenerateConfig:
    def test_prompts_and_writes_vergil_toml(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        inputs = iter([
            "4",    # repository-type: tooling
            "6",    # primary-language: shell
            "1",    # branching-model: library-release
            "2",    # versioning-scheme: semver
            "2",    # release-model: tagged-release
            "latest",  # ci versions
            "n",    # integration tests
            "y",    # publish releases
            "y",    # publish docs
            "",     # vergil version (default v2.0)
            "1",    # license: GPL-3.0
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
        self, tmp_path: Path,
    ) -> None:
        ctx = RepoInitContext(
            org="vergil-project", name="vergil-vm", adopt=True,
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepGenerateConfig -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement step 3**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
def _default_ci_versions(language: str) -> str:
    """Return sensible default CI versions for a language."""
    defaults: dict[str, str] = {
        "python": "3.12, 3.13, 3.14",
        "go": "1.23",
        "java": "21",
        "ruby": "3.3",
        "rust": "stable",
        "shell": "latest",
        "none": "latest",
        "claude-plugin": "latest",
    }
    return defaults.get(language, "latest")


def _load_existing_config(work_dir: Path) -> dict[str, Any] | None:
    """Load existing vergil.toml if present. Returns raw TOML dict."""
    toml_path = work_dir / "vergil.toml"
    if not toml_path.is_file():
        return None
    import tomllib

    with toml_path.open("rb") as f:
        return tomllib.load(f)


def step_generate_config(ctx: RepoInitContext) -> None:
    """Step 3: Interactive vergil.toml generation."""
    print("Step 3: Generating vergil.toml...")

    existing = _load_existing_config(ctx.work_dir) if ctx.adopt else None
    project = existing.get("project", {}) if existing else {}
    ci_raw = existing.get("ci", {}) if existing else {}
    pub_raw = existing.get("publish", {}) if existing else {}
    deps = existing.get("dependencies", {}) if existing else {}

    enum_fields = [
        ("repository_type", "repository-type", "Repository type"),
        ("primary_language", "primary-language", "Primary language"),
        ("branching_model", "branching-model", "Branching model"),
        ("versioning_scheme", "versioning-scheme", "Versioning scheme"),
        ("release_model", "release-model", "Release model"),
    ]

    for attr, toml_key, label in enum_fields:
        options = sorted(_ENUMS[toml_key])
        default = project.get(toml_key, "")
        value = prompt_choice(label, options, default=default)
        setattr(ctx, attr, value)

    default_versions = _default_ci_versions(ctx.primary_language)
    existing_versions = ", ".join(ci_raw.get("versions", []))
    raw_versions = prompt_free_text(
        "CI versions (comma-separated)",
        default=existing_versions or default_versions,
    )
    ctx.ci_versions = [v.strip() for v in raw_versions.split(",")]

    ctx.integration_tests = prompt_yes_no(
        "Integration tests?",
        default=ci_raw.get("integration-tests", False),
    )

    release_default = ctx.release_model != "none"
    ctx.publish_release = prompt_yes_no(
        "Publish releases?",
        default=pub_raw.get("release", release_default),
    )

    ctx.publish_docs = prompt_yes_no(
        "Publish docs?",
        default=pub_raw.get("docs", True),
    )

    ctx.vergil_version = prompt_free_text(
        "Vergil dependency version",
        default=deps.get("vergil", "v2.0"),
    )

    license_options = ["GPL-3.0", "MIT", "Apache-2.0", "none"]
    ctx.license_type = prompt_choice("License", license_options, default="GPL-3.0")

    content = render_vergil_toml(ctx)
    (ctx.work_dir / "vergil.toml").write_text(content)

    git.run("add", "vergil.toml")
    git.run("commit", "-m", "chore(init): step 3 - vergil.toml")
    print("  vergil.toml committed.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepGenerateConfig -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add wizard step 3: interactive vergil.toml generation"
```

---

## Task 7: Wizard Step 4 (Scaffold Config Files)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for step 4**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepScaffoldConfigFiles -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement step 4**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
import stat
from datetime import datetime, timezone


def step_scaffold_config_files(ctx: RepoInitContext) -> None:
    """Step 4: Scaffold local config files."""
    print("Step 4: Scaffolding config files...")
    wd = ctx.work_dir
    assert wd is not None

    # .githooks/pre-commit
    hooks_dir = wd / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_content = _load_data_file("githooks_pre_commit.sh")
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(hook_content)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Activate hooks
    git.run("config", "core.hooksPath", ".githooks")

    # CLAUDE.md
    claude_md = render_claude_md(ctx)
    (wd / "CLAUDE.md").write_text(claude_md)

    # .claude/settings.json
    claude_dir = wd / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings = _load_data_file("claude_settings.json")
    (claude_dir / "settings.json").write_text(settings)

    # README.md
    readme = render_readme(ctx)
    (wd / "README.md").write_text(readme)

    # .gitignore
    gitignore = render_gitignore()
    (wd / ".gitignore").write_text(gitignore)

    # LICENSE
    if ctx.license_type != "none":
        year = datetime.now(tz=timezone.utc).year
        license_text = _load_license(ctx.license_type)
        license_text = license_text.replace("{year}", str(year))
        license_text = license_text.replace("{copyright_holder}", ctx.org)
        (wd / "LICENSE").write_text(license_text)

    git.run("add", "-A")
    git.run("commit", "-m", "chore(init): step 4 - config files")
    print("  Config files committed.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepScaffoldConfigFiles -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add wizard step 4: scaffold config files"
```

---

## Task 8: Wizard Steps 5-6 (CI/CD Workflows and Docs Site)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for steps 5 and 6**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "StepCiCd or StepDocsSite" -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement steps 5 and 6**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
def step_ci_cd_workflows(ctx: RepoInitContext) -> None:
    """Step 5: Generate CI and CD workflow files."""
    print("Step 5: Generating CI/CD workflows...")
    wd = ctx.work_dir
    assert wd is not None

    workflows_dir = wd / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    ci_content = render_ci_workflow(ctx)
    (workflows_dir / "ci.yml").write_text(ci_content)

    if ctx.publish_docs:
        cd_content = render_cd_workflow(ctx)
        (workflows_dir / "cd.yml").write_text(cd_content)

    git.run("add", "-A")
    git.run("commit", "-m", "chore(init): step 5 - CI/CD workflows")
    print("  Workflows committed.")


def step_docs_site(ctx: RepoInitContext) -> None:
    """Step 6: Scaffold the docs site."""
    if not ctx.publish_docs:
        print("Step 6: Docs disabled, skipping.")
        return

    print("Step 6: Scaffolding docs site...")
    wd = ctx.work_dir
    assert wd is not None

    docs_dir = wd / "docs" / "site" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    mkdocs = render_mkdocs_yml(ctx)
    (wd / "docs" / "site" / "mkdocs.yml").write_text(mkdocs)

    index = render_docs_index(ctx)
    (docs_dir / "index.md").write_text(index)

    # Create getting-started.md stub (referenced in nav)
    (docs_dir / "getting-started.md").write_text(
        f"# Getting Started\n\nTODO: Add getting started guide for {ctx.name}.\n"
    )

    git.run("add", "-A")
    git.run("commit", "-m", "chore(init): step 6 - docs site")
    print("  Docs site committed.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "StepCiCd or StepDocsSite" -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add wizard steps 5-6: CI/CD workflows and docs site"
```

---

## Task 9: Wizard Steps 7-9 (Branch Structure, GitHub Config, Pages)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for steps 7-9**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
class TestStepBranchStructure:
    def test_pushes_develop_and_creates_main(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")

        calls: list[tuple[str, ...]] = []

        def mock_git_run(*args: str) -> None:
            calls.append(args)

        def mock_read_output(*args: str) -> str:
            return ""

        with (
            patch("vergil_tooling.lib.repo_init.git.run", side_effect=mock_git_run),
            patch("vergil_tooling.lib.repo_init.git.read_output", side_effect=mock_read_output),
            patch("vergil_tooling.lib.repo_init.github.run"),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
        ):
            step_branch_structure(ctx)

        assert ("branch", "-m", "main", "develop") in calls or \
               ("checkout", "-b", "develop") in calls
        assert any("push" in c for c in calls)


class TestStepGithubConfig:
    def test_applies_config_and_labels(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = Path("/tmp/test")

        config_applied = []
        labels_synced = []

        def mock_apply(*a: Any, **kw: Any) -> list[str]:
            config_applied.append(True)
            return []

        def mock_sync(repo: str) -> None:
            labels_synced.append(repo)

        with (
            patch("vergil_tooling.lib.repo_init.fetch_actual_state"),
            patch("vergil_tooling.lib.repo_init.compute_desired_state"),
            patch("vergil_tooling.lib.repo_init.apply_desired_state", side_effect=mock_apply),
            patch("vergil_tooling.lib.repo_init.sync_labels", side_effect=mock_sync),
            patch("vergil_tooling.lib.repo_init.config_module.read_config"),
        ):
            step_github_config(ctx)

        assert config_applied
        assert ctx.repo in labels_synced


class TestStepGithubPages:
    def test_creates_gh_pages_branch_and_configures(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = True

        gh_calls: list[tuple[str, ...]] = []
        write_json_calls: list[tuple[str, str, dict[str, object]]] = []

        def mock_gh_run(*args: str) -> None:
            gh_calls.append(args)

        def mock_write_json(method: str, endpoint: str, body: dict[str, object]) -> None:
            write_json_calls.append((method, endpoint, body))

        with (
            patch("vergil_tooling.lib.repo_init.github.run", side_effect=mock_gh_run),
            patch("vergil_tooling.lib.repo_init.github.write_json", side_effect=mock_write_json),
            patch("vergil_tooling.lib.repo_init._remote_branch_exists", return_value=False),
            patch("vergil_tooling.lib.repo_init.git.run"),
            patch("vergil_tooling.lib.repo_init.git.read_output", return_value="abc123"),
        ):
            step_github_pages(ctx)

        assert any("repos/vergil-project/vergil-vm/pages" in c[1] for c in write_json_calls)

    def test_skips_when_docs_disabled(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.publish_docs = False

        step_github_pages(ctx)
        # No exception, no calls
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "StepBranch or StepGithub" -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement steps 7-9**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
from vergil_tooling.lib import config as config_module
from vergil_tooling.lib.github_config import (
    apply_desired_state,
    compute_desired_state,
    fetch_actual_state,
)
from vergil_tooling.lib.labels import load_labels


def sync_labels(repo: str) -> None:
    """Provision all canonical labels into a repo."""
    registry = load_labels()
    for label in registry["labels"]:
        cmd: list[str] = [
            "label", "create", label["name"],
            "--repo", repo, "--force",
        ]
        if label.get("color"):
            cmd.extend(["--color", label["color"]])
        if label.get("description"):
            cmd.extend(["--description", label["description"]])
        github.run(*cmd)


def _remote_branch_exists(repo: str, branch: str) -> bool:
    """Check if a branch exists on the remote."""
    try:
        github.read_output(
            "api", f"repos/{repo}/branches/{branch}", "--jq", ".name",
        )
        return True
    except subprocess.CalledProcessError:
        return False


def step_branch_structure(ctx: RepoInitContext) -> None:
    """Step 7: Set up develop + main branches."""
    print("Step 7: Setting up branch structure...")

    develop_exists = _remote_branch_exists(ctx.repo, "develop")
    main_exists = _remote_branch_exists(ctx.repo, "main")

    if not develop_exists:
        try:
            git.run("branch", "-m", "main", "develop")
        except subprocess.CalledProcessError:
            git.run("checkout", "-b", "develop")
        git.run("push", "-u", "origin", "develop")
        print("  Pushed develop.")

    if not main_exists:
        git.run("branch", "main")
        git.run("push", "-u", "origin", "main")
        print("  Pushed main.")

    # Set develop as default branch
    github.run("repo", "edit", ctx.repo, "--default-branch", "develop")
    print("  Default branch set to develop.")


def step_github_config(ctx: RepoInitContext) -> None:
    """Step 8: Apply GitHub config and labels."""
    print("Step 8: Applying GitHub config...")

    cfg = config_module.read_config(ctx.work_dir)
    result = fetch_actual_state(ctx.repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(cfg, visibility=result.visibility, is_org=is_org)
    removed = apply_desired_state(ctx.repo, desired)
    if removed:
        print(f"  Legacy protection removed: {', '.join(removed)}")
    print("  GitHub config applied.")

    print("  Syncing labels...")
    sync_labels(ctx.repo)
    print("  Labels synced.")


def step_github_pages(ctx: RepoInitContext) -> None:
    """Step 9: Configure GitHub Pages."""
    if not ctx.publish_docs:
        print("Step 9: Docs disabled, skipping Pages.")
        return

    print("Step 9: Configuring GitHub Pages...")

    # Create gh-pages branch if it doesn't exist
    if not _remote_branch_exists(ctx.repo, "gh-pages"):
        current_sha = git.read_output("rev-parse", "HEAD")
        git.run("checkout", "--orphan", "gh-pages")
        git.run("reset", "--hard")
        git.run("commit", "--allow-empty", "-m", "chore: initialize gh-pages")
        git.run("push", "origin", "gh-pages")
        git.run("checkout", "develop")
        print("  Created gh-pages branch.")

    # Configure Pages source
    github.write_json(
        "POST",
        f"repos/{ctx.repo}/pages",
        {"source": {"branch": "gh-pages", "path": "/"}},
    )
    print("  Pages source configured.")

    # Set homepage URL
    homepage = f"https://{ctx.org}.github.io/{ctx.name}/"
    github.write_json(
        "PATCH",
        f"repos/{ctx.repo}",
        {"homepage": homepage},
    )
    print(f"  Homepage set to {homepage}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -k "StepBranch or StepGithub" -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add wizard steps 7-9: branches, GitHub config, Pages"
```

---

## Task 10: Wizard Orchestrator

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write tests for the orchestrator**

Add to `tests/vergil_tooling/test_repo_init.py`:

```python
class TestRunWizard:
    def test_skips_completed_local_steps(self) -> None:
        ctx = RepoInitContext(org="vergil-project", name="test")
        ctx.completed_steps = {3, 4}

        steps_run: list[int] = []

        def mock_step(step_num: int):
            def inner(ctx: RepoInitContext, **kw: Any) -> None:
                steps_run.append(step_num)
            return inner

        with (
            patch("vergil_tooling.lib.repo_init.step_create_repo", side_effect=mock_step(1)),
            patch("vergil_tooling.lib.repo_init.step_clone", side_effect=mock_step(2)),
            patch("vergil_tooling.lib.repo_init.step_generate_config", side_effect=mock_step(3)),
            patch("vergil_tooling.lib.repo_init.step_scaffold_config_files", side_effect=mock_step(4)),
            patch("vergil_tooling.lib.repo_init.step_ci_cd_workflows", side_effect=mock_step(5)),
            patch("vergil_tooling.lib.repo_init.step_docs_site", side_effect=mock_step(6)),
            patch("vergil_tooling.lib.repo_init.step_branch_structure", side_effect=mock_step(7)),
            patch("vergil_tooling.lib.repo_init.step_github_config", side_effect=mock_step(8)),
            patch("vergil_tooling.lib.repo_init.step_github_pages", side_effect=mock_step(9)),
            patch("vergil_tooling.lib.repo_init._check_remote_steps", return_value=set()),
            patch("vergil_tooling.lib.repo_init.git.read_output", return_value=""),
        ):
            run_wizard(ctx)

        assert 3 not in steps_run
        assert 4 not in steps_run
        assert 5 in steps_run
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestRunWizard -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement the orchestrator**

Add to `src/vergil_tooling/lib/repo_init.py`:

```python
def _check_remote_steps(ctx: RepoInitContext) -> set[int]:
    """Check which remote-only steps are already complete."""
    completed: set[int] = set()

    # Step 1: repo exists?
    try:
        github.read_output("repo", "view", ctx.repo, "--json", "name")
        completed.add(1)
    except subprocess.CalledProcessError:
        pass

    # Step 7: both branches exist?
    if (
        _remote_branch_exists(ctx.repo, "develop")
        and _remote_branch_exists(ctx.repo, "main")
    ):
        completed.add(7)

    return completed


def run_wizard(ctx: RepoInitContext) -> None:
    """Run all wizard steps, skipping completed ones."""
    # Detect completed checkpoint steps from git log
    try:
        log_output = git.read_output("log", "--oneline")
    except subprocess.CalledProcessError:
        log_output = ""
    local_completed = detect_completed_steps(log_output)

    # Detect completed remote steps
    remote_completed = _check_remote_steps(ctx)

    ctx.completed_steps = local_completed | remote_completed

    if ctx.completed_steps:
        print(f"Resuming — completed steps: {sorted(ctx.completed_steps)}")

    steps: list[tuple[int, str, Any]] = [
        (1, "Repo creation", lambda: step_create_repo(ctx)),
        (2, "Clone", lambda: step_clone(ctx)),
        (3, "vergil.toml", lambda: step_generate_config(ctx)),
        (4, "Config files", lambda: step_scaffold_config_files(ctx)),
        (5, "CI/CD workflows", lambda: step_ci_cd_workflows(ctx)),
        (6, "Docs site", lambda: step_docs_site(ctx)),
        (7, "Branch structure", lambda: step_branch_structure(ctx)),
        (8, "GitHub config", lambda: step_github_config(ctx)),
        (9, "GitHub Pages", lambda: step_github_pages(ctx)),
    ]

    for step_num, desc, func in steps:
        if step_num in ctx.completed_steps:
            print(f"Step {step_num} ({desc}): already completed, skipping.")
            continue
        func()

    print("\nRepository bootstrap complete!")
    print(f"  Repo: https://github.com/{ctx.repo}")
    if ctx.publish_docs:
        print(f"  Docs: https://{ctx.org}.github.io/{ctx.name}/")
    print("  Ready for PRs.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestRunWizard -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope 807 --message "add wizard orchestrator with idempotent step skipping"
```

---

## Task 11: CLI Entry Point and Registration

**Files:**
- Create: `src/vergil_tooling/bin/vrg_github_repo_init.py`
- Create: `tests/vergil_tooling/test_vrg_github_repo_init.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write tests for CLI argument parsing**

Create `tests/vergil_tooling/test_vrg_github_repo_init.py`:

```python
from __future__ import annotations

import pytest

from vergil_tooling.bin.vrg_github_repo_init import parse_args


class TestParseArgs:
    def test_new_repo(self) -> None:
        args = parse_args(["vergil-project/vergil-vm"])
        assert args.repo == "vergil-project/vergil-vm"
        assert args.adopt is False

    def test_adopt_mode(self) -> None:
        args = parse_args(["--adopt"])
        assert args.adopt is True
        assert args.repo is None

    def test_visibility(self) -> None:
        args = parse_args(["vergil-project/vergil-vm", "--visibility", "private"])
        assert args.visibility == "private"

    def test_repo_format_validation(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["just-a-name"])

    def test_adopt_with_repo_is_error(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["vergil-project/vergil-vm", "--adopt"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_github_repo_init.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement CLI entry point**

Create `src/vergil_tooling/bin/vrg_github_repo_init.py`:

```python
"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    prompt_choice,
    prompt_free_text,
    prompt_yes_no,
    run_wizard,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Bootstrap a VERGIL-managed repository.",
    )

    parser.add_argument(
        "repo",
        nargs="?",
        help="Repository to create (ORG/NAME format)",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help="Adopt an existing repo (run from inside its clone)",
    )
    parser.add_argument(
        "--visibility",
        choices=("public", "private"),
        help="Repository visibility (new repos only)",
    )

    args = parser.parse_args(argv)

    if args.adopt and args.repo:
        parser.error("--adopt cannot be used with a repo argument")

    if not args.adopt and not args.repo:
        parser.error("provide ORG/NAME or use --adopt from inside a clone")

    if args.repo and "/" not in args.repo:
        parser.error("repo must be in ORG/NAME format")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.adopt:
        from vergil_tooling.lib import github

        repo_slug = github.current_repo()
        org, name = repo_slug.split("/", 1)

        print(f"Adopting {repo_slug}...")
        print(
            "WARNING: This will overwrite all Vergil-managed files to canonical state.\n"
            "Files affected: vergil.toml, CLAUDE.md, .claude/settings.json,\n"
            ".githooks/pre-commit, LICENSE, README.md, .gitignore, CI/CD workflows,\n"
            "docs site config, GitHub settings, rulesets, and labels."
        )
        if not prompt_yes_no("Continue?", default=False):
            print("Aborted.")
            return 1

        ctx = RepoInitContext(org=org, name=name, adopt=True)
    else:
        org, name = args.repo.split("/", 1)
        ctx = RepoInitContext(org=org, name=name)

        # Pre-step 1 prompts
        if args.visibility:
            ctx.visibility = args.visibility
        else:
            ctx.visibility = prompt_choice(
                "Repository visibility", ["public", "private"], default="public",
            )

        ctx.description = prompt_free_text("Project description (one paragraph)")

    run_wizard(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register the entry point in pyproject.toml**

Add to `[project.scripts]` in `pyproject.toml`:

```toml
vrg-github-repo-init = "vergil_tooling.bin.vrg_github_repo_init:main"
```

Insert alphabetically after `vrg-github-repo-config`.

- [ ] **Step 5: Run tests to verify they pass**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_github_repo_init.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope 807 --message "add vrg-github-repo-init CLI entry point"
```

---

## Task 12: Full Validation

**Files:** None (validation only)

- [ ] **Step 1: Run the full test suite**

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_init.py tests/vergil_tooling/test_vrg_github_repo_init.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run vrg-validate**

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: all checks pass (lint, typecheck, tests, audit).

- [ ] **Step 3: Commit any fixes needed**

If validation found issues, fix them and commit:

```
vrg-commit --type style --scope 807 --message "fix lint and type errors in repo init"
```

---

## Task 13: Integration Smoke Test

**Files:** None (manual test)

This task is a manual test using the real `vergil-vm` repo. Run
`vrg-github-repo-init vergil-project/vergil-vm` and verify:

- [ ] **Step 1: Run the wizard**

```bash
vrg-github-repo-init vergil-project/vergil-vm
```

Walk through the prompts using the vergil-docker profile:
- Visibility: public
- Description: "Lima VM image definitions for Vergil identity VMs"
- Repository type: tooling (but consider infrastructure)
- Primary language: shell
- Branching model: library-release
- Versioning scheme: semver
- Release model: tagged-release
- CI versions: latest
- Integration tests: no
- Publish releases: yes
- Publish docs: yes
- Vergil version: v2.0
- License: GPL-3.0

- [ ] **Step 2: Verify the result**

Check:
- [ ] Repo exists at github.com/vergil-project/vergil-vm
- [ ] Both `develop` and `main` branches exist
- [ ] `develop` is the default branch
- [ ] `vrg-github-repo-config audit --repo vergil-project/vergil-vm` passes
- [ ] GitHub Pages is configured and serving
- [ ] Standard labels exist
- [ ] All generated files are present and correct

- [ ] **Step 3: Test idempotency**

Re-run `vrg-github-repo-init vergil-project/vergil-vm` and
verify it skips all completed steps without errors.

- [ ] **Step 4: Commit final docs update**

```
vrg-commit --type docs --scope 807 --message "mark design spec as implemented"
```
