"""Add ``pytest-xdist`` to a repo's dev dependencies, in its declared shape.

The Task-9 fleet sweep (epic vergil-project/.github#333) propagates
``pytest-xdist`` into every Python repo so the shared TEST command can run the
suite in parallel (``-n auto --dist worksteal``). Repos declare dev
dependencies in several shapes, so :func:`add_xdist` first classifies the shape
via :func:`vergil_tooling.lib.dev_deps.classify_dev_deps` and then edits the
right place:

* ``UV_GROUPS`` — the ``dev`` array under ``[dependency-groups]``.
* ``PEP621_OPTIONAL`` — the dev/test group under
  ``[project.optional-dependencies]``.
* ``REQUIREMENTS_TXT`` — a ``requirements-dev.txt`` / ``requirements-test.txt``
  line.
* ``POETRY`` — the dev/test table under ``[tool.poetry…]``.

The edit is **idempotent**: if ``pytest-xdist`` is already declared the applicator
reports no change. For :attr:`~vergil_tooling.lib.dev_deps.DevDepShape.UNKNOWN` —
and for a recognized shape whose concrete target cannot be located — it makes
**no edit** and returns an :class:`~vergil_tooling.lib.fleet_sweep.AppResult`
flagged ``needs_followup=True`` so the driver reports loudly and files a
follow-up, never a silent no-op (repo "no silent failures" policy).
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib.dev_deps import DevDepShape, classify_dev_deps
from vergil_tooling.lib.fleet_sweep import AppResult

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_PACKAGE = "pytest-xdist"

#: Requirements filenames, checked in this order (matching ``dev_deps``), for the
#: requirements-file shape.
_REQUIREMENTS_FILES = ("requirements-dev.txt", "requirements-test.txt")

#: Preference order for the dependency group/table to place ``pytest-xdist`` in
#: when the repo declares several. ``pytest-xdist`` is a pytest plugin, so a
#: group that already carries ``pytest`` wins; otherwise a conventional dev/test
#: group is used.
_GROUP_PREFERENCE = ("dev", "test")


def _normalize_req_name(spec: str) -> str:
    """Return the bare, normalized distribution name from a requirement *spec*.

    Strips any version constraint / marker / extras and normalizes separators
    (PEP 503) so ``pytest_xdist>=3`` and ``pytest-xdist`` compare equal.
    """
    name = re.split(r"[<>=!~;\[ ]", spec.strip(), maxsplit=1)[0]
    return name.replace("_", "-").lower()


def _has_package(entries: Iterable[object]) -> bool:
    """Return ``True`` if ``pytest-xdist`` is already among *entries*."""
    return any(isinstance(e, str) and _normalize_req_name(e) == _PACKAGE for e in entries)


def _insert_into_array(text: str, header: str, key: str, package: str) -> str | None:
    """Insert ``"package"`` into the ``key = [...]`` array under ``[header]``.

    Returns the rewritten file text, or ``None`` if the section or the array
    could not be located. Handles both inline (``key = ["a"]``) and multiline
    array forms, preserving the surrounding layout with a minimal insertion.
    A dependency array never contains a ``]`` character, so matching to the
    first ``]`` is safe.
    """
    header_re = re.compile(r"^\[" + re.escape(header) + r"\][ \t]*$", re.MULTILINE)
    m = header_re.search(text)
    if m is None:
        return None
    body_start = m.end()
    next_header = re.compile(r"^\[", re.MULTILINE).search(text, body_start)
    body_end = next_header.start() if next_header else len(text)

    arr_re = re.compile(
        r"^([ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*\[)([^\]]*)(\])",
        re.MULTILINE,
    )
    am = arr_re.search(text, body_start, body_end)
    if am is None:
        return None

    open_bracket, inner, close_bracket = am.group(1), am.group(2), am.group(3)
    entry = f'"{package}"'
    if "\n" in inner:
        indent_match = re.search(r"\n([ \t]+)\S", inner)
        indent = indent_match.group(1) if indent_match else "    "
        trail_match = re.search(r"\n([ \t]*)\Z", inner)
        trailing_indent = trail_match.group(1) if trail_match else ""
        content = inner[: trail_match.start()] if trail_match else inner
        content = content.rstrip()
        if content and not content.endswith(","):
            content += ","
        new_inner = f"{content}\n{indent}{entry},\n{trailing_indent}"
    elif inner.strip():
        new_inner = f"{inner.rstrip()}, {entry}"
    else:
        new_inner = entry
    new_array = f"{open_bracket}{new_inner}{close_bracket}"
    return text[: am.start()] + new_array + text[am.end() :]


def _apply_uv_groups(worktree: Path) -> AppResult:
    """Add ``pytest-xdist`` to the ``dev`` array under ``[dependency-groups]``."""
    pyproject = worktree / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = data.get("dependency-groups", {}).get("dev")
    if not isinstance(dev, list):
        return _cannot_place("[dependency-groups] has no 'dev' array")
    if _has_package(dev):
        return _already("[dependency-groups].dev")
    new_text = _insert_into_array(
        pyproject.read_text(encoding="utf-8"), "dependency-groups", "dev", _PACKAGE
    )
    if new_text is None:  # pragma: no cover - guarded by the isinstance check above
        return _cannot_place("could not locate the [dependency-groups].dev array")
    pyproject.write_text(new_text, encoding="utf-8")
    return _added("[dependency-groups].dev")


def _select_group(groups: dict[str, object]) -> str | None:
    """Pick the dependency group/table to receive ``pytest-xdist``.

    Prefers a group that already carries ``pytest`` (the plugin belongs with its
    host), then a conventionally named ``dev``/``test`` group.
    """
    for name, entries in groups.items():
        if isinstance(entries, list) and _has_pytest(entries):
            return name
    for name in _GROUP_PREFERENCE:
        if name in groups:
            return name
    return None


def _has_pytest(entries: Iterable[object]) -> bool:
    """Return ``True`` if ``pytest`` itself is among *entries*."""
    return any(isinstance(e, str) and _normalize_req_name(e) == "pytest" for e in entries)


def _apply_pep621_optional(worktree: Path) -> AppResult:
    """Add ``pytest-xdist`` to the dev/test group under optional-dependencies."""
    pyproject = worktree / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    groups = data.get("project", {}).get("optional-dependencies", {})
    target = _select_group(groups)
    if target is None:
        return _cannot_place("no dev/test group found under [project.optional-dependencies]")
    entries = groups[target]
    if isinstance(entries, list) and _has_package(entries):
        return _already(f"[project.optional-dependencies].{target}")
    new_text = _insert_into_array(
        pyproject.read_text(encoding="utf-8"),
        "project.optional-dependencies",
        target,
        _PACKAGE,
    )
    if new_text is None:  # pragma: no cover - the target came from the parsed table
        return _cannot_place(f"could not locate the [project.optional-dependencies].{target} array")
    pyproject.write_text(new_text, encoding="utf-8")
    return _added(f"[project.optional-dependencies].{target}")


def _apply_requirements(worktree: Path) -> AppResult:
    """Append a ``pytest-xdist`` line to the repo's requirements-*.txt file."""
    for name in _REQUIREMENTS_FILES:
        path = worktree / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lines = [
            line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        ]
        if _has_package(lines):
            return _already(name)
        suffix = "" if text.endswith("\n") or text == "" else "\n"
        path.write_text(f"{text}{suffix}{_PACKAGE}\n", encoding="utf-8")
        return _added(name)
    # Unreachable via classify_dev_deps (it only returns REQUIREMENTS_TXT when a
    # file exists), but guarded so a shape/file mismatch never silently no-ops.
    return _cannot_place(
        "no requirements-dev.txt / requirements-test.txt found"
    )  # pragma: no cover


