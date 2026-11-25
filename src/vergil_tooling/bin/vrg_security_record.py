"""Record a security finding as a private advisory paired with a neutral issue.

The ``vrg-security-record`` command over :mod:`vergil_tooling.lib.security_records`
(epic vergil-project/.github#517). Every finding is one **pair**: a draft
repository security advisory in ``<org>/.github`` holding the details, and a
neutral ``security``-labelled intake issue carrying only a pointer to it.

- ``create`` makes the pair: the intake epic's capacity check (refused with
  nothing created), duplicate check, advisory, issue in the affected
  repo's queue home, link into its intake epic's chain, then the advisory's
  ``Tracking issue:`` line. ``--issue`` attaches the advisory to an existing
  issue instead (attach mode), and ``--resume`` finishes a pair whose advisory
  already exists. A failure after the advisory exists prints the exact command
  that finishes the pair and exits 1. ``create`` makes the advisory's first
  pairing only: one that already tracks another issue is refused before any
  issue is created, marked or linked. A body file, details file or summary
  holding a ``Moved from:`` or ``Moved to:`` line is refused before any
  GitHub call, with ``--resume`` too: only a move writes those lines.
- ``list`` names advisories by GHSA ID, state, affected repo and tracked
  issues; ``--details`` adds each summary and is refused under the sweep.
- ``show`` prints one pair with its details, to the terminal only. Given an
  issue, it shows only the advisories its ``Security advisory:`` lines name
  that also track it (their ``Tracking issue:`` lines name it); any other is
  named on stderr and not shown.
- ``retarget`` moves an advisory's tracking from ``--from``, an issue it
  tracks, to new issues. It refuses an untracked source, and stopping
  tracking an open issue that no ``--drop`` names, before any write. When
  every ``--to`` is in another org it moves the advisory to that org's
  ``.github`` (a copy with ``Moved from:``; the original closed with
  ``Moved to:``); ``--to`` issues spanning orgs are refused (epic
  vergil-project/.github#1417).
- ``close`` closes a draft advisory whose issues are all closed, with a
  required ``--reason``: it first posts ``Security record <GHSA> closed:
  <reason>`` on every issue the record tracks, the audit trail
  (vergil-tooling#4000). It reads only those issues, with no org-wide scan
  (vergil-tooling#5138); ``sync`` reports any broken pair.
- ``note`` appends a dated note, read from a file, to an advisory's
  description, keeping the ``Tracking issue:`` lines last; it is append-only
  and prints only ``Noted <GHSA> in <org>/.github.`` (vergil-tooling#3998).
- ``sync`` runs the security-records pass over the org: it sets each paired
  advisory to ``closed`` or ``draft`` from its issues and reports broken
  pairs and unpaired external reports; ``--dry-run`` previews it. An issue
  whose body the trust layer does not trust is reported by metadata only,
  each advisory tracking it is left unchanged, and the run exits 1, preview
  included (vergil-tooling#4990). Its close
  guard keeps an advisory ``draft``, reported on a ``held:`` line, while a
  queue item or an issue closed other than as completed tracks it. It needs
  Repository security advisories read and write on ``<org>/.github``, which
  the human's login and the user App hold (vergil-tooling#4313); the human
  or the user agent runs it, never a schedule.

**Output rule.** An advisory's summary and description reach stdout only
through ``show`` and ``list --details``. Every other line, and every error,
names only GHSA IDs, issue refs, labels and states.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import epic_chain, epics, github, security_records, trust
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.labels import ensure_labels_or_report
from vergil_tooling.lib.process_errors import failure_message

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROG = "vrg-security-record"
_GHSA_RE = re.compile(r"^GHSA(?:-[0-9a-z]{4}){3}$")
_SUMMARY_MAX = 1024
_STATES = ("draft", "closed", "all")


class _RefusedError(Exception):
    """A refusal already worded for the operator; ``main`` prints it and exits 1."""


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "create":
            return _create(args)
        if args.command == "list":
            return _list(args)
        if args.command == "show":
            return _show(args)
        if args.command == "sync":
            return _sync(args)
        if args.command == "close":
            return _close(args)
        if args.command == "note":
            return _note(args)
        return _retarget(args)
    except _RefusedError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1


def _reason(exc: BaseException) -> str:
    """A one-line reason for *exc* that never carries a command's stdout.

    A failed ``gh`` call keeps its exit status, command and stderr; its stdout
    is dropped because, for an advisory call, it may hold the advisory text.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        text = failure_message(exc)
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        if stderr and stderr.strip():
            text += f" stderr: {' '.join(stderr.split())}"
        return text
    return " ".join(str(exc).split())


