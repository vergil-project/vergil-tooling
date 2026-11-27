"""Provenance verification for auto-merging PRs.

``vrg-submit-pr`` runs it on each PR before it turns auto-merge on, the one
PR path (``lib/pr_path.py``) on every head pushed to an open PR before it
is pushed, and ``vrg-finalize-pr``'s watch phase (``lib/finalize_watch.py``)
on each PR that merged, to report a violation. None of them merges a PR
itself: GitHub merges it through auto-merge.

Fetches a PR's action history (reviews and timeline events) from
GitHub and verifies that no identity performed an action its role forbids
on the PR. Every identity's role comes from the trusted-authors
configuration through the trust layer (``trust.role_of``), never from its
name alone: the human holds every right; an agent is the user identity, or
the audit identity when its login ends in ``-vergil-audit``; the org's
automation identity is never forbidden; and a login with no role (a
stranger, a deleted account, a role's login of the wrong account type) is
**untrusted**, never human. An untrusted identity may not author, edit,
close, reopen or merge the PR or turn auto-merge on; its approval counts for
nothing and is reported on stderr, metadata only, so no outsider's review
can stop a submit (GHSA-q6hx-8c85-9f56).

The user identity may open, turn auto-merge on for and merge **its own** PR,
one whose author is the same identity as the action's actor (epic
vergil-project/.github#1384, its spec's §4.3). On anyone else's PR (the
human's, automation's, another agent's, an untrusted or deleted author's)
every action it is forbidden stays a violation, and its approval is one on
every PR, its own included. Submit never edits or renames a PR after
creating it, so ``edited`` stays forbidden on its own PR too. GitHub records
a ``closed`` event, by the merging actor, for every merge; that one closing
is part of the merge and is allowed with it (:func:`_merge_close`), while any
other close stays a violation (vergil-tooling#5140, vergil-tooling#5178).

**The one PR path** (epic vergil-project/.github#1664, its spec's §5.1).
Any trusted identity with write access may arm a PR the path accepts,
whoever authored it: a same-repo PR whose branch has a trusted author (the
watch's ``confirmed_head`` rule). So on such a PR the user identity may also
turn auto-merge on for, and merge, a PR it did not author, and the closing
GitHub records with that merge is allowed with it. Nothing else is relaxed:
on a fork PR, or one whose author is untrusted, both stay violations, as do
the user identity's other forbidden actions everywhere and the audit
identity's, which holds no write access. :func:`check_pr` reads whether the
PR is such a PR from the PR itself (:func:`trust.is_same_repo` and the
author's role) and hands it to :func:`evaluate`.

An action is either allowed or a violation, and a violation always fails:
there is no override
(vergil-tooling#4695). It is either a real problem to investigate or a bug
in this classifier to fix.

This is the host-side "human chokepoint" verification described in
docs/specs/2026-05-29-agent-permission-model-design.md: it closes the
gap the coarse GitHub permission model cannot enforce server-side
(notably the audit identity's ``pull_requests: write`` scope, which
GitHub cannot narrow to "review but never author/close/merge").
Turning auto-merge on merges a PR, so an agent's auto-merge enable event,
under any method's event name, is a forbidden action too, bar the path's
relaxation above; turning it off is not.

**Involvement** (:func:`pr_actors`) leaves out the actors of the events that
change no content: every auto-merge enable and disable, and, on a PR the
path accepts, the merge and the closing GitHub records with it (the path's
direct merge at clean status is the same decision as an arm). So arming a
PR, or merging one that can already merge, never makes it agent-involved.

Read-only ``gh api`` GET calls back this check. They run in the human
context where the API is available; the same endpoints are reachable
from the audit context under the identity-aware API allowance. A fetch
failure propagates (``github.read_output`` raises on nonzero exit) so a
broken check fails its caller rather than silently passing: submit leaves
the PR's auto-merge off, the path refuses the push, and the watch stops.
"""

from __future__ import annotations

