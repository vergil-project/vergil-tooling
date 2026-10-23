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
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import github, release, remote_config
from vergil_tooling.lib.config import CONFIG_FILE
from vergil_tooling.lib.labels import ensure_labels

if TYPE_CHECKING:
    from collections.abc import Sequence


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
    closed child by its close-quarter (issue #2678).
    """

    ref: IssueRef
    state: str  # "OPEN" | "CLOSED"
    title: str = ""
    closed_at: str = ""  # ISO-8601 closedAt; "" when open


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

_PARENT_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      parent { number repository { name owner { login } } }
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
    accepting it.
    """
    results: Any = github.read_json(
        "search",
        "issues",
        f"Parent: {epic.slug}",
        "--owner",
        epic.owner,
        "--json",
        "number,state,title,closedAt,repository,body",
    )
    states: list[ChildState] = []
    for item in results if isinstance(results, list) else []:
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
        if "/" not in name_with_owner:
            continue
        if not _body_declares_parent(str(item.get("body") or ""), epic):
            continue
        owner, name = name_with_owner.split("/", 1)
        states.append(
            ChildState(
                ref=IssueRef(owner=owner, repo=name, number=int(item["number"])),
                state=str(item["state"]).upper(),
                title=str(item.get("title") or ""),
                closed_at=str(item.get("closedAt") or ""),
            )
        )
    return states


def child_states(epic: IssueRef) -> list[ChildState]:
    """All child tasks of *epic*: native sub-issues preferred, reflink fallback."""
    native = _native_child_states(epic)
    return native if native else _reflink_child_states(epic)


def render_blocked_by(deps: list[IssueRef]) -> str:
    """Render the ``Blocked-by:`` reflink lines for a validation task body.

    One ``Blocked-by: owner/repo#N`` line per dependency — the portable mirror of
    the ``Parent:`` sub-issue reflink. Empty *deps* yields the empty string.
    """
    return "".join(f"Blocked-by: {dep.slug}\n" for dep in deps)


def blockers_of(task: IssueRef) -> list[IssueRef]:
    """Deps *task* is blocked by, parsed from its ``Blocked-by:`` body reflinks.

    Storage is the portable body reflink — the mirror of the ``Parent:``
    sub-issue fallback — chosen by the storage spike (#2184) because GitHub's
    native issue dependencies are REST-only and out of the sanctioned tooling's
    reach. If native dependencies become reachable later, this is the one place
    that would prefer them over the reflink.
    """
    body = github.read_output("api", _issue_endpoint(task), "--jq", ".body") or ""
    return [
        IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
        for match in _BLOCKED_BY_RE.finditer(body)
    ]


def all_blockers_closed(task: IssueRef) -> bool:
    """True iff every blocker of *task* is CLOSED.

    No blockers means nothing holds *task*, so it is runnable — the empty case is
    vacuously True. Used by the validation-aware rollup to classify an open
    validation task as runnable (blockers closed) vs blocked.
    """
    return all(_issue_state(dep) == "CLOSED" for dep in blockers_of(task))


def native_parent_of(task: IssueRef) -> IssueRef | None:
    """*task*'s native sub-issue parent, or None — never the body ``Parent:`` line.

    The orphan sweep and the queue checks key off the native link only: a task
    whose sole link is a body ``Parent:`` reference is not attached on GitHub,
    so it must read as unparented here (unlike :func:`parent_of`).
    """
    data: Any = github.graphql(_PARENT_QUERY, id=_node_id(task))
    parent = ((data or {}).get("node") or {}).get("parent")
    if isinstance(parent, dict):
        return _ref_from_node(parent)
    return None


def parent_of(task: IssueRef) -> IssueRef | None:
    """The epic *task* belongs to: native parent preferred, reflink fallback."""
    native = native_parent_of(task)
    if native is not None:
        return native
    body = github.read_output("api", _issue_endpoint(task), "--jq", ".body")
    match = _PARENT_RE.search(body or "")
    if match:
        return IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
    return None


def add_child(epic: IssueRef, task: IssueRef) -> None:
    """Link *task* under *epic*. Reopen the epic first if it is closed.

    Adding a task to an already-closed finite epic must reopen it (the
    reopen-on-late-child rule); the later finalize rollup closes it again.
    """
    if _issue_state(epic) == "CLOSED":
        github.run("issue", "reopen", str(epic.number), "--repo", f"{epic.owner}/{epic.repo}")
    github.graphql(_ADD_SUBISSUE, parent=_node_id(epic), child=_node_id(task))


def remove_child(epic: IssueRef, task: IssueRef) -> None:
    """Unlink *task* from *epic* (remove the native sub-issue relationship)."""
    github.graphql(_REMOVE_SUBISSUE, parent=_node_id(epic), child=_node_id(task))


def reparent_child(new_parent: IssueRef, task: IssueRef) -> None:
    """Atomically move *task* from its current parent under *new_parent*.

    GitHub's native sub-issues enforce a single parent, so an add-before-remove
    re-parent is rejected while the child is still linked to its old parent
    ("Sub issue may only have one parent"). ``replaceParent: true`` performs the
    move in one call — no orphan window, no separate remove (issue #2691, proven
    live via #2677). Only ever targets the current-quarter archive, which is
    always open, so no reopen-if-closed is needed here.
    """
    github.graphql(_REPARENT_SUBISSUE, parent=_node_id(new_parent), child=_node_id(task))


def all_children_closed(epic: IssueRef) -> bool:
    """True iff *epic* has at least one child and all children are closed."""
    children = child_states(epic)
    return bool(children) and all(child.state == "CLOSED" for child in children)


def _labels(ref: IssueRef) -> set[str]:
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", f"{ref.owner}/{ref.repo}", "--json", "labels"
    )
    labels = (raw or {}).get("labels") or [] if isinstance(raw, dict) else []
    return {str(label.get("name", "")) for label in labels}


