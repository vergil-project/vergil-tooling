"""Canonical work-unit identifier: branch name ⇄ worktree directory name.

One unit of work threads a single identifier across the tooling —
``feature/<N>-<slug>`` as the git branch and ``issue-<N>-<slug>`` as the
canonical ``.worktrees/`` directory. This module is the single source of
truth for parsing and validating that identifier, so the format cannot
drift between the commit-time guard (``vrg-commit``) and the
creation-time guard (``vrg-git worktree add``/``checkout -b``). Issue #2550.

An "issue branch" is one whose prefix commits it to carrying a repo issue
number: ``feature/``, ``bugfix/``, ``hotfix/``, ``chore/``. Integration and
release branches (``develop``, ``main``, ``release/*``, ``promotion/*``) are
deliberately *not* issue branches — they carry no issue number and are left
untouched by every helper here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Branch prefixes that must carry a repo issue number. Kept as a tuple so the
# two regexes below and the human-facing message all read from one list.
ISSUE_BRANCH_TYPES: tuple[str, ...] = ("feature", "bugfix", "hotfix", "chore")

_TYPES_ALT = "|".join(ISSUE_BRANCH_TYPES)

# A branch *commits to* the issue-number rule as soon as it uses an issue-branch
# prefix; whether it satisfies the rule is a separate check (_ISSUE_FORMAT_RE).
_ISSUE_REQUIRED_RE = re.compile(rf"^({_TYPES_ALT})/")

# The full canonical form: <type>/<N>-<slug>, where <N> is one or more digits and
# <slug> starts with a lowercase alphanumeric then lowercase alphanumerics, dots
# or hyphens. The capture groups back parse_issue_branch.
_ISSUE_FORMAT_RE = re.compile(rf"^({_TYPES_ALT})/([0-9]+)-([a-z0-9][a-z0-9.-]*)$")

# The canonical worktree-directory prefix. The directory is type-agnostic —
# always ``issue-<N>-<slug>`` regardless of the branch's <type> — matching the
# convention in lib/worktrees.py (_branch_from_worktree_name, match_worktrees).
WORKTREE_DIR_PREFIX = "issue-"

# Shown wherever a branch fails the format check, so the two guards give an
# identical, actionable message.
FORMAT_HINT = "Expected format: {type}/{issue}-{description}  (example: feature/42-add-caching)"


@dataclass(frozen=True)
class ParsedBranch:
    """The three components of a canonical issue branch."""

    type: str
    number: str
    slug: str


def requires_issue_number(branch: str) -> bool:
    """True when *branch*'s prefix commits it to carrying a repo issue number.

    Only the issue-branch prefixes (``feature/`` etc.) qualify; ``develop``,
    ``main``, ``release/*`` and the like return False and are never subject to
    the format rule.
    """
    return bool(_ISSUE_REQUIRED_RE.match(branch))


def is_valid_issue_branch(branch: str) -> bool:
    """True when *branch* is a fully-formed ``<type>/<N>-<slug>`` issue branch."""
    return bool(_ISSUE_FORMAT_RE.match(branch))


def parse_issue_branch(branch: str) -> ParsedBranch | None:
    """Split *branch* into ``ParsedBranch(type, number, slug)``, or None.

    Returns None for anything that is not a fully-formed issue branch — a
    non-issue prefix, or an issue prefix missing the ``<N>-<slug>`` body — so a
    caller can distinguish "not our concern" from a positive parse.
    """
    match = _ISSUE_FORMAT_RE.match(branch)
    if match is None:
        return None
    return ParsedBranch(type=match.group(1), number=match.group(2), slug=match.group(3))


def expected_worktree_dirname(branch: str) -> str | None:
    """Return the canonical ``issue-<N>-<slug>`` directory name for *branch*.

    Derived from the same ``<N>`` and ``<slug>`` as the branch so the two stay
    in lockstep (issue #2550). Returns None when *branch* is not a fully-formed
    issue branch, since there is no canonical directory to derive.
    """
    parsed = parse_issue_branch(branch)
    if parsed is None:
        return None
    return f"{WORKTREE_DIR_PREFIX}{parsed.number}-{parsed.slug}"
