"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

_CHECKPOINT_RE = re.compile(r"chore\(init\): step (\d+) -")


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


def detect_completed_steps(log_output: str) -> set[int]:
    """Parse git log output for checkpoint markers."""
    steps: set[int] = set()
    for line in log_output.splitlines():
        m = _CHECKPOINT_RE.search(line)
        if m:
            steps.add(int(m.group(1)))
    return steps


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
    label: str,
    *,
    default: str = "",
    required: bool = True,
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


def _load_data_file(filename: str) -> str:
    """Load a file from the vergil_tooling.data package."""
    ref = resources.files("vergil_tooling.data").joinpath(filename)
    return ref.read_text(encoding="utf-8")


def _load_license(license_type: str) -> str:
    """Load a license template from vergil_tooling.data.licenses."""
    filename_map = {
        "GPL-3.0": "gpl-3.0.txt",
        "MIT": "mit.txt",
        "Apache-2.0": "apache-2.0.txt",
    }
    filename = filename_map[license_type]
    ref = resources.files("vergil_tooling.data").joinpath("licenses").joinpath(filename)
    return ref.read_text(encoding="utf-8")


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
        "This file provides guidance to Claude Code when working in this repository.\n"
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
    """Derive the container tag from language and versions."""
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
