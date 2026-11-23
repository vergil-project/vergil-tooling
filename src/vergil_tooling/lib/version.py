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
from vergil_tooling.lib.container import PackageJsonError, read_package_json

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

# The lockfile a bump rewrites, relative to the repo root: by the language's
# _LOCKFILE_COMMANDS entry or, for TypeScript, by a direct edit of its root
# version fields (_write_package_lock_version), which runs no npm.
_LOCKFILES: dict[str, str] = {
    "python": "uv.lock",
    "rust": "Cargo.lock",
    "typescript": "package-lock.json",
}

# Runs a command in the repo's dev container: the language image, the
# workspace mounts and the resolved toolchain environment validation uses.
_CONTAINER_RUN = ("vrg-container-run", "--")

_DEFAULT_VERSION_FILES: dict[str, str] = {
    "python": "pyproject.toml",
    "rust": "Cargo.toml",
    "typescript": "package.json",
}

VERSION_FILE = "VERSION"

_LANGUAGES_WITH_SEPARATE_VERSION = frozenset(
    {
        "python",
        "rust",
        "go",
        "typescript",
    }
)

_SECONDARY_VERSION_FILES: dict[str, str] = {
    ".claude-plugin/plugin.json": "claude-plugin",
}


class VersionSyncError(Exception):
    """Raised when VERSION and language-specific file disagree."""


class CargoVersionError(Exception):
    """A Cargo manifest's version cannot be read, or a bump of it would break the workspace.

    Deliberately not a :class:`ValueError`, so a caller catching one for
    another purpose never hides it and leaves ``VERSION`` and ``Cargo.toml``
    out of step.
    """


class VersionFileError(Exception):
    """A version file holds no version that can be read (#5019).

    Each expected problem with ``pyproject.toml``, ``version.go``,
    ``Cargo.toml`` or ``.claude-plugin/plugin.json`` (a file that does not
    parse, no version where one belongs) is raised as this error, naming the
    file and the fix, so the ``vrg-version`` entry point can print it as one
    line. Deliberately not a :class:`ValueError` or :class:`KeyError`, which
    the entry point never catches: one of those is a bug and keeps its
    traceback.
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


_JSON_DECODER = json.JSONDecoder()

_PACKAGE_JSON = "package.json"

# The two root version fields of an npm lockfile (lockfileVersion 2 and 3).
_PACKAGE_LOCK_VERSION_PATHS: tuple[tuple[str, ...], ...] = (
    ("version",),
    ("packages", "", "version"),
)


def _skip_json_whitespace(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\n\r":
        pos += 1
    return pos


def _json_member_span(text: str, start: int, key: str) -> tuple[int, int] | None:
    """The ``(start, end)`` span of member *key*'s value in the JSON object at *start*.

    *text* is valid JSON and *start* the index of an object's ``{``. The
    members are walked by the JSON grammar, so any spacing works and a nested
    member of the same name is never taken. With a duplicate key the last one
    wins, as in :func:`json.loads`. ``None`` when the object has no such member.
    """
    found: tuple[int, int] | None = None
    pos = _skip_json_whitespace(text, start + 1)
    while text[pos] != "}":
        name, pos = _JSON_DECODER.raw_decode(text, pos)
        pos = _skip_json_whitespace(text, pos) + 1  # past the ':'
        value_start = _skip_json_whitespace(text, pos)
        _value, value_end = _JSON_DECODER.raw_decode(text, value_start)
        if name == key:
            found = (value_start, value_end)
        pos = _skip_json_whitespace(text, value_end)
        if text[pos] == ",":
            pos = _skip_json_whitespace(text, pos + 1)
    return found


def _json_path_label(path: tuple[str, ...]) -> str:
    return "".join(f"[{json.dumps(key)}]" for key in path)


def _parse_json_object(text: str, name: str) -> dict[str, Any]:
    """The JSON object *text*; :class:`VersionSyncError` naming *name* otherwise."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"{name} cannot be parsed: {exc}. Fix: correct the JSON"
        raise VersionSyncError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{name} cannot be parsed: the top level is not a JSON object. Fix: correct the JSON"
        raise VersionSyncError(msg)
    return data


