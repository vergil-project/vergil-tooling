"""Record a security finding as a private advisory paired with a neutral issue.

The ``vrg-security-record`` command over :mod:`vergil_tooling.lib.security_records`
(epic vergil-project/.github#517). Every finding is one **pair**: a draft
repository security advisory in ``<org>/.github`` holding the details, and a
neutral ``security``-labelled intake issue carrying only a pointer to it.

- ``create`` makes the pair: duplicate check, advisory, issue in the affected
  repo's queue home, link under its intake epic, then the advisory's
  ``Tracking issue:`` line. ``--issue`` attaches the advisory to an existing
  issue instead (attach mode), and ``--resume`` finishes a pair whose advisory
  already exists. A failure after the advisory exists prints the exact command
  that finishes the pair and exits 1.
- ``list`` names advisories by GHSA ID, state, affected repo and tracked
  issues; ``--details`` adds each summary and is refused under the sweep.
- ``show`` prints one pair with its details, to the terminal only.
- ``retarget`` moves an advisory's tracking to new issues.

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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import epics, github, security_records
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
_SUB_ISSUES_JQ = '[.[] | {number, repository_url, body: (.body // "")}]'


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
        return _retarget(args)
    except _RefusedError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except security_records.PrivateHomeError as exc:
        # The message is already prefixed with the program name.
        print(str(exc), file=sys.stderr)
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


def _find_resumed_issue(intake: IssueRef, home: str, ghsa_id: str) -> IssueRef | None:
    """The intake child whose ``Security advisory:`` line names *ghsa_id*, or None.

    Reads the intake epic's sub-issues through REST, never through search,
    whose index can lag.
    """
    rows = github.read_json_paginated(
        f"repos/{intake.owner}/{intake.repo}/issues/{intake.number}/sub_issues",
        jq=_SUB_ISSUES_JQ,
    )
    line = security_records.advisory_line(home, ghsa_id)
    for row in rows:
        item: Any = row
        body = str(item.get("body") or "")
        if line in (candidate.rstrip() for candidate in body.splitlines()):
            owner, repo = str(item["repository_url"]).rstrip("/").split("/")[-2:]
            return IssueRef(owner=owner, repo=repo, number=int(item["number"]))
    return None


def _create(args: argparse.Namespace) -> int:
    org = _owner_of(args.repo)
    if args.resume is not None:
        _ghsa(args.resume, "--resume")
    attach = _full_ref(args.issue, "--issue") if args.issue is not None else None
    body = _read_file(args.body_file, "--body-file")
    details = _read_file(args.details_file, "--details-file")
    # With --resume the advisory already exists, so no summary is needed.
    summary = _summary_of(args, details) if args.resume is None else ""
    with github.target_org(org):
        home = security_records.advisory_home(org)
        queue_home = epics.queue_home(args.repo)[0]
        intake = epics.ensure_intake_epic(args.repo)
        if args.resume is None:
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
    """Every step after the advisory exists; a failure names the resume command."""
    issue = attach
    step = "finding the issue"
    try:
        if issue is not None:
            step = f"marking {issue.slug}"
            security_records.mark_issue(issue, home, ghsa_id)
            if args.resume is not None:
                step = f"linking {issue.slug} under intake epic {intake.slug}"
                _link_if_unparented(intake, issue)
        else:
            if args.resume is not None:
                issue = _find_resumed_issue(intake, home, ghsa_id)
            if issue is None:
                issue = _create_issue(args, home, queue_home, ghsa_id, body)
                step = f"linking {issue.slug} under intake epic {intake.slug}"
                epics.add_child(intake, issue)
        step = f"writing the tracking line for {issue.slug}"
        security_records.retarget(home, ghsa_id, (issue,))
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
    print(f"Recorded {ghsa_id} in {home}, tracked by {issue.slug}.")
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
    existing issue) keeps it.
    """
    if epics.native_parent_of(issue) is None:
        epics.add_child(intake, issue)


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
    targets = [_full_ref(text, "--to") for text in args.to]
    try:
        org = epics.single_target_org(*targets)
    except ValueError as exc:
        raise _RefusedError(str(exc)) from None
    with github.target_org(org):
        home = security_records.advisory_home(org)
        try:
            security_records.retarget(home, ghsa_id, targets)
        except security_records.RetargetError as exc:
            print(f"{_PROG}: {_reason(exc)}", file=sys.stderr)
            return 1
    print(f"Retargeted {ghsa_id} to {', '.join(ref.slug for ref in targets)}.")
    return 0


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

    retarget = sub.add_parser("retarget", help="Move an advisory's tracking to new issues")
    retarget.add_argument("ghsa", help="The advisory's GHSA ID")
    retarget.add_argument(
        "--to", action="append", required=True, help="Target issue owner/repo#N (repeatable)"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
