"""Preflight checks for vrg-release."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from vergil_tooling.lib import config, git, github
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import find_existing_tracking_issue

_VERSION_OVERRIDE_FIELDS = ("minor", "major")


def preflight(
    *,
    version_override: str | None,
    repo_root: Path,
) -> ReleaseContext:
    """Run all preflight checks and return an initialized ReleaseContext."""
    _check_host_prerequisites()
    repo = _check_gh_auth()
    cfg = _read_and_validate_config(repo_root)
    _check_branch_and_tree()
    _audit_repo_config(repo)
    version = _detect_version(repo_root)
    _check_version_not_tagged(version)
    _check_no_existing_tracking_issue(repo, version)

    if version_override in _VERSION_OVERRIDE_FIELDS:
        version = _apply_version_override(repo_root, version, version_override, cfg)

    print(f"Preflight passed: {repo} v{version}")
    return ReleaseContext(
        repo=repo,
        version=version,
        repo_root=repo_root,
        version_override=version_override,
    )


def _check_host_prerequisites() -> None:
    if shutil.which("git-cliff") is None:
        raise ReleaseError(
            phase="preflight",
            command="which git-cliff",
            message="git-cliff is not on PATH. Install it before running vrg-release.",
        )


def _check_gh_auth() -> str:
    try:
        return github.read_output(
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        )
    except Exception as exc:
        raise ReleaseError(
            phase="preflight",
            command="gh repo view",
            message="GitHub CLI authentication failed.",
            detail=str(exc),
        ) from exc


def _read_and_validate_config(repo_root: Path) -> config.StConfig:
    return config.read_config(repo_root)


def _check_branch_and_tree() -> None:
    branch = git.current_branch()
    if branch != "develop":
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse --abbrev-ref HEAD",
            message=f"Must be on develop branch (currently on '{branch}').",
        )
    status = git.read_output("status", "--porcelain")
    if status:
        raise ReleaseError(
            phase="preflight",
            command="git status --porcelain",
            message="Working tree is not clean.",
            detail=status,
        )
    git.run("fetch", "--tags", "--force", "origin", "develop")
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse HEAD vs origin/develop",
            message=(
                f"Local develop ({local_sha[:8]}) does not match "
                f"origin/develop ({remote_sha[:8]}). Pull latest first."
            ),
        )


def _audit_repo_config(repo: str) -> None:
    result = subprocess.run(  # noqa: S603
        ("vrg-github-repo-config", "audit", "--repo", repo),  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseError(
            phase="preflight",
            command=f"vrg-github-repo-config audit --repo {repo}",
            message="Repository configuration is non-compliant.",
            detail=result.stdout + result.stderr,
        )


# -- version detection (absorbed from vrg-prepare-release) --


def _detect_python() -> str | None:
    path = Path("pyproject.toml")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _detect_maven() -> str | None:
    path = Path("pom.xml")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r"<artifactId>[^<]+</artifactId>\s*<version>([^<]+)</version>", text)
    return match.group(1) if match else None


def _detect_go() -> str | None:
    if not Path("go.mod").is_file():
        return None
    for path in Path().rglob("version.go"):
        text = path.read_text(encoding="utf-8")
        match = re.search(r'(?:const\s+)?Version\s*=\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    return None


def _detect_ruby() -> str | None:
    if not Path("Gemfile").is_file():
        return None
    for path in Path().rglob("version.rb"):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    return None


def _detect_cargo() -> str | None:
    path = Path("Cargo.toml")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _detect_claude_plugin() -> str | None:
    path = Path(".claude-plugin/plugin.json")
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    version: str | None = data.get("version")
    return version


def _detect_version_file() -> str | None:
    path = Path("VERSION")
    if not path.is_file():
        return None
    version = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(
            phase="preflight",
            command="read VERSION",
            message=f"VERSION file contains '{version}' — not valid semver (MAJOR.MINOR.PATCH).",
        )
    return version


_DETECTORS = [
    _detect_python,
    _detect_maven,
    _detect_go,
    _detect_ruby,
    _detect_cargo,
    _detect_claude_plugin,
    _detect_version_file,
]


def _detect_version(repo_root: Path) -> str:
    prev = Path.cwd()
    os.chdir(repo_root)
    try:
        for detector in _DETECTORS:
            version = detector()
            if version is not None:
                return version
    finally:
        os.chdir(prev)
    raise ReleaseError(
        phase="preflight",
        command="detect version",
        message="Could not detect project version from any supported manifest.",
    )


def _check_version_not_tagged(version: str) -> None:
    latest_tag = git.read_output(
        "describe",
        "--tags",
        "--abbrev=0",
        "--match",
        "v*",
    )
    if latest_tag == f"v{version}":
        raise ReleaseError(
            phase="preflight",
            command="git describe --tags --match v*",
            message=(
                f"Version {version} is already tagged as {latest_tag}. "
                f"The post-publish version bump may not have run."
            ),
        )


def _check_no_existing_tracking_issue(repo: str, version: str) -> None:
    existing = find_existing_tracking_issue(repo, version)
    if existing is not None:
        raise ReleaseError(
            phase="preflight",
            command=f"gh issue list --search 'release: {version}'",
            message=(
                f"A tracking issue already exists for version {version}: {existing}\n"
                f"Close the stale issue or investigate before re-running."
            ),
        )


def _apply_version_override(
    repo_root: Path,
    current: str,
    override: str,
    cfg: config.StConfig,
) -> str:
    parts = current.split(".")
    if len(parts) != 3:
        raise ReleaseError(
            phase="preflight",
            command="version override",
            message=f"Version '{current}' is not valid semver for override.",
        )
    major, minor, _patch = int(parts[0]), int(parts[1]), int(parts[2])
    target = f"{major}.{minor + 1}.0" if override == "minor" else f"{major + 1}.0.0"

    _bump_version_in_manifest(repo_root, current, target, cfg)
    print(f"Version override: {current} -> {target}")
    return target


def _bump_version_in_manifest(repo_root: Path, old: str, new: str, cfg: config.StConfig) -> None:
    if cfg.project.primary_language == "python":
        path = repo_root / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        text = text.replace(f'version = "{old}"', f'version = "{new}"')
        path.write_text(text, encoding="utf-8")
        subprocess.run(("uv", "lock"), check=True, cwd=repo_root)  # noqa: S603, S607
    else:
        raise ReleaseError(
            phase="preflight",
            command="version override",
            message=(
                f"Version override not yet implemented for "
                f"language '{cfg.project.primary_language}'."
            ),
        )
    git.run("add", "-A")
    git.run("commit", "-m", f"chore(release): bump version to {new}")
