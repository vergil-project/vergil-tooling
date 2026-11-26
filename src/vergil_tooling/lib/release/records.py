"""The ``release-records`` stage: a ``Released:`` record on every issue the release contains.

Spec §4.9 of epic vergil-project/.github#1440. The stage posts one comment on
each issue in the state block's issue list that has no counting record yet::

    <!-- vrg-release:released:<owner>/<repo>:v2.1.340 -->
    Released: vergil-project/vergil-tooling v2.1.340 on 2026-11-20

    - Release: https://github.com/vergil-project/vergil-tooling/releases/tag/v2.1.340
    - Closed by: #4840 (merge commit 1a2b3c4)
    - Install: ran — installed v2.1.340 (commit fab2581)

The Install line names the release and commit the install reported
installing (``install_version`` and ``install_commit`` in the state block);
an install that reported neither reads ``ran (`[publish].install`)``, and a
repo with none configured ``none configured``.

A record counts, here and as evidence anywhere, only when its comment's
author and last editor both hold the ``human`` role (:data:`RECORD_ROLES`,
``editors="last"``), as the trust layer judges it
(:func:`~vergil_tooling.lib.trust.read_issue`, epic
vergil-project/.github#1467): a ``human`` login of ``vrg-trusted-authors``
whose account is a ``User``. Any other comment is never read for a record:
it is reported on stderr, metadata only, and ignored.

The stage is idempotent. Before posting, it reads the issue's comments, every
page, for a counting record; one found is recorded as posted in the state
block, as is each record it posts, one issue at a time. A run interrupted
partway therefore posts only the rest, and no issue gets two. A post is made
exactly once, never retried: a failure that may hide a landed comment fails
the issue, and the next run's read finds the comment if it landed.

Every read fails loudly (spec §6): a failed GraphQL page, or a response that
is not what was asked for, raises, never passes as "no record". The stage
fails if any issue's record could not be posted or confirmed, naming each,
and ``close-tracking`` does not run; ``vrg-release --resume`` posts the rest.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from vergil_tooling.lib import github, trust, trusted_authors
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import write_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.release.context import ReleaseContext
    from vergil_tooling.lib.release.state import IssueEntry, StateBlock
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

PHASE = "release-records"
# The roles a record's author and last editor must hold: the human only.
RECORD_ROLES: frozenset[trust.Role] = frozenset({trust.Role.HUMAN})
# The stage whose done record says the install ran or had nothing to run.
_INSTALL = "install"
_SHORT_SHA = 7
# The field the trust layer gives an issue body's Skipped record.
_BODY_FIELD = "body"
_REF_RE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<number>[1-9][0-9]*)$")

_MERGE_COMMIT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { mergeCommit { oid } }
  }
}
"""

_RELEASE_DATE_QUERY = """
query($owner: String!, $name: String!, $tag: String!) {
  repository(owner: $owner, name: $name) {
    release(tagName: $tag) { publishedAt }
  }
}
"""


class RecordReadError(github.GitHubAPIError):
    """A read a record depends on returned something other than what was asked for."""

    def __init__(self, what: str, detail: str) -> None:
        super().__init__(1, ("gh", "api", "graphql"), stderr=f"cannot read {what}: {detail}")


def marker(repo: str, version: str) -> str:
    """The record marker for *repo*'s release *version* (``X.Y.Z``), alone on its line."""
    return f"<!-- vrg-release:released:{repo}:v{version} -->"


def record_body(  # noqa: PLR0913 - the record's fields, each named
    *,
    repo: str,
    version: str,
    date: str,
    release_url: str,
    prs: Sequence[tuple[int, str]],
    install: Literal["ran", "none configured"],
    supersedes: str | None,
    installed: tuple[str, str] | None = None,
) -> str:
    """The spec §4.9 record of *repo*'s release *version*, published on *date*.

    *prs* are the issue's closing PRs, each ``(number, merge commit)``, all
    *repo*'s, as every issue in the list is (a closing reference to another
    repo's issue is not listed). *supersedes* is the ``X.Y.Z`` a
    superseding release absorbed the issue from, or ``None``. *installed*
    is the ``(X.Y.Z, commit)`` an install that ran reported installing, or
    ``None`` when it reported nothing; the Install line then keeps its
    plain ``ran`` wording.
    """
    closed_by = ", ".join(f"#{number} (merge commit {sha[:_SHORT_SHA]})" for number, sha in prs)
    install_line = _install_line(install, installed)
    lines = [
        marker(repo, version),
        f"Released: {repo} v{version} on {date}",
        "",
        f"- Release: {release_url}",
        f"- Closed by: {closed_by}",
        f"- Install: {install_line}",
    ]
    if supersedes is not None:
        lines.append(f"- Supersedes: v{supersedes.removeprefix('v')}")
    return "\n".join(lines)


