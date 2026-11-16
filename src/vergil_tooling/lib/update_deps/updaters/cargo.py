"""Cargo updater: ``cargo update`` plus the held-back report (spec §6.6).

``cargo update`` runs in the dev container and moves every locked crate to
the newest version its ``Cargo.toml`` requirement allows; it never edits a
``Cargo.toml``. The packages it moved are itemized from the parsed
``Cargo.lock`` before and after.

Afterwards every direct dependency of every workspace member (normal, dev and
build) whose locked version is below the newest stable, unyanked version in
Cargo's sparse index is reported as held back, with the reason: the newest
version is outside the requirement, it needs a newer Rust than the package's
``rust-version``, both, or neither, in which case another requirement in the
dependency graph holds it. The dependencies come from ``cargo metadata``, run
in the container like ``cargo update``, so host tooling never needs a Rust
toolchain.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import cargo_semver, git, progress
from vergil_tooling.lib.update_deps import sparse_index
from vergil_tooling.lib.update_deps.changes import NEW, REMOVED, diff_versions
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.failure import combined_output
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.changes import PackageChange
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext
    from vergil_tooling.lib.update_deps.sparse_index import Fetch, IndexVersion

UPDATE_CMD = ("vrg-container-run", "--", "cargo", "update")
METADATA_CMD = (
    "vrg-container-run",
    "--",
    "cargo",
    "metadata",
    "--format-version",
    "1",
    "--locked",
)

COMMIT_SUBJECT = "chore(deps): cargo update"
REFRESH_COMMIT_MESSAGE = "chore(deps): refresh Cargo.lock"

OUTSIDE_REQUIREMENT = "outside the Cargo.toml requirement"
NEEDS_NEWER_RUST = "needs a newer Rust than rust-version"
HELD_BY_RESOLVER = "held by the resolver: another requirement in the dependency graph"

# crates.io as Cargo.lock and cargo metadata name it: the git-protocol URL is
# crates.io's canonical identity even when the sparse protocol fetches it.
CRATES_IO_SOURCES = frozenset(
    {
        "registry+https://github.com/rust-lang/crates.io-index",
        "sparse+https://index.crates.io/",
    }
)

# How many moved crates the one-line summary names before it counts the rest.
_SUMMARY_LIMIT = 5


def _base(ctx: UpdateDepsContext) -> Path:
    return ctx.worktree_path if ctx.worktree_path is not None else ctx.repo_root


def read_toml(path: Path) -> dict[str, Any]:
    """Parse the TOML file at *path*; an unreadable or invalid file raises, naming it."""
    try:
        return tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UpdateDepsError(
            phase="update",
            command=str(path),
            message=f"Cannot read {path.name}: {exc}",
        ) from exc


def _lock_packages(lock: dict[str, Any], *, where: str) -> list[dict[str, Any]]:
    packages = lock.get("package", [])
    if not isinstance(packages, list) or not all(
        isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("version"), str)
        for p in packages
    ):
        raise UpdateDepsError(
            phase="update",
            command=where,
            message=f"{where}: every [[package]] entry needs a string name and version.",
        )
    return packages


def locked_package_versions(
    lock: dict[str, Any], *, where: str = "Cargo.lock"
) -> dict[str, list[str]]:
    """Crate name to every version ``Cargo.lock`` locks for it (a crate can be locked twice)."""
    found: dict[str, list[str]] = {}
    for p in _lock_packages(lock, where=where):
        found.setdefault(p["name"], []).append(p["version"])
    return found


def _locked_with_sources(lock: dict[str, Any]) -> dict[str, list[tuple[str, str | None]]]:
    """Crate name to every ``(version, source)`` locked for it; a path crate has no source."""
    found: dict[str, list[tuple[str, str | None]]] = {}
    for p in _lock_packages(lock, where="Cargo.lock"):
        source = p.get("source")
        found.setdefault(p["name"], []).append(
            (p["version"], source if isinstance(source, str) else None)
        )
    return found


def _move(change: PackageChange) -> str:
    return f"{change.name} {change.old or NEW} -> {change.new or REMOVED}"


def _summary(moves: list[str]) -> str:
    if len(moves) <= _SUMMARY_LIMIT:
        return "; ".join(moves)
    rest = len(moves) - _SUMMARY_LIMIT
    return "; ".join(moves[:_SUMMARY_LIMIT]) + f"; and {rest} more"


def run_captured(cmd: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing its output."""
    return subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603


