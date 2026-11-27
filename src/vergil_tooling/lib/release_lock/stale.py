"""Whether a held release lock is stale (spec §5.6 of epic vergil-project/.github#1664).

:func:`judge` applies the staleness rule, in order:

1. An absent lock is not stale.
2. A lock that is held but cannot be read or parsed is not stale (reason
   ``unreadable``): only ``vrg-release-lock break`` removes it.
3. A ``running`` lock whose heartbeat is younger than
   :data:`~.model.STALE_AFTER_SECS` is **live**: its holder is alive, so it is
   never stale, whatever state its tracking issue is in. No issue is read.
4. A lock that names a tracking issue is stale exactly when that issue is
   closed. An open one keeps it the human's (a broken release, or a laptop
   asleep mid-release), however old its heartbeat.
5. A lock that names none is checked for an open release tracking issue of
   its version with a trusted body, by the rule a fresh ``vrg-release``
   start uses (``resume._trusted_release_issues``). One found is the
   lock's issue, and the lock is not stale: the release died after
   ``prepare``. Failing that, the lock is stale once its heartbeat is older
   than the threshold.

A failed issue read raises :class:`IssueReadError`; it never means stale.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.resume import _open_release_issues, _trusted_release_issues
from vergil_tooling.lib.release_lock.model import (
    STALE_AFTER_SECS,
    ReleaseLockError,
    heartbeat_age_secs,
)

if TYPE_CHECKING:
    from datetime import datetime

    from vergil_tooling.lib.release_lock.refs import LockState

_ISSUE_REF = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>[1-9][0-9]*)")
_OPEN = "OPEN"
_CLOSED = "CLOSED"

# Every way an issue read can fail: a gh failure (GitHubAPIError and
# MissingGitHubTokenError are CalledProcessErrors), a refused or malformed
# listing or an untrusted-author read failure (ReleaseError), a response that
# does not parse (ValueError), no App installation or token (AppAuthError,
# NoInstallationError, RuntimeError) and gh not runnable (OSError).
_READ_ERRORS = (
    subprocess.CalledProcessError,
    ReleaseError,
    ValueError,
    OSError,
    RuntimeError,
    github.AppAuthError,
    github.NoInstallationError,
)


class IssueReadError(ReleaseLockError):
    """A tracking issue's state could not be read, so staleness cannot be judged."""


@dataclass(frozen=True)
class Staleness:
    """The staleness verdict on a lock.

    *live* is true for a ``running`` lock with a fresh heartbeat, which is
    never stale. *tracking_issue* is the issue the verdict rests on: the one
    the lock names, or the open release issue of its version found for a lock
    that names none; ``""`` when there is none.
    """

    stale: bool
    live: bool
    reason: str
    tracking_issue: str


def _require_aware(now: datetime) -> None:
    if now.utcoffset() is None:
        msg = "now must be a timezone-aware datetime"
        raise ValueError(msg)


def _issue_state(ref: str) -> str:
    """``OPEN`` or ``CLOSED`` for issue *ref*; :class:`IssueReadError` on any failure."""
    match = _ISSUE_REF.fullmatch(ref)
    if match is None:
        msg = f"the lock's tracking issue is not <owner>/<repo>#<n>: {ref!r}"
        raise IssueReadError(msg)
    repo = match.group("repo")
    try:
        with github.target_org(repo.split("/", 1)[0]):
            data = github.read_json(
                "issue", "view", match.group("number"), "--repo", repo, "--json", "state"
            )
    except _READ_ERRORS as exc:
        msg = f"could not read the state of the lock's tracking issue {ref}: {error_text(exc)}"
        raise IssueReadError(msg) from exc
    state = data.get("state") if isinstance(data, dict) else None
    if state not in (_OPEN, _CLOSED):
        msg = f"reading the lock's tracking issue {ref} gave no OPEN or CLOSED state: {data!r}"
        raise IssueReadError(msg)
    return str(state)


def _open_issue_of_version(repo: str, version: str) -> str:
    """The open, trusted ``release: <version>`` issue of *repo* as a ref, ``""`` if none."""
    try:
        issues = _trusted_release_issues(_open_release_issues(repo, phase="release-lock"), repo)
    except _READ_ERRORS as exc:
        detail = f" ({exc.detail})" if isinstance(exc, ReleaseError) and exc.detail else ""
        msg = (
            f"could not read the open release tracking issues of {repo} for version "
            f"{version}: {error_text(exc)}{detail}"
        )
        raise IssueReadError(msg) from exc
    numbers = sorted(number for ver, number, _body in issues if ver == version)
    return f"{repo}#{numbers[0]}" if numbers else ""


def judge(repo: str, state: LockState, *, now: datetime) -> Staleness:
    """The staleness of *repo*'s lock *state* at *now* (see the module docstring)."""
    _require_aware(now)
    if not state.held:
        return Staleness(stale=False, live=False, reason="no release lock held", tracking_issue="")
    rec = state.record
    if rec is None:
        return Staleness(stale=False, live=False, reason="unreadable", tracking_issue="")
    age = heartbeat_age_secs(rec, now=now)
    fresh = age < STALE_AFTER_SECS
    if rec.health == "running" and fresh:
        return Staleness(
            stale=False,
            live=True,
            reason="running with a fresh heartbeat",
            tracking_issue=rec.tracking_issue,
        )
    issue = rec.tracking_issue
    if issue:
        closed = _issue_state(issue) == _CLOSED
        verdict = "closed" if closed else "open"
        return Staleness(
            stale=closed,
            live=False,
            reason=f"its tracking issue {issue} is {verdict}",
            tracking_issue=issue,
        )
    found = _open_issue_of_version(repo, rec.version)
    if found:
        return Staleness(
            stale=False,
            live=False,
            reason=f"it names no tracking issue, but release {rec.version}'s issue {found} is open",
            tracking_issue=found,
        )
    if fresh:
        reason = f"it names no tracking issue and its heartbeat is {age}s old"
    else:
        reason = (
            f"it names no tracking issue, none is open for release {rec.version}, "
            f"and its heartbeat is {age}s old (stale after {STALE_AFTER_SECS}s)"
        )
    return Staleness(stale=not fresh, live=False, reason=reason, tracking_issue="")