def is_epic(ref: IssueRef) -> bool:
    """True if *ref* carries the ``epic`` label (i.e. it is in the model)."""
    return "epic" in _labels(ref)


def qualify_epic_ref(ref: str, *, repo: str) -> IssueRef:
    """Parse an explicit epic ref, reading a bare ``#N`` against *repo*'s epic home.

    *repo* (``"owner/name"``) is the repo of the **task** the epic is for — never
    simply the cwd repo. Epics live in their epic home
    (:func:`resolve_epic_home`), so a bare ``#N`` names ``<org>/.github#N`` for a
    public task and ``<repo>#N`` for a self-homed private one (#3411). A fully
    qualified ``owner/repo#N`` is returned as written, with no network call.

    A bare ref probes repo visibility, so call it under
    ``github.target_org(<task owner>)``. Fail-loud: a visibility error propagates.
    """
    parsed = parse_issue_ref(ref, default_repo=repo)
    if not ref.strip().startswith("#"):
        return parsed
    home_owner, home_repo = resolve_epic_home(parsed.owner, parsed.repo).split("/", 1)
    return IssueRef(owner=home_owner, repo=home_repo, number=parsed.number)


def resolve_epic_ref(ref: str, *, repo: str) -> IssueRef:
    """Resolve an epic ref for a task in *repo*, accepting the standing sentinels.

    ``"adhoc"`` ensures *repo*'s ad-hoc epic exists in its epic home — creating
    it if absent and reusing it otherwise — via :func:`ensure_adhoc_epic`.
    ``"intake"`` and ``"planning"`` likewise ensure *repo*'s intake or planning
    queue epic in its queue home (:func:`ensure_intake_epic`,
    :func:`ensure_planning_epic`). Any other ref is parsed with
    :func:`qualify_epic_ref` (a bare ``#N`` reads against *repo*'s epic home)
    and validated to carry the ``epic`` label.
    """
    if ref == "adhoc":
        return ensure_adhoc_epic(repo)
    if ref == "intake":
        return ensure_intake_epic(repo)
    if ref == "planning":
        return ensure_planning_epic(repo)
    epic = qualify_epic_ref(ref, repo=repo)
    if not is_epic(epic):
        raise ValueError(f"{epic.slug} is not an epic (missing the 'epic' label)")
    return epic


