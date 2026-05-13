"""Automate release preparation: branch, changelog, PR.

Shared script for library repositories using the library-release branching
model. Auto-detects the ecosystem to find the version source of truth.

The release PR is created but not merged — callers (typically the publish
skill) drive the merge via ``vrg-merge-when-green`` once CI passes.

Supported ecosystems:
  - Python: reads version from pyproject.toml
  - Maven:  reads version from pom.xml
  - Go:     reads version from **/version.go
  - Ruby:   reads version from **/version.rb
  - Cargo:  reads version from Cargo.toml
  - Claude plugin: reads version from .claude-plugin/plugin.json
  - VERSION file: reads version from VERSION (fallback)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from vergil_tooling.lib import git, github

# -- ecosystem detection -----------------------------------------------------


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
    import json

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
        msg = (
            f"VERSION file contains '{version}' which is not valid semver.\n"
            f"Expected format: MAJOR.MINOR.PATCH (e.g. 1.2.3)"
        )
        raise SystemExit(msg)
    return version


_Detector = Callable[[], str | None]

_DETECTORS: list[tuple[str, _Detector]] = [
    ("python", _detect_python),
    ("maven", _detect_maven),
    ("go", _detect_go),
    ("ruby", _detect_ruby),
    ("cargo", _detect_cargo),
    ("claude-plugin", _detect_claude_plugin),
    ("version-file", _detect_version_file),
]


def detect_ecosystem() -> tuple[str, str]:
    """Return (ecosystem_name, version) or fail."""
    for name, detector in _DETECTORS:
        version = detector()
        if version is not None:
            return name, version
    msg = (
        "Could not detect ecosystem. Expected one of:\n"
        "  - pyproject.toml with version (Python)\n"
        "  - pom.xml with version (Maven)\n"
        "  - go.mod + **/version.go (Go)\n"
        "  - Gemfile + **/version.rb (Ruby)\n"
        "  - Cargo.toml with version (Cargo/Rust)\n"
        "  - .claude-plugin/plugin.json with version (Claude plugin)\n"
        "  - VERSION file with MAJOR.MINOR.PATCH"
    )
    raise SystemExit(msg)


# -- precondition checks ----------------------------------------------------


def _ensure_on_develop() -> None:
    branch = git.current_branch()
    if branch != "develop":
        raise SystemExit(f"Must be on develop branch (currently on '{branch}').")


def _ensure_clean_tree() -> None:
    status = git.read_output("status", "--porcelain")
    if status:
        raise SystemExit("Working tree is not clean. Commit or stash changes first.")


def _ensure_develop_up_to_date() -> None:
    git.run("fetch", "--tags", "--force", "origin", "develop")
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise SystemExit(
            f"Local develop ({local_sha[:8]}) does not match "
            f"origin/develop ({remote_sha[:8]}). "
            f"Pull latest changes before preparing a release."
        )


# -- release steps -----------------------------------------------------------


def _create_release_branch(branch: str) -> None:
    if git.ref_exists(branch) or git.ref_exists(f"origin/{branch}"):
        raise SystemExit(f"Release branch '{branch}' already exists.")
    print(f"Creating branch: {branch} (from develop)")
    git.run("checkout", "-b", branch)


def _merge_main(version: str) -> None:
    print("Merging main into release branch...")
    git.run("fetch", "--tags", "--force", "origin", "main")
    git.run(
        "merge",
        "origin/main",
        "-X",
        "ours",
        "-m",
        f"chore(release): merge main into release/{version}",
    )


RELEASE_NOTES_DIR = "releases"


def _generate_changelog(version: str) -> None:
    tag = f"develop-v{version}"
    print(f"Generating changelog with boundary tag: {tag}")
    config = files("vergil_tooling.configs") / "cliff.toml"
    subprocess.run(  # noqa: S603
        ("git-cliff", "--config", str(config), "--tag", tag, "-o", "CHANGELOG.md"),  # noqa: S607
        check=True,
    )
    _normalize_trailing_newline(Path("CHANGELOG.md"))
    git.run("add", "CHANGELOG.md")
    _generate_release_notes(version, tag)
    status = git.read_output("status", "--porcelain")
    if not status:
        raise SystemExit(
            f"No publishable changes since the last release.\n"
            f"All commits after develop-v{version} are filtered by git-cliff.\n"
            f"Aborting release preparation."
        )
    git.run("commit", "-m", f"chore(release): prepare {version}")


def _generate_release_notes(version: str, tag: str) -> None:
    config = files("vergil_tooling.configs") / "cliff-release-notes.toml"
    releases_dir = Path(RELEASE_NOTES_DIR)
    releases_dir.mkdir(exist_ok=True)
    output_file = releases_dir / f"v{version}.md"
    print(f"Generating release notes: {output_file}")
    subprocess.run(  # noqa: S603
        (  # noqa: S607
            "git-cliff",
            "--config",
            str(config),
            "--tag",
            tag,
            "--unreleased",
            "-o",
            str(output_file),
        ),
        check=True,
    )
    _normalize_trailing_newline(output_file)
    git.run("add", str(releases_dir))


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")


def _create_pr(version: str, issue: int) -> str:
    import tempfile

    print("Creating pull request to main...")
    title = f"release: {version}"
    body = (
        f"## Summary\n\nRelease {version}\n\nRef #{issue}\n\nGenerated with `vrg-prepare-release`\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        url = github.create_pr(base="main", title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    print(f"PR created: {url}")
    return url


# -- main --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Prepare a release.")
    parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="GitHub issue number for release tracking (used for PR linkage).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    _ensure_on_develop()
    _ensure_clean_tree()
    _ensure_develop_up_to_date()

    ecosystem, version = detect_ecosystem()
    branch = f"release/{version}"

    print(f"Preparing release {version} ({ecosystem})")

    _create_release_branch(branch)
    _merge_main(version)
    _generate_changelog(version)
    print(f"Pushing branch: {branch}")
    git.run("push", "-u", "origin", branch)
    url = _create_pr(version, args.issue)

    git.run("checkout", "develop")

    print(f"Release {version} preparation complete.")
    print(f"Merge when green: vrg-merge-when-green {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
