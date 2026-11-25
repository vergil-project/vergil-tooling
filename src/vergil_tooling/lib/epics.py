"""Epic <-> task umbrella relationship, mechanism-agnostic.

An *epic* is an umbrella over *task* issues that may live in other repos within
the same org. The link is GitHub **native sub-issues** where available, with a
portable **cross-repo reference fallback** — a ``Parent: <owner>/<repo>#<N>``
line in the task body — for forges (Forgejo/Codeberg) that lack sub-issues. All
consumers (the finalize close+rollup, the roadmap generator) speak this module's
``IssueRef`` vocabulary, never the underlying mechanism.

Node-id resolution and issue state use REST (``gh api``); the parent/children
traversal and the link mutation use GraphQL (``github.graphql``). The unit tests
mock the ``github`` boundary; real GraphQL/REST correctness is exercised in use.

**Trusted content** (epic vergil-project/.github#1467, spec §5.2). The body
lines (``Parent:``, ``Blocked-by:``) count only from a body the trust layer
(:mod:`vergil_tooling.lib.trust`) trusts. An untrusted body is reported on
stderr, metadata only, and gives no parent and no reflink child; for
:func:`blockers_of` it is a stop. The rollup acts on a close only when its
actor is trusted (:func:`rollup`).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import github, release, remote_config, retry, trust
from vergil_tooling.lib.config import CONFIG_FILE

# The listing bound and its truncation refusal live in lib.github, which uses
# them itself and cannot import this module at load time (it imports
# lib.github); they are re-exported so every caller keeps reaching them here.
from vergil_tooling.lib.github import LISTING_LIMIT as LISTING_LIMIT
from vergil_tooling.lib.github import EpicListTruncatedError as EpicListTruncatedError
from vergil_tooling.lib.github import refuse_truncated as refuse_truncated
from vergil_tooling.lib.labels import ensure_labels
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssueRef:
    """A cross-repo issue coordinate."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass(frozen=True)
class ChildState:
    """A child task: its ref, open/closed state, title, and close timestamp.

    ``title`` is descriptive metadata for machine-readable enumeration (issue
    #2538); it defaults to ``""`` so state-only consumers construct a
    ``ChildState`` without supplying it. ``closed_at`` is the ISO-8601
    ``closedAt`` string (``""`` when open), so ad-hoc archiving can bucket a
    closed child by its close-quarter (issue #2678). ``native_blockers`` is
    the child's native blocked-by list when the read that listed it read that
    too (:func:`child_states_with_blockers`), and ``None`` when it did not.
    """

    ref: IssueRef
    state: str  # "OPEN" | "CLOSED"
    title: str = ""
    closed_at: str = ""  # ISO-8601 closedAt; "" when open
    native_blockers: tuple[BlockerState, ...] | None = None


@dataclass(frozen=True)
class BlockerState:
    """An issue a task is natively blocked by, and its ``OPEN``/``CLOSED`` state."""

    ref: IssueRef
    state: str  # "OPEN" | "CLOSED"


class NativeBlockersReadError(RuntimeError):
    """A task's native blocked-by list could not be read in full.

    Raised when the ``blockedBy`` list is missing or malformed, holds more
    relationships than one read takes, or gives fewer readable issues than its
    ``totalCount`` (a blocker the token cannot see). It is never taken as "no
    blockers".
    """


def _quarter_str(dt: datetime) -> str:
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def quarter_of(closed_at: str) -> str:
    """Return the ``YYYY-Qn`` quarter of an ISO-8601 timestamp (UTC)."""
    if not closed_at:
        raise ValueError("quarter_of: empty timestamp")
    dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    return _quarter_str(dt)


def current_quarter(now: datetime) -> str:
    """Return the ``YYYY-Qn`` quarter containing *now*."""
    return _quarter_str(now)


_PARENT_RE = re.compile(
    r"^\s*Parent:\s*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\s*$",
    re.MULTILINE,
)

_BLOCKED_BY_RE = re.compile(
    r"^\s*Blocked-by:\s*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\s*$",
    re.MULTILINE,
)

# The field the trust layer gives a body's Skipped record.
_BODY_FIELD = "body"

_REF_RE = re.compile(r"^(?:([A-Za-z0-9._-]+/[A-Za-z0-9._-]+))?#([0-9]+)$")


def parse_issue_ref(ref: str, *, default_repo: str) -> IssueRef:
    """Parse a linkage ref (``"#42"`` or ``"owner/repo#42"``) into an ``IssueRef``.

    A bare ``#N`` uses *default_repo* (``"owner/name"``). Raises ``ValueError`` if
    *ref* is malformed or the resolved repo lacks an ``owner/name`` form.
    """
    match = _REF_RE.match(ref.strip())
    if match is None:
        raise ValueError(f"not an issue ref: {ref!r}")
    repo_part, number = match.groups()
    full = repo_part or default_repo
    if "/" not in full:
        raise ValueError(f"cannot resolve repo for {ref!r} (default_repo={default_repo!r})")
    owner, name = full.split("/", 1)
    return IssueRef(owner=owner, repo=name, number=int(number))


def single_target_org(*refs: IssueRef) -> str:
    """Return the single owner shared by *refs*, or raise on a cross-org span.

    Commands that mint a GitHub App token for an explicit target select the
    installation by owner, and one token cannot reach two owners. When an
    operation names refs under different owners (e.g. an epic and a task in
    different orgs) that is out of scope: fail clearly here rather than mint a
    token for one owner and hit a cryptic ``403`` on the other (issue #2070).
    """
    owners = {ref.owner for ref in refs}
    if len(owners) != 1:
        joined = ", ".join(sorted(owners))
        raise ValueError(
            f"cross-org operation is out of scope: refs span multiple owners ({joined})"
        )
    return next(iter(owners))


_SUBISSUES_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      subIssues(first: 100) {
        nodes { number state title closedAt repository { name owner { login } } }
      }
    }
  }
}
"""

#: The most native blocked-by relationships one read takes; a longer list is refused.
NATIVE_BLOCKERS_LIMIT = 100

_BLOCKED_BY_SELECTION = (
    f"blockedBy(first: {NATIVE_BLOCKERS_LIMIT}) "
    "{ totalCount nodes { number state repository { name owner { login } } } }"
)

# The sub-issue listing with each child's native blockers, in the one query.
_SUBISSUES_WITH_BLOCKERS_QUERY = f"""
query($id: ID!) {{
  node(id: $id) {{
    ... on Issue {{
      subIssues(first: 100) {{
        nodes {{
          number state title closedAt repository {{ name owner {{ login }} }}
          {_BLOCKED_BY_SELECTION}
        }}
      }}
    }}
  }}
}}
"""

_NATIVE_BLOCKERS_QUERY = f"""
query($owner: String!, $repo: String!, $number: Int!) {{
  repository(owner: $owner, name: $repo) {{
    issue(number: $number) {{ {_BLOCKED_BY_SELECTION} }}
  }}
}}
"""

_PARENT_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      parent { number repository { name owner { login } } }
    }
  }
}
"""

# A task's native parent with the parent's labels and whether it has a parent
# of its own, in one read, so the chain-aware parent lookup tells a head from
# what may be a continuation without a read of its own (epic
# vergil-project/.github#1609).
_PARENT_WITH_LABELS_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      parent {
        number repository { name owner { login } }
        labels(first: 100) { totalCount nodes { name } }
        parent { number }
      }
    }
  }
}
"""

# One container of an epic's chain (epic vergil-project/.github#1609): its own
# state, its sub-issue total (GitHub's count of every child, open and closed),
# and each child with its labels, so the chain layer can tell a continuation
# from an item without a read per child.
_CONTAINER_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      state
      subIssues(first: 100) {
        totalCount
        nodes {
          number state title closedAt repository { name owner { login } }
          labels(first: 100) { totalCount nodes { name } }
        }
      }
    }
  }
}
"""

_ADD_SUBISSUE = """
mutation($parent: ID!, $child: ID!) {
  addSubIssue(input: {issueId: $parent, subIssueId: $child}) {
    subIssue { number }
  }
}
"""

_REMOVE_SUBISSUE = """
mutation($parent: ID!, $child: ID!) {
  removeSubIssue(input: {issueId: $parent, subIssueId: $child}) {
    subIssue { number }
  }
}
"""

_REPARENT_SUBISSUE = """
mutation($parent: ID!, $child: ID!) {
  addSubIssue(input: {issueId: $parent, subIssueId: $child, replaceParent: true}) {
    subIssue { number }
  }
}
"""


def _issue_endpoint(ref: IssueRef) -> str:
    return f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"


def _node_id(ref: IssueRef) -> str:
    """Resolve an issue's GraphQL global node id via REST."""
    return github.read_output("api", _issue_endpoint(ref), "--jq", ".node_id")


def _issue_state(ref: IssueRef) -> str:
    """Return ``"OPEN"`` or ``"CLOSED"`` for an issue."""
    return github.read_output("api", _issue_endpoint(ref), "--jq", ".state").upper()


def issue_state(ref: IssueRef) -> str:
    """Return ``"OPEN"`` or ``"CLOSED"`` for an issue; a failed read raises."""
    return _issue_state(ref)


def _issue_title(ref: IssueRef) -> str:
    """Return an issue's title (used to tell a live ad-hoc epic from an archive)."""
    return github.read_output("api", _issue_endpoint(ref), "--jq", ".title")


def _issue_closed_at(ref: IssueRef) -> str:
    """Return an issue's ISO-8601 ``closed_at`` string, or ``""`` when still open."""
    return github.read_output("api", _issue_endpoint(ref), "--jq", '.closed_at // ""')


def _ref_from_node(node: Any) -> IssueRef:
    """Build an ``IssueRef`` from a GraphQL issue node (number + repository)."""
    repo = node["repository"]
    return IssueRef(
        owner=str(repo["owner"]["login"]), repo=str(repo["name"]), number=int(node["number"])
    )


def _native_child_states(epic: IssueRef) -> list[ChildState]:
    data: Any = github.graphql(_SUBISSUES_QUERY, id=_node_id(epic))
    nodes = (((data or {}).get("node") or {}).get("subIssues") or {}).get("nodes") or []
    return [
        ChildState(
            ref=_ref_from_node(n),
            state=str(n["state"]).upper(),
            title=str(n.get("title") or ""),
            closed_at=str(n.get("closedAt") or ""),
        )
        for n in nodes
    ]


def _repo_slug(ref: IssueRef) -> str:
    return f"{ref.owner}/{ref.repo}"


def _trusted_body(ref: IssueRef) -> str | None:
    """*ref*'s body when the trust layer trusts it; otherwise ``None``, reported.

    The untrusted body is reported on stderr, metadata only. Its comments are
    not this reader's concern, so their skips are not reported here. A read
    the layer cannot complete raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`.
    """
    content = trust.read_issue(_repo_slug(ref), ref.number)
    trust.report_skipped(
        [item for item in content.skipped if item.field == _BODY_FIELD], stream=sys.stderr
    )
    return content.body


def _body_declares_parent(body: str, epic: IssueRef) -> bool:
    """True iff *body* carries a ``Parent:`` line naming exactly *epic*."""
    return any(
        match.group(1) == epic.owner
        and match.group(2) == epic.repo
        and int(match.group(3)) == epic.number
        for match in _PARENT_RE.finditer(body)
    )


def _reflink_child_states(epic: IssueRef) -> list[ChildState]:
    """Portable fallback: issues whose body references this epic as ``Parent:``.

    ``gh search issues`` is punctuation-blind full-text search across *all* of
    GitHub, so a bare ``Parent: <slug>`` query returns cross-repo false positives
    — unrelated issues that merely contain those words (issue #2259, Fix D). Two
    guards keep the fallback sound: scope the search to the epic's org with
    ``--owner``, and verify each candidate's body actually carries a
    ``Parent: <epic slug>`` line (via :func:`_body_declares_parent`) before
    accepting it. That line counts only from a trusted body: each candidate is
    read again through the trust layer, and one whose body it does not trust
    is reported and left out (:func:`_trusted_body`).
    """
    results: Any = github.read_json(
        "search",
        "issues",
        f"Parent: {epic.slug}",
        "--owner",
        epic.owner,
        "--limit",
        str(LISTING_LIMIT),
        "--json",
        "number,state,title,closedAt,repository,body",
    )
    refuse_truncated(results, LISTING_LIMIT, f"the Parent: {epic.slug} reflink search")
    states: list[ChildState] = []
    for item in results if isinstance(results, list) else []:
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
        if "/" not in name_with_owner:
            continue
        if not _body_declares_parent(str(item.get("body") or ""), epic):
            continue
        owner, name = name_with_owner.split("/", 1)
        ref = IssueRef(owner=owner, repo=name, number=int(item["number"]))
        if not _body_declares_parent(_trusted_body(ref) or "", epic):
            continue
        states.append(
            ChildState(
                ref=ref,
                state=str(item["state"]).upper(),
                title=str(item.get("title") or ""),
                closed_at=str(item.get("closedAt") or ""),
            )
        )
    return states


def child_states(epic: IssueRef) -> list[ChildState]:
    """All child tasks of *epic*'s chain: native sub-issues preferred, reflink fallback.

    *epic* is a head: its items and every continuation's, in container order,
    the continuations themselves excluded
    (:func:`~vergil_tooling.lib.epic_chain.chain_child_states`; chain drift
    raises). A chain with no native items falls back to the issues whose
    trusted body names the head on a ``Parent:`` line.
    """
    native = epic_chain.chain_child_states(epic)
    return native if native else _reflink_child_states(epic)