def _full_ref(text: str, flag: str) -> IssueRef:
    """Parse an ``owner/repo#N`` ref; a bare ``#N`` has no repo to resolve against."""
    try:
        return epics.parse_issue_ref(text, default_repo="")
    except ValueError:
        raise _RefusedError(f"{flag} must be owner/repo#N, got {text!r}") from None


def _ghsa(text: str, flag: str) -> str:
    if not _GHSA_RE.match(text):
        raise _RefusedError(f"{flag} must be a GHSA ID (GHSA-xxxx-xxxx-xxxx), got {text!r}")
    return text


def _owner_of(repo: str) -> str:
    if "/" not in repo or not all(repo.split("/", 1)):
        raise _RefusedError(f"--repo must be owner/name, got {repo!r}")
    return repo.split("/", 1)[0]


def _ref_from_url(url: str) -> IssueRef:
    """The issue ref of an issue URL (``…/<owner>/<repo>/issues/<N>``)."""
    parts = url.rstrip("/").split("/")
    if len(parts) < 4 or parts[-2] != "issues" or not parts[-1].isdigit():
        raise ValueError(f"not an issue URL: {url!r}")
    return IssueRef(owner=parts[-4], repo=parts[-3], number=int(parts[-1]))


def _detected_org() -> str:
    org = github.detect_org()
    if org is None:
        raise _RefusedError(
            "could not determine the GitHub org from this repo's 'origin' remote; "
            "run it from inside a repo in the target org, or pass --repo"
        )
    return org


# --- create -----------------------------------------------------------------


def _summary_of(args: argparse.Namespace, details: str) -> str:
    """The advisory summary: ``--summary``, else the details file's first line."""
    if args.summary is not None:
        summary = str(args.summary).strip()
    else:
        lines = details.splitlines()
        summary = lines[0].strip() if lines else ""
    if not summary:
        raise _RefusedError(
            "the advisory summary is empty: pass --summary, or start the details file "
            "with a summary line"
        )
    if len(summary) > _SUMMARY_MAX:
        raise _RefusedError(f"the advisory summary is longer than {_SUMMARY_MAX} characters")
    return summary


def _resume_command(args: argparse.Namespace, ghsa_id: str, issue: IssueRef | None) -> str:
    """The exact command that finishes this pair.

    The same arguments plus ``--resume`` (and ``--issue`` once the issue
    exists). ``--summary`` is left out: with ``--resume`` no advisory is
    created, so the summary is not needed, and it must not reach stderr.
    """
    parts = [
        _PROG,
        "create",
        "--repo",
        args.repo,
        "--title",
        args.title,
        "--body-file",
        args.body_file,
        "--details-file",
        args.details_file,
        "--severity",
        args.severity,
        "--resume",
        ghsa_id,
    ]
    if issue is not None:
        parts += ["--issue", issue.slug]
    return shlex.join(parts)


