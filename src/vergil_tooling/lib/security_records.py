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
classification, :func:`retarget`, the one way a link moves, and
:func:`close_record`, which closes an advisory with a public reason, and
:func:`note_record`, which appends a dated note to an advisory's description.

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
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import epics, github, labels
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from datetime import date

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
_ISSUE_JQ = '{number, repository_url, state, labels: [.labels[].name], body: (.body // "")}'
_SECURITY_ISSUES_JQ = '[.[] | select(.pull_request == null) | {number, state, body: (.body // "")}]'


class PrivateHomeError(RuntimeError):
    """The org's ``.github`` repo is not public, so it cannot hold security records."""


class RetargetError(RuntimeError):
    """A :func:`retarget` step failed; the message names the GHSA ID and the step."""


class CloseError(RuntimeError):
    """A :func:`close_record` refusal or failed step; the message names IDs and refs only."""


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


def parse_advisory_lines(issue_body: str) -> tuple[tuple[str, str], ...]:
    """Every ``(org, GHSA ID)`` the exact ``Security advisory:`` lines name, in order, no repeats.

    :func:`parse_advisory_refs` keeps the GHSA IDs only; this keeps the org of
    each line's ``<org>/.github`` too, for a caller that checks it.
    """
    pairs = ((match.group(1), match.group(2)) for match in _ADVISORY_RE.finditer(issue_body))
    return tuple(dict.fromkeys(pairs))


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

    The only path to an advisory's text; for ``show``, ``list --details``,
    :func:`retarget`'s rewrite and :func:`note_record`'s append, never for
    anything that prints to a log.
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


def _issue_state(ref: IssueRef) -> tuple[IssueRef, set[str], str]:
    """*ref*'s current ref, labels and body.

    The current ref differs from *ref* only when the issue was transferred:
    the read at its old number follows GitHub's redirect to the moved issue,
    while writes and sub-resource reads at the old number fail with a 404.
    """
    raw = _read_issue(ref)
    labels_ = {str(name) for name in raw.get("labels") or []}
    return _current_ref(raw), labels_, str(raw.get("body") or "")


def _read_issue(ref: IssueRef) -> Any:
    """*ref*'s REST issue read, projected by ``_ISSUE_JQ``; it follows a transfer."""
    return github.read_json("api", _issue_endpoint(ref), "--jq", _ISSUE_JQ)


def _current_ref(raw: Any) -> IssueRef:
    """The ref an issue read (:func:`_read_issue`) actually returned."""
    owner, repo = str(raw["repository_url"]).rstrip("/").split("/")[-2:]
    return IssueRef(owner=owner, repo=repo, number=int(raw["number"]))


def _issue_labels_body(ref: IssueRef) -> tuple[set[str], str]:
    _current, present, body = _issue_state(ref)
    return present, body


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


def unmark_issue(ref: IssueRef, ghsa_id: str, moved_to: Sequence[IssueRef]) -> str | None:
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

    A *ref* that was **transferred** (its read leads to an issue under another
    ref) is never written at its old number. If the issue it moved to is in
    *moved_to*, nothing is left to unmark: that issue is a target itself, and
    unmarking it would undo the retarget. Otherwise the unmark follows the
    transfer and acts on the issue's current ref. Either way it returns a
    one-line note, naming only refs, that says which it did; it returns
    ``None`` for an issue that was not transferred.
    """
    if not moved_to:
        raise ValueError(f"unmark {ref.slug}: no issue the record moved to")
    current, present, body = _issue_state(ref)
    if current == ref:
        _unmark_current(ref, present, body, ghsa_id, moved_to)
        return None
    if current in moved_to:
        return (
            f"{ref.slug} was transferred to {current.slug}, a target: "
            f"nothing left to unmark at {ref.slug}"
        )
    if _unmark_current(current, present, body, ghsa_id, moved_to):
        outcome = f"unmarked {current.slug}"
    else:
        outcome = f"{current.slug} has no line for this advisory, nothing to unmark"
    return f"{ref.slug} was transferred to {current.slug}: followed the transfer and {outcome}"


def _unmark_current(
    ref: IssueRef, present: set[str], body: str, ghsa_id: str, moved_to: Sequence[IssueRef]
) -> bool:
    """:func:`unmark_issue` at the issue's current *ref*; ``True`` if it named *ghsa_id*."""
    if ghsa_id not in parse_advisory_refs(body):
        return False
    endpoint = _issue_endpoint(ref)
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
    return True


