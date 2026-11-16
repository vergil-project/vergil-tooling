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
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import cargo_semver
from vergil_tooling.lib.config import read_config

if TYPE_CHECKING:
    from pathlib import Path

_GO_VERSION_RE = re.compile(r'Version\s*=\s*"([^"]+)"')

# A TOML table header line, ``[name]`` or ``[[name]]``, with an optional comment.
_TOML_HEADER_RE = re.compile(r"^\s*\[\[?(?P<name>[^\[\]]+)\]\]?\s*(?:#.*)?$")

# The Cargo manifest tables that hold dependencies, in a crate or under a
# ``[target.<cfg>]`` table.
_CARGO_DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")

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


class CargoVersionError(Exception):
    """A Cargo manifest's version cannot be read, or a bump of it would break the workspace.

    Deliberately not a :class:`ValueError`: :func:`bump` and :func:`bump_paths`
    skip a language file on ``(FileNotFoundError, ValueError)``, and this error
    must pass through them rather than leave ``VERSION`` and ``Cargo.toml``
    out of step.
    """


def cargo_version_table(data: dict[str, Any]) -> tuple[str, ...]:
    """The TOML table path of the table holding a parsed ``Cargo.toml``'s version.

    The source is chosen by value: ``("package",)`` when ``[package].version``
    is a string, and ``("workspace", "package")`` when there is no
    ``[package]`` (a virtual workspace) or it inherits its version with
    ``version.workspace = true``. Anything else raises
    :class:`CargoVersionError` naming both tables.
    """
    package = data.get("package")
    if isinstance(package, dict):
        package_version = package.get("version")
        if isinstance(package_version, str):
            return ("package",)
        inherits = isinstance(package_version, dict) and package_version.get("workspace") is True
        if not inherits:
            msg = (
                "Cargo.toml holds no version: [package] has no version string "
                "and does not inherit one from [workspace.package] "
                "(version.workspace = true)"
            )
            raise CargoVersionError(msg)
    workspace = data.get("workspace")
    workspace_package = workspace.get("package") if isinstance(workspace, dict) else None
    if isinstance(workspace_package, dict) and isinstance(workspace_package.get("version"), str):
        return ("workspace", "package")
    where = (
        "[package] inherits its version but" if isinstance(package, dict) else "no [package] and"
    )
    msg = f"Cargo.toml holds no version: {where} [workspace.package] has no version string"
    raise CargoVersionError(msg)


def _cargo_version(data: dict[str, Any]) -> str:
    table: Any = data
    for key in cargo_version_table(data):
        table = table[key]
    return str(table["version"])


def _toml_header_path(line: str) -> tuple[str, ...] | None:
    """The table path a TOML header line opens, or ``None`` for any other line."""
    m = _TOML_HEADER_RE.match(line)
    if m is None:
        return None
    return tuple(part.strip().strip("\"'") for part in m["name"].split("."))


def _rewrite_cargo_version(text: str, old: str, new: str) -> str:
    """*text* with the version in the table :func:`cargo_version_table` names set to *new*.

    A line edit scoped to that table, from its header to the next header, so
    a ``version = "<old>"`` in any other table (a ``[workspace.dependencies]``
    entry, say) is never touched. Raises :class:`VersionSyncError` when the
    manifest's version is not *old*, and :class:`CargoVersionError` when the
    key cannot be rewritten in place.
    """
    data = tomllib.loads(text)
    table = cargo_version_table(data)
    current = _cargo_version(data)
    header = "[" + ".".join(table) + "]"
    if current != old:
        msg = f"VERSION contains {old} but Cargo.toml {header} contains {current}"
        raise VersionSyncError(msg)
    key_re = re.compile(
        r"^(?P<lead>\s*version\s*=\s*)(?P<q>[\"'])" + re.escape(old) + r"(?P=q)(?P<tail>.*)$",
        re.DOTALL,
    )
    lines = text.splitlines(keepends=True)
    in_table = False
    for index, line in enumerate(lines):
        path = _toml_header_path(line)
        if path is not None:
            in_table = path == table
            continue
        m = key_re.match(line) if in_table else None
        if m is not None:
            lines[index] = f"{m['lead']}{m['q']}{new}{m['q']}{m['tail']}"
            rewritten = "".join(lines)
            # A matching line inside a multi-line string is not the key: the
            # parse would still read the old version, so keep looking.
            if _cargo_version(tomllib.loads(rewritten)) == new:
                return rewritten
            lines[index] = line
    msg = (
        f"cannot rewrite the version in Cargo.toml {header}: no "
        f'version = "{old}" line in that table (write it as a plain key)'
    )
    raise CargoVersionError(msg)


def _load_manifest(manifest: Path, root: Path) -> dict[str, Any]:
    rel = manifest.relative_to(root).as_posix()
    try:
        return tomllib.loads(manifest.read_text())
    except FileNotFoundError as exc:
        msg = f"workspace member manifest {rel} not found"
        raise CargoVersionError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"cannot parse {rel}: {exc}"
        raise CargoVersionError(msg) from exc


