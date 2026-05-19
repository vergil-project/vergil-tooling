"""Shared issue-linkage regex patterns.

Extracted from ``vrg_pr_issue_linkage`` so both the CI gate and
``vrg-resolve-tracking-issue`` use the same patterns.
"""

from __future__ import annotations

import re

LINKAGE_RE = re.compile(
    r"^\s*[-*]?\s*Ref:?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#([0-9]+)",
    re.MULTILINE,
)

AUTOCLOSE_RE = re.compile(
    r"^\s*[-*]?\s*(close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#[0-9]+",
    re.MULTILINE | re.IGNORECASE,
)


def extract_tracking_issue(text: str) -> int | None:
    """Return the tracking issue number from a ``Ref #N`` match.

    Raises ``ValueError`` if multiple ``Ref`` lines are found.
    """
    matches = LINKAGE_RE.findall(text)
    if not matches:
        return None
    if len(matches) > 1:
        msg = f"multiple tracking issue references found ({len(matches)})"
        raise ValueError(msg)
    return int(matches[0][1])