def _native_blockers(connection: Any, task: IssueRef) -> tuple[BlockerState, ...]:
    """Every blocker in *task*'s ``blockedBy`` *connection*, or a raise.

    A missing or malformed connection, a list longer than one read takes, and
    a blocker the token cannot read (a ``null`` node, or fewer nodes than
    ``totalCount``) all raise :class:`NativeBlockersReadError`: a blocker is
    never dropped.
    """
    if not isinstance(connection, dict):
        raise NativeBlockersReadError(f"no blockedBy list for {task.slug}: {connection!r}")
    total = connection.get("totalCount")
    nodes = connection.get("nodes")
    if not isinstance(total, int) or not isinstance(nodes, list):
        raise NativeBlockersReadError(f"malformed blockedBy list for {task.slug}: {connection!r}")
    if total > NATIVE_BLOCKERS_LIMIT:
        raise NativeBlockersReadError(
            f"{task.slug} has {total} native blockers, more than the "
            f"{NATIVE_BLOCKERS_LIMIT} one read takes"
        )
    blockers: list[BlockerState] = []
    for node in nodes:
        try:
            blockers.append(BlockerState(_ref_from_node(node), str(node["state"]).upper()))
        except (KeyError, TypeError, ValueError) as exc:
            raise NativeBlockersReadError(
                f"unreadable native blocker of {task.slug}: {node!r}"
            ) from exc
    if len(blockers) != total:
        raise NativeBlockersReadError(
            f"{task.slug} has {total} native blockers but only {len(blockers)} could be read"
        )
    return tuple(blockers)


def native_blockers_of(task: IssueRef) -> tuple[BlockerState, ...]:
    """*task*'s native blocked-by relationships, each with its blocker's state.

    One GraphQL read of the task's ``blockedBy`` list; a blocker in any repo
    or org counts. The relationships are GitHub metadata, which only someone
    with triage rights on the task's repo can set, and they only ever hold a
    task back, so no body trust applies. A failed read raises, and an
    incomplete one raises :class:`NativeBlockersReadError`: never "no
    blockers".
    """
    data: Any = github.graphql(
        _NATIVE_BLOCKERS_QUERY, owner=task.owner, repo=task.repo, number=task.number
    )
    issue = ((data or {}).get("repository") or {}).get("issue")
    if not isinstance(issue, dict):
        raise NativeBlockersReadError(f"no issue {task.slug} in the blockedBy read: {data!r}")
    return _native_blockers(issue.get("blockedBy"), task)


def child_states_with_blockers(
    epic: IssueRef,
    *,
    wanted: Callable[[ChildState], bool] | None = None,
    checked: Callable[[ChildState], bool] | None = None,
) -> list[ChildState]:
    """:func:`child_states`, with each native child's blocked-by list read too.

    The native sub-issue listing reads every child's ``blockedBy`` list in the
    same query, so each child carries ``native_blockers``. A reflink-fallback
    child carries ``None``, since its blockers were not read: a caller that
    needs them reads them with :func:`native_blockers_of`. An incomplete list
    raises :class:`NativeBlockersReadError` for the whole listing.

    With *wanted*, only the children it accepts (judged on their ref, state,
    title and close time, before any blocker is taken) are returned, and only
    their lists are checked: a child the caller would discard, such as a
    closed one, never fails the listing over a list it does not need.

    With *checked*, every wanted child is still returned, but only the ones
    it accepts carry their list: the rest carry ``None``, their list neither
    taken nor checked. A caller that lists every child but needs only the
    open ones' blockers so reads the listing once.

    *epic* is a head, and the listing spans its chain: each container is read
    in chain order, its continuation left out (chain drift raises).
    """
    chain = epic_chain.chain_of(epic)
    continuations = set(chain[1:])
    nodes = [
        n
        for container in chain
        for n in _subissue_nodes_with_blockers(container)
        if _ref_from_node(n) not in continuations
    ]
    if not nodes:
        reflinked = _reflink_child_states(epic)
        return reflinked if wanted is None else [c for c in reflinked if wanted(c)]
    states: list[ChildState] = []
    for n in nodes:
        child = ChildState(
            ref=_ref_from_node(n),
            state=str(n["state"]).upper(),
            title=str(n.get("title") or ""),
            closed_at=str(n.get("closedAt") or ""),
        )
        if wanted is not None and not wanted(child):
            continue
        if checked is not None and not checked(child):
            states.append(child)
            continue
        states.append(
            replace(child, native_blockers=_native_blockers(n.get("blockedBy"), child.ref))
        )
    return states


def _subissue_nodes_with_blockers(container: IssueRef) -> list[Any]:
    """*container*'s own sub-issue nodes, each with its native blocked-by list."""
    data: Any = github.graphql(_SUBISSUES_WITH_BLOCKERS_QUERY, id=_node_id(container))
    nodes = (((data or {}).get("node") or {}).get("subIssues") or {}).get("nodes") or []
    return list(nodes)


def native_children(epic: IssueRef) -> list[IssueRef]:
    """*epic*'s native sub-issues only, never the body ``Parent:`` reflinks.

    *epic* is a head: its items and every continuation's, in container order,
    the continuations themselves excluded (chain drift raises).

    The sub-issue list is a separate view from each child's ``parent`` field,
    and GitHub can let the two disagree for a while after a transfer
    (vergil-project/vergil-tooling#4004), so a move that must be settled checks
    both. Unlike :func:`child_states` there is no reflink fallback: an empty
    list means no native sub-issues.
    """
    return [child.ref for child in epic_chain.chain_child_states(epic)]


def render_blocked_by(deps: list[IssueRef]) -> str:
    """Render the ``Blocked-by:`` reflink lines for a validation task body.

    One ``Blocked-by: owner/repo#N`` line per dependency — the portable mirror of
    the ``Parent:`` sub-issue reflink. Empty *deps* yields the empty string.
    """
    return "".join(f"Blocked-by: {dep.slug}\n" for dep in deps)


def blockers_of(task: IssueRef) -> list[IssueRef]:
    """Deps *task* is blocked by, parsed from its ``Blocked-by:`` body reflinks.

    Storage is the portable body reflink — the mirror of the ``Parent:``
    sub-issue fallback — chosen by the storage spike (#2184), when GitHub's
    native issue dependencies were REST-only. They are now readable through
    GraphQL (:func:`native_blockers_of`, :func:`child_states_with_blockers`),
    and ``vrg-queue``'s task views count both (#5094); this reader is the
    body lines alone.

    The lines count only from a trusted body: an untrusted one is a stop
    (:class:`~vergil_tooling.lib.trust.UntrustedContentError`, metadata only),
    never "no blockers".
    """
    content = trust.read_issue(_repo_slug(task), task.number, require_body=True)
    body = content.body or ""
    return [
        IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
        for match in _BLOCKED_BY_RE.finditer(body)
    ]


def all_blockers_closed(blockers: list[IssueRef]) -> bool:
    """True iff every one of *blockers*, a task's blockers already read, is CLOSED.

    No blockers means nothing holds the task, so it is runnable — the empty
    case is vacuously True. It takes the list rather than the task, so a
    batch run reads the blockers with :func:`blockers_or_stop` and stops an
    untrusted child alone; the audit uses it to classify an open child as
    runnable (blockers closed) vs blocked.
    """
    return all(_issue_state(dep) == "CLOSED" for dep in blockers)


#: The field a blocker read's stop names: the ``Blocked-by:`` lines are in the body.
BLOCKERS_STOP_FIELD = "body"


def blockers_or_stop(task: IssueRef, stops: trust.BatchStops) -> list[IssueRef] | None:
    """*task*'s blockers, as :func:`blockers_of` reads them, or ``None`` on a stop.

    For a batch run, which stops the item, not the run (spec §5.3 of epic
    vergil-project/.github#1467): an untrusted body
    (:class:`~vergil_tooling.lib.trust.UntrustedContentError`) is added to
    *stops* with its own metadata and cause, and a body the trust layer cannot
    read in full (:class:`~vergil_tooling.lib.trust.TrustReadError`) as a
    read failure on the ``body`` field, with its message as the detail. The
    caller leaves *task* out and carries on; anything else propagates.
    """
    try:
        return blockers_of(task)
    except trust.UntrustedContentError as exc:
        stops.add(exc.item, exc.item.cause)
    except trust.TrustReadError as exc:
        item = trust.Skipped(
            repo=_repo_slug(task),
            number=task.number,
            kind="issue",
            login=None,
            at="",
            field=BLOCKERS_STOP_FIELD,
            cause=trust.READ_FAILURE,
        )
        stops.add(item, trust.READ_FAILURE, detail=str(exc))
    return None


def _container_parent(task: IssueRef) -> IssueRef | None:
    """*task*'s native sub-issue parent, raw: the one container it is linked in.

    The per-container read the chain layer (:mod:`vergil_tooling.lib.epic_chain`)
    walks with; nothing outside ``epics`` and ``epic_chain`` calls it.
    """
    data: Any = github.graphql(_PARENT_QUERY, id=_node_id(task))
    parent = ((data or {}).get("node") or {}).get("parent")
    if isinstance(parent, dict):
        return _ref_from_node(parent)
    return None


def _label_names(connection: Any, what: str) -> set[str]:
    """The label names of a GraphQL ``labels`` *connection*; a missing or short one raises."""
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        raise RuntimeError(f"the label read of {what} returned no label list")
    nodes = connection["nodes"]
    if int(connection.get("totalCount") or 0) > len(nodes):
        raise RuntimeError(f"the label read of {what} was truncated")
    return {str(label.get("name", "")) for label in nodes}


def _chain_head(
    container: IssueRef, labels: Collection[str], *, nested: bool | None = None
) -> IssueRef:
    """The head of *container*'s chain when it is a continuation, else *container*.

    :func:`~vergil_tooling.lib.epic_chain.head_of` decides, checking every
    rule of spec §4.1 and raising chain drift on a broken one, so a
    continuation that lost either of its labels is drift, never a head. It is
    asked whenever *container* could be a continuation: it carries the
    ``continuation`` label, or it carries ``epic`` and is itself a native
    sub-issue (*nested*; read here when not given), as every linked
    continuation is. A top-level issue without the ``continuation`` label is
    taken as itself: only an orphan that also lost that label escapes, and
    the orphan search, which lists continuations by that label, cannot see it
    either.
    """
    if CONTINUATION_LABEL in labels:
        return epic_chain.head_of(container)
    if "epic" not in labels:
        return container
    if nested is None:
        nested = _container_parent(container) is not None
    return epic_chain.head_of(container) if nested else container


def native_parent_of(task: IssueRef) -> IssueRef | None:
    """*task*'s epic by its native link, or None — never the body ``Parent:`` line.

    The orphan sweep and the queue checks key off the native link only: a task
    whose sole link is a body ``Parent:`` reference is not attached on GitHub,
    so it must read as unparented here (unlike :func:`parent_of`).

    Chain-aware (epic vergil-project/.github#1609): a task linked in a
    continuation belongs to the chain's head, so the head is returned. The
    parent's labels, and whether it has a parent of its own, come in the same
    read, so only a parent that could be a continuation costs more
    (:func:`_chain_head`).
    """
    data: Any = github.graphql(_PARENT_WITH_LABELS_QUERY, id=_node_id(task))
    parent = ((data or {}).get("node") or {}).get("parent")
    if not isinstance(parent, dict):
        return None
    ref = _ref_from_node(parent)
    labels = _label_names(parent.get("labels"), ref.slug)
    if "parent" not in parent:
        raise RuntimeError(f"the parent read of {task.slug} did not say whether {ref.slug} has one")
    return _chain_head(ref, labels, nested=isinstance(parent["parent"], dict))


@dataclass(frozen=True)
class _ContainerChild:
    """One child in a container's listing: its state and its labels."""

    child: ChildState
    labels: frozenset[str]


@dataclass(frozen=True)
class _ContainerListing:
    """One container's raw read: its state, its sub-issue total and its children."""

    state: str  # "OPEN" | "CLOSED"
    total: int  # every child, open and closed, as GitHub counts them
    children: tuple[_ContainerChild, ...]


def _container_listing(container: IssueRef) -> _ContainerListing:
    """Read *container*'s state, sub-issue total and children with their labels.

    The raw per-container read of the chain layer. A missing issue node, or a
    child listing or label list shorter than its total, raises: a partial read
    is never taken as the container's whole.
    """
    data: Any = github.graphql(_CONTAINER_QUERY, id=_node_id(container))
    node = (data or {}).get("node")
    if not isinstance(node, dict) or not isinstance(node.get("subIssues"), dict):
        raise RuntimeError(f"the sub-issue read of {container.slug} returned no issue")
    connection = node["subIssues"]
    nodes = connection.get("nodes") or []
    total = int(connection.get("totalCount") or 0)
    if total > len(nodes):
        raise RuntimeError(
            f"the sub-issue read of {container.slug} listed {len(nodes)} of its {total} children"
        )
    children: list[_ContainerChild] = []
    for n in nodes:
        ref = _ref_from_node(n)
        label_connection = n.get("labels") or {}
        names = [str(label.get("name", "")) for label in label_connection.get("nodes") or []]
        if int(label_connection.get("totalCount") or 0) > len(names):
            raise RuntimeError(f"the label read of {ref.slug} in {container.slug} was truncated")
        children.append(
            _ContainerChild(
                child=ChildState(
                    ref=ref,
                    state=str(n["state"]).upper(),
                    title=str(n.get("title") or ""),
                    closed_at=str(n.get("closedAt") or ""),
                ),
                labels=frozenset(names),
            )
        )
    return _ContainerListing(
        state=str(node.get("state") or "").upper(), total=total, children=tuple(children)
    )


