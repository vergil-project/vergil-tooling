"""Version management for standard-tooling managed repositories.

Discovers, reads, and bumps version strings based on the
``primary-language`` field in ``standard-tooling.toml``.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from typing import TYPE_CHECKING

from standard_tooling.lib.config import read_config

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
}


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

    return text.strip()


def _get_version_file(repo_root: Path) -> tuple[Path, str]:
    cfg = read_config(repo_root)
    language = cfg.project.primary_language

    raw_toml = (repo_root / "standard-tooling.toml").read_text()
    raw = tomllib.loads(raw_toml)
    override = raw.get("project", {}).get("version-file")
    if override:
        return repo_root / override, language

    version_file = _discover_version_file(repo_root, language)
    if not version_file.is_file():
        msg = f"Version file not found: {version_file}"
        raise FileNotFoundError(msg)
    return version_file, language


def _version_file_relative(repo_root: Path) -> tuple[str, str]:
    version_file, language = _get_version_file(repo_root)
    return str(version_file.relative_to(repo_root)), language


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
        rel_path, language = _version_file_relative(repo_root)
        return _read_version_from_ref(ref, rel_path, language)

    version_file, language = _get_version_file(repo_root)
    return _read_version(version_file.read_text(), language)


def show_major_minor(repo_root: Path, *, ref: str | None = None) -> str:
    version = show(repo_root, ref=ref)
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}"


def _increment_patch(version: str) -> str:
    parts = version.split(".")
    parts[2] = str(int(parts[2]) + 1)
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
    else:
        text = new + "\n"

    version_file.write_text(text)


def _run_lockfile_maintenance(repo_root: Path, language: str) -> None:
    cmd = _LOCKFILE_COMMANDS.get(language)
    if cmd is None:
        return
    subprocess.run(cmd, cwd=repo_root, check=True)  # noqa: S603


def bump(repo_root: Path) -> str:
    version_file, language = _get_version_file(repo_root)
    old_version = _read_version(version_file.read_text(), language)
    new_version = _increment_patch(old_version)
    _write_version(version_file, language, old_version, new_version)
    _run_lockfile_maintenance(repo_root, language)
    return new_version