def resolve_epic_home(org: str, target_repo: str) -> str:
    """Map an explicit *target_repo* (bare name) to its epic home ``"owner/repo"``.

    A public target homes centrally in ``<org>/.github`` (today's behavior). A
    private target with a public ``.github`` homes its epics in itself
    (self-contained). A private ``.github`` means the whole org is private, so
    everything routes to ``.github``. Fail-loud: visibility errors propagate
    (see :func:`github.repo_visibility`).
    """
    if target_repo == ".github":
        return f"{org}/.github"
    if github.is_public(f"{org}/{target_repo}"):
        return f"{org}/.github"
    if github.is_public(f"{org}/.github"):
        return f"{org}/{target_repo}"
    return f"{org}/.github"


@dataclass(frozen=True)
class StandingKind:
    """One kind of perpetual standing epic: ad-hoc, intake, or planning.

    ``key`` is the discriminating label that, alongside ``epic``, marks a live
    standing epic of this kind (and, alongside ``archive``, its per-quarter
    archives). ``body`` is a :meth:`str.format` template taking ``owner`` (the
    home's owner) and ``name`` (the ``<name>`` in the title); ``archive_body``
    takes the same plus ``quarter``. Standing epics are never rolled up: they
    stay open forever (see :func:`rollup`), and their closed children drain into
    per-quarter archives (see :func:`drain_standing_child`).
    """

    key: str
    title_prefix: str
    labels: tuple[str, str]
    archive_prefix: str
    archive_labels: tuple[str, str]
    body: str
    archive_body: str


ADHOC = StandingKind(
    key="ad-hoc",
    title_prefix="Epic (ad hoc): ",
    labels=("epic", "ad-hoc"),
    archive_prefix="Archive (ad hoc): ",
    archive_labels=("archive", "ad-hoc"),
    body=(
        "Perpetual umbrella for ad-hoc work in {owner}/{name}. Created and reused "
        "idempotently; tasks routed to the ad-hoc epic are linked here.\n"
    ),
    archive_body="Ad-hoc work in {owner}/{name} finished in {quarter}. Managed automatically.\n",
)
INTAKE = StandingKind(
    key="intake",
    title_prefix="Epic (intake): ",
    labels=("epic", "intake"),
    archive_prefix="Archive (intake): ",
    archive_labels=("archive", "intake"),
    body=(
        "Perpetual intake queue for {name}: every captured, unreviewed item and "
        "every orphan the sweep finds. Reviewed oldest first by intake-review.\n"
    ),
    archive_body="Intake items for {name} closed in {quarter}. Managed automatically.\n",
)
PLANNING = StandingKind(
    key="planning",
    title_prefix="Epic (planning): ",
    labels=("epic", "planning"),
    archive_prefix="Archive (planning): ",
    archive_labels=("archive", "planning"),
    body=(
        "Perpetual planning queue for {name}: items that need a full epic, each "
        "tracked through its epic-create session.\n"
    ),
    archive_body="Planning items for {name} closed in {quarter}. Managed automatically.\n",
)
STANDING_KINDS: tuple[StandingKind, ...] = (ADHOC, INTAKE, PLANNING)
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