def _container_child_states(container: IssueRef) -> list[ChildState]:
    """*container*'s own native children, raw: no chain walk and no reflink fallback."""
    return [entry.child for entry in _container_listing(container).children]


def parent_of(task: IssueRef) -> IssueRef | None:
    """The epic *task* belongs to: native parent preferred, reflink fallback.

    Chain-aware: the epic is the head of the chain *task* is linked in
    (:func:`native_parent_of`); an item in an orphaned continuation resolves
    to the head its trusted ``Continues:`` line names, reported as drift, and
    nothing is written. The fallback reads the ``Parent:`` line of a trusted
    body only: an untrusted body is reported and gives no parent
    (:func:`_trusted_body`). A ``Parent:`` line naming a continuation means
    its head.
    """
    native = native_parent_of(task)
    if native is not None:
        return native
    body = _trusted_body(task)
    match = _PARENT_RE.search(body or "")
    if match:
        ref = IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
        return _chain_head(ref, _labels(ref))
    return None


class EpicRefusalError(Exception):
    """The base of every refusal to write to an epic (epic vergil-project/.github#1609).

    Carries a complete one-line, user-facing message; :func:`cli.cli_main`
    prints it as ``<prog>: <message>`` and exits 1, with no traceback.
    """


class ClosedEpicError(EpicRefusalError, ValueError):
    """*epic* is closed, and closed epics are not reopened by the tooling.

    Closed is closed (spec §4.2 of epic vergil-project/.github#1609): a closed
    target is an error, never a state to repair by reopening. *hint*, when
    given, is appended to the message (what a person may do about it).
    """

    def __init__(self, epic: IssueRef, hint: str = "") -> None:
        message = f"epic {epic.slug} is closed; closed epics are not reopened by the tooling"
        super().__init__(f"{message}; {hint}" if hint else message)
        self.epic = epic


class ClosedStandingEpicError(ClosedEpicError):
    """No open standing epic has the canonical title, and a closed one does.

    Raised by :func:`ensure_standing_epic`, which never reopens or replaces a
    closed standing epic. *epic* is the lowest-numbered closed match; *others*
    are any further closed epics with the same title.
    """

    def __init__(self, epic: IssueRef, others: Sequence[IssueRef] = ()) -> None:
        hint = "a person may reopen it by hand"
        if others:
            hint += f" (also closed with that title: {', '.join(r.slug for r in others)})"
        super().__init__(epic, hint)
        self.others = tuple(others)


def require_open_epic(epic: IssueRef) -> None:
    """Raise :class:`ClosedEpicError` if *epic* is closed; a failed state read raises."""
    if _issue_state(epic) == "CLOSED":
        raise ClosedEpicError(epic)


def add_child(epic: IssueRef, task: IssueRef) -> None:
    """Link *task* under *epic*; a closed *epic* raises :class:`ClosedEpicError`.

    Closed is closed: the epic is never reopened, and nothing is linked.
    """
    require_open_epic(epic)
    github.graphql(_ADD_SUBISSUE, parent=_node_id(epic), child=_node_id(task))


def _add_child_unchecked(parent: IssueRef, child: IssueRef) -> None:
    """Link *child* under *parent* with no state check: closed is closed's one exception.

    Only :func:`vergil_tooling.lib.epic_chain.find_or_create_continuation`
    calls it, and only to link the next continuation under a closed
    continuation that has no successor (spec §4.2 of epic
    vergil-project/.github#1609): the one link a closed container takes,
    which leaves it closed. Every other link goes through :func:`add_child`.
    """
    github.graphql(_ADD_SUBISSUE, parent=_node_id(parent), child=_node_id(child))


def remove_child(epic: IssueRef, task: IssueRef) -> None:
    """Unlink *task* from *epic* (remove the native sub-issue relationship)."""
    github.graphql(_REMOVE_SUBISSUE, parent=_node_id(epic), child=_node_id(task))


def reparent_child(new_parent: IssueRef, task: IssueRef) -> None:
    """Atomically move *task* from its current parent under *new_parent*.

    GitHub's native sub-issues enforce a single parent, so an add-before-remove
    re-parent is rejected while the child is still linked to its old parent
    ("Sub issue may only have one parent"). ``replaceParent: true`` performs the
    move in one call — no orphan window, no separate remove (issue #2691, proven
    live via #2677). The target's state is not checked here: the archive drain
    targets the current-quarter archive, which is always open, and
    ``vrg-epic-move`` refuses a closed target itself before calling this.
    """
    github.graphql(_REPARENT_SUBISSUE, parent=_node_id(new_parent), child=_node_id(task))


def all_children_closed(epic: IssueRef) -> bool:
    """True iff *epic*'s chain has at least one child and all its children are closed."""
    children = child_states(epic)
    return bool(children) and all(child.state == "CLOSED" for child in children)


def _labels(ref: IssueRef) -> set[str]:
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", f"{ref.owner}/{ref.repo}", "--json", "labels"
    )
    labels = (raw or {}).get("labels") or [] if isinstance(raw, dict) else []
    return {str(label.get("name", "")) for label in labels}


def _labels_and_title(ref: IssueRef) -> tuple[set[str], str]:
    raw: Any = github.read_json(
        "issue",
        "view",
        str(ref.number),
        "--repo",
        f"{ref.owner}/{ref.repo}",
        "--json",
        "labels,title",
    )
    data = raw if isinstance(raw, dict) else {}
    labels = data.get("labels") or []
    return {str(label.get("name", "")) for label in labels}, str(data.get("title") or "")


def is_epic(ref: IssueRef) -> bool:
    """True if *ref* carries the ``epic`` label (i.e. it is in the model)."""
    return "epic" in _labels(ref)


def qualify_epic_ref(ref: str, *, repo: str) -> IssueRef:
    """Parse an explicit epic ref, reading a bare ``#N`` against *repo*'s epic home.

    *repo* (``"owner/name"``) is the repo of the **task** the epic is for — never
    simply the cwd repo. Epics live in their epic home
    (:func:`resolve_epic_home`), so a bare ``#N`` names ``<org>/.github#N``
    (#3411). A fully qualified ``owner/repo#N`` is returned as written, with no
    network call.

    A bare ref checks that ``<org>/.github`` exists, so call it under
    ``github.target_org(<task owner>)``. Fail-loud: a failed check propagates.
    """
    parsed = parse_issue_ref(ref, default_repo=repo)
    if not ref.strip().startswith("#"):
        return parsed
    home_owner, home_repo = resolve_epic_home(parsed.owner, parsed.repo).split("/", 1)
    return IssueRef(owner=home_owner, repo=home_repo, number=parsed.number)


def epic_head(epic: IssueRef, *, labels: Collection[str] | None = None) -> IssueRef:
    """The epic *epic* names: its chain's head when it is a continuation, else *epic*.

    GitHub's UI shows a continuation as a task's parent, so its ref gets
    copied into commands (spec §4.1 of epic vergil-project/.github#1609). A
    continuation is mapped to its head
    (:func:`~vergil_tooling.lib.epic_chain.head_of`, chain drift raises) and
    ``<continuation> is a continuation of <head>; using <head>`` is printed to
    stderr; every kind check then runs against the head. One label read
    (none when the caller passes *epic*'s *labels*), and for an epic, one
    read of whether it is itself a sub-issue (:func:`_chain_head`).
    """
    head = _chain_head(epic, _labels(epic) if labels is None else labels)
    if head != epic:
        print(f"{epic.slug} is a continuation of {head.slug}; using {head.slug}", file=sys.stderr)
    return head


def resolve_epic_ref(ref: str, *, repo: str, sentinels: bool = True) -> IssueRef:
    """Resolve an epic ref for a task in *repo*, accepting the standing sentinels.

    ``"adhoc"`` ensures *repo*'s ad-hoc epic exists in its epic home — creating
    it if absent and reusing it otherwise — via :func:`ensure_adhoc_epic`.
    ``"intake"``, ``"planning"`` and ``"deferred"`` likewise ensure *repo*'s
    intake, planning or deferred queue epic in its queue home
    (:func:`ensure_intake_epic`, :func:`ensure_planning_epic`,
    :func:`ensure_deferred_epic`), ``"hoshin"`` the hoshin epic in *repo*'s
    org ``.github`` (:func:`ensure_hoshin_epic`), and ``"operations"`` the
    operations epic there (:func:`ensure_operations_epic`); ``vrg-intake-route``
    refuses the hoshin sentinel itself. The ``vrg-epic-move`` and ``vrg-issue-create``
    CLIs refuse the deferred epic themselves (:data:`DEFERRED_ROUTE_ONLY`); the
    sentinel resolves here so internal callers can reach it. Any other ref is parsed with
    :func:`qualify_epic_ref` (a bare ``#N`` reads against *repo*'s epic home)
    and validated to carry the ``epic`` label. A continuation's ref resolves to
    its chain's head, with a line on stderr saying so (:func:`epic_head`).

    With *sentinels* False no sentinel is accepted, so nothing is created: a
    sentinel is then parsed as a ref and refused as malformed. For the
    read-only lookups (``vrg-epic-audit --epic``) and the commands that take
    only an explicit epic (``vrg-epic-link``).
    """
    # Keyed by each StandingKind's sentinel, so this cannot drift from
    # EPIC_SENTINELS (the set the CLIs' cross-org guards exempt, #3514).
    ensure = {
        ADHOC.sentinel: ensure_adhoc_epic,
        INTAKE.sentinel: ensure_intake_epic,
        PLANNING.sentinel: ensure_planning_epic,
        DEFERRED.sentinel: ensure_deferred_epic,
        HOSHIN.sentinel: ensure_hoshin_epic,
        OPERATIONS.sentinel: ensure_operations_epic,
    }.get(ref)
    if ensure is not None and sentinels:
        return ensure(repo)
    epic = qualify_epic_ref(ref, repo=repo)
    labels = _labels(epic)
    if "epic" not in labels:
        raise ValueError(f"{epic.slug} is not an epic (missing the 'epic' label)")
    return epic_head(epic, labels=labels)


class EpicHomeError(Exception):
    """An epic home could not be resolved: ``<org>/.github`` is missing or its check failed.

    Carries a one-line, user-facing message; :func:`cli.cli_main` prints it as
    ``<prog>: <message>`` instead of a traceback.
    """


def resolve_epic_home(org: str, target_repo: str) -> str:
    """Map an explicit *target_repo* (bare name) to its epic home ``"owner/repo"``.

    Every repo's epics and queues home in ``<org>/.github``. An owner with no
    ``.github`` is refused up front with an :class:`EpicHomeError` naming the
    repo to create, rather than failing later when GitHub cannot find it
    (#3643). Fail-loud: any other failure of the existence check raises
    :class:`EpicHomeError` (a missing token keeps its own error for
    :func:`cli.cli_main`).
    """
    if target_repo == ".github":
        return f"{org}/.github"
    try:
        exists = github.repo_exists(org, ".github")
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        msg = f"cannot resolve the epic home of {org}/{target_repo}: {exc}"
        raise EpicHomeError(msg) from exc
    if not exists:
        msg = (
            f"the epics and queues of {org}/{target_repo} live in {org}/.github, "
            f"which does not exist; create the {org}/.github repository and try again"
        )
        raise EpicHomeError(msg)
    return f"{org}/.github"


def require_org_home(org: str) -> str:
    """Return ``<org>/.github``, the org-wide queue and epic home, checking it exists.

    The org-wide paths of ``vrg-queue`` and ``vrg-epic-audit`` assume it does; an
    owner with none (a personal account, typically) has no queue or epic home at
    all (#3613). Fail-loud: a missing
    ``.github`` and any other probe failure both raise :class:`EpicHomeError` (a
    missing token keeps its own error for :func:`cli.cli_main`).
    """
    try:
        exists = github.repo_exists(org, ".github")
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        msg = f"cannot check for {org}/.github, the org-wide queue and epic home: {exc}"
        raise EpicHomeError(msg) from exc
    if not exists:
        msg = (
            f"{org} has no .github repository, so there is no org-wide queue or "
            f"epic home; create the {org}/.github repository to give it one"
        )
        raise EpicHomeError(msg)
    return f"{org}/.github"


@dataclass(frozen=True)
class StandingKind:
    """One kind of perpetual standing epic: ad-hoc, the queues, hoshin or operations.

    ``key`` is the discriminating label that, alongside ``epic``, marks a live
    standing epic of this kind (and, alongside ``archive``, its per-quarter
    archives). ``body`` is a :meth:`str.format` template taking ``owner`` (the
    home's owner) and ``name`` (the ``<name>`` in the title); ``archive_body``
    takes the same plus ``quarter``. Standing epics are never rolled up: they
    stay open forever (see :func:`rollup`), and their closed children drain into
    per-quarter archives (see :func:`drain_standing_child`). ``sentinel`` is the
    ``--epic`` value that names this kind's epic (see :func:`resolve_epic_ref`).
    """

    key: str
    sentinel: str
    title_prefix: str
    labels: tuple[str, str]
    archive_prefix: str
    archive_labels: tuple[str, str]
    body: str
    archive_body: str


