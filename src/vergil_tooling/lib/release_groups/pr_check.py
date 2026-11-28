"""The whole-group rule on a PR: every group a PR closes is whole, and in build order.

Epic vergil-project/.github#1672, phase 3 Task 11 (spec §5.2). A release group ships as one
batch, so a PR that closes some of a group's members, and not the rest, would ship part of
it. :func:`check_whole_groups` refuses that, and refuses a group whose members a PR closes
out of the group's build order (its order is the batch's commit order when the PR is a batch;
groups may interleave, each keeping its own order). Both checks read the group records through
:func:`record.groups_of`, which fails closed: an unreadable or edited record refuses the PR.

What a PR closes is GitHub's closing references, in any repo, plus every issue its body names
with a closing keyword of the whole autoclose family. :func:`closing_of_pr` reads that
superset from the PR. Only the body is parsed here, whoever wrote it: an untrusted reference
is used to refuse, never to allow. Every read that fails raises, and the caller translates it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import epics, github, trust
from vergil_tooling.lib.pr_workflow.batch_rules import _READ_ERRORS, BatchRuleError
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_groups import record

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

_WHOLE_RULE = "whole groups"
_ORDER_RULE = "group order"

# The errors a group read can end in: a bad record, or any read error batch_rules refuses on.
_GROUP_READ_ERRORS: tuple[type[Exception], ...] = (record.GroupRecordError, *_READ_ERRORS)

# The closing keywords of the autoclose family, any case, with an optional colon. A keyword
# must not be the end of a longer word (prefixes, closing). The reference is #N, owner/repo#N
# or a full issue URL; the optional whitespace is wider than GitHub's, so the match is a superset.
_CLOSING_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"(?:https://github\.com/(?P<url_owner>[A-Za-z0-9._-]+)/(?P<url_repo>[A-Za-z0-9._-]+)"
    r"/issues/(?P<url_number>[0-9]+)"
    r"|(?:(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+))?#(?P<number>[0-9]+))"
    r"(?![0-9])",
    re.IGNORECASE,
)


def _key(ref: epics.IssueRef) -> tuple[str, str, int]:
    """*ref* as a key that ignores the case of its owner and repo (GitHub names ignore it)."""
    return (ref.owner.casefold(), ref.repo.casefold(), ref.number)


def _group_key(group: record.Group) -> tuple[str, str, int, str]:
    return (*_key(group.epic), group.group)


def _distinct(refs: Iterable[epics.IssueRef]) -> list[epics.IssueRef]:
    """*refs* with each issue once, the first spelling kept, in order."""
    seen: dict[tuple[str, str, int], epics.IssueRef] = {}
    for ref in refs:
        seen.setdefault(_key(ref), ref)
    return list(seen.values())


def _split_repo(name: str) -> tuple[str, str]:
    owner, sep, repo = name.partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise ValueError(f"not an owner/name repo: {name!r}")
    return owner, repo


def _groups_of(ref: epics.IssueRef) -> list[record.Group]:
    """The live groups holding *ref*; a failed read refuses, naming the issue."""
    try:
        return record.groups_of(ref)
    except github.MissingGitHubTokenError:
        raise
    except _GROUP_READ_ERRORS as exc:
        raise BatchRuleError(
            _WHOLE_RULE,
            f"could not read the group records of {ref.slug}: {error_text(exc)}",
        ) from exc


def _live_groups(refs: Sequence[epics.IssueRef]) -> list[record.Group]:
    """Every distinct live group holding a ref of *refs*, in the order first met."""
    found: dict[tuple[str, str, int, str], record.Group] = {}
    for ref in refs:
        for group in _groups_of(ref):
            found.setdefault(_group_key(group), group)
    return list(found.values())


def _check_whole(groups: Sequence[record.Group], closing: Sequence[epics.IssueRef]) -> None:
    """Refuse when a group has a member the PR does not close, naming each such group."""
    closed = {_key(ref) for ref in closing}
    lines: list[str] = []
    for group in groups:
        missing = [member.slug for member in group.members if _key(member) not in closed]
        if missing:
            lines.append(f"{group.group} of {group.epic.slug} is missing {', '.join(missing)}")
    if lines:
        raise BatchRuleError(
            _WHOLE_RULE,
            "; ".join(lines)
            + ". A release group ships whole: the PR must close every member of each"
            " group it closes a member of",
        )


def _check_order(groups: Sequence[record.Group], order: Sequence[epics.IssueRef]) -> None:
    """Refuse when a group's members are not in its build order within *order*.

    A member *order* does not list counts as out of order. Other groups' members between a
    group's own members are fine: each group keeps its own order.
    """
    position: dict[tuple[str, str, int], int] = {}
    for index, ref in enumerate(order):
        position.setdefault(_key(ref), index)
    lines: list[str] = []
    for group in groups:
        last = -1
        out_of_order: list[str] = []
        for member in group.members:
            at = position.get(_key(member))
            if at is None or at <= last:
                out_of_order.append(member.slug)
            else:
                last = at
        if out_of_order:
            build = ", ".join(member.slug for member in group.members)
            lines.append(
                f"{group.group} of {group.epic.slug} builds in the order {build}; out of"
                f" order: {', '.join(out_of_order)}"
            )
    if lines:
        raise BatchRuleError(
            _ORDER_RULE,
            "; ".join(lines) + ". A PR lists each group's members in the group's build order",
        )


def check_whole_groups(
    closing: Sequence[epics.IssueRef], *, order: Sequence[epics.IssueRef] | None = None
) -> None:
    """Refuse unless *closing* holds each live group it touches whole, in build order.

    *closing* is every issue the PR closes. Each group of each issue in it must have all its
    members in *closing*; a group with a missing member is refused, naming the group, its epic
    and its missing members. When *order* is given (a batch's commit order), each group's
    members must also appear in *order* in the group's build order. Interleaved groups are fine.
    A failed group read refuses, naming the issue; no group is read for an empty *closing*.
    """
    refs = _distinct(closing)
    if not refs:
        return
    groups = _live_groups(refs)
    _check_whole(groups, refs)
    if order is not None:
        _check_order(groups, order)


def closing_body_refs(body: str, repo: str) -> list[epics.IssueRef]:
    """The issues *body* names with a closing keyword, in body order, each once.

    A keyword of the autoclose family (close, fix or resolve, in any tense) followed by ``#N``,
    ``owner/repo#N`` or a full ``https://github.com/owner/repo/issues/N`` URL names that issue. A
    bare ``#N`` is in *repo*, the PR's own ``owner/name``. The body is parsed whoever wrote it.
    """
    home_owner, home_repo = _split_repo(repo)
    refs: list[epics.IssueRef] = []
    for match in _CLOSING_RE.finditer(body):
        if match.group("url_number") is not None:
            refs.append(
                epics.IssueRef(
                    owner=match.group("url_owner"),
                    repo=match.group("url_repo"),
                    number=int(match.group("url_number")),
                )
            )
            continue
        owner = match.group("owner") or home_owner
        name = match.group("repo") or home_repo
        refs.append(epics.IssueRef(owner=owner, repo=name, number=int(match.group("number"))))
    return _distinct(refs)


def _closing_field(node: Mapping[str, Any], call: str) -> list[epics.IssueRef]:
    """The PR's GitHub closing references, in any repo, counted or not.

    Raises :class:`trust.TrustReadError` when the connection is malformed or truncated: a
    reference left unread could be a member GitHub would close.
    """
    connection = node.get("closingIssuesReferences")
    if not isinstance(connection, dict):
        raise trust.TrustReadError(call, "the closing references are not a connection")
    nodes = connection.get("nodes")
    total = connection.get("totalCount")
    if not isinstance(nodes, list) or not isinstance(total, int):
        raise trust.TrustReadError(call, "the closing references are malformed")
    if total > len(nodes):
        raise trust.TrustReadError(
            call,
            f"its closing references are truncated: {total} listed, {len(nodes)} read",
        )
    malformed = trust.TrustReadError(call, "a closing reference is malformed")
    refs: list[epics.IssueRef] = []
    for item in nodes:
        if not isinstance(item, dict):
            raise malformed
        repository = item.get("repository")
        name = repository.get("nameWithOwner") if isinstance(repository, dict) else None
        number = item.get("number")
        if not isinstance(name, str) or not isinstance(number, int):
            raise malformed
        owner, repo = _split_repo(name)
        refs.append(epics.IssueRef(owner=owner, repo=repo, number=number))
    return refs


def closing_of_pr(repo: str, number: int) -> list[epics.IssueRef]:
    """Every issue GitHub will close with PR *repo*#*number*, a superset, each once.

    It is GitHub's closing references, in any repo and counted or not, and every issue the
    body names with a closing keyword. A PR read or a malformed or truncated reference raises
    (see :func:`trust.read_pr_closing`), and the caller translates it.
    """
    call = f"closing_of_pr {repo}#{number}"
    node = trust.read_pr_closing(repo, number).node
    body = node.get("body")
    if not isinstance(body, str):
        raise trust.TrustReadError(call, "the body is not text")
    return _distinct([*_closing_field(node, call), *closing_body_refs(body, repo)])


def check_pr_whole_groups(repo: str, number: int) -> None:
    """Refuse unless PR *repo*#*number* closes each group it touches whole (no order).

    The check of the PR paths that have no batch order. A failed PR read raises through
    unchanged, for the caller to translate.
    """
    check_whole_groups(closing_of_pr(repo, number))