def _rewrite_json_string(text: str, path: tuple[str, ...], new: str, name: str) -> str:
    """*text* with the string at member *path* set to *new*, every other byte kept.

    The value's exact span is found by walking the parsed structure, never by
    a text replace, so ``"version":"0.1.0"`` with no space is rewritten too.
    Raises :class:`VersionSyncError` naming *name* when *text* is not a JSON
    object or the value at *path* is not a string.
    """
    value: Any = _parse_json_object(text, name)
    span = (_skip_json_whitespace(text, 0), len(text))
    for key in path:
        if not isinstance(value, dict) or key not in value:
            msg = f"{name} has no {_json_path_label(path)}"
            raise VersionSyncError(msg)
        value = value[key]
        member = _json_member_span(text, span[0], key)
        if member is None:  # pragma: no cover - the parse above found the key
            msg = f"cannot locate {_json_path_label(path)} in {name}"
            raise VersionSyncError(msg)
        span = member
    if not isinstance(value, str):
        msg = f"{name} {_json_path_label(path)} is not a string"
        raise VersionSyncError(msg)
    return text[: span[0]] + json.dumps(new) + text[span[1] :]


def _package_json_version(text: str, canonical: str) -> str:
    """The top-level ``version`` of the ``package.json`` *text*.

    Raises :class:`VersionSyncError` naming the fix, setting it to the
    VERSION file's *canonical*, when it is missing or not a string.
    """
    return _manifest_version(_parse_json_object(text, _PACKAGE_JSON), canonical)


def _manifest_version(manifest: dict[str, object], canonical: str) -> str:
    """The top-level ``version`` of the parsed ``package.json`` *manifest*.

    Raises :class:`VersionSyncError` as :func:`_package_json_version` does.
    """
    version = manifest.get("version")
    if not isinstance(version, str):
        what = "no" if version is None else "a non-string"
        msg = (
            f'{_PACKAGE_JSON} has {what} "version". Fix: set it to the VERSION file\'s {canonical}'
        )
        raise VersionSyncError(msg)
    return version


def _version_mismatch(rel: str, canonical: str, found: str, language: str | None) -> str:
    msg = f"VERSION contains {canonical} but {rel} contains {found}"
    if language == "typescript":
        msg += f". Fix: set it to the VERSION file's {canonical}"
    return msg


