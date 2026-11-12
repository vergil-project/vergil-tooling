"""Common validation checks.

Runs inside the dev container via ``vrg-container-run``:
  1. Repository profile validation (includes README structural checks)
  2. markdownlint on docs/site/, README.md and epics/, in one pass against
     the bundled config, which disables MD013 line-length (issue #4103)
  3. shellcheck on all shell scripts under ``scripts/`` and every ``*.sh``
     file elsewhere in the repository (issue #3830)
  4. yamllint on YAML files under ``.github/`` and ``docs/`` using
     the bundled canonical config (issue #302, #590)
  5. dependabot-limit: every ``.github/dependabot.yml`` ``updates`` entry
     must carry ``open-pull-requests-limit: 0`` (epic .github#420)
  6. hadolint on Dockerfile* files at the repo root
  7. actionlint on ``.github/workflows/``
  8. ansible-lint when the repo carries Ansible content, using the
     bundled canonical config as the rule baseline while honoring the
     repo's local ``skip_list`` / ``exclude_paths`` (issues #1667, #1952)

Every discovered file list drops the paths the repo's ``.gitignore`` excludes,
so local build output inside a linted scope is never linted (issue #3594).
"""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from typing import TYPE_CHECKING

import yaml

from vergil_tooling.bin import repo_profile
from vergil_tooling.lib import dependabot, git
from vergil_tooling.lib.config import read_config

