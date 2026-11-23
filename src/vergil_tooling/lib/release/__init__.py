"""Release workflow utilities."""

from __future__ import annotations

import re
import sys

from vergil_tooling.lib import trust
from vergil_tooling.lib.release.checklist import BEGIN as _PROGRESS_MARKER

_LEGACY_CHORE_RE = re.compile(r"^chore/(bump-version-|\d+-next-cycle-deps-)")

# A release tracking issue's title is exactly ``release: X.Y.Z`` (see
# release.tracking / release.resume). Matching the version form — not a bare
# ``release:`` prefix — keeps a legitimately-titled epic (e.g. one about the
# release pipeline) from being mistaken for release bookkeeping.
_RELEASE_TITLE_RE = re.compile(r"^release:\s+\d+\.\d+\.\d+\s*$")


def is_release_branch(branch: str) -> bool:
    """Return True if the branch is part of the release workflow.

    All release-cycle branches use the ``release/`` prefix.
    The legacy ``chore/bump-version-`` and ``chore/<N>-next-cycle-deps-``
    patterns are transitional: standard-actions and the publish skill
    still create these with the old prefix. Remove once all creators
    are updated to use ``release/``.
    """
    return branch.startswith("release/") or bool(_LEGACY_CHORE_RE.match(branch))


def is_release_tracking_issue(
    repo: str, number: int, *, title: str | None = None, body: str | None = None
) -> bool:
    """Return True if *repo*#*number* is a vrg-release tracking issue, not epic/task work.

    Release-tracking issues (``release: X.Y.Z``) are release bookkeeping and must
    never appear in the epic/task observability outputs (roadmap, activity log,
    drift audit) — they are not tasks (issue #1984). The authoritative signal is
    the ``<!-- vrg-release:progress -->`` checklist marker vrg-release writes into
    the body; the ``release: X.Y.Z`` title is a secondary signal for callers that
    only have the title, or for an issue whose body marker was hand-removed.

    A title or marker alone never identifies the issue (epic
    vergil-project/.github#1467, spec §5.2). When either signal matches the
    caller's *title* or *body*, the body is read again through the trust layer
    (:func:`trust.read_issue`), and the issue is a tracking issue only when that
    body is trusted; the marker counts only in the trusted body. An untrusted
    body is reported on stderr, metadata only, and the issue is not a tracking
    issue. A read the layer cannot complete raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`. With neither signal no
    read is made.
    """
    titled = bool(title and _RELEASE_TITLE_RE.match(title))
    if not titled and not (body and _PROGRESS_MARKER in body):
        return False
    content = trust.read_issue(repo, number)
    trust.report_skipped(
        [item for item in content.skipped if item.field == "body"], stream=sys.stderr
    )
    if content.body is None:
        return False
    return titled or _PROGRESS_MARKER in content.body