def _read_typescript_file(path: Path, fix: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        msg = f"{path.name} not found at {path.parent}. Fix: {fix}"
        raise VersionSyncError(msg) from exc
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read {path.name}: {exc}"
        raise VersionSyncError(msg) from exc


def _write_typescript_file(path: Path, text: str) -> None:
    _write_version_text(path, text, encoding="utf-8")


def _read_version_text(path: Path) -> str:
    """The text of the version file *path*; :class:`VersionSyncError` naming it otherwise."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read {path.name}: {exc}. Fix: make it a readable text file"
        raise VersionSyncError(msg) from exc


def _write_version_text(path: Path, text: str, *, encoding: str | None = None) -> None:
    """Write *text* to the version file *path*; :class:`VersionSyncError` naming it otherwise."""
    try:
        path.write_text(text, encoding=encoding)
    except OSError as exc:
        msg = f"cannot write {path.name}: {exc}. Fix: make it writable, then bump again"
        raise VersionSyncError(msg) from exc


def _rewrite_package_json_version(text: str, old: str, new: str) -> str:
    """``package.json`` *text* with its ``version`` moved from *old* to *new*."""
    current = _package_json_version(text, old)
    if current != old:
        raise VersionSyncError(_version_mismatch(_PACKAGE_JSON, old, current, "typescript"))
    return _rewrite_json_string(text, ("version",), new, _PACKAGE_JSON)


def _rewrite_package_lock_version(text: str, new: str, name: str) -> str:
    for path in _PACKAGE_LOCK_VERSION_PATHS:
        text = _rewrite_json_string(text, path, new, name)
    return text


def _write_package_lock_version(lockfile: Path, new: str) -> None:
    """Set ``package-lock.json``'s top-level ``version`` and ``packages[""].version``.

    A direct edit of those two values in place: it runs no npm, so no script
    runs and no dependency is re-resolved, and every other byte is kept
    (epic vergil-project/.github#1174, spec §6.5). Raises
    :class:`VersionSyncError` naming the file when it is missing, cannot be
    parsed, lacks either field, or cannot be written.
    """
    text = _read_typescript_file(lockfile, "run npm install to create it, then commit it")
    _write_typescript_file(lockfile, _rewrite_package_lock_version(text, new, lockfile.name))


def _check_typescript_bump(repo_root: Path, old: str, new: str) -> None:
    """Everything a TypeScript bump checks before it writes any file.

    ``package.json`` must exist and hold the version *old*, and both root
    version fields of ``package-lock.json`` must be present. Raises
    :class:`VersionSyncError` naming the file and the fix otherwise.
    """
    package_json = repo_root / _PACKAGE_JSON
    text = _read_typescript_file(package_json, f'create it with "version": "{old}"')
    _rewrite_package_json_version(text, old, new)
    lockfile = repo_root / _LOCKFILES["typescript"]
    lock_text = _read_typescript_file(lockfile, "run npm install to create it, then commit it")
    _rewrite_package_lock_version(lock_text, new, lockfile.name)


def _discover_version_file(repo_root: Path, language: str) -> Path | None:
    """The version file *language* keeps beside VERSION, or ``None`` when it has none.

    A language with a fixed file name gets its path whether or not the file
    exists. Go's file is found by search, so a Go repo with no
    ``**/version.go`` has no version file: ``None``, an explicit answer
    rather than an exception, so no caller needs a handler that would also
    hide a real failure to read or write the file (#4588). A language this
    function does not know raises :class:`ValueError`: that is a bug in the
    caller, never a missing file.
    """
    if language in _DEFAULT_VERSION_FILES:
        return repo_root / _DEFAULT_VERSION_FILES[language]

    if language == "go":
        matches = [m for m in repo_root.glob("**/version.go") if ".git" not in m.parts]
        return matches[0] if matches else None

    msg = f"Unsupported language for version discovery: {language}"
    raise ValueError(msg)


def _cross_check_language_file(
    repo_root: Path, language: str | None, canonical_version: str
) -> None:
    if language is not None and language in _LANGUAGES_WITH_SEPARATE_VERSION:
        lang_file = _discover_version_file(repo_root, language)
        if lang_file is None:
            print(
                f"warning: {language} version file not found; sync check skipped",
                file=sys.stderr,
            )
        else:
            if language == "typescript" and not lang_file.is_file():
                # A TypeScript repo's version lives in package.json too: a
                # missing one is an error to fix, never a skipped check.
                msg = (
                    f"{_PACKAGE_JSON} not found at {repo_root}. "
                    f'Fix: create it with "version": "{canonical_version}"'
                )
                raise VersionSyncError(msg)
            if not lang_file.is_file():
                rel = lang_file.relative_to(repo_root)
                print(
                    f"warning: {rel} not found; sync check skipped",
                    file=sys.stderr,
                )
            else:
                if language == "typescript":
                    # The shared reader, so a package.json that is not UTF-8 or
                    # not JSON fails with its message, never a traceback (#4992),
                    # and the TypeScript pre-gate reports it once.
                    try:
                        manifest = read_package_json(lang_file)
                    except PackageJsonError as exc:
                        raise VersionSyncError(str(exc)) from exc
                    lang_version = _manifest_version(manifest, canonical_version)
                else:
                    lang_version = _read_version(
                        _read_version_text(lang_file),
                        language,
                        lang_file.relative_to(repo_root).as_posix(),
                    )
                if lang_version != canonical_version:
                    lang_rel = lang_file.relative_to(repo_root).as_posix()
                    msg = _version_mismatch(lang_rel, canonical_version, lang_version, language)
                    raise VersionSyncError(msg)

    for rel_path, fmt in _SECONDARY_VERSION_FILES.items():
        secondary = repo_root / rel_path
        if secondary.is_file():
            sec_version = _read_version(_read_version_text(secondary), fmt, rel_path)
            if sec_version != canonical_version:
                msg = f"VERSION contains {canonical_version} but {rel_path} contains {sec_version}"
                raise VersionSyncError(msg)


def _parse_version_toml(text: str, name: str) -> dict[str, Any]:
    """The TOML *text* of the version file *name*; :class:`VersionFileError` otherwise."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{name} is not valid TOML: {exc}. Fix: correct the TOML"
        raise VersionFileError(msg) from exc


def _read_python_version(text: str, name: str) -> str:
    """The ``[project].version`` of the ``pyproject.toml`` *text*, called *name*."""
    project = _parse_version_toml(text, name).get("project")
    if not isinstance(project, dict):
        problem = "no [project] table"
    elif "version" not in project:
        problem = "no [project] version"
    elif not isinstance(project["version"], str):
        problem = "a [project] version that is not a string"
    else:
        return project["version"]
    msg = (
        f"{name} has {problem}. Fix: set [project] version to the VERSION file's "
        'version, as version = "<version>"'
    )
    raise VersionFileError(msg)


def _read_plugin_version(text: str, name: str) -> str:
    """The ``version`` of the ``plugin.json`` *text*, called *name*."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"{name} is not valid JSON: {exc}. Fix: correct the JSON"
        raise VersionFileError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{name} is not a JSON object. Fix: make its top level an object"
        raise VersionFileError(msg)
    version = data.get("version")
    if not isinstance(version, str):
        problem = "no version" if version is None else "a version that is not a string"
        msg = (
            f"{name} has {problem}. Fix: set its version to the VERSION file's "
            'version, as "version": "<version>"'
        )
        raise VersionFileError(msg)
    return version


def _read_version(text: str, language: str, name: str | None = None) -> str:
    """The version in *text*, a version file of *language* called *name*.

    *name* names the file in an error; it defaults to the language's usual
    file name. A version file that does not parse or holds no version raises
    :class:`VersionFileError`, or for Cargo.toml's version table
    :class:`CargoVersionError` (#5019).
    """
    if language == "python":
        return _read_python_version(text, name or "pyproject.toml")

    if language == "rust":
        return _cargo_version(_parse_version_toml(text, name or "Cargo.toml"))

    if language == "go":
        m = _GO_VERSION_RE.search(text)
        if not m:
            msg = (
                f'{name or "version.go"} has no Version = "..." line. '
                'Fix: declare the version in it as Version = "<version>"'
            )
            raise VersionFileError(msg)
        return m.group(1)

    if language == "claude-plugin":
        return _read_plugin_version(text, name or ".claude-plugin/plugin.json")

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
    if language == "typescript":
        text = _read_typescript_file(version_file, f'create it with "version": "{old}"')
        _write_typescript_file(version_file, _rewrite_package_json_version(text, old, new))
        return

    text = _read_version_text(version_file)

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

    _write_version_text(version_file, text)


def _run_lockfile_maintenance(repo_root: Path, language: str) -> None:
    """Refresh *language*'s lockfile for the bumped version, in the dev container.

    The lock command runs through ``vrg-container-run``, so a release needs no
    host toolchain for any language and the lockfile is written by the same
    ``uv`` or ``cargo`` that validation checks it with (#4407). A missing or
    stopped container runtime, or a failing lock command, raises
    :class:`subprocess.CalledProcessError` after its output is shown.

    TypeScript runs no command: its lockfile's root version fields are set
    to the VERSION file's version, which :func:`bump` has just written, by
    :func:`_write_package_lock_version`.
    """
    if language == "typescript":
        new = (repo_root / VERSION_FILE).read_text().strip()
        _write_package_lock_version(repo_root / _LOCKFILES[language], new)
        return
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
        if cargo_toml is not None and cargo_toml.is_file():
            _check_cargo_bump(repo_root, cargo_toml, old_version, new_version)

    # Likewise for TypeScript: a missing package.json or lockfile, a missing
    # or mismatched version, or a lockfile with no root version fields fails
    # before VERSION is written.
    if language == "typescript":
        _check_typescript_bump(repo_root, old_version, new_version)

    # All or nothing (#4587): every path the bump may write is saved first, and
    # any failure from here on, the lockfile step's included, puts them back
    # before the error propagates, so no half-bumped tree is left behind.
    saved = _save_paths(repo_root, bump_paths(repo_root))
    try:
        _write_bump(repo_root, language, old_version, new_version)
    except BaseException:
        _restore_paths(repo_root, saved, new_version)
        raise
    return new_version


def _save_paths(repo_root: Path, paths: frozenset[str]) -> dict[str, bytes | None]:
    """The bytes of each of *paths* under *repo_root*, or ``None`` for one that is absent."""
    saved: dict[str, bytes | None] = {}
    for rel in sorted(paths):
        path = repo_root / rel
        saved[rel] = path.read_bytes() if path.exists() else None
    return saved


def _restore_paths(repo_root: Path, saved: dict[str, bytes | None], new_version: str) -> None:
    """Put every path in *saved* back as it was, after a failed bump to *new_version*.

    A path that was absent is removed, since a lock command may have created
    it. A path that cannot be restored is named in a :class:`VersionSyncError`,
    raised in place of the original failure (which it chains), so a
    half-bumped tree is never silent.
    """
    failed: list[str] = []
    for rel, data in saved.items():
        path = repo_root / rel
        try:
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        except OSError as exc:
            failed.append(f"{rel} ({exc})")
    if failed:
        msg = (
            f"bump to {new_version} failed and these files could not be restored: "
            f"{', '.join(failed)}. The tree is half-bumped. "
            "Fix: restore them with git checkout, then re-run the bump."
        )
        raise VersionSyncError(msg)
    print(
        f"vrg-version: bump to {new_version} failed; "
        f"{', '.join(saved)} are back as they were before the bump.",
        file=sys.stderr,
    )


def _write_bump(repo_root: Path, language: str | None, old_version: str, new_version: str) -> None:
    """Write *new_version* to every version file, then refresh the lockfile."""
    (repo_root / VERSION_FILE).write_text(new_version + "\n")

    if language == "typescript":
        _write_version(repo_root / _PACKAGE_JSON, language, old_version, new_version)
    elif language is not None and language in _LANGUAGES_WITH_SEPARATE_VERSION:
        # No version file (a Go repo with no version.go, or a missing
        # pyproject.toml or Cargo.toml) means VERSION alone is bumped, as
        # #1165 Task 5 kept. Any failure to read or write a file that does
        # exist propagates, and bump() restores every saved path (#4588).
        lang_file = _discover_version_file(repo_root, language)
        if lang_file is not None and lang_file.is_file():
            _write_version(lang_file, language, old_version, new_version)

    for rel_path, fmt in _SECONDARY_VERSION_FILES.items():
        secondary = repo_root / rel_path
        if secondary.is_file():
            _write_version(secondary, fmt, old_version, new_version)

    if language is not None:
        _run_lockfile_maintenance(repo_root, language)


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
        lang_file = _discover_version_file(repo_root, language)
        # With no version file bump() writes none, so none is listed: a dirty
        # path outside this set is refused by the caller, never ignored.
        if lang_file is not None:
            paths.add(lang_file.relative_to(repo_root).as_posix())
    if language is not None and language in _LOCKFILES:
        paths.add(_LOCKFILES[language])
    return frozenset(paths)