def _with_tracking(description: str, targets: Sequence[IssueRef]) -> str:
    kept = re.sub(r"\n{3,}", "\n\n", _TRACKING_RE.sub("", description)).rstrip()
    lines = "\n".join(tracking_line(ref) for ref in targets)
    return f"{kept}\n\n{lines}" if kept else lines


@contextlib.contextmanager
def _step(ghsa_id: str, step: str, error: type[RuntimeError] = RetargetError) -> Generator[None]:
    """Turn any failure inside one step into *error*, a :class:`RetargetError` by default.

    The message names the advisory and the step; a ``gh`` failure is scrubbed
    of its stdout first, both in the message and in the chained cause.
    """
    try:
        yield
    except github.GitHubAPIError as exc:
        clean = _scrubbed(exc)
        raise error(f"advisory {ghsa_id}: {step} failed: {clean}") from clean
    except Exception as exc:
        raise error(f"advisory {ghsa_id}: {step} failed: {exc}") from exc


def retarget(
    home: str,
    ghsa_id: str,
    targets: Sequence[IssueRef],
    *,
    renamed: Sequence[IssueRef] = (),
) -> tuple[str, ...]:
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

    A dropped issue that was transferred but is not named in *renamed* is
    never written at its old number (:func:`unmark_issue`). The return value
    holds one note per such issue, saying what the unmark did.
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
    notes: list[str] = []
    for ref in dict.fromkeys((*previous, *marked)):
        if ref not in wanted and ref not in renamed:
            with _step(ghsa_id, f"unmarking {ref.slug}"):
                note = unmark_issue(ref, ghsa_id, wanted)
            if note is not None:
                notes.append(note)
    return tuple(notes)


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