def _apply_poetry(worktree: Path) -> AppResult:
    """Add ``pytest-xdist = "*"`` to the dev/test poetry dependency table."""
    pyproject = worktree / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    poetry = data.get("tool", {}).get("poetry", {})
    header, table = _select_poetry_table(poetry)
    if header is None:
        return _cannot_place("no dev/test dependency table found under [tool.poetry]")
    if isinstance(table, dict) and _has_package(table):
        return _already(f"[{header}]")
    new_text = _insert_poetry_dependency(text, header, _PACKAGE)
    if new_text is None:  # pragma: no cover - the header came from the parsed table
        return _cannot_place(f"could not locate the [{header}] table")
    pyproject.write_text(new_text, encoding="utf-8")
    return _added(f"[{header}]")


def _select_poetry_table(poetry: dict[str, object]) -> tuple[str | None, object]:
    """Return the (header, table) poetry dev/test dependency table to edit.

    Prefers a ``[tool.poetry.group.<name>.dependencies]`` table (modern), then
    the legacy ``[tool.poetry.dev-dependencies]`` table.
    """
    groups = poetry.get("group")
    if isinstance(groups, dict):
        typed_groups = cast("dict[str, object]", groups)
        for name in _GROUP_PREFERENCE:
            group = typed_groups.get(name)
            if isinstance(group, dict):
                deps = cast("dict[str, object]", group).get("dependencies")
                if isinstance(deps, dict):
                    return f"tool.poetry.group.{name}.dependencies", deps
    if isinstance(poetry.get("dev-dependencies"), dict):
        return "tool.poetry.dev-dependencies", poetry["dev-dependencies"]
    return None, None


