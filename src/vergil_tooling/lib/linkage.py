"""Shared issue-linkage regex patterns.

Extracted from ``vrg_pr_issue_linkage`` so both the CI gate and
``vrg-resolve-tracking-issue`` use the same patterns.
"""

from __future__ import annotations

import re

# The only linkage keywords allowed in PR bodies and templates.
# Auto-close keywords (Closes/Fixes/Resolves) are banned repo-wide —
# issues stay open until post-merge workflows succeed.
ALLOWED_LINKAGES = ("Ref",)

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

# A linkage *value* in the PR submission fields: a bare keyword, optionally
# followed by an issue reference (``#42`` or ``org/repo#42``). The reference is
# matched so it can be stripped — the issue number is appended automatically
# when the PR body is rendered, so it must not be carried on the keyword.
LINKAGE_VALUE_RE = re.compile(
    r"^\s*(?P<keyword>[A-Za-z]+)"
    r"(?:\s+(?:[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#[0-9]+)?\s*$"
)


def normalize_linkage(value: str) -> tuple[str, str | None]:
    """Validate and normalize a PR issue-linkage value to a bare keyword.

    The linkage stored in the PR submission fields must be a *bare* allowed
    keyword (e.g. ``Ref``); the issue number is appended automatically when the
    PR body is built. Passing the keyword *with* the issue number
    (``Ref #1761``) is a common and unambiguous mistake, so strip the number and
    return a warning rather than hard-failing.

    Returns a ``(canonical, warning)`` pair:

    - ``canonical`` is the bare allowed keyword to use downstream.
    - ``warning`` is a human-readable note when a stray issue number was
      stripped, or ``None`` when *value* was already a clean bare keyword.

    Raises ``ValueError`` with a user-ready message when *value* is not a
    recognized linkage keyword (the message states the contract and echoes the
    offending value so the caller can surface it verbatim).
    """
    match = LINKAGE_VALUE_RE.match(value)
    keyword = match.group("keyword") if match else None
    if keyword not in ALLOWED_LINKAGES:
        allowed = ", ".join(ALLOWED_LINKAGES)
        raise ValueError(
            f"linkage must be a bare keyword (one of: {allowed}) — the issue "
            f"number is added automatically; got {value!r}. "
            f"Pass just {ALLOWED_LINKAGES[0]!r}."
        )
    if value.strip() == keyword:
        return keyword, None
    warning = (
        f"linkage {value!r} includes an issue number; using bare {keyword!r} "
        "(the issue number is appended automatically)."
    )
    return keyword, warning


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
