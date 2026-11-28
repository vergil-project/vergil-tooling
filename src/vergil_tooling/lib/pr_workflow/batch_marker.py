"""The batch marker: a batch's ordered issue list and fixed base, per member.

A batch (epic vergil-project/.github#1019, spec §5.2) records its issues and
its ``base_sha`` in ``build/.vergil-state/issue-implement/<N>/batch`` in the
main clone, for every member ``N``. That is the per-issue directory
``epic-implement`` already scans for stopped runs, so a run that finds a
member's marker resumes the whole batch rather than the one issue.

The file is JSON, ``{"issues": ["#10", "#11"], "base_sha": "<sha>"}``. The
issues are canonical same-repo refs in batch order; ``base_sha`` is the
``origin/develop`` commit when the batch started, the batch's fixed base from
then on. A marker always means a batch of :data:`MIN_BATCH` or more: when an
extraction leaves one member, that member's marker goes too.

Every level from ``.vergil-state`` down to ``<N>`` is checked to be a real,
mode-700 directory before a write, read or removal
(:mod:`vergil_tooling.lib.merge_review.state`). Each marker is written to a
mode-600 temporary file in its directory and renamed over the old one, so no
link at the marker's name is followed and a reader never sees half a file. A
marker that cannot be read or does not parse is an error, never "no marker".

:func:`extract` takes a set of members out of their batch in one rewrite of
the markers that remain. It removes each leaving member's whole
``issue-implement/<N>/`` directory, its ``merge-review/`` state and its
``writer-commits`` ledger included, but refuses while any ``security-*.md``
details file is under one of them: those hold a security finding's details
until the merge review records them, and deleting one would lose them.
:func:`remove_markers` (report-ready's step) leaves the ledger in place, since
it stays until the PR merges. :func:`batch_of` reads the batch a set is in.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.build_dir import BuildDirError
from vergil_tooling.lib.merge_review.state import (
    StateDirError,
    ensure_issue_dir,
    existing_issue_dir,
)
from vergil_tooling.lib.pr_workflow.batch_rules import MAX_BATCH, MIN_BATCH
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The marker's file name inside ``issue-implement/<N>/``.
MARKER_FILE = "batch"

#: A security finding's details file, kept until the merge review records it.
SECURITY_GLOB = "security-*.md"

_REF_RE = re.compile(r"#([1-9][0-9]*)")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_KEYS = frozenset({"issues", "base_sha"})
_FILE_MODE = 0o600


class BatchMarkerError(WorkflowError):
    """A marker cannot be written, read or removed, or the markers disagree."""


@dataclass(frozen=True)
class Marker:
    """One member's marker: the batch's issues in order and its fixed base."""

    issues: list[str]
    base_sha: str


def _number(issue: str) -> int:
    match = _REF_RE.fullmatch(issue)
    if match is None:
        msg = f"{issue!r} is not a canonical issue ref (#N)"
        raise BatchMarkerError(msg)
    return int(match.group(1))


def _check_list(issues: list[str], where: str) -> None:
    """A marker's list: MIN_BATCH to MAX_BATCH canonical refs, none twice."""
    for issue in issues:
        _number(issue)
    if not MIN_BATCH <= len(issues) <= MAX_BATCH:
        msg = (
            f"{where}: a batch marker lists {MIN_BATCH} to {MAX_BATCH} issues,"
            f" not {len(issues)} ({', '.join(issues) or 'none'})"
        )
        raise BatchMarkerError(msg)
    if len(set(issues)) != len(issues):
        msg = f"{where}: a batch marker lists an issue more than once ({', '.join(issues)})"
        raise BatchMarkerError(msg)


def _check_sha(base_sha: str, where: str) -> None:
    if _SHA_RE.fullmatch(base_sha) is None:
        msg = f"{where}: base_sha {base_sha!r} is not a full commit SHA"
        raise BatchMarkerError(msg)


def marker_path(root: Path, issue: str) -> Path:
    """Where *issue*'s marker lives in the main clone *root*."""
    return root / "build" / ".vergil-state" / "issue-implement" / str(_number(issue)) / MARKER_FILE


def _ensure_dir(root: Path, issue: str) -> Path:
    try:
        return ensure_issue_dir(root, _number(issue))
    except (StateDirError, BuildDirError, OSError) as exc:
        msg = f"{issue}'s state directory is not usable: {exc}"
        raise BatchMarkerError(msg) from exc


def _existing_dir(root: Path, issue: str) -> Path | None:
    try:
        return existing_issue_dir(root, _number(issue))
    except (StateDirError, OSError) as exc:
        msg = f"{issue}'s state directory is not usable: {exc}"
        raise BatchMarkerError(msg) from exc


