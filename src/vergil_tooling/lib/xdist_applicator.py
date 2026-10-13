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
* ``POETRY`` — **not supported yet**: a Poetry repo needing the change is
  refused loudly (``needs_followup=True``, no edit) because the sweep does not
  run ``poetry lock``, and an edit without it would commit a stale
  ``poetry.lock``. One that already declares ``pytest-xdist`` is reported as
  unchanged.

**Lockfile refresh (#2980).** Editing ``pyproject.toml`` in a uv-managed repo
(one carrying a ``uv.lock``) without re-locking leaves the lockfile stale, and
the PR then fails the audit gate (``uv lock --check``). So after a
``UV_GROUPS`` / ``PEP621_OPTIONAL`` edit, when ``uv.lock`` is present, the
applicator runs ``uv lock`` in the worktree (on the host, where the sweep
runs) so the lock change lands in the same commit. If ``uv`` is not on
``PATH`` or ``uv lock`` fails, it raises :class:`UvLockError`; the sweep driver
records that repo as an ``error`` rather than committing a stale lock. The
``REQUIREMENTS_TXT`` shape carries no lockfile, so there is nothing to refresh.

The edit is **idempotent**: if ``pytest-xdist`` is already declared the applicator
reports no change. For :attr:`~vergil_tooling.lib.dev_deps.DevDepShape.UNKNOWN` —
and for a recognized shape whose concrete target cannot be located — it makes
**no edit** and returns an :class:`~vergil_tooling.lib.fleet_sweep.AppResult`
flagged ``needs_followup=True`` so the driver reports loudly and files a
follow-up, never a silent no-op (repo "no silent failures" policy).
"""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 - fixed `uv lock` argv, never shell
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

_UV_LOCK = "uv.lock"


class UvLockError(RuntimeError):
    """``uv.lock`` could not be refreshed after editing ``pyproject.toml``.

    Raised (never swallowed) so the sweep driver records the repo as an
    ``error`` instead of committing a ``pyproject.toml`` whose lockfile is stale.
    """


def _refresh_uv_lock(worktree: Path) -> bool:
    """Run ``uv lock`` in *worktree* if it is uv-managed (has a ``uv.lock``).

    Returns ``True`` when the lock was refreshed, ``False`` when the repo has no
    ``uv.lock`` (nothing to keep in sync). Raises :class:`UvLockError` when
    ``uv`` is not on ``PATH`` or ``uv lock`` exits non-zero.
    """
    if not (worktree / _UV_LOCK).is_file():
        return False
    uv = shutil.which("uv")
    if uv is None:
        raise UvLockError(
            f"{worktree}: pyproject.toml was edited but `uv` is not on PATH, so "
            f"{_UV_LOCK} cannot be refreshed; install uv and re-run the sweep "
            f"(refusing to commit a stale {_UV_LOCK})"
        )
    result = subprocess.run(  # noqa: S603 - fixed `uv lock` argv, no shell
        [uv, "lock"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise UvLockError(
            f"{worktree}: `uv lock` failed (exit {result.returncode}) after editing "
            f"pyproject.toml; refusing to commit a stale {_UV_LOCK}: {detail}"
        )
    return True


def _write_and_lock(worktree: Path, pyproject: Path, new_text: str, where: str) -> AppResult:
    """Write the edited *pyproject*, refresh ``uv.lock`` if present, report it."""
    pyproject.write_text(new_text, encoding="utf-8")
    if _refresh_uv_lock(worktree):
        return _added(f"{where} (and refreshed {_UV_LOCK})")
    return _added(where)


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
    return _write_and_lock(worktree, pyproject, new_text, "[dependency-groups].dev")


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
    return _write_and_lock(
        worktree, pyproject, new_text, f"[project.optional-dependencies].{target}"
    )


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
    """Refuse a Poetry repo that needs ``pytest-xdist``: no edit, loud follow-up.

    The sweep does not run ``poetry lock``, so editing the dependency table
    would commit a stale ``poetry.lock`` (the uv analogue is #2980). Until
    Poetry is supported the repo is flagged ``needs_followup``. A repo that
    already declares ``pytest-xdist`` needs no change and is reported as such.
    """
    pyproject = worktree / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    poetry = data.get("tool", {}).get("poetry", {})
    header, table = _select_poetry_table(poetry)
    if header is not None and isinstance(table, dict) and _has_package(table):
        return _already(f"[{header}]")
    return _cannot_place(
        "Poetry-managed repos are not supported by the xdist sweep yet (it does not "
        "run `poetry lock`, so an edit would leave poetry.lock stale)"
    )


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
