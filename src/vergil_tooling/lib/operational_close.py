"""The gate on closing an operational task as completed: its result comes first.

An operational task (validation, deployment, ...) is accepted by a recorded
``Outcome: SUCCESS`` comment, not a merged PR, and closes only once that
result is posted. A skill that sends the result comment and the close in one
batch can close the task even when the comment failed, leaving a close with no
result above it (vergil-project/vergil-tooling#4997). This module makes the
order mechanical: :func:`refusal` names why a completed close must wait while
the issue's trusted comments carry no success ``Outcome:`` line.

The comments are read through the trust layer
(:func:`~vergil_tooling.lib.trusted_comments.bodies`), so an outsider's
``Outcome: SUCCESS`` never lets a close through. The same success marker is
what ``vrg-epic-audit``'s closed-operational invariant looks for
(:data:`SUCCESS_OUTCOME_RE`), so a close this gate lets through is never
flagged there as closed without a success.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, github, trusted_comments

if TYPE_CHECKING:
    from collections.abc import Iterable

# The unified success marker is ``Outcome: SUCCESS``; ``Outcome: PASS`` is a
# legacy alias, so validations recorded before the unification still count.
# Kept narrow so an unresolved ``Outcome: SUCCESS / FAILURE`` template line does
# not read as a success.
SUCCESS_OUTCOME_RE = re.compile(
    r"^\s*[-*]?\s*Outcome:\s*(?:SUCCESS|PASS)\s*$", re.MULTILINE | re.IGNORECASE
)


def records_success(bodies: Iterable[str]) -> bool:
    """True if any of *bodies* carries a success ``Outcome:`` line."""
    return any(SUCCESS_OUTCOME_RE.search(body) for body in bodies)


def refusal(ref: epics.IssueRef) -> str | None:
    """Why closing *ref* as completed must wait, or ``None`` when it may close.

    ``None`` for an issue with no operational label, and for an operational
    task whose trusted comments already record a success. Reads run under
    *ref*'s owner's App installation. A read that cannot complete raises (a
    :class:`subprocess.CalledProcessError`, a
    :class:`~vergil_tooling.lib.trust.TrustReadError`, or the trusted-author
    configuration's error), never reading as "may close".
    """
    with github.target_org(ref.owner):
        kind = epics.operational_kind(ref)
        if kind is None:
            return None
        if records_success(trusted_comments.bodies(ref)):
            return None
    slug = f"{ref.owner}/{ref.repo}#{ref.number}"
    return (
        f"{slug} is a {kind} task, and no trusted comment on it records "
        "'Outcome: SUCCESS'. Post the result comment first and close only once it "
        "has posted; a close comment does not count as the result. A task that did "
        "not succeed stays open."
    )