def _find_epic_by_title(
    home: str,
    title: str,
    *,
    prefer_oldest: bool = False,
    labels: Sequence[str] = ("epic", "ad-hoc"),
) -> IssueRef | None:
    """Return the open issue in *home* carrying *labels* whose title is exactly *title*.

    Shared title search for the standing-epic and archive finders. Scoped to open
    issues in *home* carrying every one of *labels* (default ``epic`` +
    ``ad-hoc``); returns None when absent. On more than one match the default
    raises (ambiguous — name an explicit ref rather than guess), which is correct
    for a *live* standing epic where two rows is real corruption. When
    *prefer_oldest* is True the lowest-numbered match is returned instead: archive
    resolution tolerates the transient duplicate archives produced by the
    list-consistency race (#2698) and reuses the oldest.
    """
    owner, home_repo = home.split("/", 1)
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        *[arg for label in labels for arg in ("--label", label)],
        "--state",
        "open",
        "--json",
        "number,title",
    )
    rows = (
        [r for r in raw if isinstance(r, dict) and r.get("title") == title]
        if isinstance(raw, list)
        else []
    )
    if len(rows) > 1:
        if prefer_oldest:
            oldest = min(int(r["number"]) for r in rows)
            return IssueRef(owner=owner, repo=home_repo, number=oldest)
        nums = ", ".join(f"#{r['number']}" for r in rows)
        kind = labels[-1]
        raise ValueError(
            f"multiple {kind} epics titled {title!r} in {home} ({nums}) — pass an explicit --epic"
        )
    if rows:
        return IssueRef(owner=owner, repo=home_repo, number=int(rows[0]["number"]))
    return None


def ensure_adhoc_epic(target_repo: str) -> IssueRef:
    """Return *target_repo*'s ad-hoc epic in its resolved epic home, creating it if absent.

    The home is derived from *target_repo*'s visibility by
    :func:`resolve_epic_home`: a public repo homes its ad-hoc epic centrally in
    ``<org>/.github``; a private repo homes it in itself. One per repo, each
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


def ensure_standing_epic(kind: StandingKind, home: str, name: str) -> IssueRef:
    """Return the *kind* standing epic ``<title_prefix><name>`` in *home*, creating it if absent.

    Idempotent: an open issue in *home* carrying ``kind.labels`` with exactly
    that title is reused; none means ensure the labels exist and create it; two
    sharing the title is ambiguous and raises rather than guessing.
    """
    home_owner, home_repo = home.split("/", 1)
    title = f"{kind.title_prefix}{name}"
    found = _find_epic_by_title(home, title, labels=kind.labels)
    if found is not None:
        return found
    ensure_labels(home, kind.labels)
    url = github.create_issue(
        repo=home,
        title=title,
        body=kind.body.format(owner=home_owner, name=name),
        labels=list(kind.labels),
    )
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return IssueRef(owner=home_owner, repo=home_repo, number=number)


def queue_home(target_repo: str) -> tuple[str, str]:
    """Map *target_repo* (``"owner/repo"``) to its intake/planning queue home and name.

    Returns ``(home, name)``. Queues follow the epic-home rule
    (:func:`resolve_epic_home`): a repo homed in ``<org>/.github`` (every public
    repo, and ``.github`` itself) shares the org's queues, named for the org; a
    self-homed private repo keeps its own queues, named for its bare repo name.
    """
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for queue home (repo={target_repo!r})")
    owner, bare = target_repo.split("/", 1)
    home = resolve_epic_home(owner, bare)
    name = owner if home == f"{owner}/.github" else bare
    return home, name


def ensure_intake_epic(target_repo: str) -> IssueRef:
    """Return the intake queue epic for *target_repo*'s queue home, creating it if absent."""
    home, name = queue_home(target_repo)
    return ensure_standing_epic(INTAKE, home, name)


def ensure_planning_epic(target_repo: str) -> IssueRef:
    """Return the planning queue epic for *target_repo*'s queue home, creating it if absent.

    Also ensures the per-item ``in-progress`` label exists in that home, every
    time, so ``epic-create`` can apply it in any planning home.
    """
    home, name = queue_home(target_repo)
    epic = ensure_standing_epic(PLANNING, home, name)
    ensure_labels(home, (PLANNING_IN_PROGRESS_LABEL,))
    return epic


def resolve_orphan_epic(repo: str) -> IssueRef:
    """The epic an orphaned issue in *repo* is attached to: its intake queue epic."""
    return ensure_intake_epic(repo)


def standing_kind_of(labels: set[str]) -> StandingKind | None:
    """The :class:`StandingKind` a live standing epic with *labels* is, else None.

    A live standing epic carries ``epic`` plus its kind's key label; an archive
    (``archive`` + key, no ``epic``) is not a live standing epic.
    """
    if "epic" not in labels:
        return None
    return next((kind for kind in STANDING_KINDS if kind.key in labels), None)


