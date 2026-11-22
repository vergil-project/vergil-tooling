"""What ``develop`` holds that ``main`` does not: the pending-release data layer.

:func:`gather_pending_release` reads the remote state without fetching and
returns a :class:`PendingRelease`: the current release tags and one
:class:`PendingChange` per PR in ``main..<integration>`` (first parent), in
merge order. The calls are bounded by the size of the pending range, never one
per PR:

1. one ``ls-remote`` for the two branch tips and the ``v*`` tags;
2. the GitHub compare of ``main...<integration>``, one call per page;
3. GraphQL commit-to-PR lookups, one call per :data:`_GRAPHQL_CHUNK` commits.

Each commit is mapped to its PR **by SHA**, through GitHub's
associated-pull-request lookup restricted to merged PRs into the integration
branch, so a squash merge, a merge commit and a rebase-merged batch PR (whose
commit subjects carry issue numbers, not the PR number) all map the same way.

A lookup that fails or cannot give a complete answer raises
:class:`PendingReleaseError`; there is never a partial list. Every error
reason passes through :func:`redact_userinfo`, which strips URL userinfo, so
no credential reaches the message; :func:`lookup_failure_reason` turns any
lookup failure into such a reason (``lib/open_prs.py`` shares both). Retrying
transient failures is the ``lib/git.py`` and ``lib/github.py`` wrappers' job;
this module adds none.

Epic vergil-project/.github#1049 (spec §5.2-§5.4).

:func:`gather_release_contents` reads the same range for a release, pinned to
its cut, and also the issues the release contains: the trusted closing
references (:func:`trusted_closing_refs`) of its ``feature/`` PRs, read in the
same GraphQL queries (spec §4.1 of epic vergil-project/.github#1440).
"""

from __future__ import annotations

import datetime
import enum
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.linkage import LINKAGE_RE
from vergil_tooling.lib.process_errors import failure_message
from vergil_tooling.lib.release.state import IssueEntry
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    bare_login,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence

# The release branch; ``vrg-release`` hardcodes it too (spec §6).
RELEASE_BRANCH = "main"

# The tool named in the standard missing-token guidance.
_TOOL = "vrg-repo-status"

# A release tag (exactly three numeric parts) and a rolling minor tag (exactly
# two). ``develop-v*`` and rolling major ``vX`` tags match neither.
_RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_ROLLING_TAG_RE = re.compile(r"^v(\d+)\.(\d+)$")

_TAG_PREFIX = "refs/tags/"
_PEELED_SUFFIX = "^{}"

# Commits per compare page (the API's maximum), and SHAs per GraphQL query.
_PAGE_SIZE = 100
_GRAPHQL_CHUNK = 50

# Connection sizes requested per commit and per PR; a larger totalCount is a
# truncated answer and an error.
_PR_LIMIT = 5
_LABEL_LIMIT = 20
# The connections a release's issue list reads per PR: its closing references,
# its body's edit history and the issues linked to it by hand.
_CLOSING_LIMIT = 20
_EDIT_LIMIT = 100
_CONNECTED_LIMIT = 100

# The integration branch a release is cut from, and the head-branch prefix of
# the PRs whose closing references make up its issue list.
_RELEASE_INTEGRATION = "develop"
_FEATURE_BRANCH_PREFIX = "feature/"

# A full commit SHA, checked before it is interpolated into a GraphQL query.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_SHORT_SHA = 7

# The conventional-commit type of a PR title.
_CONVENTIONAL_RE = re.compile(r"^([a-z]+)(\([^)]*\))?!?:")

_RELEASE_BRANCH_PREFIX = "release/post-"
_DEPS_BRANCH_PREFIX = "deps/update-"
_DEPS_LABEL = "dependency-update"
# Mechanized PRs are opened from ``automation/<job>/<run-id>`` branches
# (epic vergil-project/.github#1064).
_AUTOMATION_BRANCH_PREFIX = "automation/"

# ``scheme://user[:secret]@`` in a URL: the userinfo is removed from every
# error reason. The match runs to the last ``@`` before the path, so a secret
# that itself holds an ``@`` is removed whole.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#]*@")


class PendingReleaseError(RuntimeError):
    """The pending release could not be determined completely.

    The message is the human-readable reason, already stripped of URL
    userinfo by :func:`redact_userinfo`.
    """


