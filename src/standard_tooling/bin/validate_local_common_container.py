"""Common validation checks (container-local).

Runs inside the dev container via ``st-docker-run``:
  1. Repository profile validation (includes README structural checks)
  2. markdownlint on published markdown (docs/site/, README.md) using
     the bundled canonical config
  3. shellcheck on all shell scripts under ``scripts/``
  4. yamllint on YAML files under ``.github/`` and ``docs/`` (issue #302)
"""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from typing import TYPE_CHECKING

from standard_tooling.bin import repo_profile_cli
from standard_tooling.lib import git
from standard_tooling.lib.config import read_config

if TYPE_CHECKING:
    from pathlib import Path


def _find_shell_files(repo_root: Path) -> list[str]:
    """Discover shell files under scripts/."""
    scripts_dir = repo_root / "scripts"
    if not scripts_dir.is_dir():
        return []

    found: list[str] = []
    for path in scripts_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".sh" or "git-hooks" in path.parts or "bin" in path.parts:
            found.append(str(path))
    return sorted(found)


def _find_markdown_files(repo_root: Path, ignore: list[str] | None = None) -> list[str]:
    """Discover published markdown files: docs/site/**/*.md and README.md."""
    found: list[str] = []
    ignore_paths = [repo_root / p for p in (ignore or [])]

    site_dir = repo_root / "docs" / "site"
    if site_dir.is_dir():
        for path in site_dir.rglob("*.md"):
            if any(path.is_relative_to(ip) for ip in ignore_paths):
                continue
            found.append(str(path))

    readme = repo_root / "README.md"
    if readme.is_file():
        found.append(str(readme))

    return sorted(found)


_YAML_EXTS = frozenset({".yml", ".yaml"})


def _find_yaml_files(repo_root: Path) -> list[str]:
    """Discover YAML files we care about: repo-root config
    (.markdownlint.yaml etc.), `.github/` tree (workflows, issue
    templates), and `docs/site/mkdocs.yml`. The yamllint config lives
    at the repo root (`.yamllint`).

    Vendored paths (`.worktrees`, `.venv`, `.venv-host`,
    `node_modules`) are excluded by construction — discovery only
    walks the listed locations, never venv/worktree subtrees.
    """
    found: list[str] = []

    # Repo-root level YAML config files (e.g., .markdownlint.yaml).
    for path in repo_root.iterdir():
        if path.is_file() and path.suffix in _YAML_EXTS:
            found.append(str(path))

    # .github/ tree (workflows, issue templates, etc.).
    github_dir = repo_root / ".github"
    if github_dir.is_dir():
        for path in github_dir.rglob("*"):
            if path.is_file() and path.suffix in _YAML_EXTS:
                found.append(str(path))

    # docs/site/mkdocs.yml.
    mkdocs = repo_root / "docs" / "site" / "mkdocs.yml"
    if mkdocs.is_file():
        found.append(str(mkdocs))

    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    repo_root = git.repo_root()

    print("Running: repo-profile")
    rc = repo_profile_cli.main()
    if rc != 0:
        return rc

    cfg = read_config(repo_root)
    md_files = _find_markdown_files(repo_root, ignore=cfg.markdownlint.ignore)
    if md_files:
        print(f"Running: markdownlint ({len(md_files)} files)")
        config = files("standard_tooling.configs") / "markdownlint.yaml"
        cmd: list[str] = ["markdownlint", "--config", str(config), *md_files]
        result = subprocess.run(cmd, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            return result.returncode

    shell_files = _find_shell_files(repo_root)
    if shell_files:
        print(f"Running: shellcheck ({len(shell_files)} files)")
        result = subprocess.run(  # noqa: S603
            ["shellcheck", *shell_files],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    yaml_files = _find_yaml_files(repo_root)
    if yaml_files:
        print(f"Running: yamllint ({len(yaml_files)} files)")
        result = subprocess.run(  # noqa: S603
            ["yamllint", *yaml_files],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
