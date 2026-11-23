"""Report an unattended job's result on its ops issue (epic vergil-project/.github#1494).

One call at the end of every unattended job's run (spec §5.2). A job is named
by its **report key**: the repo it ran in and a stable job name, never the
workflow file. Its ops issue is the one open issue in that repo titled
:func:`report_title`, labelled :data:`LABEL` and opened by the org's
automation App, under the org's operations epic
(:func:`~vergil_tooling.lib.epics.ensure_operations_epic`).

- **failure / cancelled**: no open issue → create one, naming the run, the
  step, the date and the previous occurrence; an open issue → a comment with
  the same facts.
- **success**: a ``recovered`` comment on an open issue whose latest trusted
  report is a failure or a cancel; otherwise nothing.
- **no result** (every GitHub job of the named job skipped): nothing at all.

**Nothing here closes an issue** (spec §2.3): a recovered issue stays open
for the human, or an agent working the queue, to close.

**Every read goes through the trust layer** (:mod:`vergil_tooling.lib.trust`,
epic vergil-project/.github#1467). An issue counts only when its author, its
body's editors and everyone who renamed it hold :attr:`~trust.Role.AUTOMATION`,
and a report comment counts only when the App wrote it: an outsider's issue
with the same title and label, or an outsider's comment carrying a marker,
is never acted on. The previous-occurrence lookup follows the same rule.

**Public text only:** a body or comment holds a marker line and the job, run
link, step, date, other open issues and previous occurrence; never log text
or environment values (spec §2.3).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import TYPE_CHECKING, Literal, cast, get_args

from vergil_tooling.lib import epics, github, retry, trust
from vergil_tooling.lib.labels import ensure_labels
from vergil_tooling.lib.markdown_escape import escape_inline
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.shell_ref import quoted_slug

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

log = logging.getLogger(__name__)

Result = Literal["success", "failure", "cancelled"]
Outcome = Literal["created", "commented", "recovered", "nothing"]
State = Literal["failing", "recovered"]

#: The GitHub job results a named job's result combines (``needs.<job>.result``).
JOB_RESULTS: tuple[str, ...] = (*get_args(Result), "skipped")

#: The label of an ops issue the reporter files; it never matches one filed by hand.
LABEL = "ops-report"

#: The first line of every report the reporter writes, body or comment.
MARKER_FAILURE = "ops-report: failure"
MARKER_CANCELLED = "ops-report: cancelled"
MARKER_RECOVERED = "ops-report: recovered"
_MARKERS: dict[Result, str] = {
    "failure": MARKER_FAILURE,
    "cancelled": MARKER_CANCELLED,
    "success": MARKER_RECOVERED,
}
_STATE_OF_MARKER: dict[str, State] = {
    MARKER_FAILURE: "failing",
    MARKER_CANCELLED: "failing",
    MARKER_RECOVERED: "recovered",
}

#: How long a create waits for the title lookup to list the new issue.
VISIBILITY_BUDGET_SECS = epics.STANDING_EPIC_VISIBILITY_BUDGET_SECS

# A job or bare repo name goes into a title and a search phrase verbatim, so
# each is held to GitHub's repo-name characters (a repo may be ``.github``).
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_JOB_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_AUTOMATION = frozenset({trust.Role.AUTOMATION})


class OpsReportError(RuntimeError):
    """A report could not be completed; the message names any issue it created."""


def combine_results(results: Sequence[str]) -> Result | None:
    """The one result of a named job from its GitHub jobs' results (spec §5.3).

    Any ``failure`` → failure; else any ``cancelled`` → cancelled; else at
    least one ``success`` → success; all ``skipped`` → ``None`` (no report).
    An empty sequence, or a value that is not a GitHub job result, raises
    :class:`ValueError`.
    """
    if not results:
        raise ValueError("no job results to combine")
    unknown = sorted({r for r in results if r not in JOB_RESULTS})
    if unknown:
        raise ValueError(
            f"unknown job result(s) {', '.join(map(repr, unknown))};"
            f" expected {', '.join(JOB_RESULTS)}"
        )
    for result in ("failure", "cancelled", "success"):
        if result in results:
            return cast("Result", result)
    return None


def report_title(repo: str, job: str) -> str:
    """The ops issue title of *job* in *repo* (the bare repo name)."""
    if not _REPO_NAME_RE.fullmatch(repo):
        raise ValueError(f"invalid repo name {repo!r}: a bare repo name, without its owner")
    if not _JOB_NAME_RE.fullmatch(job):
        raise ValueError(
            f"invalid job name {job!r}: use letters, digits, '.', '_' and '-',"
            " starting with a letter or digit"
        )
    return f"ops: {repo} / {job} failing"


def _split(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"repo must be OWNER/NAME, got {repo!r}")
    return owner, name


def _reports(
    repo: str, job: str, state: Literal["open", "closed"], authors: TrustedAuthors | None
) -> list[int]:
    """App-opened *state* issues in *repo* with *job*'s exact title and the label, ascending."""
    _, name = _split(repo)
    title = report_title(name, job)
    hits, skipped = trust.search_items(
        f'repo:{repo} is:{state} label:{LABEL} in:title "{title}"',
        kind="issue",
        roles=_AUTOMATION,
        authors=authors,
    )
    trust.report_skipped(skipped, stream=sys.stderr)
    return sorted(
        hit.number
        for hit in hits
        if hit.state == state.upper()
        and hit.title == title
        and LABEL in hit.labels
        and hit.body_role is trust.Role.AUTOMATION
    )


