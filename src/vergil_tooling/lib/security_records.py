"""Security records: private draft advisories paired with neutral issues.

Every security finding gets one **pair** (epic vergil-project/.github#517):

- a **draft repository security advisory** in ``<org>/.github`` that holds the
  details, with one ``Tracking issue: <owner>/<repo>#<N>`` line per issue it
  tracks, and the affected repo as its ``other``-ecosystem package name;
- a neutral ``security``-labelled **issue** carrying the line
  ``Security advisory: <org>/.github <GHSA-id>``.

One issue may name several advisories, one line each (epic
vergil-project/.github#573); it keeps the ``security`` label while any line
remains.

This module owns the advisories REST API, the two reference lines, pair
classification and :func:`retarget`, the one way a link moves.

**Output rule.** An advisory's ``summary`` and ``description`` reach a caller
only through :func:`read_text`. :class:`Advisory` has no text field, the list
reads project the text away with ``--jq`` before it reaches stdout, and every
advisory helper re-raises a ``gh`` failure **without its stdout**, so no error
message can echo advisory text. Everything else names only GHSA IDs, issue
refs, labels and states.

The advisory create is sent **exactly once** (:func:`github.post_json_once`):
drafts cannot be deleted, so a retried create could leave a second record.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import epics, github, labels
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

SECURITY_LABEL = "security"
DISCLOSED_LABEL = "security-disclosed"
SEVERITIES = ("critical", "high", "medium", "low")

_GHSA = r"GHSA(?:-[0-9a-z]{4}){3}"
_NAME = r"[A-Za-z0-9._-]+"

# Each reference line is exact and alone on its line; anything else (a quoted
# line, a mention mid-sentence, a repo other than .github) is not a reference.
_ADVISORY_RE = re.compile(rf"^Security advisory: ({_NAME})/\.github ({_GHSA})[ \t]*\r?$", re.M)
_TRACKING_RE = re.compile(rf"^Tracking issue: ({_NAME})/({_NAME})#([0-9]+)[ \t]*\r?$", re.M)

# The same tracking-line test, applied by gh's jq to each page so only the
# reference lines (never the rest of the description) reach stdout.
_JQ_TRACKING_TEST = r"^Tracking issue: [A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+[ \t]*$"
_LIST_JQ = (
    "[.[] | {ghsa_id, state,"
    " affected: ((.vulnerabilities // []) | map(.package.name // empty) | first),"
    " submission: (.submission != null),"
    ' tracking: [(.description // "") | split("\\n")[] | rtrimstr("\\r")'
    f' | select(test("{_JQ_TRACKING_TEST}"))]}}]'
)
_DUPLICATE_JQ = (
    "[.[] | {ghsa_id, summary, names: [(.vulnerabilities // [])[] | .package.name // empty]}]"
)
_ISSUE_JQ = '{labels: [.labels[].name], body: (.body // "")}'
_SECURITY_ISSUES_JQ = '[.[] | select(.pull_request == null) | {number, state, body: (.body // "")}]'


class PrivateHomeError(RuntimeError):
    """The org's ``.github`` repo is not public, so it cannot hold security records."""


class RetargetError(RuntimeError):
    """A :func:`retarget` step failed; the message names the GHSA ID and the step."""


@dataclass(frozen=True)
class Advisory:
    """One advisory as the list reads see it: IDs and states, never its text.

    It deliberately has **no** ``summary`` or ``description`` field, so a
    caller holding an ``Advisory`` cannot print the text by mistake.
    """

    ghsa_id: str
    state: str  # triage | draft | published | closed | withdrawn
    affected: str | None  # the first ``other``-ecosystem package name
    tracking: tuple[IssueRef, ...]
    external: bool  # came from private vulnerability reporting


