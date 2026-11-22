"""Read a task's ``Released:`` record as release evidence (spec §4.10 of epic .github#1440).

``vrg-release-record check <owner>/<repo>#<N>`` is the one reader of the
records ``vrg-release`` posts (:mod:`vergil_tooling.lib.release.records`). It
reads every comment on the task, page by page, and finds the records of the
task's repo that count: a record counts only when its comment's author and
last editor are ``human`` logins of ``vrg-trusted-authors``. A record marker
on any other identity's comment is a warning and is otherwise ignored.

The newest counting record (the highest ``X.Y.Z``) is the one used; any
other counting record is a warning. The check then confirms the record
against git: the merge commit of every closing PR of the task whose closing
reference passes the release's trust rule
(:func:`~vergil_tooling.lib.pending_release.trusted_closing_refs`), read from
GitHub and never from the record, must be an ancestor of the record's tag.
The tag is read with ``git ls-remote``, and the ancestry with ``git
merge-base --is-ancestor`` in a temporary bare fetch of the task's repo
(commits only, no trees or blobs). A record the ancestry check does not
confirm, including one for a task no counted closing PR closed, is a failed
verdict, never a pass.

It prints one JSON object::

    {"task": "<owner>/<repo>#<N>", "verdict": "pass" | "fail" | "none",
     "record": {"version": "X.Y.Z", "tag": "vX.Y.Z", "url": "<release URL>"} | null,
     "ancestry": "pass" | "fail" | null, "warnings": ["..."]}

and exits 0 for ``pass``, 3 for ``fail`` and 4 for ``none`` (no counting
record). Every read fails loudly (spec §6): a failed or incomplete GitHub or
git read is an error, printed on stderr with no JSON, and exits 1; it is
never read as "no record" or "no closing PR".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.pending_release import (
    CLOSING_FIELDS,
    LOOKUP_ERRORS,
    lookup_failure_reason,
    trusted_closing_refs,
)
from vergil_tooling.lib.release.records import (
    RecordReadError,
    counting_record,
    read_comments,
)
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    bare_login,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from vergil_tooling.lib.release.records import Comment

EXIT_PASS = 0
EXIT_ERROR = 1
EXIT_FAIL = 3
EXIT_NONE = 4

Verdict = Literal["pass", "fail", "none"]
_EXIT: dict[str, int] = {"pass": EXIT_PASS, "fail": EXIT_FAIL, "none": EXIT_NONE}

_TASK_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)$")
# A record marker of any repo and version, alone on its (stripped) line.
_MARKER_RE = re.compile(
    r"^<!-- vrg-release:released:(?P<repo>[^\s:]+/[^\s:]+):v(?P<version>\d+\.\d+\.\d+) -->$"
)
# The task's closing PRs, each with what the closing-reference trust check reads.
_PR_LIMIT = 20
_SHORT_SHA = 7
_CLOSING_PRS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      closedByPullRequestsReferences(first: {_PR_LIMIT}, includeClosedPrs: true) {{
        totalCount
        nodes {{
          number merged mergeCommit {{ oid }}{CLOSING_FIELDS}
        }}
      }}
    }}
  }}
}}
"""
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = check(args.task)
    except (*LOOKUP_ERRORS, TrustedAuthorsNotConfiguredError) as exc:
        print(f"ERROR: {args.task}: {lookup_failure_reason(exc)}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"ERROR: {args.task}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(result.as_json(), indent=2))
    return _EXIT[result.verdict]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Read a task's Released: record, posted by vrg-release, as release evidence."),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser(
        "check",
        help="Check that a task's newest counting record is confirmed by git ancestry.",
        description=(
            "Find the task's newest Released: record by a human login, and check that the"
            " merge commit of each of its counted closing PRs is an ancestor of the record's"
            " tag. Prints JSON; exits 0 pass, 3 fail, 4 no record, 1 error."
        ),
    )
    check_parser.add_argument("task", help="The task issue, <owner>/<repo>#<N>.")
    args = parser.parse_args(argv)
    if _TASK_RE.match(args.task) is None:
        parser.error(f"task {args.task!r} is not <owner>/<repo>#<N>")
    return args


