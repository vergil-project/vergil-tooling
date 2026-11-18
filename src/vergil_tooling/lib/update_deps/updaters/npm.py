"""npm updater: ``npm update`` plus the held-back report (epic 1339, spec §6.2).

``npm update --ignore-scripts`` runs in the dev container and moves every
locked package to the newest version its ``package.json`` range allows; since
npm 8.3.2 it leaves ``package.json`` alone unless ``--save`` is given. A
``save`` setting in the repo's ``.npmrc`` would still rewrite it, so the
updater fails if ``package.json`` changed by a single byte. The packages it
moved are itemized from the parsed ``package-lock.json`` before and after, and
every entry it added or moved must carry the ``license`` the license gate
reads, so a missing one fails here rather than later on the update's PR.

Afterwards ``npm ci --ignore-scripts`` installs the updated lockfile and
``npm outdated --json`` reports, with npm's own range matching, each direct
dependency whose ``latest`` dist-tag is newer than its locked version. Every
such dependency is reported as held back, with the reason: outside its
``package.json`` range, held by the resolver, or both, plus a note when
Vergil's accepted major (``dev-dependencies.json``) is what holds it.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import progress, typescript_tools
from vergil_tooling.lib.container import PackageJsonError, read_package_json
from vergil_tooling.lib.update_deps import sparse_index
from vergil_tooling.lib.update_deps.changes import NEW, REMOVED, diff_versions
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.failure import combined_output
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult
from vergil_tooling.lib.update_deps.updaters.cargo import HELD_BY_RESOLVER, run_captured

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.update_deps.changes import PackageChange
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

    Run = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]

UPDATE_CMD = ("vrg-container-run", "--", "npm", "update", "--ignore-scripts")
CI_CMD = ("vrg-container-run", "--", "npm", "ci", "--ignore-scripts")
OUTDATED_CMD = ("vrg-container-run", "--", "npm", "outdated", "--json")

COMMIT_SUBJECT = "chore(deps): npm update"
REFRESH_COMMIT_MESSAGE = "chore(deps): refresh package-lock.json"

OUTSIDE_RANGE = "outside the package.json range"

PACKAGE_JSON = "package.json"
LOCKFILE = "package-lock.json"

# The package.json tables the held-back report covers (spec §6.2).
DEPENDENCY_TABLES = ("dependencies", "devDependencies", "optionalDependencies")

_NODE_MODULES = "node_modules/"

# Range specs that do not name a registry package. ``npm outdated`` reports
# such a dependency with a ``latest`` that is not a version (``git``,
# ``linked``) or not at all, and it has no registry release to be held back
# from, so it is skipped. A ``user/repo`` shorthand is a GitHub spec; an
# ``npm:`` alias is a registry package and is kept.
_NON_REGISTRY_PREFIXES = (
    "file:",
    "link:",
    "git:",
    "git+",
    "github:",
    "gitlab:",
    "bitbucket:",
    "gist:",
    "http:",
    "https:",
    "./",
    "../",
    "/",
    "~/",
)

# How many moved packages the one-line summary names before it counts the rest.
_SUMMARY_LIMIT = 5


def vergil_major_note(name: str, major: int) -> str:
    """The note for a package Vergil accepts at one major only."""
    return f"Vergil accepts {name} {major} only (dev-dependencies.json)"


def _base(ctx: UpdateDepsContext) -> Path:
    return ctx.worktree_path if ctx.worktree_path is not None else ctx.repo_root


def _read_error(path: Path, why: str) -> UpdateDepsError:
    return UpdateDepsError(
        phase="update", command=str(path), message=f"Cannot read {path.name}: {why}"
    )


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _read_error(path, exc.strerror or str(exc)) from exc


def read_package_manifest(path: Path) -> dict[str, Any]:
    """The parsed ``package.json`` at *path*; an unreadable or invalid one raises, naming it."""
    try:
        return read_package_json(path)
    except PackageJsonError as exc:
        raise _read_error(path, str(exc)) from exc


def read_lockfile(path: Path) -> dict[str, Any]:
    """The parsed ``package-lock.json`` at *path*; an unreadable or invalid one raises."""
    try:
        data = json.loads(_read_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _read_error(path, str(exc)) from exc
    if not isinstance(data, dict):
        raise _read_error(path, "the top level is not a JSON object")
    return data


def _lock_error(message: str) -> UpdateDepsError:
    return UpdateDepsError(phase="update", command=LOCKFILE, message=f"{LOCKFILE}: {message}")


def _installed_entries(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The lockfile's installed-package entries, keyed by their ``node_modules/…`` path.

    Only a v2 or v3 lockfile has the ``packages`` map; an older one raises,
    naming the fix. The root entry (``""``) and workspace entries (paths
    outside ``node_modules``) are the repo's own packages, and a ``link``
    entry points at one of them, so none of those is a dependency. Every
    other entry must carry a string ``version``.
    """
    version = lock.get("lockfileVersion")
    packages = lock.get("packages")
    if isinstance(version, bool) or not isinstance(version, int) or version < 2:
        raise _lock_error(
            f"lockfileVersion {version!r} is older than 2 and has no packages map; "
            "regenerate it with npm 7 or later (npm install --package-lock-only --ignore-scripts)."
        )
    if not isinstance(packages, dict):
        raise _lock_error("the packages map is missing or not an object.")
    found: dict[str, dict[str, Any]] = {}
    for key, entry in packages.items():
        if not isinstance(entry, dict):
            raise _lock_error(f"entry {key!r} is not an object.")
        if _NODE_MODULES not in key or entry.get("link") is True:
            continue
        if not isinstance(entry.get("version"), str):
            raise _lock_error(f"entry {key!r} has no string version.")
        found[key] = entry
    return found


