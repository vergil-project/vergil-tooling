"""The TypeScript pre-gate checks: the repo's own tools and its ``build`` script.

TypeScript gates run the tools the repo declares, from ``node_modules/.bin``
(epic vergil-project/.github#1174, spec §6.3). Before they run,
``vrg-validate`` checks that ``package.json`` declares each required
devDependency, and that both its declared range and its installed version sit
inside the accepted major kept in ``configs/typescript/dev-dependencies.json``,
the one place the majors live. It also checks that ``package.json`` defines
the ``build`` script the BUILD check and the release build run (spec §6.5).

Every check in :data:`PRE_GATE_CHECKS` takes the repo root and returns its
problems, empty when all pass; the pre-gate stage fails with every problem from
all of them at once.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import TYPE_CHECKING

from vergil_tooling.lib import version
from vergil_tooling.lib.container import PackageJsonError, read_package_json

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REQUIRED_FILE = "dev-dependencies.json"

_INSTALLED_MAJOR = re.compile(r"(\d+)\.")


def load_required() -> dict[str, int]:
    """Return each required devDependency mapped to its accepted major.

    Read from the packaged ``configs/typescript/dev-dependencies.json``. A
    packaged file that is not a JSON object of package names to positive
    integer majors is a defect in vergil-tooling, so it raises ``ValueError``.
    """
    resource = files("vergil_tooling.configs").joinpath("typescript", _REQUIRED_FILE)
    data = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        msg = f"configs/typescript/{_REQUIRED_FILE}: not a non-empty JSON object"
        raise ValueError(msg)
    required: dict[str, int] = {}
    for name, major in data.items():
        if isinstance(major, bool) or not isinstance(major, int) or major < 1:
            msg = (
                f"configs/typescript/{_REQUIRED_FILE}: {name!r} maps to {major!r}, "
                "not a positive integer major"
            )
            raise ValueError(msg)
        required[name] = major
    return required


def _range_in_major(declared: str, major: int) -> bool:
    """Whether *declared* is ``^M…``, ``~M…`` or ``M…`` with ``M`` == *major*.

    ``M…`` takes the X-range forms too (``6.x``, ``6.0.*``), which stay
    inside the major; any other range (``latest``, ``*``, ``>=5``) does not.
    """
    pattern = rf"[\^~]?{major}(\.(\d+|[xX*])){{0,2}}"
    return re.fullmatch(pattern, declared.strip()) is not None


def _installed_problem(repo_root: Path, name: str, major: int) -> str | None:
    """The problem with *name*'s installed copy, or ``None`` when it is in range."""
    manifest = repo_root / "node_modules" / name / "package.json"
    if not manifest.is_file():
        return f"{name}: declared but not installed (node_modules/{name}/package.json is absent)"
    try:
        version = read_package_json(manifest).get("version")
    except PackageJsonError as exc:
        return f"{name}: {exc}"
    if not isinstance(version, str):
        return f"{name}: node_modules/{name}/package.json has no version string"
    found = _INSTALLED_MAJOR.match(version)
    if found is None or int(found.group(1)) != major:
        return f"{name}: installed {version}, outside the accepted major {major}"
    return None


def check_dev_dependencies(repo_root: Path) -> list[str]:
    """Return the problems with the repo's required devDependencies.

    One problem per failing package, then one ``Fix:`` line naming the
    ``npm install --save-dev <name>@^<M> …`` that fixes them all. A package
    fails when ``devDependencies`` does not declare it, when its declared range
    is not ``^M…``, ``~M…`` or ``M…`` with ``M`` its accepted major, when it is
    not installed, or when the installed version has another major. A
    ``package.json`` that cannot be parsed is the one problem returned.
    """
    try:
        manifest = read_package_json(repo_root / "package.json")
    except PackageJsonError as exc:
        return [str(exc)]
    dev_deps = manifest.get("devDependencies")
    declared_deps = dev_deps if isinstance(dev_deps, dict) else {}
    problems: list[str] = []
    failing: list[str] = []
    for name, major in load_required().items():
        declared = declared_deps.get(name)
        if declared is None:
            problem: str | None = f"{name}: not in devDependencies"
        elif not isinstance(declared, str) or not _range_in_major(declared, major):
            problem = f"{name}: declares {json.dumps(declared)}, outside the accepted major {major}"
        else:
            problem = _installed_problem(repo_root, name, major)
        if problem is not None:
            problems.append(problem)
            failing.append(f"{name}@^{major}")
    if failing:
        problems.append(f"Fix: npm install --save-dev {' '.join(failing)}")
    return problems


def check_version_matches(repo_root: Path) -> list[str]:
    """Return the problem with the repo's versions, empty when they agree.

    The same cross-check ``vrg-version show`` makes, so a ``package.json``
    version that is missing or differs from ``VERSION`` fails on the PR, not
    first at ``vrg-release`` preflight (spec §6.5). A missing ``VERSION`` is
    the problem too.
    """
    try:
        version.show(repo_root)
    except (version.VersionSyncError, FileNotFoundError) as exc:
        return [str(exc)]
    return []


def check_build_script(repo_root: Path) -> list[str]:
    """Return the problem with the repo's ``build`` script, empty when it has one.

    The BUILD check and the release build run ``npm run build`` (spec §6.5),
    so ``package.json`` must define ``scripts.build`` as a non-empty string.
    A ``package.json`` that cannot be parsed is the one problem returned.
    """
    try:
        manifest = read_package_json(repo_root / "package.json")
    except PackageJsonError as exc:
        return [str(exc)]
    scripts = manifest.get("scripts")
    build = scripts.get("build") if isinstance(scripts, dict) else None
    if isinstance(build, str) and build.strip():
        return []
    return [
        'package.json: no "build" script (the BUILD check and the release build run it)',
        'Fix: add a "build" script to package.json (for example "tsc -p tsconfig.json")',
    ]


PRE_GATE_CHECKS: tuple[Callable[[Path], list[str]], ...] = (
    check_dev_dependencies,
    check_version_matches,
    check_build_script,
)
"""The TypeScript pre-gate checks, run in order; every problem is reported."""


def run_pre_gate_checks(repo_root: Path) -> list[str]:
    """Return every problem from every check in :data:`PRE_GATE_CHECKS`.

    A problem two checks share (a ``package.json`` neither can parse) is
    reported once, where it first appears.
    """
    problems = (problem for check in PRE_GATE_CHECKS for problem in check(repo_root))
    return list(dict.fromkeys(problems))
