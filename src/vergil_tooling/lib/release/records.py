"""The ``release-records`` stage: a ``Released:`` record on every issue the release contains.

Spec §4.9 of epic vergil-project/.github#1440. The stage posts one comment on
each issue in the state block's issue list that has no counting record yet::

    <!-- vrg-release:released:<owner>/<repo>:v2.1.340 -->
    Released: vergil-project/vergil-tooling v2.1.340 on 2026-11-20

    - Release: https://github.com/vergil-project/vergil-tooling/releases/tag/v2.1.340
    - Closed by: #4840 (merge commit 1a2b3c4)
    - Install: ran (`[publish].install`)

A record counts, here and as evidence anywhere, only when its comment's
author and last editor are both ``human`` logins of ``vrg-trusted-authors``.
A record marker on any other identity's comment never counts: it is named as
a warning and ignored.

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

from vergil_tooling.lib import github, trusted_authors
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import write_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.release.context import ReleaseContext
    from vergil_tooling.lib.release.state import IssueEntry, StateBlock

PHASE = "release-records"
# The stage whose done record says the install ran or had nothing to run.
_INSTALL = "install"
# GraphQL caps a connection page at 100 nodes; the read follows every page.
_PAGE = 100
_SHORT_SHA = 7
# The login a deleted account's author or editor reads as.
_GHOST = "ghost"
_REF_RE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<number>[1-9][0-9]*)$")

_COMMENTS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      comments(first: {_PAGE}, after: $after) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ author {{ login }} editor {{ login }} body }}
      }}
    }}
  }}
}}
"""

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


@dataclass(frozen=True)
class Comment:
    """One issue comment: its author, its last editor (``None`` if never edited) and its body.

    Logins are bare (a leading ``app/`` stripped); a deleted account is ``ghost``.
    """

    author: str
    editor: str | None
    body: str


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
) -> str:
    """The spec §4.9 record of *repo*'s release *version*, published on *date*.

    *prs* are the issue's closing PRs, each ``(number, merge commit)``, all
    *repo*'s, as every issue in the list is (a closing reference to another
    repo's issue is not listed). *supersedes* is the ``X.Y.Z`` a
    superseding release absorbed the issue from, or ``None``.
    """
    closed_by = ", ".join(f"#{number} (merge commit {sha[:_SHORT_SHA]})" for number, sha in prs)
    install_line = "ran (`[publish].install`)" if install == "ran" else "none configured"
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


def counting_record(
    comments: Sequence[Comment], repo: str, version: str, humans: Sequence[str]
) -> tuple[Comment | None, list[str]]:
    """The first of *comments* holding the record of *repo* *version* that counts, and warnings.

    A comment holds the record when a line of it, stripped, is the
    :func:`marker`. It counts only when its author and, if it was edited,
    its last editor are in *humans*. Each holding comment that does not
    count is a warning naming the identity, and is otherwise ignored.
    """
    expected = marker(repo, version)
    humans_bare = {trusted_authors.bare_login(login) for login in humans}
    found: Comment | None = None
    warnings: list[str] = []
    for comment in comments:
        if expected not in (line.strip() for line in comment.body.splitlines()):
            continue
        if comment.author not in humans_bare:
            warnings.append(
                f"a record marker for {repo} v{version} in a comment by {comment.author} "
                "does not count: only a human login may post a record"
            )
        elif comment.editor is not None and comment.editor not in humans_bare:
            warnings.append(
                f"a record marker for {repo} v{version} in a comment last edited by "
                f"{comment.editor} does not count: only a human login may edit a record"
            )
        elif found is None:
            found = comment
    return found, warnings


