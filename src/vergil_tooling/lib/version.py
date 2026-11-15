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

_GO_VERSION_RE = re.compile(r'Version\s*=\s*"([^"]+)"')

# The command that refreshes a language's committed lockfile after a version
# bump. Each runs in the repo's dev container (see _run_lockfile_maintenance),
# never on the host. ``cargo update --workspace`` updates only the workspace's
# own packages, so the lockfile picks up the bumped version and no dependency
# is upgraded (https://doc.rust-lang.org/cargo/commands/cargo-update.html).
_LOCKFILE_COMMANDS: dict[str, list[str]] = {
    "python": ["uv", "lock"],
    "rust": ["cargo", "update", "--workspace"],
}

# The lockfile each _LOCKFILE_COMMANDS entry rewrites, relative to the repo root.
_LOCKFILES: dict[str, str] = {
    "python": "uv.lock",
    "rust": "Cargo.lock",
}

# Runs a command in the repo's dev container: the language image, the
# workspace mounts and the resolved toolchain environment validation uses.
_CONTAINER_RUN = ("vrg-container-run", "--")

_DEFAULT_VERSION_FILES: dict[str, str] = {
    "python": "pyproject.toml",
    "rust": "Cargo.toml",
}

VERSION_FILE = "VERSION"

_LANGUAGES_WITH_SEPARATE_VERSION = frozenset(
    {
        "python",
        "rust",
        "go",
    }
)

_SECONDARY_VERSION_FILES: dict[str, str] = {
    ".claude-plugin/plugin.json": "claude-plugin",
}


class VersionSyncError(Exception):
    """Raised when VERSION and language-specific file disagree."""


def _discover_version_file(repo_root: Path, language: str) -> Path:
    if language in _DEFAULT_VERSION_FILES:
        return repo_root / _DEFAULT_VERSION_FILES[language]

    if language == "go":
        matches = [m for m in repo_root.glob("**/version.go") if ".git" not in m.parts]
        if not matches:
            msg = f"No **/version.go found in {repo_root}"
            raise FileNotFoundError(msg)
        return matches[0]

    msg = f"Unsupported language for version discovery: {language}"
    raise ValueError(msg)


def _cross_check_language_file(
    repo_root: Path, language: str | None, canonical_version: str
) -> None:
    if language is not None and language in _LANGUAGES_WITH_SEPARATE_VERSION:
        try:
            lang_file = _discover_version_file(repo_root, language)
        except (FileNotFoundError, ValueError):
            print(
                f"warning: {language} version file not found; sync check skipped",
                file=sys.stderr,
            )
        else:
            if not lang_file.is_file():
                rel = lang_file.relative_to(repo_root)
                print(
                    f"warning: {rel} not found; sync check skipped",
                    file=sys.stderr,
                )
            else:
                lang_version = _read_version(lang_file.read_text(), language)
                if lang_version != canonical_version:
                    rel = lang_file.relative_to(repo_root)
                    msg = f"VERSION contains {canonical_version} but {rel} contains {lang_version}"
                    raise VersionSyncError(msg)

    for rel_path, fmt in _SECONDARY_VERSION_FILES.items():
        secondary = repo_root / rel_path
        if secondary.is_file():
            sec_version = _read_version(secondary.read_text(), fmt)
            if sec_version != canonical_version:
                msg = f"VERSION contains {canonical_version} but {rel_path} contains {sec_version}"
                raise VersionSyncError(msg)


def _read_version(text: str, language: str) -> str:
    if language == "python":
        data = tomllib.loads(text)
        return str(data["project"]["version"])

    if language == "rust":
        data = tomllib.loads(text)
        return str(data["package"]["version"])

    if language == "go":
        m = _GO_VERSION_RE.search(text)
        if not m:
            msg = 'No Version = "..." found'
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
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "show", f"{ref}:{relative_path}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise
    return _read_version(result.stdout, language)


def show(repo_root: Path, *, ref: str | None = None) -> str:
    if ref is not None:
        return _read_version_from_ref(ref, VERSION_FILE, "plaintext")

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
    elif language == "go":
        text = text.replace(f'Version = "{old}"', f'Version = "{new}"', 1)
    elif language == "claude-plugin":
        text = text.replace(f'"version": "{old}"', f'"version": "{new}"', 1)
    else:
        text = new + "\n"

    version_file.write_text(text)


def _run_lockfile_maintenance(repo_root: Path, language: str) -> None:
    """Refresh *language*'s lockfile for the bumped version, in the dev container.

    The lock command runs through ``vrg-container-run``, so a release needs no
    host toolchain for any language and the lockfile is written by the same
    ``uv`` or ``cargo`` that validation checks it with (#4407). A missing or
    stopped container runtime, or a failing lock command, raises
    :class:`subprocess.CalledProcessError` after its output is shown.
    """
    cmd = _LOCKFILE_COMMANDS.get(language)
    if cmd is None:
        return
    try:
        result = subprocess.run(  # noqa: S603
            [*_CONTAINER_RUN, *cmd],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise
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
    if language is not None and language in _LANGUAGES_WITH_SEPARATE_VERSION:
        try:
            lang_file = _discover_version_file(repo_root, language)
            if lang_file.is_file():
                _write_version(lang_file, language, old_version, new_version)
        except (FileNotFoundError, ValueError):
            pass

    for rel_path, fmt in _SECONDARY_VERSION_FILES.items():
        secondary = repo_root / rel_path
        if secondary.is_file():
            _write_version(secondary, fmt, old_version, new_version)

    if language is not None:
        _run_lockfile_maintenance(repo_root, language)
    return new_version


def next_version(current: str, part: str = "patch") -> str:
    """The version :func:`bump` moves *current* to, without writing anything."""
    if part not in _VALID_PARTS:
        msg = f"part must be one of {sorted(_VALID_PARTS)}, got '{part}'"
        raise ValueError(msg)
    return _increment_version(current, part)


def bump_paths(repo_root: Path) -> frozenset[str]:
    """Every path, relative to *repo_root*, that :func:`bump` may write.

    The same discovery :func:`bump` runs: VERSION, the language's version
    file, the secondary version files and the language's lockfile. A path is
    listed whether or not it exists, since a lock command may create it.
    """
    paths = {VERSION_FILE, *_SECONDARY_VERSION_FILES}
    language = read_config(repo_root).project.primary_language
    if language is not None and language in _LANGUAGES_WITH_SEPARATE_VERSION:
        try:
            lang_file = _discover_version_file(repo_root, language)
        except (FileNotFoundError, ValueError):
            # bump() writes no language file then, so none is listed: a dirty
            # path outside this set is refused by the caller, never ignored.
            pass
        else:
            paths.add(lang_file.relative_to(repo_root).as_posix())
    if language is not None and language in _LOCKFILES:
        paths.add(_LOCKFILES[language])
    return frozenset(paths)
