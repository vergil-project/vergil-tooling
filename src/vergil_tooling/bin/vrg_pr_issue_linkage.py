"""Check that a pull request body includes primary issue linkage.

Reads the GitHub event payload from ``GITHUB_EVENT_PATH`` and validates
that the PR body contains ``Ref`` followed by an issue reference.
Auto-close keywords (Fixes, Closes, Resolves and variants) are
rejected — issues must remain open until post-merge workflows succeed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib import epics
from vergil_tooling.lib.linkage import AUTOCLOSE_RE, LINKAGE_RE, extract_tracking_ref
from vergil_tooling.lib.output import emit_error, write_summary


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")

    if not event_path:
        emit_error("GITHUB_EVENT_PATH is not set.")
        return 2

    event_file = Path(event_path)
    if not event_file.is_file():
        emit_error(f"event payload not found at {event_path}")
        return 2

    with event_file.open(encoding="utf-8") as f:
        event = json.load(f)

    pr_body: str = event.get("pull_request", {}).get("body", "") or ""

    if not pr_body:
        msg = "pull request body is empty; issue linkage is required."
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    if AUTOCLOSE_RE.search(pr_body):
        msg = (
            "pull request body contains a GitHub auto-close keyword "
            "(close/fix/resolve). Use 'Ref #N' instead. "
            "Issues must remain open until post-merge workflows succeed."
        )
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    if not LINKAGE_RE.search(pr_body):
        msg = (
            "pull request body must include primary issue linkage "
            "(Ref #123). Cross-repo references (Ref owner/repo#123) are "
            "also accepted."
        )
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    try:
        ref = extract_tracking_ref(pr_body)
    except ValueError:
        msg = "pull request must link exactly one task (multiple Ref lines found)."
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    # Reject linking an epic: PRs link a task; epics are umbrellas closed by
    # rollup. Self-scoping — legacy issues are never epics, so they pass.
    repo_env = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        issue = epics.parse_issue_ref(ref or "", default_repo=repo_env)
    except ValueError:
        return 0
    if epics.is_epic(issue):
        msg = (
            "pull request links an epic; PRs link a task, not an epic "
            "(epics are closed by rollup when their tasks complete)."
        )
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
