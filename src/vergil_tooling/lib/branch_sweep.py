"""Find, and optionally delete, finished branches left on a GitHub remote.

A branch is *finished* when its pull-request work is done but the branch was
never removed: GitHub's delete-on-merge setting occasionally skips its
after-merge processing, and a PR closed without merging is never auto-deleted
(issue #3333). This module is the safety net behind both the delete-on-merge
repo setting and the delete-on-close rule (issue #3332).

The rules, applied per remote branch by :func:`decide`:

* **Never deleted:** the repo's default branch, ``develop``, ``main`` and
  ``gh-pages``; any branch protected by classic branch protection or a
  ruleset; any branch with **no PR** (an agent pushes its branch before
  ``report-ready``, and the branch has no PR until the human submits it).
* **Kept:** a branch with an open PR.
* **Finished:** none of the branch's PRs is open and its newest PR is merged
  or closed. It is deleted only if the branch's current tip equals that PR's
  ``headRefOid``. A different tip means the branch name was reused after the
  PR finished (issue #1719), so the branch is kept.

Every call goes through the GitHub API (``gh``), so the target repo need not
be the cwd checkout. Failures are never swallowed: :func:`sweep` records a
per-branch error, keeps processing the remaining branches, and the caller
exits non-zero.
"""

from __future__ import annotations

import enum
import subprocess
from collections import Counter
from dataclasses import dataclass
from typing import cast
from urllib.parse import quote

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text

#: Branch names that are never deleted, whatever their PR history.
RESERVED_BRANCHES = frozenset({"develop", "main", "gh-pages"})

#: Upper bound on PRs fetched per branch. Reaching it is an error, not a
#: silent truncation — an unseen older PR could still be open.
PR_LIST_LIMIT = 1000

_OPEN = "OPEN"
_FINISHED_STATES = frozenset({"MERGED", "CLOSED"})


class Action(enum.Enum):
    """What the sweep did, or would do, with one branch."""

    KEPT = "kept"
    WOULD_DELETE = "would delete"
    DELETED = "deleted"
    ERROR = "error"


@dataclass(frozen=True)
class RemoteBranch:
    """A branch on the remote, with its tip SHA and protection flag."""

    name: str
    tip: str
    protected: bool


@dataclass(frozen=True)
class PullRequest:
    """The fields of a PR the sweep decides on."""

    number: int
    state: str
    head_oid: str


@dataclass(frozen=True)
class Decision:
    """The verdict of :func:`decide`: delete the branch or keep it, and why."""

    delete: bool
    reason: str
    pr: PullRequest | None


@dataclass(frozen=True)
class Outcome:
    """One branch's line in the sweep report."""

    branch: str
    pr: PullRequest | None
    action: Action
    reason: str


def newest_pr(prs: list[PullRequest]) -> PullRequest | None:
    """The most recently created PR (highest number), or ``None`` if none."""
    if not prs:
        return None
    return max(prs, key=lambda pr: pr.number)


def decide(branch: RemoteBranch, prs: list[PullRequest], *, default_branch: str) -> Decision:
    """Apply the keep/delete rules to *branch* given all of its *prs*.

    Pure: no API calls. Ruleset protection that the branches API does not
    flag is checked separately by :func:`sweep`, only for branches this
    function would delete.
    """
    newest = newest_pr(prs)
    if branch.name == default_branch:
        return Decision(delete=False, reason="default branch", pr=newest)
    if branch.name in RESERVED_BRANCHES:
        return Decision(delete=False, reason="reserved branch", pr=newest)
    if branch.protected:
        return Decision(delete=False, reason="protected branch", pr=newest)
    if newest is None:
        return Decision(delete=False, reason="no PR", pr=None)
    open_prs = sorted(pr.number for pr in prs if pr.state == _OPEN)
    if open_prs:
        numbers = ", ".join(f"#{n}" for n in open_prs)
        return Decision(delete=False, reason=f"open PR {numbers}", pr=newest)
    if newest.state not in _FINISHED_STATES:
        msg = f"PR #{newest.number} has unexpected state {newest.state!r}"
        raise ValueError(msg)
    if branch.tip != newest.head_oid:
        return Decision(
            delete=False,
            reason=(
                f"reused branch name: tip {branch.tip[:12]} is not "
                f"PR #{newest.number}'s head {newest.head_oid[:12]}"
            ),
            pr=newest,
        )
    return Decision(delete=True, reason=f"PR #{newest.number} {newest.state.lower()}", pr=newest)


