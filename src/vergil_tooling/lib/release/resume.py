"""Discover the open tracking issue to resume (vrg-release --resume, #1612).

A fresh release creates the ``release: X.Y.Z`` tracking issue and a stage
checklist in its body. Resume finds that open issue, validates the checklist
against the running tooling's stage list, and reports the version and number so
the pipeline can re-enter with the existing artifacts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Sequence

_TITLE_RE = re.compile(r"^release: (\d+\.\d+\.\d+)$")
# Upper bound on the open issues the release search lists. Reaching it is a
# refusal: an in-flight release issue could lie beyond it (#4038).
_SEARCH_LIST_LIMIT = 100
_SEARCH_COMMAND = "gh issue list --search 'release: in:title' --state open"


def _open_release_issues(repo: str, *, phase: str = "resume") -> list[tuple[str, int, str]]:
    """Return ``(version, number, body)`` for each open ``release: X.Y.Z`` issue.

    Shared by ``--resume``'s lookup and a fresh start's one-release-at-a-time
    check (spec §4.7 of epic vergil-project/.github#1440); *phase* names the
    caller in a refusal. Raises :class:`ReleaseError` when the search fills the
    listing limit, since the list may then be truncated, and when the response
    is not a list of issue objects, which is never read as "no open release".
    """
    results = github.read_json(
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        "release: in:title",
        "--state",
        "open",
        "--limit",
        str(_SEARCH_LIST_LIMIT),
        "--json",
        "number,title,body",
    )
    if not isinstance(results, list):
        raise ReleaseError(
            phase=phase,
            command=_SEARCH_COMMAND,
            message=f"The release-issue search returned {type(results).__name__}, not a list.",
        )
    if len(results) >= _SEARCH_LIST_LIMIT:
        raise ReleaseError(
            phase=phase,
            command=_SEARCH_COMMAND,
            message=(
                f"The release-issue search matched {_SEARCH_LIST_LIMIT}+ open issues; "
                "refusing to judge the open releases from a possibly truncated list."
            ),
        )
    issues: list[tuple[str, int, str]] = []
    for raw in results:
        if not isinstance(raw, dict):
            raise ReleaseError(
                phase=phase,
                command=_SEARCH_COMMAND,
                message=f"The release-issue search returned a non-object item: {raw!r}.",
            )
        item = cast("dict[str, object]", raw)
        match = _TITLE_RE.match(str(item.get("title", "")))
        if match:
            issues.append((match.group(1), int(str(item["number"])), str(item.get("body", ""))))
    return issues


def _check_blockless_issue_resumable(version: str, number: int) -> None:
    """Refuse to resume an issue with no progress block unless its release branch exists.

    A tracking issue is created with its checklist (#4367), so an issue without
    one was opened by an older vrg-release that was interrupted in prepare, or
    by hand. Preflight creates the release branch only once a fresh start's
    checks have passed, so the branch's existence shows they did: resume then
    re-runs the pipeline, prepare adopting what was done, and prepare writes the
    checklist. With no branch, nothing shows the checks passed, and a resume,
    which skips them, must not start the release.
    """
    branch = f"release/{version}"
    if git.ref_exists(branch) or git.ref_exists(f"origin/{branch}"):
        print(
            f"Tracking issue #{number} has no vrg-release progress block (it was "
            f"interrupted before one was written); resuming on '{branch}'."
        )
        return
    raise ReleaseError(
        phase="resume",
        command="checklist",
        message=(
            f"Tracking issue #{number} has no vrg-release progress block, and no "
            f"release branch '{branch}' exists, so nothing shows that a fresh "
            "start's checks passed. Close the issue and re-run vrg-release."
        ),
    )


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
    if not checklist.has_block(body):
        _check_blockless_issue_resumable(found_version, number)
        return found_version, number
    try:
        checklist.first_unchecked(body, stages)
    except checklist.ChecklistError as exc:
        raise ReleaseError(phase="resume", command="checklist", message=str(exc)) from exc
    return found_version, number
