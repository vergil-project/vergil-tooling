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

from vergil_tooling.lib import github
from vergil_tooling.lib.labels import load_labels

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


def parent_of(task: IssueRef) -> IssueRef | None:
    """The epic *task* belongs to: native parent preferred, reflink fallback."""
    data: Any = github.graphql(_PARENT_QUERY, id=_node_id(task))
    parent = ((data or {}).get("node") or {}).get("parent")
    if isinstance(parent, dict):
        return _ref_from_node(parent)
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


def resolve_epic_ref(ref: str, *, repo: str) -> IssueRef:
    """Resolve an epic ref, accepting the ``"adhoc"`` sentinel.

    ``"adhoc"`` ensures *repo*'s ad-hoc epic exists in ``<org>/.github`` —
    creating it if absent and reusing it otherwise — via
    :func:`ensure_adhoc_epic`. Any other ref is parsed with
    :func:`parse_issue_ref` and validated to carry the ``epic`` label.
    """
    if ref == "adhoc":
        return ensure_adhoc_epic(repo)
    epic = parse_issue_ref(ref, default_repo=repo)
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


_ADHOC_EPIC_TITLE_PREFIX = "Epic (ad hoc): "
_ADHOC_EPIC_LABELS = ("epic", "ad-hoc")
_ADHOC_ARCHIVE_TITLE_PREFIX = "Archive (ad hoc): "
_ADHOC_ARCHIVE_LABELS = ("archive", "ad-hoc")
_ADHOC_EPIC_BODY = (
    "Perpetual umbrella for ad-hoc work in {repo}. Created and reused "
    "idempotently; tasks routed to the ad-hoc epic are linked here.\n"
)
# A stamped per-quarter archive: "Archive (ad hoc): <bare> — <YYYY>-Qn". The
# separator is a space, U+2014 em-dash, space. Matching this distinguishes a
# terminal archive from the live canonical ad-hoc epic (which has no stamp). A
# busy quarter overflows GitHub's 100-sub-issue cap into further segments stamped
# " (N)" for N≥2 (issue #2872); the optional segment group captures that suffix
# (absent → segment 1, the unsuffixed original form, so existing archives match).
_ADHOC_ARCHIVE_RE = re.compile(
    r"^Archive \(ad hoc\): (?P<bare>.+) — (?P<quarter>\d{4}-Q[1-4])(?: \((?P<segment>\d+)\))?$"
)
# GitHub rejects a 101st sub-issue, so an archive segment is treated as full at
# this count and the drain rolls to the next segment. The 5-slot margin below the
# hard 100 cap absorbs races between concurrent drain runs near the boundary.
_ADHOC_ARCHIVE_SEGMENT_CAP = 95
# Legacy pre-rename archive title ("Epic (ad hoc): <bare> — <YYYY>-Qn"), used
# ONLY by the self-healing creation path and the normalize sweep to find
# archives still in the old form. Steady-state code keys off _ADHOC_ARCHIVE_RE.
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
    """Return the open ``ad-hoc`` epic in *home* whose title is exactly *title*.

    Shared title search for the ad-hoc epic finders. Scoped to open ``epic`` +
    ``ad-hoc`` issues in *home*; returns None when absent. On more than one match
    the default raises (ambiguous — name an explicit ref rather than guess), which
    is correct for the *live* ad-hoc epic where two rows is real corruption. When
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
        raise ValueError(
            f"multiple ad-hoc epics titled {title!r} in {home} ({nums}) — pass an explicit --epic"
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
    home = resolve_epic_home(owner, bare)
    home_repo = home.split("/", 1)[1]
    title = f"{_ADHOC_EPIC_TITLE_PREFIX}{bare}"
    found = _find_epic_by_title(home, title)
    if found is not None:
        return found
    _ensure_labels(home, _ADHOC_EPIC_LABELS)
    url = github.create_issue(
        repo=home,
        title=title,
        body=_ADHOC_EPIC_BODY.format(repo=target_repo),
        labels=list(_ADHOC_EPIC_LABELS),
    )
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return IssueRef(owner=owner, repo=home_repo, number=number)


def find_adhoc_epic(target_repo: str) -> IssueRef | None:
    """Return the live canonical ad-hoc epic for *target_repo*, or None.

    Unlike :func:`ensure_adhoc_epic`, this never creates: it is the read-only
    lookup the drain uses to find the epic whose closed children it archives.
    """
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for ad-hoc epic (repo={target_repo!r})")
    owner, bare = target_repo.split("/", 1)
    home = resolve_epic_home(owner, bare)
    return _find_epic_by_title(home, f"{_ADHOC_EPIC_TITLE_PREFIX}{bare}")


def _ensure_labels(repo: str, names: Sequence[str]) -> None:
    """Create each of *names* in *repo* if absent, from the canonical registry.

    The ad-hoc/archive machinery labels issues with ``archive``/``ad-hoc``/
    ``epic``; in an org whose ``.github`` was never label-synced these labels do
    not exist, and ``gh`` refuses to apply a missing one (``'archive' not
    found``). Creating them on demand from ``labels.json`` — idempotent
    ``label create --force`` with the canonical colour and description — makes
    archive creation and normalization self-healing in every org, not only the
    migrated one. This is the root cause of the cross-org epic-rollup failures
    (vergil-project/.github#305): only ``vergil-project/.github`` had the
    ``archive`` label, so every other org's ``on: issues.closed`` rollup crashed.
    """
    specs = {entry["name"]: entry for entry in load_labels()["labels"]}
    for name in names:
        spec = specs[name]
        github.run(
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--force",
            "--color",
            spec["color"],
            "--description",
            spec["description"],
        )


def _normalize_archive_in_place(ref: IssueRef, new_title: str) -> None:
    """Convert a legacy-form archive to the new form: retitle, +archive, -epic.

    Keeps ``ad-hoc``. Works on open or closed issues (``gh issue edit`` permits
    editing a closed issue's title and labels). Ensures the ``archive`` label
    exists first, so a not-yet-migrated org's ``.github`` self-heals instead of
    failing with ``'archive' not found``.
    """
    _ensure_labels(f"{ref.owner}/{ref.repo}", ("archive",))
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


def _archive_title(bare: str, quarter: str, segment: int) -> str:
    """Title of *bare*'s ``quarter`` archive segment.

    Segment 1 is the original unsuffixed form (so archives created before
    segmentation keep their exact title, no migration); segment N≥2 appends
    ``" (N)"`` — the overflow buckets for a quarter past GitHub's 100-sub-issue
    cap (issue #2872).
    """
    base = f"{_ADHOC_ARCHIVE_TITLE_PREFIX}{bare} — {quarter}"
    return base if segment <= 1 else f"{base} ({segment})"


def _segment_placements(
    start_segment: int, start_occupancy: int, num_children: int, cap: int
) -> list[int]:
    """Segment number for each of *num_children* successive children.

    Fills *start_segment* (already holding *start_occupancy* children), rolling to
    the next segment each time one reaches *cap*. Pure — the read-then-mutate fill
    core of :func:`apply_adhoc_drain`, split out so the roll logic is testable
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


def ensure_adhoc_archive(target_repo: str, quarter: str, segment: int = 1) -> IssueRef:
    """Return *target_repo*'s ``— <quarter>`` archive *segment*, creating it if absent.

    The archive is the stamped sibling of the live ad-hoc epic — same home,
    labelled ``archive`` + ``ad-hoc`` — into which closed children of *quarter*
    are re-parented. *segment* selects the overflow bucket: 1 is the original
    unsuffixed archive, N≥2 the ``" (N)"``-suffixed spillover for a quarter that
    exceeds GitHub's 100-sub-issue cap (issue #2872). Self-healing and idempotent:
    an existing new-form archive is reused; else — **for segment 1 only** — a
    legacy-form archive (old ``Epic (ad hoc): … — Qn`` title, ``epic`` +
    ``ad-hoc``) is normalized in place and returned; else a fresh new-form archive
    is created. This makes creation race-free regardless of whether the bulk
    normalize sweep has run. Pre-existing duplicate archives (the list-consistency
    race, #2698) collapse to the oldest.
    """
    owner, bare = target_repo.split("/", 1)
    home = resolve_epic_home(owner, bare)
    home_repo = home.split("/", 1)[1]
    title = _archive_title(bare, quarter, segment)
    existing = _find_epic_by_title(home, title, prefer_oldest=True, labels=_ADHOC_ARCHIVE_LABELS)
    if existing is not None:
        return existing
    if segment <= 1:
        legacy_title = f"{_ADHOC_EPIC_TITLE_PREFIX}{bare} — {quarter}"
        legacy = _find_epic_by_title(home, legacy_title, prefer_oldest=True)
        if legacy is not None:
            _normalize_archive_in_place(legacy, title)
            return legacy
    _ensure_labels(home, _ADHOC_ARCHIVE_LABELS)
    url = github.create_issue(
        repo=home,
        title=title,
        body=f"Ad-hoc work in {target_repo} finished in {quarter}. Managed automatically.\n",
        labels=list(_ADHOC_ARCHIVE_LABELS),
    )
    return IssueRef(owner=owner, repo=home_repo, number=int(url.rstrip("/").rsplit("/", 1)[-1]))


def _adhoc_archive_segments(home: str, bare: str, quarter: str) -> list[tuple[int, IssueRef]]:
    """Open archive segments for *bare*'s *quarter* in *home*, sorted by segment.

    Returns ``(segment_number, ref)`` pairs for every open ``archive``-labelled
    issue whose title matches *bare* + *quarter* (segment absent → 1). Used by the
    drain to find a quarter's current write head before filling (issue #2872).
    """
    owner, home_repo = home.split("/", 1)
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        *[arg for label in _ADHOC_ARCHIVE_LABELS for arg in ("--label", label)],
        "--state",
        "open",
        "--json",
        "number,title",
    )
    out: list[tuple[int, IssueRef]] = []
    for r in raw if isinstance(raw, list) else []:
        m = _ADHOC_ARCHIVE_RE.match(str(r.get("title", ""))) if isinstance(r, dict) else None
        if m and m.group("bare") == bare and m.group("quarter") == quarter:
            segment = int(m.group("segment")) if m.group("segment") else 1
            out.append((segment, IssueRef(owner, home_repo, int(r["number"]))))
    return sorted(out, key=lambda pair: pair[0])


def list_open_adhoc_archives(home: str) -> list[tuple[IssueRef, str]]:
    """Open ``ad-hoc`` archive epics in *home* with their ``YYYY-Qn`` quarter.

    Only stamped archives (title carrying a ``— YYYY-Qn`` suffix) are returned;
    the live canonical ad-hoc epic (no stamp) is skipped. Used to find archives
    whose quarter is now past and should be closed.
    """
    owner, home_repo = home.split("/", 1)
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        *[arg for label in _ADHOC_ARCHIVE_LABELS for arg in ("--label", label)],
        "--state",
        "open",
        "--json",
        "number,title",
    )
    out: list[tuple[IssueRef, str]] = []
    for r in raw if isinstance(raw, list) else []:
        m = _ADHOC_ARCHIVE_RE.match(str(r.get("title", ""))) if isinstance(r, dict) else None
        if m:
            out.append((IssueRef(owner, home_repo, int(r["number"])), m.group("quarter")))
    return out


@dataclass(frozen=True)
class DrainPlan:
    """A per-repo ad-hoc drain: what to re-parent and what to close.

    ``live`` is the canonical (unstamped) ad-hoc epic. ``moves`` pairs each
    closed child with its close-quarter (``YYYY-Qn``) archive bucket. ``close``
    lists open archive epics whose quarter is now past and should be closed.
    """

    live: IssueRef
    moves: list[tuple[IssueRef, str]]  # (closed child, its close-quarter)
    close: list[IssueRef]  # open archives whose quarter is now past


def plan_adhoc_drain(target_repo: str, *, now: datetime) -> DrainPlan | None:
    """Plan *target_repo*'s ad-hoc drain, or None if it has no live ad-hoc epic.

    Buckets each closed child of the live epic by its close-quarter and lists the
    open archive epics whose quarter is strictly before the current quarter (so
    they are eligible to close). Pure/read-only: no mutation happens here.
    """
    live = find_adhoc_epic(target_repo)
    if live is None:
        return None
    moves = [
        (c.ref, quarter_of(c.closed_at))
        for c in child_states(live)
        if c.state == "CLOSED" and c.closed_at
    ]
    owner, bare = target_repo.split("/", 1)
    home = resolve_epic_home(owner, bare)
    cur = current_quarter(now)
    close = [ref for ref, q in list_open_adhoc_archives(home) if q < cur]
    return DrainPlan(live=live, moves=moves, close=close)


def _archive_child_count(archive: IssueRef) -> int:
    """Number of native sub-issues under *archive* (capped at 100 by the query).

    The occupancy read the drain uses to decide whether a quarter's write-head
    segment still has room before GitHub's 100-sub-issue cap (issue #2872). The
    query fetches the first 100, which is exactly the bound that matters: a
    full archive reports 100 and the drain rolls to the next segment.
    """
    return len(_native_child_states(archive))


def _adhoc_archive_head(target_repo: str, quarter: str) -> tuple[int, IssueRef]:
    """The current write-head segment for (*target_repo*, *quarter*): ``(segment, ref)``.

    The highest existing archive segment for the quarter, or a freshly ensured
    segment 1 when none exist yet. Shared by the single-child event path
    (:func:`ensure_writable_adhoc_archive`) and the batch drain
    (:func:`apply_adhoc_drain`) so both resolve the head identically (issue #2872).
    """
    owner, bare = target_repo.split("/", 1)
    home = resolve_epic_home(owner, bare)
    segments = dict(_adhoc_archive_segments(home, bare, quarter))
    if segments:
        head = max(segments)
        return head, segments[head]
    return 1, ensure_adhoc_archive(target_repo, quarter, segment=1)


def ensure_writable_adhoc_archive(target_repo: str, quarter: str) -> IssueRef:
    """The quarter's archive segment with room for one more child, rolling if full.

    Single-child entry point (the ``on: issues.closed`` :func:`rollup` path):
    resolve the write-head segment and, if it has reached
    :data:`_ADHOC_ARCHIVE_SEGMENT_CAP`, create and return the next segment so the
    child never lands in a full archive — GitHub rejects a 101st sub-issue (issue
    #2872). The batch drain does not use this; it fills across segments in one pass
    via :func:`_segment_placements`.
    """
    head_segment, head_ref = _adhoc_archive_head(target_repo, quarter)
    if _archive_child_count(head_ref) >= _ADHOC_ARCHIVE_SEGMENT_CAP:
        return ensure_adhoc_archive(target_repo, quarter, segment=head_segment + 1)
    return head_ref


def apply_adhoc_drain(target_repo: str, plan: DrainPlan) -> None:
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
    by_quarter: dict[str, list[IssueRef]] = {}
    for child, quarter in plan.moves:
        by_quarter.setdefault(quarter, []).append(child)
    for quarter, children in by_quarter.items():
        head_segment, head_ref = _adhoc_archive_head(target_repo, quarter)
        cache: dict[int, IssueRef] = {head_segment: head_ref}
        occupancy = _archive_child_count(head_ref)
        placements = _segment_placements(
            head_segment, occupancy, len(children), _ADHOC_ARCHIVE_SEGMENT_CAP
        )
        for child, segment in zip(children, placements, strict=True):
            archive = cache.get(segment)
            if archive is None:
                archive = ensure_adhoc_archive(target_repo, quarter, segment=segment)
                cache[segment] = archive
            if archive == plan.live:
                continue  # defensive: never re-parent into the live epic
            reparent_child(archive, child)
    for archive in plan.close:
        github.run(
            "issue", "close", str(archive.number), "--repo", f"{archive.owner}/{archive.repo}"
        )


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
    :func:`find_adhoc_epic` → :func:`resolve_epic_home`, so public
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
            new_title = _ADHOC_ARCHIVE_TITLE_PREFIX + old_title.removeprefix(
                _ADHOC_EPIC_TITLE_PREFIX
            )
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
    legacy issues have no epic parent, so finalize never rolls them up. An
    ``ad-hoc`` epic is perpetual and never auto-closes; instead, when the
    just-closed child's parent is the **live** ad-hoc epic, drain that one child
    into its close-quarter archive (the steady-state event path). A parent that
    is itself a stamped archive is terminal and left untouched.
    """
    parent = parent_of(task)
    if parent is None or not is_epic(parent):
        return
    if "ad-hoc" in _labels(parent):
        title = _issue_title(parent)
        # Only the LIVE canonical ad-hoc epic drains; archives (stamped) are terminal.
        if _ADHOC_ARCHIVE_RE.match(title):
            return
        # Derive the target repo from the epic's OWN bare name, not parent.repo:
        # a public repo's ad-hoc epic lives in <org>/.github, so parent.repo is
        # always ".github" and would misfile every public-repo child into one
        # ".github" bucket (#2709). Drain only for a canonical per-repo epic —
        # <bare> must be a real repo. A repo name has no space or "/", so reject
        # those fast (also skips non-repo special epics whose bare is a prose
        # description); then confirm via a direct existence probe (repo_exists,
        # not list membership, so a self-homed private repo still passes).
        bare = title.removeprefix(_ADHOC_EPIC_TITLE_PREFIX)
        if " " in bare or "/" in bare or not github.repo_exists(parent.owner, bare):
            return
        closed_at = _issue_closed_at(task)
        if not closed_at:
            return
        archive = ensure_writable_adhoc_archive(f"{parent.owner}/{bare}", quarter_of(closed_at))
        if archive != parent:
            reparent_child(archive, task)  # atomic move: single-parent safe (#2691)
        return
    if all_children_closed(parent):
        print(f"Rolling up epic {parent.slug} — all child tasks closed.")
        github.run("issue", "close", str(parent.number), "--repo", f"{parent.owner}/{parent.repo}")