@dataclass(frozen=True)
class SecurityIssue:
    """One ``security``-labelled issue, open or closed, and every advisory it names.

    ``ghsa_ids`` is in body order and empty when the body has no line.
    """

    ref: IssueRef
    open: bool
    ghsa_ids: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    """The pair check's verdict: consistent pairs, external reports, broken pairs.

    ``broken`` holds one-line reasons naming only GHSA IDs and issue refs. An
    advisory with any broken tracking line is reported there and left out of
    ``paired``, so nothing acts on a pair whose links are wrong.
    """

    paired: list[Advisory]
    external: list[Advisory]
    broken: list[str]


def _scrubbed(exc: github.GitHubAPIError) -> github.GitHubAPIError:
    """*exc* without its stdout: the exit status, the command and stderr only."""
    cls = (
        github.MissingGitHubTokenError
        if isinstance(exc, github.MissingGitHubTokenError)
        else github.GitHubAPIError
    )
    return cls(exc.returncode, exc.cmd, None, exc.stderr)


@contextlib.contextmanager
def _no_text() -> Generator[None]:
    """Re-raise any ``gh`` failure without its stdout, which may hold advisory text."""
    try:
        yield
    except github.GitHubAPIError as exc:
        raise _scrubbed(exc) from None


def _advisories(home: str) -> str:
    return f"repos/{home}/security-advisories"


def _issue_endpoint(ref: IssueRef) -> str:
    return f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"


def advisory_home(org: str) -> str:
    """Return ``"<org>/.github"``, the advisory home, or raise :class:`PrivateHomeError`.

    Advisories are documented for public repos only; a private home is refused
    loudly, never worked around by falling back to issue-only records.
    """
    home = f"{org}/.github"
    if github.repo_visibility(home) != "PUBLIC":
        raise PrivateHomeError(
            f"vrg-security-record: {home} is not public; security records need a "
            "public org .github (private orgs are unsupported)"
        )
    return home


def advisory_line(home: str, ghsa_id: str) -> str:
    """The issue-body line naming an advisory: ``Security advisory: <home> <GHSA>``."""
    return f"Security advisory: {home} {ghsa_id}"


def tracking_line(ref: IssueRef) -> str:
    """The advisory-description line naming a tracked issue."""
    return f"Tracking issue: {ref.slug}"


def parse_advisory_refs(issue_body: str) -> tuple[str, ...]:
    """Every GHSA ID the exact ``Security advisory:`` lines name, in order, without repeats."""
    return tuple(dict.fromkeys(match.group(2) for match in _ADVISORY_RE.finditer(issue_body)))


def parse_advisory_ref(issue_body: str) -> str | None:
    """The GHSA ID the first exact ``Security advisory:`` line names, or None.

    Kept only for the intake-routing and planning-hold callers until they move
    to :func:`parse_advisory_refs` (epic vergil-project/.github#573, Task 2).
    """
    refs = parse_advisory_refs(issue_body)
    return refs[0] if refs else None


def parse_tracking(description: str) -> tuple[IssueRef, ...]:
    """Every issue the exact ``Tracking issue:`` lines name, in order, without repeats."""
    refs = (
        IssueRef(match.group(1), match.group(2), int(match.group(3)))
        for match in _TRACKING_RE.finditer(description)
    )
    return tuple(dict.fromkeys(refs))


def create_advisory(
    home: str, *, summary: str, details: str, affected_repo: str, severity: str
) -> str:
    """Create a draft advisory in *home* and return its GHSA ID.

    The POST is sent exactly once. It never publishes, never requests a CVE
    and never asks for a private fork. On failure the caller decides (the
    duplicate guard, :func:`find_duplicate`, covers a create whose response
    was lost).
    """
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}: {severity!r}")
    body: dict[str, object] = {
        "summary": summary,
        "description": details,
        "severity": severity,
        "vulnerabilities": [{"package": {"ecosystem": "other", "name": _bare(affected_repo)}}],
    }
    with _no_text():
        payload = github.post_json_once(_advisories(home), body)
    ghsa_id = payload.get("ghsa_id")
    if not isinstance(ghsa_id, str) or not ghsa_id:
        raise RuntimeError(f"advisory create in {home} returned no ghsa_id")
    return ghsa_id


