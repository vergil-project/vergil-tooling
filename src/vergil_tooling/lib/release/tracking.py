"""GitHub tracking issue management for release operations."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import github, retry, text_table, trust, trusted_authors
from vergil_tooling.lib.release import checklist, guard, state
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vergil_tooling.lib.pending_release import PendingChange, PendingRelease
    from vergil_tooling.lib.release.context import ReleaseContext
    from vergil_tooling.lib.release.state import IssueEntry, StateBlock

# GitHub's addComment API rejects any comment body longer than this many
# characters. Posting a larger body fails with
# "Body is too long (maximum is 65536 characters) (addComment)".
_MAX_COMMENT_CHARS = 65_536
# Stay comfortably under the hard limit so the truncation marker and any
# character-vs-byte counting differences cannot push us back over.
_COMMENT_BUDGET = 65_000
# The Contents section's pending-release table, and the short-SHA width of a
# commit with no PR in it.
_CONTENTS_COLUMNS = ("PR", "BRANCH", "TYPE", "TITLE")
_SHORT_SHA = 7
# What the Contents table writes in place of the characters of an HTML comment
# marker and of a code fence, in text others control (PR titles and branches).
_NEUTRAL = str.maketrans({"<": "\u2039", ">": "\u203a", "`": "'"})


def _truncate_for_comment(body: str, *, budget: int = _COMMENT_BUDGET) -> str:
    """Shrink *body* to fit GitHub's comment-size limit.

    Bodies within *budget* are returned unchanged. Larger bodies keep the
    head (the marker comment and the structured phase/command/error preamble)
    and the tail (where failure logs put the actual error), dropping the
    middle and replacing it with a marker noting how many characters were
    removed.
    """
    if len(body) <= budget:
        return body
    marker_template = "\n\n[... {dropped} characters truncated ...]\n\n"
    # The marker's length depends on the dropped count, which we don't know
    # until we know how much we keep. Reserve an upper bound: the dropped
    # count can never have more digits than the original length.
    marker_reserve = len(marker_template.format(dropped=len(body)))
    available = budget - marker_reserve
    head_chars = available // 2
    tail_chars = available - head_chars
    dropped = len(body) - head_chars - tail_chars
    marker = marker_template.format(dropped=dropped)
    return body[:head_chars] + marker + body[-tail_chars:]


def create_tracking_issue(ctx: ReleaseContext) -> None:
    """Create a release tracking issue, with its checklist, Contents and state, and populate ctx.

    The creation body carries the stage checklist (``ctx.progress_block``),
    the Contents section and the state block, never a later edit, so no
    interrupt can leave an open tracking issue that ``vrg-release --resume``
    refuses for want of one (#4367; spec §4.2 of epic vergil-project/.github#1440).
    """
    if not ctx.progress_block:
        raise ReleaseError(
            phase="prepare",
            command="create_tracking_issue",
            message=(
                "No stage checklist on the release context — preflight did not run; "
                "refusing to create a tracking issue that --resume could not adopt."
            ),
        )
    if ctx.cut is None or ctx.pending_release is None:
        raise ReleaseError(
            phase="prepare",
            command="create_tracking_issue",
            message=(
                "No release cut or contents on the release context — preflight did not "
                "read them; refusing to create a tracking issue without its state block."
            ),
        )
    superseded = ctx.superseded
    absorbed = () if superseded is None else tuple(i.ref for i in superseded.issues)
    initial = state.initial_state(
        version=ctx.version,
        cut=ctx.cut,
        issues=ctx.issues,
        without_deps=ctx.without_deps,
        version_bump=ctx.version_override,
        supersedes=None if superseded is None else superseded.version,
        absorbed=absorbed,
        superseded_main_commit=None if superseded is None else superseded.main_commit,
    )
    contents = render_contents(
        ctx.pending_release,
        ctx.issues,
        supersedes=None if superseded is None else superseded.version,
        absorbed=absorbed,
    )
    body = (
        f"## Release {ctx.version}\n\nRepo: {ctx.repo}\n\n{contents}\n\n"
        f"{ctx.progress_block}\n\n{state.render_state(initial)}\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        url = github.read_output(
            "issue",
            "create",
            "--repo",
            ctx.repo,
            "--title",
            f"release: {ctx.version}",
            "--body-file",
            tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        msg = f"Could not extract issue number from URL: {url}"
        raise ValueError(msg)
    ctx.issue_number = int(match.group(1))
    ctx.issue_url = url
    ctx.state = initial
    # Locked at birth, so only repository collaborators can comment (spec
    # §4.2). A lock that fails leaves an unlocked issue, which every later
    # read of the state locks before it trusts it (guard.ensure_locked).
    number = ctx.issue_number
    retry.retry_known_resource(
        lambda: github.run("issue", "lock", str(number), "--repo", ctx.repo),
        resource=f"tracking issue #{number}",
    )


def _neutral(text: str) -> str:
    """*text* with every character that could form a marker or close a fence replaced.

    PR titles and branch names are written by others and can change after a
    merge, so none may carry an HTML comment (a checklist or state marker) or
    a backtick run (closing the table's fence) into the tracking-issue body.
    """
    return text.translate(_NEUTRAL)


def _change_row(change: PendingChange) -> tuple[str, str, str, str]:
    """One Contents-table row; a commit with no PR shows its short SHA as its branch."""
    if change.pr_number is None:
        pr, branch = "PROBLEM", change.shas[0][:_SHORT_SHA]
    else:
        pr, branch = f"#{change.pr_number}", _neutral(change.branch or "-")
    return (pr, branch, change.type_label, text_table.truncate(_neutral(change.title)))


def render_contents(
    pending: PendingRelease,
    issues: Sequence[IssueEntry],
    *,
    supersedes: str | None = None,
    absorbed: Sequence[str] = (),
) -> str:
    """The tracking issue's ``## Contents`` section: the pending-release table and the issues.

    The table lists every change in ``main..<cut>``, as ``vrg-repo-status``
    does (a commit that reached the branch with no PR as ``PROBLEM``, with
    its short SHA); under it, each issue the release contains with its closing
    PRs (spec §4.1-§4.2 of epic vergil-project/.github#1440). A superseding
    release names the release it *supersedes*, and marks each issue it
    *absorbed* from it (spec §4.7).
    """
    lines = ["## Contents", ""]
    if pending.changes:
        rows = [_change_row(change) for change in pending.changes]
        lines += ["```text", text_table.render(_CONTENTS_COLUMNS, rows), "```"]
    else:
        lines.append("No changes pending release.")
    if supersedes is not None:
        lines += [
            "",
            "### Supersedes",
            "",
            f"Release {supersedes}, which cannot finish: its issues are absorbed into this "
            "release, and their records say it supersedes it.",
        ]
    lines += ["", "### Issues", ""]
    if issues:
        marked = set(absorbed)
        lines += [
            f"- {issue.ref} ({', '.join(f'#{pr}' for pr in issue.prs)})"
            + (f", absorbed from {supersedes}" if issue.ref in marked else "")
            for issue in issues
        ]
    else:
        lines.append("None: the release's PRs close no issues.")
    return "\n".join(lines)


def comment_phase_complete(ctx: ReleaseContext, phase: str, details: str) -> None:
    """Post a phase-completion comment on the tracking issue."""
    body = f"<!-- vrg-release:{phase}:complete -->\n\n**{phase}** complete.\n\n{details}"
    _comment(ctx, body)


def comment_phase_failed(ctx: ReleaseContext, phase: str, exc: ReleaseError) -> None:
    """Post a phase-failure comment on the tracking issue."""
    lines = [
        f"<!-- vrg-release:{phase}:failed -->",
        "",
        f"**{phase}** failed.",
        "",
        f"**Command:** `{exc.command}`",
        f"**Error:** {exc}",
    ]
    if exc.detail:
        lines.append(f"**Detail:** {exc.detail}")
    _comment(ctx, "\n".join(lines))


def close_tracking_issue(ctx: ReleaseContext, summary: str) -> None:
    """Post a summary comment and close the tracking issue."""
    _comment(ctx, summary)
    github.run(
        "issue",
        "close",
        str(ctx.issue_number),
        "--repo",
        ctx.repo,
    )


def read_issue_body(ctx: ReleaseContext) -> str:
    """Return the current body of the tracking issue.

    ctx.issue_number always comes from GitHub itself — gh issue create
    (prepare) or gh issue list (resume) — so a 404 right after it was
    reported is read lag and is retried under the known-resource budget (#3137).
    """
    return retry.retry_known_resource(
        lambda: github.read_output(
            "issue",
            "view",
            str(ctx.issue_number),
            "--repo",
            ctx.repo,
            "--json",
            "body",
            "--jq",
            ".body",
        ),
        resource=f"tracking issue #{ctx.issue_number}",
    )


def _write_issue_body(ctx: ReleaseContext, body: str) -> None:
    """Replace the tracking-issue body with *body*.

    gh's stdout, the tracking issue's URL, is not echoed: the body is
    rewritten after every recorded issue and every stage, and the URL said
    nothing about either (vergil-tooling#5221).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        github.run(
            "issue",
            "edit",
            str(ctx.issue_number),
            "--repo",
            ctx.repo,
            "--body-file",
            tmp_path,
            echo=False,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _read_title_and_body(ctx: ReleaseContext) -> tuple[str, str]:
    """The tracking issue's title and body, retried as :func:`read_issue_body` is."""
    issue = retry.retry_known_resource(
        lambda: github.read_json(
            "issue",
            "view",
            str(ctx.issue_number),
            "--repo",
            ctx.repo,
            "--json",
            "title,body",
        ),
        resource=f"tracking issue #{ctx.issue_number}",
    )
    if not isinstance(issue, dict):
        msg = f"gh issue view #{ctx.issue_number} returned {type(issue).__name__}, not an object"
        raise TypeError(msg)
    title, body = issue["title"], issue["body"]
    if not isinstance(title, str) or not isinstance(body, str):
        msg = f"gh issue view #{ctx.issue_number} returned a non-string title or body"
        raise TypeError(msg)
    return title, body


def _state_error(ctx: ReleaseContext, command: str, message: str) -> ReleaseError:
    return ReleaseError(
        phase="state",
        command=command,
        message=f"Tracking issue #{ctx.issue_number}'s release state block: {message}",
    )


def _trusted_authors() -> trusted_authors.TrustedAuthors:
    """The ``vrg-trusted-authors`` configuration; a failed load is an error, never empty."""
    try:
        return trusted_authors.load_trusted_authors()
    except (trusted_authors.TrustedAuthorsNotConfiguredError, ValueError) as exc:
        raise ReleaseError(
            phase="state",
            command="vrg-trusted-authors",
            message=f"Cannot check the identities on the tracking issue: {exc}",
        ) from exc


def _guard(ctx: ReleaseContext) -> None:
    """Lock the tracking issue if it is not, then apply the identity rule to it.

    Run after the body is read, so an edit made before the identities are
    read is in the edit history they are checked against. A breach raises
    :class:`guard.TrackingIssueBreach` naming the issue, before any write;
    a read the trust layer cannot complete raises :class:`ReleaseError`;
    each foreign trace is printed as a warning once per run (spec §4.2 of
    epic vergil-project/.github#1440).
    """
    number = cast("int", ctx.issue_number)
    authors = _trusted_authors()
    resource = f"tracking issue #{number}"
    # The lock read retries a 404 on a just-created issue; once it has read
    # the issue, the identity read below finds it too.
    retry.retry_known_resource(lambda: guard.ensure_locked(ctx.repo, number), resource=resource)
    try:
        warnings = guard.check_tracking_issue(ctx.repo, number, authors=authors)
    except trust.TrustReadError as exc:
        raise ReleaseError(
            phase="state",
            command="check_tracking_issue",
            message=(
                f"Cannot read who changed tracking issue #{number}, so its release state "
                "is not trusted."
            ),
            detail=str(exc),
        ) from exc
    for warning in warnings:
        if warning not in ctx.tracking_warnings:
            ctx.tracking_warnings.add(warning)
            print(f"warning: tracking issue #{number}: {warning}", file=sys.stderr)


def read_state(ctx: ReleaseContext) -> StateBlock:
    """The state block in the tracking-issue body, and set it on *ctx*.

    The state is read from the body only, never from comments, and trusted
    only once the issue is locked and passes the identity rule
    (:func:`_guard`). A missing or malformed block, or one naming a version
    other than the issue title's, is a :class:`ReleaseError` naming the
    block, never a fallback to defaults (spec §4.2 of epic
    vergil-project/.github#1440).
    """
    command = f"gh issue view {ctx.issue_number} --json title,body"
    title, body = _read_title_and_body(ctx)
    _guard(ctx)
    try:
        current = state.parse_state(body)
    except state.StateBlockError as exc:
        raise _state_error(ctx, command, str(exc)) from exc
    if title != f"release: {current.version}":
        raise _state_error(
            ctx,
            command,
            f"it names version {current.version!r}, but the issue is {title!r}",
        )
    ctx.state = current
    return current


def write_state(ctx: ReleaseContext, new: StateBlock) -> None:
    """Write *new* into the tracking-issue body by a read-modify-write, and set it on *ctx*.

    The read is guarded as :func:`read_state`'s is, so a foreign edit since
    the last read stops the write. Everything else in the body, the
    checklist and Contents included, is kept.
    """
    current = read_issue_body(ctx)
    _guard(ctx)
    try:
        body = state.upsert_state(current, new)
    except state.StateBlockError as exc:
        raise _state_error(ctx, f"gh issue edit {ctx.issue_number}", str(exc)) from exc
    _write_issue_body(ctx, body)
    ctx.state = new


def ensure_checklist(
    ctx: ReleaseContext, stages: Sequence[str], checked: Iterable[str] = ()
) -> None:
    """Embed the stage checklist in the tracking-issue body if not already there.

    *checked* pre-marks stages that have already completed (e.g. the read-only
    stages that ran before the issue existed). No-op if a block is present.
    """
    body = read_issue_body(ctx)
    if checklist.BEGIN in body:
        return
    _write_issue_body(ctx, checklist.upsert(body, stages, checked))


def tick_stage(ctx: ReleaseContext, stage: str) -> None:
    """Check *stage*'s box in the tracking-issue checklist."""
    _write_issue_body(ctx, checklist.tick(read_issue_body(ctx), stage))


def _comment(ctx: ReleaseContext, body: str) -> None:
    """Post a comment on the tracking issue.

    Bodies are truncated to GitHub's comment-size limit first, so an
    oversized payload (e.g. a multi-minute CD watch log captured into a
    failure detail) reports the failure instead of crashing the release.
    """
    body = _truncate_for_comment(body)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        github.run(
            "issue",
            "comment",
            str(ctx.issue_number),
            "--repo",
            ctx.repo,
            "--body-file",
            tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