def _package_name(key: str) -> str:
    """The package name of a ``node_modules/…`` lockfile key (its last ``node_modules/`` part)."""
    return key.rsplit(_NODE_MODULES, 1)[1]


def locked_package_versions(lock: dict[str, Any]) -> dict[str, list[str]]:
    """Package name to every version the lockfile locks for it (nested copies included)."""
    found: dict[str, list[str]] = {}
    for key, entry in _installed_entries(lock).items():
        found.setdefault(_package_name(key), []).append(entry["version"])
    return found


def _unlicensed_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """``name@version`` of each entry the update added or moved that has no ``license``."""
    old = {key: entry["version"] for key, entry in _installed_entries(before).items()}
    missing: list[str] = []
    for key, entry in _installed_entries(after).items():
        if old.get(key) == entry["version"]:
            continue
        license_value = entry.get("license")
        if not isinstance(license_value, str) or not license_value.strip():
            missing.append(f"{_package_name(key)}@{entry['version']}")
    return sorted(set(missing))


def _move(change: PackageChange) -> str:
    return f"{change.name} {change.old or NEW} -> {change.new or REMOVED}"


def _summary(moves: list[str]) -> str:
    if len(moves) <= _SUMMARY_LIMIT:
        return "; ".join(moves)
    rest = len(moves) - _SUMMARY_LIMIT
    return "; ".join(moves[:_SUMMARY_LIMIT]) + f"; and {rest} more"


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(s.rstrip("\n") for s in (result.stdout, result.stderr) if s and s.strip())


def _outdated_error(message: str, detail: str | None = None) -> UpdateDepsError:
    return UpdateDepsError(
        phase="update", command=" ".join(OUTDATED_CMD), message=message, detail=detail
    )


def outdated(run: Run = run_captured) -> dict[str, dict[str, Any]]:
    """``npm outdated --json`` for the repo, after ``npm ci``, both in the dev container.

    ``npm ci --ignore-scripts`` first installs the updated lockfile, so
    ``npm outdated`` compares the versions actually locked. ``vrg-container-run``
    prints its own header on stdout before the command runs, so the JSON
    document starts at the first line beginning ``{``.

    ``npm outdated`` exits 1 whenever anything is outdated, so its exit status
    alone is neither success nor failure: exit 0 with ``{}`` (nothing
    outdated) and exit 1 with a non-empty object are the normal cases. Every
    other combination, no JSON, malformed JSON, a JSON ``error`` object and a
    failed ``npm ci`` raise.
    """
    ci = run(CI_CMD)
    if ci.returncode != 0:
        raise UpdateDepsError(
            phase="update",
            command=" ".join(CI_CMD),
            message=f"npm ci failed (exit {ci.returncode}).",
            detail=_output(ci),
        )
    result = run(OUTDATED_CMD)
    lines = result.stdout.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    if start is None:
        raise _outdated_error(
            f"npm outdated printed no JSON document (exit {result.returncode}).", _output(result)
        )
    try:
        data: dict[str, Any] = json.loads("\n".join(lines[start:]))
    except json.JSONDecodeError as exc:
        raise _outdated_error(
            f"npm outdated printed malformed JSON: {exc.msg}.", _output(result)
        ) from exc
    # A document that starts with "{" parses to an object or not at all.
    if "error" in data:
        raise _outdated_error(
            f"npm outdated reported an error (exit {result.returncode}).", _output(result)
        )
    if (result.returncode, bool(data)) not in ((0, False), (1, True)):
        state = "packages" if data else "nothing"
        raise _outdated_error(
            f"npm outdated exited {result.returncode} reporting {state} outdated.",
            _output(result),
        )
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise _outdated_error(
                f"npm outdated reported {name} as {type(entry).__name__}, not an object."
            )
    return data


def _registry_spec(spec: str) -> bool:
    """Whether the ``package.json`` range *spec* names a registry package."""
    if spec.startswith(_NON_REGISTRY_PREFIXES):
        return False
    # ``user/repo`` (optionally ``#ref``) is GitHub shorthand; a scoped name never
    # appears as a range, and ``npm:@scope/name@range`` is an alias, kept above.
    return spec.startswith("npm:") or "/" not in spec


def _declared(manifest: dict[str, Any]) -> dict[str, str]:
    """Each direct dependency in the covered tables, mapped to its range spec."""
    found: dict[str, str] = {}
    for table in DEPENDENCY_TABLES:
        deps = manifest.get(table, {})
        if not isinstance(deps, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in deps.items()
        ):
            raise UpdateDepsError(
                phase="update",
                command=PACKAGE_JSON,
                message=f"{PACKAGE_JSON}: {table} must map package names to range strings.",
            )
        found.update(deps)
    return found


