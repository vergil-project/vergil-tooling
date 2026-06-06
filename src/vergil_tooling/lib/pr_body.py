"""Standards-compliant PR body construction.

Extracted from ``vrg_submit_pr`` so every tool that writes a PR body
(``vrg-submit-pr`` at creation, ``vrg-pr-fix-body`` on repair) builds
it through the same template and linkage-validated fields — a body can
only ever be produced from compliant inputs (#1459).
"""

from __future__ import annotations

import re

_ISSUE_PLAIN_RE = re.compile(r"^[1-9]\d*$")
_ISSUE_CROSS_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9]\d*$")


def resolve_issue_ref(issue: str) -> str:
    """Validate and normalize the issue reference."""
    if _ISSUE_PLAIN_RE.match(issue):
        return f"#{issue}"
    if _ISSUE_CROSS_RE.match(issue):
        return issue
    msg = f"--issue must be a number (42) or cross-repo ref (owner/repo#42), got '{issue}'."
    raise SystemExit(msg)


def build_pr_body(*, summary: str, linkage: str, issue_ref: str, notes: str) -> str:
    """Render the canonical PR body from validated fields."""
    notes_section = notes or "-"
    return (
        f"# Pull Request\n\n"
        f"## Summary\n\n- {summary}\n\n"
        f"## Issue Linkage\n\n- {linkage} {issue_ref}\n\n"
        f"## Notes\n\n- {notes_section}"
    )
