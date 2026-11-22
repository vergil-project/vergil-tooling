"""The bodies of an issue's trusted comments, for readers that need nothing else.

A thin reader over :func:`vergil_tooling.lib.trust.read_issue` for the callers
that parse markers or dedup on comment text (epic vergil-project/.github#1467,
spec §5.2): the queue views, intake routing's planning-epic guard, the
operational-outcome audit and the security-record comment dedups. A comment the
layer does not trust (its author or an editor holds no role) is left out and
reported on stderr, metadata only (:func:`~vergil_tooling.lib.trust.report_skipped`);
the body is not judged here, since these callers take nothing from it through
this read. A read the layer cannot complete raises
:class:`~vergil_tooling.lib.trust.TrustReadError`.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import trust

if TYPE_CHECKING:
    from vergil_tooling.lib.epics import IssueRef
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

# The field the layer gives a body's Skipped record; a comment's is "comment <id>".
_BODY_FIELD = "body"


def bodies(ref: IssueRef, authors: TrustedAuthors | None = None) -> list[str]:
    """The bodies of *ref*'s trusted comments, oldest first; each skipped one is reported.

    *authors* defaults to the loaded configuration, whose errors propagate.
    """
    content = trust.read_issue(f"{ref.owner}/{ref.repo}", ref.number, authors=authors)
    trust.report_skipped(
        [item for item in content.skipped if item.field != _BODY_FIELD], stream=sys.stderr
    )
    return [comment.body for comment in content.comments]