class ChangeKind(enum.Enum):
    """What kind of change a pending row is."""

    DEPS = "deps"
    RELEASE = "release"
    AUTOMATION = "automation"
    OTHER = "other"
    NO_PR = "no-pr"


@dataclass(frozen=True)
class ReleaseTags:
    """The current release tags on the remote."""

    latest: str | None
    """The highest ``vX.Y.Z`` tag, or ``None`` when there is none."""
    rolling: str | None
    """The highest ``vX.Y`` tag, or ``None`` when there is none."""
    rolling_target: str | None
    """The highest ``vX.Y.Z`` on the rolling tag's commit, else that commit's
    short SHA; ``None`` exactly when *rolling* is ``None``."""


@dataclass(frozen=True)
class PendingChange:
    """One pending row: a PR, or a commit that reached the branch without one."""

    pr_number: int | None
    """The PR number; ``None`` only for :attr:`ChangeKind.NO_PR`."""
    branch: str | None
    """The PR's head branch; ``None`` for :attr:`ChangeKind.NO_PR`."""
    title: str
    """The PR title, or the commit subject for :attr:`ChangeKind.NO_PR`."""
    kind: ChangeKind
    type_label: str
    """``deps``, ``release``, ``automation``, the conventional type, or ``-``."""
    merged_ts: float
    """Epoch seconds: the PR's merge time, or the commit date for NO_PR."""
    shas: tuple[str, ...]
    """The first-parent commits of this change, oldest first."""


@dataclass(frozen=True)
class PendingRelease:
    """Everything the pending-release section shows."""

    tags: ReleaseTags
    main_moved_ts: float
    """Epoch seconds: the committer date of the release branch's tip."""
    changes: tuple[PendingChange, ...]
    """In merge order, oldest first."""


@dataclass(frozen=True)
class _PullRequest:
    number: int
    title: str
    branch: str
    merged_ts: float
    labels: tuple[str, ...]
    # The PR's GraphQL node, read by the issue list's trust check.
    node: dict[str, object] = field(compare=False, repr=False)


def redact_userinfo(text: str) -> str:
    """The one builder of error reasons: *text* with URL userinfo removed."""
    return _URL_USERINFO_RE.sub(r"\1", text)


def _error(text: str) -> PendingReleaseError:
    return PendingReleaseError(redact_userinfo(text))


def _version(match: re.Match[str]) -> tuple[int, ...]:
    return tuple(int(part) for part in match.groups())


def parse_release_tags(ls_remote: str) -> ReleaseTags:
    """The release tags in ``git ls-remote`` output *ls_remote*.

    Versions compare numerically (``v2.1.303`` outranks ``v2.1.99``). An
    annotated tag's peeled ``refs/tags/<t>^{}`` line gives the commit it
    points to, overriding the tag object's SHA on its plain line. Lines that
    are not tags, and tags that are neither ``vX.Y.Z`` nor ``vX.Y``, are
    ignored.
    """
    plain: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in ls_remote.splitlines():
        sha, _, ref = line.strip().partition("\t")
        if not ref.startswith(_TAG_PREFIX):
            continue
        name = ref.removeprefix(_TAG_PREFIX)
        if name.endswith(_PEELED_SUFFIX):
            peeled[name.removesuffix(_PEELED_SUFFIX)] = sha
        else:
            plain[name] = sha
    commits = {name: peeled.get(name, sha) for name, sha in plain.items()}

    releases: list[tuple[tuple[int, ...], str]] = []
    rollings: list[tuple[tuple[int, ...], str]] = []
    for name in commits:
        if m := _RELEASE_TAG_RE.match(name):
            releases.append((_version(m), name))
        elif m := _ROLLING_TAG_RE.match(name):
            rollings.append((_version(m), name))

    latest = max(releases)[1] if releases else None
    if not rollings:
        return ReleaseTags(latest, None, None)
    rolling = max(rollings)[1]
    rolling_sha = commits[rolling]
    on_commit = [(v, name) for v, name in releases if commits[name] == rolling_sha]
    target = max(on_commit)[1] if on_commit else rolling_sha[:_SHORT_SHA]
    return ReleaseTags(latest, rolling, target)


