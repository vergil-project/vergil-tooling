"""Reading an issue's trusted comment stream for the turn checks, through the trust layer.

Every comment is read and judged by
:func:`vergil_tooling.lib.trust.read_comment_stream` (epic
vergil-project/.github#1467, spec §5.2): a comment counts only when its
author and every editor are the human (a GraphQL ``User``) or an agent (a
``Bot``). A login named like a trusted one but of the wrong account type, an
outsider, a deleted account, the automation App, or an outsider's edit of a
trusted comment leaves the comment out of the stream; each one left out is
reported on stderr, metadata only. A read the layer cannot complete raises
:class:`~vergil_tooling.lib.trust.TrustReadError`, so the turn checks never
see a truncated stream.

Each comment's ID is GraphQL's ``fullDatabaseId`` (a ``BigInt``, returned as a
string), which equals the REST ``id``; never ``databaseId``, a 32-bit ``Int``
that current comment IDs overflow.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import trust
from vergil_tooling.lib.rulings import RulingError

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

_ISSUE_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#([1-9][0-9]*)$")
_ID_RE = re.compile(r"^[1-9][0-9]*$")

#: The roles whose comments make up the stream: the human and the agents.
STREAM_ROLES = frozenset({trust.Role.HUMAN, trust.Role.AGENTS})


@dataclass(frozen=True)
class IssueComment:
    """One trusted issue comment as the turn checks see it.

    ``author`` is the bare login of an author the trust layer trusts.
    ``created_at`` is GitHub's ``createdAt``, an ISO 8601 timestamp; it is
    always set on a read comment, and empty only on one built by hand.
    ``role`` is the role the trust layer judged the comment's author to hold
    (the human or the agents, for a stream read with the default roles); it
    is ``None`` only on one built by hand.
    """

    id: int
    author: str
    body: str
    edited: bool
    created_at: str = ""
    role: trust.Role | None = None


@dataclass(frozen=True)
class Stream:
    """An issue's trusted comments, oldest first, and every comment ID on it, trusted or not."""

    comments: list[IssueComment]
    all_ids: frozenset[int]


def parse_issue_ref(ref: str) -> tuple[str, int]:
    """Split ``owner/repo#N`` into ``("owner/repo", N)``; refuse any other form."""
    match = _ISSUE_REF_RE.fullmatch(ref)
    if match is None:
        msg = f"--issue must be owner/repo#N, got {ref!r}"
        raise RulingError(msg)
    return match.group(1), int(match.group(2))


def _comment_id(raw: str, where: str) -> int:
    if _ID_RE.fullmatch(raw) is None:
        msg = f"{where}: a comment has no usable fullDatabaseId"
        raise RulingError(msg)
    return int(raw)


def read_stream(
    repo: str,
    issue: int,
    authors: TrustedAuthors | None = None,
    *,
    roles: frozenset[trust.Role] = STREAM_ROLES,
) -> Stream:
    """Every comment on ``repo#issue`` the layer trusts, oldest first, and every comment ID.

    *authors* defaults to the loaded configuration. *roles* narrows the
    stream to the roles a reader accepts; it defaults to :data:`STREAM_ROLES`.
    Each comment carries its judged role. Each untrusted comment
    is reported on stderr, metadata only, and its ID is still in
    ``all_ids``, so a request that exists but is untrusted is told apart
    from one that is gone. Raises
    :class:`~vergil_tooling.lib.trust.TrustReadError` when the comments
    cannot be read in full, and :class:`RulingError` on a comment ID that is
    not a positive integer.
    """
    where = f"{repo}#{issue}"
    read = trust.read_comment_stream(repo, issue, roles=roles, authors=authors)
    trust.report_skipped(read.skipped, stream=sys.stderr)
    return Stream(
        comments=[
            IssueComment(
                id=_comment_id(comment.id, where),
                author=comment.login,
                body=comment.body,
                edited=comment.edited,
                created_at=comment.at,
                role=comment.role,
            )
            for comment in read.comments
        ],
        all_ids=frozenset(_comment_id(raw, where) for raw in read.ids),
    )