def is_standing(labels: set[str]) -> bool:
    """True iff *labels* mark a perpetual standing epic (ad-hoc, intake, or planning)."""
    return standing_kind_of(labels) is not None


def queue_kind(epic: IssueRef) -> str | None:
    """``"intake"`` or ``"planning"`` when *epic* is a live queue epic, else None."""
    kind = standing_kind_of(_labels(epic))
    if kind is INTAKE or kind is PLANNING:
        return kind.key
    return None


def queue_item_kind(ref: str, *, default_repo: str) -> str | None:
    """``"intake"`` / ``"planning"`` when *ref*'s parent is a live queue epic, else None.

    The PR refusal's test (#2485): an item under the intake or planning epic is
    not buildable until it is routed out, so ``vrg-pr-workflow report-ready``
    and ``vrg-submit-pr`` refuse it. Keys off the **native** parent only
    (:func:`native_parent_of`), the same link intake review lists and the
    orphan migration writes. Self-scoping: an unparseable ref is never queued
    and returns None. A failed lookup raises — it is never read as "not queued".
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return None
    parent = native_parent_of(issue)
    if parent is None:
        return None
    return queue_kind(parent)


_QUEUE_REFUSAL_TAILS = {
    INTAKE.key: "it is not buildable until it is routed. Route it with intake-review first.",
    PLANNING.key: (
        "it is not buildable until its epic is expanded. Expand it with epic-create first."
    ),
}


def queue_refusal_message(ref: str, kind: str) -> str:
    """The one refusal text for a queue item, shared by every PR-path guard.

    *kind* is a :func:`queue_item_kind` result (``"intake"`` or ``"planning"``);
    anything else is a caller bug and raises ``ValueError``.
    """
    tail = _QUEUE_REFUSAL_TAILS.get(kind)
    if tail is None:
        raise ValueError(f"not a queue kind: {kind!r}")
    return f"--issue ({ref}) is in the {kind} queue; {tail}"


def is_legitimate_top_level(labels: set[str], title: str, body: str) -> bool:
    """True iff an issue may stand without an epic parent.

    The legitimate top-level kinds are epics (``epic``, including the standing
    epics), per-quarter archives (``archive``), and release tracking issues
    (:func:`release.is_release_tracking_issue`). Everything else needs a parent.
    """
    if "epic" in labels or "archive" in labels:
        return True
    return release.is_release_tracking_issue(title, body)


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
    """``(home, bare)`` for *target_repo*'s ad-hoc epic and archives (visibility-resolved)."""
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
    ensure_labels(f"{ref.owner}/{ref.repo}", ("archive",))
    github.run(
        "issue",
        "edit",
        str(ref.number),
        "--repo",
        f"{ref.owner}/{ref.repo}",
        "--title",
        new_title,
        "--add-label",
        "archive",
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
        "--json",
        "number,title",
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

    Each repo resolves its own epic home via :func:`drain_adhoc_repo` →
    :func:`plan_adhoc_drain` → :func:`resolve_epic_home`, so public
    (``.github``-homed) and private (self-homed) ad-hoc epics are both covered. A
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


# The queue kinds: one intake and one planning epic per queue home.
_QUEUE_KINDS: tuple[StandingKind, ...] = (INTAKE, PLANNING)


def _queue_homes(org: str) -> dict[str, str]:
    """Every intake/planning queue home in *org* mapped to its queue name.

    ``<org>/.github`` (named for the org) always, plus each private repo that
    self-homes its queues (named for its bare repo name) — the same rule
    :func:`queue_home` applies to a single repo. Insertion-ordered, deduped.
    """
    homes: dict[str, str] = {f"{org}/.github": org}
    for bare in github.list_org_repos(org):
        home, name = queue_home(f"{org}/{bare}")
        homes.setdefault(home, name)
    return homes


def drain_standing_org(org: str, *, apply: bool, now: datetime) -> list[DrainPlan]:
    """Drain the intake and planning epics of every queue home in *org*.

    Covers ``<org>/.github``'s pair and every private repo's self-homed pair
    (:func:`_queue_homes`). Each (kind, home) is planned and, when *apply*,
    executed via :func:`apply_standing_drain`. A corrupted home (e.g. two intake
    epics sharing a title) is reported on stderr and skipped — never aborting the
    sweep. Returns one plan per live queue epic. Ad-hoc epics are drained by
    :func:`drain_adhoc_org`, not here.
    """
    plans: list[DrainPlan] = []
    for home, name in _queue_homes(org).items():
        for kind in _QUEUE_KINDS:
            try:
                plan = plan_standing_drain(kind, home, name, now=now)
                if plan is not None and apply:
                    apply_standing_drain(kind, home, name, plan)
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
      <bare>``), which must be a real repo; its home is resolved by visibility.
      A stamped archive title or a non-repo special epic is left untouched.
    - **intake / planning**: the epic must be titled for its home — the org in
      ``<org>/.github``, the bare repo name in a self-homed private repo. Any
      other title is reported on stderr and skipped (never guessed at).
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
        # not list membership, so a self-homed private repo still passes).
        name = title.removeprefix(ADHOC.title_prefix)
        if " " in name or "/" in name or not github.repo_exists(parent.owner, name):
            return
    else:
        name = parent.owner if parent.repo == ".github" else parent.repo
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
    # An ad-hoc archive homes where the repo's epics home (by visibility); a queue
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
    """Every legacy-form archive across *org*, open or closed, to convert.

    Pure/read-only. Resolves each repo's own epic home (public → ``<org>/.github``,
    private → itself) the way :func:`drain_adhoc_org` does, deduping homes so a
    shared ``.github`` is scanned once. A legacy archive is an issue whose title
    matches :data:`_LEGACY_ADHOC_ARCHIVE_RE`; the unstamped live epic never matches.
    """
    homes: dict[str, tuple[str, str]] = {}
    for bare in github.list_org_repos(org):
        home = resolve_epic_home(org, bare)
        home_owner, home_repo = home.split("/", 1)
        homes[home] = (home_owner, home_repo)
    homes.setdefault(f"{org}/.github", (org, ".github"))
    conversions: list[ArchiveConversion] = []
    for home, (owner, home_repo) in homes.items():
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
            "500",
            "--json",
            "number,title",
        )
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            old_title = str(item.get("title", ""))
            if not _LEGACY_ADHOC_ARCHIVE_RE.match(old_title):
                continue
            new_title = ADHOC.archive_prefix + old_title.removeprefix(ADHOC.title_prefix)
            conversions.append(
                ArchiveConversion(
                    IssueRef(owner, home_repo, int(item["number"])), old_title, new_title
                )
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


def is_epic_linkage(ref: str, *, default_repo: str) -> bool:
    """True if *ref* points at an epic, so it must not be linked as a PR's task.

    Single source of truth for "is this linkage an epic?", shared by
    ``vrg-submit-pr`` and ``vrg-pr-workflow report-ready``. Self-scoping: an
    unparseable ref (e.g. a legacy issue with no resolvable repo) is never an
    epic and returns False.
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return False
    return is_epic(issue)


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
    standing epic (ad-hoc, intake, planning — :func:`standing_kind_of`) is
    perpetual and never auto-closes. Instead, the just-closed child of a **live**
    standing epic drains into its close-quarter archive of the same kind (the
    steady-state event path, :func:`drain_standing_child`). A parent that is
    itself an archive is terminal and left untouched.
    """
    parent = parent_of(task)
    if parent is None or not is_epic(parent):
        return
    kind = standing_kind_of(_labels(parent))
    if kind is not None:
        drain_standing_child(kind, parent, task)
        return
    if all_children_closed(parent):
        print(f"Rolling up epic {parent.slug} — all child tasks closed.")
        github.run("issue", "close", str(parent.number), "--repo", f"{parent.owner}/{parent.repo}")