def _obj(value: object, what: str, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecordReadError(where, f"{what} is {value!r}")
    return cast("dict[str, Any]", value)


def _login(value: object, what: str, where: str) -> str:
    if value is None:
        return _GHOST
    login = _obj(value, what, where).get("login")
    if not isinstance(login, str) or not login:
        raise RecordReadError(where, f"{what} has no login: {value!r}")
    return trusted_authors.bare_login(login)


def _split_ref(ref: str) -> tuple[str, int]:
    match = _REF_RE.match(ref)
    if match is None:
        msg = f"issue ref {ref!r} is not <owner>/<repo>#<N>"
        raise ValueError(msg)
    return match.group("repo"), int(match.group("number"))


def _comment(node: object, where: str) -> Comment:
    item = _obj(node, "a comment", where)
    body = item.get("body")
    if not isinstance(body, str):
        raise RecordReadError(where, f"a comment has no body: {node!r}")
    raw_editor = item.get("editor")
    return Comment(
        author=_login(item.get("author"), "a comment's author", where),
        editor=None if raw_editor is None else _login(raw_editor, "a comment's editor", where),
        body=body,
    )


def read_comments(ref: str) -> list[Comment]:
    """Every comment on issue *ref* (``<owner>/<repo>#<N>``), with its author and last editor.

    A paginated GraphQL read, followed until the last page. A failed page
    raises from :func:`github.graphql`; a page that is not what was asked
    for, or comments that do not add up to the ``totalCount``, raises
    :class:`RecordReadError` naming the issue.
    """
    repo, number = _split_ref(ref)
    owner, name = repo.split("/", 1)
    where = f"the comments on {ref}"
    comments: list[Comment] = []
    total: object = None
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name, "number": number}
        if cursor is not None:
            variables["after"] = cursor
        data = github.graphql(_COMMENTS_QUERY, **variables)
        repository = _obj(data.get("repository"), "repository", where)
        issue = _obj(repository.get("issue"), "issue", where)
        conn = _obj(issue.get("comments"), "comments", where)
        nodes = conn.get("nodes")
        info = _obj(conn.get("pageInfo"), "comments.pageInfo", where)
        total = conn.get("totalCount")
        if not isinstance(nodes, list) or not isinstance(total, int) or isinstance(total, bool):
            raise RecordReadError(where, f"comments has no totalCount or nodes: {conn!r}")
        comments.extend(_comment(node, where) for node in cast("list[object]", nodes))
        has_next, end = info.get("hasNextPage"), info.get("endCursor")
        if not isinstance(has_next, bool):
            raise RecordReadError(where, f"comments.pageInfo has no hasNextPage: {info!r}")
        if not has_next:
            break
        if not isinstance(end, str) or not end:
            raise RecordReadError(where, f"comments has a next page but no cursor: {info!r}")
        cursor = end
    if len(comments) != total:
        raise RecordReadError(where, f"GitHub lists {total} comments but returned {len(comments)}")
    return comments


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


def _humans() -> tuple[str, ...]:
    """The ``human`` logins of ``vrg-trusted-authors``; a failed load is an error, never ``()``."""
    try:
        return trusted_authors.load_trusted_authors().human
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
    )


def _post(ref: str, body: str) -> None:
    """Post *body* on issue *ref* exactly once: a failure may hide a landed comment."""
    repo, number = _split_ref(ref)
    github.post_json_once(f"repos/{repo}/issues/{number}/comments", {"body": body})


def _record_issue(
    issue: IssueEntry,
    release: _Release,
    humans: Sequence[str],
    merge_commits: dict[int, str],
) -> bool:
    """Confirm or post *issue*'s record; True when it was posted by this call.

    Each foreign record marker found on the issue is printed as a warning.
    """
    found, warnings = counting_record(
        read_comments(issue.ref), release.repo, release.version, humans
    )
    for warning in warnings:
        print(f"warning: {issue.ref}: {warning}", file=sys.stderr)
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
            # A superseding release's absorbed issues name the release they
            # came from (spec §4.7); that is --supersede's to set.
            supersedes=None,
        ),
    )
    return True


def post_records(ctx: ReleaseContext) -> None:
    """Post a ``Released:`` record on every issue in the state block that has none yet.

    An issue the state block records as posted is skipped. Every other one
    is read for a counting record (:func:`counting_record`), and given one
    when it has none; either way it is then recorded as posted in the state
    block before the next issue, so a resume never posts a second. An issue
    whose read or post fails is named, and the stage carries on to the
    rest, then fails naming each. Writing the state block is never
    tolerated: a failed write stops the stage at once.
    """
    ctx.records_new = ctx.records_found = 0
    current = ctx.state
    if current is None:
        raise _refuse("the run holds no release state block, so its issue list is unknown.")
    pending = [issue for issue in current.issues if issue.ref not in current.records_posted]
    if not pending:
        return
    release = _release(ctx, current)
    humans = _humans()
    merge_commits: dict[int, str] = {}
    failures: list[str] = []
    for issue in pending:
        try:
            new = _record_issue(issue, release, humans, merge_commits)
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
            failures.append(f"{issue.ref}: {exc}")
            continue
        current = replace(current, records_posted=(*current.records_posted, issue.ref))
        write_state(ctx, current)
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