if TYPE_CHECKING:
    from pathlib import Path


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    repo_root = git.repo_root()

    print("Running: repo-profile")
    rc = repo_profile.main([])
    if rc != 0:
        return rc

    cfg = read_config(repo_root)
    markdown_files = _drop_gitignored(
        repo_root, _find_markdown_files(repo_root, ignore=cfg.markdownlint.ignore)
    )
    # The "(N files ...)" counts below name their fixed discovery scope on
    # purpose: each check lints a scoped slice of the working tree (published
    # docs, scripts/, etc.), NOT the branch diff. Agents kept reading a bare
    # "(N files)" as "N markdown files I changed" and reporting a phantom
    # off-by-one when a changed file fell outside the scope (issue #2601).
    if markdown_files:
        print(
            f"Running: markdownlint ({len(markdown_files)} files in docs/site/, README.md, epics/)"
        )
        markdownlint_config = files("vergil_tooling.configs") / "markdownlint.yaml"
        result = subprocess.run(  # noqa: S603
            ["markdownlint", "--config", str(markdownlint_config), *markdown_files],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    shell_files = _drop_gitignored(repo_root, _find_shell_files(repo_root))
    if shell_files:
        print(f"Running: shellcheck ({len(shell_files)} files in scripts/, *.sh repo-wide)")
        result = subprocess.run(  # noqa: S603
            ["shellcheck", *shell_files],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    yaml_files = _drop_gitignored(repo_root, _find_yaml_files(repo_root))
    if yaml_files:
        print(
            f"Running: yamllint ({len(yaml_files)} files in "
            ".github/, repo root, docs/site/mkdocs.yml)"
        )
        yaml_config = files("vergil_tooling.configs") / "yamllint.yaml"
        result = subprocess.run(  # noqa: S603
            ["yamllint", "--config-file", str(yaml_config), *yaml_files],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    rc = _check_dependabot_limit(repo_root)
    if rc != 0:
        return rc

    dockerfile_files = _drop_gitignored(repo_root, _find_dockerfiles(repo_root))
    if dockerfile_files:
        print(f"Running: hadolint ({len(dockerfile_files)} Dockerfile* in repo root)")
        result = subprocess.run(  # noqa: S603
            ["hadolint", *dockerfile_files],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    workflows_dir = repo_root / ".github" / "workflows"
    if workflows_dir.is_dir():
        print("Running: actionlint")
        result = subprocess.run(  # noqa: S603
            ["actionlint"],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    if _has_ansible_content(repo_root):
        print("Running: ansible-lint")
        ansible_config = files("vergil_tooling.configs") / "ansible-lint.yaml"
        overrides: list[str] = []
        local_config = _find_ansible_lint_config(repo_root)
        if local_config is not None:
            try:
                overrides = _ansible_lint_overrides(local_config)
            except yaml.YAMLError as exc:
                print(
                    f"error: failed to parse {local_config}: {exc}",
                    file=sys.stderr,
                )
                return 1
        result = subprocess.run(  # noqa: S603
            ["ansible-lint", "-c", str(ansible_config), *overrides],  # noqa: S607
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    return 0


def _drop_gitignored(repo_root: Path, paths: list[str]) -> list[str]:
    """Drop the paths the repository's gitignore rules exclude (issue #3594).

    The file-list checks walk the working tree, so build output that lands
    inside a linted scope (e.g. the release notes and changelog that
    ``vrg-container-docs build`` copies into ``docs/site/docs/``) would
    otherwise be linted although it is never committed. Filtering against the
    repo's own ``.gitignore`` keeps each scope to source without every repo
    repeating its ignore list in ``vergil.toml``. Tracked files are always
    kept, and a ``git check-ignore`` failure raises rather than linting less.
    """
    ignored = git.ignored_paths(paths, repo=repo_root)
    return [path for path in paths if path not in ignored]


def _check_dependabot_limit(repo_root: Path) -> int:
    """Reject any ``dependabot.yml`` entry that could open a PR (epic .github#420).

    Runs only when ``.github/dependabot.yml`` exists — the file is never
    required here. It checks **only** that every ``updates`` entry carries
    ``open-pull-requests-limit: 0`` (a missing key means Dependabot's default
    of 5); exact match against the canonical render is the config audit's job.
    """
    path = repo_root / dependabot.DEPENDABOT_PATH
    if not path.is_file():
        return 0
    print(f"Running: dependabot-limit ({dependabot.DEPENDABOT_PATH.as_posix()})")
    violations = dependabot.limit_violations(path.read_text(encoding="utf-8"))
    for reason in violations:
        print(f"dependabot.yml: {reason}", file=sys.stderr)
    return 1 if violations else 0


def _find_shell_files(repo_root: Path) -> list[str]:
    """Discover shell files: everything under scripts/ that looks like a
    script, plus every ``*.sh`` file anywhere in the repository.

    Under ``scripts/``, extensionless executables in ``bin/`` and
    ``git-hooks/`` count as shell too. Outside it only the ``.sh`` suffix
    marks a shell file, found by :func:`_find_repo_sh_files`, so scripts
    kept elsewhere (a plugin's ``hooks/``, an action's step scripts, a
    repo's ``tests/``) are linted as well (issue #3830).
    """
    found: set[str] = set(_find_repo_sh_files(repo_root))
    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        for path in scripts_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix == ".sh" or "git-hooks" in path.parts or "bin" in path.parts:
                found.add(str(path))
    return sorted(found)


def _find_repo_sh_files(repo_root: Path) -> list[str]:
    """Return every ``*.sh`` file in the repository, tracked or untracked,
    leaving out what ``.gitignore`` excludes (issue #3830).

    ``git ls-files`` lists them without walking ignored trees such as
    ``.worktrees/``, ``.venv/`` or ``node_modules/``. A tracked file deleted
    from the working tree is dropped, since there is nothing to lint. A git
    failure raises rather than linting less.
    """
    output = git.read_output(
        "-C",
        str(repo_root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "*.sh",
    )
    found: list[str] = []
    for rel in output.split("\0"):
        if not rel:
            continue
        path = repo_root / rel
        if path.is_file():
            found.append(str(path))
    return found


def _find_markdown_files(repo_root: Path, ignore: list[str] | None = None) -> list[str]:
    """Discover the markdown markdownlint checks: ``docs/site/**/*.md``,
    ``README.md`` and ``epics/**/*.md``.

    All of it is linted in one pass against the bundled config (issue #4103).
    The ``[markdownlint].ignore`` paths drop files under ``docs/site/`` and
    ``epics/``; ``README.md`` is always linted.
    """
    found: list[str] = []
    ignore_paths = [repo_root / p for p in (ignore or [])]

    for tree in (repo_root / "docs" / "site", repo_root / "epics"):
        if tree.is_dir():
            for path in tree.rglob("*.md"):
                if any(path.is_relative_to(ip) for ip in ignore_paths):
                    continue
                found.append(str(path))

    readme = repo_root / "README.md"
    if readme.is_file():
        found.append(str(readme))

    return sorted(found)


def _find_dockerfiles(repo_root: Path) -> list[str]:
    """Discover Dockerfile* files at the repo root."""
    found: list[str] = []
    for path in repo_root.iterdir():
        if path.is_file() and path.name.startswith("Dockerfile"):
            found.append(str(path))
    return sorted(found)


_YAML_EXTS = frozenset({".yml", ".yaml"})


def _find_yaml_files(repo_root: Path) -> list[str]:
    """Discover YAML files we care about: repo-root config
    (.markdownlint.yaml etc.), `.github/` tree (workflows, issue
    templates), and `docs/site/mkdocs.yml`.

    Vendored paths (`.worktrees`, `.venv`,
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


# Explicit signal files/directories that mark a repo as carrying Ansible
# content. Ansible playbooks are plain YAML with no unique extension, so
# detection is heuristic — we gate on concrete, deterministic signals
# rather than deferring to ansible-lint's own auto-detection (issue #1667).
_ANSIBLE_FILE_SIGNALS = (
    "ansible.cfg",
    ".ansible-lint",
    ".ansible-lint.yml",
    ".ansible-lint.yaml",
    "galaxy.yml",
)
# ``ansible`` is listed first: a top-level ``ansible/`` directory is the most
# common convention for standardizing Ansible layout (playbooks + a nested
# ``ansible/roles`` tree), more common than bare root-level
# ``playbooks/``/``roles/``. Detecting it also covers the nested
# ``ansible/roles`` and ``ansible/playbooks`` layouts, since those imply a
# top-level ``ansible/`` directory (issue #1906).
_ANSIBLE_DIR_SIGNALS = ("ansible", "playbooks", "roles")


def _has_ansible_content(repo_root: Path) -> bool:
    """Return True when the repo carries Ansible content.

    Detection is signal-based: a recognized config/manifest file at the
    repo root (``ansible.cfg``, ``.ansible-lint*``, ``galaxy.yml``), a
    role/collection metadata file (``meta/main.yml``), or an ``ansible/``,
    ``playbooks/``, or ``roles/`` directory. Mirrors the conditional-skip
    behavior of the other common checks — no Ansible content means the
    check is skipped.
    """
    if any((repo_root / name).is_file() for name in _ANSIBLE_FILE_SIGNALS):
        return True
    if (repo_root / "meta" / "main.yml").is_file():
        return True
    return any((repo_root / name).is_dir() for name in _ANSIBLE_DIR_SIGNALS)


# Repo-local ansible-lint config filenames, in ansible-lint's own discovery
# order. We pass the bundled config via ``-c``, which suppresses ansible-lint's
# config discovery entirely, so we read these ourselves to recover the two
# repo-side levers the gate must still honor (issue #1952).
_ANSIBLE_LINT_CONFIG_NAMES = (
    ".ansible-lint",
    ".ansible-lint.yml",
    ".ansible-lint.yaml",
)


def _find_ansible_lint_config(repo_root: Path) -> Path | None:
    """Return the repo's local ansible-lint config file, if any."""
    for name in _ANSIBLE_LINT_CONFIG_NAMES:
        candidate = repo_root / name
        if candidate.is_file():
            return candidate
    return None


def _ansible_lint_overrides(config_path: Path) -> list[str]:
    """Translate a repo's local ansible-lint ``skip_list`` / ``exclude_paths``
    into CLI flags layered on top of the bundled ``-c`` baseline.

    Passing ``-c <bundled>`` makes ansible-lint ignore the repo's own config
    discovery, so a consuming repo otherwise cannot defer rules (``skip_list``)
    or scope out non-Ansible YAML (``exclude_paths``). We read those two keys
    ourselves and re-express them as ``-x rule1,rule2`` and ``--exclude <path>``
    flags, which apply on top of the bundled rule baseline (issue #1952).

    Raises ``yaml.YAMLError`` if the config is not valid YAML; the caller
    surfaces that as a hard failure rather than silently dropping the repo's
    intended skips.
    """
    raw = yaml.safe_load(config_path.read_text())
    if not isinstance(raw, dict):
        return []

    overrides: list[str] = []

    skip_list = raw.get("skip_list")
    if isinstance(skip_list, list) and skip_list:
        overrides += ["-x", ",".join(str(rule) for rule in skip_list)]

    exclude_paths = raw.get("exclude_paths")
    if isinstance(exclude_paths, list):
        for entry in exclude_paths:
            overrides += ["--exclude", str(entry)]

    return overrides


if __name__ == "__main__":
    sys.exit(main())
