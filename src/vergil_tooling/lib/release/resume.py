"""Discover the open tracking issue to resume (vrg-release --resume, #1612).

A fresh release creates the ``release: X.Y.Z`` tracking issue and a stage
checklist in its body. Resume finds that open issue, validates the checklist
against the running tooling's stage list, and reports the version and number so
the pipeline can re-enter with the existing artifacts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Sequence

_TITLE_RE = re.compile(r"^release: (\d+\.\d+\.\d+)$")


def _open_release_issues(repo: str) -> list[tuple[str, int, str]]:
    """Return ``(version, number, body)`` for each open ``release: X.Y.Z`` issue."""
    results = github.read_json(
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        "release: in:title",
        "--state",
        "open",
        "--json",
        "number,title,body",
    )
    issues: list[tuple[str, int, str]] = []
    if isinstance(results, list):
        for raw in results:
            if not isinstance(raw, dict):
                continue
            item = cast("dict[str, object]", raw)
            match = _TITLE_RE.match(str(item.get("title", "")))
            if match:
                issues.append((match.group(1), int(str(item["number"])), str(item.get("body", ""))))
    return issues


def find_resume_target(
    repo: str, stages: Sequence[str], *, version: str | None = None
) -> tuple[str, int]:
    """Return ``(version, issue_number)`` of the in-flight release to resume.

    Also validates the checklist version-skew. Raises ``ReleaseError`` when
    there is no in-flight release, when the choice is ambiguous (and *version*
    did not disambiguate), or when the checklist was written by a different
    tooling version.
    """
    issues = _open_release_issues(repo)
    if version is not None:
        issues = [issue for issue in issues if issue[0] == version]
    if not issues:
        msg = (
            f"No open release issue for {version}."
            if version
            else "No in-flight release to resume."
        )
        raise ReleaseError(phase="resume", command="gh issue list", message=msg)
    if len(issues) > 1:
        names = ", ".join(sorted(issue[0] for issue in issues))
        raise ReleaseError(
            phase="resume",
            command="gh issue list",
            message=(
                f"Multiple in-flight releases ({names}); name one with vrg-release --resume X.Y.Z."
            ),
        )
    found_version, number, body = issues[0]
    try:
        checklist.first_unchecked(body, stages)
    except checklist.ChecklistError as exc:
        raise ReleaseError(phase="resume", command="checklist", message=str(exc)) from exc
    return found_version, number