def _version_key(name: str, field: str, value: object) -> sparse_index.VersionKey:
    if not isinstance(value, str):
        raise _outdated_error(f"npm outdated reports no {field} version for {name}.")
    try:
        return sparse_index.version_key(value)
    except ValueError as exc:
        raise _outdated_error(f"npm outdated reports {name} {field} {value!r}: {exc}.") from exc


def held_back(ctx: UpdateDepsContext, run: Run = run_captured) -> list[HeldBack]:
    """Every direct registry dependency locked below npm's ``latest`` for it.

    ``current`` precedes ``latest`` under semver precedence, prereleases
    included, for an entry to be held back; one at or above ``latest`` (a
    prerelease ahead of the dist-tag, say) is not reported. Dependencies
    ``npm outdated`` reports that the covered tables do not declare, such as
    peer dependencies, are outside the report (spec §6.2).
    """
    base = _base(ctx)
    declared = _declared(read_package_manifest(base / PACKAGE_JSON))
    locked = _installed_entries(read_lockfile(base / LOCKFILE))
    report = outdated(run)
    majors = typescript_tools.load_required()
    entries: list[HeldBack] = []
    for name in sorted(report):
        spec = declared.get(name)
        if spec is None or not _registry_spec(spec):
            continue
        entry = report[name]
        current = entry.get("current")
        if current is None:
            # ``npm ci`` skips an optional dependency its platform excludes;
            # the lockfile still records the version it locked.
            lock_entry = locked.get(_NODE_MODULES + name)
            if lock_entry is None:
                raise _lock_error(
                    f"{name} is a direct dependency that npm outdated reports with no "
                    "current version, and the lockfile has no node_modules entry for it."
                )
            current = lock_entry["version"]
        current_key = _version_key(name, "current", current)
        wanted_key = _version_key(name, "wanted", entry.get("wanted"))
        latest_key = _version_key(name, "latest", entry.get("latest"))
        if current_key >= latest_key:
            continue
        reasons = [
            reason
            for reason, applies in (
                (OUTSIDE_RANGE, wanted_key < latest_key),
                (HELD_BY_RESOLVER, current_key < wanted_key),
            )
            if applies
        ]
        if name in majors:
            reasons.append(vergil_major_note(name, majors[name]))
        entries.append(
            HeldBack(
                source="npm",
                name=name,
                current=str(current),
                latest=str(entry["latest"]),
                note="; ".join(reasons),
            )
        )
    return entries


class NpmUpdater:
    """Update package-lock.json within the package.json ranges."""

    name = "npm"

    def __init__(self, run: Run = run_captured) -> None:
        self._run = run

    def applies(self, ctx: UpdateDepsContext) -> bool:
        root = ctx.repo_root
        return (root / PACKAGE_JSON).is_file() and (root / LOCKFILE).is_file()

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        manifest_path, lock_path = base / PACKAGE_JSON, base / LOCKFILE
        # Read both first, so a corrupt one fails here, naming it.
        read_package_manifest(manifest_path)
        manifest_before = _read_bytes(manifest_path)
        lock_before = _read_bytes(lock_path)
        before = read_lockfile(lock_path)
        before_versions = locked_package_versions(before)
        try:
            progress.run(list(UPDATE_CMD))
        except subprocess.CalledProcessError as exc:
            raise UpdateDepsError(
                phase="update",
                command=" ".join(UPDATE_CMD),
                message=f"npm update failed (exit {exc.returncode}).",
                detail=combined_output(exc),
            ) from exc
        if _read_bytes(manifest_path) != manifest_before:
            raise UpdateDepsError(
                phase="update",
                command=" ".join(UPDATE_CMD),
                message=(
                    f"npm update changed {PACKAGE_JSON}; the updater changes only {LOCKFILE}. "
                    "Check the repo's .npmrc for a save setting."
                ),
            )
        after = read_lockfile(lock_path)
        unlicensed = _unlicensed_changes(before, after)
        if unlicensed:
            raise _lock_error(
                "npm update added or moved entries with no license field: "
                + ", ".join(unlicensed)
                + ". The license gate needs one on every entry."
            )
        changed = _read_bytes(lock_path) != lock_before
        packages = diff_versions(before_versions, locked_package_versions(after))
        moves = [_move(p) for p in packages]
        if moves:
            summary = _summary(moves)
            commit_message = COMMIT_SUBJECT + "\n\n" + "\n".join(f"- {m}" for m in moves)
        elif changed:
            summary = f"refreshed {LOCKFILE} (no package versions changed)"
            commit_message = REFRESH_COMMIT_MESSAGE
        else:
            summary = "npm update (no changes)"
            commit_message = COMMIT_SUBJECT
        return UpdateResult(
            updater=self.name,
            changed=changed,
            summary=summary,
            commit_message=commit_message,
            held_back=held_back(ctx, self._run),
            packages=packages,
        )