def _bare(repo: str) -> str:
    """The bare name of ``owner/name`` (a bare name is returned unchanged)."""
    return repo.rsplit("/", 1)[-1]


def find_duplicate(home: str, *, summary: str, affected_repo: str) -> str | None:
    """The GHSA ID of a draft in *home* with this *summary* and affected repo, or None.

    The summaries are compared here and never returned or printed.
    """
    with _no_text():
        rows = github.read_json_paginated(f"{_advisories(home)}?state=draft", jq=_DUPLICATE_JQ)
    bare = _bare(affected_repo)
    for row in rows:
        item: Any = row
        if item.get("summary") == summary and bare in (item.get("names") or []):
            return str(item["ghsa_id"])
    return None


def _advisory_from_row(row: Any) -> Advisory:
    ghsa_id, state = row.get("ghsa_id"), row.get("state")
    if not isinstance(ghsa_id, str) or not isinstance(state, str):
        raise ValueError("advisory list row lacks a ghsa_id or state")
    affected = row.get("affected")
    lines = [str(line) for line in row.get("tracking") or []]
    return Advisory(
        ghsa_id=ghsa_id,
        state=state,
        affected=affected if isinstance(affected, str) else None,
        tracking=parse_tracking("\n".join(lines)),
        external=state == "triage" or bool(row.get("submission")),
    )


def list_advisories(home: str) -> list[Advisory]:
    """Every advisory in *home*, in every state, across every page.

    The fetch projects each page to the GHSA ID, state, package name, whether
    a ``submission`` exists and the ``Tracking issue:`` lines, so no summary or
    description text reaches stdout.
    """
    with _no_text():
        rows = github.read_json_paginated(_advisories(home), jq=_LIST_JQ)
    return [_advisory_from_row(row) for row in rows]


def read_text(home: str, ghsa_id: str) -> tuple[str, str]:
    """Return an advisory's ``(summary, description)``.

    The only path to an advisory's text; for ``show``, ``list --details`` and
    :func:`retarget`'s rewrite, never for anything that prints to a log.
    """
    with _no_text():
        raw: Any = github.read_json(
            "api", f"{_advisories(home)}/{ghsa_id}", "--jq", "{summary, description}"
        )
    summary, description = raw.get("summary"), raw.get("description") or ""
    if not isinstance(summary, str) or not isinstance(description, str):
        raise ValueError(f"advisory {ghsa_id} read returned no summary or description")
    return summary, description


def _write_description(home: str, ghsa_id: str, description: str) -> None:
    with _no_text():
        github.write_json("PATCH", f"{_advisories(home)}/{ghsa_id}", {"description": description})


def set_state(home: str, ghsa_id: str, state: Literal["draft", "closed"]) -> None:
    """Set an advisory's state to ``draft`` or ``closed`` (never ``published``)."""
    if state not in ("draft", "closed"):
        raise ValueError(f"advisory state must be draft or closed: {state!r}")
    with _no_text():
        github.write_json("PATCH", f"{_advisories(home)}/{ghsa_id}", {"state": state})


def _issue_labels_body(ref: IssueRef) -> tuple[set[str], str]:
    raw: Any = github.read_json("api", _issue_endpoint(ref), "--jq", _ISSUE_JQ)
    return {str(name) for name in raw.get("labels") or []}, str(raw.get("body") or "")


def _append_line(text: str, line: str) -> str:
    stripped = text.rstrip()
    return f"{stripped}\n\n{line}" if stripped else line