def _parents(commit: dict[str, object]) -> list[object]:
    parents = commit["parents"]
    if not isinstance(parents, list):
        msg = f"compare commit parents is not a list: {type(parents).__name__}"
        raise TypeError(msg)
    return cast("list[object]", parents)


def first_parent_chain(commits: list[dict[str, object]], head: str) -> list[dict[str, object]]:
    """The first-parent history of *head* within *commits*, oldest first.

    Starts at *head* and follows each commit's first parent while it is in
    *commits*; commits brought in only by a merge's other parents are left
    out. A *head* not in *commits* (nothing pending) gives an empty chain.
    """
    by_sha = {cast("str", commit["sha"]): commit for commit in commits}
    chain: list[dict[str, object]] = []
    sha: str | None = head
    while sha is not None and sha in by_sha:
        commit = by_sha.pop(sha)
        chain.append(commit)
        parents = _parents(commit)
        sha = cast("str", _obj(parents[0], "parent")["sha"]) if parents else None
    chain.reverse()
    return chain


def classify(title: str, branch: str | None, labels: Sequence[str]) -> tuple[ChangeKind, str]:
    """The kind and TYPE label of a PR, from its *title*, head *branch* and *labels*."""
    if branch is not None and branch.startswith(_RELEASE_BRANCH_PREFIX):
        return ChangeKind.RELEASE, "release"
    # The branch decides before the dependency-update label: an automation
    # PR is never counted as a dependency update or a development PR.
    if branch is not None and branch.startswith(_AUTOMATION_BRANCH_PREFIX):
        return ChangeKind.AUTOMATION, "automation"
    if (branch is not None and branch.startswith(_DEPS_BRANCH_PREFIX)) or _DEPS_LABEL in labels:
        return ChangeKind.DEPS, "deps"
    m = _CONVENTIONAL_RE.match(title)
    return ChangeKind.OTHER, (m.group(1) if m else "-")