def _path_dependencies(data: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every ``(table, key, entry)`` path dependency a crate manifest declares."""
    tables: list[tuple[str, Any]] = [(name, data.get(name)) for name in _CARGO_DEPENDENCY_TABLES]
    targets = data.get("target")
    if isinstance(targets, dict):
        for cfg, target in targets.items():
            if isinstance(target, dict):
                tables.extend(
                    (f"target.{cfg}.{name}", target.get(name)) for name in _CARGO_DEPENDENCY_TABLES
                )
    found: list[tuple[str, str, dict[str, Any]]] = []
    for table_name, table in tables:
        if not isinstance(table, dict):
            continue
        for key, entry in table.items():
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                found.append((table_name, key, entry))
    return found


def _workspace_members(repo_root: Path, data: dict[str, Any]) -> dict[Path, dict[str, Any]]:
    """Every crate in the workspace rooted at *repo_root*, by resolved directory.

    The root itself when it is a package, every ``[workspace].members`` glob
    match not under ``exclude``, and, as Cargo does, every path dependency of
    a member that lies inside the workspace directory.
    """
    root = repo_root.resolve()
    workspace = data.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    excluded = {(root / str(p)).resolve() for p in workspace.get("exclude", [])}
    members: dict[Path, dict[str, Any]] = {}
    pending: list[Path] = []
    if isinstance(data.get("package"), dict):
        members[root] = data
        pending.extend(root / entry["path"] for _table, _key, entry in _path_dependencies(data))
    for pattern in workspace.get("members", []):
        pending.extend(sorted(p for p in root.glob(str(pattern)) if p.is_dir()))
    while pending:
        crate_dir = pending.pop(0).resolve()
        if crate_dir in members or crate_dir in excluded or not crate_dir.is_relative_to(root):
            continue
        crate_data = _load_manifest(crate_dir / "Cargo.toml", root)
        members[crate_dir] = crate_data
        pending.extend(
            crate_dir / entry["path"] for _table, _key, entry in _path_dependencies(crate_data)
        )
    return members


def _inherits_version(crate_data: dict[str, Any]) -> bool:
    package = crate_data.get("package")
    version = package.get("version") if isinstance(package, dict) else None
    return isinstance(version, dict) and version.get("workspace") is True


def _check_member_requirements(repo_root: Path, data: dict[str, Any], new: str) -> None:
    """Fail when *new* would break a versioned path dependency on a bumped crate.

    The bumped crates are those whose version is the one the bump writes:
    the root package alone when it holds its own version, else every member
    inheriting ``[workspace.package].version``. Every ``[workspace.dependencies]``
    entry and every member's own dependency that points at one of them by
    ``path`` and carries a ``version`` must still accept *new*, or Cargo
    rejects the bumped workspace. Raises :class:`CargoVersionError` naming
    each entry that would not, before anything is written.
    """
    root = repo_root.resolve()
    members = _workspace_members(repo_root, data)
    if cargo_version_table(data) == ("package",):
        bumped = {root}
    else:
        bumped = {path for path, crate in members.items() if _inherits_version(crate)}
    entries: list[tuple[str, Path, str, str, dict[str, Any]]] = []
    workspace = data.get("workspace")
    workspace_deps = workspace.get("dependencies") if isinstance(workspace, dict) else None
    if isinstance(workspace_deps, dict):
        entries.extend(
            ("Cargo.toml", root, "workspace.dependencies", key, entry)
            for key, entry in workspace_deps.items()
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        )
    for crate_dir, crate_data in members.items():
        rel = (crate_dir / "Cargo.toml").relative_to(root).as_posix()
        entries.extend(
            (rel, crate_dir, table, key, entry)
            for table, key, entry in _path_dependencies(crate_data)
        )
    broken: list[str] = []
    for manifest, base, table, key, entry in entries:
        req = entry.get("version")
        if not isinstance(req, str) or (base / entry["path"]).resolve() not in bumped:
            continue
        where = f'{manifest} [{table}] {key} = {{ path = "{entry["path"]}", version = "{req}" }}'
        try:
            ok = cargo_semver.matches(req, new)
        except ValueError as exc:
            msg = f"cannot check {where}: {exc}"
            raise CargoVersionError(msg) from exc
        if not ok:
            broken.append(where)
    if broken:
        listed = "\n  ".join(broken)
        msg = (
            f"bumping to {new} would break these versioned path dependencies on "
            f"workspace members, so nothing was written; update their version "
            f"requirements first:\n  {listed}"
        )
        raise CargoVersionError(msg)


def _check_cargo_bump(repo_root: Path, cargo_toml: Path, old: str, new: str) -> None:
    """Everything a Rust bump checks before it writes any file."""
    text = cargo_toml.read_text()
    _rewrite_cargo_version(text, old, new)
    _check_member_requirements(repo_root, tomllib.loads(text), new)


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
        return _cargo_version(tomllib.loads(text))

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

    if language == "rust":
        text = _rewrite_cargo_version(text, old, new)
    elif language == "python":
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

    cfg = read_config(repo_root)
    language = cfg.project.primary_language

    # Every Rust check runs before any file is written, VERSION included, so a
    # manifest with no version table, or a bump that would break a member's
    # versioned path dependency, fails with nothing changed.
    if language == "rust":
        cargo_toml = _discover_version_file(repo_root, language)
        if cargo_toml.is_file():
            _check_cargo_bump(repo_root, cargo_toml, old_version, new_version)

    version_file.write_text(new_version + "\n")

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