import enum
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github, trust
from vergil_tooling.lib.trusted_authors import load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vergil_tooling.lib.trusted_authors import TrustedAuthors


class Role(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"
    AUTOMATION = "automation"
    UNTRUSTED = "untrusted"


# An agent identity whose login ends so is the audit identity; any other
# agent is the user identity, whose forbidden actions are the wider set.
_AUDIT_SUFFIX = "-vergil-audit"
# How an identity with no login (a deleted account) is named in a violation.
_DELETED = "(deleted account)"


def classify(identity: trust.Identity, *, org: str, authors: TrustedAuthors) -> Role:
    """The provenance role of *identity* on a PR in a repo of *org*.

    The trust layer's role decides it (:func:`trust.role_of`): a login with
    none is :attr:`Role.UNTRUSTED`, never :attr:`Role.HUMAN`.
    """
    judgement = trust.role_of(identity, org=org, authors=authors)
    if judgement.role is trust.Role.HUMAN:
        return Role.HUMAN
    if judgement.role is trust.Role.AUTOMATION:
        return Role.AUTOMATION
    if judgement.role is trust.Role.AGENTS:
        login = trust.normalise_login(cast("str", identity.login))
        return Role.AUDIT if login.endswith(_AUDIT_SUFFIX) else Role.USER
    return Role.UNTRUSTED


# The action for any auto-merge enable event, whatever its method (see
# ``_EVENT_MAP``).
_AUTO_MERGE_ENABLED = "auto_merge_enabled"

# Actions a role must never perform on a PR it is merging, bar the user
# identity's own-PR actions (``_USER_OWN_PR``). Every other
# action is allowed: the check has two outcomes, allowed or a violation, and a
# violation always fails (vergil-tooling#4695). The audit identity is not in
# use yet; its entry is kept so its forbidden actions are enforced from the
# day it is.
_FORBIDDEN: dict[Role, frozenset[str]] = {
    Role.USER: frozenset(
        {"created", "edited", "closed", "reopened", "merged", _AUTO_MERGE_ENABLED, "approved"}
    ),
    Role.AUDIT: frozenset(
        {"created", "edited", "closed", "reopened", "merged", _AUTO_MERGE_ENABLED}
    ),
    # Anyone may leave a review on a public repo, so an untrusted approval is
    # reported, not a violation: no outsider can stop a submit by reviewing.
    Role.UNTRUSTED: frozenset(
        {"created", "edited", "closed", "reopened", "merged", _AUTO_MERGE_ENABLED}
    ),
}

# The forbidden actions the user identity may still perform on its own PR:
# open it, turn auto-merge on, and merge it (the ``merged_now`` path of
# ``lib/auto_merge.py``). Submit never renames a PR, so ``edited`` is not
# among them; nor are closing, reopening or approving. The one close that is
# allowed, the closing GitHub records with the merge, is judged apart
# (:func:`_merge_close`).
_USER_OWN_PR = frozenset({"created", "merged", _AUTO_MERGE_ENABLED})

# The forbidden actions the user identity may perform on a PR the one PR path
# accepts, whoever authored it (spec §5.1): turn auto-merge on, and merge it
# at clean status. The close GitHub records with that merge is judged apart
# (:func:`_merge_close`), as on its own PR.
_USER_PATH_PR = frozenset({"merged", _AUTO_MERGE_ENABLED})

# The events whose actor is not counted as involvement on any PR: turning
# auto-merge on (any method's name, see ``_map_event``) or off changes no
# content (spec §5.1).
_AUTO_MERGE_DISABLED = "auto_merge_disabled"


@dataclass(frozen=True)
class Action:
    """A single attributed action on the PR.

    *identity* is the actor as GitHub named it; ``None`` (not known) never
    makes the PR the actor's own. *at* is the timeline event's ``created_at``
    as GitHub gave it, and *position* its index in the PR's whole timeline
    (every event, those no action records included); ``None`` for either (not
    a timeline event, or no time given) never ties a close to the merge.
    """

    login: str
    role: Role
    action: str
    identity: trust.Identity | None = None
    at: str | None = None
    position: int | None = None


def _same_identity(a: trust.Identity, b: trust.Identity) -> bool:
    """Whether *a* and *b* are one account: the same login and account type.

    A deleted account (no login) is never the same as anything.
    """
    if a.login is None or b.login is None:
        return False
    return trust.normalise_login(a.login) == trust.normalise_login(b.login) and (
        a.typename == b.typename
    )


def _own_pr(act: Action, author: trust.Identity | None) -> bool:
    """Whether *act* is the user identity acting on the PR it authored."""
    return (
        act.role is Role.USER
        and act.identity is not None
        and author is not None
        and _same_identity(act.identity, author)
    )


def _path_pr(act: Action, *, on_path: bool) -> bool:
    """Whether *act* is the user identity acting on a PR the one PR path accepts.

    *on_path* is whether the PR is one: a same-repo PR whose branch has a
    trusted author. The human and the automation App are never forbidden
    an action, so only the user identity's need relaxing; the audit
    identity holds no write access and an untrusted one is no trusted
    writer, so neither is relaxed (spec §5.1).
    """
    return on_path and act.role is Role.USER


def _identity(user: object, *, typename_key: str = "type") -> trust.Identity:
    """The :class:`trust.Identity` of a REST user object; None is a deleted account.

    A REST user names its account type in type (``User`` or ``Bot``), as
    GraphQL's ``__typename`` does.
    """
    data = cast("Mapping[str, object]", user) if isinstance(user, dict) else {}
    login = data.get("login")
    typename = data.get(typename_key)
    return trust.Identity(
        login if isinstance(login, str) and login else None,
        typename if isinstance(typename, str) else None,
    )


def _shown(identity: trust.Identity) -> str:
    return _DELETED if identity.login is None else trust.normalise_login(identity.login)


@dataclass
class ProvenanceResult:
    violations: list[Action] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


# Timeline event name -> action. Turning auto-merge on is how a PR is merged,
# so every name GitHub uses for an enable is the one ``auto_merge_enabled``
# action. ``auto_squash_enabled`` and ``auto_rebase_enabled`` were recorded
# from a live PR (tests/vergil_tooling/fixtures/pr_provenance/);
# ``auto_merge_enabled``, the merge-commit method's expected name, was not
# recorded and is listed so the map names every method, not left to the
# fail-closed pattern below.
# ``auto_merge_disabled`` is deliberately absent: an agent turning auto-merge
# off is not a violation.
_EVENT_MAP = {
    "closed": "closed",
    "reopened": "reopened",
    "merged": "merged",
    "renamed": "edited",
    "auto_merge_enabled": _AUTO_MERGE_ENABLED,
    "auto_squash_enabled": _AUTO_MERGE_ENABLED,
    "auto_rebase_enabled": _AUTO_MERGE_ENABLED,
}

# Fail closed on any other auto-merge enable: a renamed or new
# ``auto_<method>_enabled`` event by an agent is a violation, never ignored.
_AUTO_ENABLE_PATTERN = re.compile(r"auto_\w+_enabled")


def _map_event(name: str) -> str | None:
    """The action a timeline event *name* records, or ``None`` if none counts."""
    mapped = _EVENT_MAP.get(name)
    if mapped is None and _AUTO_ENABLE_PATTERN.fullmatch(name):
        return _AUTO_MERGE_ENABLED
    return mapped


# What :func:`_collect_actions` reads with ``gh pr view``: the author, and
# what :func:`trust.is_same_repo` needs (``trust.SAME_REPO_FIELDS``, named as
# ``gh pr view --json`` names them).
_VIEW_FIELDS = "author,isCrossRepository,headRepositoryOwner,headRepository"


@dataclass(frozen=True)
class _History:
    """What :func:`_collect_actions` read of a PR.

    *actions* are the attributed actions the provenance check judges.
    *actors* are every identity counted as involvement on the PR
    (:func:`pr_actors`). *same_repo* is whether the PR's head is in its own
    repo, and *branch_trusted* whether its author holds a trusted role: both
    together make it a PR the one PR path accepts.
    """

    author: trust.Identity
    actions: list[Action]
    actors: tuple[trust.Identity, ...]
    same_repo: bool
    branch_trusted: bool


def _collect_actions(pr: str) -> _History:
    """The author of *pr*, its attributed actions from GitHub, and its actors.

    The repo and number come from the PR itself, not the current directory's
    remote: ``vrg-finalize-pr <PR URL>`` run from a checkout of another repo
    would otherwise check the wrong PR's provenance, or 404 (#4013). A PR whose
    repo or number cannot be read raises ``github.GitHubAPIError``.
    """
    repo, number = github.pr_repo_and_number(pr, "the provenance check")
    org = repo.split("/", 1)[0]
    authors = load_trusted_authors()
    actions: list[Action] = []

    def add(
        identity: trust.Identity,
        action: str,
        at: str | None = None,
        position: int | None = None,
    ) -> None:
        role = classify(identity, org=org, authors=authors)
        actions.append(Action(_shown(identity), role, action, identity, at, position))

    # ``gh pr view`` names the author's account type by ``is_bot``; an App
    # author's login is ``app/<slug>``, which the trust layer normalises.
    viewed = github.read_json("pr", "view", pr, "--json", _VIEW_FIELDS)
    view = cast("Mapping[str, object]", viewed) if isinstance(viewed, dict) else {}
    raw_author = view.get("author")
    author_data = cast("Mapping[str, object]", raw_author) if isinstance(raw_author, dict) else {}
    login = author_data.get("login")
    author = trust.Identity(
        login if isinstance(login, str) and login else None,
        "Bot" if author_data.get("is_bot") is True else "User",
    )
    add(author, "created")
    try:
        same_repo = trust.is_same_repo(repo, view)
    except trust.TrustReadError as exc:
        raise github.GitHubAPIError(
            1, ("gh", "pr", "view", pr), stderr=f"the provenance check: {exc}"
        ) from exc
    branch_trusted = classify(author, org=org, authors=authors) is not Role.UNTRUSTED

    # Both lists are paginated (issue #3162): a single ``gh api`` call returns
    # only the first 30 items, so a forbidden action past page 1 was never
    # seen and the check failed open. A malformed page raises ValueError.
    reviews = cast(
        "list[dict[str, Any]]",
        github.read_json_paginated(f"repos/{repo}/pulls/{number}/reviews"),
    )
    skipped: list[trust.Skipped] = []
    actors: list[trust.Identity] = [author]
    for review in reviews:
        # Every review's author acted on the PR, whatever the review's state;
        # a review with no user is a deleted account's.
        actors.append(_identity(review.get("user")))
        if review.get("state") != "APPROVED":
            continue
        reviewer = _identity(review.get("user"))
        role = classify(reviewer, org=org, authors=authors)
        if role is Role.UNTRUSTED:
            judgement = trust.role_of(reviewer, org=org, authors=authors)
            skipped.append(
                trust.Skipped(
                    repo,
                    number,
                    "review",
                    None if reviewer.login is None else trust.normalise_login(reviewer.login),
                    str(review.get("submitted_at") or "an unknown time"),
                    f"review {review.get('id')}",
                    str(judgement.cause),
                )
            )
            continue
        actions.append(Action(_shown(reviewer), role, "approved", reviewer))
    trust.report_skipped(skipped, stream=sys.stderr)

    timeline = cast(
        "list[dict[str, Any]]",
        github.read_json_paginated(f"repos/{repo}/issues/{number}/timeline"),
    )
    uninvolved = not_involvement(timeline, on_path=same_repo and branch_trusted)
    for position, event in enumerate(timeline):
        # An event's actor, and the user of an event that names one instead
        # (a review or a comment), acted on the PR. An event naming neither
        # (a commit, whose pusher GitHub does not record) adds no actor, and
        # nor does one whose action is not involvement.
        if position not in uninvolved:
            actors.extend(
                _identity(event[key])
                for key in ("actor", "user")
                if isinstance(event.get(key), dict)
            )
        mapped = _map_event(event.get("event", ""))
        if mapped is None:
            continue
        created_at = event.get("created_at")
        add(
            _identity(event.get("actor")),
            mapped,
            created_at if isinstance(created_at, str) and created_at else None,
            position,
        )

    return _History(
        author=author,
        actions=actions,
        actors=tuple(actors),
        same_repo=same_repo,
        branch_trusted=branch_trusted,
    )


# How long after the merge GitHub may record the ``closed`` it writes with it.
# GitHub writes both events for one merge and times each to the second, so
# the close lands in the merge's second or, when the second ticks between the
# two writes, the next one (vergil-project/vergil-claude-plugin PR #1375 was
# 1 second; vergil-tooling#5178). Five seconds covers that tick and a slow
# write with margin, while staying short: the close must also be the very
# next timeline event, by the merging actor, so the window only bounds how
# late that one event may be timed and never admits a close of its own.
MERGE_CLOSE_WINDOW = timedelta(seconds=5)


def _timestamp(value: str | None) -> datetime | None:
    """*value*, a timeline ``created_at``, as a zone-aware time; ``None`` if unreadable.

    An unreadable or zone-less time cannot tie a close to the merge, so the
    close stays a violation (it fails closed, reported as one).
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class _Event:
    """A timeline event as :func:`not_involvement` judges it: no role needed."""

    action: str
    identity: trust.Identity | None
    at: str | None
    position: int


def _merge_close(actions: Sequence[Action | _Event]) -> int | None:
    """The index in *actions* of the ``closed`` event that accompanied the merge.

    GitHub records a ``closed`` event, by the merging actor, for every merge
    (vergil-tooling#5140). A close is that event only when all of these hold:

    - the PR merged: *actions* hold exactly one ``merged`` event;
    - the close is the first ``closed`` event after it in the timeline's
      order (GitHub lists a timeline oldest first, and lists the merge's
      ``closed`` right after its ``merged``);
    - no other timeline event lies between the two: the close's position in
      the PR's whole timeline is the one right after the merge's, so an event
      the check otherwise ignores (a comment, a label, a branch deletion)
      between them disqualifies it too;
    - its ``created_at`` is the merge's or later by at most
      :data:`MERGE_CLOSE_WINDOW`: GitHub gives both to the second, usually
      the same second (vergil-project/vergil-fixture-python-lib PR #87,
      recorded in
      ``tests/vergil_tooling/fixtures/pr_provenance/own_pr_squash_merge.json``)
      but sometimes the next (vergil-project/vergil-claude-plugin PR #1375,
      recorded in ``own_pr_merge_close_next_second.json`` beside it;
      vergil-tooling#5178);
    - its actor is the merge's actor.

    Order, adjacency and timestamp are all required, so a close before the
    merge (a close and reopen included), one with any event before it since
    the merge, or one outside the window never qualifies, nor does an event
    with no position, no readable timestamp or no actor. ``None`` when no
    close accompanies a merge.
    """
    merges = [i for i, act in enumerate(actions) if act.action == "merged"]
    if len(merges) != 1:
        return None
    merge = actions[merges[0]]
    close_at = next(
        (i for i in range(merges[0] + 1, len(actions)) if actions[i].action == "closed"), None
    )
    if close_at is None:
        return None
    close = actions[close_at]
    if merge.position is None or close.position != merge.position + 1:
        return None
    merged_when = _timestamp(merge.at)
    closed_when = _timestamp(close.at)
    if merged_when is None or closed_when is None:
        return None
    if not timedelta(0) <= closed_when - merged_when <= MERGE_CLOSE_WINDOW:
        return None
    if merge.identity is None or close.identity is None:
        return None
    if not _same_identity(merge.identity, close.identity):
        return None
    return close_at


def evaluate(
    actions: list[Action],
    *,
    author: trust.Identity | None = None,
    same_repo: bool,
    branch_trusted: bool,
) -> ProvenanceResult:
    """The violations among *actions* on a PR *author* opened. Pure.

    Each action its role forbids is a violation, except the user identity's
    opening, auto-merging and merging of its own PR, and the closing GitHub
    records with that merge (:func:`_merge_close`). With no *author*, no PR
    is anyone's own. *actions* keep the timeline's order.

    *same_repo* (the PR's head is in its own repo) and *branch_trusted* (its
    author holds a trusted role) together make it a PR the one PR path
    accepts, on which the user identity's auto-merge enable and merge, and
    the closing with that merge, are allowed whoever authored it (spec
    §5.1). Both are required: every caller reads them from the PR.
    """
    on_path = same_repo and branch_trusted
    result = ProvenanceResult()
    merge_close = _merge_close(actions)
    for index, act in enumerate(actions):
        if act.action not in _FORBIDDEN.get(act.role, frozenset()):
            continue
        if act.action in _USER_OWN_PR and _own_pr(act, author):
            continue
        if act.action in _USER_PATH_PR and _path_pr(act, on_path=on_path):
            continue
        if index == merge_close and (_own_pr(act, author) or _path_pr(act, on_path=on_path)):
            continue
        result.violations.append(act)
    return result


def check_pr(pr: str) -> ProvenanceResult:
    """Verify PR provenance by fetching and evaluating its action history."""
    history = _collect_actions(pr)
    return evaluate(
        history.actions,
        author=history.author,
        same_repo=history.same_repo,
        branch_trusted=history.branch_trusted,
    )


def not_involvement(timeline: Sequence[Mapping[str, object]], *, on_path: bool) -> frozenset[int]:
    """The positions in *timeline* whose actors are not counted as involvement. Pure.

    *timeline* is a PR's whole timeline, oldest first, each event with its
    ``event`` name and, where GitHub gives them, its ``actor`` (a REST user
    object) and ``created_at``. Every auto-merge enable (under any method's
    name) and disable changes no content, so it is not involvement. On a PR
    the one PR path accepts (*on_path*: a same-repo PR whose branch has a
    trusted author), neither is its merge, the path's direct merge at clean
    status being the same decision as an arm, nor the closing GitHub records
    with it (:func:`_merge_close`). The finalize watch (through
    :func:`pr_actors`) and the daily agent audit both judge involvement by
    this rule (spec §5.1, §5.9).
    """
    left_out: set[int] = set()
    events: list[_Event] = []
    for position, event in enumerate(timeline):
        name = event.get("event")
        name = name if isinstance(name, str) else ""
        if name == _AUTO_MERGE_DISABLED or _map_event(name) == _AUTO_MERGE_ENABLED:
            left_out.add(position)
            continue
        if name not in ("merged", "closed"):
            continue
        created_at = event.get("created_at")
        events.append(
            _Event(
                action=name,
                identity=_identity(event.get("actor")) if "actor" in event else None,
                at=created_at if isinstance(created_at, str) and created_at else None,
                position=position,
            )
        )
    if on_path:
        left_out.update(e.position for e in events if e.action == "merged")
        close = _merge_close(events)
        if close is not None:
            left_out.add(events[close].position)
    return frozenset(left_out)


def pr_actors(pr: str) -> tuple[trust.Identity, ...]:
    """Every identity GitHub records acting on *pr*, its author first.

    The evidence :func:`vergil_tooling.lib.agent_gates.agent_involved` takes
    for an open or merged PR: the author, the author of every review (any
    state) and the actor or user of every timeline event naming one, bar
    the events :func:`not_involvement` leaves out (arming, and on a PR the
    one PR path accepts its merge and the merge's closing). Read
    as :func:`check_pr` reads the PR, every page; a failed or malformed read
    raises, never an empty answer.
    """
    return _collect_actions(pr).actors