def _obj(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{what} is not an object: {type(value).__name__}"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def _list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{what} is not a list: {type(value).__name__}"
        raise TypeError(msg)
    return cast("list[object]", value)


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        msg = f"{what} is not a string: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _int(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{what} is not an integer: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _timestamp(value: object, what: str) -> float:
    return datetime.datetime.fromisoformat(_str(value, what)).timestamp()


def _committer_date(commit: dict[str, object], what: str) -> float:
    inner = _obj(commit["commit"], f"{what} commit")
    return _timestamp(_obj(inner["committer"], f"{what} committer")["date"], f"{what} date")


def _subject(commit: dict[str, object]) -> str:
    message = _str(_obj(commit["commit"], "commit")["message"], "commit message")
    return message.split("\n", 1)[0]


def _remote_tips(ls_remote: str, integration: str, remote: str) -> tuple[str, str]:
    heads: dict[str, str] = {}
    for line in ls_remote.splitlines():
        sha, _, ref = line.strip().partition("\t")
        heads[ref] = sha
    main_sha = heads.get(f"refs/heads/{RELEASE_BRANCH}")
    dev_sha = heads.get(f"refs/heads/{integration}")
    if main_sha is None or dev_sha is None:
        raise _error(f"{RELEASE_BRANCH} or {integration} not found on {remote}")
    return main_sha, dev_sha


def _compare(
    repo: str, main_sha: str, dev_sha: str, integration: str
) -> tuple[list[dict[str, object]], float]:
    """Every commit of ``main...<integration>``, and the release tip's commit date.

    The pages are read one by one, not with ``github.read_json_paginated``:
    that helper flattens the pages and drops the envelope, so it cannot check
    the collected count against ``total_commits``, the completeness proof
    this lookup needs.
    """
    endpoint = f"repos/{repo}/compare/{main_sha}...{dev_sha}"
    commits: list[dict[str, object]] = []
    total: int | None = None
    main_moved_ts = 0.0
    page = 1
    while total is None or len(commits) < total:
        body = _obj(
            github.read_json("api", f"{endpoint}?per_page={_PAGE_SIZE}&page={page}"),
            "compare response",
        )
        if total is None:
            total = _int(body["total_commits"], "total_commits")
            main_moved_ts = _committer_date(_obj(body["base_commit"], "base_commit"), "base")
        added = [_obj(c, "compare commit") for c in _list(body["commits"], "commits")]
        if not added:
            break
        commits.extend(added)
        page += 1
    if len(commits) != total:
        msg = f"compare {RELEASE_BRANCH}...{integration} returned {len(commits)} of {total} commits"
        raise _error(msg)
    return commits, main_moved_ts


# An issue on either side of a ConnectedEvent; a PR on a side reads as ``{}``.
_LINKED_ISSUE = "... on Issue { number repository { nameWithOwner } }"

# What the issue list reads of each PR, besides the pending-release fields: the
# fields :func:`trusted_closing_refs` reads of a PR node, which
# ``vrg-release-record check`` asks for too.
CLOSING_FIELDS = f"""
          repository {{ nameWithOwner }}
          body
          author {{ login }}
          editor {{ login }}
          userContentEdits(first: {_EDIT_LIMIT}) {{ totalCount nodes {{ editor {{ login }} }} }}
          closingIssuesReferences(first: {_CLOSING_LIMIT}, userLinkedOnly: false) {{
            totalCount nodes {{ number repository {{ nameWithOwner }} }}
          }}
          timelineItems(itemTypes: [CONNECTED_EVENT], first: {_CONNECTED_LIMIT}) {{
            filteredCount
            nodes {{
              ... on ConnectedEvent {{
                actor {{ login }} subject {{ {_LINKED_ISSUE} }} source {{ {_LINKED_ISSUE} }}
              }}
            }}
          }}"""


def _pr_fields(*, closing: bool) -> str:
    extra = CLOSING_FIELDS if closing else ""
    return f"""associatedPullRequests(first: {_PR_LIMIT}) {{
        totalCount
        nodes {{
          number title headRefName mergedAt baseRefName merged
          labels(first: {_LABEL_LIMIT}) {{ totalCount nodes {{ name }} }}{extra}
        }}
      }}"""


def _pr_query(shas: Sequence[str], *, closing: bool = False) -> str:
    fields = _pr_fields(closing=closing)
    aliases = []
    for index, sha in enumerate(shas):
        if not _SHA_RE.match(sha):
            raise _error("the compare returned a malformed commit SHA")
        aliases.append(f'    c{index}: object(oid: "{sha}") {{ ... on Commit {{ {fields} }} }}')
    body = "\n".join(aliases)
    return (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{body}\n"
        "  }\n"
        "}"
    )


def _complete_nodes(
    connection: dict[str, object], what: str, *, count: str = "totalCount"
) -> list[dict[str, object]]:
    """The nodes of *connection*, raising when they fall short of its *count* field.

    A ``timelineItems(itemTypes: ...)`` connection passes ``filteredCount``:
    GitHub's ``totalCount`` there counts every timeline item, whatever the
    type filter.
    """
    nodes = [_obj(n, f"{what} node") for n in _list(connection["nodes"], f"{what} nodes")]
    if _int(connection[count], f"{what} {count}") > len(nodes):
        raise _error(f"{what} truncated")
    return nodes


def _merged_pr(sha: str, commit: dict[str, object], integration: str) -> _PullRequest | None:
    """The one merged PR into *integration* associated with commit *sha*, if any."""
    short = sha[:_SHORT_SHA]
    connection = _obj(commit["associatedPullRequests"], "associatedPullRequests")
    matches: list[_PullRequest] = []
    for node in _complete_nodes(connection, f"associatedPullRequests for {short}"):
        if node["merged"] is not True or node["baseRefName"] != integration:
            continue
        number = _int(node["number"], "PR number")
        labels = _complete_nodes(_obj(node["labels"], "labels"), f"labels of #{number} for {short}")
        matches.append(
            _PullRequest(
                number=number,
                title=_str(node["title"], "PR title"),
                branch=_str(node["headRefName"], "PR headRefName"),
                merged_ts=_timestamp(node["mergedAt"], "PR mergedAt"),
                labels=tuple(_str(label["name"], "label name") for label in labels),
                node=node,
            )
        )
    if len(matches) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in matches)
        raise _error(f"commit {short} has {len(matches)} merged PRs into {integration}: {numbers}")
    return matches[0] if matches else None


