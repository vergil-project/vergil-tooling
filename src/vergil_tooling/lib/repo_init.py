"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from vergil_tooling.lib import git, github, repo_config
from vergil_tooling.lib.config import _ENUMS

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
    vergil_version: str = "v2.1"
    license_type: str = "GPL-3.0"
    initial_version: str = "0.1.0"

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


def prompt_language(*, default: str = "") -> str:
    """Prompt for the primary language, with an explicit no-language option.

    "No primary language" is the *absence* of a language, not a sixth language.
    It is presented as a separate "0. None of the above" choice, set apart from
    the real languages, and maps to an empty string so the caller omits the
    ``primary-language`` key entirely (see issue #1579). This is deliberately
    asymmetric with the other enums (e.g. ``versioning-scheme``), which encode
    their no-op as a literal ``none`` member.
    """
    languages = sorted(_ENUMS["primary-language"])
    print("\nPrimary language:")
    for i, lang in enumerate(languages, 1):
        marker = " (default)" if lang == default else ""
        print(f"  {i}. {lang}{marker}")
    print()
    none_marker = " (default)" if not default else ""
    print(f"  0. None of the above — this repo has no primary language{none_marker}")

    while True:
        hint = f" [{default}]" if default else " [none]"
        raw = input(f"  Choice{hint}: ").strip()
        if not raw:
            return default  # may be "" → no primary language
        if raw == "0":
            return ""
        try:
            idx = int(raw)
            if 1 <= idx <= len(languages):
                return languages[idx - 1]
        except ValueError:
            pass
        print(f"  Enter 0–{len(languages)}.")


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
    lang_line = f'primary-language = "{ctx.primary_language}"\n' if ctx.primary_language else ""
    return (
        "[project]\n"
        f'repository-type = "{ctx.repository_type}"\n'
        f'versioning-scheme = "{ctx.versioning_scheme}"\n'
        f'branching-model = "{ctx.branching_model}"\n'
        f'release-model = "{ctx.release_model}"\n'
        f"{lang_line}"
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
    """Render CLAUDE.md with project header + marker-delimited consumer template."""
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
        "vrg-container-run -- vrg-validate\n"
        "```\n"
        "\n"
    )
    return (
        header
        + repo_config.CLAUDE_MD_MARKER_BEGIN
        + "\n"
        + consumer
        + repo_config.CLAUDE_MD_MARKER_END
        + "\n"
    )


def render_readme(ctx: RepoInitContext) -> str:
    """Render README.md from wizard context."""
    lines = [
        f"# {ctx.name}\n",
        "\n",
        f"{ctx.description}\n",
        "\n",
        "## Table of Contents\n",
        "\n",
        "- [Status](#status)\n",
        "- [Overview](#overview)\n",
        "- [Getting Started](#getting-started)\n",
        "- [License](#license)\n",
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
        lines.append(f"See the [documentation](https://{ctx.org}.github.io/{ctx.name}/).\n")
    else:
        lines.append("See the documentation in the `docs/` directory.\n")
    lines.extend(
        [
            "\n",
            "## License\n",
            "\n",
        ]
    )
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
        ".vergil/\n"
        ".superpowers/\n"
        "build/\n"
    )


def _container_suffix(language: str | None) -> str:
    """Map primary language to dev container image suffix."""
    if language is None:
        return "base"
    suffix_map = {
        "python": "python",
        "go": "go",
        "java": "java",
        "ruby": "ruby",
        "rust": "rust",
    }
    return suffix_map.get(language, "base")


def _container_tag(language: str | None, versions: list[str]) -> str:
    """Derive the container tag from language and versions."""
    if language == "python" and versions:
        return versions[-1]
    return "latest"


_CODEQL_LANGUAGES = frozenset(
    {
        "python",
        "go",
        "java",
        "ruby",
        "cpp",
        "csharp",
        "javascript",
        "typescript",
        "swift",
        "kotlin",
    }
)


