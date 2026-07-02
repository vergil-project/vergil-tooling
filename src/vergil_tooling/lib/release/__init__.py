"""Release workflow utilities."""

from __future__ import annotations

import re

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


def is_release_tracking_issue(title: str | None = None, body: str | None = None) -> bool:
    """Return True if an issue is a vrg-release tracking issue, not epic/task work.

    Release-tracking issues (``release: X.Y.Z``) are release bookkeeping and must
    never appear in the epic/task observability outputs (roadmap, activity log,
    drift audit) — they are not tasks (issue #1984). The authoritative signal is
    the ``<!-- vrg-release:progress -->`` checklist marker vrg-release writes into
    the body; the ``release: X.Y.Z`` title is a secondary signal for callers that
    only have the title, or for an issue whose body marker was hand-removed.
    """
    if body and _PROGRESS_MARKER in body:
        return True
    return bool(title and _RELEASE_TITLE_RE.match(title))
