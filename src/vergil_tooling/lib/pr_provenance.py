"""Pre-merge provenance verification for vrg-finalize-pr.

Fetches a PR's action history (reviews and timeline events) from
GitHub and verifies that no agent identity performed an action its
role forbids on the PR being merged. Agent identities are recognized
by the account naming convention (``*-vergil-user`` and
``*-vergil-audit``); everything else is treated as human and is never
forbidden — the human holds every right.

This is the host-side "human chokepoint" verification described in
docs/specs/2026-05-29-agent-permission-model-design.md: it closes the
gap the coarse GitHub permission model cannot enforce server-side
(notably the audit identity's ``pull_requests: write`` scope, which
GitHub cannot narrow to "review but never author/close/merge").

Read-only ``gh api`` GET calls back this check. They run in the human
context where the API is available; the same endpoints are reachable
from the audit context under the identity-aware API allowance. A fetch
failure propagates (``github.read_output`` raises on nonzero exit) so a
broken check aborts the merge rather than silently passing.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field

from vergil_tooling.lib import github


class Role(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"


def classify_login(login: str) -> Role:
    """Map a GitHub login to an identity role by naming convention."""
    if login.endswith("-vergil-audit"):
        return Role.AUDIT
    if login.endswith("-vergil-user"):
        return Role.USER
    return Role.HUMAN


# Actions an agent role must never perform on a PR it is merging.
_FORBIDDEN: dict[Role, frozenset[str]] = {
    Role.USER: frozenset({"created", "edited", "closed", "reopened", "merged", "approved"}),
    Role.AUDIT: frozenset({"created", "edited", "closed", "reopened", "merged"}),
}

# Actions permitted but advisory — surfaced to the human, not blocked.
_ADVISORY: dict[Role, frozenset[str]] = {
    Role.AUDIT: frozenset({"approved"}),
}


@dataclass(frozen=True)
class Action:
    """A single agent-attributed action on the PR."""

    login: str
    role: Role
    action: str


@dataclass
class ProvenanceResult:
    violations: list[Action] = field(default_factory=list)
    advisories: list[Action] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


_EVENT_MAP = {
    "closed": "closed",
    "reopened": "reopened",
    "merged": "merged",
    "renamed": "edited",
}


def _collect_actions(pr: str) -> list[Action]:
    """Collect agent-attributed actions on *pr* from GitHub."""
    repo = github.current_repo()
    number = github.read_output("pr", "view", pr, "--json", "number", "--jq", ".number").strip()
    actions: list[Action] = []

    author = github.read_output(
        "pr", "view", pr, "--json", "author", "--jq", ".author.login"
    ).strip()
    if author:
        actions.append(Action(author, classify_login(author), "created"))

    reviews_raw = github.read_output("api", f"repos/{repo}/pulls/{number}/reviews")
    for review in json.loads(reviews_raw or "[]"):
        if review.get("state") == "APPROVED":
            login = (review.get("user") or {}).get("login", "")
            if login:
                actions.append(Action(login, classify_login(login), "approved"))

    timeline_raw = github.read_output("api", f"repos/{repo}/issues/{number}/timeline")
    for event in json.loads(timeline_raw or "[]"):
        mapped = _EVENT_MAP.get(event.get("event", ""))
        if mapped is None:
            continue
        login = (event.get("actor") or {}).get("login", "")
        if login:
            actions.append(Action(login, classify_login(login), mapped))

    return actions


def evaluate(actions: list[Action]) -> ProvenanceResult:
    """Partition *actions* into violations and advisories. Pure."""
    result = ProvenanceResult()
    for act in actions:
        if act.role is Role.HUMAN:
            continue
        if act.action in _ADVISORY.get(act.role, frozenset()):
            result.advisories.append(act)
        elif act.action in _FORBIDDEN.get(act.role, frozenset()):
            result.violations.append(act)
    return result


def check_pr(pr: str) -> ProvenanceResult:
    """Verify PR provenance by fetching and evaluating its action history."""
    return evaluate(_collect_actions(pr))