def _write_file(path: Path, text: str) -> None:
    """Write *text* to *path* through a mode-600 temporary file and a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), _FILE_MODE)
            handle.write(text)
        Path(tmp).replace(path)
    except OSError as exc:
        with contextlib.suppress(FileNotFoundError):
            Path(tmp).unlink()
        msg = f"could not write the batch marker {path}: {exc}"
        raise BatchMarkerError(msg) from exc


def write_markers(root: Path, issues: list[str], *, base_sha: str) -> None:
    """Write the marker ``{"issues": issues, "base_sha": base_sha}`` for every member.

    Every member's directory is checked (and created) before any marker is
    written, so a directory that is not private refuses with nothing written.
    """
    _check_list(issues, "write_markers")
    _check_sha(base_sha, "write_markers")
    text = json.dumps({"issues": list(issues), "base_sha": base_sha}, indent=2) + "\n"
    directories = [_ensure_dir(root, issue) for issue in issues]
    for directory in directories:
        _write_file(directory / MARKER_FILE, text)


def _parse(path: Path, issue: str, text: str) -> Marker:
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = f"the batch marker {path} is not valid JSON: {exc}"
        raise BatchMarkerError(msg) from exc
    if not isinstance(data, dict) or set(data) != _KEYS:
        msg = f"the batch marker {path} must hold exactly the keys issues and base_sha"
        raise BatchMarkerError(msg)
    issues, base_sha = data["issues"], data["base_sha"]
    if not isinstance(issues, list) or not all(isinstance(i, str) for i in issues):
        msg = f"the batch marker {path}: issues must be a list of issue refs"
        raise BatchMarkerError(msg)
    if not isinstance(base_sha, str):
        msg = f"the batch marker {path}: base_sha must be a string"
        raise BatchMarkerError(msg)
    _check_list(issues, str(path))
    _check_sha(base_sha, str(path))
    if issue not in issues:
        msg = f"the batch marker {path} is {issue}'s, but does not list {issue}"
        raise BatchMarkerError(msg)
    return Marker(issues=issues, base_sha=base_sha)


def read_marker(root: Path, issue: str) -> Marker | None:
    """*issue*'s marker, or ``None`` when it has none.

    A marker that is not a regular file (a symlink included), cannot be read,
    or does not parse raises :class:`BatchMarkerError`.
    """
    found = _read_marker(root, issue)
    return None if found is None else found[1]


def _read_marker(root: Path, issue: str) -> tuple[Path, Marker] | None:
    """:func:`read_marker`, with the checked directory the marker was read from."""
    directory = _existing_dir(root, issue)
    if directory is None:
        return None
    path = directory / MARKER_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = f"could not read the batch marker {path}: {exc}"
        raise BatchMarkerError(msg) from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        msg = f"the batch marker {path} is not a regular file"
        raise BatchMarkerError(msg)
    with os.fdopen(fd, encoding="utf-8") as handle:
        try:
            text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"could not read the batch marker {path}: {exc}"
            raise BatchMarkerError(msg) from exc
    return directory, _parse(path, issue, text)


def existing_batch(root: Path, issues: list[str]) -> Marker | None:
    """The batch *issues* already form, or ``None`` when no member has a marker.

    When any member has a marker, every member must hold one listing exactly
    *issues*, in order, with one shared ``base_sha`` (a resumed start);
    otherwise :class:`BatchMarkerError` names the first member that differs.
    """
    markers = {issue: read_marker(root, issue) for issue in issues}
    if all(marker is None for marker in markers.values()):
        return None
    found = next(marker for marker in markers.values() if marker is not None)
    for issue, marker in markers.items():
        if marker is None:
            msg = (
                f"{issue} has no batch marker, but another member of"
                f" {', '.join(issues)} has one; the markers do not form this batch"
            )
            raise BatchMarkerError(msg)
        if marker.issues != issues:
            msg = (
                f"{issue} is already in the batch {', '.join(marker.issues)};"
                f" it cannot start the batch {', '.join(issues)}"
            )
            raise BatchMarkerError(msg)
        if marker.base_sha != found.base_sha:
            msg = (
                f"{issue}'s batch marker has base_sha {marker.base_sha}, but another"
                f" member's has {found.base_sha}; the markers disagree"
            )
            raise BatchMarkerError(msg)
    return found


def _security_files(directory: Path) -> list[Path]:
    """Every security details file under *directory*, symlinks not followed.

    A directory that cannot be listed refuses: a details file in it could not
    be ruled out.
    """

    def refuse(exc: OSError) -> None:
        msg = f"cannot look for security details under {directory}: {exc}; nothing was changed"
        raise BatchMarkerError(msg) from exc

    found: list[Path] = []
    for current, dirs, files in os.walk(directory, onerror=refuse, followlinks=False):
        dirs.sort()
        here = Path(current)
        found.extend(here / name for name in sorted(files) if Path(name).match(SECURITY_GLOB))
        found.extend(here / name for name in sorted(dirs) if Path(name).match(SECURITY_GLOB))
    return sorted(found)


def _remove_marker(root: Path, issue: str) -> None:
    """Remove *issue*'s marker, then its directory if that leaves it empty."""
    directory = _existing_dir(root, issue)
    if directory is None:
        return
    try:
        (directory / MARKER_FILE).unlink(missing_ok=True)
        if not any(directory.iterdir()):
            directory.rmdir()
    except OSError as exc:
        msg = f"could not remove {issue}'s batch marker {directory / MARKER_FILE}: {exc}"
        raise BatchMarkerError(msg) from exc