def _prs_by_sha(
    repo: str, shas: Sequence[str], integration: str, *, closing: bool
) -> dict[str, _PullRequest | None]:
    owner, name = repo.split("/", 1)
    result: dict[str, _PullRequest | None] = {}
    for start in range(0, len(shas), _GRAPHQL_CHUNK):
        chunk = shas[start : start + _GRAPHQL_CHUNK]
        data = github.graphql(_pr_query(chunk, closing=closing), owner=owner, name=name)
        repository = _obj(data["repository"], "repository")
        for index, sha in enumerate(chunk):
            node = repository[f"c{index}"]
            if node is None:
                raise _error(f"commit {sha[:_SHORT_SHA]} not found on GitHub")
            result[sha] = _merged_pr(sha, _obj(node, f"commit {sha[:_SHORT_SHA]}"), integration)
    return result


def _changes(
    chain: list[dict[str, object]], prs: dict[str, _PullRequest | None]
) -> tuple[PendingChange, ...]:
    """One change per run of consecutive chain commits with the same PR."""
    changes: list[PendingChange] = []
    current: _PullRequest | None = None
    for commit in chain:
        sha = cast("str", commit["sha"])
        pr = prs[sha]
        if pr is not None and current is not None and pr.number == current.number:
            last = changes[-1]
            changes[-1] = PendingChange(
                last.pr_number,
                last.branch,
                last.title,
                last.kind,
                last.type_label,
                last.merged_ts,
                (*last.shas, sha),
            )
            continue
        current = pr
        if pr is None:
            changes.append(
                PendingChange(
                    pr_number=None,
                    branch=None,
                    title=_subject(commit),
                    kind=ChangeKind.NO_PR,
                    type_label="-",
                    merged_ts=_committer_date(commit, f"commit {sha[:_SHORT_SHA]}"),
                    shas=(sha,),
                )
            )
            continue
        kind, type_label = classify(pr.title, pr.branch, pr.labels)
        changes.append(
            PendingChange(
                pr_number=pr.number,
                branch=pr.branch,
                title=pr.title,
                kind=kind,
                type_label=type_label,
                merged_ts=pr.merged_ts,
                shas=(sha,),
            )
        )
    return tuple(changes)


def _gather(
    integration: str, remote: str, head: str | None, *, closing: bool = False
) -> tuple[PendingRelease, str, dict[str, _PullRequest | None]]:
    """The pending release, the remote's ``<owner>/<repo>``, and each chain commit's PR."""
    repo = github.github_remote_repo(git.remote_url(remote))
    if repo is None:
        raise _error(f"{remote} is not a github.com remote")
    ls_remote = git.read_output(
        "ls-remote",
        remote,
        f"refs/heads/{RELEASE_BRANCH}",
        f"refs/heads/{integration}",
        f"{_TAG_PREFIX}v*",
    )
    main_sha, dev_sha = _remote_tips(ls_remote, integration, remote)
    if head is not None:
        if not _SHA_RE.match(head):
            raise _error(f"the pinned {integration} commit is not a full SHA: {head!r}")
        dev_sha = head
    tags = parse_release_tags(ls_remote)
    commits, main_moved_ts = _compare(repo, main_sha, dev_sha, integration)
    chain = first_parent_chain(commits, dev_sha)
    shas = [cast("str", commit["sha"]) for commit in chain]
    prs = _prs_by_sha(repo, shas, integration, closing=closing)
    pending = PendingRelease(tags=tags, main_moved_ts=main_moved_ts, changes=_changes(chain, prs))
    return pending, repo, prs


# How a login that is absent (a deleted account) is named in a warning.
_NO_LOGIN = "(no login)"


def _login(actor: object) -> str:
    """The bare login of a GraphQL actor, or ``""`` for none (a deleted account)."""
    if actor is None:
        return ""
    return bare_login(_str(_obj(actor, "actor")["login"], "actor login"))


def _names(logins: Iterable[str]) -> str:
    return ", ".join(sorted({login or _NO_LOGIN for login in logins}))


def _linked_issue(side: object, what: str) -> tuple[str, int] | None:
    """The ``(<owner>/<repo>, number)`` of an issue on a ConnectedEvent side, else None."""
    if side is None:
        return None
    node = _obj(side, what)
    if not node:
        return None
    repo = _str(_obj(node["repository"], f"{what} repository")["nameWithOwner"], f"{what} repo")
    return repo, _int(node["number"], f"{what} number")