def render_ci_workflow(ctx: RepoInitContext) -> str:
    """Render .github/workflows/ci.yml."""
    from vergil_tooling.lib.languages import CheckKind, language_commands

    lang_yaml = ctx.primary_language or ""
    suffix = _container_suffix(ctx.primary_language)
    tag = _container_tag(ctx.primary_language, ctx.ci_versions)
    versions_json = json.dumps(ctx.ci_versions)

    has_audit = len(language_commands(ctx.primary_language, CheckKind.AUDIT)) > 0
    has_test = (
        len(language_commands(ctx.primary_language, CheckKind.TEST)) > 0 or ctx.integration_tests
    )

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
    ]

    if has_audit:
        lines.extend(
            [
                "  audit:\n",
                "    uses: vergil-project/vergil-actions/.github/workflows/ci-audit.yml@v2.1\n",
                "    with:\n",
                f"      language: {lang_yaml}\n",
                f"      versions: '{versions_json}'\n",
                f"      container-tag: '{tag}'\n",
                f"      container-suffix: {suffix}\n",
                "\n",
            ]
        )

    lines.extend(
        [
            "  quality:\n",
            "    uses: vergil-project/vergil-actions/.github/workflows/ci-quality.yml@v2.1\n",
            "    with:\n",
            f"      language: {lang_yaml}\n",
            f"      versions: '{versions_json}'\n",
            f"      container-tag: '{tag}'\n",
            f"      container-suffix: {suffix}\n",
            "\n",
            "  security:\n",
            "    uses: vergil-project/vergil-actions/.github/workflows/ci-security.yml@v2.1\n",
            "    permissions:\n",
            "      contents: read\n",
            "      security-events: write\n",
            "      # Requested by ci-security.yml@v2.1.3: codeql-action/upload-sarif\n",
            "      # needs it on private repos. See vergil-project/vergil-actions#698.\n",
            "      actions: read\n",
            "    with:\n",
            f"      language: {lang_yaml}\n",
            "      run-standards: ${{ inputs.run-release != 'false' }}\n",
            "      run-security: ${{ inputs.run-security != 'false' }}\n",
        ]
    )

    if ctx.visibility == "private":
        lines.extend(
            [
                "      # Private repo without GHAS: scanners gate via exit codes,\n",
                "      # SARIF is kept as build artifacts, and CodeQL is skipped.\n",
                "      # See vergil-project/vergil-actions#693; to enable GHAS later\n",
                "      # see docs/specs/2026-06-06-ghas-posture-design.md.\n",
                "      upload-sarif: false\n",
            ]
        )

    lines.extend(
        [
            f"      container-tag: '{tag}'\n",
            f"      container-suffix: {suffix}\n",
        ]
    )

    if ctx.primary_language not in _CODEQL_LANGUAGES:
        lines.append("      run-codeql: false\n")

    if has_test:
        lines.extend(
            [
                "\n",
                "  test:\n",
                "    uses: vergil-project/vergil-actions/.github/workflows/ci-test.yml@v2.1\n",
                "    with:\n",
                f"      language: {lang_yaml}\n",
                f"      versions: '{versions_json}'\n",
                f"      container-tag: '{tag}'\n",
                f"      container-suffix: {suffix}\n",
            ]
        )

    if ctx.release_model != "none":
        lines.extend(
            [
                "\n",
                "  version:\n",
                "    uses: vergil-project/vergil-actions/"
                ".github/workflows/ci-version-bump.yml@v2.1\n",
                "    with:\n",
                f"      language: {lang_yaml}\n",
                "      run-release: ${{ inputs.run-release != 'false' }}\n",
                f"      container-tag: '{tag}'\n",
                f"      container-suffix: {suffix}\n",
            ]
        )

    return "".join(lines)


def render_cd_workflow(ctx: RepoInitContext) -> str:
    """Render .github/workflows/cd.yml."""
    permissions = ["  contents: write\n"]
    if ctx.publish_release:
        permissions = [
            "  attestations: write\n",
            "  contents: write\n",
            "  id-token: write\n",
            "  pull-requests: write\n",
        ]

    lines = [
        "name: CD\n",
        "\n",
        "on:\n",
        "  push:\n",
        "    branches: [develop, main]\n",
        "  workflow_dispatch:\n",
        "\n",
        "permissions:\n",
        *permissions,
        "\n",
        "jobs:\n",
    ]

    if ctx.publish_docs:
        lines.extend(
            [
                "  docs:\n",
                "    uses: vergil-project/vergil-actions/.github/workflows/cd-docs.yml@v2.1\n",
                "    permissions:\n",
                "      contents: write\n",
            ]
        )

    if ctx.publish_release:
        suffix = _container_suffix(ctx.primary_language)
        tag = _container_tag(ctx.primary_language, ctx.ci_versions)
        lines.append("\n")
        lines.extend(
            [
                "  release:\n",
                "    if: github.ref == 'refs/heads/main'\n",
                "    uses: vergil-project/vergil-actions/.github/workflows/cd-release.yml@v2.1\n",
                "    with:\n",
                f"      language: {suffix}\n",
                f'      container-tag: "{tag}"\n',
                "    secrets: inherit\n",
            ]
        )

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
        "repo",
        "create",
        ctx.repo,
        f"--{ctx.visibility}",
        "--description",
        ctx.description,
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
        print("Step 2: Using current directory as working directory.")
        return

    if (target / ".git").is_dir():
        ctx.work_dir = target
        os.chdir(ctx.work_dir)
        print(f"Step 2: {target} already cloned, skipping.")
        return

    resolved = target.resolve()
    if not prompt_yes_no(f"Step 2: Clone to {resolved}?", default=True):
        print("Aborted. Re-run from the directory where you want the clone.")
        raise SystemExit(1)

    print(f"Step 2: Cloning {ctx.repo}...")
    subprocess.run(  # noqa: S603
        ("git", "clone", f"git@github.com:{ctx.repo}.git", str(target)),  # noqa: S607
        check=True,
    )
    ctx.work_dir = target
    os.chdir(ctx.work_dir)
    print(f"  Cloned to {target}.")


