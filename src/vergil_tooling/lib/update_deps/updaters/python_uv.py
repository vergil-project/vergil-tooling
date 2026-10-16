"""Python language-library updater: ``uv lock --upgrade`` plus the held-back report.

The upgrade runs in the dev container. Afterwards every **directly declared**
requirement (spec §8.1: ``[project] dependencies``, every dependency group,
``[build-system] requires`` and ``[tool.uv] constraint-dependencies``) is
compared with its latest PyPI release, and any whose latest release falls
outside the repo's own specifier is reported as held back (spec §7.3).
Transitive packages are never reported.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from vergil_tooling.lib import git, progress
from vergil_tooling.lib.update_deps import pypi
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_COMMIT_MESSAGE = "chore(deps): uv lock --upgrade"

NOT_LOCKED = "(not locked)"


def _strings(values: Any) -> list[str]:
    # Dependency groups may hold {include-group = "..."} tables; only strings are requirements.
    return [v for v in (values or []) if isinstance(v, str)]


def direct_requirements(pyproject: dict[str, Any]) -> list[tuple[Requirement, str]]:
    """Directly declared requirements (spec §8.1), each with the table it came from.

    URL requirements are skipped: they have no index release to compare against.
    An unparseable requirement fails loud.
    """
    project = pyproject.get("project") or {}
    build_system = pyproject.get("build-system") or {}
    tool_uv = (pyproject.get("tool") or {}).get("uv") or {}
    sources: list[tuple[str, list[str]]] = [
        ("project.dependencies", _strings(project.get("dependencies"))),
    ]
    for group, values in (pyproject.get("dependency-groups") or {}).items():
        sources.append((f"dependency-groups.{group}", _strings(values)))
    sources.append(("build-system.requires", _strings(build_system.get("requires"))))
    sources.append(
        ("tool.uv.constraint-dependencies", _strings(tool_uv.get("constraint-dependencies")))
    )
    found: list[tuple[Requirement, str]] = []
    for where, values in sources:
        for text in values:
            try:
                req = Requirement(text)
            except InvalidRequirement as exc:
                raise UpdateDepsError(
                    phase="update",
                    command="pyproject.toml",
                    message=f"{where}: {text!r} is not a valid requirement: {exc}",
                ) from exc
            if req.url is None:
                found.append((req, where))
    return found


def locked_versions(lock: dict[str, Any]) -> dict[str, str]:
    """Canonical package name to locked version, from a parsed ``uv.lock``."""
    return {
        canonicalize_name(p["name"]): p["version"]
        for p in lock.get("package", [])
        if "version" in p
    }


def held_back(
    pyproject: dict[str, Any], lock: dict[str, Any], *, latest: Callable[[str], str]
) -> list[HeldBack]:
    """Direct requirements whose latest release is outside their own specifier."""
    locked = locked_versions(lock)
    entries: list[HeldBack] = []
    seen: set[tuple[str, str]] = set()
    for req, where in direct_requirements(pyproject):
        name = canonicalize_name(req.name)
        key = (name, str(req.specifier))
        # An unbounded requirement cannot hold anything back.
        if not req.specifier or key in seen:
            continue
        seen.add(key)
        newest = latest(name)
        try:
            newest_version = Version(newest)
        except InvalidVersion as exc:
            raise UpdateDepsError(
                phase="update",
                command=f"latest release of {name}",
                message=f"Latest release of {name!r} is not a valid version: {newest!r}",
            ) from exc
        if req.specifier.contains(newest_version, prereleases=True):
            continue
        entries.append(
            HeldBack(
                source="python",
                name=name,
                current=locked.get(name, NOT_LOCKED),
                latest=newest,
                note=f"{where}: {req}",
            )
        )
    return entries


def _base(ctx: UpdateDepsContext) -> Path:
    return ctx.worktree_path if ctx.worktree_path is not None else ctx.repo_root


class PythonUvUpdater:
    """Upgrade Python dependencies within their declared constraints."""

    name = "python"

    def __init__(self, latest: Callable[[str], str] = pypi.latest_version) -> None:
        self._latest = latest

    def applies(self, ctx: UpdateDepsContext) -> bool:
        root = ctx.repo_root
        return (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file()

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        progress.run(["vrg-container-run", "--", "uv", "lock", "--upgrade"])
        dirty = bool(git.read_output("status", "--porcelain", "uv.lock").strip())
        base = _base(ctx)
        pyproject = tomllib.loads((base / "pyproject.toml").read_text())
        lock = tomllib.loads((base / "uv.lock").read_text())
        return UpdateResult(
            updater=self.name,
            changed=dirty,
            summary="uv lock --upgrade" if dirty else "uv lock --upgrade (no changes)",
            commit_message=_COMMIT_MESSAGE,
            held_back=held_back(pyproject, lock, latest=self._latest),
        )