def cargo_metadata() -> dict[str, Any]:
    """``cargo metadata`` for the repo, run in the dev container.

    ``vrg-container-run`` prints its own header on stdout before the command
    runs; ``cargo metadata`` prints its JSON as one line, so the last
    non-blank line is the document.
    """
    command = " ".join(METADATA_CMD)
    result = run_captured(METADATA_CMD)
    if result.returncode != 0:
        raise UpdateDepsError(
            phase="update",
            command=command,
            message=f"cargo metadata failed (exit {result.returncode}).",
            detail="\n".join(s.rstrip("\n") for s in (result.stdout, result.stderr) if s.strip()),
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        data = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as exc:
        raise UpdateDepsError(
            phase="update",
            command=command,
            message=f"cargo metadata printed no JSON document: {exc.msg}.",
            detail=result.stdout,
        ) from exc
    if not isinstance(data, dict):
        raise UpdateDepsError(
            phase="update",
            command=command,
            message="cargo metadata printed no JSON object.",
            detail=result.stdout,
        )
    return data


def _metadata_error(message: str) -> UpdateDepsError:
    return UpdateDepsError(phase="update", command=" ".join(METADATA_CMD), message=message)


def _members(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """The workspace members' package entries, in ``workspace_members`` order."""
    ids = metadata.get("workspace_members")
    packages = metadata.get("packages")
    if not isinstance(ids, list) or not isinstance(packages, list):
        raise _metadata_error("cargo metadata has no workspace_members or packages list.")
    by_id = {p.get("id"): p for p in packages if isinstance(p, dict)}
    members: list[dict[str, Any]] = []
    for member_id in ids:
        package = by_id.get(member_id)
        if package is None:
            raise _metadata_error(
                f"cargo metadata lists workspace member {member_id!r} with no package."
            )
        members.append(package)
    return members


def _locked_version(
    name: str, req: str, locked: dict[str, list[tuple[str, str | None]]], member: str
) -> str | None:
    """The highest crates.io version locked for *name* that satisfies *req*.

    ``None`` when the dependency is declared from crates.io but locked only
    from a path or git crate, as a ``[patch.crates-io]`` or ``[replace]``
    override does: like a path or git dependency, the crate it resolves to has
    no index entry to compare against, so it is skipped. A dependency with no
    matching lock entry at all fails loud.
    """
    matching = [(v, s) for v, s in locked.get(name, []) if _matches(req, v, name=name)]
    candidates = [v for v, s in matching if s in CRATES_IO_SOURCES]
    if not candidates and matching:
        return None
    if not candidates:
        raise UpdateDepsError(
            phase="update",
            command="Cargo.lock",
            message=(
                f"{member}: dependency {name} {req!r} has no matching version locked "
                "from crates.io in Cargo.lock."
            ),
        )
    return max(candidates, key=sparse_index.version_key)


def _matches(req: str, version: str, *, name: str) -> bool:
    try:
        return cargo_semver.matches(req, version)
    except ValueError as exc:
        raise UpdateDepsError(
            phase="update",
            command="cargo_semver.matches",
            message=f"Cannot match {name} {version} against requirement {req!r}: {exc}",
        ) from exc


def _needs_newer_rust(member_rust: str | None, newest: IndexVersion, member: str) -> bool:
    if member_rust is None or newest.rust_version is None:
        return False
    try:
        member_key = sparse_index.rust_version_key(member_rust)
    except ValueError as exc:
        raise _metadata_error(f"{member}: {exc}") from exc
    return sparse_index.rust_version_key(newest.rust_version) > member_key


def _note(*, outside: bool, rust: bool) -> str:
    reasons = [
        r for r, applies in ((OUTSIDE_REQUIREMENT, outside), (NEEDS_NEWER_RUST, rust)) if applies
    ]
    return "; ".join(reasons) if reasons else HELD_BY_RESOLVER


def _direct_dependencies(member: dict[str, Any]) -> list[dict[str, Any]]:
    """The member's crates.io dependencies, normal, dev and build alike.

    Path and git dependencies are skipped: they have no index entry to compare
    against. A dependency from any other registry fails loud, since its newest
    version is not in crates.io's index and skipping it would hide it.
    """
    member_name = str(member.get("name"))
    deps = member.get("dependencies")
    if not isinstance(deps, list):
        raise _metadata_error(f"{member_name}: cargo metadata has no dependencies list.")
    found: list[dict[str, Any]] = []
    for dep in deps:
        if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
            raise _metadata_error(f"{member_name}: malformed dependency entry {dep!r}.")
        source = dep.get("source")
        if source is None or (isinstance(source, str) and source.startswith("git+")):
            continue
        if source not in CRATES_IO_SOURCES:
            raise _metadata_error(
                f"{member_name}: dependency {dep['name']} comes from {source!r}, not crates.io; "
                "its held-back status cannot be checked."
            )
        if not isinstance(dep.get("req"), str):
            raise _metadata_error(f"{member_name}: dependency {dep['name']} has no requirement.")
        found.append(dep)
    return found


def held_back(ctx: UpdateDepsContext, fetch: Fetch = sparse_index.fetch_url) -> list[HeldBack]:
    """Every direct crates.io dependency locked below its newest stable, unyanked version.

    A workspace-inherited dependency (``foo.workspace = true``) is checked
    against the requirement ``cargo metadata`` reports for it, which is the
    workspace's. Identical entries from several members or tables are
    reported once.
    """
    locked = _locked_with_sources(read_toml(_base(ctx) / "Cargo.lock"))
    metadata = cargo_metadata()
    newest_by_crate: dict[str, IndexVersion | None] = {}
    entries: list[HeldBack] = []
    for member in _members(metadata):
        member_name = str(member.get("name"))
        member_rust = member.get("rust_version")
        if member_rust is not None and not isinstance(member_rust, str):
            raise _metadata_error(f"{member_name}: rust_version is not a string.")
        for dep in _direct_dependencies(member):
            name, req = dep["name"], dep["req"]
            current = _locked_version(name, req, locked, member_name)
            if current is None:
                continue
            if name not in newest_by_crate:
                newest_by_crate[name] = sparse_index.newest_stable(
                    sparse_index.versions(name, fetch)
                )
            newest = newest_by_crate[name]
            if newest is None or sparse_index.version_key(current) >= sparse_index.version_key(
                newest.vers
            ):
                continue
            entry = HeldBack(
                source="cargo",
                name=name,
                current=current,
                latest=newest.vers,
                note=_note(
                    outside=not _matches(req, newest.vers, name=name),
                    rust=_needs_newer_rust(member_rust, newest, member_name),
                ),
            )
            if entry not in entries:
                entries.append(entry)
    return entries


class CargoUpdater:
    """Update Cargo.lock within the Cargo.toml requirements."""

    name = "cargo"

    def __init__(self, fetch: Fetch = sparse_index.fetch_url) -> None:
        self._fetch = fetch

    def applies(self, ctx: UpdateDepsContext) -> bool:
        root = ctx.repo_root
        return (root / "Cargo.toml").is_file() and (root / "Cargo.lock").is_file()

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        # Read the manifest first, so a corrupt one fails here, naming it.
        read_toml(base / "Cargo.toml")
        before = locked_package_versions(read_toml(base / "Cargo.lock"))
        try:
            progress.run(list(UPDATE_CMD))
        except subprocess.CalledProcessError as exc:
            raise UpdateDepsError(
                phase="update",
                command=" ".join(UPDATE_CMD),
                message=f"cargo update failed (exit {exc.returncode}).",
                detail=combined_output(exc),
            ) from exc
        dirty = bool(git.read_output("status", "--porcelain", "Cargo.lock").strip())
        packages = diff_versions(before, locked_package_versions(read_toml(base / "Cargo.lock")))
        moves = [_move(p) for p in packages]
        if moves:
            summary = _summary(moves)
            commit_message = COMMIT_SUBJECT + "\n\n" + "\n".join(f"- {m}" for m in moves)
        elif dirty:
            summary = "refreshed Cargo.lock (no package versions changed)"
            commit_message = REFRESH_COMMIT_MESSAGE
        else:
            summary = "cargo update (no changes)"
            commit_message = COMMIT_SUBJECT
        return UpdateResult(
            updater=self.name,
            changed=dirty,
            summary=summary,
            commit_message=commit_message,
            held_back=held_back(ctx, self._fetch),
            packages=packages,
        )
