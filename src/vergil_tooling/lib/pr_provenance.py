"""Provenance verification for auto-merging PRs.

``vrg-submit-pr`` runs it on each PR before it turns auto-merge on, and
``vrg-finalize-pr``'s watch phase (``lib/finalize_watch.py``) runs it when
the watch starts and again each time a PR's head changes. Neither command
merges a PR itself: GitHub merges it through auto-merge.

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
creating it, so ``edited`` stays forbidden on its own PR too. An action is
either allowed or a
violation, and a violation always fails: there is no override
(vergil-tooling#4695). It is either a real problem to investigate or a bug
in this classifier to fix.

This is the host-side "human chokepoint" verification described in
docs/specs/2026-05-29-agent-permission-model-design.md: it closes the
gap the coarse GitHub permission model cannot enforce server-side
(notably the audit identity's ``pull_requests: write`` scope, which
GitHub cannot narrow to "review but never author/close/merge").
Turning auto-merge on merges a PR, so an agent's auto-merge enable event,
under any method's event name, is a forbidden action too; turning it off is
not.

Read-only ``gh api`` GET calls back this check. They run in the human
context where the API is available; the same endpoints are reachable
from the audit context under the identity-aware API allowance. A fetch
failure propagates (``github.read_output`` raises on nonzero exit) so a
broken check fails its caller rather than silently passing: submit leaves
the PR's auto-merge off, and the watch stops.
"""

from __future__ import annotations

import enum
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github, trust
from vergil_tooling.lib.trusted_authors import load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Mapping

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
# among them; nor are closing, reopening or approving.
_USER_OWN_PR = frozenset({"created", "merged", _AUTO_MERGE_ENABLED})


@dataclass(frozen=True)
class Action:
    """A single attributed action on the PR.

    *identity* is the actor as GitHub named it; ``None`` (not known) never
    makes the PR the actor's own.
    """

    login: str
    role: Role
    action: str
    identity: trust.Identity | None = None


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


@dataclass(frozen=True)
class _History:
    """What :func:`_collect_actions` read of a PR.

    *actions* are the attributed actions the provenance check judges.
    *actors* are every identity GitHub records acting on the PR: its author,
    the author of every review (whatever its state, an untrusted one
    included) and the actor or user of every timeline event that names one.
    """

    author: trust.Identity
    actions: list[Action]
    actors: tuple[trust.Identity, ...]


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

    def add(identity: trust.Identity, action: str) -> None:
        actions.append(
            Action(_shown(identity), classify(identity, org=org, authors=authors), action, identity)
        )

    # ``gh pr view`` names the author's account type by ``is_bot``; an App
    # author's login is ``app/<slug>``, which the trust layer normalises.
    viewed = github.read_json("pr", "view", pr, "--json", "author")
    raw_author = viewed.get("author") if isinstance(viewed, dict) else None
    author_data = cast("Mapping[str, object]", raw_author) if isinstance(raw_author, dict) else {}
    login = author_data.get("login")
    author = trust.Identity(
        login if isinstance(login, str) and login else None,
        "Bot" if author_data.get("is_bot") is True else "User",
    )
    add(author, "created")

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
    for event in timeline:
        # An event's actor, and the user of an event that names one instead
        # (a review or a comment), acted on the PR. An event naming neither
        # (a commit, whose pusher GitHub does not record) adds no actor.
        actors.extend(
            _identity(event[key]) for key in ("actor", "user") if isinstance(event.get(key), dict)
        )
        mapped = _map_event(event.get("event", ""))
        if mapped is None:
            continue
        add(_identity(event.get("actor")), mapped)

    return _History(author=author, actions=actions, actors=tuple(actors))


def evaluate(actions: list[Action], *, author: trust.Identity | None = None) -> ProvenanceResult:
    """The violations among *actions* on a PR *author* opened. Pure.

    Each action its role forbids is a violation, except the user identity's
    opening, auto-merging and merging of its own PR. With no *author*, no
    PR is anyone's own.
    """
    result = ProvenanceResult()
    for act in actions:
        if act.action not in _FORBIDDEN.get(act.role, frozenset()):
            continue
        if act.action in _USER_OWN_PR and _own_pr(act, author):
            continue
        result.violations.append(act)
    return result


def check_pr(pr: str) -> ProvenanceResult:
    """Verify PR provenance by fetching and evaluating its action history."""
    history = _collect_actions(pr)
    return evaluate(history.actions, author=history.author)


def pr_actors(pr: str) -> tuple[trust.Identity, ...]:
    """Every identity GitHub records acting on *pr*, its author first.

    The evidence :func:`vergil_tooling.lib.agent_gates.agent_involved` takes
    for an open or merged PR: the author, the author of every review (any
    state) and the actor or user of every timeline event naming one. Read
    as :func:`check_pr` reads the PR, every page; a failed or malformed read
    raises, never an empty answer.
    """
    return _collect_actions(pr).actors
