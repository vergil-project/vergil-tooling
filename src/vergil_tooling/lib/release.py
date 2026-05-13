"""Release-workflow branch identification."""

from __future__ import annotations

import re

_LEGACY_CHORE_RE = re.compile(r"^chore/(bump-version-|\d+-next-cycle-deps-)")


def is_release_branch(branch: str) -> bool:
    """Return True if the branch is part of the release workflow.

    All release-cycle branches use the ``release/`` prefix.
    The legacy ``chore/bump-version-`` and ``chore/<N>-next-cycle-deps-``
    patterns are transitional: standard-actions and the publish skill
    still create these with the old prefix. Remove once all creators
    are updated to use ``release/``.
    """
    return branch.startswith("release/") or bool(_LEGACY_CHORE_RE.match(branch))