@dataclass(frozen=True)
class Record:
    """The record a verdict rests on: its version, tag and GitHub Release URL."""

    version: str
    tag: str
    url: str


@dataclass(frozen=True)
class CheckResult:
    """What ``vrg-release-record check`` prints."""

    task: str
    verdict: Verdict
    record: Record | None
    ancestry: Literal["pass", "fail"] | None
    warnings: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        record = None
        if self.record is not None:
            record = {
                "version": self.record.version,
                "tag": self.record.tag,
                "url": self.record.url,
            }
        return {
            "task": self.task,
            "verdict": self.verdict,
            "record": record,
            "ancestry": self.ancestry,
            "warnings": list(self.warnings),
        }


def check(task: str) -> CheckResult:
    """The verdict on *task* (``<owner>/<repo>#<N>``): its newest counting record, confirmed.

    Raises on any failed or incomplete read; see the module docstring.
    """
    match = _TASK_RE.match(task)
    if match is None:
        msg = f"task {task!r} is not <owner>/<repo>#<N>"
        raise ValueError(msg)
    repo, number = match.group("repo"), int(match.group("number"))
    with github.target_org(repo.split("/", 1)[0]):
        authors = load_trusted_authors()
        humans = authors.human
        version, warnings = _newest_record(read_comments(task), task, repo, humans)
        if version is None:
            return CheckResult(task, "none", None, None, tuple(warnings))
        tag = f"v{version}"
        record = Record(version, tag, f"https://github.com/{repo}/releases/tag/{tag}")
        vouched = frozenset(bare_login(login) for login in (*authors.human, *authors.agents))
        prs, pr_warnings = _closing_prs(repo, number, vouched)
        warnings.extend(pr_warnings)
        ancestry, ancestry_warnings = _ancestry(task, repo, tag, prs)
        warnings.extend(ancestry_warnings)
    verdict: Verdict = "pass" if ancestry == "pass" else "fail"
    return CheckResult(task, verdict, record, ancestry, tuple(warnings))


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _newest_record(
    comments: Sequence[Comment], task: str, repo: str, humans: Sequence[str]
) -> tuple[str | None, list[str]]:
    """The newest version of *repo* with a counting record among *comments*, and warnings.

    A record of another repo is ignored with a warning (a release records
    only its own repo's issues); so is each record that does not count
    (:func:`counting_record`) and each older counting record.
    """
    versions: set[str] = set()
    warnings: list[str] = []
    for comment in comments:
        for line in comment.body.splitlines():
            found = _MARKER_RE.match(line.strip())
            if found is None:
                continue
            if found.group("repo") != repo:
                warnings.append(
                    f"a record marker for {found.group('repo')} v{found.group('version')} "
                    f"is on {task}, which is in {repo}; ignored"
                )
                continue
            versions.add(found.group("version"))
    counted: list[str] = []
    for version in sorted(versions, key=_version_key, reverse=True):
        record, record_warnings = counting_record(comments, repo, version, humans)
        warnings.extend(record_warnings)
        if record is not None:
            counted.append(version)
    if not counted:
        return None, warnings
    newest = counted[0]
    warnings.extend(
        f"{task} also carries a counting record of {repo} v{older}; the newest, v{newest}, is used"
        for older in counted[1:]
    )
    return newest, warnings


def _closing_prs(
    repo: str, number: int, vouched: Collection[str]
) -> tuple[list[tuple[int, str]], list[str]]:
    """``(PR number, merge commit)`` of each merged PR whose closing reference to *number* counts.

    The closing PRs and their merge commits are read from GitHub. A PR
    counts only when :func:`trusted_closing_refs` counts its reference to
    the task; each other merged closing PR is a warning. A PR that never
    merged did not close the task and is left out.
    """
    owner, name = repo.split("/", 1)
    where = f"the closing PRs of {repo}#{number}"
    data = github.graphql(_CLOSING_PRS_QUERY, owner=owner, name=name, number=number)
    connection = _connection(data, where)
    nodes = cast("list[object]", connection["nodes"])
    prs: list[tuple[int, str]] = []
    warnings: list[str] = []
    for raw in nodes:
        if not isinstance(raw, dict):
            raise RecordReadError(where, f"a PR is {raw!r}")
        node = cast("dict[str, object]", raw)
        pr = node.get("number")
        if not isinstance(pr, int) or isinstance(pr, bool):
            raise RecordReadError(where, f"a PR has no number: {node!r}")
        if node.get("merged") is not True:
            continue
        issues, _ = trusted_closing_refs(node, vouched)
        if number not in issues:
            warnings.append(
                f"closing PR #{pr} of {repo}#{number} does not count: its closing reference "
                "is not one the release's trust rule accepts, so the ancestry check skips it"
            )
            continue
        prs.append((pr, _merge_sha(node, pr, where)))
    return prs, warnings


