"""Shared construction of standards-compliant commit messages.

Both ``vrg-commit`` (new commits) and ``vrg-reword`` (rewriting an
existing branch-local commit's message) build the same conventional
subject, optional body, and co-author trailer through these helpers, so
a reworded message is stamped exactly like a freshly authored one.
"""

from __future__ import annotations

import re

ALLOWED_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "test",
    "chore",
    "ci",
    "build",
    "revert",
)

# Matches a GitHub auto-close keyword anywhere in a commit body. Broader
# than ``linkage.AUTOCLOSE_RE`` (which anchors to a line start for PR
# bodies) — a commit body must not smuggle an auto-close even mid-line.
AUTOCLOSE_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    r":?\s+([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#[0-9]+",
    re.IGNORECASE,
)


def contains_autoclose(body: str) -> bool:
    """Return True if *body* contains a GitHub auto-close keyword."""
    return bool(AUTOCLOSE_RE.search(body))


def build_commit_message(
    *,
    commit_type: str,
    scope: str,
    message: str,
    body: str = "",
    co_author: str | None = None,
) -> str:
    """Return the full commit message text (subject, body, co-author trailer).

    The layout matches what ``vrg-commit`` writes to its commit-message
    file byte-for-byte: a ``type(scope): message`` subject line, then an
    optional blank-line-separated body, then an optional
    ``Co-Authored-By`` trailer.
    """
    subject = f"{commit_type}({scope}): {message}"
    parts = [f"{subject}\n"]
    if body:
        parts.append(f"\n{body}\n")
    if co_author:
        parts.append(f"\nCo-Authored-By: {co_author}\n")
    return "".join(parts)
