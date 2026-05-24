"""Version management for vergil-tooling managed repositories.

Discovers, reads, and bumps version strings based on the
``primary-language`` field in ``vergil.toml``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from typing import TYPE_CHECKING

from vergil_tooling.lib.config import read_config

if TYPE_CHECKING:
    from pathlib import Path

_RUBY_VERSION_RE = re.compile(r"VERSION\s*=\s*'([^']+)'")
_GO_VERSION_RE = re.compile(r'Version\s*=\s*"([^"]+)"')
_JAVA_VERSION_RE = re.compile(r"<version>([^<]+)</version>")

_LOCKFILE_COMMANDS: dict[str, list[str]] = {
    "python": ["uv", "lock"],
    "rust": ["cargo", "update", "--workspace"],
    "ruby": ["bundle", "install"],
}

_DEFAULT_VERSION_FILES: dict[str, str] = {
    "python": "pyproject.toml",
    "rust": "Cargo.toml",
    "java": "pom.xml",
    "shell": "VERSION",
    "none": "VERSION",
    "claude-plugin": ".claude-plugin/plugin.json",
}

VERSION_FILE = "VERSION"

_LANGUAGES_WITH_SEPARATE_VERSION = frozenset(
    {
        "python",
        "rust",
        "java",
        "ruby",
        "go",
        "claude-plugin",
    }
)


class VersionSyncError(Exception):
    """Raised when VERSION and language-specific file disagree."""


def _discover_version_file(repo_root: Path, language: str) -> Path:
    if language in _DEFAULT_VERSION_FILES:
        return repo_root / _DEFAULT_VERSION_FILES[language]

    if language == "ruby":
        matches = list(repo_root.glob("lib/**/version.rb"))
        if not matches:
            msg = f"No lib/**/version.rb found in {repo_root}"
            raise FileNotFoundError(msg)
        return matches[0]

    if language == "go":
        matches = [m for m in repo_root.glob("**/version.go") if ".git" not in m.parts]
        if not matches:
            msg = f"No **/version.go found in {repo_root}"
            raise FileNotFoundError(msg)
        return matches[0]

    msg = f"Unsupported language for version discovery: {language}"
    raise ValueError(msg)


def _cross_check_language_file(repo_root: Path, language: str, canonical_version: str) -> None:
    if language not in _LANGUAGES_WITH_SEPARATE_VERSION:
        return
    try:
        lang_file = _discover_version_file(repo_root, language)
    except (FileNotFoundError, ValueError):
        print(
            f"warning: {language} version file not found; sync check skipped",
            file=sys.stderr,
        )
        return
    if not lang_file.is_file():
        rel = lang_file.relative_to(repo_root)
        print(
            f"warning: {rel} not found; sync check skipped",
            file=sys.stderr,
        )
        return
    lang_version = _read_version(lang_file.read_text(), language)
    if lang_version != canonical_version:
        rel = lang_file.relative_to(repo_root)
        msg = f"VERSION contains {canonical_version} but {rel} contains {lang_version}"
        raise VersionSyncError(msg)


def _read_version(text: str, language: str) -> str:
    if language == "python":
        data = tomllib.loads(text)
        return str(data["project"]["version"])

    if language == "rust":
        data = tomllib.loads(text)
        return str(data["package"]["version"])

    if language == "ruby":
        m = _RUBY_VERSION_RE.search(text)
        if not m:
            msg = "No VERSION = '...' found"
            raise ValueError(msg)
        return m.group(1)

    if language == "go":
        m = _GO_VERSION_RE.search(text)
        if not m:
            msg = 'No Version = "..." found'
            raise ValueError(msg)
        return m.group(1)

    if language == "java":
        m = _JAVA_VERSION_RE.search(text)
        if not m:
            msg = "No <version>...</version> found"
            raise ValueError(msg)
        return m.group(1)

    if language == "claude-plugin":
        data = json.loads(text)
        version = data.get("version")
        if version is None:
            msg = "No 'version' key in plugin.json"
            raise ValueError(msg)
        return str(version)

    return text.strip()


def _read_version_from_ref(ref: str, relative_path: str, language: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{ref}:{relative_path}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return _read_version(result.stdout, language)


def show(repo_root: Path, *, ref: str | None = None) -> str:
    if ref is not None:
        return _read_version_from_ref(ref, VERSION_FILE, "shell")

    version_file = repo_root / VERSION_FILE
    if not version_file.is_file():
        msg = f"VERSION file not found at {repo_root}"
        raise FileNotFoundError(msg)
    version = version_file.read_text().strip()

    cfg = read_config(repo_root)
    _cross_check_language_file(repo_root, cfg.project.primary_language, version)

    return version


def show_major_minor(repo_root: Path, *, ref: str | None = None) -> str:
    version = show(repo_root, ref=ref)
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}"


_VALID_PARTS = frozenset({"patch", "minor", "major"})


def _increment_version(version: str, part: str) -> str:
    parts = version.split(".")
    if part == "patch":
        parts[2] = str(int(parts[2]) + 1)
    elif part == "minor":
        parts[1] = str(int(parts[1]) + 1)
        parts[2] = "0"
    else:
        parts[0] = str(int(parts[0]) + 1)
        parts[1] = "0"
        parts[2] = "0"
    return ".".join(parts)


def _write_version(version_file: Path, language: str, old: str, new: str) -> None:
    text = version_file.read_text()

    if language in ("python", "rust"):
        text = text.replace(f'version = "{old}"', f'version = "{new}"', 1)
    elif language == "ruby":
        text = text.replace(f"VERSION = '{old}'", f"VERSION = '{new}'", 1)
    elif language == "go":
        text = text.replace(f'Version = "{old}"', f'Version = "{new}"', 1)
    elif language == "java":
        text = text.replace(f"<version>{old}</version>", f"<version>{new}</version>", 1)
    elif language == "claude-plugin":
        text = text.replace(f'"version": "{old}"', f'"version": "{new}"', 1)
    else:
        text = new + "\n"

    version_file.write_text(text)


def _run_lockfile_maintenance(repo_root: Path, language: str) -> None:
    cmd = _LOCKFILE_COMMANDS.get(language)
    if cmd is None:
        return
    result = subprocess.run(cmd, cwd=repo_root, check=True, capture_output=True, text=True)  # noqa: S603
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def bump(repo_root: Path, part: str = "patch") -> str:
    if part not in _VALID_PARTS:
        msg = f"part must be one of {sorted(_VALID_PARTS)}, got '{part}'"
        raise ValueError(msg)

    version_file = repo_root / VERSION_FILE
    if not version_file.is_file():
        msg = f"VERSION file not found at {repo_root}"
        raise FileNotFoundError(msg)
    old_version = version_file.read_text().strip()
    new_version = _increment_version(old_version, part)

    version_file.write_text(new_version + "\n")

    cfg = read_config(repo_root)
    language = cfg.project.primary_language
    if language in _LANGUAGES_WITH_SEPARATE_VERSION:
        try:
            lang_file = _discover_version_file(repo_root, language)
            if lang_file.is_file():
                _write_version(lang_file, language, old_version, new_version)
        except (FileNotFoundError, ValueError):
            pass

    _run_lockfile_maintenance(repo_root, language)
    return new_version