def mark_issue(ref: IssueRef, home: str, ghsa_id: str) -> None:
    """Give *ref* the ``security`` label and the advisory line. Idempotent."""
    labels.ensure_labels(f"{ref.owner}/{ref.repo}", [SECURITY_LABEL])
    present, body = _issue_labels_body(ref)
    if SECURITY_LABEL not in present:
        github.write_json("POST", f"{_issue_endpoint(ref)}/labels", {"labels": [SECURITY_LABEL]})
    line = advisory_line(home, ghsa_id)
    if line not in (candidate.rstrip() for candidate in body.splitlines()):
        github.write_json("PATCH", _issue_endpoint(ref), {"body": _append_line(body, line)})


def _moved_comment(moved_to: Sequence[IssueRef]) -> str:
    return f"Security record moved to {', '.join(ref.slug for ref in moved_to)}"


def unmark_issue(ref: IssueRef, ghsa_id: str, moved_to: Sequence[IssueRef]) -> None:
    """Drop *ref*'s line naming *ghsa_id*; say where it moved.

    An issue may name several advisories (:func:`parse_advisory_refs`). If no
    line of *ref*'s body names *ghsa_id* — it names only others, or none — the
    issue is left untouched: no body edit, no label change, no comment.
    Otherwise only the line naming *ghsa_id* is removed, and the ``security``
    label goes too only when no advisory line is left after the removal.

    The comment goes first and the line and label then go in **one** PATCH, so
    an interrupted unmark never leaves the issue with only one of the two: it
    either still carries both (and :func:`security_issues` still finds it
    naming this advisory, so a re-run of :func:`retarget` finishes it) or
    neither. That makes it idempotent: once the PATCH has landed the line is
    gone and a re-run does nothing, so the comment is never posted twice.
    """
    if not moved_to:
        raise ValueError(f"unmark {ref.slug}: no issue the record moved to")
    endpoint = _issue_endpoint(ref)
    present, body = _issue_labels_body(ref)
    if ghsa_id not in parse_advisory_refs(body):
        return
    comment = _moved_comment(moved_to)
    existing = github.read_json_paginated(f"{endpoint}/comments", jq="[.[] | .body]")
    if comment not in existing:
        github.write_json("POST", f"{endpoint}/comments", {"body": comment})
    this_line = re.compile(
        rf"^Security advisory: {_NAME}/\.github {re.escape(ghsa_id)}[ \t]*\r?$", re.M
    )
    remaining = re.sub(r"\n{3,}", "\n\n", this_line.sub("", body)).rstrip()
    update: dict[str, object] = {"body": remaining}
    if SECURITY_LABEL in present and not parse_advisory_refs(remaining):
        update["labels"] = sorted(present - {SECURITY_LABEL})
    github.write_json("PATCH", endpoint, update)


def _with_tracking(description: str, targets: Sequence[IssueRef]) -> str:
    kept = re.sub(r"\n{3,}", "\n\n", _TRACKING_RE.sub("", description)).rstrip()
    lines = "\n".join(tracking_line(ref) for ref in targets)
    return f"{kept}\n\n{lines}" if kept else lines


@contextlib.contextmanager
def _step(ghsa_id: str, step: str) -> Generator[None]:
    """Turn any failure inside one retarget step into a :class:`RetargetError`.

    The message names the advisory and the step; a ``gh`` failure is scrubbed
    of its stdout first, both in the message and in the chained cause.
    """
    try:
        yield
    except github.GitHubAPIError as exc:
        clean = _scrubbed(exc)
        raise RetargetError(f"advisory {ghsa_id}: {step} failed: {clean}") from clean
    except Exception as exc:
        raise RetargetError(f"advisory {ghsa_id}: {step} failed: {exc}") from exc


