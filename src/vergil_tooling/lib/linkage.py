"""Shared issue-linkage regex patterns.

Extracted from ``vrg_pr_issue_linkage`` so both the CI gate and
``vrg-resolve-tracking-issue`` use the same patterns.
"""

from __future__ import annotations

import re

# ``ALLOWED_LINKAGES`` is the keyword set accepted as a PR *submission-field*
# value (the ``--linkage`` choices for vrg-submit-pr / vrg-pr-fix-body). It stays
# Ref-only until vrg-submit-pr learns to emit ``Closes`` for managed tasks (epic
# vergil-project/.github#75, task T3), where ``Closes`` is added alongside the
# auto-selection logic and its tests.
#
# The *body* patterns below already recognize ``Closes`` as sanctioned linkage,
# so the CI gate and the extract_* helpers accept a task PR that closes its issue
# by keyword. ``Fixes``/``Resolves`` stay banned so there is one close keyword.
ALLOWED_LINKAGES = ("Ref",)

# Primary linkage in a PR body: ``Ref`` or the close family (canonical
# ``Closes``; ``Close``/``Closed`` accepted as equivalents). The keyword is
# non-capturing, so the two capture groups stay (repo, number) and the
# extract_* helpers recognize a task linked by either keyword.
LINKAGE_RE = re.compile(
    r"^\s*[-*]?\s*(?:Ref|Close[sd]?):?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#([0-9]+)",
    re.MULTILINE | re.IGNORECASE,
)

# Auto-close keywords that remain BANNED in PR bodies (``Fixes``/``Resolves`` and
# variants); ``Closes`` is sanctioned above as the one close keyword. Used only
# by the PR-body gate — commit-message auto-close banning has its own regex in
# ``commit_message.py``.
AUTOCLOSE_RE = re.compile(
    r"^\s*[-*]?\s*(fix(?:e[sd])?|resolve[sd]?):?\s+"
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
    """Return the tracking issue number from a ``Ref #N`` / ``Closes #N`` match.

    Recognizes a task linked by either sanctioned keyword. Raises ``ValueError``
    if multiple linkage lines are found.
    """
    matches = LINKAGE_RE.findall(text)
    if not matches:
        return None
    if len(matches) > 1:
        msg = f"multiple tracking issue references found ({len(matches)})"
        raise ValueError(msg)
    return int(matches[0][1])


def extract_tracking_ref(text: str) -> str | None:
    """Return the single linkage as ``"#N"`` or ``"owner/repo#N"`` (cross-repo).

    Like :func:`extract_tracking_issue` but preserves the optional ``owner/repo``
    so callers can identify a cross-repo linkage (e.g. a mistaken link to an epic
    in ``.github``). Matches ``Ref`` or ``Closes``. Raises ``ValueError`` if
    multiple linkage lines are found.
    """
    matches = LINKAGE_RE.findall(text)
    if not matches:
        return None
    if len(matches) > 1:
        msg = f"multiple tracking issue references found ({len(matches)})"
        raise ValueError(msg)
    repo_part, number = matches[0]
    return f"{repo_part}#{number}" if repo_part else f"#{number}"