ADHOC = StandingKind(
    key="ad-hoc",
    sentinel="adhoc",
    title_prefix="Epic (ad hoc): ",
    labels=("epic", "ad-hoc"),
    archive_prefix="Archive (ad hoc): ",
    archive_labels=("archive", "ad-hoc"),
    body=(
        "Perpetual umbrella for ad-hoc work in {owner}/{name}: standalone, "
        "single-issue work only, each task picked up, merged and released on its "
        "own. A task owned by any epic or skill never goes here. Created and "
        "reused idempotently; tasks routed to the ad-hoc epic are linked here.\n"
    ),
    archive_body="Ad-hoc work in {owner}/{name} finished in {quarter}. Managed automatically.\n",
)
INTAKE = StandingKind(
    key="intake",
    sentinel="intake",
    title_prefix="Epic (intake): ",
    labels=("epic", "intake"),
    archive_prefix="Archive (intake): ",
    archive_labels=("archive", "intake"),
    body=(
        "Perpetual intake queue for {name}: every captured, unreviewed item and "
        "every orphan the sweep finds, reviewed oldest first.\n"
    ),
    archive_body="Intake items for {name} closed in {quarter}. Managed automatically.\n",
)
PLANNING = StandingKind(
    key="planning",
    sentinel="planning",
    title_prefix="Epic (planning): ",
    labels=("epic", "planning"),
    archive_prefix="Archive (planning): ",
    archive_labels=("archive", "planning"),
    body="Perpetual planning queue for {name}: items that need a full epic.\n",
    archive_body="Planning items for {name} closed in {quarter}. Managed automatically.\n",
)
DEFERRED = StandingKind(
    key="deferred",
    sentinel="deferred",
    title_prefix="Epic (deferred): ",
    labels=("epic", "deferred"),
    archive_prefix="Archive (deferred): ",
    archive_labels=("archive", "deferred"),
    body=(
        "Perpetual deferred queue for {name}: evaluated items parked until their "
        "revival conditions are met.\n"
    ),
    archive_body="Deferred items for {name} closed in {quarter}. Managed automatically.\n",
)
# The hoshin epic: the logbook tasks of the hoshin review, one epic per org,
# always in <org>/.github (ensure_hoshin_epic). Its tasks are PR-workable, so it
# is not a queue kind; only the hoshin review files and works them, and
# vrg-intake-route refuses to route work there.
HOSHIN = StandingKind(
    key="hoshin",
    sentinel="hoshin",
    title_prefix="Epic (hoshin): ",
    labels=("epic", "hoshin"),
    archive_prefix="Archive (hoshin): ",
    archive_labels=("archive", "hoshin"),
    body=(
        "Perpetual hoshin epic for {name}: the logbook tasks of the hoshin "
        "review, filed and worked only by that review and never by a queue "
        "processor. Created and reused idempotently.\n"
    ),
    archive_body="Hoshin logbook tasks for {name} closed in {quarter}. Managed automatically.\n",
)
# The operations epic: the ops issues of an org's unattended jobs, one epic per
# org, always in <org>/.github (ensure_operations_epic), whatever repo the job
# ran in (epic vergil-project/.github#1494). It is not a queue kind: an ops
# issue is closed by hand once the problem is dealt with, never routed out, and
# no PR closes one (unbuildable_parent_kind).
OPERATIONS = StandingKind(
    key="operations",
    sentinel="operations",
    title_prefix="Epic (operations): ",
    labels=("epic", "operations"),
    archive_prefix="Archive (operations): ",
    archive_labels=("archive", "operations"),
    body=(
        "Perpetual operations epic for {name}: one ops issue per failing "
        "unattended job, filed by vrg-ops-report and closed by hand once the "
        "problem is dealt with. Created and reused idempotently.\n"
    ),
    archive_body="Ops issues for {name} closed in {quarter}. Managed automatically.\n",
)
STANDING_KINDS: tuple[StandingKind, ...] = (
    ADHOC,
    INTAKE,
    PLANNING,
    DEFERRED,
    HOSHIN,
    OPERATIONS,
)
# The queue kinds: one intake, one planning and one deferred epic per queue home
# (queue_home). Their items are not PR-workable (unbuildable_parent_kind).
QUEUE_KINDS: tuple[StandingKind, ...] = (INTAKE, PLANNING, DEFERRED)
QUEUE_KIND_BY_KEY: dict[str, StandingKind] = {kind.key: kind for kind in QUEUE_KINDS}
# The standing kinds the sweep's backstop drain (drain_standing_org) covers:
# every one but ad-hoc, which has its own drain (drain_adhoc_org).
DRAINED_STANDING_KINDS: tuple[StandingKind, ...] = tuple(
    kind for kind in STANDING_KINDS if kind is not ADHOC
)
# The --epic sentinels resolve_epic_ref accepts: "adhoc", "intake", "planning",
# "deferred", "hoshin", "operations".
# Each resolves within the target repo's own org (its epic or queue home), so a
# CLI's pre-network cross-org guard exempts exactly these (#3514).
EPIC_SENTINELS: tuple[str, ...] = tuple(kind.sentinel for kind in STANDING_KINDS)
# The single per-item label on a planning-queue item undergoing epic-create.
PLANNING_IN_PROGRESS_LABEL = "in-progress"

# A stamped per-quarter archive of each standing kind:
# "<archive_prefix><name> — <YYYY>-Qn", e.g. "Archive (ad hoc): <bare> — 2026-Q3"
# or "Archive (intake): <org> — 2026-Q3". The separator is a space, U+2014
# em-dash, space. Matching this distinguishes a terminal archive from the live
# canonical standing epic (which has no stamp). A busy quarter overflows GitHub's
# 100-sub-issue cap into further segments stamped " (N)" for N≥2 (issue #2872);
# the optional segment group captures that suffix (absent → segment 1, the
# unsuffixed original form, so existing archives match).
_ARCHIVE_RES: dict[str, re.Pattern[str]] = {
    kind.key: re.compile(
        rf"^{re.escape(kind.archive_prefix)}(?P<name>.+) — "
        r"(?P<quarter>\d{4}-Q[1-4])(?: \((?P<segment>\d+)\))?$"
    )
    for kind in STANDING_KINDS
}


def archive_re(kind: StandingKind) -> re.Pattern[str]:
    """The stamped per-quarter archive recognizer for *kind* (groups: name, quarter, segment)."""
    return _ARCHIVE_RES[kind.key]


# GitHub rejects a 101st sub-issue, so an archive segment is treated as full at
# this count and the drain rolls to the next segment. The 5-slot margin below the
# hard 100 cap absorbs races between concurrent drain runs near the boundary.
_ADHOC_ARCHIVE_SEGMENT_CAP = 95
# Legacy pre-rename archive title ("Epic (ad hoc): <bare> — <YYYY>-Qn"), used
# ONLY by the self-healing creation path and the normalize sweep to find
# archives still in the old form. Steady-state code keys off archive_re(ADHOC).
_LEGACY_ADHOC_ARCHIVE_RE = re.compile(
    r"^Epic \(ad hoc\): (?P<bare>.+) — (?P<quarter>\d{4}-Q[1-4])$"
)


#: Upper bound on the open issues :func:`_find_epic_by_title` lists. gh returns
#: only 30 rows without ``--limit`` (#3822). Reaching the bound is an error, not
#: a silent truncation: an unseen older epic could carry the title.
EPIC_TITLE_LIST_LIMIT = 1000


class EpicTitleAmbiguousError(ValueError):
    """More than one open epic carries the title :func:`_find_epic_by_title` looked up.

    *refs* are the matching epics, lowest number first, so a caller that knows
    which one it created can name the others.
    """

    def __init__(self, message: str, refs: Sequence[IssueRef]) -> None:
        super().__init__(message)
        self.refs = tuple(refs)


def _find_epic_by_title(
    home: str,
    title: str,
    *,
    prefer_oldest: bool = False,
    labels: Sequence[str] = ("epic", "ad-hoc"),
    states: Literal["open", "all"] = "open",
) -> IssueRef | None:
    """Return the open issue in *home* carrying *labels* whose title is exactly *title*.

    With *states* ``"all"`` the one listing reads closed issues too: an open
    match is returned whatever closed ones share its title, and when only
    closed ones match this raises :class:`ClosedStandingEpicError` naming them
    (closed is closed). An archive never carries the ``epic`` label, so the
    closed rows do not crowd the listing's bound.

    Shared title search for the standing-epic and archive finders. Scoped to open
    issues in *home* carrying every one of *labels* (default ``epic`` +
    ``ad-hoc``); returns None when absent. On more than one match the default
    raises (ambiguous — name an explicit ref rather than guess), which is correct
    for a *live* standing epic where two rows is real corruption. When
    *prefer_oldest* is True the lowest-numbered match is returned instead: archive
    resolution tolerates the transient duplicate archives produced by the
    list-consistency race (#2698) and reuses the oldest.

    The listing asks for up to :data:`EPIC_TITLE_LIST_LIMIT` rows and raises
    :class:`EpicListTruncatedError` when it gets that many, since the list may
    then be truncated and neither "absent" nor a single match can be trusted.
    """
    owner, home_repo = home.split("/", 1)
    rows = _titled_rows(home, title, labels, states)
    closed = sorted(int(r["number"]) for r in rows if r["state"] == "CLOSED")
    rows = [r for r in rows if r["state"] == "OPEN"]
    if len(rows) > 1:
        if prefer_oldest:
            oldest = min(int(r["number"]) for r in rows)
            return IssueRef(owner=owner, repo=home_repo, number=oldest)
        nums = ", ".join(f"#{r['number']}" for r in rows)
        kind = labels[-1]
        raise EpicTitleAmbiguousError(
            f"multiple {kind} epics titled {title!r} in {home} ({nums}) — pass an explicit --epic",
            [
                IssueRef(owner=owner, repo=home_repo, number=number)
                for number in sorted(int(r["number"]) for r in rows)
            ],
        )
    if rows:
        return IssueRef(owner=owner, repo=home_repo, number=int(rows[0]["number"]))
    if closed:
        refs = [IssueRef(owner=owner, repo=home_repo, number=number) for number in closed]
        raise ClosedStandingEpicError(refs[0], refs[1:])
    return None


def _titled_rows(
    home: str, title: str, labels: Sequence[str], states: Literal["open", "all"]
) -> list[dict[str, Any]]:
    """The *states* issues in *home* carrying every one of *labels*, titled exactly *title*.

    Each row carries its ``state`` (``"OPEN"`` or ``"CLOSED"``): an ``"open"``
    listing's rows are open by the query; an ``"all"`` listing asks GitHub for
    each row's state, and a row without a readable one raises
    :class:`ValueError` rather than be guessed. Lists up to
    :data:`EPIC_TITLE_LIST_LIMIT` rows and raises
    :class:`EpicListTruncatedError` when it gets that many.
    """
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        *[arg for label in labels for arg in ("--label", label)],
        "--state",
        states,
        "--limit",
        str(EPIC_TITLE_LIST_LIMIT),
        "--json",
        "number,title,state,labels",
    )
    if isinstance(raw, list) and len(raw) >= EPIC_TITLE_LIST_LIMIT:
        kind = "open issues" if states == "open" else "issues, open and closed,"
        raise EpicListTruncatedError(
            f"{home} has {EPIC_TITLE_LIST_LIMIT}+ {kind} labelled "
            f"{' + '.join(labels)}; refusing to look up {title!r} on a possibly "
            "truncated list — pass an explicit --epic"
        )
    if not isinstance(raw, list):
        return []
    # A continuation is never a canonical epic (spec §4.1 of epic
    # vergil-project/.github#1609): it carries no kind label and its own title,
    # and a row labelled as one is dropped even if it somehow matched both.
    rows = [
        r
        for r in raw
        if isinstance(r, dict)
        and r.get("title") == title
        and not is_continuation({str(label.get("name", "")) for label in r.get("labels") or []})
    ]
    if states == "open":
        return [{**r, "state": "OPEN"} for r in rows]
    for row in rows:
        if row.get("state") not in ("OPEN", "CLOSED"):
            raise ValueError(
                f"listing {home} for {title!r} gave #{row.get('number')} no readable state "
                f"({row.get('state')!r})"
            )
    return rows


def ensure_adhoc_epic(target_repo: str) -> IssueRef:
    """Return *target_repo*'s ad-hoc epic in its resolved epic home, creating it if absent.

    The home is ``<org>/.github`` (:func:`resolve_epic_home`). One per repo, each
    disambiguated by the title ``Epic (ad hoc): <bare repo name>`` and labelled
    ``epic`` + ``ad-hoc``. Idempotent: an existing epic with that title is
    reused; none means create it; two sharing the title is ambiguous and an
    error names an explicit ref instead of guessing. Applies to member repos and
    ``.github`` itself alike.
    """
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for ad-hoc epic (repo={target_repo!r})")
    owner, bare = target_repo.split("/", 1)
    return ensure_standing_epic(ADHOC, resolve_epic_home(owner, bare), bare)


# How long a just-created standing epic may stay missing from ``gh issue list``
# before ensure_standing_epic gives up. Shares the known-resource budget: the
# list lag is seconds (8 s was enough to duplicate a deferred epic,
# vergil-project/.github#531/#532), and 60 s is generous while still surfacing a
# real problem promptly.
STANDING_EPIC_VISIBILITY_BUDGET_SECS = retry.KNOWN_RESOURCE_BUDGET_SECS