def retarget(
    home: str,
    ghsa_id: str,
    targets: Sequence[IssueRef],
    *,
    renamed: Sequence[IssueRef] = (),
) -> None:
    """Move advisory *ghsa_id*'s tracking to *targets*.

    In order: mark every target, replace every ``Tracking issue:`` line with
    one per target, then unmark every dropped issue. Each step only starts when
    the one before succeeded, so a failure never leaves an advisory tracking an
    unmarked issue. Any failure raises :class:`RetargetError` naming the
    advisory and the step; the caller must not close or move the old issue
    until this returns.

    A **dropped** issue is one that is not a target and either was named by
    the tracking lines this run replaced, or is a ``security``-labelled issue
    in the home's org whose advisory line names *ghsa_id*
    (:func:`security_issues`, a REST listing, never search). The second source
    is what makes a re-run with the same arguments finish an interrupted run:
    once the tracking lines are rewritten they no longer name the dropped
    issue, but the issue itself still does until it is unmarked. A dropped
    issue whose body has no line naming *ghsa_id* (it names only other
    advisories, or none) is never touched (:func:`unmark_issue`); one that also
    names other advisories loses only this line and keeps its label.

    *renamed* names old numbers of issues that are themselves a target (a
    transferred issue): their tracking line is replaced but they are never
    unmarked, since the old number now leads to the moved issue itself.
    """
    wanted = tuple(dict.fromkeys(targets))
    if not wanted:
        raise RetargetError(f"advisory {ghsa_id}: no target issues given")
    for ref in wanted:
        with _step(ghsa_id, f"marking {ref.slug}"):
            mark_issue(ref, home, ghsa_id)
    with _step(ghsa_id, "reading the tracking lines"):
        _summary, description = read_text(home, ghsa_id)
    previous = parse_tracking(description)
    with _step(ghsa_id, "rewriting the tracking lines"):
        _write_description(home, ghsa_id, _with_tracking(description, wanted))
    with _step(ghsa_id, "listing the security issues"):
        marked = [
            issue.ref
            for issue in security_issues(home.split("/", 1)[0])
            if ghsa_id in issue.ghsa_ids
        ]
    for ref in dict.fromkeys((*previous, *marked)):
        if ref not in wanted and ref not in renamed:
            with _step(ghsa_id, f"unmarking {ref.slug}"):
                unmark_issue(ref, ghsa_id, wanted)


def security_issues(org: str) -> list[SecurityIssue]:
    """Every ``security``-labelled issue in *org*'s managed repos, open or closed.

    The same repo enumeration as :func:`epic_audit.open_issues` (non-archived
    repos with a ``vergil.toml``), read per repo through the REST label filter,
    never through search, whose index can lag.
    """
    found: list[SecurityIssue] = []
    for bare in github.list_org_repos(org):
        repo = f"{org}/{bare}"
        if not epics.is_managed_repo(repo):
            continue
        rows = github.read_json_paginated(
            f"repos/{repo}/issues?state=all&labels={SECURITY_LABEL}", jq=_SECURITY_ISSUES_JQ
        )
        for row in rows:
            item: Any = row
            found.append(
                SecurityIssue(
                    ref=IssueRef(org, bare, int(item["number"])),
                    open=str(item["state"]).lower() == "open",
                    ghsa_ids=parse_advisory_refs(str(item.get("body") or "")),
                )
            )
    return found


def _tracking_problems(advisory: Advisory, by_ref: dict[IssueRef, SecurityIssue]) -> list[str]:
    if not advisory.tracking:
        return [f"{advisory.ghsa_id}: no Tracking issue: lines"]
    problems: list[str] = []
    for ref in advisory.tracking:
        issue = by_ref.get(ref)
        if issue is None:
            problems.append(
                f"{advisory.ghsa_id}: tracks {ref.slug}, which is not a security-labelled issue"
            )
        elif advisory.ghsa_id not in issue.ghsa_ids:
            problems.append(f"{advisory.ghsa_id}: tracks {ref.slug}, which does not point back")
    return problems