def trusted_closing_refs(
    pr: Mapping[str, object], trusted: Collection[str]
) -> tuple[tuple[int, ...], list[str]]:
    """The issue numbers PR node *pr* closes by a trusted reference, and a warning for each other.

    *pr* is a PR's GraphQL node with the fields of ``CLOSING_FIELDS``;
    *trusted* holds bare logins. A closing reference counts only when it is
    in the PR's own repo and either a sanctioned closing line in the body
    names it (:data:`~vergil_tooling.lib.linkage.LINKAGE_RE`'s close family)
    while every identity in the body's history (its author, its last editor
    and every editor in ``userContentEdits``) is trusted, or it was linked by
    hand and every actor that linked it is trusted. Any other reference
    contributes no issue and is named in a warning (spec §4.1 of epic
    vergil-project/.github#1440).

    Raises :class:`PendingReleaseError` when a connection is truncated, and
    :class:`TypeError` or :class:`KeyError` on a malformed node.
    """
    return _closing_refs(pr, trusted)


def _closing_refs(
    pr: Mapping[str, object], trusted: Collection[str]
) -> tuple[tuple[int, ...], list[str]]:
    """The body of :func:`trusted_closing_refs`, which :func:`_issue_list` calls."""
    number = _int(pr["number"], "PR number")
    repo = _str(_obj(pr["repository"], f"repository of #{number}")["nameWithOwner"], "PR repo")
    refs = _complete_nodes(
        _obj(pr["closingIssuesReferences"], "closingIssuesReferences"),
        f"closingIssuesReferences of #{number}",
    )
    edits = _complete_nodes(
        _obj(pr["userContentEdits"], "userContentEdits"), f"userContentEdits of #{number}"
    )
    events = _complete_nodes(
        _obj(pr["timelineItems"], "timelineItems"),
        f"connected events of #{number}",
        count="filteredCount",
    )

    history = [_login(pr["author"]), *(_login(edit["editor"]) for edit in edits)]
    if pr["editor"] is not None:
        history.append(_login(pr["editor"]))
    untrusted_body = [login for login in history if login not in trusted]

    body = _str(pr["body"], f"body of #{number}")
    named = {
        int(match["number"])
        for match in LINKAGE_RE.finditer(body)
        if match["keyword"].lower() != "ref"
        and (match["repo"] is None or match["repo"].lower() == repo.lower())
    }

    linked_by: dict[int, list[str]] = {}
    for event in events:
        actor = _login(event["actor"])
        for side in ("subject", "source"):
            linked = _linked_issue(event[side], f"connected event {side} of #{number}")
            if linked is not None and linked[0].lower() == repo.lower():
                linked_by.setdefault(linked[1], []).append(actor)

    counted: set[int] = set()
    warnings: list[str] = []
    for ref in refs:
        ref_repo = _str(_obj(ref["repository"], "closing ref repository")["nameWithOwner"], "repo")
        issue = _int(ref["number"], "closing ref number")
        if ref_repo.lower() != repo.lower():
            warnings.append(
                f"#{number}: closing reference {ref_repo}#{issue} is in another repo; ignored"
            )
            continue
        from_body = issue in named and not untrusted_body
        linkers = linked_by.get(issue, [])
        by_hand = bool(linkers) and all(login in trusted for login in linkers)
        if from_body or by_hand:
            counted.add(issue)
            continue
        reasons = []
        if issue in named:
            reasons.append(f"its body's history includes untrusted {_names(untrusted_body)}")
        if linkers:
            untrusted = [login for login in linkers if login not in trusted]
            reasons.append(f"it was linked by hand by untrusted {_names(untrusted)}")
        if not reasons:
            reasons.append("no closing line in the body names it and no one linked it by hand")
        warnings.append(f"#{number}: closing reference #{issue} not counted: {'; '.join(reasons)}")
    return tuple(sorted(counted)), warnings


def _issue_list(
    repo: str, prs: Iterable[_PullRequest | None], trusted: Collection[str]
) -> tuple[tuple[IssueEntry, ...], list[str]]:
    """Each issue the ``feature/`` PRs among *prs* close, by number, with every closing PR."""
    features = {
        pr.number: pr
        for pr in prs
        if pr is not None and pr.branch.startswith(_FEATURE_BRANCH_PREFIX)
    }
    closed_by: dict[int, set[int]] = {}
    warnings: list[str] = []
    for number in sorted(features):
        issues, found = _closing_refs(features[number].node, trusted)
        warnings.extend(found)
        for issue in issues:
            closed_by.setdefault(issue, set()).add(number)
    entries = tuple(
        IssueEntry(ref=f"{repo}#{issue}", prs=tuple(sorted(closed_by[issue])))
        for issue in sorted(closed_by)
    )
    return entries, warnings