def _ref_path(branch: str) -> str:
    """*branch* escaped for a REST path, keeping ``/`` separators."""
    return quote(branch, safe="/")


def _as_dict(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"unexpected API response for {what}: {value!r}"
        raise ValueError(msg)
    return cast("dict[str, object]", value)


def _as_str(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"unexpected API response for {what}: {value!r}"
        raise ValueError(msg)
    return value


def default_branch(repo: str) -> str:
    """The default branch of *repo* (``OWNER/NAME``)."""
    data = _as_dict(github.read_json("api", f"repos/{repo}"), f"repos/{repo}")
    return _as_str(data.get("default_branch"), f"{repo} default_branch")


def list_branches(repo: str) -> list[RemoteBranch]:
    """Every branch of *repo* with its tip SHA and ``protected`` flag."""
    branches: list[RemoteBranch] = []
    for item in github.read_json_paginated(f"repos/{repo}/branches"):
        entry = _as_dict(item, f"{repo} branch")
        name = _as_str(entry.get("name"), f"{repo} branch name")
        commit = _as_dict(entry.get("commit"), f"{repo} branch {name} commit")
        tip = _as_str(commit.get("sha"), f"{repo} branch {name} tip")
        protected = entry.get("protected")
        if not isinstance(protected, bool):
            msg = f"unexpected API response for {repo} branch {name} protected: {protected!r}"
            raise ValueError(msg)
        branches.append(RemoteBranch(name=name, tip=tip, protected=protected))
    return branches


def list_prs(repo: str, branch: str) -> list[PullRequest]:
    """Every same-repo PR whose head is *branch*, in any state.

    PRs from forks can share a head branch name but say nothing about this
    repo's branch, so they are dropped.
    """
    raw = github.read_json(
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        branch,
        "--state",
        "all",
        "--limit",
        str(PR_LIST_LIMIT),
        "--json",
        "number,state,headRefOid,isCrossRepository",
    )
    if not isinstance(raw, list):
        msg = f"unexpected API response for PRs of {branch}: {raw!r}"
        raise ValueError(msg)
    if len(raw) >= PR_LIST_LIMIT:
        msg = f"{branch} has {PR_LIST_LIMIT}+ PRs; refusing to decide on a truncated list"
        raise ValueError(msg)
    prs: list[PullRequest] = []
    for item in raw:
        entry = _as_dict(item, f"PR of {branch}")
        if entry.get("isCrossRepository") is True:
            continue
        number = entry.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            msg = f"unexpected API response for PR number of {branch}: {number!r}"
            raise ValueError(msg)
        prs.append(
            PullRequest(
                number=number,
                state=_as_str(entry.get("state"), f"PR #{number} state"),
                head_oid=_as_str(entry.get("headRefOid"), f"PR #{number} headRefOid"),
            )
        )
    return prs


def ruleset_rule_types(repo: str, branch: str) -> list[str]:
    """The types of every active ruleset rule that applies to *branch*.

    The branches API ``protected`` flag is the primary signal; this endpoint
    is consulted as well so a branch targeted by any ruleset is never deleted
    even if the flag does not reflect it.
    """
    rules = github.read_json_paginated(f"repos/{repo}/rules/branches/{_ref_path(branch)}")
    return [
        _as_str(_as_dict(rule, f"rule on {branch}").get("type"), f"rule type on {branch}")
        for rule in rules
    ]


def current_tip(repo: str, branch: str) -> str:
    """The SHA *branch* points at right now."""
    endpoint = f"repos/{repo}/git/ref/heads/{_ref_path(branch)}"
    data = _as_dict(github.read_json("api", endpoint), endpoint)
    obj = _as_dict(data.get("object"), f"{endpoint} object")
    return _as_str(obj.get("sha"), f"{endpoint} sha")


def delete_branch(repo: str, branch: str) -> None:
    """Delete *branch* from *repo* via the git refs API."""
    github.delete(f"repos/{repo}/git/refs/heads/{_ref_path(branch)}")


def _sweep_one(repo: str, branch: RemoteBranch, *, default: str, apply: bool) -> Outcome:
    exempt = branch.name == default or branch.name in RESERVED_BRANCHES or branch.protected
    prs = [] if exempt else list_prs(repo, branch.name)
    decision = decide(branch, prs, default_branch=default)
    if not decision.delete:
        return Outcome(branch.name, decision.pr, Action.KEPT, decision.reason)
    rules = ruleset_rule_types(repo, branch.name)
    if rules:
        reason = f"protected by ruleset ({', '.join(sorted(set(rules)))})"
        return Outcome(branch.name, decision.pr, Action.KEPT, reason)
    if not apply:
        return Outcome(branch.name, decision.pr, Action.WOULD_DELETE, decision.reason)
    # Re-read the tip right before deleting: a push since the listing means
    # new work on the branch, which must survive.
    tip = current_tip(repo, branch.name)
    if tip != branch.tip:
        reason = f"tip moved during sweep ({branch.tip[:12]} -> {tip[:12]})"
        return Outcome(branch.name, decision.pr, Action.KEPT, reason)
    delete_branch(repo, branch.name)
    return Outcome(branch.name, decision.pr, Action.DELETED, decision.reason)


def sweep(repo: str, *, apply: bool) -> list[Outcome]:
    """Evaluate every branch of *repo*; delete the finished ones when *apply*.

    Listing failures (default branch, branch list) propagate — there is
    nothing to sweep without them. A failure on one branch is recorded as an
    :attr:`Action.ERROR` outcome and the sweep moves on; the caller turns any
    error outcome into a non-zero exit. Authentication errors are not
    per-branch problems and propagate immediately.
    """
    default = default_branch(repo)
    outcomes: list[Outcome] = []
    for branch in sorted(list_branches(repo), key=lambda b: b.name):
        try:
            outcomes.append(_sweep_one(repo, branch, default=default, apply=apply))
        except github.MissingGitHubTokenError:
            raise
        except (subprocess.CalledProcessError, ValueError) as exc:
            detail = " ".join(error_text(exc).split())
            outcomes.append(Outcome(branch.name, None, Action.ERROR, detail))
    return outcomes


def format_outcomes(outcomes: list[Outcome]) -> list[str]:
    """One aligned line per outcome: branch, PR, PR state, action (reason)."""
    width = max((len(o.branch) for o in outcomes), default=0)
    lines: list[str] = []
    for o in outcomes:
        pr = f"#{o.pr.number}" if o.pr else "-"
        state = o.pr.state if o.pr else "-"
        lines.append(f"{o.branch:<{width}}  {pr:<7} {state:<7} {o.action.value} ({o.reason})")
    return lines


def summarize(repo: str, outcomes: list[Outcome], *, apply: bool) -> str:
    """The closing summary line of a sweep report."""
    counts = Counter(o.action for o in outcomes)
    removed = (
        f"{counts[Action.DELETED]} deleted"
        if apply
        else f"{counts[Action.WOULD_DELETE]} would delete"
    )
    mode = "" if apply else " (dry run; pass --apply to delete)"
    return (
        f"{repo}: {len(outcomes)} branches, {removed}, {counts[Action.KEPT]} kept, "
        f"{counts[Action.ERROR]} errors{mode}"
    )