def _issue_problems(issue: SecurityIssue, by_id: dict[str, Advisory]) -> list[str]:
    """One broken line per wrong advisory line of *issue* (or one for having none)."""
    if not issue.ghsa_ids:
        return [f"{issue.ref.slug}: security label but no Security advisory: line"]
    problems: list[str] = []
    for ghsa_id in issue.ghsa_ids:
        advisory = by_id.get(ghsa_id)
        if advisory is None:
            problems.append(f"{issue.ref.slug}: names {ghsa_id}, which does not exist")
        elif issue.ref not in advisory.tracking:
            problems.append(f"{issue.ref.slug}: names {ghsa_id}, which does not track it")
    return problems


def classify(advisories: Sequence[Advisory], issues: Sequence[SecurityIssue]) -> Classification:
    """Classify every advisory and check every pair's links both ways (spec 4.3).

    - An advisory some ``security`` issue names is **paired** when every one of
      its ``Tracking issue:`` lines names a ``security`` issue that names it
      back; otherwise each wrong or missing line is **broken**.
    - An advisory no issue names is an **external** report when it came from
      private vulnerability reporting (``triage`` state or a submission), and
      **broken** otherwise.
    - A ``security`` issue is **broken** when it has no advisory line; each of
      its lines naming an advisory that does not exist or does not track it is
      a broken line of its own.

    An issue :func:`retarget` unmarked has no ``security`` label, so it is not
    among *issues* and is never reported.
    """
    by_id = {advisory.ghsa_id: advisory for advisory in advisories}
    by_ref = {issue.ref: issue for issue in issues}
    named = {ghsa_id for issue in issues for ghsa_id in issue.ghsa_ids}
    paired: list[Advisory] = []
    external: list[Advisory] = []
    broken: list[str] = []
    for advisory in sorted(advisories, key=lambda a: a.ghsa_id):
        if advisory.ghsa_id not in named:
            if advisory.external:
                external.append(advisory)
            else:
                broken.append(f"{advisory.ghsa_id}: no security issue names this advisory")
            continue
        problems = _tracking_problems(advisory, by_ref)
        if problems:
            broken += problems
        else:
            paired.append(advisory)
    for issue in sorted(issues, key=lambda i: i.ref.slug):
        broken += _issue_problems(issue, by_id)
    return Classification(paired=paired, external=external, broken=broken)


# The states the sweep keeps in step with the issues. Any other state (a
# published or withdrawn advisory, or a triage-state report a pair adopted) is
# a human's decision, so the sweep never moves an advisory out of it.
_SYNCED_STATES = ("draft", "closed")


def pending_syncs(
    classification: Classification, issues: Sequence[SecurityIssue]
) -> list[tuple[str, Literal["draft", "closed"]]]:
    """The ``(GHSA ID, target state)`` changes the issue-driven sync would make.

    For each **paired** advisory the target is ``closed`` when every issue it
    tracks is closed, and ``draft`` while any is open (spec 4.3: the issue
    always wins). Only a mismatch is listed. External reports are never
    touched, and neither is an advisory outside ``draft``/``closed``.
    """
    open_by_ref = {issue.ref: issue.open for issue in issues}
    changes: list[tuple[str, Literal["draft", "closed"]]] = []
    for advisory in classification.paired:
        if advisory.external or advisory.state not in _SYNCED_STATES:
            continue
        # classify() pairs an advisory only when every tracked issue is listed.
        any_open = any(open_by_ref[ref] for ref in advisory.tracking)
        target: Literal["draft", "closed"] = "draft" if any_open else "closed"
        if advisory.state != target:
            changes.append((advisory.ghsa_id, target))
    return changes


def sync_states(home: str, classification: Classification, issues: Sequence[SecurityIssue]) -> int:
    """Apply :func:`pending_syncs` to *home* and return the number of advisories changed.

    Stops at the first failed write, which propagates scrubbed of its stdout;
    the next sweep retries whatever is still out of step.
    """
    changes = pending_syncs(classification, issues)
    for ghsa_id, state in changes:
        set_state(home, ghsa_id, state)
    return len(changes)