def _read_file(path: str, flag: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise _RefusedError(f"cannot read {flag} {path}: {exc.strerror or exc}") from None


def _names_line(body: str, line: str) -> bool:
    return line in (candidate.rstrip() for candidate in body.splitlines())


def _find_resumed_issue(intake: IssueRef, home: str, ghsa_id: str) -> IssueRef | None:
    """The intake item whose trusted ``Security advisory:`` line names *ghsa_id*, or None.

    Reads the items of the intake epic's whole chain
    (:func:`epic_chain.chain_child_states`, epic vergil-project/.github#1609),
    so an item in a continuation is found, never through search, whose index
    can lag. Their bodies are read through the trust layer
    (:func:`trust.read_bodies`), and an item counts only when its body is
    trusted and has the line. A body the layer does not trust (its author or
    an editor holds no role) is never read for the line, so when no item
    matches, each one skipped is reported on stderr, metadata only: it may be
    the issue sought.
    """
    line = security_records.advisory_line(home, ghsa_id)
    by_repo: dict[str, list[IssueRef]] = {}
    for child in epic_chain.chain_child_states(intake):
        by_repo.setdefault(f"{child.ref.owner}/{child.ref.repo}", []).append(child.ref)
    skipped: list[trust.Skipped] = []
    for repo, refs in by_repo.items():
        bodies = trust.read_bodies(repo, [ref.number for ref in refs])
        for ref in refs:
            content = bodies[ref.number]
            if content.body is None:
                if content.skipped is None:
                    raise RuntimeError(f"the trust layer read no body of {ref.slug} and no skip")
                skipped.append(content.skipped)
                continue
            if _names_line(content.body, line):
                return ref
    trust.report_skipped(skipped, stream=sys.stderr)
    return None


def _create(args: argparse.Namespace) -> int:
    org = _owner_of(args.repo)
    if args.resume is not None:
        _ghsa(args.resume, "--resume")
    attach = _full_ref(args.issue, "--issue") if args.issue is not None else None
    body = _read_file(args.body_file, "--body-file")
    details = _read_file(args.details_file, "--details-file")
    # Only a move writes these lines, so the caller's text may not, with
    # --resume too: the resume command re-reads the same files.
    security_records.refuse_move_lines(body, "--body-file", _RefusedError)
    security_records.refuse_move_lines(details, "--details-file", _RefusedError)
    # With --resume the advisory already exists, so no summary is needed.
    summary = _summary_of(args, details) if args.resume is None else ""
    security_records.refuse_move_lines(summary, "--summary", _RefusedError)
    with github.target_org(org):
        home = security_records.advisory_home(org)
        queue_home = epics.queue_home(args.repo)[0]
        intake = epics.ensure_intake_epic(args.repo)
        if args.resume is None:
            if attach is None:
                # The chain layer's capacity check before anything is created
                # (epic vergil-project/.github#1609, spec §4.5): a refusal of
                # the intake epic (closed, full while rollover is off, at the
                # depth limit) leaves neither an advisory nor an issue behind.
                epic_chain.check_capacity(intake)
            duplicate = security_records.find_duplicate(
                home, summary=summary, affected_repo=args.repo
            )
            if duplicate is not None:
                raise _RefusedError(
                    f"a draft advisory with this summary for {args.repo} already exists: "
                    f"{duplicate}. Finish its pair with: "
                    f"{_resume_command(args, duplicate, attach)}"
                )
            try:
                ghsa_id = security_records.create_advisory(
                    home,
                    summary=summary,
                    details=details,
                    affected_repo=args.repo,
                    severity=args.severity,
                )
            except Exception as exc:  # noqa: BLE001 - reported, never retried
                print(
                    f"{_PROG}: creating the advisory in {home} failed: {_reason(exc)}. "
                    "Nothing else was created. If the draft was created anyway, a "
                    "re-run is refused by the duplicate guard and names its GHSA ID.",
                    file=sys.stderr,
                )
                return 1
        else:
            ghsa_id = args.resume
        return _finish_pair(args, home, queue_home, intake, ghsa_id, body, attach)


def _finish_pair(
    args: argparse.Namespace,
    home: str,
    queue_home: str,
    intake: IssueRef,
    ghsa_id: str,
    body: str,
    attach: IssueRef | None,
) -> int:
    """Every step after the advisory exists; a failure names the resume command.

    The advisory's first pairing (:func:`security_records.pair`) is checked
    first (:func:`security_records.pair_check`): an advisory that already
    tracks another issue is refused before any issue is created, marked or
    linked, with no resume command, since resuming cannot fix it.
    """
    issue = attach
    step = "finding the issue"
    try:
        if issue is None and args.resume is not None:
            issue = _find_resumed_issue(intake, home, ghsa_id)
        step = "checking the advisory's tracking lines"
        security_records.pair_check(home, ghsa_id, issue)
        if attach is not None:
            paired = attach
            step = f"marking {paired.slug}"
            security_records.mark_issue(paired, home, ghsa_id)
            if args.resume is not None:
                step = f"linking {paired.slug} under intake epic {intake.slug}"
                _link_if_unparented(intake, paired)
        elif issue is not None:
            paired = issue
        else:
            # Checked again just before the create (and first, on --resume):
            # a refusal here leaves the advisory with no issue, which the
            # resume command finishes once the intake epic has room.
            step = f"checking the capacity of intake epic {intake.slug}"
            epic_chain.check_capacity(intake)
            step = "creating the issue"
            paired = issue = _create_issue(args, home, queue_home, ghsa_id, body)
            step = f"linking {paired.slug} under intake epic {intake.slug}"
            epic_chain.link_into(intake, paired)
        step = f"writing the tracking line for {paired.slug}"
        security_records.pair(home, ghsa_id, paired)
    except security_records.RetargetRefusedError as exc:
        print(f"{_PROG}: {_reason(exc)}", file=sys.stderr)
        return 1
    except _RefusedError as exc:
        print(
            f"{_PROG}: advisory {ghsa_id} exists but its pair is unfinished: {exc}. "
            f"Finish it with: {_resume_command(args, ghsa_id, issue)}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - every partial pair is reported
        print(
            f"{_PROG}: advisory {ghsa_id} exists but {step} failed: {_reason(exc)}. "
            f"Finish the pair with: {_resume_command(args, ghsa_id, issue)}",
            file=sys.stderr,
        )
        return 1
    print(f"Recorded {ghsa_id} in {home}, tracked by {paired.slug}.")
    return 0


def _create_issue(
    args: argparse.Namespace, home: str, queue_home: str, ghsa_id: str, body: str
) -> IssueRef:
    """Create the neutral issue in the queue home: the body plus the advisory line."""
    label = security_records.SECURITY_LABEL
    if not ensure_labels_or_report(_PROG, queue_home, [label]):
        raise _RefusedError(f"the {label!r} label could not be ensured in {queue_home}")
    line = security_records.advisory_line(home, ghsa_id)
    stripped = body.rstrip()
    text = f"{stripped}\n\n{line}" if stripped else line
    url = github.create_issue(repo=queue_home, title=args.title, body=text, labels=[label])
    return _ref_from_url(url)


def _link_if_unparented(intake: IssueRef, issue: IssueRef) -> None:
    """Link *issue* under *intake* unless it already has a parent.

    On resume this re-runs the link a failed create left undone, and is a
    no-op once linked. An issue that already has another parent (an attached
    existing issue) keeps it. The link goes through the chain layer
    (:func:`epic_chain.link_into`).
    """
    if epics.native_parent_of(issue) is None:
        epic_chain.link_into(intake, issue)


# --- list, show, retarget ---------------------------------------------------


def _list(args: argparse.Namespace) -> int:
    if args.details and os.environ.get("VRG_EPIC_SWEEP"):
        raise _RefusedError(
            "--details is refused under the scheduled sweep (VRG_EPIC_SWEEP is set): "
            "advisory text must never reach a log"
        )
    org = _owner_of(args.repo) if args.repo else _detected_org()
    with github.target_org(org):
        home = security_records.advisory_home(org)
        advisories = security_records.list_advisories(home)
        if args.state != "all":
            advisories = [a for a in advisories if a.state == args.state]
        if args.repo:
            bare = args.repo.split("/", 1)[1]
            advisories = [a for a in advisories if a.affected == bare]
        if not advisories:
            print(f"No security records in {home} match.")
            return 0
        for advisory in sorted(advisories, key=lambda a: a.ghsa_id):
            print(_record_line(advisory))
            if args.details:
                summary, _description = security_records.read_text(home, advisory.ghsa_id)
                print(f"    summary: {summary}")
    return 0


def _record_line(advisory: security_records.Advisory) -> str:
    """One advisory as IDs, state and refs only (the output rule)."""
    tracking = ", ".join(ref.slug for ref in advisory.tracking) or "(no tracking issue)"
    origin = "  [external report]" if advisory.external else ""
    affected = advisory.affected or "(no affected repo)"
    return f"{advisory.ghsa_id}  {advisory.state}  {affected}  tracks: {tracking}{origin}"


def _tracking_only(
    ref: IssueRef, named: tuple[str, ...], by_id: dict[str, security_records.Advisory]
) -> tuple[str, ...]:
    """The advisories of *named* whose ``Tracking issue:`` lines name *ref*.

    An issue's body line alone never shows a pair: anyone can write one. Each
    named advisory that does not track *ref* is left out and named on stderr
    by GHSA ID; none left is a refusal.
    """
    untracked = [g for g in named if ref not in by_id[g].tracking]
    if untracked:
        noun, verb = ("advisory", "does") if len(untracked) == 1 else ("advisories", "do")
        print(
            f"{_PROG}: not shown: {noun} {', '.join(untracked)} {verb} not track {ref.slug}",
            file=sys.stderr,
        )
    shown = tuple(g for g in named if g not in untracked)
    if not shown:
        raise _RefusedError(f"no advisory {ref.slug} names tracks it")
    return shown


def _show(args: argparse.Namespace) -> int:
    target: str = args.target
    if _GHSA_RE.match(target):
        org = _detected_org()
        ghsa_id = target
    else:
        ref = _full_ref(target, "show")
        org = ref.owner
        ghsa_id = ""
    with github.target_org(org):
        home = security_records.advisory_home(org)
        if ghsa_id:
            wanted: tuple[str, ...] = (ghsa_id,)
        else:
            raw: Any = github.read_json(
                "issue",
                "view",
                str(ref.number),
                "--repo",
                f"{ref.owner}/{ref.repo}",
                "--json",
                "body",
            )
            wanted = security_records.parse_advisory_refs(str(raw.get("body") or ""))
            if not wanted:
                raise _RefusedError(f"{ref.slug} has no Security advisory: line")
        # Check every named advisory exists before reading or printing any text,
        # so a partly broken issue prints nothing rather than some of its pairs.
        by_id = {a.ghsa_id: a for a in security_records.list_advisories(home)}
        missing = [g for g in wanted if g not in by_id]
        if missing:
            noun, verb = ("advisory", "does") if len(missing) == 1 else ("advisories", "do")
            raise _RefusedError(f"{noun} {', '.join(missing)} {verb} not exist in {home}")
        if not ghsa_id:
            wanted = _tracking_only(ref, wanted, by_id)
        pairs = [(by_id[g], *security_records.read_text(home, g)) for g in wanted]
    for index, (advisory, summary, description) in enumerate(pairs):
        if index:
            print()
        print(_record_line(advisory))
        print(f"summary: {summary}")
        print("description:")
        print(description)
    return 0


def _retarget(args: argparse.Namespace) -> int:
    ghsa_id = _ghsa(args.ghsa, "GHSA")
    source = _full_ref(args.source, "--from")
    targets = [_full_ref(text, "--to") for text in args.to]
    drop = [_full_ref(text, "--drop") for text in args.drop or ()]
    try:
        # The advisory lives in the org of --from and every issue it drops;
        # the --to issues may all be in one other org, which moves it there.
        org = epics.single_target_org(source, *drop)
        to_org = security_records.fix_org(ghsa_id, targets)
    except (ValueError, security_records.RetargetRefusedError) as exc:
        raise _RefusedError(str(exc)) from None
    with github.target_org(org):
        home = security_records.advisory_home(org)
        try:
            notes = security_records.retarget(home, ghsa_id, targets, source=source, drop=drop)
        except security_records.RetargetError as exc:
            print(f"{_PROG}: {_reason(exc)}", file=sys.stderr)
            return 1
    refs = ", ".join(ref.slug for ref in targets)
    if to_org == org:
        for note in notes:
            print(f"{ghsa_id}: {note}.")
        print(f"Retargeted {ghsa_id} to {refs}.")
        return 0
    # A move's last note is the original's Moved to: line, naming the copy.
    *transfers, moved = notes
    (_copy_org, copy_id), *_rest = security_records.parse_moved_to(moved)
    for note in transfers:
        print(f"{ghsa_id}: {note}.")
    print(
        f"Moved {ghsa_id} to {to_org}/.github as {copy_id}, tracked by {refs}; "
        f"{ghsa_id} is closed as moved."
    )
    return 0


def _close(args: argparse.Namespace) -> int:
    ghsa_id = _ghsa(args.ghsa, "GHSA")
    try:
        reason = security_records.check_close_reason(args.reason)
    except security_records.CloseError as exc:
        raise _RefusedError(f"--reason: {exc}") from None
    org = _detected_org()
    with github.target_org(org):
        home = security_records.advisory_home(org)
        try:
            result = security_records.close_record(home, ghsa_id, reason)
        except security_records.CloseError as exc:
            print(f"{_PROG}: {_reason(exc)}", file=sys.stderr)
            return 1
    for note in result.notes:
        print(f"{ghsa_id}: {note}.")
    refs = ", ".join(ref.slug for ref in result.issues)
    if result.closed:
        print(f"Closed {ghsa_id} in {home}; the reason is on {refs}.")
    else:
        print(f"{ghsa_id} was already closed in {home}; the reason is on {refs}.")
    return 0


def _note(args: argparse.Namespace) -> int:
    ghsa_id = _ghsa(args.ghsa, "GHSA")
    note = _read_file(args.file, "--file")
    try:
        note = security_records.check_note(note)
    except security_records.NoteError as exc:
        raise _RefusedError(f"--file: {exc}") from None
    org = _detected_org()
    with github.target_org(org):
        home = security_records.advisory_home(org)
        try:
            security_records.note_record(home, ghsa_id, note, datetime.now(UTC).date())
        except security_records.NoteError as exc:
            print(f"{_PROG}: {_reason(exc)}", file=sys.stderr)
            return 1
    print(f"Noted {ghsa_id} in {home}.")
    return 0


def _sync(args: argparse.Namespace) -> int:
    org = _detected_org()
    with github.target_org(org):
        return security_records.records_pass(org, apply=not args.dry_run, prog=_PROG)


# --- arguments --------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Record a security finding as a private draft advisory in <org>/.github "
            "paired with a neutral security-labelled intake issue."
        ),
        epilog=(
            "Advisory text reaches stdout only through 'show' and 'list --details'; "
            "everything else names GHSA IDs, issue refs and states only."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a pair (or attach, or resume one)")
    create.add_argument("--repo", required=True, help="The affected repo, owner/name")
    create.add_argument("--title", required=True, help="Neutral issue title")
    create.add_argument("--body-file", required=True, help="Neutral issue body file")
    create.add_argument("--details-file", required=True, help="Private advisory details file")
    create.add_argument(
        "--severity", required=True, choices=security_records.SEVERITIES, help="Severity"
    )
    create.add_argument(
        "--summary", help="Advisory summary (default: the details file's first line)"
    )
    create.add_argument(
        "--issue", help="Attach to this existing issue (owner/repo#N) instead of creating one"
    )
    create.add_argument("--resume", help="Finish the pair of this existing advisory (GHSA ID)")

    lister = sub.add_parser("list", help="List security records (IDs and states)")
    lister.add_argument("--repo", help="Only records about this affected repo, owner/name")
    lister.add_argument("--state", choices=_STATES, default="all", help="Advisory state")
    lister.add_argument(
        "--details", action="store_true", help="Also print each summary (interactive only)"
    )

    show = sub.add_parser("show", help="Show a pair, or every pair an issue names, with details")
    show.add_argument("target", help="A GHSA ID or an issue ref (owner/repo#N)")

    retarget = sub.add_parser(
        "retarget",
        help="Move an advisory's tracking from an issue it tracks to new issues",
        description=(
            "Move an advisory's tracking from --from, an issue it tracks, to the --to "
            "set. It refuses, before any write, a --from the advisory does not track, "
            "and stopping tracking an open issue (other than --from) that no --drop "
            "names; each refusal prints the command that would succeed. A re-run with "
            "the same arguments finishes an interrupted retarget. Leaving an open "
            "issue untracked with --drop is the human's decision. Every --to must be "
            "in one org; when that is not the advisory's org, the advisory moves "
            "there: a copy with a 'Moved from:' line is created in that org's .github "
            "and tracks the --to set, and the original gets a 'Moved to:' line and is "
            "closed."
        ),
    )
    retarget.add_argument("ghsa", help="The advisory's GHSA ID")
    retarget.add_argument(
        "--from",
        dest="source",
        required=True,
        help="The issue the advisory tracks now and moves from, owner/repo#N",
    )
    retarget.add_argument(
        "--to", action="append", required=True, help="Target issue owner/repo#N (repeatable)"
    )
    retarget.add_argument(
        "--drop",
        action="append",
        help="An open tracked issue deliberately left untracked, owner/repo#N (repeatable)",
    )

    close = sub.add_parser(
        "close",
        help="Close a draft advisory with a reason posted on every issue it tracks",
        description=(
            "Close a draft advisory in this repo's org's .github. It refuses while any "
            "issue the record tracks is open (sync would set it back to draft), and "
            "for an external report or a published, withdrawn or triage advisory. It "
            "first posts 'Security record <GHSA> closed: <reason>' on every issue the "
            "record tracks, then sets the advisory to closed; a re-run with the same "
            "reason finishes an interrupted close without posting twice. It reads only "
            "the issues the record tracks and scans no other: an issue that names the "
            "record without being tracked by it is a broken pair, which sync reports. "
            "The reason is public: never put advisory details in it."
        ),
    )
    close.add_argument("ghsa", help="The advisory's GHSA ID")
    close.add_argument(
        "--reason",
        required=True,
        help="Why the record is closed: one neutral line, public, no advisory details",
    )

    note = sub.add_parser(
        "note",
        help="Append a dated note from a file to an advisory's description",
        description=(
            "Append the text of --file to a draft or closed advisory's description in "
            "this repo's org's .github, under a 'Note (<YYYY-MM-DD>):' header (UTC), "
            "keeping the 'Tracking issue:' lines last. It is append-only: the existing "
            "text is never changed. It refuses an empty note, a note with a "
            "'Tracking issue:', 'Moved from:' or 'Moved to:' line, a missing advisory, "
            "an external report and any "
            "other state. A re-run with the same file on the same day adds nothing "
            "while that note is still the advisory's last. "
            "It prints only the GHSA ID and the advisory home, never the note."
        ),
    )
    note.add_argument("ghsa", help="The advisory's GHSA ID")
    note.add_argument("--file", required=True, help="File holding the private note text")

    sync = sub.add_parser(
        "sync",
        help="Sync advisory states to their issues and report broken pairs",
        description=(
            "Run the security-records pass over this repo's org: set each paired "
            "draft advisory in <org>/.github to closed when every issue it tracks "
            "is closed as completed, and back to draft while any is open; report "
            "broken pairs and unpaired external reports by ID only. An advisory "
            "that a queue item (an intake, planning or deferred item, live or "
            "archived) or an issue closed with any other reason still tracks "
            "stays draft and is reported on a held: line with the next step; a "
            "hold does not fail the pass. An issue whose body is untrusted is "
            "reported by metadata only and each advisory tracking it is left "
            "unchanged. Exit 1 if any pair is broken or any issue's parent cannot "
            "be read (after syncing the rest), if any issue body is untrusted, or "
            "the pass fails. Needs Repository security advisories: read and write on "
            "<org>/.github. No schedule runs it: the human or the user agent "
            "runs it (the human's gh login or the user App), and vrg-epic-audit "
            "previews it read-only."
        ),
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview: report what would sync, any broken pairs and any held advisories, "
            "change nothing, exit 0 even on a broken pair or a failed parent read "
            "(but 1 on an untrusted issue body)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