def _connection(data: dict[str, object], where: str) -> dict[str, object]:
    repository = data.get("repository")
    issue = repository.get("issue") if isinstance(repository, dict) else None
    connection = issue.get("closedByPullRequestsReferences") if isinstance(issue, dict) else None
    if not isinstance(connection, dict):
        raise RecordReadError(where, f"no closedByPullRequestsReferences in {data!r}")
    nodes, total = connection.get("nodes"), connection.get("totalCount")
    if not isinstance(nodes, list) or not isinstance(total, int) or isinstance(total, bool):
        raise RecordReadError(where, "closedByPullRequestsReferences has no totalCount or nodes")
    if total > len(cast("list[object]", nodes)):
        raise RecordReadError(where, f"GitHub lists {total} closing PRs but returned {len(nodes)}")
    return cast("dict[str, object]", connection)


def _merge_sha(node: dict[str, object], pr: int, where: str) -> str:
    commit = node.get("mergeCommit")
    oid = commit.get("oid") if isinstance(commit, dict) else None
    if not isinstance(oid, str) or _SHA_RE.match(oid) is None:
        raise RecordReadError(where, f"merged PR #{pr} has no merge commit: {commit!r}")
    return oid


def _ancestry(
    task: str, repo: str, tag: str, prs: Sequence[tuple[int, str]]
) -> tuple[Literal["pass", "fail"], list[str]]:
    """Whether every merge commit in *prs* is an ancestor of *repo*'s *tag*, and why not.

    With no PR to check, nothing confirms the record, so the answer is
    ``fail``. A tag *repo* does not have is ``fail``. Every failed git call
    raises.
    """
    if not prs:
        return "fail", [
            f"no closing PR of {task} counts, so nothing confirms that {tag} contains it"
        ]
    with tempfile.TemporaryDirectory(prefix="vrg-release-record-") as tmp:
        bare = Path(tmp) / "repo.git"
        git.read_output("init", "--quiet", "--bare", str(bare))
        here = ("-C", str(bare))
        git.read_output(*here, "remote", "add", "origin", _remote_url(repo))
        # Commits only: the ancestry walk needs no trees or blobs.
        git.read_output(*here, "config", "remote.origin.promisor", "true")
        git.read_output(*here, "config", "remote.origin.partialclonefilter", "tree:0")
        tag_sha = _tag_commit(here, tag)
        if tag_sha is None:
            return "fail", [f"{repo} has no tag {tag}, so the record names no release"]
        shas = sorted({sha for _, sha in prs})
        git.read_output(
            *here, "fetch", "--quiet", "--no-tags", "--filter=tree:0", "origin", tag_sha, *shas
        )
        warnings = [
            f"#{pr} (merge commit {sha[:_SHORT_SHA]}) is not an ancestor of {tag}"
            for pr, sha in prs
            if not git.is_ancestor(sha, tag_sha, repo=bare)
        ]
    return ("fail" if warnings else "pass"), warnings


def _remote_url(repo: str) -> str:
    """The URL the bare fetch reads *repo* from."""
    return f"https://github.com/{repo}.git"


def _tag_commit(here: tuple[str, ...], tag: str) -> str | None:
    """The commit origin's *tag* resolves to (peeled), or ``None`` when it has none."""
    raw = peeled = None
    listing = git.read_output(
        *here, "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"
    )
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            raw = sha
    commit = peeled or raw
    if commit is not None and _SHA_RE.match(commit) is None:
        msg = f"git ls-remote returned a malformed SHA for {tag}: {commit!r}"
        raise ValueError(msg)
    return commit


if __name__ == "__main__":
    sys.exit(main())