def remove_markers(root: Path, issues: list[str]) -> None:
    """Remove each member's marker (and its directory, when that leaves it empty)."""
    for issue in issues:
        _number(issue)
    for issue in issues:
        _remove_marker(root, issue)


def _canonical_set(issues: Sequence[str]) -> list[str]:
    """A set of issues: at least one canonical ref, none listed twice."""
    if not issues:
        msg = "no issues were given: a batch stop takes at least one issue out of its batch"
        raise BatchMarkerError(msg)
    for issue in issues:
        _number(issue)
    if len(set(issues)) != len(issues):
        msg = f"the set lists an issue more than once ({', '.join(issues)})"
        raise BatchMarkerError(msg)
    return list(issues)


def _found_markers(root: Path, issues: list[str]) -> dict[str, tuple[Path, Marker]]:
    """Each issue's checked directory and marker; every issue must hold a marker."""
    found: dict[str, tuple[Path, Marker]] = {}
    for issue in issues:
        entry = _read_marker(root, issue)
        if entry is None:
            msg = f"{issue} is not in a batch (it has no batch marker)"
            raise BatchMarkerError(msg)
        found[issue] = entry
    return found


def _one_batch(found: dict[str, tuple[Path, Marker]]) -> Marker:
    """The batch every found marker lists, with one base; refuses when they disagree."""
    first_issue, (_, first) = next(iter(found.items()))
    for issue, (_, marker) in found.items():
        if marker.issues != first.issues:
            msg = (
                f"{issue}'s batch marker lists {', '.join(marker.issues)}, but"
                f" {first_issue}'s lists {', '.join(first.issues)};"
                " the markers disagree"
            )
            raise BatchMarkerError(msg)
        if marker.base_sha != first.base_sha:
            msg = (
                f"{issue}'s batch marker has base_sha {marker.base_sha}, but"
                f" {first_issue}'s has {first.base_sha}; the markers disagree"
            )
            raise BatchMarkerError(msg)
    return first


def batch_of(root: Path, issues: Sequence[str]) -> Marker:
    """The batch that *issues* are all in, read from their markers; nothing is written.

    Every issue must hold a marker, and every marker must list the same batch
    with the same ``base_sha``; otherwise :class:`BatchMarkerError` names the
    difference.
    """
    return _one_batch(_found_markers(root, _canonical_set(issues)))


def extract(root: Path, issues: Sequence[str]) -> list[str]:
    """Take *issues* out of their batch; return the remaining members, in order.

    *issues* must all be in one batch (:func:`batch_of`). Refuses, naming each
    file, while any ``security-*.md`` details file is under a leaving issue's
    directory. Otherwise it rewrites every remaining member's marker once,
    without the set (removing the last one's when fewer than :data:`MIN_BATCH`
    remain), then removes each leaving issue's whole ``issue-implement/<N>/``
    directory, ``writer-commits`` ledger included. Every remaining member's
    marker is checked against the batch first, so nothing changes when they
    disagree; a member already rewritten by an interrupted extraction is
    accepted.
    """
    leaving = _canonical_set(issues)
    found = _found_markers(root, leaving)
    batch = _one_batch(found)

    security: list[Path] = []
    for issue in leaving:
        security.extend(_security_files(found[issue][0]))
    if security:
        listed = "\n".join(f"  {path}" for path in security)
        msg = (
            f"{', '.join(leaving)} cannot be extracted while security details await a record:\n"
            f"{listed}\n"
            "Record each one first with the merge review's record step"
            " (vrg-security-record create ... --details-file <file>), then delete"
            " the file and re-run batch-extract. Nothing was changed."
        )
        raise BatchMarkerError(msg)

    remaining = [member for member in batch.issues if member not in leaving]
    for member in remaining:
        other = read_marker(root, member)
        if other is None and len(remaining) < MIN_BATCH:
            continue  # An interrupted extraction already removed it.
        if other is None or other.issues not in (batch.issues, remaining):
            held = "none" if other is None else ", ".join(other.issues)
            msg = (
                f"{member}'s batch marker lists {held}, but the batch"
                f" {', '.join(batch.issues)} lists its members; the markers disagree,"
                " so nothing was changed"
            )
            raise BatchMarkerError(msg)
        if other.base_sha != batch.base_sha:
            msg = (
                f"{member}'s batch marker has base_sha {other.base_sha}, but the batch's"
                f" is {batch.base_sha}; the markers disagree, so nothing was changed"
            )
            raise BatchMarkerError(msg)

    if len(remaining) >= MIN_BATCH:
        write_markers(root, remaining, base_sha=batch.base_sha)
    else:
        remove_markers(root, remaining)
    for issue in leaving:
        directory = found[issue][0]
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            msg = (
                f"could not remove {issue}'s state directory {directory}: {exc};"
                " the other members no longer list it. Re-run batch-extract to finish."
            )
            raise BatchMarkerError(msg) from exc
    return remaining