def _trusted_logins() -> frozenset[str]:
    """Every ``human`` and ``agents`` login; a failed load is an error, never an empty set."""
    try:
        authors = load_trusted_authors()
    except (TrustedAuthorsNotConfiguredError, ValueError, OSError) as exc:
        raise _error(f"could not load the trusted authors: {exc}") from exc
    return frozenset((*authors.human, *authors.agents))


def _process_reason(exc: subprocess.CalledProcessError) -> str:
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
    if stderr and stderr.strip():
        return stderr.strip()
    return failure_message(exc)


# Every exception a remote lookup can raise that :func:`lookup_failure_reason`
# names. Anything else is a bug and propagates.
LOOKUP_ERRORS = (
    github.AppAuthError,
    github.NoInstallationError,
    subprocess.CalledProcessError,
    RuntimeError,
    KeyError,
    TypeError,
    ValueError,
)


def lookup_failure_reason(exc: Exception) -> str:
    """The human-readable reason a remote lookup failed with *exc*, userinfo removed.

    A missing token or an App authentication failure gives the standard
    guidance; a failed process its stderr, else its command; a malformed
    response says so. *exc* is one of :data:`LOOKUP_ERRORS`.
    """
    if isinstance(exc, github.MissingGitHubTokenError):
        text = github.missing_token_message(_TOOL)
    elif isinstance(exc, github.AppAuthError):
        text = github.app_auth_message(exc)
    elif isinstance(exc, github.NoInstallationError):
        text = github.no_installation_message(exc)
    elif isinstance(exc, subprocess.CalledProcessError):
        text = _process_reason(exc)
    elif isinstance(exc, (KeyError, TypeError, ValueError)):
        text = f"unexpected GitHub response: {type(exc).__name__}: {exc}"
    else:
        text = str(exc)
    return redact_userinfo(text)


def gather_pending_release(
    *, integration: str, remote: str = "origin", head: str | None = None
) -> PendingRelease:
    """What *integration* on *remote* holds that ``main`` does not.

    With *head*, a full commit SHA on *integration*, the range is
    ``main..<head>`` instead of ``main`` to the integration branch's tip, so a
    release reads what it was cut from even when the branch has moved since
    (spec §4.1 of epic vergil-project/.github#1440).

    Read-only: it never fetches and never writes a ref. Raises
    :class:`PendingReleaseError` when any lookup fails or is incomplete, with
    the underlying error as its ``__cause__``.
    """
    try:
        return _gather(integration, remote, head)[0]
    except PendingReleaseError:
        raise
    except LOOKUP_ERRORS as exc:
        raise PendingReleaseError(lookup_failure_reason(exc)) from exc


def gather_release_contents(
    *, cut: str, remote: str = "origin"
) -> tuple[PendingRelease, tuple[IssueEntry, ...]]:
    """What ``main..<cut>`` (first parent) holds on *remote*, and the issues it closes.

    The pending release is :func:`gather_pending_release`'s, pinned to the
    ``develop`` commit *cut*. The issue list is the union of the trusted
    closing references (:func:`trusted_closing_refs`) of every PR in the range
    whose head branch starts ``feature/``, in the released repo only, sorted
    by number, each issue with every PR that closes it. Every reference that
    does not count is printed as a ``WARNING:`` on stderr (spec §4.1 of epic
    vergil-project/.github#1440).

    Read-only. Raises :class:`PendingReleaseError` when the trusted authors
    cannot be loaded or any lookup fails or is incomplete.
    """
    trusted = _trusted_logins()
    try:
        pending, repo, prs = _gather(_RELEASE_INTEGRATION, remote, cut, closing=True)
        issues, warnings = _issue_list(repo, prs.values(), trusted)
    except PendingReleaseError:
        raise
    except LOOKUP_ERRORS as exc:
        raise PendingReleaseError(lookup_failure_reason(exc)) from exc
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return pending, issues