def find_open_reports(repo: str, job: str, *, authors: TrustedAuthors | None = None) -> list[int]:
    """The open ops issues of *job* in *repo* (``owner/name``), oldest first.

    Only issues the org's automation App opened, with the exact title and the
    :data:`LABEL` label, whose body and title no one else changed. A closed
    issue is never matched.
    """
    return _reports(repo, job, "open", authors)


def previous_occurrence(
    repo: str, job: str, *, authors: TrustedAuthors | None = None
) -> int | None:
    """The most recent closed ops issue of *job* in *repo*, by the same rule, else ``None``."""
    closed = _reports(repo, job, "closed", authors)
    return closed[-1] if closed else None


def latest_state(content: trust.IssueContent) -> State:
    """Whether an ops issue's latest trusted report is a failure or a recovery.

    Reads the body and the comments the automation App wrote, in order; the
    last whose first line is a marker decides. Untrusted text never counts. An
    issue with no trusted marker (its body was edited by someone else) reads
    as failing, so the next green run records the recovery.
    """
    pieces: list[str] = []
    if content.body is not None and content.body_role is trust.Role.AUTOMATION:
        pieces.append(content.body)
    pieces += [c.body for c in content.comments if c.role is trust.Role.AUTOMATION]
    state: State = "failing"
    for text in pieces:
        first = text.strip().splitlines()[0].strip() if text.strip() else ""
        if first in _STATE_OF_MARKER:
            state = _STATE_OF_MARKER[first]
    return state


def _check_run_url(run_url: str) -> str:
    """*run_url*, refused unless it is one ``https://`` URL with no whitespace."""
    if not run_url.startswith("https://") or any(c.isspace() for c in run_url):
        raise ValueError(f"the run URL must be one https:// URL, got {run_url!r}")
    return run_url


def _check_step(step: str) -> str:
    """*step*, refused unless it is one non-empty line."""
    if not step.strip() or "\n" in step or "\r" in step:
        raise ValueError(f"the step name must be one non-empty line, got {step!r}")
    return step.strip()


def _text(
    result: Result,
    *,
    job: str,
    run_url: str,
    step: str | None,
    today: date,
    others: Sequence[int] = (),
    previous: int | None = None,
) -> str:
    """A report's text: its marker line, then the public facts only."""
    lines = [_MARKERS[result], ""]
    if result == "success":
        lines.append(f"Recovered on {today.isoformat()}, {run_url}.")
        lines.append("")
        lines.append(f"- job: `{job}`")
    else:
        lines.append(f"- job: `{job}`")
        lines.append(f"- run: {run_url}")
        lines.append(f"- step: {escape_inline(step) if step else 'not named'}")
        lines.append(f"- date: {today.isoformat()}")
    if others:
        lines.append(
            f"- also open with this title: {', '.join(f'#{n}' for n in others)}"
            " (none is closed automatically)"
        )
    if previous is not None:
        lines.append(f"- previous occurrence: #{previous}")
    return "\n".join(lines) + "\n"


def _ensure_linked(issue: epics.IssueRef, epic: epics.IssueRef) -> None:
    """Link *issue* under *epic* when its native parent is missing or another issue."""
    parent = epics.native_parent_of(issue)
    if parent == epic:
        return
    try:
        if parent is None:
            epics.add_child(epic, issue)
        else:
            epics.reparent_child(epic, issue)
    except Exception as exc:
        raise OpsReportError(
            f"{issue.slug} is not linked under the operations epic {epic.slug}"
            f" (its parent: {parent.slug if parent else 'none'}) and linking it failed:"
            f" {error_text(exc)}. Link it with: vrg-epic-move --task {quoted_slug(issue)}"
            f" --epic {quoted_slug(epic)}"
        ) from exc