def _install_line(
    install: Literal["ran", "none configured"], installed: tuple[str, str] | None
) -> str:
    """The record's Install line after ``- Install: ``."""
    if install != "ran":
        return "none configured"
    if installed is None:
        return "ran (`[publish].install`)"
    version, commit = installed
    return f"ran — installed v{version} (commit {commit[:_SHORT_SHA]})"


def counting_record(
    comments: Sequence[trust.TrustedComment], repo: str, version: str
) -> trust.TrustedComment | None:
    """The first of *comments* holding the record of *repo* *version*, or ``None``.

    *comments* are the ones :func:`record_comments` trusts, so each already
    passes the record rule. A comment holds the record when a line of it,
    stripped, is the :func:`marker`.
    """
    expected = marker(repo, version)
    for comment in comments:
        if expected in (line.strip() for line in comment.body.splitlines()):
            return comment
    return None


def _obj(value: object, what: str, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecordReadError(where, f"{what} is {value!r}")
    return cast("dict[str, Any]", value)


def _split_ref(ref: str) -> tuple[str, int]:
    match = _REF_RE.match(ref)
    if match is None:
        msg = f"issue ref {ref!r} is not <owner>/<repo>#<N>"
        raise ValueError(msg)
    return match.group("repo"), int(match.group("number"))


def _issue_url(ref: str) -> str:
    """The web URL of issue *ref* (``<owner>/<repo>#<N>``)."""
    repo, number = _split_ref(ref)
    return f"https://github.com/{repo}/issues/{number}"


def record_comments(ref: str, *, authors: TrustedAuthors) -> tuple[trust.TrustedComment, ...]:
    """The comments on issue *ref* (``<owner>/<repo>#<N>``) that may hold a record.

    Read through :func:`~vergil_tooling.lib.trust.read_issue` with the record
    rule, :data:`RECORD_ROLES` for the author and the last editor
    (``editors="last"``), every page. Every other comment is reported on
    stderr, metadata only, and never read for a record; the issue's own
    body is not this reader's concern. A read that cannot complete raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`.
    """
    repo, number = _split_ref(ref)
    content = trust.read_issue(repo, number, roles=RECORD_ROLES, editors="last", authors=authors)
    trust.report_skipped(
        [item for item in content.skipped if item.field != _BODY_FIELD], stream=sys.stderr
    )
    return content.comments


def _merge_commit(repo: str, number: int) -> str:
    """The commit *repo*'s PR *number* merged as; a PR with none raises."""
    owner, name = repo.split("/", 1)
    where = f"the merge commit of {repo}#{number}"
    data = github.graphql(_MERGE_COMMIT_QUERY, owner=owner, name=name, number=number)
    repository = _obj(data.get("repository"), "repository", where)
    pr = _obj(repository.get("pullRequest"), "pullRequest", where)
    commit = _obj(pr.get("mergeCommit"), "mergeCommit", where)
    oid = commit.get("oid")
    if not isinstance(oid, str) or not oid:
        raise RecordReadError(where, f"mergeCommit has no oid: {commit!r}")
    return oid


def _release_date(repo: str, tag: str) -> str:
    """The ``YYYY-MM-DD`` (UTC) the GitHub Release of *tag* was published."""
    owner, name = repo.split("/", 1)
    where = f"the GitHub Release of {repo} {tag}"
    data = github.graphql(_RELEASE_DATE_QUERY, owner=owner, name=name, tag=tag)
    repository = _obj(data.get("repository"), "repository", where)
    release = _obj(repository.get("release"), "release", where)
    published = release.get("publishedAt")
    if not isinstance(published, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", published):
        raise RecordReadError(where, f"release has no publishedAt: {release!r}")
    return published[:10]


def _authors() -> TrustedAuthors:
    """The ``vrg-trusted-authors`` configuration; a failed load is an error, never empty."""
    try:
        return trusted_authors.load_trusted_authors()
    except (trusted_authors.TrustedAuthorsNotConfiguredError, ValueError) as exc:
        raise ReleaseError(
            phase=PHASE,
            command="vrg-trusted-authors",
            message=f"Cannot tell which release records count: {exc}",
        ) from exc


def _refuse(message: str, command: str = PHASE) -> ReleaseError:
    return ReleaseError(phase=PHASE, command=command, message=message)


@dataclass(frozen=True)
class _Release:
    """What every record of this release says: the version, date, URL and install line."""

    repo: str
    version: str
    date: str
    release_url: str
    install: Literal["ran", "none configured"]
    # What the install reported installing, (X.Y.Z, commit), or None.
    installed: tuple[str, str] | None


def _release(ctx: ReleaseContext, current: StateBlock) -> _Release:
    """The release every record describes, as *current* and *ctx* record it.

    Refuses what a record could not honestly attest: no tag or GitHub
    Release, or an install *current* does not record done.
    """
    if ctx.tag is None or ctx.release_url is None:
        raise _refuse(
            f"no tag or GitHub Release is recorded for v{ctx.version}, so a record "
            "could not name the release."
        )
    if _INSTALL not in current.done:
        raise _refuse(
            "the state block does not record install done, so a record could not say "
            f"whether it ran. Once it succeeds, `vrg-release --resume {ctx.version}` "
            "posts the records."
        )
    return _Release(
        repo=ctx.repo,
        version=ctx.version,
        date=_release_date(ctx.repo, ctx.tag),
        release_url=ctx.release_url,
        install="ran" if current.install_done else "none configured",
        installed=(
            (current.install_version, current.install_commit)
            if current.install_version is not None and current.install_commit is not None
            else None
        ),
    )


def _post(ref: str, body: str) -> None:
    """Post *body* on issue *ref* exactly once: a failure may hide a landed comment."""
    repo, number = _split_ref(ref)
    github.post_json_once(f"repos/{repo}/issues/{number}/comments", {"body": body})


def _record_issue(
    issue: IssueEntry,
    release: _Release,
    authors: TrustedAuthors,
    merge_commits: dict[int, str],
    supersedes: str | None,
) -> bool:
    """Confirm or post *issue*'s record; True when it was posted by this call.

    *supersedes* is the release the issue was absorbed from, or None. Each
    comment that is not read for a record is reported on stderr
    (:func:`record_comments`).
    """
    found = counting_record(
        record_comments(issue.ref, authors=authors), release.repo, release.version
    )
    if found is not None:
        return False
    for pr in issue.prs:
        if pr not in merge_commits:
            merge_commits[pr] = _merge_commit(release.repo, pr)
    _post(
        issue.ref,
        record_body(
            repo=release.repo,
            version=release.version,
            date=release.date,
            release_url=release.release_url,
            prs=[(pr, merge_commits[pr]) for pr in issue.prs],
            install=release.install,
            supersedes=supersedes,
            installed=release.installed,
        ),
    )
    return True


def post_records(ctx: ReleaseContext) -> None:
    """Post a ``Released:`` record on every issue in the state block that has none yet.

    An issue the state block records as posted is skipped. Every other one
    is read for a counting record (:func:`counting_record`), and given one
    when it has none; either way it is then recorded as posted in the state
    block before the next issue, so a resume never posts a second, and
    named on one line, ``posted: <issue URL>`` or ``found existing: <issue
    URL>`` (vergil-tooling#5221). An issue whose read or post fails is
    named, and the stage carries on to the rest, then fails naming each.
    Writing the state block is never tolerated: a failed write stops the
    stage at once.
    """
    ctx.records_new = ctx.records_found = 0
    current = ctx.state
    if current is None:
        raise _refuse("the run holds no release state block, so its issue list is unknown.")
    pending = [issue for issue in current.issues if issue.ref not in current.records_posted]
    if not pending:
        return
    release = _release(ctx, current)
    authors = _authors()
    merge_commits: dict[int, str] = {}
    failures: list[str] = []
    for issue in pending:
        try:
            # An issue absorbed from a superseded release says so (spec §4.7).
            absorbed = current.supersedes if issue.ref in current.absorbed else None
            new = _record_issue(issue, release, authors, merge_commits, absorbed)
        except (
            subprocess.CalledProcessError,
            RuntimeError,
            ValueError,
            trust.TrustReadError,
        ) as exc:
            failures.append(f"{issue.ref}: {exc}")
            continue
        current = replace(current, records_posted=(*current.records_posted, issue.ref))
        write_state(ctx, current)
        print(f"{'posted' if new else 'found existing'}: {_issue_url(issue.ref)}")
        if new:
            ctx.records_new += 1
        else:
            ctx.records_found += 1
    if failures:
        raise _refuse(
            f"{len(failures)} release record(s) could not be posted or confirmed: "
            + "; ".join(failures)
            + f". Once the cause is fixed, `vrg-release --resume {ctx.version}` posts the rest.",
            command="gh api repos/<repo>/issues/<N>/comments",
        )