# The states the sync keeps in step with the issues. Any other state (a
# published or withdrawn advisory, or a triage-state report a pair adopted) is
# a human's decision, so the sync never moves an advisory out of it.
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
    the next sync retries whatever is still out of step.
    """
    changes = pending_syncs(classification, issues)
    for ghsa_id, state in changes:
        set_state(home, ghsa_id, state)
    return len(changes)


def records_pass(org: str, *, apply: bool, prog: str) -> int:
    """The security-records pass over *org*; *apply* False previews it without writing.

    Lists the advisories in the org's advisory home, syncs each paired
    advisory's ``draft``/``closed`` state to its issues (:func:`sync_states`),
    and reports broken pairs and unpaired external reports. It needs
    **Repository security advisories: read** on ``<org>/.github``, and with
    *apply* also **write**. *prog* prefixes the failure line on stderr.

    Its output names only GHSA IDs, issue refs and states, never advisory
    text: every advisory read it makes projects the text away, and
    :class:`Advisory` has no text field. The advisory home is resolved only
    when the org has a ``security`` issue or a ``.github`` repo; with neither
    there is nothing to check. A private ``.github`` is refused loudly, never
    skipped: :class:`PrivateHomeError` propagates to the caller, whose message
    already names the program.

    Exit 1 when the pass fails, or, with *apply*, when any pair is broken
    (after syncing the rest). The preview reports broken pairs without failing.
    """
    mode = "" if apply else " (preview)"
    try:
        issues = security_issues(org)
        if not issues and not github.repo_exists(org, ".github"):
            print(f"Security records{mode}: none.")
            return 0
        home = advisory_home(org)
        result = classify(list_advisories(home), issues)
        synced = sync_states(home, result, issues) if apply else len(pending_syncs(result, issues))
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        print(f"{prog}: security records pass failed: {exc}", file=sys.stderr)
        return 1
    verb = "synced" if apply else "would sync"
    states = "advisory state" if synced == 1 else "advisory states"
    broken = len(result.broken)
    pairs = "broken pair" if broken == 1 else "broken pairs"
    print(f"Security records{mode}: {synced} {states} {verb}, {broken} {pairs}.")
    for line in result.broken:
        print(f"- broken: {line}")
    for advisory in result.external:
        print(f"- unpaired external report (left untouched): {advisory.ghsa_id}")
    return 1 if apply and result.broken else 0


# --- close ------------------------------------------------------------------

CLOSE_REASON_MAX = 500


def close_comment(ghsa_id: str, reason: str) -> str:
    """The public comment a close posts: the GHSA ID and the reason, nothing else."""
    return f"Security record {ghsa_id} closed: {reason}"


def check_close_reason(reason: str) -> str:
    """*reason* stripped; :class:`CloseError` when it is empty, multi-line or too long."""
    text = reason.strip()
    if not text:
        raise CloseError("the close reason is empty")
    if "\n" in text or "\r" in text:
        raise CloseError("the close reason must be one line")
    if len(text) > CLOSE_REASON_MAX:
        raise CloseError(f"the close reason is longer than {CLOSE_REASON_MAX} characters")
    return text


@dataclass(frozen=True)
class CloseResult:
    """What :func:`close_record` did, by refs only.

    ``issues`` carry the close comment, at their current refs; ``notes`` say
    which tracked issue was transferred where; ``closed`` is False when the
    advisory was already closed, so this run only completed the comments.
    """

    issues: tuple[IssueRef, ...]
    notes: tuple[str, ...]
    closed: bool


def close_record(home: str, ghsa_id: str, reason: str) -> CloseResult:
    """Close advisory *ghsa_id* in *home*, leaving *reason* on every issue it tracks.

    The issues are those its ``Tracking issue:`` lines name plus every
    ``security``-labelled issue in the org whose advisory line names it (the
    other half of a broken pair). Each is read at its current ref, following
    a transfer. The steps run in order, each only once the one before it
    succeeded:

    1. **Check.** Refuse, changing nothing, when the advisory does not exist,
       is an external report or is in a state other than ``draft`` or
       ``closed`` (a human's decision, which ``sync`` leaves alone too), has
       no issue to carry the comment, or has any issue still open: ``sync``
       sets an advisory back to draft while any issue it tracks is open, so
       the close would not hold.
    2. **Comment.** Post :func:`close_comment` on each issue, skipping one
       that already carries that exact comment.
    3. **Close.** Set the advisory's state to ``closed``, unless it already is.

    The comments go first, so a closed advisory always has its audit trail,
    and a re-run with the same reason after a failure finishes the job
    without posting twice. Any failure raises :class:`CloseError` naming the
    advisory and the step, scrubbed of ``gh`` stdout. *reason* is public
    text; keeping it neutral is the caller's duty. No advisory text is read.
    """
    text = check_close_reason(reason)
    with _step(ghsa_id, "reading the advisories", CloseError):
        advisory = {a.ghsa_id: a for a in list_advisories(home)}.get(ghsa_id)
    if advisory is None:
        raise CloseError(f"advisory {ghsa_id} does not exist in {home}")
    if advisory.external or advisory.state not in _SYNCED_STATES:
        kind = "an external report" if advisory.external else f"in state {advisory.state}"
        raise CloseError(
            f"advisory {ghsa_id} is {kind}; close only moves a draft security record "
            "to closed, and any other state is a human's decision"
        )
    with _step(ghsa_id, "listing the security issues", CloseError):
        naming = [
            issue.ref
            for issue in security_issues(home.split("/", 1)[0])
            if ghsa_id in issue.ghsa_ids
        ]
    refs = tuple(dict.fromkeys((*advisory.tracking, *naming)))
    if not refs:
        raise CloseError(
            f"advisory {ghsa_id} tracks no issue and no security issue names it, so no "
            "issue can carry the close comment"
        )
    current: dict[IssueRef, None] = {}
    notes: list[str] = []
    still_open: list[IssueRef] = []
    for ref in refs:
        with _step(ghsa_id, f"reading {ref.slug}", CloseError):
            raw = _read_issue(ref)
            now = _current_ref(raw)
        if now != ref:
            notes.append(f"{ref.slug} was transferred to {now.slug}: commented there")
        if now not in current and str(raw.get("state") or "").lower() != "closed":
            still_open.append(now)
        current[now] = None
    if still_open:
        names = ", ".join(ref.slug for ref in still_open)
        noun = "issue" if len(still_open) == 1 else "issues"
        raise CloseError(
            f"advisory {ghsa_id}: open {noun} {names}; close or retarget first, since "
            "sync sets an advisory back to draft while any issue it tracks is open"
        )
    comment = close_comment(ghsa_id, text)
    for ref in current:
        endpoint = _issue_endpoint(ref)
        with _step(ghsa_id, f"commenting on {ref.slug}", CloseError):
            existing = github.read_json_paginated(f"{endpoint}/comments", jq="[.[] | .body]")
            if comment not in existing:
                github.write_json("POST", f"{endpoint}/comments", {"body": comment})
    closing = advisory.state != "closed"
    if closing:
        with _step(ghsa_id, "setting the state to closed", CloseError):
            set_state(home, ghsa_id, "closed")
    return CloseResult(issues=tuple(current), notes=tuple(notes), closed=closing)


# --- note -------------------------------------------------------------------


class NoteError(RuntimeError):
    """A :func:`note_record` refusal or failed step; the message names IDs only."""


def note_header(day: date) -> str:
    """The line a note starts with: ``Note (<YYYY-MM-DD>):``."""
    return f"Note ({day.isoformat()}):"


def check_note(note: str) -> str:
    """*note* with its outer blank lines and every line's trailing spaces dropped.

    :class:`NoteError` when it is empty, or when any of its lines is an exact
    ``Tracking issue:`` line: the note would then change what the record
    tracks, which only :func:`retarget` may do. The message never quotes the
    note.
    """
    text = "\n".join(line.rstrip() for line in note.splitlines()).strip("\n")
    if not text.strip():
        raise NoteError("the note is empty")
    if _TRACKING_RE.search(text):
        raise NoteError(
            "the note has a 'Tracking issue:' line, which would change what the record "
            "tracks; use retarget to move a record's tracking"
        )
    return text


def with_note(description: str, note: str, day: date) -> str:
    """*description* with *note* appended under :func:`note_header`.

    The ``Tracking issue:`` lines are kept, in their order, as the last lines,
    so :func:`retarget`, :func:`list_advisories` and the sync read them as
    before.
    """
    tracking = parse_tracking(description)
    kept = re.sub(r"\n{3,}", "\n\n", _TRACKING_RE.sub("", description)).rstrip()
    block = f"{note_header(day)}\n{note}"
    text = f"{kept}\n\n{block}" if kept else block
    if tracking:
        text += "\n\n" + "\n".join(tracking_line(ref) for ref in tracking)
    return text


def note_record(home: str, ghsa_id: str, note: str, day: date) -> bool:
    """Append *note* to advisory *ghsa_id*'s description in *home*, dated *day*.

    Refuses, changing nothing, when the note is empty or holds a
    ``Tracking issue:`` line (:func:`check_note`), or when the advisory does
    not exist, is an external report or is in a state other than ``draft`` or
    ``closed``. Returns ``False``, writing nothing, when the description
    already holds this exact dated note, so a re-run after a lost response
    never adds it twice; ``True`` once it is written.

    Any failure raises :class:`NoteError` naming the advisory and the step,
    scrubbed of ``gh`` stdout. Neither the note nor the description ever
    reaches a message.
    """
    text = check_note(note)
    with _step(ghsa_id, "reading the advisories", NoteError):
        advisory = {a.ghsa_id: a for a in list_advisories(home)}.get(ghsa_id)
    if advisory is None:
        raise NoteError(f"advisory {ghsa_id} does not exist in {home}")
    if advisory.external or advisory.state not in _SYNCED_STATES:
        kind = "an external report" if advisory.external else f"in state {advisory.state}"
        raise NoteError(
            f"advisory {ghsa_id} is {kind}; note only annotates a draft or closed "
            "security record"
        )
    with _step(ghsa_id, "reading the description", NoteError):
        _summary, description = read_text(home, ghsa_id)
    if f"{note_header(day)}\n{text}" in description:
        return False
    with _step(ghsa_id, "writing the note", NoteError):
        _write_description(home, ghsa_id, with_note(description, text, day))
    return True