def _insert_poetry_dependency(text: str, header: str, package: str) -> str | None:
    """Insert ``package = "*"`` immediately after the ``[header]`` table line."""
    header_re = re.compile(r"^\[" + re.escape(header) + r"\][ \t]*$", re.MULTILINE)
    m = header_re.search(text)
    if m is None:
        return None
    insert_at = m.end()
    return f'{text[:insert_at]}\n{package} = "*"{text[insert_at:]}'


def _added(where: str) -> AppResult:
    return AppResult(changed=True, summary=f"added {_PACKAGE} to {where}")


def _already(where: str) -> AppResult:
    return AppResult(changed=False, summary=f"{_PACKAGE} already present in {where}")


def _cannot_place(reason: str) -> AppResult:
    """A recognized shape whose concrete target could not be located: no edit, loud."""
    return AppResult(
        changed=False,
        summary=f"{reason}; {_PACKAGE} NOT added — needs manual handling",
        needs_followup=True,
    )


_HANDLERS = {
    DevDepShape.UV_GROUPS: _apply_uv_groups,
    DevDepShape.PEP621_OPTIONAL: _apply_pep621_optional,
    DevDepShape.REQUIREMENTS_TXT: _apply_requirements,
    DevDepShape.POETRY: _apply_poetry,
}


def add_xdist(worktree: Path) -> AppResult:
    """Add ``pytest-xdist`` to ``worktree``'s dev deps in its declared shape.

    Dispatches on :func:`classify_dev_deps`. Idempotent (no-op when already
    present). For an :attr:`~DevDepShape.UNKNOWN` shape it makes **no** edit and
    returns an :class:`AppResult` flagged ``needs_followup=True`` so the sweep
    driver reports loudly and files a follow-up.
    """
    shape = classify_dev_deps(worktree)
    handler = _HANDLERS.get(shape)
    if handler is None:  # DevDepShape.UNKNOWN
        return AppResult(
            changed=False,
            summary=(
                f"could not classify the dev-dependency shape of {worktree}; "
                f"{_PACKAGE} NOT added — needs manual handling"
            ),
            needs_followup=True,
        )
    return handler(worktree)
