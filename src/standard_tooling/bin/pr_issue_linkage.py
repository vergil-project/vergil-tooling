"""Check that a pull request body includes primary issue linkage.

Reads the GitHub event payload from ``GITHUB_EVENT_PATH`` and validates
that the PR body contains ``Ref`` followed by an issue reference.
Auto-close keywords (Fixes, Closes, Resolves and variants) are
rejected — issues must remain open until post-merge workflows succeed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_LINKAGE_RE = re.compile(
    r"^\s*[-*]?\s*Ref:?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#[0-9]+",
    re.MULTILINE,
)

_AUTOCLOSE_RE = re.compile(
    r"^\s*[-*]?\s*(close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#[0-9]+",
    re.MULTILINE | re.IGNORECASE,
)


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")

    if not event_path:
        print("ERROR: GITHUB_EVENT_PATH is not set.", file=sys.stderr)
        return 2

    event_file = Path(event_path)
    if not event_file.is_file():
        print(f"ERROR: event payload not found at {event_path}", file=sys.stderr)
        return 2

    with event_file.open(encoding="utf-8") as f:
        event = json.load(f)

    pr_body: str = event.get("pull_request", {}).get("body", "") or ""

    if not pr_body:
        print(
            "ERROR: pull request body is empty; issue linkage is required.",
            file=sys.stderr,
        )
        return 1

    if _AUTOCLOSE_RE.search(pr_body):
        print(
            "ERROR: pull request body contains a GitHub auto-close keyword "
            "(close/fix/resolve). Use 'Ref #N' instead. "
            "Issues must remain open until post-merge workflows succeed.",
            file=sys.stderr,
        )
        return 1

    if not _LINKAGE_RE.search(pr_body):
        print(
            "ERROR: pull request body must include primary issue linkage "
            "(Ref #123). Cross-repo references (Ref owner/repo#123) are "
            "also accepted.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
