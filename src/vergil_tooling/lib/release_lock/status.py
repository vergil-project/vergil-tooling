"""The release lock's state as ``vrg-release-lock status`` and ``vrg-repo-status`` show it.

Spec §5.6 of epic vergil-project/.github#1664. :func:`gather` reads the lock
(:func:`~.refs.read_lock`, no trusted-author check), judges its staleness
(:func:`~.stale.judge`) and, while it is held, lists the PRs it holds
unarmed: the open, non-draft PRs into ``develop`` with auto-merge off that
the one PR path accepts (:func:`pr_path.accepts`). :meth:`LockStatus.as_json`
is the machine-readable form (``--json``), and :func:`text_lines` the text
form both commands print.

Any read that fails raises a :class:`~.model.ReleaseLockError`: a
:class:`~.model.RefReadError` for the lock, an :class:`~.stale.IssueReadError`
for a tracking issue, and a :class:`StatusError` for the PRs. A failure is
never shown as "no lock" or "no PRs held".
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, pr_path
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import stale
from vergil_tooling.lib.release_lock.model import LockRecord, ReleaseLockError
from vergil_tooling.lib.release_lock.refs import read_lock

if TYPE_CHECKING:
    from vergil_tooling.lib.release_lock.refs import LockState
    from vergil_tooling.lib.release_lock.stale import Staleness

_DEVELOP = "develop"
#: The open develop PRs read in one listing; a listing this long may be cut short.
_PR_LIST_LIMIT = 200
_RECORD_FIELDS = tuple(f.name for f in fields(LockRecord))

# Every way the PR listing can fail: a gh failure, a response that does not
# parse, no App installation or token, and gh not runnable.
_LIST_ERRORS = (
    subprocess.CalledProcessError,
    ValueError,
    OSError,
    RuntimeError,
    github.AppAuthError,
    github.NoInstallationError,
)


class StatusError(ReleaseLockError):
    """The PRs a lock holds unarmed could not be listed completely."""


@dataclass(frozen=True)
class HeldPR:
    """A PR the lock holds unarmed."""

    number: int
    url: str


@dataclass(frozen=True)
class LockStatus:
    """The lock of *repo* as read, its staleness, and the PRs it holds unarmed."""

    repo: str
    state: LockState
    staleness: Staleness
    heartbeat_age_secs: int | None  # None when there is no readable record
    held_prs: tuple[HeldPR, ...]

    def as_json(self) -> dict[str, object]:
        """The JSON form: every key always present, a record field ``null`` with no record."""
        rec = self.state.record
        data: dict[str, object] = {
            "repo": self.repo,
            "held": self.state.held,
            "sha": self.state.sha,
            "error": self.state.error,
        }
        for name in _RECORD_FIELDS:
            data[name] = getattr(rec, name) if rec is not None else None
        data.update(
            {
                "stale": self.staleness.stale,
                "live": self.staleness.live,
                "reason": self.staleness.reason,
                "judged_tracking_issue": self.staleness.tracking_issue,
                "heartbeat_age_secs": self.heartbeat_age_secs,
                "held_prs": [{"number": p.number, "url": p.url} for p in self.held_prs],
            }
        )
        return data


def origin_repo(remote: str = "origin") -> str | None:
    """``<owner>/<repo>`` of *remote*'s GitHub repository, ``None`` when it names none."""
    return github.github_remote_repo(git.remote_url(remote))


def _develop_prs(repo: str) -> list[dict[str, object]]:
    """The open PRs into ``develop`` of *repo*, with their draft and auto-merge state."""
    what = f"the open PRs into {_DEVELOP} of {repo}"
    try:
        data = github.read_json(
            "pr",
            "list",
            "--repo",
            repo,
            "--base",
            _DEVELOP,
            "--state",
            "open",
            "--limit",
            str(_PR_LIST_LIMIT),
            "--json",
            "number,url,isDraft,autoMergeRequest",
        )
    except _LIST_ERRORS as exc:
        msg = f"could not list {what}: {error_text(exc)}"
        raise StatusError(msg) from exc
    if not isinstance(data, list):
        msg = f"listing {what} gave {type(data).__name__}, not a list"
        raise StatusError(msg)
    if len(data) >= _PR_LIST_LIMIT:
        msg = f"listing {what} filled the limit of {_PR_LIST_LIMIT}; it may be cut short"
        raise StatusError(msg)
    prs: list[dict[str, object]] = []
    for item in data:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("number"), int)
            or not isinstance(item.get("url"), str)
            or not isinstance(item.get("isDraft"), bool)
            or "autoMergeRequest" not in item
        ):
            msg = f"listing {what} gave a malformed PR: {item!r}"
            raise StatusError(msg)
        prs.append(item)
    return prs


def held_prs(repo: str) -> tuple[HeldPR, ...]:
    """The open, non-draft ``develop`` PRs of *repo* with auto-merge off that the path accepts."""
    held: list[HeldPR] = []
    for item in _develop_prs(repo):
        if item["isDraft"] or item["autoMergeRequest"] is not None:
            continue
        number, url = int(str(item["number"])), str(item["url"])
        try:
            refusal = pr_path.accepts(url)
        except pr_path.PRPathError as exc:
            msg = f"could not tell whether the path accepts #{number}: {exc}"
            raise StatusError(msg) from exc
        if refusal is None:
            held.append(HeldPR(number=number, url=url))
    return tuple(held)


def gather(repo: str, *, now: datetime | None = None) -> LockStatus:
    """Read *repo*'s lock, judge it and list the PRs it holds; any failed read raises."""
    when = now if now is not None else datetime.now(UTC)
    state = read_lock(repo)
    staleness = stale.judge(repo, state, now=when)
    rec = state.record
    return LockStatus(
        repo=repo,
        state=state,
        staleness=staleness,
        heartbeat_age_secs=stale.heartbeat_age_secs(rec, now=when) if rec is not None else None,
        held_prs=held_prs(repo) if state.held else (),
    )


def _tracking_line(rec: LockRecord, judged: str) -> str:
    if rec.tracking_issue:
        return f"tracking issue: {rec.tracking_issue}"
    if judged:
        return f"tracking issue: {judged} (open release issue of {rec.version}; not in the lock)"
    return "tracking issue: none"


def text_lines(status: LockStatus) -> list[str]:
    """The text form: ``no release lock held``, or one line per field."""
    state, judged = status.state, status.staleness
    if not state.held:
        return ["no release lock held"]
    rec = state.record
    lines: list[str] = []
    if rec is None:
        lines.append(f"release lock: held, unreadable: {state.error}")
    else:
        health = f"broken (at {rec.broken_stage})" if rec.health == "broken" else rec.health
        age = (
            f" ({status.heartbeat_age_secs}s ago)" if status.heartbeat_age_secs is not None else ""
        )
        lines += [
            "release lock: held",
            f"holder: {rec.holder_identity} on {rec.holder_host}",
            f"version: {rec.version}",
            _tracking_line(rec, judged.tracking_issue),
            f"stage: {rec.stage}",
            f"health: {health}",
            f"started: {rec.started_at}",
            f"heartbeat: {rec.heartbeat_at}{age}",
            f"worktree: {rec.worktree or 'none'}",
        ]
    lines.append(f"stale: {'yes' if judged.stale else 'no'} ({judged.reason})")
    held = ", ".join(f"#{p.number}" for p in status.held_prs)
    lines.append(f"held PRs: {held or 'none'}")
    return lines
