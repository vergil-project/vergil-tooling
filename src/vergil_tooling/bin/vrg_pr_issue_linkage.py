"""Check that a pull request body includes primary issue linkage.

Reads the GitHub event payload from ``GITHUB_EVENT_PATH`` and validates
that the PR body contains ``Ref`` or ``Closes`` followed by an issue
reference. ``Closes`` is the sanctioned auto-close keyword (a task is one
PR, so "in develop = done"); ``Fixes``/``Resolves`` and variants remain
rejected so there is exactly one close keyword. The epic-vs-task policy
(which keyword is correct for a given issue) lives in ``vrg-submit-pr``;
this gate is a dependency-free syntax/uniqueness check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib.linkage import AUTOCLOSE_RE, LINKAGE_RE, extract_tracking_ref
from vergil_tooling.lib.output import emit_error, write_summary


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-pr-issue-linkage",
        description=(
            "Validate that a pull request body includes primary issue linkage "
            "(Ref #N or Closes #N) and exactly one task reference. A "
            "dependency-free CI gate: pure regex over the PR body, no GitHub "
            "API calls."
        ),
        epilog=(
            "Reads the pull request from the GitHub event payload at "
            "$GITHUB_EVENT_PATH. Exit codes: 0 ok, 1 compliance failure, "
            "2 configuration error (event path unset or missing)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
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
            "pull request body contains a banned GitHub auto-close keyword "
            "(fix/resolve). Use 'Ref #N' to reference, or 'Closes #N' to "
            "close the task on merge."
        )
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    if not LINKAGE_RE.search(pr_body):
        msg = (
            "pull request body must include primary issue linkage "
            "(Ref #123 or Closes #123). Cross-repo references "
            "(Ref owner/repo#123) are also accepted."
        )
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    # Enforce a single task ref (pure-regex, no API). The epic-vs-task check
    # needs authenticated cross-repo gh and lives in vrg-submit-pr; this CI gate
    # stays dependency-free so it can run without a gh token.
    try:
        extract_tracking_ref(pr_body)
    except ValueError:
        msg = "pull request must link exactly one task (multiple Ref lines found)."
        emit_error(msg)
        write_summary(f"## PR Body Compliance Failed\n\n{msg}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