def _comment(repo: str, number: int, body: str) -> None:
    github.run("issue", "comment", str(number), "--repo", repo, "--body", body)


def _await_listed(
    repo: str,
    job: str,
    created: int,
    *,
    authors: TrustedAuthors | None,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Block until :func:`find_open_reports` lists *created* (``epics._await_listed``'s pattern).

    The search behind the lookup is eventually consistent; a run right behind
    this one must find the new issue rather than open a second.
    """

    def probe() -> list[int]:
        try:
            return find_open_reports(repo, job, authors=authors)
        except Exception as exc:
            raise OpsReportError(
                f"created {repo}#{created} but checking that it is listed failed:"
                f" {error_text(exc)}; a later report could open a duplicate"
            ) from exc

    def on_wait(_seen: list[int], delay: float) -> None:
        log.warning("%s#%d not listed yet, rechecking in %.1fs", repo, created, delay)

    listed, seen = retry.poll_until(
        probe,
        lambda found: created in found,
        budget=VISIBILITY_BUDGET_SECS,
        sleep=sleep,
        clock=clock,
        on_wait=on_wait,
    )
    if not listed:
        raise OpsReportError(
            f"created {repo}#{created} but it is still not listed after"
            f" {VISIBILITY_BUDGET_SECS:g}s (saw {seen or 'none'}); a later report"
            " could open a duplicate"
        )


def report(
    repo: str,
    job: str,
    result: Result | None,
    *,
    run_url: str,
    step: str | None,
    today: date,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> Outcome:
    """Report *job*'s *result* in *repo* (``owner/name``) on its ops issue.

    ``None`` (every GitHub job skipped) reads and writes nothing. Any failed
    read or write raises; nothing is ever closed. Two open ops issues for the
    key (a race) get the report on the oldest, naming the others. Every issue
    found or created is linked under the org's operations epic if it is not
    already. *sleep* and *clock* are the create's visibility wait's, for tests.
    """
    if result is None:
        return "nothing"
    if result not in _MARKERS:
        raise ValueError(f"unknown result {result!r}")
    _, name = _split(repo)
    title = report_title(name, job)
    run_url = _check_run_url(run_url)
    if step is not None:
        step = _check_step(step)
    authors = trust.load_trusted_authors()
    found = find_open_reports(repo, job, authors=authors)
    if not found:
        if result == "success":
            return "nothing"
        return _create(
            repo,
            job,
            title,
            result,
            run_url=run_url,
            step=step,
            today=today,
            authors=authors,
            sleep=sleep or time.sleep,
            clock=clock or time.monotonic,
        )
    oldest, others = found[0], found[1:]
    owner, _ = _split(repo)
    _ensure_linked(epics.IssueRef(owner, name, oldest), epics.ensure_operations_epic(repo))
    if result == "success":
        content = trust.read_issue(repo, oldest, roles=_AUTOMATION, authors=authors)
        trust.report_skipped(content.skipped, stream=sys.stderr)
        if latest_state(content) == "recovered":
            return "nothing"
    text = _text(result, job=job, run_url=run_url, step=step, today=today, others=others)
    _comment(repo, oldest, text)
    return "recovered" if result == "success" else "commented"


def _create(
    repo: str,
    job: str,
    title: str,
    result: Result,
    *,
    run_url: str,
    step: str | None,
    today: date,
    authors: TrustedAuthors,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Outcome:
    """Open *job*'s ops issue under the operations epic and wait until it is listed."""
    epic = epics.ensure_operations_epic(repo)
    previous = previous_occurrence(repo, job, authors=authors)
    ensure_labels(repo, [LABEL])
    body = _text(result, job=job, run_url=run_url, step=step, today=today, previous=previous)
    url = github.create_issue(repo=repo, title=title, body=body, labels=[LABEL])
    try:
        number = int(url.strip().rstrip("/").rsplit("/", 1)[-1])
    except ValueError as exc:
        raise OpsReportError(f"created an issue but cannot read its number from {url!r}") from exc
    owner, name = _split(repo)
    _ensure_linked(epics.IssueRef(owner, name, number), epic)
    _await_listed(repo, job, number, authors=authors, sleep=sleep, clock=clock)
    return "created"