class StandingEpicNotVisibleError(RuntimeError):
    """A standing epic was created but the title lookup never confirmed it is listed.

    Either the lookup kept missing it until the budget ran out, or a lookup
    failed (the error is chained as ``__cause__``). The message always names
    the created epic, so a caller can check it rather than create a duplicate.
    """


class StandingEpicDuplicateError(StandingEpicNotVisibleError):
    """A standing epic was created but the title lookup lists another epic with its title.

    Raised by :func:`_await_listed` in place of the lookup's
    :class:`EpicTitleAmbiguousError`, so the error names the epic this run
    created (*created*) and the other same-titled epics (*others*), which only
    this run can tell apart.
    """

    def __init__(self, message: str, created: IssueRef, others: Sequence[IssueRef]) -> None:
        super().__init__(message)
        self.created = created
        self.others = tuple(others)


def _await_listed(
    kind: StandingKind,
    home: str,
    title: str,
    created: IssueRef,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Block until :func:`_find_epic_by_title` returns exactly *created*.

    ``gh issue list`` is eventually consistent, so an issue just created can be
    missing from it for several seconds; a find-or-create caller in that window
    would create a duplicate (#3695). Polls with :func:`retry.compute_delay`'s
    jittered backoff, the last sleep clamped to land on the deadline, followed
    by one final look. Raises :class:`StandingEpicNotVisibleError` naming
    *created* and what the lookup last saw if the budget runs out. A lookup
    that sees two or more same-titled epics stops the wait at once with
    :class:`StandingEpicDuplicateError`, naming *created* and the others. A
    lookup that fails any other way (a ``gh`` read the retry layer gave up on,
    unparseable output, a truncated list) stops the wait at once with
    :class:`StandingEpicNotVisibleError` naming *created*, chained from the
    failure (#3820). The loop itself is :func:`retry.poll_until` (#3821).
    """

    def probe() -> IssueRef | None:
        try:
            return _find_epic_by_title(home, title, labels=kind.labels)
        except EpicTitleAmbiguousError as exc:
            others = [ref for ref in exc.refs if ref != created]
            listed = "" if len(others) < len(exc.refs) else f"; {created.slug} is not listed yet"
            raise StandingEpicDuplicateError(
                f"created {kind.key} epic {created.slug} ({title!r}) but {home} also has "
                f"open epics with that title: {', '.join(ref.slug for ref in others)}{listed}; "
                f"keep one and close the rest before retrying",
                created,
                others,
            ) from exc
        except Exception as exc:
            # Whatever the cause, the epic exists now: an error that dropped its
            # ref would let a re-run in the list-lag window create a duplicate.
            raise StandingEpicNotVisibleError(
                f"created {kind.key} epic {created.slug} ({title!r}) but checking that it "
                f"is listed in {home} failed: {error_text(exc)}; a later lookup could "
                f"create a duplicate — check {created.slug} before retrying"
            ) from exc

    def on_wait(_seen: IssueRef | None, delay: float) -> None:
        log.warning(
            "%s not listed yet right after creating it, rechecking in %.1fs", created.slug, delay
        )

    listed, seen = retry.poll_until(
        probe,
        lambda found: found == created,
        budget=STANDING_EPIC_VISIBILITY_BUDGET_SECS,
        sleep=sleep,
        clock=clock,
        on_wait=on_wait,
    )
    if listed:
        return
    saw = f"saw {seen.slug}" if seen is not None else "saw no matching open epic"
    raise StandingEpicNotVisibleError(
        f"created {kind.key} epic {created.slug} ({title!r}) but it is still not "
        f"listed in {home} after {STANDING_EPIC_VISIBILITY_BUDGET_SECS:g}s ({saw}); "
        f"a later lookup could create a duplicate — check {created.slug} before retrying"
    )


def ensure_standing_epic(
    kind: StandingKind,
    home: str,
    name: str,
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> IssueRef:
    """Return the *kind* standing epic ``<title_prefix><name>`` in *home*, creating it if absent.

    Idempotent: an open issue in *home* carrying ``kind.labels`` with exactly
    that title is reused, whatever closed ones share it; two open ones sharing
    the title is ambiguous and raises rather than guessing. With no open one,
    a closed one with the title raises :class:`ClosedStandingEpicError`
    naming it: closed is closed, so it is never reopened or replaced, and a
    person may reopen it by hand. Neither means ensure the labels exist and
    create it, adding its derived category label (:func:`category_labels`).

    After a create it does not return until the title lookup lists exactly the
    new epic (see :func:`_await_listed`), so a back-to-back caller finds it
    rather than creating a duplicate; it raises
    :class:`StandingEpicNotVisibleError` if that never happens within
    :data:`STANDING_EPIC_VISIBILITY_BUDGET_SECS` or a lookup fails, or its
    :class:`StandingEpicDuplicateError` subclass if the lookup lists another
    epic with the same title; each names the new epic. *sleep* and *clock* default to
    :func:`time.sleep`/:func:`time.monotonic` (resolved at call time) and are
    injectable for tests.
    """
    home_owner, home_repo = home.split("/", 1)
    title = f"{kind.title_prefix}{name}"
    found = _find_epic_by_title(home, title, labels=kind.labels, states="all")
    if found is not None:
        return found
    # The derived category label (epic-management for every kind but ad-hoc) is
    # added at creation only. The lookups keep filtering on kind.labels, so an
    # existing standing epic without it is still found and reused (#1450).
    labels = category_labels(kind.labels, title)
    ensure_labels(home, labels)
    url = github.create_issue(
        repo=home,
        title=title,
        body=kind.body.format(owner=home_owner, name=name),
        labels=labels,
    )
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    created = IssueRef(owner=home_owner, repo=home_repo, number=number)
    _await_listed(
        kind, home, title, created, sleep=sleep or time.sleep, clock=clock or time.monotonic
    )
    return created


def queue_home(target_repo: str) -> tuple[str, str]:
    """Map *target_repo* (``"owner/repo"``) to its queue home and name.

    The home of its intake, planning and deferred queue epics.

    Returns ``(home, name)``: ``<org>/.github`` (:func:`resolve_epic_home`, which
    checks it exists) and the org-level queue, named for the org. Every repo in
    the org shares the org's queues.
    """
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for queue home (repo={target_repo!r})")
    owner, bare = target_repo.split("/", 1)
    return resolve_epic_home(owner, bare), owner


def ensure_intake_epic(target_repo: str) -> IssueRef:
    """Return the intake queue epic for *target_repo*'s queue home, creating it if absent."""
    home, name = queue_home(target_repo)
    return ensure_standing_epic(INTAKE, home, name)


def ensure_planning_epic(target_repo: str) -> IssueRef:
    """Return the planning queue epic for *target_repo*'s queue home, creating it if absent.

    Also ensures the per-item ``in-progress`` label exists, every time, in that
    home and in *target_repo* — the item's own repo, which differs from the home
    for a member-repo item — so ``epic-create`` can apply it to the item (#3520).
    Every entry into planning (the ``planning`` sentinel and
    ``vrg-intake-route … planning``) comes through here with the item's repo.
    """
    home, name = queue_home(target_repo)
    epic = ensure_standing_epic(PLANNING, home, name)
    for repo in dict.fromkeys((home, target_repo)):
        ensure_labels(repo, (PLANNING_IN_PROGRESS_LABEL,))
    return epic


def ensure_deferred_epic(target_repo: str) -> IssueRef:
    """Return the deferred queue epic for *target_repo*'s queue home, creating it if absent."""
    home, name = queue_home(target_repo)
    return ensure_standing_epic(DEFERRED, home, name)


def _org_home(target_repo: str, what: str) -> tuple[str, str]:
    """``(<owner>/.github, <owner>)`` for *target_repo*: the home of an org-only epic.

    Checks that ``.github`` exists (:func:`require_org_home`), so an owner with
    none is an :class:`EpicHomeError`, never a create in a missing repo. *what*
    names the epic in the error for a repo with no owner.
    """
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for {what} epic (repo={target_repo!r})")
    owner = target_repo.split("/", 1)[0]
    return require_org_home(owner), owner


def hoshin_home(target_repo: str) -> tuple[str, str]:
    """The home and name of *target_repo*'s hoshin epic: ``(<owner>/.github, <owner>)``.

    Always the org's ``.github``, like every queue (:func:`queue_home`); see
    :func:`_org_home`.
    """
    return _org_home(target_repo, "hoshin")


def ensure_hoshin_epic(target_repo: str) -> IssueRef:
    """Return the hoshin epic of *target_repo*'s org, creating it if absent.

    ``Epic (hoshin): <org>`` in ``<org>/.github`` (:func:`hoshin_home`),
    labelled ``epic`` + ``hoshin``, created and reused idempotently like
    :func:`ensure_intake_epic`.
    """
    home, name = hoshin_home(target_repo)
    return ensure_standing_epic(HOSHIN, home, name)


def operations_home(target_repo: str) -> tuple[str, str]:
    """The home and name of *target_repo*'s operations epic: ``(<owner>/.github, <owner>)``.

    Homed like the hoshin epic (:func:`hoshin_home`): one per org, whatever
    repo the job ran in; see :func:`_org_home`.
    """
    return _org_home(target_repo, "operations")


def ensure_operations_epic(target_repo: str) -> IssueRef:
    """Return the operations epic of *target_repo*'s org, creating it if absent.

    ``Epic (operations): <org>`` in ``<org>/.github``
    (:func:`operations_home`), labelled ``epic`` + ``operations``, created and
    reused idempotently like :func:`ensure_hoshin_epic`.
    """
    home, name = operations_home(target_repo)
    return ensure_standing_epic(OPERATIONS, home, name)


def resolve_orphan_epic(repo: str) -> IssueRef:
    """The epic an orphaned issue in *repo* is attached to: its intake queue epic."""
    return ensure_intake_epic(repo)


def standing_kind_of(labels: set[str]) -> StandingKind | None:
    """The :class:`StandingKind` a live standing epic with *labels* is, else None.

    A live standing epic carries ``epic`` plus its kind's key label; an archive
    (``archive`` + key, no ``epic``) is not a live standing epic, and nor is a
    continuation (:func:`is_continuation`), whose kind is its head's.
    """
    if "epic" not in labels or is_continuation(labels):
        return None
    return next((kind for kind in STANDING_KINDS if kind.key in labels), None)


def is_standing(labels: set[str]) -> bool:
    """True iff *labels* mark a perpetual standing epic (any :data:`STANDING_KINDS` kind)."""
    return standing_kind_of(labels) is not None


# A proxy epic (vergil-project/.github#1417): a same-org umbrella for the tasks
# another org's development epic (its primary) has in this org. It is not a
# development epic: rollup, the sweep's drift close, the Documents audit and the
# roadmap all skip it, and it lives as long as its primary (lib/proxies.py).
PROXY_LABEL = "proxy"


def is_proxy(labels: Collection[str]) -> bool:
    """True iff *labels* mark a proxy epic (``epic`` + ``proxy``)."""
    return "epic" in labels and PROXY_LABEL in labels


# A continuation (vergil-project/.github#1609, spec §4.1): a container of an
# epic's chain past its head, labelled ``epic`` + ``continuation`` and no kind
# label. Its kind is always its head's (lib/epic_chain.py).
CONTINUATION_LABEL = "continuation"


def is_continuation(labels: Collection[str]) -> bool:
    """True iff *labels* mark a continuation (``epic`` + ``continuation``)."""
    return "epic" in labels and CONTINUATION_LABEL in labels


# An epic's category (vergil-project/.github#1450): management (a queue, the
# hoshin epic or the operations epic), ad-hoc, proxy, or development (every
# other epic). Each has one category label, for display in the GitHub UI only:
# no code reads
# epic-development or epic-management to decide behaviour or to find an epic.
EpicCategory = Literal["management", "ad-hoc", "proxy", "development"]
CATEGORY_LABELS: Mapping[EpicCategory, str] = MappingProxyType(
    {
        "management": "epic-management",
        "ad-hoc": ADHOC.key,
        "proxy": PROXY_LABEL,
        "development": "epic-development",
    }
)
# The category labels only the tools apply, never a caller (ad-hoc and proxy
# are also the markers the category is derived from).
DERIVED_CATEGORY_LABELS = frozenset({"epic-development", "epic-management"})
_MANAGEMENT_KEYS = frozenset(kind.key for kind in STANDING_KINDS if kind is not ADHOC)


class AmbiguousEpicCategoryError(ValueError):
    """An epic carries the markers of more than one category; none is guessed."""


def epic_category(
    labels: Collection[str], title: str, *, ref: IssueRef | None = None
) -> EpicCategory | None:
    """The category of an issue with *labels* and *title*, or None if it is not an epic.

    Derived from the markers the tools already read (``epic``, :func:`is_archive`,
    the standing kinds' keys and ``proxy``), never from the category labels
    themselves. An archive, by label or stamped title, is not an epic. Several
    management kinds together are one category; markers of two categories
    raise :class:`AmbiguousEpicCategoryError`, naming them.

    A continuation (:func:`is_continuation`) carries no kind marker, and its
    category is its head's: it is read from the head of *ref*'s chain, so a
    continuation is never taken for a development epic. Without *ref* a
    continuation's category cannot be known, and :class:`ValueError` is raised.
    """
    if is_continuation(labels):
        if ref is None:
            raise ValueError("a continuation's category is its head's; pass the continuation's ref")
        head = epic_chain.head_of(ref)
        head_labels, head_title = _labels_and_title(head)
        if is_continuation(head_labels):
            raise RuntimeError(f"the head {head.slug} of {ref.slug} is itself a continuation")
        return epic_category(head_labels, head_title)
    if "epic" not in labels or is_archive(labels, title):
        return None
    markers: dict[EpicCategory, list[str]] = {}
    for label in sorted(labels):
        if label in _MANAGEMENT_KEYS:
            markers.setdefault("management", []).append(label)
        elif label == ADHOC.key:
            markers.setdefault("ad-hoc", []).append(label)
        elif label == PROXY_LABEL:
            markers.setdefault("proxy", []).append(label)
    if len(markers) > 1:
        found = ", ".join(label for names in markers.values() for label in names)
        raise AmbiguousEpicCategoryError(
            f"labels mark more than one epic category ({found}); an epic has exactly one"
        )
    return next(iter(markers), "development")


def category_labels(labels: Sequence[str], title: str) -> list[str]:
    """*labels* plus the derived category label an epic created with them needs.

    The category label is appended only when it is one of
    :data:`DERIVED_CATEGORY_LABELS` and not already present; ad-hoc and proxy
    already carry theirs. Raises :class:`AmbiguousEpicCategoryError` on an
    ambiguous set.
    """
    result = list(dict.fromkeys(labels))
    category = epic_category(result, title)
    label = CATEGORY_LABELS[category] if category is not None else None
    if label in DERIVED_CATEGORY_LABELS and label not in result:
        result.append(label)
    return result


def _queue_key(labels: set[str]) -> str | None:
    kind = standing_kind_of(labels)
    if kind in QUEUE_KINDS:
        return kind.key
    return None


def _head_labels(epic: IssueRef) -> set[str]:
    """*epic*'s labels, or its chain head's when *epic* is a continuation.

    A continuation's kind is always its head's (spec §4.1 of epic
    vergil-project/.github#1609), so every kind check reads the head.
    """
    labels = _labels(epic)
    head = _chain_head(epic, labels)
    return labels if head == epic else _labels(head)


def queue_kind(epic: IssueRef) -> str | None:
    """``"intake"``, ``"planning"`` or ``"deferred"`` for a live queue epic, else None.

    A continuation of a queue epic is that queue's kind: its head's.
    """
    return _queue_key(_head_labels(epic))


def queue_or_archive_kind(epic: IssueRef) -> str | None:
    """The queue kind of a live queue epic or of a queue archive epic, else None.

    ``"intake"``, ``"planning"`` or ``"deferred"`` for a live queue epic
    (:func:`queue_kind`'s rule) or for a per-quarter archive of one, so an item
    already drained out of its queue still reads as a queue item. An archive is
    recognized as :func:`is_archive` recognizes it, by its ``archive`` label or
    its stamped title alone, and its kind comes from its kind label or its
    title prefix. None for a development epic, an ad-hoc epic or archive, or a
    non-epic. One read of the labels and title; a failed read raises. A
    continuation is its head's kind, read from the head.
    """
    labels, title = _labels_and_title(epic)
    if is_continuation(labels):
        labels, title = _labels_and_title(epic_chain.head_of(epic))
    live = _queue_key(labels)
    if live is not None or not is_archive(labels, title):
        return live
    return next(
        (kind.key for kind in QUEUE_KINDS if kind.key in labels or archive_re(kind).match(title)),
        None,
    )


def container_kind(epic: IssueRef) -> str | None:
    """The container kind of a live standing epic, else None.

    ``"ad-hoc"``, ``"intake"``, ``"planning"``, ``"deferred"``, ``"hoshin"`` or
    ``"operations"``
    when *epic* is the live standing epic of that kind; None for a development
    (finite) epic, a proxy epic, an archive, or a non-epic. One label read; a
    continuation is its head's kind, read from the head.
    """
    kind = standing_kind_of(_head_labels(epic))
    return kind.key if kind is not None else None


def unbuildable_kind(epic: IssueRef) -> str | None:
    """The kind of *epic* when no PR may close its children, else None.

    ``"intake"``, ``"planning"`` or ``"deferred"`` for a live queue epic
    (:func:`queue_kind`'s rule), and ``"operations"`` for the live operations
    epic, whose ops issues are closed by hand (epic
    vergil-project/.github#1494). None for any other epic or a non-epic. One
    label read; a failed read raises. A continuation is its head's kind, read
    from the head.
    """
    return _unbuildable_key(_head_labels(epic))


def _unbuildable_key(labels: set[str]) -> str | None:
    """The :func:`unbuildable_kind` of a head with *labels*."""
    kind = standing_kind_of(labels)
    if kind in QUEUE_KINDS or kind is OPERATIONS:
        return kind.key
    return None


def unbuildable_parent_kind(ref: str, *, default_repo: str) -> str | None:
    """The :func:`unbuildable_kind` of *ref*'s native parent, else None.

    The PR refusal's test: ``vrg-pr-workflow report-ready``, ``vrg-submit-pr``
    and the batch rules refuse an item under a queue epic, which is not
    buildable until it is routed out (#2485), and an ops issue under the
    operations epic, which records that something broke and is closed by hand
    (a fix for it is a follow-up task of its own). Keys off the **native**
    parent only (:func:`native_parent_of`), the same link intake review lists
    and the orphan migration writes. Self-scoping: an unparseable ref returns
    None. A failed lookup raises — it is never read as "buildable". An item
    linked in a continuation is judged by the chain's head, so an intake item
    in a continuation of the intake queue is still refused.
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return None
    parent = native_parent_of(issue)
    if parent is None:
        return None
    # native_parent_of has already resolved the chain, so its head's own labels decide.
    return _unbuildable_key(_labels(parent))


_QUEUE_REFUSAL_TAILS = {
    INTAKE.key: "it is not buildable until it is routed. Route it with epic-review intake first.",
    PLANNING.key: (
        "it is not buildable until its epic is expanded. Expand it with epic-create first."
    ),
    DEFERRED.key: (
        "it is deferred until its revival conditions are met. "
        "Revive it with epic-review deferred first."
    ),
    OPERATIONS.key: (
        "it is an ops issue, which no PR closes: file the fix as a follow-up task in its "
        "own repo, and close the ops issue by hand once the problem is dealt with."
    ),
}

# The shared refusal for a direct move into or out of the deferred queue
# (vrg-epic-move, vrg-issue-create): only the route records a deferral, so no
# item is deferred without its record and none is revived silently.
DEFERRED_ROUTE_ONLY = (
    "an item enters and leaves the deferred queue only through vrg-intake-route, "
    "which records the deferral (use 'vrg-intake-route <ref> defer' to defer, or a "
    "vrg-intake-route revive route to bring it back)"
)


def queue_refusal_message(ref: str, kind: str) -> str:
    """The one refusal text for an unbuildable item, shared by every PR-path guard.

    *kind* is an :func:`unbuildable_parent_kind` result (a :data:`QUEUE_KINDS`
    key, or ``"operations"`` for an ops issue); anything else is a caller bug
    and raises ``ValueError``.
    """
    tail = _QUEUE_REFUSAL_TAILS.get(kind)
    if tail is None:
        raise ValueError(f"not a queue kind: {kind!r}")
    where = "under the operations epic" if kind == OPERATIONS.key else f"in the {kind} queue"
    return f"--issue ({ref}) is {where}; {tail}"


def is_legitimate_top_level(ref: IssueRef, labels: set[str], title: str, body: str) -> bool:
    """True iff the issue *ref* may stand without an epic parent.

    The legitimate top-level kinds are epics (``epic``, including the standing
    epics), per-quarter archives (:func:`is_archive`), and release tracking issues
    (:func:`release.is_release_tracking_issue`, which counts a title or marker
    only from a trusted body). Everything else needs a parent.
    """
    if "epic" in labels or is_archive(labels, title):
        return True
    return release.is_release_tracking_issue(_repo_slug(ref), ref.number, title=title, body=body)


def is_stamped_archive_title(title: str) -> bool:
    """True iff *title* is a stamped per-quarter archive title of any standing kind."""
    return any(archive_re(kind).match(title) for kind in STANDING_KINDS)


def is_archive(labels: Collection[str], title: str) -> bool:
    """True iff an issue is a per-quarter archive, by its ``archive`` label or its title.

    The title counts on its own, so an archive that lost its label (a partial
    legacy normalize, vergil-tooling#3817) is never taken for an orphan task;
    the audit's misplaced-archive check reports the missing label instead.
    """
    return "archive" in labels or is_stamped_archive_title(title)


def is_managed_repo(repo: str) -> bool:
    """True iff *repo* (``"owner/repo"``) has ``vergil.toml`` on its default branch.

    A 404 means the file is absent (unmanaged). Any other API error propagates.
    """
    try:
        github.read_json("api", f"repos/{repo}/contents/{CONFIG_FILE}")
    except github.GitHubAPIError as exc:
        if remote_config.is_not_found(exc):
            return False
        raise
    return True


def find_standing_epic(kind: StandingKind, home: str, name: str) -> IssueRef | None:
    """Return the live canonical *kind* standing epic ``<title_prefix><name>`` in *home*, or None.

    Unlike :func:`ensure_standing_epic`, this never creates: it is the read-only
    lookup the drain uses to find the epic whose closed children it archives.
    """
    return _find_epic_by_title(home, f"{kind.title_prefix}{name}", labels=kind.labels)


def _adhoc_home(target_repo: str) -> tuple[str, str]:
    """``(home, bare)`` for *target_repo*'s ad-hoc epic and archives, homed in ``<org>/.github``."""
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for ad-hoc epic (repo={target_repo!r})")
    owner, bare = target_repo.split("/", 1)
    return resolve_epic_home(owner, bare), bare


def _normalize_archive_in_place(ref: IssueRef, new_title: str) -> None:
    """Convert a legacy-form archive to the new form: retitle, +archive, -epic.

    Keeps ``ad-hoc``. Works on open or closed issues (``gh issue edit`` permits
    editing a closed issue's title and labels). Ensures the ``archive`` label
    exists first, so a not-yet-migrated org's ``.github`` self-heals instead of
    failing with ``'archive' not found``.
    """
    repo = f"{ref.owner}/{ref.repo}"
    ensure_labels(repo, ("archive",))
    # Two edits, ``archive`` first (vergil-tooling#3817). A single ``gh issue
    # edit`` does not apply its title, add and remove atomically: a failed add
    # of ``archive`` still retitled the issue and dropped ``epic``, leaving it
    # with neither label, and the orphan pass then attached it to an intake
    # epic. With ``archive`` added first, a failed first edit leaves the issue
    # unchanged, so a re-run finishes the conversion. The second edit has the
    # same split: if it drops ``epic`` but fails to retitle, the issue keeps
    # ``archive`` and so is never orphaned, but no normalize path finds it
    # again; while it is open, ``vrg-epic-audit``'s misplaced-archive check
    # reports it for a human to retitle.
    github.run("issue", "edit", str(ref.number), "--repo", repo, "--add-label", "archive")
    github.run(
        "issue",
        "edit",
        str(ref.number),
        "--repo",
        repo,
        "--title",
        new_title,
        "--remove-label",
        "epic",
    )


def archive_title(kind: StandingKind, name: str, quarter: str, segment: int = 1) -> str:
    """Title of the *kind* standing epic *name*'s ``quarter`` archive segment.

    ``<archive_prefix><name> — <quarter>``, e.g. ``Archive (intake): org — 2026-Q4``.
    Segment 1 is the original unsuffixed form (so archives created before
    segmentation keep their exact title, no migration); segment N≥2 appends
    ``" (N)"`` — the overflow buckets for a quarter past GitHub's 100-sub-issue
    cap (issue #2872).
    """
    base = f"{kind.archive_prefix}{name} — {quarter}"
    return base if segment <= 1 else f"{base} ({segment})"


def _segment_placements(
    start_segment: int, start_occupancy: int, num_children: int, cap: int
) -> list[int]:
    """Segment number for each of *num_children* successive children.

    Fills *start_segment* (already holding *start_occupancy* children), rolling to
    the next segment each time one reaches *cap*. Pure — the read-then-mutate fill
    core of :func:`apply_standing_drain`, split out so the roll logic is testable
    without any GitHub IO.
    """
    placements: list[int] = []
    segment, occupancy = start_segment, start_occupancy
    for _ in range(num_children):
        if occupancy >= cap:
            segment += 1
            occupancy = 0
        placements.append(segment)
        occupancy += 1
    return placements


def ensure_standing_archive(
    kind: StandingKind, home: str, name: str, quarter: str, segment: int = 1
) -> IssueRef:
    """Return the *kind* epic *name*'s ``— <quarter>`` archive *segment* in *home*, creating it.

    The archive is the stamped sibling of the live standing epic — same home,
    labelled ``kind.archive_labels`` (``archive`` + the kind's key) — into which
    closed children of *quarter* are re-parented. *segment* selects the overflow
    bucket: 1 is the original unsuffixed archive, N≥2 the ``" (N)"``-suffixed
    spillover for a quarter that exceeds GitHub's 100-sub-issue cap (issue
    #2872). Self-healing and idempotent: an existing new-form archive is reused;
    else — **for an ad-hoc segment 1 only** — a legacy-form archive (old
    ``Epic (ad hoc): … — Qn`` title, ``epic`` + ``ad-hoc``) is normalized in place
    and returned; else a fresh new-form archive is created. Intake and planning
    never had a legacy form. Pre-existing duplicate archives (the
    list-consistency race, #2698) collapse to the oldest.
    """
    home_owner, home_repo = home.split("/", 1)
    title = archive_title(kind, name, quarter, segment)
    existing = _find_epic_by_title(home, title, prefer_oldest=True, labels=kind.archive_labels)
    if existing is not None:
        return existing
    if kind is ADHOC and segment <= 1:
        legacy_title = f"{ADHOC.title_prefix}{name} — {quarter}"
        legacy = _find_epic_by_title(home, legacy_title, prefer_oldest=True)
        if legacy is not None:
            _normalize_archive_in_place(legacy, title)
            return legacy
    ensure_labels(home, kind.archive_labels)
    url = github.create_issue(
        repo=home,
        title=title,
        body=kind.archive_body.format(owner=home_owner, name=name, quarter=quarter),
        labels=list(kind.archive_labels),
    )
    return IssueRef(
        owner=home_owner, repo=home_repo, number=int(url.rstrip("/").rsplit("/", 1)[-1])
    )


def _open_standing_archive_rows(kind: StandingKind, home: str) -> list[tuple[int, re.Match[str]]]:
    """``(number, title-match)`` for every open stamped *kind* archive in *home*.

    The one listing both the segment discovery and the past-archive close read,
    so the two cannot disagree about which issues are *kind* archives.
    """
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        *[arg for label in kind.archive_labels for arg in ("--label", label)],
        "--state",
        "open",
        "--limit",
        str(LISTING_LIMIT),
        "--json",
        "number,title",
    )
    refuse_truncated(
        raw, LISTING_LIMIT, f"the open {' + '.join(kind.archive_labels)} archives of {home}"
    )
    pattern = archive_re(kind)
    out: list[tuple[int, re.Match[str]]] = []
    for r in raw if isinstance(raw, list) else []:
        m = pattern.match(str(r.get("title", ""))) if isinstance(r, dict) else None
        if m:
            out.append((int(r["number"]), m))
    return out


def _standing_archive_segments(
    kind: StandingKind, home: str, name: str, quarter: str
) -> list[tuple[int, IssueRef]]:
    """Open *kind* archive segments for *name*'s *quarter* in *home*, sorted by segment.

    Returns ``(segment_number, ref)`` pairs for every open archive whose title
    matches *name* + *quarter* (segment absent → 1). Used by the drain to find a
    quarter's current write head before filling (issue #2872).
    """
    owner, home_repo = home.split("/", 1)
    out: list[tuple[int, IssueRef]] = []
    for number, m in _open_standing_archive_rows(kind, home):
        if m.group("name") == name and m.group("quarter") == quarter:
            segment = int(m.group("segment")) if m.group("segment") else 1
            out.append((segment, IssueRef(owner, home_repo, number)))
    return sorted(out, key=lambda pair: pair[0])


def list_open_standing_archives(kind: StandingKind, home: str) -> list[tuple[IssueRef, str]]:
    """Open *kind* archives in *home* with their ``YYYY-Qn`` quarter.

    Only stamped archives (title carrying a ``— YYYY-Qn`` suffix) are returned;
    the live canonical standing epic (no stamp) is skipped. Used to find archives
    whose quarter is now past and should be closed.
    """
    owner, home_repo = home.split("/", 1)
    return [
        (IssueRef(owner, home_repo, number), m.group("quarter"))
        for number, m in _open_standing_archive_rows(kind, home)
    ]


@dataclass(frozen=True)
class DrainPlan:
    """A per-epic standing drain: what to re-parent and what to close.

    ``live`` is the canonical (unstamped) standing epic — ad-hoc, intake, or
    planning. ``moves`` pairs each closed child with its close-quarter
    (``YYYY-Qn``) archive bucket. ``close`` lists open archives of the same kind
    whose quarter is now past and should be closed.
    """

    live: IssueRef
    moves: list[tuple[IssueRef, str]]  # (closed child, its close-quarter)
    close: list[IssueRef]  # open archives whose quarter is now past


def plan_standing_drain(
    kind: StandingKind, home: str, name: str, *, now: datetime
) -> DrainPlan | None:
    """Plan the *kind* epic *name*'s drain in *home*, or None if that epic does not exist.

    Buckets each closed child of the live epic by its close-quarter and lists the
    open *kind* archives in *home* whose quarter is strictly before the current
    quarter (so they are eligible to close). Pure/read-only: no mutation here.
    The live epic's children are its whole chain's (:func:`child_states`), so
    a closed child in any continuation is planned too.
    """
    live = find_standing_epic(kind, home, name)
    if live is None:
        return None
    moves = [
        (c.ref, quarter_of(c.closed_at))
        for c in child_states(live)
        if c.state == "CLOSED" and c.closed_at
    ]
    cur = current_quarter(now)
    close = [ref for ref, q in list_open_standing_archives(kind, home) if q < cur]
    return DrainPlan(live=live, moves=moves, close=close)


def plan_adhoc_drain(target_repo: str, *, now: datetime) -> DrainPlan | None:
    """Plan *target_repo*'s ad-hoc drain, or None if it has no live ad-hoc epic."""
    home, bare = _adhoc_home(target_repo)
    return plan_standing_drain(ADHOC, home, bare, now=now)


def _archive_child_count(archive: IssueRef) -> int:
    """Number of native sub-issues under *archive* (capped at 100 by the query).

    The occupancy read the drain uses to decide whether a quarter's write-head
    segment still has room before GitHub's 100-sub-issue cap (issue #2872). The
    query fetches the first 100, which is exactly the bound that matters: a
    full archive reports 100 and the drain rolls to the next segment.
    """
    return len(_native_child_states(archive))


def _standing_archive_head(
    kind: StandingKind, home: str, name: str, quarter: str
) -> tuple[int, IssueRef]:
    """The current write-head segment for (*kind*, *home*, *name*, *quarter*): ``(segment, ref)``.

    The highest existing archive segment for the quarter, or a freshly ensured
    segment 1 when none exist yet. Shared by the single-child event path
    (:func:`ensure_writable_standing_archive`) and the batch drain
    (:func:`apply_standing_drain`) so both resolve the head identically (issue
    #2872).
    """
    segments = dict(_standing_archive_segments(kind, home, name, quarter))
    if segments:
        head = max(segments)
        return head, segments[head]
    return 1, ensure_standing_archive(kind, home, name, quarter, segment=1)


def ensure_writable_standing_archive(
    kind: StandingKind, home: str, name: str, quarter: str
) -> IssueRef:
    """The quarter's *kind* archive segment with room for one more child, rolling if full.

    Single-child entry point (the ``on: issues.closed`` :func:`rollup` path via
    :func:`drain_standing_child`): resolve the write-head segment and, if it has
    reached :data:`_ADHOC_ARCHIVE_SEGMENT_CAP`, create and return the next segment
    so the child never lands in a full archive — GitHub rejects a 101st sub-issue
    (issue #2872). The batch drain does not use this; it fills across segments
    in one pass via :func:`_segment_placements`.
    """
    head_segment, head_ref = _standing_archive_head(kind, home, name, quarter)
    if _archive_child_count(head_ref) >= _ADHOC_ARCHIVE_SEGMENT_CAP:
        return ensure_standing_archive(kind, home, name, quarter, segment=head_segment + 1)
    return head_ref


def apply_standing_drain(kind: StandingKind, home: str, name: str, plan: DrainPlan) -> None:
    """Execute *plan*: ensure each archive segment, atomically re-parent, close past.

    Each closed child is atomically re-parented from the live epic into its
    quarter's archive via :func:`reparent_child` (``replaceParent: true``) — one
    call, no orphan window, no separate remove (issue #2691). Past archives are
    then closed. ``YYYY-Qn`` is lexicographically chronological, so ``q < cur`` in
    the plan is a correct "quarter is past" test.

    A single quarter can exceed GitHub's 100-sub-issue cap, so each quarter's
    children are distributed across capped **segments** (issue #2872). Per quarter:
    discover existing segments, take the highest as the write head, read its
    occupancy once, and fill forward via :func:`_segment_placements` — creating the
    next segment on demand when one reaches :data:`_ADHOC_ARCHIVE_SEGMENT_CAP`.
    Segments are ensured **once** each and cached, never once per child: the list
    index lags issue creation, so ensuring per child let same-quarter children each
    create their own archive before the first became visible (duplicate archives,
    #2698). Caching per (quarter, segment) closes that race.
    """
    # SIBLING RE-PARENT SITE: drain_standing_child() (called from rollup()) moves
    # single closed children into these same archives — ad-hoc, intake, and
    # planning alike — on the on:issues.closed event. Both sites must honour the
    # per-segment cap; they can't merge (batch vs single-child) but they share
    # _standing_archive_head + _ADHOC_ARCHIVE_SEGMENT_CAP. Change a capacity rule
    # in those helpers — never open-code a reparent that skips them — and if you
    # touch how one site fills or rolls segments, make the matching change in the
    # other. See drain_standing_child().
    by_quarter: dict[str, list[IssueRef]] = {}
    for child, quarter in plan.moves:
        by_quarter.setdefault(quarter, []).append(child)
    for quarter, children in by_quarter.items():
        head_segment, head_ref = _standing_archive_head(kind, home, name, quarter)
        cache: dict[int, IssueRef] = {head_segment: head_ref}
        occupancy = _archive_child_count(head_ref)
        placements = _segment_placements(
            head_segment, occupancy, len(children), _ADHOC_ARCHIVE_SEGMENT_CAP
        )
        for child, segment in zip(children, placements, strict=True):
            archive = cache.get(segment)
            if archive is None:
                archive = ensure_standing_archive(kind, home, name, quarter, segment=segment)
                cache[segment] = archive
            if archive == plan.live:
                continue  # defensive: never re-parent into the live epic
            reparent_child(archive, child)
    for archive in plan.close:
        github.run(
            "issue", "close", str(archive.number), "--repo", f"{archive.owner}/{archive.repo}"
        )


def apply_adhoc_drain(target_repo: str, plan: DrainPlan) -> None:
    """Execute *target_repo*'s ad-hoc drain *plan* (see :func:`apply_standing_drain`)."""
    home, bare = _adhoc_home(target_repo)
    apply_standing_drain(ADHOC, home, bare, plan)


def drain_adhoc_repo(target_repo: str, *, apply: bool, now: datetime) -> DrainPlan | None:
    """Plan *target_repo*'s ad-hoc drain, applying it when *apply* is True.

    Returns the plan (None if *target_repo* has no live ad-hoc epic). With
    ``apply=False`` this is a pure dry-run.
    """
    plan = plan_adhoc_drain(target_repo, now=now)
    if plan is not None and apply:
        apply_adhoc_drain(target_repo, plan)
    return plan


def drain_adhoc_org(org: str, *, apply: bool, now: datetime) -> list[DrainPlan]:
    """Drain every repo in *org*, isolating per-repo failures (spec §7).

    Each repo's ad-hoc epic is found in ``<org>/.github`` via
    :func:`drain_adhoc_repo` → :func:`plan_adhoc_drain` → :func:`resolve_epic_home`. A
    corrupted repo (e.g. two ad-hoc epics sharing a title) is reported and
    skipped — never aborting the whole sweep. Returns one plan per repo that has
    a live ad-hoc epic.
    """
    plans: list[DrainPlan] = []
    for bare in github.list_org_repos(org):
        try:
            plan = drain_adhoc_repo(f"{org}/{bare}", apply=apply, now=now)
        except (ValueError, RuntimeError) as exc:
            print(f"skipped {org}/{bare}: {exc}", file=sys.stderr)
            continue
        if plan is not None:
            plans.append(plan)
    return plans


def drain_standing_org(org: str, *, apply: bool, now: datetime) -> list[DrainPlan]:
    """Drain every standing epic but ad-hoc (:data:`DRAINED_STANDING_KINDS`) in *org*.

    Covers the queues and the hoshin and operations epics of ``<org>/.github``,
    their only home (:func:`queue_home`, :func:`hoshin_home`,
    :func:`operations_home`). Each kind is planned and, when
    *apply*, executed via :func:`apply_standing_drain`. A corrupted kind (e.g.
    two intake epics sharing a title) is reported on stderr and skipped — never
    aborting the sweep. Returns one plan per live standing epic. Ad-hoc epics
    are drained by :func:`drain_adhoc_org`, not here.
    """
    home = f"{org}/.github"
    plans: list[DrainPlan] = []
    for kind in DRAINED_STANDING_KINDS:
        try:
            plan = plan_standing_drain(kind, home, org, now=now)
            if plan is not None and apply:
                apply_standing_drain(kind, home, org, plan)
        except (ValueError, RuntimeError) as exc:
            print(f"skipped {kind.key} drain in {home}: {exc}", file=sys.stderr)
            continue
        if plan is not None:
            plans.append(plan)
    return plans


def drain_standing_child(kind: StandingKind, parent: IssueRef, task: IssueRef) -> None:
    """Drain one just-closed *task* from its live standing epic *parent* into an archive.

    The single-child ``on: issues.closed`` path of :func:`rollup`. *task* is
    re-parented into the *kind* archive for its close-quarter, rolling to the next
    segment when the head is full. A no-op when *parent* is not the live
    canonical epic of *kind* or *task* has no ``closedAt``:

    - **ad-hoc**: the name is the bare repo in the title (``Epic (ad hoc):
      <bare>``), which must be a real repo; its home is ``<org>/.github``.
      A stamped archive title or a non-repo special epic is left untouched.
    - **intake / planning / deferred / hoshin / operations**: the epic must be titled for its
      org, whose ``.github`` is its only home. Any other title is reported on
      stderr and skipped (never guessed at).

    *parent* is the chain's head, as :func:`rollup` resolves it, so its title
    and kind decide the drain whichever container *task* is in (spec §4.6 of
    epic vergil-project/.github#1609); the re-parent moves *task* out of that
    container.
    """
    title = _issue_title(parent)
    if kind is ADHOC:
        # Only the LIVE canonical ad-hoc epic drains; archives (stamped) are terminal.
        if archive_re(ADHOC).match(title):
            return
        # Derive the target repo from the epic's OWN bare name, not parent.repo:
        # a public repo's ad-hoc epic lives in <org>/.github, so parent.repo is
        # always ".github" and would misfile every public-repo child into one
        # ".github" bucket (#2709). Drain only for a canonical per-repo epic —
        # <bare> must be a real repo. A repo name has no space or "/", so reject
        # those fast (also skips non-repo special epics whose bare is a prose
        # description); then confirm via a direct existence probe (repo_exists,
        # not list membership).
        name = title.removeprefix(ADHOC.title_prefix)
        if " " in name or "/" in name or not github.repo_exists(parent.owner, name):
            return
    else:
        name = parent.owner
        if title != f"{kind.title_prefix}{name}":
            print(
                f"skipped {kind.key} drain of {task.slug}: parent {parent.slug} is titled "
                f"{title!r}, not the canonical {kind.title_prefix + name!r}",
                file=sys.stderr,
            )
            return
    closed_at = _issue_closed_at(task)
    if not closed_at:
        return
    # An ad-hoc archive homes where the repo's epics home (<org>/.github); a queue
    # archive is the sibling of its queue epic, in the queue epic's own repo.
    home = (
        resolve_epic_home(parent.owner, name) if kind is ADHOC else f"{parent.owner}/{parent.repo}"
    )
    # SIBLING RE-PARENT SITE: apply_standing_drain() moves closed children into
    # these same archives in the scheduled batch drain. Both sites must honour the
    # per-segment cap — this single-child path rolls via
    # ensure_writable_standing_archive(); if you change how segments fill or when
    # they roll, make the matching change there too. See apply_standing_drain().
    archive = ensure_writable_standing_archive(kind, home, name, quarter_of(closed_at))
    if archive != parent:
        reparent_child(archive, task)  # atomic move: single-parent safe (#2691)


@dataclass(frozen=True)
class ArchiveConversion:
    """A single legacy archive to convert: where it is and its new title."""

    ref: IssueRef
    old_title: str
    new_title: str


def plan_normalize_adhoc(org: str) -> list[ArchiveConversion]:
    """Every legacy-form archive in *org*, open or closed, to convert.

    Pure/read-only. Scans <org>/.github, the home of every repo's ad-hoc epic
    and archives. A legacy archive is an issue whose title matches
    :data:`_LEGACY_ADHOC_ARCHIVE_RE`; the unstamped live epic never matches.
    """
    home = f"{org}/.github"
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        "--label",
        "epic",
        "--label",
        "ad-hoc",
        "--state",
        "all",
        "--limit",
        str(LISTING_LIMIT),
        "--json",
        "number,title",
    )
    refuse_truncated(raw, LISTING_LIMIT, f"the epic + ad-hoc issues of {home}")
    conversions: list[ArchiveConversion] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        old_title = str(item.get("title", ""))
        if not _LEGACY_ADHOC_ARCHIVE_RE.match(old_title):
            continue
        new_title = ADHOC.archive_prefix + old_title.removeprefix(ADHOC.title_prefix)
        conversions.append(
            ArchiveConversion(IssueRef(org, ".github", int(item["number"])), old_title, new_title)
        )
    return conversions


def apply_normalize(conversions: list[ArchiveConversion]) -> None:
    """Convert each planned archive in place (retitle, +archive, -epic)."""
    for conv in conversions:
        _normalize_archive_in_place(conv.ref, conv.new_title)


def normalize_adhoc_archives(org: str, *, apply: bool) -> list[ArchiveConversion]:
    """Plan (and, when *apply*, execute) the org-wide legacy-archive conversion.

    Idempotent: an already-migrated archive is new-form, does not match the legacy
    recognizer, and is skipped — so re-running is a no-op.
    """
    conversions = plan_normalize_adhoc(org)
    if apply:
        apply_normalize(conversions)
    return conversions


# Labels marking an umbrella issue that is never itself a task: an ``epic`` (live
# or standing) or a per-quarter standing-epic ``archive``. Checked in this order,
# so an issue carrying both reads as the epic it still is.
_NON_TASK_LABELS = ("epic", "archive")


def non_task_kind_of_labels(labels: Collection[str]) -> str | None:
    """``"epic"`` or ``"archive"`` when *labels* mark an umbrella, not a task; else None.

    The lookup-free form of :func:`non_task_kind`, for a caller that already
    read the labels alongside other fields in one call (``vrg-git``'s
    branch-creation guard reads state and labels together, #3592).
    """
    return next((label for label in _NON_TASK_LABELS if label in labels), None)


def non_task_kind(ref: IssueRef) -> str | None:
    """``"epic"`` or ``"archive"`` when *ref* is an umbrella, not a task; else None."""
    return non_task_kind_of_labels(_labels(ref))


def non_task_ref_kind(ref: str, *, default_repo: str) -> str | None:
    """``"epic"`` / ``"archive"`` when the ref string *ref* names a non-task, else None.

    Single source of truth for "is this an epic or archive where a task is
    expected?" (#2842), shared by ``vrg-submit-pr``, ``vrg-pr-workflow
    report-ready``, ``vrg-epic-link --task`` and ``vrg-issue-create
    --blocked-by``. Self-scoping: an unparseable ref (e.g. a legacy issue with
    no resolvable repo) is never a non-task and returns None. A failed label
    lookup raises — it is never read as "a task".
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return None
    return non_task_kind(issue)


_NON_TASK_REFUSAL_TAILS = {
    "epic": (
        "an epic is closed by rollup when its tasks complete, never by a PR. "
        "Create the task under the epic first and use the task's number."
    ),
    "archive": "an archive only holds a standing epic's closed tasks. Use a task's number.",
}


def non_task_refusal_message(flag: str, ref: str, kind: str) -> str:
    """The one refusal text for an epic or archive given where a task is expected.

    *flag* names the option the value came in on (``--issue``, ``--task``,
    ``--blocked-by``); *kind* is a :func:`non_task_kind` result (``"epic"`` or
    ``"archive"``). Anything else is a caller bug and raises ``ValueError``.
    """
    tail = _NON_TASK_REFUSAL_TAILS.get(kind)
    if tail is None:
        raise ValueError(f"not a non-task kind: {kind!r}")
    return f"{flag} ({ref}) is an {kind}, not a task; {tail}"


# Labels marking a not-PR-workable *operational task* — one that is run and whose
# acceptance is a recorded ``Outcome:`` comment, not a merged PR. Extended as new
# operational kinds are added (e.g. ``deployment``). NOTE: ``retrospective`` is a
# labelled kind but is deliberately NOT operational — its acceptance IS a merged
# docs PR (publishing ``retrospective.md``), so PR tooling must accept it.
_OPERATIONAL_LABELS: set[str] = {"validation", "deployment"}


def is_operational(ref: IssueRef) -> bool:
    """True if *ref* carries any operational label (validation, deployment, …)."""
    return bool(_labels(ref) & _OPERATIONAL_LABELS)


def operational_kind(ref: IssueRef) -> str | None:
    """The operational label on *ref* (``"validation"`` / ``"deployment"``), or None."""
    kinds = _labels(ref) & _OPERATIONAL_LABELS
    return next(iter(kinds)) if kinds else None


def operational_labels() -> frozenset[str]:
    """The operational label set (validation, deployment, …) — the public view."""
    return frozenset(_OPERATIONAL_LABELS)


def is_operational_task(ref: str, *, default_repo: str) -> bool:
    """True if *ref* is an operational task, so PR tooling must refuse it.

    Single source of truth for "is this an operational task?", shared by
    ``vrg-submit-pr`` and ``vrg-pr-workflow report-ready``. An operational task
    (validation, deployment, …) is proven by *running* it and recording an
    ``Outcome:`` comment — it has no code PR — so the PR path is refused before
    any work begins. Self-scoping: an unparseable ref (e.g. a legacy issue with
    no resolvable repo) is never operational and returns False.
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return False
    return is_operational(issue)


def rollup(task: IssueRef) -> None:
    """Close *task*'s parent epic if the epic is finite and all children closed.

    A no-op unless the task has an ``epic``-labeled parent (the transition gate):
    legacy issues have no epic parent, so finalize never rolls them up. Every
    standing epic (every :data:`STANDING_KINDS` kind, :func:`standing_kind_of`) is
    perpetual and never auto-closes. Instead, the just-closed child of a **live**
    standing epic drains into its close-quarter archive of the same kind (the
    steady-state event path, :func:`drain_standing_child`). A parent that is
    itself an archive is terminal and left untouched. A proxy epic
    (:func:`is_proxy`) is never rolled up: it stays open until its primary closes.

    A close by an untrusted actor is not a close for the rollup (spec §5.2):
    before the drain or the epic's close, *task*'s close, and before the
    epic's close every other child's, must be trusted
    (:func:`~vergil_tooling.lib.trust.require_trusted_close`). An untrusted
    one is a stop before any write
    (:class:`~vergil_tooling.lib.trust.UntrustedContentError`).

    Chain-aware (spec §4.6 of epic vergil-project/.github#1609): the epic is
    the head of the chain *task* is in (:func:`parent_of`), whose title and
    kind decide the drain, from any container. A finite epic closes only when
    every item across its chain is closed, and then its whole chain closes,
    head first (:func:`~vergil_tooling.lib.epic_chain.close_chain`). Before
    judging, its orphaned continuations are adopted (:func:`_adopt_orphans`).
    A continuation closing rolls nothing up.
    """
    parent = parent_of(task)
    if parent is None or not is_epic(parent):
        return
    if is_continuation(_labels(task)):
        # close_chain closes a chain's continuations after its head: their
        # close events are not the close of an item.
        return
    labels = _labels(parent)
    kind = standing_kind_of(labels)
    if kind is None and is_proxy(labels):
        # A proxy lives as long as its primary: its own org's daily sweep closes
        # it once the primary is closed (vergil-project/.github#1417).
        return
    trust.require_trusted_close(_repo_slug(task), task.number)
    if kind is not None:
        drain_standing_child(kind, parent, task)
        return
    _adopt_orphans(parent)
    children = child_states(parent)
    if children and all(child.state == "CLOSED" for child in children):
        for child in children:
            if child.ref != task:
                trust.require_trusted_close(_repo_slug(child.ref), child.ref.number)
        print(f"Rolling up epic {parent.slug} — all child tasks closed.")
        epic_chain.close_chain(parent)


class OrphanedContinuationError(EpicRefusalError, RuntimeError):
    """*head* has orphaned continuations the rollup cannot adopt, so it is not judged."""

    def __init__(self, head: IssueRef, orphans: Sequence[IssueRef], reason: str) -> None:
        super().__init__(
            f"epic {head.slug} is not rolled up: its orphaned continuations "
            f"{', '.join(ref.slug for ref in orphans)} cannot be adopted, {reason}"
        )
        self.head = head
        self.orphans = tuple(orphans)


def _adopt_orphans(head: IssueRef) -> None:
    """Adopt *head*'s orphaned continuations into its chain before the rollup judges it.

    An item that closed inside an orphan fired no rollup the head could see,
    and an open item there must hold the epic open, so the rollup looks
    first (spec §4.6). Each orphan that passes the trust rules is linked
    into the tail's last slot
    (:func:`~vergil_tooling.lib.epic_chain.find_or_create_continuation`),
    lowest number first, and reported. One that fails them raises
    :class:`~vergil_tooling.lib.epic_chain.ChainDriftError`, so the epic is
    never closed past it. An orphan of a closed head is reported as drift and
    never adopted (closed is closed). While rollover is off nothing may link
    a continuation, so an orphan raises :class:`OrphanedContinuationError`.
    """
    orphans = epic_chain.find_orphans(head)
    if not orphans:
        return
    if _issue_state(head) == "CLOSED":
        for orphan in orphans:
            print(
                f"chain drift: {orphan.slug} is an orphaned continuation of the closed epic "
                f"{head.slug}; it is not adopted, since closed epics are not written to",
                file=sys.stderr,
            )
        return
    if not epic_chain.ROLLOVER_ENABLED:
        raise OrphanedContinuationError(
            head, orphans, "since rollover into a continuation is not enabled yet"
        )
    remaining = orphans
    for _ in range(len(orphans)):
        # Each call adopts the lowest-numbered orphan into the current tail.
        tail = epic_chain.chain_of(head)[-1]
        adopted = epic_chain.find_or_create_continuation(head, tail)
        print(f"adopted orphaned continuation {adopted.slug} into epic {head.slug}")
        remaining = epic_chain.find_orphans(head)
        if not remaining:
            return
    # More orphans than were found (a racing writer's), or one adoption missed.
    raise OrphanedContinuationError(
        head,
        remaining,
        f"since they were still outside the chain after {len(orphans)} "
        + ("adoption" if len(orphans) == 1 else "adoptions"),
    )


# Imported last: the chain layer imports this module, and the readers above
# reach it only at call time (epic vergil-project/.github#1609).
from vergil_tooling.lib import epic_chain  # noqa: E402
