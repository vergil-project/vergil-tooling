"""The local-settings guard: ``.claude/settings.local.json`` stays absent or ``{}``.

Claude Code loads ``.claude/settings.local.json`` above the shared repo
settings, so any key in it silently overrides the managed template for that
checkout — a permission allow, a disabled hook, an environment variable. The
ownership model (epic vergil-project/.github#558) gives that file nothing: a
copy Claude Code may load must be absent or parse to exactly ``{}``.

The agent chokepoints — ``vrg-commit``, ``vrg-pr-workflow report-ready`` and
``vrg-container-run`` (host-side, before any container starts) — call
:func:`enforce`. An agent identity is refused; a human gets a warning and the
tool proceeds. They are deliberately not Claude Code hooks: a local file that
disables hooks would disable the check too. ``vrg-validate`` is not a caller —
inside the container it sees neither the main checkout's working tree nor the
identity signals, and ``vrg-container-run`` has already checked every copy.

Known limit: when the repo root, its ``.git`` or ``.claude`` isn't owned by the
user, Claude Code keeps the local file elsewhere. That location is not
checked; :func:`local_settings_paths` reports it as a note.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import github, identity_mode

# The epic's explicit org scope (epic vergil-project/.github#558, Global
# Constraints). Repos in any other org — including the on-hold orgs — are
# ignored, so the guard is silent there.
IN_SCOPE_ORGS = ("vergil-project", "logical-minds-foundry", "mnemosys-project")

_RULE = (
    "{path} must be absent or exactly {{}} — move any rule it holds into the repo "
    "template or your user settings, then delete it (epic vergil-project/.github#558)."
)
REFUSAL_TEMPLATE = "refusing: " + _RULE
WARNING_TEMPLATE = "warning: " + _RULE
AGENT_LINE = "Agents: stop and ask the human."

_LOCAL_FILE = Path(".claude") / "settings.local.json"


class LocalSettingsError(RuntimeError):
    """The candidate paths could not be resolved inside an in-scope repo."""


def _current_uid() -> int:
    return os.getuid()


def _rev_parse(cwd: Path) -> tuple[Path, Path]:
    """Return ``(worktree root, git common dir)`` for *cwd*, both absolute.

    Raises :class:`LocalSettingsError` carrying git's own error; a failure here
    is never a pass.
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(cwd),
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:  # noqa: PLR2004
        detail = result.stderr.strip() or f"unexpected output {result.stdout!r}"
        msg = f"git rev-parse failed in {cwd}: {detail}"
        raise LocalSettingsError(msg)
    return Path(lines[0]).resolve(), Path(lines[1]).resolve()


def _ownership_note(root: Path) -> str | None:
    """A "not checked" note when *root*, its ``.git`` or ``.claude`` isn't the user's."""
    uid = _current_uid()
    for path in (root, root / ".git", root / ".claude"):
        try:
            owner = path.stat().st_uid
        except FileNotFoundError:
            continue
        if owner != uid:
            return (
                f"{path} is not owned by you, so Claude Code keeps {_LOCAL_FILE} for "
                f"{root} elsewhere; that copy is not checked (epic vergil-project/.github#558)."
            )
    return None


def local_settings_paths(cwd: Path) -> tuple[list[Path], list[str]]:
    """Return every ``settings.local.json`` Claude Code may load from *cwd*, plus notes.

    The candidates are the invoking directory's copy and each ancestor's up to
    the worktree root (a session started in a subdirectory, or after ``/cd``),
    and the main checkout root's copy, found through ``git rev-parse
    --git-common-dir``. Paths are absolute and de-duplicated. The notes name
    locations that are not checked (see the module's known limit).
    """
    cwd = cwd.resolve()
    top, common_dir = _rev_parse(cwd)
    if not cwd.is_relative_to(top):
        msg = f"git rev-parse failed in {cwd}: worktree root {top} does not contain it"
        raise LocalSettingsError(msg)
    main_root = common_dir.parent
    dirs = [cwd, *[p for p in cwd.parents if p.is_relative_to(top)], main_root]
    paths: list[Path] = []
    for directory in dirs:
        path = directory / _LOCAL_FILE
        if path not in paths:
            paths.append(path)
    notes = [note for root in dict.fromkeys((top, main_root)) if (note := _ownership_note(root))]
    return paths, notes


def find_violations(paths: list[Path]) -> list[Path]:
    """Return the *paths* that exist and do not parse to exactly ``{}``.

    Unparseable or unreadable content violates: the guard fails closed.
    """
    violations: list[Path] = []
    for path in paths:
        if not os.path.lexists(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            violations.append(path)
            continue
        if data != {}:
            violations.append(path)
    return violations


def in_scope(cwd: Path) -> bool:
    """True when *cwd*'s repo has an ``origin`` owned by one of :data:`IN_SCOPE_ORGS`.

    Not a git repo, no ``origin``, or another owner is out of scope.
    """
    return github.detect_org(cwd=cwd) in IN_SCOPE_ORGS


def enforce(cwd: Path) -> int:
    """The chokepoint check; returns the exit status the caller stops on when non-zero.

    Out of scope: 0, silently. In scope, with a violating copy: an agent (any
    agent signal, see :func:`identity_mode.any_agent_signal`) is refused (1);
    anyone else is warned and proceeds (0). A path-resolution failure in an
    in-scope repo is an error (1), never a pass.
    """
    if not in_scope(cwd):
        return 0
    try:
        paths, notes = local_settings_paths(cwd)
    except LocalSettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    violations = find_violations(paths)
    if not violations:
        return 0
    agent = identity_mode.any_agent_signal()
    template = REFUSAL_TEMPLATE if agent else WARNING_TEMPLATE
    for path in violations:
        print(template.format(path=path), file=sys.stderr)
    if agent:
        print(AGENT_LINE, file=sys.stderr)
        return 1
    return 0