def _default_ci_versions(language: str | None) -> str:
    """Return sensible default CI versions for a language."""
    if language is None:
        return "latest"
    defaults: dict[str, str] = {
        "python": "3.12, 3.13, 3.14",
        "go": "1.23",
        "java": "21",
        "ruby": "3.3",
        "rust": "stable",
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

    existing = _load_existing_config(ctx.work_dir) if ctx.adopt and ctx.work_dir else None
    project = existing.get("project", {}) if existing else {}
    ci_raw = existing.get("ci", {}) if existing else {}
    pub_raw = existing.get("publish", {}) if existing else {}
    deps = existing.get("dependencies", {}) if existing else {}

    ctx.repository_type = prompt_choice(
        "Repository type",
        sorted(_ENUMS["repository-type"]),
        default=project.get("repository-type", ""),
    )

    # Primary language is prompted on its own — "no language" is the absence of
    # a language, presented as a separate "none of the above" choice, not a
    # sixth enum value (issue #1579).
    ctx.primary_language = prompt_language(default=project.get("primary-language", ""))

    enum_fields = [
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
        default=deps.get("vergil", "v2.1"),
    )

    license_options = ["GPL-3.0", "MIT", "Apache-2.0", "none"]
    ctx.license_type = prompt_choice("License", license_options, default="GPL-3.0")

    ctx.initial_version = prompt_free_text("Initial version", default="0.1.0")

    content = render_vergil_toml(ctx)
    if ctx.work_dir is None:  # pragma: no cover
        raise RuntimeError("work_dir not set")
    (ctx.work_dir / "vergil.toml").write_text(content)

    git.run("add", "vergil.toml")
    git.run("commit", "-m", "chore(init): step 3 - vergil.toml")
    print("  vergil.toml committed.")


def step_scaffold_config_files(ctx: RepoInitContext) -> None:
    """Step 4: Scaffold local config files."""
    import datetime
    import stat

    print("Step 4: Scaffolding config files...")
    if ctx.work_dir is None:  # pragma: no cover
        raise RuntimeError("work_dir not set")
    wd = ctx.work_dir

    # VERSION (canonical version source)
    (wd / "VERSION").write_text(ctx.initial_version + "\n")

    # CLAUDE.md
    claude_md = render_claude_md(ctx)
    (wd / "CLAUDE.md").write_text(claude_md)

    # .claude/settings.json
    claude_dir = wd / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings = _load_data_file("claude_settings.json")
    (claude_dir / "settings.json").write_text(settings)

    # .claude/hooks/guard.sh
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    shim_content = _load_data_file("hook_guard_shim.sh")
    shim_path = hooks_dir / "guard.sh"
    shim_path.write_text(shim_content)
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # README.md
    readme = render_readme(ctx)
    (wd / "README.md").write_text(readme)

    # .gitignore
    gitignore = render_gitignore()
    (wd / ".gitignore").write_text(gitignore)

    # LICENSE
    if ctx.license_type != "none":
        year = datetime.datetime.now(tz=datetime.UTC).year
        license_text = _load_license(ctx.license_type)
        license_text = license_text.replace("{year}", str(year))
        license_text = license_text.replace("{copyright_holder}", ctx.org)
        (wd / "LICENSE").write_text(license_text)

    git.run("add", "-A")
    git.run("commit", "-m", "chore(init): step 4 - config files")
    print("  Config files committed.")


def step_ci_cd_workflows(ctx: RepoInitContext) -> None:
    """Step 5: Generate CI and CD workflow files."""
    print("Step 5: Generating CI/CD workflows...")
    if ctx.work_dir is None:  # pragma: no cover
        raise RuntimeError("work_dir not set")
    wd = ctx.work_dir

    workflows_dir = wd / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    ci_content = render_ci_workflow(ctx)
    (workflows_dir / "ci.yml").write_text(ci_content)

    if ctx.publish_docs or ctx.publish_release:
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
    if ctx.work_dir is None:  # pragma: no cover
        raise RuntimeError("work_dir not set")
    wd = ctx.work_dir

    docs_dir = wd / "docs" / "site" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    mkdocs = render_mkdocs_yml(ctx)
    (wd / "docs" / "site" / "mkdocs.yml").write_text(mkdocs)

    index = render_docs_index(ctx)
    (docs_dir / "index.md").write_text(index)

    (docs_dir / "getting-started.md").write_text(
        f"# Getting Started\n\nTODO: Add getting started guide for {ctx.name}.\n"
    )

    git.run("add", "-A")
    git.run("commit", "-m", "chore(init): step 6 - docs site")
    print("  Docs site committed.")


def _remote_branch_exists(repo: str, branch: str) -> bool:
    """Check if a branch exists on the remote."""
    try:
        github.read_output(
            "api",
            f"repos/{repo}/branches/{branch}",
            "--jq",
            ".name",
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

    github.run("repo", "edit", ctx.repo, "--default-branch", "develop")
    print("  Default branch set to develop.")


def _sync_labels(repo: str) -> None:
    """Provision all canonical labels into a repo."""
    from vergil_tooling.lib.labels import load_labels

    registry = load_labels()
    for label in registry["labels"]:
        cmd: list[str] = [
            "label",
            "create",
            label["name"],
            "--repo",
            repo,
            "--force",
        ]
        if label.get("color"):
            cmd.extend(["--color", label["color"]])
        if label.get("description"):
            cmd.extend(["--description", label["description"]])
        github.run(*cmd)


def step_github_config(ctx: RepoInitContext) -> None:
    """Step 8: Apply GitHub config and labels."""
    from vergil_tooling.lib import config as config_module
    from vergil_tooling.lib.github_config import (
        apply_desired_state,
        compute_desired_state,
        fetch_actual_state,
    )

    print("Step 8: Applying GitHub config...")

    if ctx.work_dir is None:  # pragma: no cover
        raise RuntimeError("work_dir not set")
    cfg = config_module.read_config(ctx.work_dir)
    result = fetch_actual_state(ctx.repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(cfg, visibility=result.visibility, is_org=is_org)
    removed = apply_desired_state(ctx.repo, desired)
    if removed:
        print(f"  Legacy protection removed: {', '.join(removed)}")
    print("  GitHub config applied.")

    print("  Syncing labels...")
    _sync_labels(ctx.repo)
    print("  Labels synced.")


def step_github_pages(ctx: RepoInitContext) -> None:
    """Step 9: Configure GitHub Pages."""
    if not ctx.publish_docs:
        print("Step 9: Docs disabled, skipping Pages.")
        return

    print("Step 9: Configuring GitHub Pages...")

    if not _remote_branch_exists(ctx.repo, "gh-pages"):
        git.run("checkout", "--orphan", "gh-pages")
        git.run("reset", "--hard")
        git.run("commit", "--allow-empty", "-m", "chore: initialize gh-pages")
        git.run("push", "origin", "gh-pages")
        git.run("checkout", "develop")
        print("  Created gh-pages branch.")

    github.write_json(
        "POST",
        f"repos/{ctx.repo}/pages",
        {"source": {"branch": "gh-pages", "path": "/"}},
    )
    print("  Pages source configured.")

    homepage = f"https://{ctx.org}.github.io/{ctx.name}/"
    github.write_json(
        "PATCH",
        f"repos/{ctx.repo}",
        {"homepage": homepage},
    )
    print(f"  Homepage set to {homepage}")


def _check_remote_steps(ctx: RepoInitContext) -> set[int]:
    """Check which remote-only steps are already complete."""
    completed: set[int] = set()

    try:
        github.read_output("repo", "view", ctx.repo, "--json", "name")
        completed.add(1)
    except subprocess.CalledProcessError:
        pass

    if _remote_branch_exists(ctx.repo, "develop") and _remote_branch_exists(ctx.repo, "main"):
        completed.add(7)

    return completed


def run_wizard(ctx: RepoInitContext) -> None:
    """Run all wizard steps, skipping completed ones."""
    try:
        log_output = git.read_output("log", "--oneline")
    except subprocess.CalledProcessError:
        log_output = ""
    local_completed = detect_completed_steps(log_output)

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
